"""Moonlight mode: for when the peer is far away. The way out through an edge, and the shortcut, open
a Moonlight stream of the peer's screen instead of sending this PC's input across, and the way back
hides the stream again.

The stream runs with Moonlight's absolute mouse, so this PC's pointer stays its own and is laid
over the streamed screen: the peer's edge that leads home is this PC's edge too. Pushing through
it while the stream is in front is the way back, measured here the way the zones measure a way
out. Settings live in moonlight.json beside settings.json, this PC's alone: they are never sent
to a peer."""

import ctypes
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from core.i18n import tr
from core.return_edge import Rect

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
    # The edge of this PC that leads home while the stream is in front.
    "home_edge": "left",
    # A hidden stream keeps streaming; after this long it is closed. 0 keeps it open.
    "close_after_idle_s": 300,
}

# A push that pauses this long starts again from nothing, as a zone's does.
PUSH_GAP_SECONDS = 0.5
LAUNCH_WAIT_SECONDS = 40.0
REFOCUS_SECONDS = 1.5
FRONT_CHECK_SECONDS = 0.25
# When, after the stream comes to the front, `on_front` is told: SDL takes the keyboard again as
# its own event loop sees the focus, which can be a second later (measured), and once in a while
# after that while the stream stays in front.
FRONT_TELL_AT = (0.15, 0.5, 1.0, 2.0)
FRONT_TELL_EVERY = 3.0


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


class HomePush:
    """The way back: pressure against one edge of the monitor the pointer is on, in the mouse's own
    counts, and a breakthrough at `resistance`."""

    def __init__(self, edge: str = "left", resistance: int = 200) -> None:
        self.edge = edge
        self.resistance = max(1, int(resistance))
        self._pressure = 0.0
        self._last = None

    def reset(self) -> None:
        self._pressure = 0.0
        self._last = None

    def feed(self, monitors, pointer, dx: float, dy: float, now: float) -> bool:
        """True once the push breaks through; it starts again from nothing after."""
        x, y = pointer
        monitor = next((m for m in monitors if m.x <= x < m.x + m.width and m.y <= y < m.y + m.height), None)
        if monitor is None:
            self.reset()
            return False
        at_edge, push = {
            "left": (x <= monitor.x, -dx),
            "right": (x >= monitor.x + monitor.width - 1, dx),
            "top": (y <= monitor.y, -dy),
            "bottom": (y >= monitor.y + monitor.height - 1, dy),
        }[self.edge]
        if self._last is not None and now - self._last > PUSH_GAP_SECONDS:
            self._pressure = 0.0
        if not at_edge or push < 0:
            self.reset()
            return False
        self._last = now
        self._pressure += push
        if self._pressure >= self.resistance:
            self.reset()
            return True
        return False


