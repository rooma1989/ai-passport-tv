"""Which product the core is (server/edition.py): full RooTV or the lite edition.

Design: docs/superpowers/specs/2026-11-04-minimal-product-design.md section 3. Checked:
the default is full; TV_EDITION switches it for development and tests and ignores
nonsense; the lite edition refuses exactly the listed feature paths, prefix-wise, and
nothing in the full edition.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import edition


class EditionTests(unittest.TestCase):
    def test_default_is_full(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(edition.ENV, None)
            self.assertEqual(edition.current(), edition.EDITION)
            self.assertIn(edition.EDITION, edition.EDITIONS)

    def test_environment_switches_and_ignores_nonsense(self):
        with mock.patch.dict(os.environ, {edition.ENV: "LITE"}):
            self.assertEqual(edition.current(), "lite")
            self.assertTrue(edition.is_lite())
        with mock.patch.dict(os.environ, {edition.ENV: "pro"}):
            self.assertEqual(edition.current(), edition.EDITION)

    def test_lite_blocks_feature_paths_by_prefix(self):
        with mock.patch.dict(os.environ, {edition.ENV: "lite"}):
            for path in ("/api/library", "/api/library/history", "/api/youtube/status", "/api/yt/prefetch",
                         "/api/listen/time", "/api/watch/prepare", "/api/admin/clouddrive/storages",
                         "/api/admin/channels", "/api/admin/channels/import", "/api/invites"):
                self.assertTrue(edition.blocked(path), path)
            for path in ("/api/state", "/api/devices", "/api/switch", "/api/bili/login/start",
                         "/api/admin/devices", "/api/admin/pack", "/api/seek", "/api/series-x", "/api/watchdog"):
                self.assertFalse(edition.blocked(path), path)

    def test_full_blocks_nothing(self):
        with mock.patch.dict(os.environ, {edition.ENV: "full"}):
            self.assertFalse(edition.blocked("/api/library"))
            self.assertFalse(edition.blocked("/api/youtube/status"))


if __name__ == "__main__":
    unittest.main()
