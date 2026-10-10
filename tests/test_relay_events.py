"""The event file the relay writes for the TV server: a sequence number that only grows."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import events


class EventTests(unittest.TestCase):
    def test_sequence_grows_and_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay-events.json"
            self.assertIsNone(events.read_event(path))
            self.assertEqual(events.write_event(path, "cast"), 1)
            self.assertEqual(events.write_event(path, "return"), 2)
            self.assertEqual(events.read_event(path), (2, "return", "", None, ""))
            self.assertEqual(events.write_event(path, "switch", channel="ch013"), 3)
            self.assertEqual(events.read_event(path), (3, "switch", "ch013", None, ""))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_list_keeps_the_last_twenty_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay-events.json"
            events.write_event(path, "switch", channel="ch001")
            events.write_event(path, "volume", value=70)
            self.assertEqual([(e.seq, e.action, e.channel, e.value) for e in events.read_events(path)],
                             [(1, "switch", "ch001", None), (2, "volume", "", 70)])
            for _ in range(30):
                events.write_event(path, "pause")
            listed = events.read_events(path)
            self.assertEqual(len(listed), events.KEEP)
            self.assertEqual(listed[-1].seq, 32)
            self.assertEqual([e.seq for e in listed], sorted(e.seq for e in listed))

    def test_old_single_event_file_still_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay-events.json"
            path.write_text('{"seq": 7, "action": "cast"}')
            self.assertEqual(events.read_event(path), (7, "cast", "", None, ""))
            self.assertEqual(events.write_event(path, "return"), 8)

    def test_values_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.json"
            with self.assertRaises(ValueError):
                events.write_event(path, "volume")
            with self.assertRaises(ValueError):
                events.write_event(path, "volume", value=101)

    def test_bad_file_reads_as_none_and_restarts_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay-events.json"
            path.write_text("{oops")
            self.assertIsNone(events.read_event(path))
            self.assertEqual(events.write_event(path, "cast"), 1)

    def test_unknown_action_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                events.write_event(Path(tmp) / "e.json", "reboot")
            with self.assertRaises(ValueError):
                events.write_event(Path(tmp) / "e.json", "switch")


class AudioEventTests(unittest.TestCase):
    def test_audio_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            events.write_event(path, "audio", value=1)
            last = events.read_event(path)
            self.assertEqual((last.action, last.value), ("audio", 1))
            with self.assertRaises(ValueError):
                events.write_event(path, "audio", value=2)


if __name__ == "__main__":
    unittest.main()
