"""Relay state: which video a creator channel plays next, and what is kept.

The choice depends only on the saved state, never on how many requests came
before: each channel start also opens a short connection to sample the palette,
so counting requests would skip a video every time the channel is chosen.
"""

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


if __name__ == "__main__":
    unittest.main()
