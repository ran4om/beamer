#!/bin/sh
# Builds the RemoteDesktop/Clipboard-capable Hyprland portal into ~/.local and points the portal
# service at it, then installs Beamer's user service. Nothing system-wide is replaced.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
work=${WORK:-$HOME/.cache/beamer-portal-build}
prefix=$HOME/.local/opt/beamer-portal
fork=https://github.com/spotdemo4/xdg-desktop-portal-hyprland
commit=544e4b880b6019da7eb367f9d58302be5ab22f20

mkdir -p "$work"
[ -d "$work/src" ] || git clone --recurse-submodules "$fork" "$work/src"
git -C "$work/src" checkout -q "$commit"
git -C "$work/src" submodule update --init --recursive
git -C "$work/src" apply --check "$here/skip-share-picker.patch" 2>/dev/null && git -C "$work/src" apply "$here/skip-share-picker.patch"
cmake -S "$work/src" -B "$work/build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix"
cmake --build "$work/build"
cmake --install "$work/build"

install -Dm644 "$here/systemd/beamer-portal.conf" "$HOME/.config/systemd/user/xdg-desktop-portal-hyprland.service.d/beamer-portal.conf"
install -Dm644 "$here/hyprland.portal" "$HOME/.local/share/xdg-desktop-portal/portals/hyprland.portal"
install -d "$HOME/.local/bin"
# The launcher points at this checkout.
sed "s|\${BEAMER_SRC:-\$HOME/.local/opt/beamer}|$repo|" "$here/beamer" > "$HOME/.local/bin/beamer"
chmod 755 "$HOME/.local/bin/beamer"
install -Dm644 "$here/systemd/beamer.service" "$HOME/.config/systemd/user/beamer.service"

systemctl --user daemon-reload
systemctl --user restart xdg-desktop-portal-hyprland.service xdg-desktop-portal.service
systemctl --user enable --now beamer.service
echo "Patched portal active and Beamer started from $repo."
