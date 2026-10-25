"""Moonlight mode: for when the peer is far away. The way out through an edge, and the shortcut, open
a Moonlight stream of the peer's screen instead of sending this PC's input across, and the way back
hides the stream again.

The stream runs with Moonlight's absolute mouse, so this PC's pointer stays its own and is laid
over the streamed display. That display is one of the peer's in Beamer's layout, and its edges do
what they would do on the peer: the edge a zone leads to is the way home, through that zone and
onto the stretch of this PC's edge it covers, and the edge beside another of the peer's displays
switches the stream to that display. Settings live in moonlight.json beside settings.json, this
PC's alone: they are never sent to a peer."""

import ctypes
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from core import protocol
from core import return_edge
from core.i18n import tr
from core.return_edge import OPPOSITE, Rect

LOGGER = logging.getLogger(__name__)

DEFAULTS = {
    "on": False,
    "host": "kia-home-link",
    "app": "Desktop",
    "exe": r"C:\Program Files\Moonlight Game Streaming\Moonlight.exe",
    # The streamed monitor's own size, so nothing is scaled twice; empty for Moonlight's setting.
    "resolution": "1680x1050",
    "fps": 60,
    # Kbps; 0 for Moonlight's own setting.
    "bitrate": 0,
    # The display a new stream starts on (Sunshine's output_name), as Beamer's layout names the
    # peer's displays; empty when not known, and the stream is switched once it is up.
    "start_display": "",
    # Sunshine's monitor index for a display, where it is not the display's own number.
    "display_index": {},
    # The edge of the stream that leads home when no zone gives one.
    "home_edge": "left",
    # A hidden stream keeps streaming; after this long it is closed. 0 keeps it open.
    "close_after_idle_s": 300,
}

LAUNCH_WAIT_SECONDS = 30.0
REFOCUS_SECONDS = 1.5
FRONT_CHECK_SECONDS = 0.25
FRONT_SETTLE_CHECK_SECONDS = 0.04
# How long a stream just brought forward has to hold the pointer to its picture and take the keyboard.
CLIP_WAIT_SECONDS = 0.5
FOCUS_SETTLE_SECONDS = 0.08
# A closing stream ends its session with the host within this, or the process is ended.
CLOSE_WAIT_SECONDS = 5.0
# When, after the stream comes to the front, `on_front` is told: SDL takes the keyboard again as
# its own event loop sees the focus, which can be a second later (measured), and once in a while
# after that while the stream stays in front.
FRONT_TELL_AT = (0.05, 0.15, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0)
FRONT_TELL_EVERY = 3.0
# The glow on the edge just left breathes at this pace while a new stream connects.
CONNECTING_PULSE_SECONDS = 1.2
# Sunshine changes monitor this long after its shortcut arrives (measured: about half a second);
# the pointer is moved again after it, so the peer's follows onto the new monitor.
SWITCH_SETTLE_SECONDS = 0.7
# Touching displays may be this far apart in the layout.
SEAM_PX = 2


def settings_path(beamer_settings: Path) -> Path:
    return Path(beamer_settings).with_name("moonlight.json")


def load(path: Path) -> dict:
    settings = dict(DEFAULTS)
    try:
        read = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return settings
    except (OSError, ValueError):
        LOGGER.exception("moonlight.json could not be read; using the defaults")
        return settings
    if isinstance(read, dict):
        settings.update({key: value for key, value in read.items() if key in DEFAULTS})
    return settings


def save(path: Path, settings: dict) -> None:
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def stream_command(settings: dict) -> list:
    command = [settings["exe"], "stream", settings["host"], settings["app"],
               "--display-mode", "borderless", "--absolute-mouse",
               "--capture-system-keys", "fullscreen", "--no-quit-after", "--keep-awake"]
    if settings.get("resolution"):
        command += ["--resolution", str(settings["resolution"])]
    if settings.get("fps"):
        command += ["--fps", str(int(settings["fps"]))]
    if settings.get("bitrate"):
        command += ["--bitrate", str(int(settings["bitrate"]))]
    return command


