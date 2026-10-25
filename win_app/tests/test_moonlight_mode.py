"""Moonlight mode: the ways out open a stream of the peer instead of sending input across, the
stream's edges lead home or on to the peer's other displays as Beamer's layout has them, and the
way home hides the stream and lands the pointer where it would have come back."""

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from links_rig import B, Rig
from core import protocol
from core.return_edge import Rect
import moonlight_mode
from moonlight_mode import MoonlightMode, Ways

SCREEN = [Rect(0, 0, 1366, 768)]
# Moonlight holds the pointer to the picture: a 16:10 stream on this 16:9 screen.
PICTURE = Rect(68, 0, 1229, 768)
PEER = protocol.id_text(B)
# The peer above this PC, with two displays side by side: "1" on the left, "0" on the right.
LAYOUT = {PEER: {"displays": {"0": {"x": 1680, "y": 0, "w": 1680, "h": 1050},
                              "1": {"x": 0, "y": 0, "w": 1680, "h": 1050}}}}
ZONES = [
    {"peer": PEER, "kind": "part", "parts": ["start", "middle"], "display": "0", "side": "top",
     "target_display": "1", "target_side": "bottom", "contact_span": [0.0, 0.5], "target_span": [0.0, 1.0]},
    {"peer": PEER, "kind": "part", "parts": ["middle", "end"], "display": "0", "side": "top",
     "target_display": "0", "target_side": "bottom", "contact_span": [0.5, 1.0], "target_span": [0.0, 1.0]},
]


class Outcome:
    def __init__(self, edge, offset, display=None, side=None):
        self.edge, self.offset, self.display, self.side = edge, offset, display, side


class FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 1


class FakeWindows:
    def __init__(self):
        self.foreground = None
        self.window = None
        self.clip = None
        self.process = None
        self.calls = []

    def foreground_pid(self):
        return self.foreground

    def stream_window(self, pid):
        return self.window

    def bring_forward(self, hwnd):
        self.calls.append(("forward", hwnd))
        self.foreground = 4242
        self.clip = PICTURE

    def clip_rect(self):
        return self.clip

    def hide(self, hwnd):
        self.calls.append(("hide", hwnd))
        self.foreground = None
        self.clip = None

    def close(self, hwnd):
        # Moonlight ends the session and leaves by itself.
        self.calls.append(("close", hwnd))
        self.window = None
        if self.process is not None:
            self.process.returncode = 0

    def set_cursor(self, x, y):
        self.calls.append(("cursor", x, y))

    def switch_display(self, index):
        self.calls.append(("switch", index))

    def watch(self, pid):
        self.calls.append(("watch", pid))

    def want_stream(self, value):
        pass


