"""Relay state: which video a creator channel plays next, and what is kept.

The choice depends only on the saved state, never on how many requests came
before: each channel start also opens a short connection to sample the palette,
so counting requests would skip a video every time the channel is chosen.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import state as relay_state
from server.relay.state import State, Video


def videos(*ids):
    """Newest first, as the upload list returns them."""
    return [Video(bvid=v, title=f"t{v}", duration=600, pubdate=1000 - n)
            for n, v in enumerate(ids)]


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "relay-state.json"
        self.state = State(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, source="1", ids=("a", "b", "c")):
        return self.state.add("bilibili", source, f"up{source}", videos(*ids), now=0.0)

    def test_ids_are_lowest_free_and_reused(self):
        self.assertEqual(self.add("1").id, "up01")
        self.assertEqual(self.add("2").id, "up02")
        self.assertTrue(self.state.remove("up01"))
        self.assertEqual(self.add("3").id, "up01")

    def test_full_and_duplicate_rejected(self):
        for n in range(relay_state.MAX_CREATORS):
            self.add(str(n))
        with self.assertRaises(ValueError):
            self.add("new")
        self.state.remove("up05")
        with self.assertRaises(ValueError):
            self.add("1")

    def test_remove_unknown(self):
        self.assertFalse(self.state.remove("up09"))

    def test_newest_unwatched_first(self):
        cid = self.add().id
        self.assertEqual(self.state.next_video(cid)[0].bvid, "a")
        self.state.mark_watched(cid, "a")
        self.assertEqual(self.state.next_video(cid), (self.state.get(cid).videos[1], 0.0))

    def test_resume_backs_off_and_never_negative(self):
        cid = self.add().id
        self.state.save_position(cid, "b", 125.0)
        video, offset = self.state.next_video(cid)
        self.assertEqual((video.bvid, offset), ("b", 115.0))
        self.state.save_position(cid, "b", 4.0)
        self.assertEqual(self.state.next_video(cid)[1], 0.0)

    def test_mark_watched_drops_position(self):
        cid = self.add().id
        self.state.save_position(cid, "a", 50.0)
        self.state.mark_watched(cid, "a")
        self.assertEqual(self.state.next_video(cid)[0].bvid, "b")

    def test_wrap_when_all_watched(self):
        cid = self.add(ids=("a", "b")).id
        self.state.mark_watched(cid, "a")
        self.state.mark_watched(cid, "b")
        self.assertEqual(self.state.next_video(cid), (self.state.get(cid).videos[0], 0.0))
        self.assertEqual(self.state.get(cid).watched, [])

    def test_no_videos(self):
        cid = self.add(ids=()).id
        self.assertIsNone(self.state.next_video(cid))
        self.assertIsNone(self.state.next_video("up12"))

    def test_set_videos_prunes_forgotten_ids(self):
        cid = self.add().id
        self.state.mark_watched(cid, "c")
        self.state.save_position(cid, "b", 30.0)
        self.state.set_videos(cid, videos("new", "a", "b"), now=99.0)
        creator = self.state.get(cid)
        self.assertEqual(creator.watched, [])
        self.assertEqual(creator.positions, {"b": 30.0})
        self.assertEqual(creator.fetched_at, 99.0)

    def test_round_trip_and_mode(self):
        cid = self.add().id
        self.state.mark_watched(cid, "a")
        self.state.save_position(cid, "b", 12.5)
        self.state.save()
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o640)
        again = State(self.path)
        self.assertEqual(again.get(cid), self.state.get(cid))

    def test_corrupt_file_refused(self):
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            State(self.path)


    def test_cast_position_and_end(self):
        video = Video(bvid="BVcast", title="cast", duration=300, pubdate=0)
        self.state.set_cast(video)
        self.assertEqual((self.state.cast, self.state.cast_position, self.state.cast_done),
                         (video, 0.0, False))
        self.state.save_cast_position("BVcast", 42.0)
        self.state.save_cast_position("BVother", 99.0)
        self.assertEqual(self.state.cast_position, 42.0)
        self.state.cast_ended("BVother")
        self.assertFalse(self.state.cast_done)
        self.state.cast_ended("BVcast")
        self.assertEqual((self.state.cast_position, self.state.cast_done), (0.0, True))
        self.state.save()
        again = State(self.path)
        self.assertEqual((again.cast, again.cast_done), (video, True))

    def test_new_cast_resets(self):
        self.state.set_cast(Video(bvid="a", title="", duration=0, pubdate=0))
        self.state.save_cast_position("a", 10.0)
        self.state.cast_ended("a")
        self.state.set_cast(Video(bvid="b", title="", duration=0, pubdate=0))
        self.assertEqual((self.state.cast_position, self.state.cast_done), (0.0, False))

    def test_old_file_without_cast_loads(self):
        self.path.write_text('{"creators": []}')
        self.assertIsNone(State(self.path).cast)


class SeriesStateTests(unittest.TestCase):
    """A series is a channel whose list is the episodes in order."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "relay-state.json"
        self.state = State(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def episodes(self, count):
        return [Video(bvid=f"ep{n}/{n}", title=f"第{n}集", duration=600, pubdate=0)
                for n in range(1, count + 1)]

    def add(self, source="ss1", count=3):
        return self.state.add("bilibili", source, "海尔兄弟", self.episodes(count), now=0.0,
                              kind="series")

    def test_own_ids_after_creators(self):
        self.assertEqual(self.add().id, "sr01")
        self.state.add("bilibili", "1", "UP", videos("a"), now=0.0)
        self.assertEqual([c.id for c in self.state.creators], ["up01", "sr01"])
        # The same source may be an UP channel and a series without clashing.
        self.assertEqual(self.add("ss2").id, "sr02")
        with self.assertRaises(ValueError):
            self.add("ss1")

    def test_long_series_kept_whole(self):
        cid = self.add(count=relay_state.LIST_SIZE * 10).id
        self.assertEqual(len(self.state.get(cid).videos), relay_state.LIST_SIZE * 10)

    def test_plays_in_order_and_resumes(self):
        cid = self.add().id
        self.assertEqual(self.state.episode(cid), 1)
        self.state.mark_watched(cid, "ep1/1")
        self.assertEqual(self.state.next_video(cid)[0].bvid, "ep2/2")
        self.state.save_position(cid, "ep2/2", 300.0)
        self.assertEqual(self.state.next_video(cid), (self.state.get(cid).videos[1], 290.0))
        self.assertEqual(self.state.episode(cid), 2)
        self.state.mark_watched(cid, "ep2/2")
        self.state.mark_watched(cid, "ep3/3")
        self.assertEqual(self.state.episode(cid), 1)
        self.assertEqual(self.state.next_video(cid)[0].bvid, "ep1/1")

    def test_play_from_an_episode(self):
        cid = self.add(count=5).id
        self.state.mark_watched(cid, "ep1/1")
        self.state.save_position(cid, "ep2/2", 300.0)
        self.state.mark_watched(cid, "ep4/4")
        # Forward: everything before counts as watched, it and later as not.
        self.assertEqual(self.state.play_from(cid, 4).bvid, "ep4/4")
        creator = self.state.get(cid)
        self.assertEqual(creator.watched, ["ep1/1", "ep2/2", "ep3/3"])
        self.assertEqual(creator.positions, {}, "other episodes' progress would be resumed first")
        self.assertEqual(self.state.next_video(cid), (creator.videos[3], 0.0))
        self.assertEqual(self.state.episode(cid), 4)
        # Back: later episodes are unwatched again.
        self.state.mark_watched(cid, "ep4/4")
        self.state.play_from(cid, 2)
        self.assertEqual(self.state.get(cid).watched, ["ep1/1"])
        self.assertEqual(self.state.next_video(cid)[0].bvid, "ep2/2")
        # The first episode: nothing watched.
        self.state.play_from(cid, 1)
        self.assertEqual(self.state.get(cid).watched, [])

    def test_play_from_keeps_the_episode_in_progress_only(self):
        cid = self.add(count=3).id
        self.state.mark_watched(cid, "ep1/1")
        self.state.save_position(cid, "ep2/2", 300.0)
        self.state.play_from(cid, 2)
        self.assertEqual(self.state.next_video(cid), (self.state.get(cid).videos[1], 290.0),
                         "the episode already being watched carries on")
        # A position on an episode that is not the one in progress is not kept.
        self.state.save_position(cid, "ep3/3", 100.0)
        self.state.play_from(cid, 3)
        self.assertEqual(self.state.next_video(cid), (self.state.get(cid).videos[2], 0.0))

    def test_play_from_refuses_bad_episodes_and_creators(self):
        cid = self.add().id
        self.state.mark_watched(cid, "ep1/1")
        for n in (0, 4, -1):
            self.assertIsNone(self.state.play_from(cid, n))
        self.assertEqual(self.state.get(cid).watched, ["ep1/1"], "nothing changed")
        up = self.state.add("bilibili", "1", "UP", videos("a", "b"), now=0.0).id
        self.assertIsNone(self.state.play_from(up, 1))
        self.assertIsNone(self.state.play_from("sr09", 1))

    def test_kind_persisted_and_old_files_are_creators(self):
        self.add()
        self.state.add("bilibili", "1", "UP", videos("a"), now=0.0)
        self.state.save()
        again = State(self.path)
        self.assertEqual([(c.id, c.kind) for c in again.creators], [("up01", "up"), ("sr01", "series")])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for c in data["creators"]:
            del c["kind"]
        data["creators"] = [c for c in data["creators"] if c["id"] == "up01"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(State(self.path).creators[0].kind, "up")


class HistoryAndSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "relay-state.json"
        self.state = State(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def video(self, vid, site="bilibili"):
        return Video(bvid=vid, title=f"t{vid}", duration=10, pubdate=0, site=site)

    def test_history_newest_first_dedupe_and_limit(self):
        for n in range(relay_state.HISTORY_SIZE + 5):
            self.state.remember(self.video(f"v{n}"), at=float(n))
        self.assertEqual(len(self.state.history), relay_state.HISTORY_SIZE)
        self.assertEqual(self.state.history[0]["id"], f"v{relay_state.HISTORY_SIZE + 4}")
        self.state.remember(self.video("v10"), at=999.0)
        self.assertEqual([h["id"] for h in self.state.history].count("v10"), 1)
        self.assertEqual(self.state.history[0], {"site": "bilibili", "id": "v10", "title": "tv10", "at": 999.0})
        # Same id on another site is another entry.
        self.state.remember(self.video("v10", "youtube"), at=1000.0)
        self.assertEqual(self.state.history[0]["site"], "youtube")
        self.assertTrue(self.state.forget("youtube", "v10"))
        self.assertFalse(self.state.forget("youtube", "v10"))
        self.assertEqual(self.state.history_entry("bilibili", "v10")["title"], "tv10")

    def test_sessions(self):
        token = self.state.new_session(now=100.0)
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(self.state.session_ok(token, now=100.0 + relay_state.SESSION_S - 1))
        self.assertFalse(self.state.session_ok(token, now=100.0 + relay_state.SESSION_S + 1))
        self.assertFalse(self.state.session_ok("nope", now=100.0))
        self.assertFalse(self.state.session_ok("", now=100.0))
        tokens = [self.state.new_session(now=float(n)) for n in range(relay_state.MAX_SESSIONS + 3)]
        self.assertEqual(len(self.state.sessions), relay_state.MAX_SESSIONS)
        self.assertFalse(self.state.session_ok(tokens[0], now=10.0))
        self.assertTrue(self.state.session_ok(tokens[-1], now=10.0))

    def test_code_issue_ends_sessions(self):
        code = self.state.ensure_code()
        self.assertRegex(code, r"^[A-Z2-7]{20}$")
        self.assertEqual(self.state.ensure_code(), code)
        token = self.state.new_session(now=1.0)
        new = self.state.new_code()
        self.assertNotEqual(new, code)
        self.assertFalse(self.state.session_ok(token, now=2.0))

    def test_persisted(self):
        self.state.remember(self.video("v1"), at=1.0)
        token = self.state.new_session(now=1.0)
        code = self.state.ensure_code()
        self.state.save()
        again = State(self.path)
        self.assertEqual(again.history, self.state.history)
        self.assertTrue(again.session_ok(token, now=2.0))
        self.assertEqual(again.ensure_code(), code)


if __name__ == "__main__":
    unittest.main()
