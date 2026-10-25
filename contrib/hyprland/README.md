# Beamer on Hyprland

Hyprland's packaged portal (`xdg-desktop-portal-hyprland`) provides InputCapture but not the
RemoteDesktop and Clipboard portals Beamer needs to *receive* input and share the clipboard. This
folder sets up [spotdemo4's portal fork](https://github.com/spotdemo4/xdg-desktop-portal-hyprland),
which adds both, without touching the packaged portal or Hyprland itself.

Tested on Arch with Hyprland, a Windows laptop driving the Hyprland PC over Tailscale: typing with
Shift/Ctrl, clicks, scrolling, pointer placement on both monitors, text and image clipboard both
ways, edge crossing both ways, and recovery after restarting either side.

## Install

Needs the portal's build dependencies (cmake, ninja, sdbus-c++, libeis, pipewire, hyprlang,
hyprutils, hyprwayland-scanner) and Beamer's Python dependencies from
`win_app/requirements-linux.txt`. From this checkout:

```sh
contrib/hyprland/install.sh
```

It:

1. Builds the fork, pinned to `544e4b8`, into `~/.local/opt/beamer-portal`. It skips the fork's
   own share picker (`skip-share-picker.patch`), which needs a newer hyprtoolkit, and reuses the
   installed `hyprland-share-picker`.
2. Points the portal service at that build with a drop-in
   (`~/.config/systemd/user/xdg-desktop-portal-hyprland.service.d/beamer-portal.conf`).
3. Advertises RemoteDesktop and Clipboard (`~/.local/share/xdg-desktop-portal/portals/hyprland.portal`).
4. Installs `~/.local/bin/beamer` for this checkout and the `beamer.service` user unit, started
   hidden with the graphical session.

Restarting the portals can interrupt an active screen share.

## Roll back

```sh
contrib/hyprland/rollback.sh
```

This stops Beamer, removes the two portal overrides and restarts the packaged portal. The build
and Beamer's settings are kept.

## Notes

- The fork is experimental. A Hyprland or portal upgrade may need a rebuild.
- The share picker is the packaged one, so screen sharing in other apps behaves as before.
