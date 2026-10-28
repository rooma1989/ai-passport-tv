"""Frequently watched live channels (server/relay/livetally.py, the Live page's 常看 group).

What is checked: time is counted, not clicks; old viewing halves every
HALF_LIFE_S so recent habits win; channels under MIN_S are left out and at most
FAVS_MAX are given, most watched first; only global-table channels (ch…) count;
each person's list is separate, single-user mode uses "0", and at most KEEP_MAX
channels are kept per person; the file is written at most every SAVE_EVERY_S and
read back; a tick credits an online TV to its owner and a PC viewer to
themself, never an offline TV, a warm stream nobody watches, or a video.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.relay import livetally


class TallyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "live-plays.json"
        self.t = 1_000_000.0
        self.tally = livetally.LiveTally(self.path, clock=lambda: self.t)

    def test_time_counts_and_short_visits_do_not(self):
        self.tally.add(1, "ch000", 600)
        self.tally.add(1, "ch005", 90)
        for _ in range(10):
            self.tally.add(1, "ch003", 30)
        self.assertEqual(self.tally.top(1), ["ch000", "ch003"])
        self.assertEqual(self.tally.top(2), [], "each person has their own")
        self.tally.add(None, "cast", 900)
        self.tally.add(None, "up01", 900)
        self.assertEqual(self.tally.top(None), [], "only global-table live channels")

    def test_recent_habits_win(self):
        self.tally.add(1, "ch000", 3000)
        self.t += 3 * livetally.HALF_LIFE_S
        self.tally.add(1, "ch007", 600)
        self.assertEqual(self.tally.top(1), ["ch007", "ch000"], "3000 s three half-lives ago is 375 s now")

    def test_caps(self):
        for i in range(livetally.KEEP_MAX + 10):
            self.tally.add(1, f"ch{i:03d}", 200 + i)
        self.assertEqual(len(self.tally._data["1"]), livetally.KEEP_MAX)
        self.assertNotIn("ch000", self.tally._data["1"], "the least watched go")
        self.assertEqual(len(self.tally.top(1)), livetally.FAVS_MAX)

    def test_saved_now_and_then_and_read_back(self):
        self.tally.add(1, "ch000", 600)
        self.tally.save()
        self.assertFalse(self.path.exists(), "not on every tick")
        self.t += livetally.SAVE_EVERY_S
        self.tally.save()
        again = livetally.LiveTally(self.path, clock=lambda: self.t)
        self.assertEqual(again.top(1), ["ch000"])
        self.assertNotIn("http", self.path.read_text())


class TickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.t = 2_000_000.0
        self.tally = livetally.LiveTally(root / "live-plays.json", clock=lambda: self.t)
        self.on = root / "on.json"
        self.on.write_text('{"channel": "ch004", "connected": true, "at": %f}' % self.t)
        self.off = root / "off.json"
        self.off.write_text('{"channel": "ch009", "connected": true, "at": %f}' % (self.t - 600))
        streams = [self.stream(3, "ch002"), self.stream(3, "ch002"), self.stream(3, "ch006", cheap=True),
                   self.stream(4, "BV1x", kind="video"), self.stream(5, "ch008", alive=False)]
        self.relay = types.SimpleNamespace(
            clock=lambda: self.t,
            devices=[{"id": "a", "now": self.on, "owner": 7}, {"id": "b", "now": self.off, "owner": 8}],
            watch=types.SimpleNamespace(streams=lambda: streams))

    @staticmethod
    def stream(user, cid, cheap=False, kind="live", alive=True):
        return types.SimpleNamespace(user=user, cheap=cheap, alive=alive,
                                     source=types.SimpleNamespace(key=(kind, cid, "u", "")))

    def test_who_gets_credited(self):
        for _ in range(5):
            livetally.tick(self.relay, self.tally, 30)
        self.assertEqual(self.tally.top(7), ["ch004"], "the online TV's owner")
        self.assertEqual(self.tally.top(8), [], "an offline TV")
        self.assertEqual(self.tally.top(3), ["ch002"], "a PC viewer, once per channel")
        self.assertEqual(self.tally._data["3"]["ch002"][0], 150)
        self.assertNotIn("4", self.tally._data)
        self.assertNotIn("5", self.tally._data)
        self.assertNotIn("ch006", self.tally._data.get("3", {}), "a warm stream nobody watches")


if __name__ == "__main__":
    unittest.main()
