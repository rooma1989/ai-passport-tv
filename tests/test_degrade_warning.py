"""Without numpy the live server must say so once at start-up.

The colour ladder it falls back to shifts colours from frame to frame, and the
server otherwise runs normally, so a host missing numpy looked like a picture
fault (blue/yellow flicker) rather than a missing dependency.
"""
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import live, tv_server


def env_without_degrade():
    return {k: v for k, v in os.environ.items() if k != "TV_DEGRADE"}


class DegradeWarning(unittest.TestCase):
    def test_missing_numpy_without_a_choice_warns(self):
        with mock.patch.object(live.perceptual, "AVAILABLE", False), \
                mock.patch.dict(os.environ, env_without_degrade(), clear=True):
            message = live.degrade_warning()
        self.assertIsNotNone(message)
        self.assertTrue(message.startswith("WARNING: numpy is not installed"))
        self.assertNotIn("\n", message)

    def test_numpy_present_is_silent(self):
        with mock.patch.object(live.perceptual, "AVAILABLE", True), \
                mock.patch.dict(os.environ, env_without_degrade(), clear=True):
            self.assertIsNone(live.degrade_warning())

    def test_explicit_ladder_is_silent(self):
        with mock.patch.object(live.perceptual, "AVAILABLE", False), \
                mock.patch.dict(os.environ, {"TV_DEGRADE": "ladder"}):
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
        with mock.patch.object(live.perceptual, "AVAILABLE", False), \
                mock.patch.dict(os.environ, env_without_degrade(), clear=True), \
                mock.patch.object(tv_server, "AVServer", FakeServer), \
                mock.patch.object(tv_server, "print_where_to_connect"), \
                mock.patch.object(tv_server.signal, "signal"), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="utf-8")):
            self.assertEqual(tv_server.main(["live", "--channel", "ch000"]), 0)
            out.flush()
        lines = raw.getvalue().decode("utf-8").splitlines()
        self.assertEqual(sum(line.startswith("WARNING: numpy") for line in lines), 1, lines)


if __name__ == "__main__":
    unittest.main()