def peer_displays(layout, peer: str) -> dict:
    """{display key: Rect} for the peer's displays, in its own coordinates, as Beamer's layout has them."""
    tile = (layout or {}).get(peer) if isinstance(layout, dict) else None
    displays = tile.get("displays") if isinstance(tile, dict) else None
    found = {}
    for key, box in (displays or {}).items():
        try:
            rect = Rect(int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"]))
        except (KeyError, TypeError, ValueError):
            continue
        if rect.width > 0 and rect.height > 0:
            found[str(key)] = rect
    return found


def to_screen(point, picture: Rect, screen: Rect):
    """A point on the stream's picture, on this PC, as the same point on the streamed display."""
    fx = (point[0] - picture.x) / max(picture.width - 1, 1)
    fy = (point[1] - picture.y) / max(picture.height - 1, 1)
    return (screen.x + round(min(max(fx, 0.0), 1.0) * (screen.width - 1)),
            screen.y + round(min(max(fy, 0.0), 1.0) * (screen.height - 1)))


def to_picture(point, picture: Rect, screen: Rect):
    """The way back of `to_screen`."""
    return to_screen(point, screen, picture)


def _touching(screen: Rect, other: Rect, edge: str):
    """The stretch of `screen`'s `edge`, as a fraction of it, that `other` sits against; None when
    it does not."""
    if edge in ("left", "right"):
        gap = other.x - screen.right - 1 if edge == "right" else screen.x - other.right - 1
        low, high, start, length = max(screen.y, other.y), min(screen.bottom, other.bottom), screen.y, screen.height
    else:
        gap = other.y - screen.bottom - 1 if edge == "bottom" else screen.y - other.bottom - 1
        low, high, start, length = max(screen.x, other.x), min(screen.right, other.right), screen.x, screen.width
    if abs(gap) > SEAM_PX or high <= low:
        return None
    return (low - start) / length, (high - start + 1) / length


class Push(NamedTuple):
    """What a push against the streamed display's edge came to: `kind` "home" or "switch", the
    edge in this PC's terms for the glow, and for a breakthrough where it leads."""
    kind: str
    edge: str
    pressure: float
    crossed: bool
    outcome: object = None
    to: Optional[str] = None
    point: Optional[tuple] = None


class Ways:
    """The edges of the peer's displays while one is streamed: the ways home, which are this PC's
    zones to the peer read from the far side, and the seams that lead on to another of its displays.
    Measured with Beamer's own edge model and resistance, so they push back like every other edge."""

    def __init__(self, displays: dict, home: list, onward: list) -> None:
        self.displays = displays
        self.home = home
        # [(model, the display it leads to)]
        self.onward = onward

    @classmethod
    def build(cls, peer: str, zones, layout, resistance: int, home_edge: str = "left",
              clock: Callable[[], float] = time.monotonic) -> "Ways":
        displays = peer_displays(layout, peer)
        home = []
        for zone in zones or ():
            if not isinstance(zone, dict) or zone.get("off") is True or zone.get("peer") != peer:
                continue
            side = zone.get("side") or zone.get("edge")
            if side not in OPPOSITE:
                continue
            far_side = zone.get("target_side") if zone.get("target_side") in OPPOSITE else OPPOSITE[side]
            far_display = zone.get("target_display")
            try:
                home.append(return_edge.ReturnEdge(
                    far_side, resistance, clock=clock,
                    display=far_display if far_display in displays else None,
                    target_display=zone.get("display"), target_side=side,
                    contact_span=zone.get("target_span"), target_span=zone.get("contact_span")))
            except ValueError:
                continue
        if not home and home_edge in OPPOSITE:
            home.append(return_edge.ReturnEdge(home_edge, resistance, clock=clock))
        onward = []
        for key, screen in displays.items():
            for other_key, other in displays.items():
                if other_key == key:
                    continue
                for edge in OPPOSITE:
                    span = _touching(screen, other, edge)
                    if span is not None:
                        onward.append((return_edge.ReturnEdge(edge, resistance, clock=clock, display=key,
                                                              contact_span=span), other_key))
        return cls(displays, home, onward)

    def screen(self, display, picture: Rect) -> Rect:
        return self.displays.get(display) or picture

    def entry(self, outcome):
        """(display, point on it) where a way out arrives: the display its zone leads to, else
        the one its point lands on."""
        if outcome is None or not self.displays:
            return None, None
        try:
            point = return_edge.arrival_position(list(self.displays.values()), outcome.edge, outcome.offset,
                                                 outcome.display, outcome.side, self.displays)
        except Exception:
            return None, None
        display = outcome.display if outcome.display in self.displays else next(
            (key for key, rect in self.displays.items()
             if rect.x <= point[0] <= rect.right and rect.y <= point[1] <= rect.bottom), None)
        return display, point

    def feed(self, display, picture: Rect, pointer, dx: float, dy: float) -> Optional[Push]:
        screen = self.screen(display, picture)
        point = to_screen(pointer, picture, screen)
        found = None
        for model in self.home:
            if model.display not in (None, display):
                continue
            outcome = model.feed([screen], point, dx, dy, display_map=self.displays)
            if outcome.action != return_edge.PASS and found is None:
                found = Push("home", model.edge, outcome.pressure, outcome.action == return_edge.CROSS, outcome)
        for model, other in self.onward:
            if model.display != display:
                continue
            outcome = model.feed([screen], point, dx, dy, display_map=self.displays)
            if outcome.action != return_edge.PASS and found is None:
                crossed = outcome.action == return_edge.CROSS
                found = Push("switch", model.edge, outcome.pressure, crossed, outcome, other,
                             self._landing(point, model.edge, self.displays[other]) if crossed else None)
        if found is not None and found.crossed:
            for model in self.home + [model for model, _other in self.onward]:
                model.armed = True
                model.reset()
        return found

    @staticmethod
    def _landing(point, edge: str, other: Rect):
        """Across the seam onto `other`, where the pointer would be on the peer itself."""
        x = min(max(point[0], other.x), other.right)
        y = min(max(point[1], other.y), other.bottom)
        return {"left": (other.right, y), "right": (other.x, y), "top": (x, other.bottom), "bottom": (x, other.y)}[edge]


class MoonlightMode:
    """The stream's life: launched on the first way out, hidden and shown after, closed when it
    has been hidden for a while or the mode goes off. Everything slow happens on its own thread:
    the way out and the way back are found on the hook thread, which must never wait."""

    def __init__(self, path: Path, *, windows=None, popen=subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic, monitors=None, cursor=None,
                 resistance: Callable[[], int] = lambda: 200, geometry=None) -> None:
        self.path = Path(path)
        self.settings = load(self.path)
        self._windows = windows if windows is not None else Win32Windows()
        self._popen = popen
        self._clock = clock
        self._monitors = monitors
        self._cursor = cursor
        # Beamer's own crossing resistance, read each time the ways are built.
        self._resistance = resistance
        # () -> (zones, layout): this PC's zones and the layout with the peer's displays in it.
        self._geometry = geometry or (lambda: ([], {}))
        self._process = None
        self._hidden_at = None
        self._refocus_at = None
        self._nudge_at = None
        self._jobs: "queue.Queue" = queue.Queue()
        self._thread = None
        self._stop = threading.Event()
        self._front_since = None
        self._front_told = 0
        # The peer streamed, the display of it in the picture, and the one Sunshine is streaming.
        self._peer: Optional[str] = None
        self._display: Optional[str] = None
        self._streamed: Optional[str] = None
        self._ways = Ways({}, [], [])
        # Called each time the stream comes to the front, once its keyboard grab is in: Beamer puts
        # its own keyboard hook back ahead of it there, or the shortcut never reaches it.
        self.on_front: Optional[Callable[[], None]] = None
        # (title, message), off the GUI thread; and the mode switching, for the tray.
        self.on_alert: Optional[Callable[[str, str], None]] = None
        self.on_changed: Optional[Callable[[bool], None]] = None
        # The edge glow: (edge, pressure, crossed, part), as Beamer's own edges report it.
        self.on_pressure: Optional[Callable] = None
        # The way home broke through: (edge, offset, display, side) on this PC, to land the pointer.
        self.on_home: Optional[Callable] = None
        # The pointer crossed onto another of the peer's displays: (edge, x, y), for the arrival effect.
        self.on_arrival: Optional[Callable] = None

    # -- the switch ---------------------------------------------------------

    @property
    def on(self) -> bool:
        return bool(self.settings.get("on"))

    def set_on(self, value: bool) -> None:
        value = bool(value)
        if value == self.on:
            return
        self.settings["on"] = value
        try:
            save(self.path, self.settings)
        except OSError:
            LOGGER.exception("moonlight.json could not be saved")
        LOGGER.info("Moonlight mode %s", "on" if value else "off")
        if not value:
            self._post("close")
        if self.on_changed is not None:
            self.on_changed(value)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="Beamer-moonlight", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Beamer is quitting: the stream it started goes with it, its session ended with the host
        as Moonlight's own close does, without waiting long for it."""
        self._stop.set()
        self._jobs.put(None)
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            LOGGER.info("Closing the Moonlight stream as Beamer quits")
            self._end(process, wait=1.5)

    # -- what the hooks ask, on the hook thread -----------------------------

    def cross_out(self, peer=None, outcome=None) -> bool:
        """The way out broke through: True when the mode took it, so Beamer sends nothing across.
        The stream opens on the display the way out leads to, with the pointer where it arrives."""
        if not self.on:
            return False
        if not self.showing():
            if peer is not None:
                self._peer = protocol.id_text(peer) if isinstance(peer, bytes) else str(peer)
            ways = self._build_ways()
            display, point = ways.entry(outcome)
            edge = OPPOSITE.get(getattr(outcome, "edge", None))
            self._post("show", (display, point, edge))
        return True

    def motion(self, dx: int, dy: int) -> bool:
        """One raw movement while input is this PC's: True when the stream is in front, so the
        zones do not see it. Pushes against the streamed display's edges are measured here."""
        if not self.on or not self.showing():
            return False
        try:
            pointer = self._cursor()
            # Moonlight holds the pointer to the picture, which is narrower than this screen when
            # the shapes differ: the picture is the streamed display.
            picture = self._windows.clip_rect() or self._monitor_at(pointer)
            push = self._ways.feed(self._display, picture, pointer, float(dx), float(dy)) if picture else None
        except Exception:
            LOGGER.exception("The way back could not be measured")
            return True
        if push is not None:
            self._glow(push.edge, push.pressure, push.crossed)
            if push.crossed and push.kind == "home":
                self._post("home", push.outcome)
            elif push.crossed:
                self._display = push.to
                self._post("switch", (push.to, to_picture(push.point, picture, self._ways.displays[push.to]),
                                      OPPOSITE[push.edge]))
        return True

    def toggle(self) -> bool:
        """The shortcut: True when the mode took it."""
        if not self.on:
            return False
        if self.showing():
            self._post("hide", "shortcut")
        else:
            self.show()
        return True

    def show(self) -> bool:
        if not self.on:
            return False
        self._build_ways()
        self._post("show", (None, None, None))
        return True

    def showing(self) -> bool:
        process = self._process
        if process is None or process.poll() is not None:
            return False
        return self._windows.foreground_pid() == process.pid

    def _monitor_at(self, pointer):
        monitors = self._monitors() if self._monitors is not None else []
        return next((m for m in monitors if m.x <= pointer[0] <= m.right and m.y <= pointer[1] <= m.bottom), None)

    def _build_ways(self) -> Ways:
        """The ways for the peer streamed, from the zones and layout as they are now. With no way out
        taken yet, the peer is the one the first zone leads to."""
        try:
            zones, layout = self._geometry()
        except Exception:
            LOGGER.exception("Could not read the zones for Moonlight mode")
            zones, layout = [], {}
        if self._peer is None:
            self._peer = next((zone.get("peer") for zone in zones or () if isinstance(zone, dict) and zone.get("peer")), None)
        ways = Ways.build(self._peer, zones, layout, max(1, int(self._resistance())),
                          self.settings.get("home_edge", "left"), self._clock)
        if self._display not in ways.displays:
            self._display = self.settings.get("start_display") or None
            if self._display not in ways.displays:
                self._display = next(iter(ways.displays), None)
        self._ways = ways
        LOGGER.info("Moonlight mode ways: display %s of %s; home through %s; on through %s", self._display,
                    sorted(ways.displays), [(model.display, model.edge) for model in ways.home],
                    [(model.display, model.edge, other) for model, other in ways.onward])
        return ways

    def _glow(self, edge, pressure, crossed) -> None:
        if self.on_pressure is not None and edge:
            try:
                self.on_pressure(edge, pressure, crossed, None)
            except Exception:
                LOGGER.exception("Pressure callback failed")

    # -- the worker ---------------------------------------------------------

    def _post(self, job: str, argument=None) -> None:
        self._jobs.put((job, argument))

    def _worker(self) -> None:
        while not self._stop.is_set():
            # Closely while the stream has just come to the front, for `on_front`'s early times.
            settling = self._front_since is not None and self._clock() - self._front_since < FRONT_TELL_AT[-1]
            try:
                item = self._jobs.get(timeout=FRONT_SETTLE_CHECK_SECONDS if settling else FRONT_CHECK_SECONDS)
            except queue.Empty:
                item = ("", None)
            if item is None:
                return
            job, argument = item
            try:
                if job in ("show", "home", "hide", "switch"):
                    getattr(self, "_" + job)(argument)
                elif job:
                    getattr(self, "_" + job)()
                else:
                    self._close_if_idle()
                    self._refocus()
                    self._nudge()
                self._watch_front()
            except Exception:
                LOGGER.exception("Moonlight mode could not %s the stream", job or "watch")

    def _running(self) -> bool:
        process = self._process
        if process is None:
            return False
        if process.poll() is None:
            return True
        LOGGER.info("Moonlight left with code %s", process.returncode)
        self._process = None
        self._streamed = None
        return False

    def _show(self, entry=None) -> None:
        display, point, edge = entry or (None, None, None)
        if display is not None:
            self._display = display
        self._hidden_at = None
        self._windows.want_stream(True)
        window = None
        if self._running():
            window = self._windows.stream_window(self._process.pid)
            if not window:
                # Moonlight is back at its own window -- a dropped connection leaves it there with
                # an error -- so the stream is started again.
                LOGGER.warning("Moonlight is running without a stream; starting it again")
                self._end(self._process, wait=0)
                self._process = None
        if window:
            self._windows.bring_forward(window)
            LOGGER.info("Moonlight stream shown")
        else:
            window = self._launch(edge)
            if not window:
                return
        self._place(point)

    def _launch(self, edge):
        command = stream_command(self.settings)
        if not Path(command[0]).exists():
            self._alert(tr("Moonlight mode: Moonlight was not found at {path}").format(path=command[0]))
            return None
        LOGGER.info("Starting the Moonlight stream of %s", self.settings["host"])
        output = self._output()
        if sys.platform == "win32":
            # Started hidden, Moonlight's own window never shows: Windows applies this to the first
            # window a program shows, which is Moonlight's (measured).
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0  # SW_HIDE
            output["startupinfo"] = startup
        try:
            self._process = self._popen(command, **output)
        finally:
            # Moonlight has its own handle on the file now.
            if output.get("stdout"):
                output["stdout"].close()
        # Moonlight's own windows -- connecting, and the app behind the stream -- are kept out of
        # sight and off the taskbar; the stream is the only one ever seen.
        self._windows.watch(self._process.pid)
        self._streamed = self.settings.get("start_display") or None
        started = self._clock()
        deadline = started + LAUNCH_WAIT_SECONDS
        try:
            while self._clock() < deadline and not self._stop.is_set():
                if self._process.poll() is not None:
                    LOGGER.warning("Moonlight left with code %s before streaming", self._process.returncode)
                    self._alert(tr("Moonlight mode: could not stream {host}").format(host=self.settings["host"]))
                    self._process = None
                    return None
                window = self._windows.stream_window(self._process.pid)
                if window:
                    self._glow(edge, 0.0, False)
                    self._windows.bring_forward(window)
                    # A process started from the background is not given the foreground, and
                    # Moonlight's own connecting window can take it back as it closes: asked again
                    # a moment later, without holding up the way back meanwhile.
                    self._refocus_at = self._clock() + REFOCUS_SECONDS
                    LOGGER.info("Moonlight stream up after %.1f s", self._clock() - started)
                    return window
                # The edge just left breathes while the stream connects.
                phase = (self._clock() - started) / CONNECTING_PULSE_SECONDS * 2 * math.pi
                self._glow(edge, 0.55 + 0.35 * math.cos(phase), False)
                time.sleep(0.05)
        finally:
            self._glow(edge, 0.0, False)
        LOGGER.warning("No Moonlight stream window after %.0f s", LAUNCH_WAIT_SECONDS)
        self._alert(tr("Moonlight mode: could not stream {host}").format(host=self.settings["host"]))
        self._end(self._process, wait=0)
        self._process = None
        return None

    def _refocus(self) -> None:
        if self._refocus_at is None or self._clock() < self._refocus_at:
            return
        self._refocus_at = None
        if self._hidden_at is None and self._running() and self._windows.foreground_pid() != self._process.pid:
            window = self._windows.stream_window(self._process.pid)
            if window:
                self._windows.bring_forward(window)

    def _nudge(self) -> None:
        """Moonlight sends where the pointer is only as it moves, and a place sent before Sunshine
        had changed monitor went to the old one: a pixel's move inward sends it again. A move and
        straight back is no move at all to SDL, which reads only where the pointer ends up."""
        if self._nudge_at is None or self._clock() < self._nudge_at:
            return
        self._nudge_at = None
        if self.showing():
            x, y = self._cursor()
            picture = self._windows.clip_rect()
            inward = 1 if picture is None or x < picture.x + picture.width // 2 else -1
            self._windows.set_cursor(x + inward, y)

    def _place(self, point) -> None:
        """Streams the display wanted, and puts the pointer where the way out arrived on it."""
        if self._display is not None and self._display != self._streamed:
            self._switch_stream(self._display)
        if point is None:
            return
        picture = self._picture()
        screen = self._ways.displays.get(self._display)
        if picture is not None and screen is not None:
            self._windows.set_cursor(*to_picture(point, picture, screen))

    def _picture(self):
        deadline = self._clock() + CLIP_WAIT_SECONDS
        while True:
            picture = self._windows.clip_rect()
            if picture is not None or self._clock() >= deadline:
                return picture
            time.sleep(0.02)

    def _switch_stream(self, display) -> None:
        index = (self.settings.get("display_index") or {}).get(display, display)
        try:
            index = int(index)
        except (TypeError, ValueError):
            LOGGER.warning("No Sunshine monitor index for display %s", display)
            return
        # The keys go to the window with the keyboard: a stream just brought forward has it a
        # moment later, and keys sent before that were lost (measured).
        deadline = self._clock() + CLIP_WAIT_SECONDS
        while self._clock() < deadline and not self.showing():
            time.sleep(0.01)
        time.sleep(FOCUS_SETTLE_SECONDS)
        self._windows.switch_display(index)
        self._streamed = display
        self._nudge_at = self._clock() + SWITCH_SETTLE_SECONDS
        LOGGER.info("Moonlight stream switched to display %s", display)

    def _switch(self, argument) -> None:
        display, point, edge = argument
        if not self._running():
            return
        self._switch_stream(display)
        self._windows.set_cursor(*point)
        if self.on_arrival is not None:
            try:
                self.on_arrival(edge, point[0], point[1])
            except Exception:
                LOGGER.exception("Arrival callback failed")

    def _home(self, outcome) -> None:
        self._hide("way_back")
        if self.on_home is not None and outcome is not None:
            self.on_home(outcome.edge, outcome.offset, outcome.display, outcome.side)

    def _output(self) -> dict:
        """Moonlight's own log of the latest stream, beside Beamer's, for when a stream fails."""
        try:
            return {"stdout": open(self.path.with_name("moonlight-stream.log"), "wb"), "stderr": subprocess.STDOUT}
        except OSError:
            return {}

    def _hide(self, reason: str = "") -> None:
        self._windows.want_stream(False)
        if not self._running():
            return
        window = self._windows.stream_window(self._process.pid)
        if window:
            self._windows.hide(window)
        self._hidden_at = self._clock()
        LOGGER.info("Moonlight stream hidden%s", f" ({reason})" if reason else "")

    def _close(self) -> None:
        process, self._process = self._process, None
        self._hidden_at = None
        self._streamed = None
        if process is None or process.poll() is not None:
            return
        LOGGER.info("Closing the Moonlight stream")
        self._end(process, wait=CLOSE_WAIT_SECONDS)

    def _end(self, process, wait: float) -> None:
        """Closes the stream's window, which ends the session with the host the way Moonlight's
        own close does, so the next stream starts at once, and Moonlight leaves by itself within half a
        second (measured); ended only when it does not. Nothing is ever shown."""
        window = self._windows.stream_window(process.pid)
        if window:
            self._windows.close(window)
            deadline = self._clock() + wait
            while self._clock() < deadline and process.poll() is None:
                time.sleep(0.05)
        if process.poll() is None:
            process.terminate()
        self._windows.watch(None)

    def _watch_front(self) -> None:
        """Tells `on_front` each time the stream comes to the front, however it got there -- a way
        out, the shortcut, or a click on it -- at FRONT_TELL_AT and then every FRONT_TELL_EVERY."""
        if not self.showing():
            self._front_since = None
            return
        now = self._clock()
        if self._front_since is None:
            self._front_since = now
            self._front_told = 0
        told = self._front_told
        due = (FRONT_TELL_AT[told] if told < len(FRONT_TELL_AT)
               else FRONT_TELL_AT[-1] + FRONT_TELL_EVERY * (told - len(FRONT_TELL_AT) + 1))
        if now - self._front_since >= due:
            self._front_told = told + 1
            if self.on_front is not None:
                self.on_front()

    def _close_if_idle(self) -> None:
        limit = self.settings.get("close_after_idle_s") or 0
        if limit and self._hidden_at is not None and self._clock() - self._hidden_at >= limit:
            LOGGER.info("The Moonlight stream was hidden for %s s", limit)
            self._close()

    def _alert(self, message: str) -> None:
        if self.on_alert is not None:
            try:
                self.on_alert("Beamer", message)
            except Exception:
                LOGGER.exception("Alert callback failed")


def _guid(text: str):
    from uuid import UUID

    return (ctypes.c_byte * 16).from_buffer_copy(UUID(text).bytes_le)


def _com_call(pointer, index: int, *args) -> int:
    """Method `index` of the COM interface at `pointer`, with `args`; its HRESULT."""
    table = ctypes.cast(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *[type(arg) for arg in args])(table[index])
    return method(pointer, *args)


class Win32Windows:
    """Moonlight's windows: the stream's found, brought forward and sent away, and every other one
    kept out of sight as it appears."""

    SW_HIDE = 0
    SW_SHOW = 5
    SW_RESTORE = 9
    WM_CLOSE = 0x0010
    WM_APP_WATCH = 0x8000 + 0x31
    EVENT_OBJECT_CREATE = 0x8000
    EVENT_OBJECT_SHOW = 0x8002
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x80
    WS_EX_APPWINDOW = 0x40000
    # Sunshine switches the streamed monitor on Ctrl+Alt+Shift+F1 (the first) to F13.
    F_KEYS = [(0x70 + n, scan) for n, scan in enumerate(
        (0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x57, 0x58, 0x64))]
    MODIFIERS = ((0xA2, 0x1D), (0xA4, 0x38), (0xA0, 0x2A))

    def __init__(self) -> None:
        self._before = None
        # The stream window as this hid it: a hidden window is not found by looking for visible ones.
        self._hidden = None
        self._wanted = False
        self._watched = None
        self._thread_id = None
        self._taskbar = None
        if sys.platform != "win32":
            self._user32 = None
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
        self._event_proc = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                              ctypes.c_long, ctypes.c_long, ctypes.c_ulong, ctypes.c_ulong)
        user32.EnumWindows.argtypes = [self._enum_proc, ctypes.c_ssize_t]
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int]
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        user32.GetShellWindow.restype = ctypes.c_void_p
        user32.ClipCursor.argtypes = [ctypes.c_void_p]
        user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWinEventHook.restype = ctypes.c_void_p
        user32.SetWinEventHook.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, self._event_proc,
                                           ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
        self._user32 = user32
        self._kernel32 = kernel32
        self._on_event = self._event_proc(self._shown)

    # -- keeping Moonlight's own windows out of sight -------------------------

    def watch(self, pid) -> None:
        """Keeps every window of `pid` but the stream out of sight, and the stream off the taskbar,
        from the moment each appears; None stops."""
        self._watched = pid
        if self._user32 is None:
            return
        if self._thread_id is None and pid is not None:
            ready = threading.Event()
            threading.Thread(target=self._watch_loop, args=(ready,), name="Beamer-moonlight-windows",
                             daemon=True).start()
            ready.wait(2.0)
        if self._thread_id is not None:
            self._user32.PostThreadMessageW(self._thread_id, self.WM_APP_WATCH, pid or 0, 0)

    def want_stream(self, value: bool) -> None:
        self._wanted = bool(value)

    def _watch_loop(self, ready) -> None:
        msg = (ctypes.c_byte * 64)()
        # A thread's message queue exists once it asks for a message.
        self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._thread_id = self._kernel32.GetCurrentThreadId()
        ready.set()
        hook = None
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            message = ctypes.c_uint.from_buffer(msg, ctypes.sizeof(ctypes.c_void_p)).value
            if message == self.WM_APP_WATCH:
                if hook:
                    self._user32.UnhookWinEvent(hook)
                    hook = None
                pid = self._watched
                if pid:
                    hook = self._user32.SetWinEventHook(self.EVENT_OBJECT_CREATE, self.EVENT_OBJECT_SHOW, None,
                                                        self._on_event, pid, 0, 0)  # WINEVENT_OUTOFCONTEXT
                    for hwnd in self._windows_of(pid):
                        self._keep_out(hwnd)
                continue
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    def _shown(self, _hook, event, hwnd, id_object, id_child, _thread, _time) -> None:
        try:
            if not hwnd or id_object != 0 or id_child != 0 or self._user32.GetAncestor(hwnd, 2) != hwnd:
                return
            if event == self.EVENT_OBJECT_SHOW:
                self._keep_out(hwnd)
            elif event == self.EVENT_OBJECT_CREATE and self._class(hwnd) == "SDL_app":
                # Made a tool window before it is first shown, it never gets a taskbar button.
                style = self._user32.GetWindowLongPtrW(hwnd, self.GWL_EXSTYLE)
                self._user32.SetWindowLongPtrW(hwnd, self.GWL_EXSTYLE,
                                               (style | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW)
        except Exception:
            LOGGER.exception("Could not keep a Moonlight window out of sight")

    def _keep_out(self, hwnd) -> None:
        if not self._user32.IsWindowVisible(hwnd):
            return
        if self._class(hwnd) != "SDL_app":
            self._user32.ShowWindow(hwnd, self.SW_HIDE)
            return
        style = self._user32.GetWindowLongPtrW(hwnd, self.GWL_EXSTYLE)
        if not self._wanted:
            self._user32.ShowWindow(hwnd, self.SW_HIDE)
            self._hidden = hwnd
        if style & self.WS_EX_TOOLWINDOW:
            return
        # A tool window gets no taskbar button when it is next shown; the one it has now is taken
        # off by the taskbar itself, or, failing that, by hiding and showing it again.
        self._user32.SetWindowLongPtrW(hwnd, self.GWL_EXSTYLE, (style | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW)
        if self._wanted and not self._taskbar_delete(hwnd):
            self._user32.ShowWindow(hwnd, self.SW_HIDE)
            self._user32.ShowWindow(hwnd, self.SW_SHOW)

    def _taskbar_delete(self, hwnd) -> bool:
        """ITaskbarList::DeleteTab, on this thread, which has COM for it."""
        try:
            if self._taskbar is None:
                ole32 = ctypes.OleDLL("ole32")
                ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
                clsid = _guid("56FDF344-FD6D-11D0-958A-006097C9A090")
                iid = _guid("56FDF342-FD6D-11D0-958A-006097C9A090")
                taskbar = ctypes.c_void_p()
                ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(taskbar))
                _com_call(taskbar, 3)  # HrInit
                self._taskbar = taskbar
            return _com_call(self._taskbar, 5, ctypes.c_void_p(hwnd)) == 0  # DeleteTab
        except OSError:
            LOGGER.debug("The taskbar would not drop Moonlight's button", exc_info=True)
            return False

    def _windows_of(self, pid) -> list:
        found = []

        def visit(hwnd, _lparam):
            owner = ctypes.c_ulong(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                found.append(hwnd)
            return 1

        self._user32.EnumWindows(self._enum_proc(visit), 0)
        return found

    def _class(self, hwnd) -> str:
        name = ctypes.create_unicode_buffer(64)
        self._user32.GetClassNameW(hwnd, name, 64)
        return name.value

    # -- the stream ---------------------------------------------------------

    def foreground_pid(self) -> Optional[int]:
        if self._user32 is None:
            return None
        hwnd = self._user32.GetForegroundWindow()
        # A minimised window can stay the foreground one, holding nothing in front.
        if not hwnd or self._user32.IsIconic(hwnd) or not self._user32.IsWindowVisible(hwnd):
            return None
        pid = ctypes.c_ulong(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def stream_window(self, pid: int):
        """The process's stream window: its visible SDL window, or the one this hid. Moonlight makes
        hidden SDL windows of its own to test the decoder, never shown."""
        if self._user32 is None:
            return None
        hidden = self._hidden
        for hwnd in self._windows_of(pid):
            if self._class(hwnd) == "SDL_app" and (self._user32.IsWindowVisible(hwnd) or hwnd == hidden):
                return hwnd
        return None

    def bring_forward(self, hwnd) -> None:
        """Windows lets only the foreground's own thread hand the foreground on, so this thread
        joins its input for the moment it takes."""
        user32 = self._user32
        foreground = user32.GetForegroundWindow()
        if foreground and foreground != hwnd:
            self._before = foreground
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, self.SW_RESTORE)
        elif not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, self.SW_SHOW)
        self._take_front(hwnd)

    def _take_front(self, hwnd) -> None:
        user32 = self._user32
        foreground = user32.GetForegroundWindow()
        theirs = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        ours = self._kernel32.GetCurrentThreadId()
        attached = bool(theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(ours, theirs, False)

    def clip_rect(self):
        """The rectangle the pointer is held to, or None when it is free to roam every screen."""
        if self._user32 is None:
            return None
        rect = (ctypes.c_long * 4)()
        if not self._user32.GetClipCursor(ctypes.byref(rect)):
            return None
        left, top, right, bottom = rect
        metric = self._user32.GetSystemMetrics
        screen = (metric(76), metric(77), metric(76) + metric(78), metric(77) + metric(79))  # SM_*VIRTUALSCREEN
        if (left, top, right, bottom) == screen:
            return None
        return Rect(left, top, right - left, bottom - top)

    def hide(self, hwnd) -> None:
        """Hidden, the stream keeps streaming and leaves the taskbar and Alt+Tab alone. Windows gives
        the front to no one in particular as it goes, and Moonlight leaves the pointer held to its
        picture (measured): the front goes back to the window that had it, else the desktop."""
        user32 = self._user32
        self._hidden = hwnd
        user32.ShowWindow(hwnd, self.SW_HIDE)
        before = self._before
        target = before if before and user32.IsWindow(before) and not user32.IsIconic(before) else user32.GetShellWindow()
        if target:
            self._take_front(target)
        user32.ClipCursor(None)

    def close(self, hwnd) -> None:
        self._user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)

    def set_cursor(self, x: int, y: int) -> None:
        self._user32.SetCursorPos(int(x), int(y))

    def switch_display(self, index: int) -> None:
        """Sunshine's own shortcut, typed into the stream, which has the keyboard while in front.
        Marked as Beamer's, so Beamer's own hook lets it by."""
        if not 0 <= index < len(self.F_KEYS):
            return
        from input_injector import _keybd_input, _send_input

        key = self.F_KEYS[index]
        up = 0x0002  # KEYEVENTF_KEYUP
        _send_input(*([_keybd_input(vk, scan, 0) for vk, scan in self.MODIFIERS]
                      + [_keybd_input(key[0], key[1], 0), _keybd_input(key[0], key[1], up)]
                      + [_keybd_input(vk, scan, up) for vk, scan in reversed(self.MODIFIERS)]))