class MoonlightMode:
    """The stream's life: launched on the first way out, hidden and shown after, closed when it
    has been hidden for a while or the mode goes off. Everything slow happens on its own thread:
    the way out and the way back are found on the hook thread, which must never wait."""

    def __init__(self, path: Path, *, windows=None, popen=subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic, monitors=None, cursor=None,
                 resistance: Callable[[], int] = lambda: 200) -> None:
        self.path = Path(path)
        self.settings = load(self.path)
        self._windows = windows if windows is not None else Win32Windows()
        self._popen = popen
        self._clock = clock
        self._monitors = monitors
        self._cursor = cursor
        # Beamer's own crossing resistance, read at each push so a change applies at once.
        self._resistance = resistance
        self._process = None
        self._hidden_at = None
        self._push = HomePush(self.settings["home_edge"])
        self._jobs: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = None
        self._stop = threading.Event()
        self._front_since = None
        self._front_told = 0
        # Called each time the stream comes to the front, once its keyboard grab is in: Beamer puts
        # its own keyboard hook back ahead of it there, or the shortcut never reaches it.
        self.on_front: Optional[Callable[[], None]] = None
        # (title, message), off the GUI thread; and the mode switching, for the tray.
        self.on_alert: Optional[Callable[[str, str], None]] = None
        self.on_changed: Optional[Callable[[bool], None]] = None

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
        """Beamer is quitting: the stream it started goes with it, and the host is told without
        waiting for its answer."""
        self._stop.set()
        self._jobs.put(None)
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            LOGGER.info("Closing the Moonlight stream as Beamer quits")
            process.terminate()
            try:
                self._popen([self.settings["exe"], "quit", self.settings["host"]])
            except Exception:
                LOGGER.exception("Moonlight could not quit the session on the host")

    # -- what the hooks ask, on the hook thread -----------------------------

    def cross_out(self, _peer=None) -> bool:
        """The way out broke through: True when the mode took it, so Beamer sends nothing across."""
        if not self.on:
            return False
        if not self.showing():
            self._post("show")
        return True

    def motion(self, dx: int, dy: int) -> bool:
        """One raw movement while input is this PC's: True when it was the way back, so the zones
        must not see it too."""
        if not self.on or not self.showing():
            self._push.reset()
            return False
        try:
            pointer = self._cursor()
            # Moonlight holds the pointer to the picture, which is narrower than this screen when
            # the shapes differ: the picture's edge is the one to push through.
            clip = self._windows.clip_rect()
            monitors = [clip] if clip is not None else self._monitors()
            self._push.resistance = max(1, int(self._resistance()))
        except Exception:
            return False
        if self._push.feed(monitors, pointer, float(dx), float(dy), self._clock()):
            self._post("hide", "way_back")
        # While the stream is in front every move is the peer's, never a way out through a zone.
        return True

    def toggle(self) -> bool:
        """The shortcut: True when the mode took it."""
        if not self.on:
            return False
        if self.showing():
            self._post("hide", "shortcut")
        else:
            self._post("show")
        return True

    def show(self) -> bool:
        if not self.on:
            return False
        self._post("show")
        return True

    def showing(self) -> bool:
        process = self._process
        if process is None or process.poll() is not None:
            return False
        return self._windows.foreground_pid() == process.pid

    # -- the worker ---------------------------------------------------------

    def _post(self, job: str, reason: str = "") -> None:
        self._jobs.put((job, reason))

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._jobs.get(timeout=FRONT_CHECK_SECONDS)
            except queue.Empty:
                item = ("", "")
            if item is None:
                return
            job, reason = item
            try:
                if job == "hide":
                    self._hide(reason)
                elif job:
                    getattr(self, "_" + job)()
                else:
                    self._close_if_idle()
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
        return False

    def _show(self) -> None:
        self._hidden_at = None
        if self._running():
            window = self._windows.stream_window(self._process.pid)
            if window:
                self._windows.bring_forward(window)
                LOGGER.info("Moonlight stream shown")
                return
            # Moonlight is back at its own window -- a dropped connection leaves it there with an
            # error -- so the stream is started again.
            LOGGER.warning("Moonlight is running without a stream; starting it again")
            self._process.terminate()
            self._process = None
        command = stream_command(self.settings)
        if not Path(command[0]).exists():
            self._alert(tr("Moonlight mode: Moonlight was not found at {path}").format(path=command[0]))
            return
        LOGGER.info("Starting the Moonlight stream of %s", self.settings["host"])
        output = self._output()
        try:
            self._process = self._popen(command, **output)
        finally:
            # Moonlight has its own handle on the file now.
            if "stdout" in output:
                output["stdout"].close()
        deadline = self._clock() + LAUNCH_WAIT_SECONDS
        while self._clock() < deadline and not self._stop.is_set():
            if self._process.poll() is not None:
                LOGGER.warning("Moonlight left with code %s before streaming", self._process.returncode)
                self._alert(tr("Moonlight mode: could not stream {host}").format(host=self.settings["host"]))
                self._process = None
                return
            window = self._windows.stream_window(self._process.pid)
            if window:
                # A process started from the background is not given the foreground, and
                # Moonlight's own connecting window can take it back as it closes: asked twice.
                self._windows.bring_forward(window)
                time.sleep(REFOCUS_SECONDS)
                if self._running() and self._windows.foreground_pid() != self._process.pid:
                    self._windows.bring_forward(window)
                LOGGER.info("Moonlight stream up")
                return
            time.sleep(0.1)
        LOGGER.warning("No Moonlight stream window after %.0f s", LAUNCH_WAIT_SECONDS)

    def _output(self) -> dict:
        """Moonlight's own log of the latest stream, beside Beamer's, for when a stream fails."""
        try:
            return {"stdout": open(self.path.with_name("moonlight-stream.log"), "wb"), "stderr": subprocess.STDOUT}
        except OSError:
            return {}

    def _hide(self, reason: str = "") -> None:
        if not self._running():
            return
        window = self._windows.stream_window(self._process.pid)
        if window:
            self._windows.minimise(window)
        self._hidden_at = self._clock()
        LOGGER.info("Moonlight stream hidden%s", f" ({reason})" if reason else "")

    def _close(self) -> None:
        process, self._process = self._process, None
        self._hidden_at = None
        if process is None or process.poll() is not None:
            return
        LOGGER.info("Closing the Moonlight stream")
        try:
            # Ends the session on the host too, so it stops capturing and encoding.
            quitter = self._popen([self.settings["exe"], "quit", self.settings["host"]])
            quitter.wait(timeout=15)
        except Exception:
            LOGGER.exception("Moonlight could not quit the session on the host")
        try:
            process.wait(timeout=5)
        except Exception:
            process.terminate()

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


