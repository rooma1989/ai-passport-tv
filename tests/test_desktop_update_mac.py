"""The macOS swap script (desktop/update.py MAC_SCRIPT) run for real: a fake old
RooTV.app in a scratch folder, a fake new one in a DMG made here with hdiutil,
launched with `open`. The new app "comes up" by writing run/control.json, as the
real core does; a new app that never does is rolled back. macOS only; never
touches an installed RooTV (the fake apps have their own bundle identifier).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop import update

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>RooTV</string>
<key>CFBundleIdentifier</key><string>{bundle}</string>
<key>CFBundleName</key><string>RooTV update test</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
"""


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("hdiutil") and shutil.which("open"),
                     "the macOS swap script needs macOS")
class MacSwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / "data"
        (self.data / "updates").mkdir(parents=True)
        self.apps = self.root / "Apps"
        self.apps.mkdir()
        self.bundle = f"test.rootv.update.{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        subprocess.run(["pkill", "-f", str(self.apps)], capture_output=True)
        self.tmp.cleanup()

    def make_app(self, folder: Path, version: str, comes_up: bool) -> Path:
        app = folder / "RooTV.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_text(PLIST.format(bundle=self.bundle))
        up = f'mkdir -p "{self.data}/run"; echo "{{}}" > "{self.data}/run/control.json"' if comes_up else "sleep 30"
        exe = app / "Contents" / "MacOS" / "RooTV"
        exe.write_text(f'#!/bin/sh\necho {version} > "{self.data}/launched"\n{up}\n')
        exe.chmod(0o755)
        return app

    def make_dmg(self, version: str, comes_up: bool) -> Path:
        stage = self.root / f"stage-{version}"
        stage.mkdir()
        self.make_app(stage, version, comes_up)
        dmg = self.root / f"RooTV-{version}.dmg"
        subprocess.run(["hdiutil", "create", "-quiet", "-srcfolder", str(stage), "-format", "UDZO",
                        "-volname", "RooTV", str(dmg)], check=True)
        return dmg

    def swap(self, dmg: Path, wait: str) -> int:
        script = self.data / "updates" / "install-macos.sh"
        script.write_text(update.MAC_SCRIPT)
        env = dict(os.environ, ROOTV_UPDATE_WAIT=wait)
        dead = subprocess.Popen(["true"])
        dead.wait()
        return subprocess.run(["/bin/sh", str(script), str(dmg), str(self.apps / "RooTV.app"), str(self.data),
                               "0.3.8-beta", "0.4.0-beta", str(dead.pid)], env=env, timeout=120).returncode

    def launched(self, timeout=15.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            mark = self.data / "launched"
            if mark.exists():
                return mark.read_text().strip()
            time.sleep(0.2)
        return ""

    def test_swap_and_start_the_new_version(self):
        self.make_app(self.apps, "old", True)
        dmg = self.make_dmg("new", True)
        self.assertEqual(self.swap(dmg, "40"), 0, (self.data / "updates" / "install.log").read_text())
        exe = self.apps / "RooTV.app" / "Contents" / "MacOS" / "RooTV"
        self.assertIn("echo new", exe.read_text())
        self.assertFalse((self.apps / "RooTV.old.app").exists(), "the old copy is removed once the new one is up")
        self.assertFalse((self.data / "updates" / "result.json").exists())
        self.assertEqual(self.launched(), "new")
        self.assertEqual(subprocess.run(["mount"], capture_output=True, text=True, encoding="utf-8").stdout.count("rootv-update."), 0,
                         "the DMG is detached")

    def test_rolls_back_when_the_new_version_does_not_come_up(self):
        self.make_app(self.apps, "old", True)
        dmg = self.make_dmg("broken", False)
        self.assertEqual(self.swap(dmg, "6"), 1)
        exe = self.apps / "RooTV.app" / "Contents" / "MacOS" / "RooTV"
        self.assertIn("echo old", exe.read_text(), "the old app is back")
        self.assertFalse((self.apps / "RooTV.old.app").exists())
        result = json.loads((self.data / "updates" / "result.json").read_text())
        self.assertEqual((result["ok"], result["from"], result["to"]), (False, "0.3.8-beta", "0.4.0-beta"))

    def test_a_broken_dmg_keeps_the_old_app(self):
        self.make_app(self.apps, "old", True)
        bad = self.root / "bad.dmg"
        bad.write_bytes(b"not a disk image")
        self.assertEqual(self.swap(bad, "6"), 1)
        self.assertIn("echo old", (self.apps / "RooTV.app" / "Contents" / "MacOS" / "RooTV").read_text())
        self.assertIn("挂载", json.loads((self.data / "updates" / "result.json").read_text())["error"])


if __name__ == "__main__":
    unittest.main()
