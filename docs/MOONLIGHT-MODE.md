# Moonlight mode (Windows)

For when the other machine is far away. With **Moonlight mode (away from home)** ticked in the
tray menu, the ways out open a [Moonlight](https://moonlight-stream.org) stream of the peer instead
of sending this PC's input across:

| Action | Mode off | Mode on |
|---|---|---|
| Push through a zone that leads to the peer | Input goes to the peer | The peer display the zone leads to opens full screen, with the pointer where you crossed |
| Push through the edge of the streamed display that leads home | Input comes back | The stream hides, and the pointer lands where that zone covers this PC's edge |
| Push through the edge beside another of the peer's displays | The pointer moves to it | The stream switches to that display |
| Double-tap the shortcut key | Switches input | Shows or hides the stream |
| Tray: Send input across | Switches input | Shows the stream |

The peer needs [Sunshine](https://github.com/LizardByte/Sunshine), already paired with Moonlight on
this PC. Beamer's own link stays up, so nothing else changes.

## Behaviour

- **The layout decides everything.** The streamed display is one of the peer's displays in Beamer's
  layout, and its edges act as they would on the peer. Rearranging the machines in Beamer changes
  the ways in, the ways home and the monitor seams together. No setting needs to follow.
- **The edges push back.** The ways home and the seams are measured with Beamer's own edge model
  and crossing resistance, and they glow like Beamer's other edges. A seam switch lands the pointer
  across the seam with the arrival effect. A way home lands it on this PC's edge, in proportion,
  as a normal crossing back would.
- **Moonlight stays out of sight.** Moonlight starts hidden, so its own windows never show. The
  stream window is a tool window, with no taskbar button and not in Alt+Tab. While a new stream
  connects (about 3 s), the edge just left breathes. After that, showing and hiding the stream is
  instant.
- **Closing is silent.** A stream hidden for `close_after_idle_s` is closed the way Moonlight's own
  close does it, which ends the session with the host cleanly, so the next stream starts at once.
  Turning the mode off or quitting Beamer closes it too.
- **Switching monitors** sends Sunshine's own shortcut (Ctrl+Alt+Shift+F1 for its first monitor,
  F2 for the second, and so on) into the stream. The switch takes about half a second on the host.
  The shortcut also works typed by hand, but Beamer then no longer knows which display is shown.
- **The keyboard shortcut.** Moonlight's keyboard capture installs its own low-level hook each time
  its window gains focus, ahead of Beamer's. Beamer re-installs its hook several times in the first
  two seconds after the stream comes to the front, then every few seconds. Every other key still
  reaches the stream, Super and Alt+Tab included.

## Settings

`moonlight.json` beside `settings.json` (`%LOCALAPPDATA%\Beamer`), this PC's alone and never
synced:

| Key | Default | |
|---|---|---|
| `on` | `false` | The tray switch |
| `host` | `kia-home-link` | Moonlight's name for the host |
| `app` | `Desktop` | The Sunshine app |
| `exe` | `C:\Program Files\Moonlight Game Streaming\Moonlight.exe` | |
| `resolution` | `1680x1050` | Best set to the streamed monitor's size; empty for Moonlight's setting |
| `fps` | `60` | |
| `bitrate` | `0` | Kbps; 0 for Moonlight's setting |
| `start_display` | `""` | The display Sunshine starts on (its `output_name`), as Beamer's layout numbers the peer's displays. Empty means not known: the stream is switched once it is up, which costs a half-second switch on each new stream |
| `display_index` | `{}` | Sunshine's monitor index for a display, where it differs from the display's own number, for example `{"0": 1, "1": 0}` |
| `home_edge` | `left` | The edge of the stream that leads home when no zone gives one |
| `close_after_idle_s` | `300` | 0 keeps a hidden stream open |

Moonlight's output for the latest stream is written to `moonlight-stream.log` beside `Beamer.log`.

## The host

- Beamer numbers a Linux peer's displays in the order its compositor lists them, and Sunshine's
  Wayland capture uses the same order. That order is in Sunshine's log under
  `Wayland monitor list`. If the order differs, use `display_index`.
- Set Sunshine's `output_name` to the monitor's **name**, for example `output_name = DP-4`, not its
  index. Sunshine 2026.516 reads the setting by index when probing encoders but by name when a
  session starts, so `output_name = 1` probes one monitor and streams the other.
- Moonlight launches faster with its configuration warning dialog and mDNS turned off: set
  `confwarnings` and `mdns` to `false` under
  `HKCU\Software\Moonlight Game Streaming Project\Moonlight`. A hardware encoder on the host
  (`encoder = vaapi` for AMD and Intel on Linux) shortens connecting too.

## Cost

A hidden stream keeps streaming: about 19 Mbps (Sunshine pads to the bitrate) and a tenth of one CPU
core here, until `close_after_idle_s` closes it.

## Code

- `win_app/moonlight_mode.py`: the mode, the ways (`Ways`), and Moonlight's windows.
- `win_app/sender.py`: the `cross_out` and `local_motion` hooks, `land`, `show_arrival` and
  `zones_and_layout`.
- `win_app/kvm_bridge_win.py`: the tray item, the shortcut and the wiring.
- `win_app/capture_win.py`: `rehook_keyboard`.
- Tests: `win_app/tests/test_moonlight_mode.py`.