class Win32Windows:
    """Finding Moonlight's stream window, and bringing it forward or sending it away."""

    SW_MINIMIZE = 6
    SW_RESTORE = 9

    def __init__(self) -> None:
        if sys.platform != "win32":
            self._user32 = None
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
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
        self._user32 = user32
        # The window in front before the stream was brought forward, given the front back after.
        self._before = None
        self._kernel32 = kernel32

    def foreground_pid(self) -> Optional[int]:
        if self._user32 is None:
            return None
        hwnd = self._user32.GetForegroundWindow()
        # A minimised window can stay the foreground one, holding nothing in front.
        if not hwnd or self._user32.IsIconic(hwnd):
            return None
        pid = ctypes.c_ulong(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def stream_window(self, pid: int):
        """The process's visible SDL window: Moonlight's Qt window comes first and the stream's
        own window only once the stream is up."""
        if self._user32 is None:
            return None
        found = []
        name = ctypes.create_unicode_buffer(64)

        def visit(hwnd, _lparam):
            owner = ctypes.c_ulong(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and self._user32.IsWindowVisible(hwnd):
                self._user32.GetClassNameW(hwnd, name, 64)
                if name.value == "SDL_app":
                    found.append(hwnd)
                    return 0
            return 1

        self._user32.EnumWindows(self._enum_proc(visit), 0)
        return found[0] if found else None

    def bring_forward(self, hwnd) -> None:
        """Windows lets only the foreground's own thread hand the foreground on, so this thread
        joins its input for the moment it takes."""
        user32 = self._user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, self.SW_RESTORE)
        foreground = user32.GetForegroundWindow()
        if foreground and foreground != hwnd:
            self._before = foreground
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

    def minimise(self, hwnd) -> None:
        """Minimising leaves the stream the foreground window, still holding the pointer to its
        picture (measured): the front goes back to the window that had it, else the desktop."""
        user32 = self._user32
        user32.ShowWindow(hwnd, self.SW_MINIMIZE)
        before = self._before
        target = before if before and user32.IsWindow(before) and not user32.IsIconic(before) else user32.GetShellWindow()
        if target:
            self._take_front(target)
        user32.ClipCursor(None)
