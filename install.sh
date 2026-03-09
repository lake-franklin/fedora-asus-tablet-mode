#!/usr/bin/env bash
# install.sh — sets up tablet-mode-daemon for the current user
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
SERVICE_DIR="$HOME/.config/systemd/user"
STATE_DIR="$HOME/.local/share/tablet-mode"
RULES_DST="/etc/udev/rules.d/99-input-inhibit.rules"

echo "=== tablet-mode-daemon installer ==="

# Create directories
mkdir -p "$BIN_DIR" "$SERVICE_DIR" "$STATE_DIR"

# Install binaries
install -m 755 "$SCRIPT_DIR/tablet_mode_daemon.py" "$BIN_DIR/tablet-mode-daemon"
install -m 755 "$SCRIPT_DIR/tablet_mode_cli.py"    "$BIN_DIR/tablet-mode"

# Install systemd user service
cp "$SCRIPT_DIR/tablet-mode-daemon.service" "$SERVICE_DIR/"

# Install udev rule (requires sudo)
echo ""
echo "Installing udev rule (requires sudo)..."
sudo cp "$SCRIPT_DIR/99-input-inhibit.rules" "$RULES_DST"

# Add user to input group if not already a member
if ! groups | grep -qw input; then
    echo "Adding $USER to 'input' group (requires sudo)..."
    sudo groupadd -f input
    sudo usermod -aG input "$USER"
    echo "WARNING: Group membership takes effect after next login."
fi

# Reload udev
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input

# Enable and start the systemd user service
systemctl --user daemon-reload
systemctl --user enable tablet-mode-daemon.service
systemctl --user start  tablet-mode-daemon.service

echo ""
echo "=== Installation complete ==="
echo "Daemon status: $(systemctl --user is-active tablet-mode-daemon.service)"
echo ""
echo "CLI commands:"
echo "  tablet-mode status"
echo "  tablet-mode set 0|1"
echo "  tablet-mode unlock [--mode 0|1]"
echo "  tablet-mode refresh-devices"
echo ""
echo "Logs: journalctl --user -u tablet-mode-daemon -f"
echo "      or: $STATE_DIR/daemon.log"
echo ""
echo "NOTE: If group membership changed, log out and back in, then verify"
echo "      with: ls -la /sys/class/input/input*/inhibited | head -5"
