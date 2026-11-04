"""Which product the core is (server/edition.py): full RooTV or the lite edition.

Design: docs/superpowers/specs/2026-11-04-minimal-product-design.md section 3. Checked:
the default is full; TV_EDITION switches it for development and tests and ignores
nonsense; the lite edition refuses exactly the listed feature paths, prefix-wise, and
nothing in the full edition.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import edition


class EditionTests(unittest.TestCase):
    @unittest.skipIf((ROOT / "server" / "_edition_build.py").exists(), "a packaged build file is present")
    def test_default_is_full(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(edition.ENV, None)
            self.assertEqual(edition.current(), "full")
            self.assertIn(edition.EDITION, edition.EDITIONS)

    def test_baked_edition_ignores_environment(self):
        with mock.patch.dict(os.environ, {edition.ENV: "full"}), \
                mock.patch.object(edition, "BAKED", True), mock.patch.object(edition, "EDITION", "lite"):
            self.assertEqual(edition.current(), "lite")

    def test_every_blocked_prefix_blocks_itself_and_children(self):
        with mock.patch.dict(os.environ, {edition.ENV: "lite"}):
            for prefix in edition.LITE_BLOCKED:
                self.assertTrue(edition.blocked(prefix), prefix)
                self.assertTrue(edition.blocked(prefix + "/x"), prefix)

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



class StampTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import build_core
        self.build_core = build_core
        self.assertEqual(build_core.EDITION_MODULE, ROOT / "server" / "_edition_build.py")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name) / "_edition_build.py"   # 不碰仓库里真的那个文件

    def stamped(self, edition, url):
        import runpy
        self.build_core.stamp_edition(edition, url, self.target)
        values = runpy.run_path(str(self.target))
        return {"EDITION": values["EDITION"], "CHANNELS_URL": values["CHANNELS_URL"]}

    def test_stamp_writes_and_clears_the_build_module(self):
        self.assertEqual(self.stamped("lite", "https://pack.example/channels.json"),
                         {"EDITION": "lite", "CHANNELS_URL": "https://pack.example/channels.json"})
        self.build_core.clear_edition(self.target)
        self.assertFalse(self.target.exists())
        self.build_core.clear_edition(self.target)  # idempotent
        self.assertEqual(self.stamped("full", ""), {"EDITION": "full", "CHANNELS_URL": ""})

    def test_stamp_overwrites_a_leftover_file(self):
        self.stamped("lite", "")
        self.assertEqual(self.stamped("full", "")["EDITION"], "full")

    def test_bad_values_are_refused_and_write_nothing(self):
        for edition, url in [("pro", ""), ("lite", "http://pack.example/c.json"), ("lite", "https://"),
                             ("lite", "https:///x"), ("lite", "https://a.example/x y"),
                             ("lite", "https://a.example/x\n"), ("lite", "https://a.example/\x00"),
                             ("lite", "ftp://a.example/")]:
            with self.subTest(edition=edition, url=url):
                with self.assertRaises(SystemExit):
                    self.build_core.stamp_edition(edition, url, self.target)
                self.assertFalse(self.target.exists())

    def test_quotes_and_backslashes_cannot_break_the_module(self):
        url = 'https://a.example/p?x=\'"\\'
        self.assertEqual(self.stamped("lite", url)["CHANNELS_URL"], url)

    def test_spec_reads_the_stamped_edition(self):
        """rootv-core.spec 只给精简版带频道包（server/data）：它用 stamped_edition() 看这次构建是哪个版本。"""
        self.assertEqual(self.build_core.stamped_edition(self.target), "full", "no stamp: full")
        self.stamped("lite", "https://pack.example/c.json")
        self.assertEqual(self.build_core.stamped_edition(self.target), "lite")
        self.stamped("full", "")
        self.assertEqual(self.build_core.stamped_edition(self.target), "full")
        self.target.write_text("EDITION = 'pro'\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.build_core.stamped_edition(self.target)
        spec = (ROOT / "packaging" / "desktop" / "rootv-core.spec").read_text(encoding="utf-8")
        self.assertIn('if stamped_edition() == "lite":', spec)
        self.assertIn('"server/data"', spec)


class DescribeTests(unittest.TestCase):
    def test_three_sources(self):
        with mock.patch.object(edition, "BAKED", True), mock.patch.object(edition, "EDITION", "lite"):
            self.assertEqual(edition.describe(), "版本：lite（打包时写死）")
        with mock.patch.object(edition, "BAKED", False), mock.patch.object(edition, "EDITION", "full"), \
                mock.patch.dict(os.environ, {"TV_EDITION": "lite"}):
            self.assertEqual(edition.describe(), "版本：lite（开发用环境变量 TV_EDITION）")
        with mock.patch.object(edition, "BAKED", False), mock.patch.object(edition, "EDITION", "full"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TV_EDITION", None)
            self.assertEqual(edition.describe(), "版本：full（默认）")


if __name__ == "__main__":
    unittest.main()
