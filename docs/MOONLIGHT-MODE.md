# Moonlight mode (Windows)

For when the other machine is far away. With **Moonlight mode (away from home)** ticked in the
tray menu, the ways out open a [Moonlight](https://moonlight-stream.org) stream of the peer instead
of sending this PC's input across:

| Action | Mode off | Mode on |
|---|---|---|
| Push through the edge that leads to the peer | Input goes to the peer | The peer's screen opens full screen in Moonlight |
| Push through the opposite edge of the stream | — | The stream hides; you are back on this PC |
| Double-tap the shortcut key | Switches input | Shows or hides the stream |
| Tray: Send input across | Switches input | Shows the stream |

The peer needs [Sunshine](https://github.com/LizardByte/Sunshine), already paired with Moonlight on
this PC. Beamer's own link stays up, so nothing else changes.

## Behaviour

- The first way out starts `Moonlight.exe stream <host> <app>`: borderless, absolute mouse,
  system keys captured. After that the stream is only minimised and restored, so going back and
  forth is instant.
- With the absolute mouse, this PC's pointer is laid over the streamed screen. Its edge that leads
  home is this PC's edge too, measured with Beamer's crossing resistance. When the picture is
  narrower than the screen, Moonlight holds the pointer to the picture, and that edge counts.
- A stream hidden for `close_after_idle_s` is closed, and the host is told to end the session.
  Turning the mode off or quitting Beamer closes it too.
- Moonlight's keyboard capture installs its own low-level hook each time its window gains focus,
  ahead of Beamer's. Beamer re-installs its hook after the stream comes to the front, so the
  shortcut keeps working. Every other key still reaches the stream, Super and Alt+Tab included.
  A double-tap within about a second of the stream appearing can be missed.

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
| `home_edge` | `left` | The edge of the stream that leads home |
| `close_after_idle_s` | `300` | 0 keeps a hidden stream open |

Moonlight's output for the latest stream is written to `moonlight-stream.log` beside `Beamer.log`.

## Which monitor

Sunshine streams one monitor. Pick the one next to this PC in Beamer's layout, so the edge you push
through to come home is the one that leads home. On Linux, set it with `output_name = <index>` in
`sunshine.conf`; the indexes are listed in Sunshine's log under `Wayland monitor list`.

## Code

- `win_app/moonlight_mode.py`: the mode, the way-back push, and Moonlight's window.
- `win_app/sender.py`: `cross_out` and `local_motion` hooks.
- `win_app/kvm_bridge_win.py`: the tray item, the shortcut and the wiring.
- `win_app/capture_win.py`: `rehook_keyboard`.
- Tests: `win_app/tests/test_moonlight_mode.py`.
