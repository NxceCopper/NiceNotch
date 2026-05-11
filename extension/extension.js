// NiceNotch Extension — UI only, delegates to daemon
// Wayland only

import GObject from 'gi://GObject';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

// ─── Constants ────────────────────────────────────────────────────────────────

const W_COLLAPSED = 160;   // pill width when closed
const W_EXPANDED  = 260;   // pill width when open
const H_PILL      = 32;    // always-visible pill height
const ANIM_MS     = 300;

// ─── Wayland guard ────────────────────────────────────────────────────────────

function checkWayland() {
    const t = GLib.getenv('XDG_SESSION_TYPE');
    if (t !== 'wayland') {
        Main.notify('NiceNotch', `Requires Wayland (got: ${t ?? 'unknown'})`);
        return false;
    }
    return true;
}

// ─── Notch widget ─────────────────────────────────────────────────────────────
//
// WHY NOT PanelMenu.Button:
//   PanelMenu.Button is a fixed-height panel widget whose internal StBin clips
//   child allocation to the panel bar height. Any attempt to grow it taller
//   (CSS transitions, ease(), set_height()) gets clipped by the parent frame.
//   We use a plain St.BoxLayout added directly to panel._centerBox instead,
//   which has no height constraint, so the expand animation works.

const NotchWidget = GObject.registerClass(
class NotchWidget extends St.BoxLayout {

    _init(commPath) {
        super._init({
            name:               'NiceNotchWidget',
            style_class:        'notch-root',
            vertical:           true,
            reactive:           true,
            track_hover:        true,
            clip_to_allocation: true,   // hides panel content while collapsed
            width:              W_COLLAPSED,
        });

        this._commPath = commPath;
        this._expanded = false;
        this._leaveId  = null;

        this._buildPill();
        this._buildPanel();
        this._connectEvents();
    }

    // ── Pill (always visible) ─────────────────────────────────────────────

    _buildPill() {
        this._pill = new St.BoxLayout({
            style_class: 'notch-pill',
            height:      H_PILL,
            x_expand:    true,
            y_expand:    false,
        });

        // Camera dot
        this._pill.add_child(new St.Widget({
            style_class: 'notch-cam-dot',
            width: 8, height: 8,
            y_align: Clutter.ActorAlign.CENTER,
        }));

        // Spacer
        this._pill.add_child(new St.Widget({ x_expand: true }));

        // Status text (hidden until transfer/playing)
        this._statusLabel = new St.Label({
            style_class: 'notch-status',
            text:        '',
            opacity:     0,
            y_align:     Clutter.ActorAlign.CENTER,
        });
        this._pill.add_child(this._statusLabel);

        this.add_child(this._pill);
    }

    // ── Expandable panel ──────────────────────────────────────────────────

    _buildPanel() {
        this._panel = new St.BoxLayout({
            style_class: 'notch-panel',
            vertical:    true,
            x_expand:    true,
            height:      0,     // collapsed — animated to natural height on open
            opacity:     0,
        });

        // ── Section: Send file ──
        this._panel.add_child(new St.Label({
            style_class: 'notch-section-label',
            text:        'SEND FILE TO',
        }));

        this._deviceList = new St.BoxLayout({
            style_class: 'notch-device-list',
            vertical:    true,
            x_expand:    true,
        });
        this._noDevLabel = new St.Label({
            style_class: 'notch-no-devices',
            text:        'No nearby devices',
        });
        this._deviceList.add_child(this._noDevLabel);
        this._panel.add_child(this._deviceList);

        // ── Divider ──
        this._panel.add_child(new St.Widget({
            style_class: 'notch-divider',
            height: 1, x_expand: true,
        }));

        // ── Section: Quick actions ──
        this._panel.add_child(new St.Label({
            style_class: 'notch-section-label',
            text:        'QUICK ACTIONS',
        }));

        const actions = [
            { icon: '⚙', label: 'Settings',    cmd: 'show-settings'  },
            { icon: '📋', label: 'Clipboard',   cmd: 'show-clipboard' },
        ];
        for (const a of actions) {
            const btn = new St.Button({
                style_class: 'notch-action-btn',
                x_align:     Clutter.ActorAlign.FILL,
                x_expand:    true,
                reactive:    true,
            });
            const row = new St.BoxLayout({ x_expand: true });
            row.add_child(new St.Label({ text: a.icon, style_class: 'notch-action-icon' }));
            row.add_child(new St.Label({
                text: a.label, style_class: 'notch-action-label',
                x_expand: true, y_align: Clutter.ActorAlign.CENTER,
            }));
            btn.set_child(row);
            btn.connect('clicked', () => {
                this._signalDaemon(a.cmd);
                this._collapse();
            });
            this._panel.add_child(btn);
        }

        this.add_child(this._panel);
    }

    // ── Events ────────────────────────────────────────────────────────────

    _connectEvents() {
        this.connect('button-press-event', (_a, ev) => {
            if (ev.get_button() === Clutter.BUTTON_PRIMARY) {
                this._expanded ? this._collapse() : this._expand();
                return Clutter.EVENT_STOP;
            }
            if (ev.get_button() === 3) {
                this._signalDaemon('show-settings');
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        this.connect('leave-event', () => {
            if (this._leaveId) GLib.source_remove(this._leaveId);
            this._leaveId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 700, () => {
                this._leaveId = null;
                if (!this.hover) this._collapse();
                return GLib.SOURCE_REMOVE;
            });
        });

        this.connect('enter-event', () => {
            if (this._leaveId) {
                GLib.source_remove(this._leaveId);
                this._leaveId = null;
            }
        });
    }

    // ── Expand / Collapse ─────────────────────────────────────────────────

    _expand() {
        if (this._expanded) return;
        this._expanded = true;

        // Measure natural height of panel content before animating
        const [, natH] = this._panel.get_preferred_height(-1);

        this.ease({
            width:    W_EXPANDED,
            duration: ANIM_MS,
            mode:     Clutter.AnimationMode.EASE_OUT_EXPO,
        });
        this._panel.ease({
            height:   natH,
            opacity:  255,
            duration: ANIM_MS,
            mode:     Clutter.AnimationMode.EASE_OUT_EXPO,
        });

        this._recenter(W_EXPANDED);
        this._notch_pill_set_state('open');
    }

    _collapse() {
        if (!this._expanded) return;
        this._expanded = false;

        this._panel.ease({
            height:   0,
            opacity:  0,
            duration: ANIM_MS - 60,
            mode:     Clutter.AnimationMode.EASE_IN_QUAD,
        });
        this.ease({
            width:    W_COLLAPSED,
            duration: ANIM_MS,
            mode:     Clutter.AnimationMode.EASE_IN_OUT_QUAD,
            onComplete: () => this._recenter(W_COLLAPSED),
        });

        this._notch_pill_set_state('closed');
    }

    _recenter(targetW) {
        const mon = Main.layoutManager.primaryMonitor;
        if (!mon) return;
        const x = mon.x + Math.floor((mon.width - targetW) / 2);
        this.ease({ x, duration: ANIM_MS, mode: Clutter.AnimationMode.EASE_OUT_EXPO });
    }

    _notch_pill_set_state(state) {
        if (state === 'open') {
            this._pill.remove_style_class_name('notch-pill-closed');
            this._pill.add_style_class_name('notch-pill-open');
        } else {
            this._pill.remove_style_class_name('notch-pill-open');
            this._pill.add_style_class_name('notch-pill-closed');
        }
    }

    // ── Status label ──────────────────────────────────────────────────────

    showStatus(text, ms = 3000) {
        this._statusLabel.set_text(text);
        this._statusLabel.ease({ opacity: 255, duration: 150 });
        if (this._statusTimer) GLib.source_remove(this._statusTimer);
        this._statusTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._statusLabel.ease({ opacity: 0, duration: 300 });
            this._statusTimer = null;
            return GLib.SOURCE_REMOVE;
        });
    }

    // ── Device list (called by extension when daemon reports targets) ──────

    setDevices(devices) {
        this._deviceList.destroy_all_children();

        if (!devices || devices.length === 0) {
            this._deviceList.add_child(new St.Label({
                style_class: 'notch-no-devices',
                text:        'No nearby devices',
            }));
            return;
        }

        const ICONS = { bluetooth: '🔵', quickshare: '📡', lan: '🌐' };
        for (const d of devices) {
            const btn = new St.Button({
                style_class: 'notch-device-btn',
                x_expand:    true,
                reactive:    true,
            });
            const row = new St.BoxLayout({ x_expand: true });
            row.add_child(new St.Label({
                text: ICONS[d.backend] || '📁',
                style_class: 'notch-device-icon',
                y_align: Clutter.ActorAlign.CENTER,
            }));
            row.add_child(new St.Label({
                text: d.name, style_class: 'notch-device-name',
                x_expand: true, y_align: Clutter.ActorAlign.CENTER,
            }));
            btn.set_child(row);
            btn.connect('clicked', () => {
                this._signalDaemon(`send-to:${d.id}`);
                this.showStatus('⬆ Sending…');
            });
            this._deviceList.add_child(btn);
        }
    }

    // ── Daemon communication (file-based) ─────────────────────────────────

    _signalDaemon(command) {
        const f = Gio.File.new_for_path(this._commPath);
        try {
            const bytes = new TextEncoder().encode(
                JSON.stringify({ cmd: command, time: Date.now() })
            );
            f.replace_contents(bytes, null, false, Gio.FileCreateFlags.NONE, null);
        } catch (e) {
            console.log(`NiceNotch: daemon signal failed (${command}): ${e}`);
        }
    }

    destroy() {
        if (this._leaveId)   { GLib.source_remove(this._leaveId);   this._leaveId = null; }
        if (this._statusTimer) { GLib.source_remove(this._statusTimer); this._statusTimer = null; }
        super.destroy();
    }
});

