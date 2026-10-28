"""Line quality: real speed and whether the picture is genuine (server/relay/linequality.py, routes.py).

What is checked: pictures are sets of frames over a few seconds and two lines
are compared by their most alike pair, so lines a few seconds apart still match;
flat frames (black, one colour) are never compared; a line whose pictures
disagree with two siblings that agree is counted as odd once (undecided with
fewer than three lines or no agreeing pair), and suspect after ODD_MIN of the
last ODD_SEEN sweeps; a line whose picture does not move and is identical to its
own picture STILL_GAP_S earlier is still (a moving picture never is); finished
recordings are never grabbed and lines whose check failed are skipped; speed is
measured at most every PACE_EVERY_S and blended with the last value; slow lines
(weak) go after working lines and before failed or suspect ones, and a slow last
good line loses its first place; the PC player records the speed it measured
while a line played; saving the table keeps quality of lines still there; the
admin view and the PC line menu say why; sweeps wait for START_DELAY_S, keep
SWEEP_GAP_S apart and one at a time; nothing is logged with an address; an
admin can look at a saved line's current picture (only lines in the table).
"""

from __future__ import annotations

import base64
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import routes
from server.relay import admin, channeltable, linecheck, linequality

LINES = ("https://a/1.m3u8", "https://b/1.m3u8", "https://c/1.m3u8", "https://d/1.m3u8")
TABLE = "ch000 | CCTV5 | https://a/1.m3u8\n"


def pic(base: int) -> bytes:
    """A frame with some texture (flat frames are ignored)."""
    return bytes((base + (i * 7) % 60) % 256 for i in range(linequality.THUMB_BYTES))


def near(frames, by: int = 3):
    shift = lambda f: bytes(min(255, x + by) for x in f)  # noqa: E731
    return [shift(f) for f in frames] if isinstance(frames, list) else shift(frames)


def still(base: int) -> list[bytes]:
    return [pic(base)] * 4


def moving(base: int) -> list[bytes]:
    return [pic(base + 30 * k) for k in range(4)]


