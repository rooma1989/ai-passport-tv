"""The live server warns once at start-up when the colour ladder is in use.

The ladder shifts colours from frame to frame (blue/yellow flicker was seen on a
device); the default, steady, needs no numpy and does not, so a ladder left in a
production unit is almost always a forgotten stopgap.
"""
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import live, tv_server


class DegradeWarning(unittest.TestCase):
    def test_ladder_warns_in_one_line(self):
        with mock.patch.object(live, "DEGRADE", "ladder"):
            message = live.degrade_warning()
        self.assertIsNotNone(message)
        self.assertTrue(message.startswith("WARNING: TV_DEGRADE=ladder"))
        self.assertNotIn("\n", message)

    def test_default_is_silent_even_without_numpy(self):
        with mock.patch.object(live, "DEGRADE", "steady"), \
                mock.patch.object(live.perceptual, "AVAILABLE", False):
            self.assertIsNone(live.degrade_warning())

    def test_main_prints_it_once_before_serving(self):
        class FakeServer:
            def __init__(self, *args, **kwargs):
                self.stop = mock.Mock()
                self.failed = self.rejected = self.dropped_video = 0
            def serve(self):
                pass
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8")
        with mock.patch.object(live, "DEGRADE", "ladder"), \
                mock.patch.object(tv_server, "AVServer", FakeServer), \
                mock.patch.object(tv_server, "print_where_to_connect"), \
                mock.patch.object(tv_server.signal, "signal"), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="utf-8")):
            self.assertEqual(tv_server.main(["live", "--channel", "ch000"]), 0)
            out.flush()
        lines = raw.getvalue().decode("utf-8").splitlines()
        self.assertEqual(sum(line.startswith("WARNING: TV_DEGRADE=ladder") for line in lines), 1)


if __name__ == "__main__":
    unittest.main()
