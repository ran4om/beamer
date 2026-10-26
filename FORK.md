# About this fork

This is [ran4om/beamer](https://github.com/ran4om/beamer), a fork of
[kalkman-code/beamer](https://github.com/kalkman-code/beamer) based on **Beamer 1.5.13**. The work
is on the `feature/moonlight-mode` branch. It adds two things and leaves everything else as
upstream has it:

1. **Moonlight mode** (Windows). For using a machine that is far away.
2. **A Hyprland setup** (Linux). For running Beamer on Hyprland.

Nothing here changes the wire protocol, the settings format or how Beamer behaves with Moonlight
mode off. A machine running this fork works with machines running upstream Beamer 1.5.13.

## Moonlight mode

When you're away from home, Beamer can't send your input to the other machine directly. Moonlight
mode, ticked in the tray menu, makes Beamer's gestures open a
[Moonlight](https://moonlight-stream.org) stream of that machine instead:

- **Going there.** Push through the edge that leads to the other machine. The display it leads to
  opens full screen, with the pointer where you crossed.
- **Between its monitors.** Push through the seam between two of its displays, and the stream
  switches to the other one.
- **Coming back.** Push through the edge that leads home. The stream hides, and the pointer lands
  on this PC's edge where the zone covers it.
- **The shortcut.** Double-tapping it shows or hides the stream.

All of this follows Beamer's layout. Rearranging the machines changes the ways in, the ways back
and the monitor seams together. The stream's edges glow and push back like Beamer's own.

Moonlight never appears as an app of its own: it starts hidden, and the stream has no taskbar
button. A hidden stream is closed quietly after five minutes. Showing a stream that is still
open is instant. A new one takes about three seconds, while the edge you left gently pulses.

The other machine needs [Sunshine](https://github.com/LizardByte/Sunshine), paired with Moonlight
on this PC. Setup, settings and limits are in [docs/MOONLIGHT-MODE.md](docs/MOONLIGHT-MODE.md).

### What changed in the code

| File | Change |
|---|---|
| `win_app/moonlight_mode.py` | New. The mode itself: the stream's life, the ways in, back and between monitors, and keeping Moonlight's windows out of sight |
| `win_app/sender.py` | Two hooks, `cross_out` and `local_motion`, through which the mode takes a way out and the pointer's movement. Also `land`, `show_arrival` and `zones_and_layout` |
| `win_app/kvm_bridge_win.py` | The tray item, the shortcut and the wiring |
| `win_app/capture_win.py` | `rehook_keyboard`: puts Beamer's keyboard hook back ahead of Moonlight's, so the shortcut keeps working in the stream |
| `win_app/tests/test_moonlight_mode.py` | New. 24 tests |
| `locales/` | The mode's three strings, with Italian translations |

With the mode off, none of this runs and the hooks are empty.

## Hyprland setup

Hyprland's own portal has no RemoteDesktop or Clipboard portal, so Beamer on Hyprland can't
receive input or share the clipboard. [`contrib/hyprland/`](contrib/hyprland/README.md) builds
[spotdemo4's portal fork](https://github.com/spotdemo4/xdg-desktop-portal-hyprland), which adds
both, into `~/.local`. It points the portal service at that build with a drop-in, and installs a
user service that starts Beamer with the session. The packaged portal is never replaced, and
`rollback.sh` undoes it all.

## Tested on

- A Windows 11 laptop and an Arch Linux PC running Hyprland with two monitors, over Tailscale.
- Moonlight 6.2.0 and Sunshine 2026.516.
- A live end-to-end run of Moonlight mode against the real stream: 18 of 18 checks passed.

## Keeping up with upstream

`main` follows upstream. To build on a newer Beamer, rebase `feature/moonlight-mode` onto it.
Upstream's in-app update notice points to upstream releases, and installing one replaces this
build.
