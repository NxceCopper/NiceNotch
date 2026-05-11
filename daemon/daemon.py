#!/usr/bin/env python3
"""
NiceNotch Daemon v6 — GTK3 GUI + file sharing backend

The daemon is now the FULL GUI. The GNOME Shell extension is just a thin
pill indicator that signals this process when clicked.

Flow:
  1. Extension writes {cmd:"toggle-menu", notch_x, notch_y, notch_w} to comm file
  2. Daemon shows/hides its GTK window positioned right below the notch
  3. GTK window handles all UI: devices, file sharing, audio, actions

Window positioning:
  - Uses gtk-layer-shell if installed (precise Wayland overlay positioning)
  - Falls back to window.move() for regular Wayland windows

Z-order guarantee:
  - Notch is in Main.panel._centerBox → panel z-level
  - This GTK window uses layer-shell OVERLAY layer OR keep_above=True
  - Either way, GTK window is always above the Shell panel
"""

import gi
gi.require_version('Gtk',  '3.0')
gi.require_version('Gdk',  '3.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gtk, Gdk, GLib, Gio, GObject

import threading, http.server, socket, os, json, uuid, time
import urllib.request, logging, shutil, subprocess

# Optional: gtk-layer-shell for precise Wayland overlay positioning
try:
    gi.require_version('GtkLayerShell', '0.1')
    from gi.repository import GtkLayerShell
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False

logging.basicConfig(
    level=logging.INFO,
    format='[NiceNotch %(asctime)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('nicenotch')

# ─── Paths ────────────────────────────────────────────────────────────────────

HOME       = os.path.expanduser('~')
CACHE_DIR  = os.path.join(HOME, '.cache', 'nicenotch')
COMM_FILE  = os.path.join(CACHE_DIR, 'comm')
RESP_FILE  = os.path.join(CACHE_DIR, 'response')
SAVE_DIR   = os.path.join(HOME, 'Downloads', 'NiceNotch')

MENU_W     = 280
NOTCH_H    = 32   # assumed panel height

# ─── CSS ──────────────────────────────────────────────────────────────────────

CSS = b"""
window, .nicenotch-window {
    background-color: rgba(12, 12, 16, 0.92);
    border-radius: 0 0 20px 20px;
    border: 1px solid rgba(255,255,255,0.10);
    border-top: none;
    color: #ffffff;
}

/* Pill strip at top — matches the notch pill */
.menu-top-strip {
    background-color: rgba(0,0,0,0.0);
    padding: 6px 14px 2px 14px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
}

.section-label {
    font-size: 9px;
    font-weight: 700;
    color: rgba(255,255,255,0.30);
    padding: 10px 14px 4px 14px;
    letter-spacing: 1px;
}

/* ── Device rows ── */
.device-row {
    padding: 8px 14px;
    border-radius: 9px;
    margin: 1px 8px;
    transition: background 120ms;
}
.device-row:hover {
    background-color: rgba(255,255,255,0.07);
}

.device-icon {
    font-size: 15px;
    margin-right: 8px;
}

.device-name {
    font-size: 13px;
    font-weight: 500;
    color: #ffffff;
}

.device-backend {
    font-size: 10px;
    color: rgba(255,255,255,0.35);
}

.no-devices {
    font-size: 11px;
    color: rgba(255,255,255,0.30);
    padding: 8px 14px;
}

/* ── Audio section ── */
.audio-box {
    padding: 8px 14px 4px 14px;
}

.track-title {
    font-size: 13px;
    font-weight: 600;
    color: #ffffff;
}

.track-artist {
    font-size: 11px;
    color: rgba(255,255,255,0.50);
}

.ctrl-btn {
    background: rgba(255,255,255,0.07);
    border: none;
    border-radius: 8px;
    color: #ffffff;
    font-size: 14px;
    padding: 5px 10px;
    min-width: 34px;
    transition: background 100ms;
}
.ctrl-btn:hover { background: rgba(255,255,255,0.14); }
.ctrl-btn:active { background: rgba(255,255,255,0.22); }

/* ── Divider ── */
.divider {
    background-color: rgba(255,255,255,0.07);
    margin: 6px 10px;
}

/* ── Action buttons ── */
.action-row {
    padding: 7px 14px;
    border-radius: 9px;
    margin: 1px 8px;
}
.action-row:hover { background-color: rgba(255,255,255,0.07); }

.action-label {
    font-size: 12px;
    font-weight: 500;
    color: rgba(255,255,255,0.85);
}

.drop-hint {
    font-size: 11px;
    color: rgba(0,220,80,0.85);
    font-weight: 600;
    padding: 6px 14px;
}

/* ── Status bar ── */
.status-bar {
    font-size: 10px;
    color: rgba(255,255,255,0.35);
    padding: 4px 14px 8px 14px;
}
"""

# ─── HTTP receive handler ─────────────────────────────────────────────────────

class ReceiveHandler(http.server.BaseHTTPRequestHandler):
    daemon = None

    def do_POST(self):
        if self.path != '/receive': self._reply(404); return
        sender   = self.headers.get('X-Sender', 'Unknown')
        filename = os.path.basename(self.headers.get('X-Filename', 'file'))
        length   = int(self.headers.get('Content-Length', 0))
        if not length: self._reply(400); return
        data = self.rfile.read(length)
        os.makedirs(SAVE_DIR, exist_ok=True)
        dest = _unique_path(os.path.join(SAVE_DIR, filename))
        with open(dest, 'wb') as f: f.write(data)
        self._reply(200)
        log.info(f'Received "{filename}" from {sender}')
        if self.daemon:
            GLib.idle_add(self.daemon._on_file_received, sender, filename, dest)

    def do_GET(self):
        if self.path == '/info':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'name': socket.gethostname(), 'v': 1}).encode())
        else: self._reply(404)

    def _reply(self, code):
        self.send_response(code); self.end_headers()
    def log_message(self, *a): pass


