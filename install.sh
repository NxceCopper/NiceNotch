#!/bin/bash
# Install NiceNotch v2

echo "Installing NiceNotch v2..."

# Create directories
mkdir -p ~/.local/share/gnome-shell/extensions/NiceNotch-v2@nice.local
mkdir -p ~/.cache/nicenotch

# Install extension
cp -r extension/* ~/.local/share/gnome-shell/extensions/NiceNotch-v2@nice.local/

# Install daemon
mkdir -p ~/.local/bin
cp daemon/daemon.py ~/.local/bin/nicenotch-daemon
chmod +x ~/.local/bin/nicenotch-daemon

# Install systemd service
mkdir -p ~/.config/systemd/user
cp daemon/nicenotch-daemon.service ~/.config/systemd/user/

# Enable and start service
systemctl --user daemon-reload
systemctl --user enable nicenotch-daemon
systemctl --user start nicenotch-daemon

# Enable extension
gnome-extensions enable NiceNotch-v2@nice.local

echo "Done! Log out and back in to see the notch."
echo "Or restart GNOME Shell: Alt+F2, type 'r', press Enter"