class WaysTests(unittest.TestCase):
    def build(self, zones=ZONES, layout=LAYOUT):
        return Ways.build(PEER, zones, layout, 100, clock=lambda: 0.0)

    def push(self, ways, display, pointer, dx, dy, times=2):
        result = None
        for _ in range(times):
            result = ways.feed(display, PICTURE, pointer, dx, dy)
        return result

    def test_the_seam_between_two_displays_leads_on_from_each_side(self):
        ways = self.build()
        self.assertEqual(sorted((model.display, model.edge, other) for model, other in ways.onward),
                         [("0", "left", "1"), ("1", "right", "0")])

    def test_the_way_home_is_the_edge_the_zone_arrives_at_on_the_far_side(self):
        ways = self.build()
        push = self.push(ways, "1", (600, 767), 0, 60)
        self.assertEqual((push.kind, push.crossed), ("home", True))
        self.assertEqual((push.outcome.edge, push.outcome.display, push.outcome.side), ("top", "0", "top"))
        # The left display leads back onto the left half of this PC's top edge, in proportion.
        self.assertAlmostEqual(push.outcome.offset, ((600 - 68) / 1228) * 0.5, places=2)
        self.assertIsNone(self.push(ways, "1", (68, 300), -60, 0))

    def test_a_push_through_the_seam_lands_across_it(self):
        ways = self.build()
        push = self.push(ways, "1", (68 + 1228, 384), 60, 0)
        self.assertEqual((push.kind, push.to), ("switch", "0"))
        self.assertEqual(push.point, (1680, 525))

    def test_a_layout_with_this_pc_to_the_left_leads_home_on_the_left(self):
        zones = [{"peer": PEER, "kind": "edge", "display": "0", "side": "right",
                  "target_display": "1", "target_side": "left"}]
        ways = self.build(zones)
        self.assertIsNone(self.push(ways, "1", (600, 767), 0, 60))
        push = self.push(ways, "1", (68, 300), -60, 0)
        self.assertEqual((push.kind, push.outcome.edge), ("home", "right"))

    def test_without_zones_or_layout_the_home_edge_setting_leads_home(self):
        ways = Ways.build(PEER, [], {}, 100, "left", clock=lambda: 0.0)
        push = self.push(ways, None, (68, 300), -60, 0)
        self.assertEqual((push.kind, push.outcome.edge), ("home", "right"))


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "moonlight.json"
        exe = Path(self.folder.name) / "Moonlight.exe"
        exe.write_bytes(b"")
        self.path.write_text(json.dumps({"on": True, "exe": str(exe), "start_display": "1"}), encoding="utf-8")
        self.windows = FakeWindows()
        self.launched = []
        self.cursor = (600, 0)
        self.now = [0.0]
        self.homes = []
        self.glows = []

        def popen(command, **_output):
            self.launched.append(command)
            self.windows.window = 77
            self.windows.process = FakeProcess()
            return self.windows.process

        self.mode = MoonlightMode(self.path, windows=self.windows, popen=popen, clock=lambda: self.now[0],
                                  monitors=lambda: SCREEN, cursor=lambda: self.cursor, resistance=lambda: 100,
                                  geometry=lambda: (ZONES, LAYOUT))
        self.mode.on_home = lambda *where: self.homes.append(where)
        self.mode.on_pressure = lambda edge, pressure, crossed, part: self.glows.append((edge, crossed))

    def run_jobs(self):
        while not self.mode._jobs.empty():
            job, argument = self.mode._jobs.get()
            if job in ("show", "home", "hide", "switch"):
                getattr(self.mode, "_" + job)(argument)
            else:
                getattr(self.mode, "_" + job)()

    def cross(self, offset=0.25, display="1"):
        self.assertTrue(self.mode.cross_out(B, Outcome("bottom", offset, display, "bottom")))
        self.run_jobs()

    def push(self, dx, dy, times=2):
        for _ in range(times):
            self.assertTrue(self.mode.motion(dx, dy))
        self.run_jobs()

    def test_off_takes_nothing(self):
        self.mode.settings["on"] = False
        self.assertFalse(self.mode.cross_out(B))
        self.assertFalse(self.mode.toggle())
        self.assertFalse(self.mode.motion(-500, 0))

    def test_the_way_out_launches_the_stream_with_absolute_mouse_and_watches_its_windows(self):
        self.cross()
        command = self.launched[0]
        self.assertEqual(command[1:4], ["stream", "kia-home-link", "Desktop"])
        self.assertIn("--absolute-mouse", command)
        self.assertEqual(command[command.index("--display-mode") + 1], "borderless")
        self.assertEqual(self.windows.calls[:2], [("watch", 4242), ("forward", 77)])

    def test_the_stream_opens_on_the_display_crossed_to_with_the_pointer_where_it_arrived(self):
        self.cross(0.25, "1")
        self.assertNotIn("switch", [call[0] for call in self.windows.calls])
        cursor = [call for call in self.windows.calls if call[0] == "cursor"][-1]
        self.assertEqual(cursor[2], PICTURE.y + PICTURE.height - 1)
        self.assertAlmostEqual(cursor[1], PICTURE.x + 0.25 * (PICTURE.width - 1), delta=2)

    def test_crossing_to_the_other_display_switches_the_stream_to_it(self):
        self.cross(0.5, "0")
        self.assertIn(("switch", 0), self.windows.calls)

    def test_a_new_stream_with_no_start_display_known_is_switched_once_up(self):
        self.mode.settings["start_display"] = ""
        self.cross(0.25, "1")
        self.assertIn(("switch", 1), self.windows.calls)

    def test_the_way_home_follows_the_layout_and_lands_the_pointer_on_the_zone(self):
        self.cross(0.25, "1")
        self.cursor = (68, 300)
        self.push(-60, 0)
        self.assertEqual(self.homes, [])
        self.cursor = (600, 767)
        self.push(0, 60)
        self.assertEqual(self.windows.calls[-1], ("hide", 77))
        edge, offset, display, side = self.homes[0]
        self.assertEqual((edge, display, side), ("top", "0", "top"))
        self.assertLess(offset, 0.5)
        self.assertEqual(self.glows[-1], ("bottom", True))
        self.windows.foreground = None
        self.assertFalse(self.mode.motion(0, 60))

    def test_pushing_through_the_seam_switches_display_and_the_way_home_follows(self):
        arrivals = []
        self.mode.on_arrival = lambda *where: arrivals.append(where)
        self.cross(0.25, "1")
        self.cursor = (PICTURE.right, 384)
        self.push(60, 0)
        self.assertIn(("switch", 0), self.windows.calls)
        self.assertEqual(self.windows.calls[-1], ("cursor", PICTURE.x, 384))
        self.assertEqual(arrivals, [("left", PICTURE.x, 384)])
        self.cursor = (900, 767)
        self.push(0, 60)
        self.assertGreater(self.homes[0][1], 0.5)

    def test_after_a_switch_the_pointer_is_moved_again_once_the_host_has_changed_monitor(self):
        self.cross(0.25, "1")
        self.cursor = (PICTURE.right, 384)
        self.push(60, 0)
        self.cursor = (PICTURE.x, 384)
        before = len(self.windows.calls)
        self.mode._nudge()
        self.assertEqual(len(self.windows.calls), before)
        self.now[0] += moonlight_mode.SWITCH_SETTLE_SECONDS
        self.mode._nudge()
        self.assertEqual(self.windows.calls[before:], [("cursor", PICTURE.x + 1, 384)])

    def test_the_next_way_out_shows_the_hidden_stream_again(self):
        self.cross()
        self.mode.toggle()
        self.run_jobs()
        self.windows.foreground = None
        self.cross()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual([call[0] for call in self.windows.calls if call[0] in ("forward", "hide")],
                         ["forward", "hide", "forward"])

    def test_the_glow_breathes_on_the_edge_left_while_a_stream_connects(self):
        windows = self.windows
        polls = [0]

        def stream_window(_pid):
            polls[0] += 1
            self.now[0] += 0.3
            return 77 if polls[0] > 3 else None

        windows.stream_window = stream_window
        with mock.patch.object(moonlight_mode.time, "sleep"):
            self.cross()
        self.assertGreaterEqual(len([glow for glow in self.glows if glow == ("top", False)]), 3)

    def test_coming_to_the_front_is_told_as_the_grab_settles_and_then_now_and_again(self):
        told = []
        self.mode.on_front = lambda: told.append(self.now[0])
        self.cross()
        start = self.now[0]
        while self.now[0] < start + 8.05:
            self.mode._watch_front()
            self.now[0] = round(self.now[0] + 0.05, 2)
        self.assertEqual([round(t - start, 2) for t in told], [0.05, 0.15, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 5.0, 8.0])
        self.mode._hide()
        self.mode._watch_front()
        self.mode._show()
        self.mode._watch_front()
        self.now[0] += 0.2
        self.mode._watch_front()
        self.assertEqual(len(told), 11)

    def test_a_moonlight_left_without_its_stream_is_started_again(self):
        self.cross()
        first = self.mode._process
        self.mode._hide()
        self.windows.window = None
        self.cross()
        self.assertEqual(len(self.launched), 2)
        self.assertIsNotNone(first.returncode)

    def test_quitting_beamer_closes_its_stream_quietly(self):
        self.cross()
        process = self.mode._process
        self.mode.stop()
        self.assertIsNotNone(process.returncode)
        self.assertIn(("close", 77), self.windows.calls)
        self.assertEqual(len(self.launched), 1)

    def test_a_hidden_stream_closes_quietly_after_its_idle_time(self):
        self.cross()
        process = self.mode._process
        self.mode._hide()
        self.now[0] += 301
        self.mode._close_if_idle()
        self.assertIsNone(self.mode._process)
        self.assertIsNotNone(process.returncode)
        self.assertIn(("close", 77), self.windows.calls)
        self.assertEqual(len(self.launched), 1)

    def test_turning_off_is_saved_and_closes_the_stream(self):
        self.cross()
        self.mode.set_on(False)
        self.run_jobs()
        self.assertIsNone(self.mode._process)
        self.assertFalse(moonlight_mode.load(self.path)["on"])

    def test_a_stream_that_never_comes_up_is_ended_and_told(self):
        alerts = []
        self.mode.on_alert = lambda title, message: alerts.append(message)
        self.windows.stream_window = lambda _pid: None

        def later(_seconds):
            self.now[0] += 1

        with mock.patch.object(moonlight_mode.time, "sleep", later):
            self.cross()
        self.assertIsNone(self.mode._process)
        self.assertEqual(len(alerts), 1)


class SenderHookTests(unittest.TestCase):
    def test_a_way_out_the_mode_takes_sends_nothing_and_stays_armed(self):
        rig = Rig()
        taken = []
        rig.sender.cross_out = lambda peer, outcome: taken.append((peer, outcome.edge)) or True
        rig.push(4)
        # The outcome names the edge on the far side.
        self.assertEqual(taken, [(B, "right")])
        self.assertFalse(rig.sender.redirecting)
        self.assertEqual(rig.sent(B, "focus"), [])
        rig.sender.cross_out = None
        rig.sender._models[0][1]._arrival_at = None
        rig.push(4)
        self.assertTrue(rig.sender.redirecting)

    def test_local_motion_that_is_taken_never_reaches_the_zones(self):
        rig = Rig()
        rig.sender.local_motion = lambda dx, dy: True
        rig.push(10)
        self.assertFalse(rig.sender.redirecting)

    def test_landing_from_the_stream_starts_the_zones_grace(self):
        rig = Rig()
        rig.sender.land("right", 0.5)
        self.assertTrue(all(model._arrival_at is not None for _peer, model in rig.sender._models))


if __name__ == "__main__":
    unittest.main()
