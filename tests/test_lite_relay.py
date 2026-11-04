"""The lite relay (docs/superpowers/specs/2026-11-04-minimal-product-design.md section 3):
which feature modules it attaches, which paths it refuses (410, never 404 -- the console
treats 404 as signed out), and that it tells the console its edition.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import edition
from server.relay import __main__ as relay_main


class AttachTests(unittest.TestCase):
    NAMES = ("ytsettings.autostart", "ytupdate.attach", "epg.attach", "linecheck.attach", "subscriptions.attach",
             "library.attach", "clouddrive.attach", "livetally.attach")

    def attached(self, which: str) -> set[str]:
        called = set()
        patches = []
        for name in self.NAMES:
            module, func = name.split(".")
            target = f"server.relay.{module}.{func}"
            patches.append(mock.patch(target, side_effect=lambda *a, _n=name, **k: called.add(_n)))
        relay = SimpleNamespace(main_channels=None, log=lambda _m: None)
        args = SimpleNamespace(db=Path("/tmp/none/tv.db"))
        with mock.patch.dict(os.environ, {edition.ENV: which}):
            for p in patches:
                p.start()
            try:
                relay_main.attach_features(relay, args)
            finally:
                for p in patches:
                    p.stop()
        return called

    def test_full_attaches_everything(self):
        self.assertEqual(self.attached("full"), set(self.NAMES))

    def test_lite_skips_youtube_library_drives_and_subscriptions(self):
        self.assertEqual(self.attached("lite"), {"epg.attach", "linecheck.attach", "livetally.attach"})


if __name__ == "__main__":
    unittest.main()
