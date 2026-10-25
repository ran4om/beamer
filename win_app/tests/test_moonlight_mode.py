"""Moonlight mode: the ways out open a stream of the peer instead of sending input across, and the
way back hides it."""

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from links_rig import B, Rig
from core.return_edge import Rect
import moonlight_mode
from moonlight_mode import HomePush, MoonlightMode

SCREEN = [Rect(0, 0, 1366, 768)]


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
        self.calls = []

    def foreground_pid(self):
        return self.foreground

    def stream_window(self, pid):
        return self.window

    def bring_forward(self, hwnd):
        self.calls.append(("forward", hwnd))
        self.foreground = 4242

    def clip_rect(self):
        return self.clip

    def minimise(self, hwnd):
        self.calls.append(("minimise", hwnd))
        self.foreground = None


class HomePushTests(unittest.TestCase):
    def test_a_push_through_the_left_edge_breaks_through_at_the_resistance(self):
        push = HomePush("left", 100)
        self.assertFalse(push.feed(SCREEN, (0, 300), -60, 0, 1.0))
        self.assertTrue(push.feed(SCREEN, (0, 300), -60, 0, 1.1))

    def test_moving_away_or_off_the_edge_starts_again(self):
        push = HomePush("left", 100)
        push.feed(SCREEN, (0, 300), -60, 0, 1.0)
        push.feed(SCREEN, (0, 300), 5, 0, 1.05)
        self.assertFalse(push.feed(SCREEN, (0, 300), -60, 0, 1.1))
        push.feed(SCREEN, (40, 300), -60, 0, 1.15)
        self.assertFalse(push.feed(SCREEN, (0, 300), -60, 0, 1.2))

    def test_a_pause_starts_the_push_again(self):
        push = HomePush("left", 100)
        push.feed(SCREEN, (0, 300), -60, 0, 1.0)
        self.assertFalse(push.feed(SCREEN, (0, 300), -60, 0, 2.0))

    def test_the_right_edge_is_the_last_column(self):
        push = HomePush("right", 10)
        self.assertTrue(push.feed(SCREEN, (1365, 10), 20, 0, 1.0))


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "moonlight.json"
        exe = Path(self.folder.name) / "Moonlight.exe"
        exe.write_bytes(b"")
        self.path.write_text(json.dumps({"on": True, "exe": str(exe)}), encoding="utf-8")
        self.windows = FakeWindows()
        self.launched = []
        self.cursor = (0, 300)
        self.now = [0.0]

        def popen(command, **_output):
            self.launched.append(command)
            process = FakeProcess()
            self.windows.window = 77
            return process

        refocus = mock.patch.object(moonlight_mode, "REFOCUS_SECONDS", 0)
        refocus.start()
        self.addCleanup(refocus.stop)
        self.mode = MoonlightMode(self.path, windows=self.windows, popen=popen, clock=lambda: self.now[0],
                                  monitors=lambda: SCREEN, cursor=lambda: self.cursor, resistance=lambda: 100)

    def run_jobs(self):
        while not self.mode._jobs.empty():
            job, reason = self.mode._jobs.get()
            if job == "hide":
                self.mode._hide(reason)
            else:
                getattr(self.mode, "_" + job)()

    def test_off_takes_nothing(self):
        self.mode.settings["on"] = False
        self.assertFalse(self.mode.cross_out(B))
        self.assertFalse(self.mode.toggle())
        self.assertFalse(self.mode.motion(-500, 0))

    def test_the_way_out_launches_the_stream_with_absolute_mouse(self):
        self.assertTrue(self.mode.cross_out(B))
        self.run_jobs()
        command = self.launched[0]
        self.assertEqual(command[1:4], ["stream", "kia-home-link", "Desktop"])
        self.assertIn("--absolute-mouse", command)
        self.assertEqual(command[command.index("--display-mode") + 1], "borderless")
        self.assertEqual(self.windows.calls, [("forward", 77)])

    def test_the_way_back_hides_the_stream_and_the_next_way_out_shows_it_again(self):
        self.mode.cross_out(B)
        self.run_jobs()
        self.windows.foreground = 4242
        self.assertTrue(self.mode.motion(-60, 0))
        self.assertTrue(self.mode.motion(-60, 0))
        self.run_jobs()
        self.assertEqual(self.windows.calls[-1], ("minimise", 77))
        self.windows.foreground = None
        self.assertFalse(self.mode.motion(-60, 0))
        self.mode.cross_out(B)
        self.run_jobs()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.windows.calls[-1], ("forward", 77))

    def test_the_way_back_is_the_edge_of_the_picture_the_pointer_is_held_to(self):
        self.mode.cross_out(B)
        self.run_jobs()
        self.windows.clip = Rect(68, 0, 1229, 768)
        self.cursor = (68, 300)
        self.mode.motion(-60, 0)
        self.mode.motion(-60, 0)
        self.run_jobs()
        self.assertEqual(self.windows.calls[-1], ("minimise", 77))

    def test_coming_to_the_front_is_told_as_the_grab_settles_and_then_now_and_again(self):
        told = []
        self.mode.on_front = lambda: told.append(self.now[0])
        self.mode.cross_out(B)
        self.run_jobs()
        start = self.now[0]
        while self.now[0] < start + 8.05:
            self.mode._watch_front()
            self.now[0] = round(self.now[0] + 0.05, 2)
        self.assertEqual([round(t - start, 2) for t in told], [0.15, 0.5, 1.0, 2.0, 5.0, 8.0])
        self.mode._hide()
        self.mode._watch_front()
        self.mode._show()
        self.mode._watch_front()
        self.now[0] += 0.2
        self.mode._watch_front()
        self.assertEqual(len(told), 7)

    def test_a_moonlight_left_without_its_stream_is_started_again(self):
        self.mode.cross_out(B)
        self.run_jobs()
        first = self.mode._process
        self.mode._hide()
        self.windows.window = None
        self.mode.cross_out(B)
        self.run_jobs()
        self.assertEqual(len(self.launched), 2)
        self.assertIsNotNone(first.returncode)

    def test_quitting_beamer_closes_its_stream(self):
        self.mode.cross_out(B)
        self.run_jobs()
        process = self.mode._process
        self.mode.stop()
        self.assertIsNotNone(process.returncode)
        self.assertEqual(self.launched[-1][1:], ["quit", "kia-home-link"])

    def test_the_shortcut_shows_then_hides(self):
        self.mode.toggle()
        self.run_jobs()
        self.windows.foreground = 4242
        self.mode.toggle()
        self.run_jobs()
        self.assertEqual([call[0] for call in self.windows.calls], ["forward", "minimise"])

    def test_a_hidden_stream_closes_after_its_idle_time(self):
        self.mode.cross_out(B)
        self.run_jobs()
        process = self.mode._process
        self.mode._hide()
        self.now[0] += 301
        self.mode._close_if_idle()
        self.assertIsNone(self.mode._process)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(self.launched[-1][1:], ["quit", "kia-home-link"])

    def test_turning_off_is_saved_and_closes_the_stream(self):
        self.mode.cross_out(B)
        self.run_jobs()
        self.mode.set_on(False)
        self.run_jobs()
        self.assertIsNone(self.mode._process)
        self.assertFalse(moonlight_mode.load(self.path)["on"])


class SenderHookTests(unittest.TestCase):
    def test_a_way_out_the_mode_takes_sends_nothing_and_stays_armed(self):
        rig = Rig()
        taken = []
        rig.sender.cross_out = lambda peer: taken.append(peer) or True
        rig.push(4)
        self.assertEqual(taken, [B])
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


if __name__ == "__main__":
    unittest.main()