def stripes(base: int) -> list[bytes]:
    """Another channel altogether: a different texture, so no frame resembles A or B."""
    return [bytes((base + 120 * ((i // 4 + k) % 2)) % 256 for i in range(linequality.THUMB_BYTES)) for k in range(4)]


A, B = moving(10), moving(150)


class JudgeTests(unittest.TestCase):
    def test_the_odd_one_out(self):
        marks = {"a": A, "b": near(A), "c": B}
        self.assertEqual(linequality.judge_odd(marks), {"a": False, "b": False, "c": True})

    def test_lines_a_few_seconds_apart_still_match(self):
        late = A[2:] + [pic(130), pic(140)]          # starts two frames later than A
        self.assertEqual(linequality.judge_odd({"a": A, "b": late, "c": near(A)}), {"a": False, "b": False, "c": False})

    def test_undecided(self):
        self.assertEqual(linequality.judge_odd({"a": A, "b": B}), {"a": None, "b": None}, "two lines: who is wrong?")
        self.assertEqual(set(linequality.judge_odd({"a": A, "b": B, "c": stripes(20)}).values()), {None}, "nobody agrees")

    def test_flat_frames_are_not_compared(self):
        black = [bytes(linequality.THUMB_BYTES)] * 4
        self.assertTrue(linequality.flat(black[0]))
        self.assertIsNone(linequality.distance(black, black))
        self.assertEqual(set(linequality.judge_odd({"a": black, "b": black, "c": A}).values()), {None})
        self.assertFalse(linequality.frozen(black))

    def test_mad_and_frozen(self):
        self.assertEqual(linequality.mad(pic(1), pic(1)), 0)
        self.assertEqual(linequality.mad(pic(1), near(pic(1), 10)), 10)
        self.assertTrue(linequality.frozen(still(5)))
        self.assertFalse(linequality.frozen(moving(5)))


class RoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.table = Path(self.tmp.name) / "channels.txt"
        self.table.write_text(TABLE, encoding="utf-8")

    def test_pace_is_blended_and_goes_stale(self):
        self.assertTrue(routes.remember_pace(self.table, "ch000", LINES[1], 0.4, 1000))
        self.assertTrue(routes.remember_pace(self.table, "ch000", LINES[1], 0.6, 2000))
        q = routes.quality_in(routes.entry_of(self.table, "ch000"))[routes.fingerprint(LINES[1])]
        self.assertEqual(q["pace"], 0.5)
        self.assertFalse(routes.remember_pace(self.table, "ch000", LINES[1], 0.52, 2100), "small change: no write")
        entry = routes.entry_of(self.table, "ch000")
        self.assertEqual(routes.weak_in(entry, now=3000), {routes.fingerprint(LINES[1])})
        self.assertEqual(routes.weak_in(entry, now=2000 + routes.PACE_FRESH_S), set(), "an old measurement")

    def test_order_slow_then_failed_and_suspect(self):
        lines = [(u, "") for u in LINES]
        marks = [routes.fingerprint(u) for u in LINES]
        order = routes.preferred(lines, good=marks[0], bad={marks[3]}, speeds={marks[0]: 900, marks[1]: 800},
                                 weak={marks[0]})
        self.assertEqual(order, [1, 2, 0, 3], "a slow last-good line loses its first place, stays before the bad")
        cycle = routes.RouteCycle(lines, marks[1], bad={marks[3]}, weak={marks[1]})
        self.assertEqual(cycle.order[-2:], [1, 3])

    def test_odd_needs_two_of_three_and_saving_keeps_it(self):
        mark = routes.fingerprint(LINES[2])
        for seen in (True, None, False, True, True):
            routes.record_quality(self.table, {"ch000": {mark: {"odd": seen}}})
            if seen is False:
                self.assertEqual(routes.suspect_in(routes.entry_of(self.table, "ch000")), set())
        entry = routes.entry_of(self.table, "ch000")
        self.assertEqual(entry["quality"][mark]["odd"], [False, True, True])
        self.assertEqual(routes.suspect_in(entry), set(), "a match in between: not every recent sweep disagreed")
        routes.record_quality(self.table, {"ch000": {mark: {"odd": True}}})
        entry = routes.entry_of(self.table, "ch000")
        self.assertEqual(routes.suspect_in(entry), {mark})
        data = routes.read_meta(self.table)
        rows = [{"id": "ch000", "url": LINES[0]}]
        channeltable.merge_meta(data, [{"id": "ch000", "url": LINES[0], "routes": [{"url": u} for u in LINES[1:3]]}], rows)
        self.assertIn(mark, data["ch000"]["quality"])
        routes.write_meta(self.table, data)
        view = channeltable.routes_view(self.table, [{"id": "ch000", "url": LINES[0]}])["ch000"]
        self.assertEqual(view["quality"], {LINES[2]: {"odd": True}})


class PeekTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.table = Path(self.tmp.name) / "channels.txt"
        self.table.write_text(TABLE, encoding="utf-8")
        routes.write_meta(self.table, {"ch000": {"routes": [{"url": LINES[1]}]}})
        self.relay = types.SimpleNamespace(main_channels=self.table, ffmpeg="ff")

    def test_only_saved_lines_are_looked_at(self):
        seen = []
        with unittest.mock.patch.object(linecheck, "grab_frame", lambda ff, url, agent, lenient=False:
                                        seen.append(url) or b"\xff\xd8jpeg"):
            answer = admin.peek(self.relay, {"id": "ch000", "url": LINES[1]})
            self.assertEqual(answer, {"ok": True, "jpeg": base64.b64encode(b"\xff\xd8jpeg").decode()})
            self.assertFalse(admin.peek(self.relay, {"id": "ch000", "url": "https://elsewhere/x"})["ok"])
            self.assertFalse(admin.peek(self.relay, {"id": "ch999", "url": LINES[0]})["ok"])
            self.assertFalse(admin.peek(self.relay, {"id": 3})["ok"])
        self.assertEqual(seen, [LINES[1]])
        with unittest.mock.patch.object(linecheck, "grab_frame", lambda *a, **k: None):
            self.assertEqual(admin.peek(self.relay, {"id": "ch000", "url": LINES[0]})["error"], "这条线路现在取不到画面")


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.table = Path(self.tmp.name) / "channels.txt"
        self.table.write_text(TABLE, encoding="utf-8")
        routes.write_meta(self.table, {"ch000": {"routes": [{"url": u} for u in LINES[1:]]}})
        self.t = 50_000.0
        self.logs: list[str] = []
        self.pictures = {LINES[0]: A, LINES[1]: near(A), LINES[2]: B, LINES[3]: near(A, 6)}
        self.measured: list[str] = []
        self.grabbed: list[str] = []
        self.sweeper = linequality.Sweeper(lambda: self.table, "ff", measure=self.measure, grab=self.grab,
                                           clock=lambda: self.t, log=self.logs.append, spawn=lambda fn, *a: fn(*a))

    def measure(self, url, agent):
        self.measured.append(url)
        return {"live": url != LINES[3], "pace": 0.4 if url == LINES[1] else 2.0}

    def grab(self, ffmpeg, url, agent, lenient=False):
        self.grabbed.append(url)
        return self.pictures[url]

    def entry(self):
        return routes.entry_of(self.table, "ch000")

    def test_a_sweep_measures_grabs_and_judges(self):
        found = self.sweeper.sweep("ch000")
        self.assertEqual(set(self.measured), set(LINES))
        self.assertNotIn(LINES[3], self.grabbed, "a finished recording is not judged by its picture")
        self.assertTrue(found[routes.fingerprint(LINES[2])]["odd"])
        self.assertEqual(routes.weak_in(self.entry(), now=self.t), {routes.fingerprint(LINES[1])})
        self.assertEqual(routes.suspect_in(self.entry()), set(), "one odd sweep is not enough")
        for k in (1, 2):
            self.t += 3600
            later = moving(40 * k)                                 # later every live picture has moved on
            self.pictures = {LINES[0]: later, LINES[1]: near(later), LINES[2]: stripes(60 * k), LINES[3]: later}
            self.sweeper.sweep("ch000")
            if k == 1:
                self.assertEqual(routes.suspect_in(self.entry()), set(), "two are not enough either")
        self.assertEqual(len(self.measured), 4, "speed is measured at most once a day")
        self.assertEqual(routes.suspect_in(self.entry()), {routes.fingerprint(LINES[2])})
        self.assertTrue(any("线路 3" in m and "对不上" in m for m in self.logs), self.logs)
        self.assertFalse(any("https://" in m for m in self.logs), self.logs)

    def test_a_picture_that_never_changes(self):
        self.pictures = {LINES[0]: still(5), LINES[1]: A, LINES[2]: near(A), LINES[3]: A}
        self.sweeper.sweep("ch000")
        self.pictures = {LINES[0]: still(5), LINES[1]: B, LINES[2]: A[:1] + near(B)[1:], LINES[3]: B}
        self.t += linequality.STILL_GAP_S + 1
        found = self.sweeper.sweep("ch000")
        self.assertTrue(found[routes.fingerprint(LINES[0])]["still"])
        self.assertFalse(found[routes.fingerprint(LINES[1])]["still"])
        self.assertFalse(found[routes.fingerprint(LINES[2])]["still"], "one old frame again, but the picture moves")
        self.assertIn(routes.fingerprint(LINES[0]), routes.suspect_in(self.entry()))
        self.assertTrue(any("线路 1" in m and "画面一直不变" in m for m in self.logs), self.logs)

    def test_failed_lines_are_skipped(self):
        routes.record_checks(self.table, "ch000", [(routes.fingerprint(LINES[2]), False, "403", 0, 1)])
        self.sweeper.sweep("ch000")
        self.assertNotIn(LINES[2], self.measured + self.grabbed)

    def test_schedule(self):
        self.assertFalse(self.sweeper.step(self.t), "waits after start")
        self.t += linequality.START_DELAY_S
        self.assertTrue(self.sweeper.step(self.t))
        self.assertFalse(self.sweeper.step(self.t + 1), "a gap between channels")
        self.assertFalse(self.sweeper.step(self.t + linequality.SWEEP_GAP_S), "the only channel was just swept")
        self.assertTrue(self.sweeper.step(self.t + linequality.SWEEP_EVERY_S))


if __name__ == "__main__":
    unittest.main()
