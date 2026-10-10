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
            self.assertEqual(events.read_event(path), (2, "return", ""))
            self.assertEqual(events.write_event(path, "switch", channel="ch013"), 3)
            self.assertEqual(events.read_event(path), (3, "switch", "ch013"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

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


if __name__ == "__main__":
    unittest.main()