// ─── Extension ────────────────────────────────────────────────────────────────

export default class NiceNotchExtension extends Extension {

    enable() {
        if (!checkWayland()) return;

        const commDir  = GLib.get_home_dir() + '/.cache/nicenotch';
        const commPath = commDir + '/comm';
        GLib.mkdir_with_parents(commDir, 0o755);

        // Build widget
        this._widget = new NotchWidget(commPath);

        // Add directly to the panel's center box — NOT via addToStatusArea.
        // addToStatusArea wraps the widget in a fixed-height panel button frame
        // which clips any expansion. Direct insertion has no height constraint.
        Main.panel._centerBox.add_child(this._widget);

        // Initial position (center of primary monitor)
        this._reposition();
        this._monitorId = Main.layoutManager.connect(
            'monitors-changed', () => this._reposition()
        );

        // Poll daemon response file for device updates
        this._pollDaemon(commPath);
    }

    disable() {
        if (this._monitorId) {
            Main.layoutManager.disconnect(this._monitorId);
            this._monitorId = null;
        }
        if (this._pollTimerId) {
            GLib.source_remove(this._pollTimerId);
            this._pollTimerId = null;
        }
        if (this._widget) {
            Main.panel._centerBox.remove_child(this._widget);
            this._widget.destroy();
            this._widget = null;
        }
    }

    _reposition() {
        if (!this._widget) return;
        const mon = Main.layoutManager.primaryMonitor;
        if (!mon) return;
        this._widget.set_position(
            mon.x + Math.floor((mon.width - W_COLLAPSED) / 2),
            mon.y,
        );
    }

    // Poll the daemon response file for JSON updates (devices list, status)
    _pollDaemon(commPath) {
        const respPath = commPath.replace('/comm', '/response');
        let lastMtime = 0;

        this._pollTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1500, () => {
            try {
                const f    = Gio.File.new_for_path(respPath);
                const info = f.query_info('time::modified', Gio.FileQueryInfoFlags.NONE, null);
                const mtime = info.get_attribute_uint64('time::modified');
                if (mtime !== lastMtime) {
                    lastMtime = mtime;
                    const [ok, contents] = f.load_contents(null);
                    if (ok) {
                        const data = JSON.parse(new TextDecoder().decode(contents));
                        if (data.devices && this._widget)
                            this._widget.setDevices(data.devices);
                        if (data.status && this._widget)
                            this._widget.showStatus(data.status);
                    }
                }
            } catch (_) { /* daemon not running yet */ }
            return GLib.SOURCE_CONTINUE;
        });
    }
}