# ─── GTK Menu Window ──────────────────────────────────────────────────────────

class MenuWindow:
    """
    The dropdown that appears below the notch when clicked.
    
    Z-order: Shell panel is below this window always because:
      - With layer-shell: OVERLAY layer sits above everything
      - Without: keep_above=True puts it above the panel
    """

    def __init__(self, daemon):
        self.daemon  = daemon
        self.visible = False
        self._build()

    def _build(self):
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title('NiceNotch Menu')
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_default_size(MENU_W, -1)

        # Transparent background so CSS border-radius shows
        screen = self.win.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.win.set_visual(visual)
        self.win.set_app_paintable(True)

        # Layer shell setup (if available) — places window in OVERLAY layer
        if HAS_LAYER_SHELL:
            GtkLayerShell.init_for_window(self.win)
            GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.TOP,    True)
            GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.LEFT,   False)
            GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.RIGHT,  False)
            GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.TOP, NOTCH_H)
            GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.ON_DEMAND)
            log.info('Using gtk-layer-shell OVERLAY layer')
        else:
            self.win.set_keep_above(True)
            self.win.set_type_hint(Gdk.WindowTypeHint.DROPDOWN_MENU)
            log.info('gtk-layer-shell not found — using keep_above')

        # Hide on focus out
        self.win.connect('focus-out-event', lambda *_: self.hide())
        self.win.connect('draw', self._on_draw)
        self.win.connect('key-press-event', self._on_key)

        # ── Build UI ──
        self._outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._outer.get_style_context().add_class('nicenotch-window')
        self.win.add(self._outer)

        self._build_audio_section()
        self._add_divider()
        self._build_device_section()
        self._add_divider()
        self._build_actions_section()
        self._build_status_bar()

        self.win.show_all()
        self.win.hide()

    def _on_draw(self, widget, cr):
        # Fill with transparent so CSS border-radius clips properly
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(1)  # OPERATOR_SOURCE
        cr.paint()
        cr.set_operator(0)  # OPERATOR_OVER
        return False

    def _on_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
        return False

    # ── Audio ──────────────────────────────────────────────────────────────

    def _build_audio_section(self):
        lbl = Gtk.Label(label='NOW PLAYING')
        lbl.get_style_context().add_class('section-label')
        lbl.set_xalign(0)
        self._outer.pack_start(lbl, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.get_style_context().add_class('audio-box')
        self._outer.pack_start(box, False, False, 0)

        self._track_title  = Gtk.Label(label='Nothing playing')
        self._track_title.get_style_context().add_class('track-title')
        self._track_title.set_xalign(0)
        self._track_title.set_ellipsize(3)  # PANGO_ELLIPSIZE_END

        self._track_artist = Gtk.Label(label='')
        self._track_artist.get_style_context().add_class('track-artist')
        self._track_artist.set_xalign(0)

        box.pack_start(self._track_title,  False, False, 0)
        box.pack_start(self._track_artist, False, False, 0)

        # Controls row
        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ctrl.set_margin_top(6)

        for icon, action in [('⏮', 'Previous'), ('▶', 'PlayPause'), ('⏭', 'Next')]:
            b = Gtk.Button(label=icon)
            b.get_style_context().add_class('ctrl-btn')
            b.connect('clicked', lambda _, a=action: self.daemon._mpris_action(a))
            ctrl.pack_start(b, False, False, 0)
            if action == 'PlayPause':
                self._play_btn = b

        # spacer
        ctrl.pack_start(Gtk.Box(), True, True, 0)

        vol_down = Gtk.Button(label='🔈')
        vol_up   = Gtk.Button(label='🔊')
        vol_down.get_style_context().add_class('ctrl-btn')
        vol_up.get_style_context().add_class('ctrl-btn')
        vol_down.connect('clicked', lambda _: self.daemon._adjust_volume(-0.1))
        vol_up.connect('clicked',   lambda _: self.daemon._adjust_volume(+0.1))
        ctrl.pack_start(vol_down, False, False, 0)
        ctrl.pack_start(vol_up,   False, False, 0)

        box.pack_start(ctrl, False, False, 0)

    def update_audio(self, title, artist, playing):
        GLib.idle_add(self._track_title.set_text,  title  or 'Nothing playing')
        GLib.idle_add(self._track_artist.set_text, artist or '')
        GLib.idle_add(self._play_btn.set_label, '⏸' if playing else '▶')

    # ── Devices ────────────────────────────────────────────────────────────

    def _build_device_section(self):
        lbl = Gtk.Label(label='SEND FILE TO')
        lbl.get_style_context().add_class('section-label')
        lbl.set_xalign(0)
        self._outer.pack_start(lbl, False, False, 0)

        self._device_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._outer.pack_start(self._device_box, False, False, 0)

        self._no_dev_label = Gtk.Label(label='No nearby devices')
        self._no_dev_label.get_style_context().add_class('no-devices')
        self._no_dev_label.set_xalign(0)
        self._device_box.pack_start(self._no_dev_label, False, False, 0)

    def rebuild_devices(self, targets: dict):
        # Clear existing rows (except the "no devices" label)
        for child in self._device_box.get_children():
            self._device_box.remove(child)

        ICONS = {'bluetooth': '🔵', 'quickshare': '📡', 'lan': '🌐'}
        LABELS = {'bluetooth': 'Bluetooth', 'quickshare': 'QuickShare', 'lan': 'WiFi LAN'}

        if not targets:
            self._device_box.pack_start(self._no_dev_label, False, False, 0)
            self._device_box.show_all()
            return

        for tid, target in targets.items():
            backend = target.get('backend', 'lan')
            icon    = ICONS.get(backend, '📁')
            blabel  = LABELS.get(backend, backend)

            # Row as EventBox for hover
            evbox = Gtk.EventBox()
            evbox.get_style_context().add_class('device-row')

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            icon_lbl = Gtk.Label(label=icon)
            icon_lbl.get_style_context().add_class('device-icon')

            name_lbl = Gtk.Label(label=target['name'])
            name_lbl.get_style_context().add_class('device-name')
            name_lbl.set_xalign(0)

            back_lbl = Gtk.Label(label=blabel)
            back_lbl.get_style_context().add_class('device-backend')

            row.pack_start(icon_lbl,  False, False, 0)
            row.pack_start(name_lbl,  True,  True,  0)
            row.pack_end  (back_lbl,  False, False, 8)

            evbox.add(row)

            # Click → open file picker via portal, then send
            evbox.connect('button-press-event',
                lambda _, e, t=target: self._on_device_click(t))

            self._device_box.pack_start(evbox, False, False, 0)

        self._device_box.show_all()

    def _on_device_click(self, target):
        self.hide()
        if target.get('backend') == 'quickshare':
            self.daemon._launch_rquickshare()
        else:
            self.daemon._pick_and_send(target)

    # ── Actions ────────────────────────────────────────────────────────────

    def _build_actions_section(self):
        lbl = Gtk.Label(label='QUICK ACTIONS')
        lbl.get_style_context().add_class('section-label')
        lbl.set_xalign(0)
        self._outer.pack_start(lbl, False, False, 0)

        actions = [
            ('⚙  Settings',    self.daemon._open_settings),
            ('📂  Open receive folder', self.daemon._open_receive_folder),
        ]
        for label, cb in actions:
            evbox = Gtk.EventBox()
            evbox.get_style_context().add_class('action-row')
            row_lbl = Gtk.Label(label=label)
            row_lbl.get_style_context().add_class('action-label')
            row_lbl.set_xalign(0)
            evbox.add(row_lbl)
            evbox.connect('button-press-event', lambda _, e, f=cb: (f(), self.hide()))
            self._outer.pack_start(evbox, False, False, 0)

    # ── Status bar ─────────────────────────────────────────────────────────

    def _build_status_bar(self):
        self._status_lbl = Gtk.Label(label='Ready')
        self._status_lbl.get_style_context().add_class('status-bar')
        self._status_lbl.set_xalign(0)
        self._outer.pack_start(self._status_lbl, False, False, 0)

    def set_status(self, text):
        GLib.idle_add(self._status_lbl.set_text, text)

    # ── Divider ────────────────────────────────────────────────────────────

    def _add_divider(self):
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.get_style_context().add_class('divider')
        self._outer.pack_start(sep, False, False, 0)

    # ── Show / hide ────────────────────────────────────────────────────────

    def toggle(self, notch_x=None, notch_y=None, notch_w=None):
        if self.visible:
            self.hide()
        else:
            self.show(notch_x, notch_y, notch_w)

    def show(self, notch_x=None, notch_y=None, notch_w=None):
        if not HAS_LAYER_SHELL and notch_x is not None:
            # Position window centered below the notch
            x = int(notch_x + (notch_w or 160) / 2 - MENU_W / 2)
            y = int((notch_y or 0) + NOTCH_H)
            self.win.move(x, y)

        self.win.show_all()
        self.win.present()
        self.visible = True

    def hide(self, *_):
        self.win.hide()
        self.visible = False

# ─── Daemon ────────────────────────────────────────────────────────────────────

class NiceNotchDaemon:

    def __init__(self):
        self.targets    = {}
        self.http_port  = _free_port()
        self._sysbus    = None
        self._mpris_player = None
        ReceiveHandler.daemon = self

    # ── Startup ────────────────────────────────────────────────────────────

    def run(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        os.makedirs(SAVE_DIR,  exist_ok=True)

        Gtk.init([])
        self._load_css()

        self._menu = MenuWindow(self)
        self._start_http()

        # Watch comm file for commands from the Shell extension
        self._watch_comm()

        # Backend discovery
        GLib.timeout_add(500,  self._start_bluetooth)
        GLib.timeout_add(900,  self._start_avahi)
        GLib.timeout_add(1300, self._add_quickshare_target)

        # MPRIS poll
        GLib.timeout_add(2000, self._poll_mpris)

        log.info(f'NiceNotch daemon v6 ready (HTTP :{self.http_port})')
        Gtk.main()

    def _load_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # ── Comm file watcher ──────────────────────────────────────────────────
    # Extension writes JSON to COMM_FILE; we poll it every 200ms.
    # Using Gio.FileMonitor would be more elegant but file monitors on
    # tmpfs/cache can be unreliable; polling is simple and works.

    def _watch_comm(self):
        self._comm_mtime = 0
        GLib.timeout_add(200, self._check_comm)

    def _check_comm(self):
        try:
            stat = os.stat(COMM_FILE)
            if stat.st_mtime_ns != self._comm_mtime:
                self._comm_mtime = stat.st_mtime_ns
                with open(COMM_FILE) as f:
                    data = json.load(f)
                self._handle_command(data)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return GLib.SOURCE_CONTINUE

    def _handle_command(self, data):
        cmd = data.get('cmd', '')
        log.debug(f'Command: {cmd}')

        if cmd in ('toggle-menu', 'show-menu'):
            nx = data.get('notch_x')
            ny = data.get('notch_y')
            nw = data.get('notch_w')
            GLib.idle_add(self._menu.toggle, nx, ny, nw)

        elif cmd == 'show-settings':
            GLib.idle_add(self._open_settings)

        elif cmd.startswith('send-to:'):
            tid  = cmd[8:]
            path = data.get('file_path', '')
            if path and tid in self.targets:
                threading.Thread(
                    target=self._send_to_target,
                    args=(tid, path), daemon=True
                ).start()

    # ── Backend: responses back to extension ───────────────────────────────

    def _write_response(self, data: dict):
        try:
            with open(RESP_FILE, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            log.error(f'Response write: {e}')

    def _on_file_received(self, sender, filename, dest):
        self._menu.set_status(f'⬇ Received "{filename}" from {sender}')
        self._write_response({'status': f'⬇ "{filename}" from {sender}', 'saved': dest})
        return False

    # ── Audio: MPRIS2 ──────────────────────────────────────────────────────

    def _poll_mpris(self):
        try:
            sess = Gio.DBus.get_sync(Gio.BusType.SESSION, None)
            r = sess.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                'org.freedesktop.DBus', 'ListNames',
                None, GLib.VariantType('(as)'), Gio.DBusCallFlags.NONE, 2000, None)
            names = r.unpack()[0]
            player = next((n for n in names if n.startswith('org.mpris.MediaPlayer2.')), None)
            if player:
                self._mpris_player = player
                self._fetch_mpris(player)
        except Exception: pass
        GLib.timeout_add(5000, self._poll_mpris)  # re-schedule
        return GLib.SOURCE_REMOVE

    def _fetch_mpris(self, player):
        try:
            sess = Gio.DBus.get_sync(Gio.BusType.SESSION, None)
            r = sess.call_sync(player, '/org/mpris/MediaPlayer2',
                'org.freedesktop.DBus.Properties', 'GetAll',
                GLib.Variant('(s)', ('org.mpris.MediaPlayer2.Player',)),
                GLib.VariantType('(a{sv})'), Gio.DBusCallFlags.NONE, 3000, None)
            props = r.unpack()[0]
            meta    = props.get('Metadata', GLib.Variant('a{sv}', {})).unpack()
            status  = props.get('PlaybackStatus', GLib.Variant('s', 'Stopped')).unpack()
            title   = meta.get('xesam:title',  GLib.Variant('s', '')).unpack()
            artists = meta.get('xesam:artist', GLib.Variant('as', [])).unpack()
            artist  = artists[0] if artists else ''
            playing = status == 'Playing'
            GLib.idle_add(self._menu.update_audio, title, artist, playing)
        except Exception: pass

    def _mpris_action(self, action):
        if not self._mpris_player: return
        try:
            sess = Gio.DBus.get_sync(Gio.BusType.SESSION, None)
            sess.call_sync(self._mpris_player, '/org/mpris/MediaPlayer2',
                'org.mpris.MediaPlayer2.Player', action,
                None, None, Gio.DBusCallFlags.NONE, 2000, None)
            GLib.timeout_add(300, lambda: (self._fetch_mpris(self._mpris_player), False)[1])
        except Exception as e:
            log.error(f'MPRIS {action}: {e}')

    def _adjust_volume(self, delta):
        pct  = abs(int(delta * 100))
        sign = '+' if delta > 0 else '-'
        try:
            subprocess.run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', f'{sign}{pct}%'],
                           check=False)
        except Exception: pass

    # ── File picker (native portal) ────────────────────────────────────────

    def _pick_and_send(self, target):
        dialog = Gtk.FileChooserDialog(
            title=f'Send to {target["name"]}',
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            'Send', Gtk.ResponseType.OK,
        )
        dialog.set_select_multiple(True)
        dialog.set_decorated(False)

        def _response(d, resp):
            if resp == Gtk.ResponseType.OK:
                files = d.get_filenames()
                for f in files:
                    threading.Thread(
                        target=self._send_to_target,
                        args=(target['id'], f), daemon=True
                    ).start()
            d.destroy()

        dialog.connect('response', _response)
        dialog.show()

    # ── File transfer: LAN HTTP ────────────────────────────────────────────

    def _send_to_target(self, target_id, fpath):
        target = self.targets.get(target_id)
        if not target:
            GLib.idle_add(self._menu.set_status, '✗ Device not found')
            return

        filename = os.path.basename(fpath)
        backend  = target.get('backend', 'lan')

        GLib.idle_add(self._menu.set_status, f'⬆ Sending {filename}…')

        try:
            if backend == 'bluetooth':
                self._send_bluetooth(target, fpath)
            elif backend == 'quickshare':
                self._launch_rquickshare()
            else:
                self._send_lan(target, fpath)
            GLib.idle_add(self._menu.set_status, f'✓ Sent {filename}')
        except Exception as e:
            log.error(f'Send error: {e}')
            GLib.idle_add(self._menu.set_status, f'✗ Failed: {e}')

    def _send_lan(self, target, fpath):
        filename = os.path.basename(fpath)
        with open(fpath, 'rb') as f: data = f.read()
        url = f"http://{target['address']}:{target['port']}/receive"
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('X-Sender',   socket.gethostname())
        req.add_header('X-Filename', filename)
        req.add_header('Content-Length', str(len(data)))
        with urllib.request.urlopen(req, timeout=30): pass

    def _send_bluetooth(self, target, fpath):
        sess = Gio.DBus.get_sync(Gio.BusType.SESSION, None)
        r = sess.call_sync('org.bluez.obex', '/org/bluez/obex',
            'org.bluez.obex.Client1', 'CreateSession',
            GLib.Variant('(sa{sv})', [target['address'], {'Target': GLib.Variant('s', 'opp')}]),
            GLib.VariantType('(o)'), Gio.DBusCallFlags.NONE, 30000, None)
        session_path = r.unpack()[0]
        sess.call_sync('org.bluez.obex', session_path,
            'org.bluez.obex.ObjectPush1', 'SendFile',
            GLib.Variant('(s)', (fpath,)),
            GLib.VariantType('(oo)'), Gio.DBusCallFlags.NONE, 60000, None)

    # ── Actions ────────────────────────────────────────────────────────────

    def _open_settings(self):
        subprocess.Popen(['gnome-control-center'], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _open_receive_folder(self):
        subprocess.Popen(['xdg-open', SAVE_DIR], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ── Backend: LAN HTTP ──────────────────────────────────────────────────

    def _start_http(self):
        srv = http.server.HTTPServer(('0.0.0.0', self.http_port), ReceiveHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info(f'LAN HTTP :{self.http_port}')

    def _start_avahi(self):
        try:
            if not self._sysbus:
                self._sysbus = Gio.DBus.get_sync(Gio.BusType.SYSTEM, None)
            self._avahi_advertise()
            self._avahi_browse()
            log.info('Avahi ready')
        except Exception as e:
            log.warning(f'Avahi: {e}')
        return False

    def _av(self, obj, iface, method, params=None, ret=None):
        return self._sysbus.call_sync('org.freedesktop.Avahi', obj, iface, method,
            params, ret, Gio.DBusCallFlags.NONE, 5000, None)

    def _avahi_advertise(self):
        grp = self._av('/', 'org.freedesktop.Avahi.Server', 'EntryGroupNew',
            ret=GLib.VariantType('(o)')).unpack()[0]
        self._av(grp, 'org.freedesktop.Avahi.EntryGroup', 'AddService',
            GLib.Variant('(iiussssqaay)', (-1,-1,0,socket.gethostname(),
                '_nicenotch._tcp','local','',self.http_port,
                [f'port={self.http_port}'.encode()])))
        self._av(grp, 'org.freedesktop.Avahi.EntryGroup', 'Commit')

    def _avahi_browse(self):
        bp = self._av('/', 'org.freedesktop.Avahi.Server', 'ServiceBrowserNew',
            GLib.Variant('(iissu)', (-1,-1,'_nicenotch._tcp','local',0)),
            GLib.VariantType('(o)')).unpack()[0]
        for sig, cb in [('ItemNew', self._on_lan_new), ('ItemRemove', self._on_lan_remove)]:
            self._sysbus.signal_subscribe('org.freedesktop.Avahi',
                'org.freedesktop.Avahi.ServiceBrowser', sig, bp, None,
                Gio.DBusSignalFlags.NONE, cb, None)

    def _on_lan_new(self, conn, *args):
        params = args[-2]
        iface_, proto, name, svc_type, domain, flags = params.unpack()
        if name == socket.gethostname(): return
        try:
            _, _, _, _, _, address, port, _, _ = self._av('/',
                'org.freedesktop.Avahi.Server', 'ResolveService',
                GLib.Variant('(iisssiu)', (iface_,proto,name,svc_type,domain,-1,0))).unpack()
            tid = f'lan_{name}_{address}'
            self.targets[tid] = {'id':tid,'name':name,'backend':'lan','address':address,'port':port}
            log.info(f'LAN: {name}')
            GLib.idle_add(self._menu.rebuild_devices, self.targets)
        except Exception as e: log.error(f'Avahi: {e}')

    def _on_lan_remove(self, conn, *args):
        params = args[-2]
        _, _, name, *_ = params.unpack()
        for tid in [k for k,v in list(self.targets.items())
                    if v.get('name')==name and v.get('backend')=='lan']:
            del self.targets[tid]
        GLib.idle_add(self._menu.rebuild_devices, self.targets)

    # ── Backend: Bluetooth ─────────────────────────────────────────────────

    def _start_bluetooth(self):
        try:
            if not self._sysbus:
                self._sysbus = Gio.DBus.get_sync(Gio.BusType.SYSTEM, None)
            objs = self._sysbus.call_sync(
                'org.bluez', '/', 'org.freedesktop.DBus.ObjectManager',
                'GetManagedObjects', None, GLib.VariantType('(a{oa{sa{sv}}})'),
                Gio.DBusCallFlags.NONE, 3000, None).unpack()[0]
            for path, ifaces in objs.items():
                dev = ifaces.get('org.bluez.Device1')
                if not dev: continue
                addr   = dev.get('Address')
                name   = dev.get('Name') or dev.get('Alias') or addr
                paired = dev.get('Paired',    GLib.Variant('b', False)).unpack()
                conn_  = dev.get('Connected', GLib.Variant('b', False)).unpack()
                if not (paired or conn_) or not addr: continue
                tid = f'bt_{addr}'
                if tid not in self.targets:
                    self.targets[tid] = {'id':tid,'name':str(name),'backend':'bluetooth','address':str(addr)}
            GLib.idle_add(self._menu.rebuild_devices, self.targets)
            log.info('Bluetooth scanned')
        except Exception as e:
            log.warning(f'Bluetooth: {e}')
        return False

    # ── QuickShare ─────────────────────────────────────────────────────────

    def _add_quickshare_target(self):
        if shutil.which('r-quick-share') or shutil.which('rquickshare'):
            tid = 'quickshare_rqs'
            self.targets[tid] = {'id':tid,'name':'Android (QuickShare)','backend':'quickshare'}
            GLib.idle_add(self._menu.rebuild_devices, self.targets)
        return False

    def _launch_rquickshare(self):
        for cmd in ('r-quick-share', 'rquickshare'):
            if shutil.which(cmd):
                subprocess.Popen([cmd], start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return


# ─── Utilities ────────────────────────────────────────────────────────────────

def _free_port():
    with socket.socket() as s:
        s.bind(('', 0)); return s.getsockname()[1]

def _unique_path(p):
    base, ext = os.path.splitext(p); n = 1
    while os.path.exists(p): p = f'{base}_{n}{ext}'; n += 1
    return p


if __name__ == '__main__':
    NiceNotchDaemon().run()