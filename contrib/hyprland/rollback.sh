#!/bin/sh
# Stops Beamer and goes back to the packaged Hyprland portal. The build and settings are kept.
set -eu
systemctl --user disable --now beamer.service || true
rm -f -- "$HOME/.config/systemd/user/xdg-desktop-portal-hyprland.service.d/beamer-portal.conf"
rm -f -- "$HOME/.local/share/xdg-desktop-portal/portals/hyprland.portal"
systemctl --user daemon-reload
systemctl --user restart xdg-desktop-portal-hyprland.service xdg-desktop-portal.service
echo "Original packaged Hyprland portal restored. Beamer is stopped."
