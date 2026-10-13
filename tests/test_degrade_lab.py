"""The picture-degrade lab (tools/degrade_lab.py): option parsing and its measurements.

    PYTHONPATH=. python tests/test_degrade_lab.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import degrade_lab as lab  # noqa: E402
from server import frames  # noqa: E402

try:
    import numpy as np
except ImportError:  # the measurements need numpy; the encoder does not
    np = None


class Variants(unittest.TestCase):
    def test_levels_and_settings(self):
        self.assertEqual(lab.parse_variant("a=rows,rows+cols;defer=0.4;climb=2;fill=0"),
                         {"a": ("steady", {"levels": ("rows", "rows+cols"), "defer": 0.4,
                                           "climb": 2, "fill": False})})

    def test_empty_levels_mean_deferral_only_and_a_dash_keeps_the_default(self):
        self.assertEqual(lab.parse_variant("d="), {"d": ("steady", {"levels": ()})})
        self.assertEqual(lab.parse_variant("w=-;max_wait=1"), {"w": ("steady", {"max_wait": 1})})


@unittest.skipIf(np is None, "numpy is needed for the measurements")
class Measurements(unittest.TestCase):
    H, W = frames.HEIGHT, frames.WIDTH

    def test_long_runs_count_only_runs_of_eight_or_more(self):
        a = np.arange(40).reshape(2, 20)
        a[0, 3:11] = 5   # a run of 8
        a[1, 0:7] = 1    # a run of 7
        self.assertAlmostEqual(lab.long_runs(a, 1), 8 / 40)

    def test_tear_is_zero_for_a_smooth_error_and_positive_for_an_old_stripe(self):
        error = np.zeros((self.H, self.W, 3))
        self.assertEqual(lab.tear(error), 0.0)
        error[frames.STRIPE_ROWS:2 * frames.STRIPE_ROWS] = 30.0
        self.assertGreater(lab.tear(error), 0.0)

    def test_an_unchanged_clip_costs_nothing_and_shows_no_error(self):
        still = bytes(range(256)) * (frames.FRAME_PIXELS // 256) + bytes(frames.FRAME_PIXELS % 256)
        summary, _ = lab.simulate([still] * 4, "steady", 1 << 20)
        self.assertEqual((summary["tint"], summary["stale"], summary["psnr"]), (0.0, 0.0, 99.0))


if __name__ == "__main__":
    unittest.main()
