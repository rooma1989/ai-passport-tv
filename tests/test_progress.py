"""The progress bar's anchors (server/progress.py): what the device is told, and when."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import progress


class Answer:
    """A finished background question to the relay."""

    def __init__(self, result, done=True):
        self.result, self.done = result, done


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.answers = []
        self.asked = []

    def fetcher(self, url):
        self.asked.append(url)
        return self.answers.pop(0)

    def tracker(self):
        return progress.Tracker(self.fetcher, clock=lambda: self.now)

    def test_from_relay(self):
        self.assertEqual(progress.from_relay({"duration": 1420, "offset": 75.4}), {"dur": 1420000, "pos": 75400})
        self.assertEqual(progress.from_relay({"duration": 600}), {"dur": 600000}, "not streaming yet")
        self.assertEqual(progress.from_relay({"duration": 60, "offset": 99}), {"dur": 60000, "pos": 60000})
        self.assertEqual(progress.from_relay({"title": "x"}), {}, "length unknown")
        for bad in (0, -5, True, "600", 10 ** 9, float("nan")):
            self.assertEqual(progress.from_relay({"duration": bad, "offset": 1}), {}, bad)
        self.assertEqual(progress.from_relay({"duration": 600, "offset": -1}), {"dur": 600000})
        self.assertIsNone(progress.from_relay(None))
        self.assertIsNone(progress.from_relay([1]))

    def test_anchor_at_the_sources_first_sound(self):
        t = self.tracker()
        pending = Answer(None, done=False)
        self.answers.append(pending)
        t.begin("http://127.0.0.1:1/info/sr01", 0)
        self.assertEqual(self.asked, ["http://127.0.0.1:1/info/sr01"])
        self.assertIsNone(t.poll(), "still asking")
        pending.result, pending.done = {"dur": 1420000, "pos": 0}, True
        self.assertEqual(t.poll(), {"progress": {"dur": 1420000, "pos": 0, "at": 0}})
        self.assertIsNone(t.poll(), "once")

    def test_stop_and_a_new_source(self):
        t = self.tracker()
        self.answers.append(Answer({"dur": 600000, "pos": 0}))
        t.begin("u", 0)
        t.poll()
        # The source stalls after the sound stamped 59960: the bar stops at 60 s.
        self.assertEqual(t.stopped(60000), {"progress": {"dur": 600000, "pos": 0, "at": 0, "stop": 60000}})
        # A seek reopens it at 300 s; its first sound is stamped 64000.
        self.answers.append(Answer({"dur": 600000, "pos": 300000}))
        t.begin("u", 64000)
        self.assertEqual(t.poll(), {"progress": {"dur": 600000, "pos": 300000, "at": 64000}})

    def test_the_same_source_recovering_carries_on_from_where_it_stopped(self):
        t = self.tracker()
        self.answers.append(Answer({"dur": 600000, "pos": 120000}))
        t.begin("u", 4000)
        t.poll()
        t.stopped(14000)                       # 10 s played: at 130 s
        self.assertEqual(t.resumed(20000), {"progress": {"dur": 600000, "pos": 120000, "at": 10000}},
                         "6 s of silence pushed the anchor on by 6 s")
        self.assertIsNone(t.resumed(30000), "not stopped: nothing")

    def test_stopped_before_the_answer(self):
        t = self.tracker()
        pending = Answer(None, done=False)
        self.answers.append(pending)
        t.begin("u", 1000)
        self.assertIsNone(t.stopped(5000), "nothing known yet")
        pending.result, pending.done = {"dur": 600000, "pos": 50000}, True
        self.assertEqual(t.poll(), {"progress": {"dur": 600000, "pos": 50000, "at": 1000, "stop": 5000}})
        # Recovered while still asking: the anchor is pushed on by the gap.
        pending = Answer(None, done=False)
        self.answers.append(pending)
        t.begin("u", 9000)
        t.stopped(11000)
        t.resumed(15000)
        pending.result, pending.done = {"dur": 600000, "pos": 70000}, True
        self.assertEqual(t.poll(), {"progress": {"dur": 600000, "pos": 70000, "at": 13000}})

    def test_retries_while_the_relay_is_not_streaming_yet(self):
        t = self.tracker()
        self.answers += [Answer({"dur": 600000}), Answer({"dur": 600000}), Answer({"dur": 600000, "pos": 0})]
        t.begin("u", 0)
        self.assertIsNone(t.poll())
        self.assertIsNone(t.poll(), "not before the retry delay")
        self.assertEqual(len(self.asked), 1)
        self.now += progress.RETRY_S
        self.assertIsNone(t.poll())
        self.assertEqual(len(self.asked), 2)
        self.assertIsNone(t.poll())
        self.now += progress.RETRY_S
        t.poll()
        self.assertEqual(t.poll(), {"progress": {"dur": 600000, "pos": 0, "at": 0}})
        self.assertEqual(len(self.asked), progress.TRIES)

    def test_gives_up_after_the_tries(self):
        t = self.tracker()
        self.answers += [Answer({"dur": 600000}) for _ in range(progress.TRIES)]
        t.begin("u", 0)
        for _ in range(progress.TRIES * 3):
            self.assertIsNone(t.poll())
            self.now += progress.RETRY_S
        self.assertEqual(len(self.asked), progress.TRIES)

    def test_unknown_length_takes_the_bar_down_only_if_one_is_up(self):
        t = self.tracker()
        self.answers.append(Answer({}))
        t.begin("u", 0)
        self.assertIsNone(t.poll(), "nothing shown, nothing to take down")
        self.answers += [Answer({"dur": 600000, "pos": 0}), Answer({})]
        t.begin("u", 0)
        t.poll()
        t.begin("u", 40000)
        self.assertEqual(t.poll(), {"progress": 0})
        self.answers.append(Answer(None))
        t.begin("u", 80000)
        self.assertIsNone(t.poll(), "no answer at all: nothing said")

    def test_no_relay_no_question(self):
        t = self.tracker()
        t.begin(None, 0)
        self.assertEqual(self.asked, [])
        self.assertIsNone(t.poll())
        self.assertIsNone(t.stopped(1000))

    def test_reset(self):
        t = self.tracker()
        self.answers.append(Answer({"dur": 600000, "pos": 0}))
        t.begin("u", 0)
        t.poll()
        t.reset()
        self.assertIsNone(t.stopped(1000))
        self.assertFalse(t.shown)


if __name__ == "__main__":
    unittest.main()
