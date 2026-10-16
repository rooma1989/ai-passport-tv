#!/usr/bin/env python3
"""The public firmware's release manifest and the web flasher that reads it.

Three things are checked here, because each fails silently in the field:

* tools/firmware_manifest.py refuses an image that is not this project's
  firmware for the board it is published for (wrong chip, wrong application,
  too big for the Passport's protected layout), and the manifest it writes is
  what the flasher accepts.
* The flasher's own decisions (packaging/flash/flasher-core.js) -- which image,
  whether to erase, whether the write stays in range -- run under Node in
  tests/flasher_core_test.mjs, and its board table agrees with the tool's.
* The page keeps the properties earlier flashers lost: no third-party script,
  no reset call that does nothing on this hardware, no chip-wide erase on a
  Passport, a restart step after writing, and a pinned, vendored esptool-js.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("firmware_manifest", ROOT / "tools" / "firmware_manifest.py")
assert SPEC and SPEC.loader
FM = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = FM
SPEC.loader.exec_module(FM)

FLASH = ROOT / "packaging" / "flash"
VENDOR = FLASH / "vendor" / "esptool-js-0.6.1"


def fake_image(chip_id: int, size: int = 0x20000, project: str = FM.APP_PROJECT_NAME,
               version: str = "v0.1.0") -> bytes:
    """A merged image shaped like idf.py merge-bin's: bootloader header at 0,
    partition table at 0x8000, application with its description at 0x10000."""
    raw = bytearray(b"\xff" * size)
    raw[0] = FM.IMAGE_MAGIC
    struct.pack_into("<H", raw, 12, chip_id)
    raw[0x8000:0x8002] = b"\xaa\x50"
    raw[0x10000] = FM.IMAGE_MAGIC
    struct.pack_into("<I", raw, 0x10020, FM.APP_DESC_MAGIC)
    raw[0x10030:0x10050] = version.encode().ljust(32, b"\0")
    raw[0x10050:0x10070] = project.encode().ljust(32, b"\0")
    return bytes(raw)


class ManifestToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = self.tmp / "release"

    def make(self, board: str, image: bytes, version: str | None = "v0.1.0") -> int:
        path = self.tmp / f"{board}-build.bin"
        path.write_bytes(image)
        args = ["make", "--board", board, "--image", str(path), "--out-dir", str(self.out)]
        if version:
            args += ["--version", version]
        return FM.main(args)

    def test_make_merge_check_round_trip(self) -> None:
        self.assertEqual(self.make("xingzhi-cube-154", fake_image(9)), 0)
        self.assertEqual(self.make("ai-passport", fake_image(5)), 0)
        self.assertEqual(FM.main(["merge", str(self.out)]), 0)
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual([b["board"] for b in manifest["builds"]], ["xingzhi-cube-154", "ai-passport"])
        self.assertEqual(manifest["version"], "v0.1.0")
        passport = manifest["builds"][1]
        self.assertEqual(passport["erase"], "never")
        self.assertEqual(passport["parts"][0]["path"], "FoloToy-AI-Passport-tv.bin")
        self.assertEqual(passport["parts"][0]["offset"], 0)
        self.assertEqual(passport["parts"][0]["sha256"],
                         hashlib.sha256((self.out / "FoloToy-AI-Passport-tv.bin").read_bytes()).hexdigest())
        sums = (self.out / "SHA256SUMS").read_text()
        self.assertIn("Xingzhi-Cube-154-tv-public.bin", sums)
        self.assertIn("manifest-ai-passport.json", sums)
        self.assertEqual(FM.main(["check", str(self.out)]), 0)

    def test_a_tampered_image_fails_the_check(self) -> None:
        self.make("ai-passport", fake_image(5))
        FM.main(["merge", str(self.out)])
        target = self.out / "FoloToy-AI-Passport-tv.bin"
        data = bytearray(target.read_bytes())
        data[-1] ^= 1
        target.write_bytes(bytes(data))
        self.assertEqual(FM.main(["check", str(self.out)]), 1)

    def test_sums_must_agree_with_the_manifest(self) -> None:
        self.make("ai-passport", fake_image(5))
        FM.main(["merge", str(self.out)])
        sums = self.out / "SHA256SUMS"
        sums.write_text(re.sub(r"^[0-9a-f]{64}", "0" * 64, sums.read_text(), count=1))
        self.assertEqual(FM.main(["check", str(self.out)]), 1)

    def test_wrong_chip_is_refused(self) -> None:
        # The fault that left a Xingzhi in a boot loop: an ESP32-C3 image on an
        # ESP32-S3 ("Invalid chip id. Expected 9 read 5").
        self.assertEqual(self.make("xingzhi-cube-154", fake_image(5)), 1)
        self.assertEqual(self.make("ai-passport", fake_image(9)), 1)

    def test_other_application_is_refused(self) -> None:
        self.assertEqual(self.make("ai-passport", fake_image(5, project="demo")), 1)

    def test_version_must_match_what_the_image_reports(self) -> None:
        self.assertEqual(self.make("ai-passport", fake_image(5, version="v0.1.0"), version="v0.2.0"), 1)

    def test_a_passport_image_reaching_the_identity_region_is_refused(self) -> None:
        self.assertEqual(self.make("ai-passport", fake_image(5, size=0x356000)), 0)
        self.assertEqual(self.make("ai-passport", fake_image(5, size=0x357000)), 1)

    def test_merge_refuses_mixed_versions(self) -> None:
        self.make("xingzhi-cube-154", fake_image(9, version="v0.1.0"))
        self.make("ai-passport", fake_image(5, version="v0.2.0"), version="v0.2.0")
        self.assertEqual(FM.main(["merge", str(self.out)]), 1)

    def test_validate_rejects_what_the_flasher_rejects(self) -> None:
        good = {"schema": 1, "version": "v1", "builds": [{
            "board": "ai-passport", "chip": "ESP32-C3", "erase": "never", "write_limit": 0x356000,
            "parts": [{"path": "a.bin", "offset": 0, "size": 0x1000, "sha256": "a" * 64}]}]}
        FM.validate_manifest(good)
        for change in (
            lambda m: m["builds"][0].update(erase="first-install"),
            lambda m: m["builds"][0]["parts"][0].update(size=0x356001),
            lambda m: m["builds"][0]["parts"][0].update(path="../a.bin"),
            lambda m: m["builds"][0]["parts"][0].update(offset=0x800),
            lambda m: m["builds"][0].update(chip="ESP32-S3"),
        ):
            bad = json.loads(json.dumps(good))
            change(bad)
            with self.assertRaises(FM.ManifestError):
                FM.validate_manifest(bad)

    def test_bundle_assembles_a_servable_folder(self) -> None:
        self.make("xingzhi-cube-154", fake_image(9))
        self.make("ai-passport", fake_image(5))
        FM.main(["merge", str(self.out)])
        site = self.tmp / "site" / "flash"
        self.assertEqual(FM.main(["bundle", str(self.out), "--out", str(site)]), 0)
        for name in ("index.html", "flasher.js", "flasher-core.js",
                     "vendor/esptool-js-0.6.1/bundle.js", "vendor/esptool-js-0.6.1/LICENSE",
                     "firmware/manifest.json", "firmware/SHA256SUMS",
                     "firmware/Xingzhi-Cube-154-tv-public.bin", "firmware/FoloToy-AI-Passport-tv.bin"):
            self.assertTrue((site / name).is_file(), name)
        self.assertEqual(FM.main(["check", str(site / "firmware")]), 0)


class PublicBuildCheckTests(unittest.TestCase):
    def build_dir(self, server: str = '""', public: bool = True, image: bytes = b"\xe9 image") -> Path:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        (folder / "sdkconfig").write_text(
            f'CONFIG_AV_RAW_PROTOTYPE=y\nCONFIG_AV_DEFAULT_SERVER={server}\nCONFIG_AV_PUBLIC_SITE=""\n')
        flag = " -DAV_PUBLIC_BUILD=1" if public else ""
        (folder / "compile_commands.json").write_text(json.dumps([
            {"file": "/src/main/av_player.c", "command": f"cc -c{flag} main/av_player.c"},
            {"file": "/src/main/ui_text.c", "command": "cc -c main/ui_text.c"},
        ]))
        (folder / "FoloToy-AI-Passport-full.bin").write_bytes(image)
        return folder

    def test_a_public_build_passes(self) -> None:
        self.assertEqual(FM.check_public(self.build_dir(), []), [])

    def test_a_default_server_fails(self) -> None:
        self.assertEqual(len(FM.check_public(self.build_dir(server='"tv.example.com"'), [])), 1)

    def test_a_build_with_the_private_header_fails(self) -> None:
        self.assertEqual(len(FM.check_public(self.build_dir(public=False), [])), 1)

    def test_forbidden_strings_are_found_and_not_echoed(self) -> None:
        problems = FM.check_public(self.build_dir(image=b"xx tv.example.org yy"), ["tv.example.org"])
        self.assertEqual(len(problems), 1)
        self.assertNotIn("tv.example.org", problems[0])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FlasherCoreTests(unittest.TestCase):
    def node(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["node", str(ROOT / "tests" / "flasher_core_test.mjs"), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_flasher_core(self) -> None:
        result = self.node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_page_and_the_tool_agree_on_every_board(self) -> None:
        result = self.node("--policy")
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = json.loads(result.stdout)
        self.assertEqual({p["board"] for p in policy.values()}, set(FM.BOARDS))
        for chip, rule in policy.items():
            board = FM.BOARDS[rule["board"]]
            with self.subTest(board=rule["board"]):
                self.assertEqual(board["chip"], chip)
                self.assertEqual(board["chip_id"], rule["chipId"])
                self.assertEqual(board["erase"], rule["erase"])
                self.assertEqual(board["write_limit"], rule["writeLimit"])
                self.assertEqual(board["protect"], rule["protect"])
                self.assertEqual(int(board["flash_size"].rstrip("MB")) * 1024 * 1024, rule["flashBytes"])


class FlasherPageTests(unittest.TestCase):
    def text(self, name: str) -> str:
        return (FLASH / name).read_text(encoding="utf-8")

    @staticmethod
    def code(text: str) -> str:
        return re.sub(r"(?m)^\s*//.*$", "", text)

    def test_no_third_party_scripts_or_requests(self) -> None:
        page = self.text("index.html")
        for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', page):
            self.assertFalse(re.match(r"^(https?:)?//", src), src)
        self.assertIn("connect-src 'self'", page)
        self.assertIn("script-src 'self'", page)
        for name in ("flasher.js", "flasher-core.js"):
            for target in re.findall(r"""(?:from|import)\s*['"]([^'"]+)['"]""", self.text(name)):
                self.assertTrue(target.startswith("./"), f"{name} imports {target}")
                self.assertTrue((FLASH / target).resolve().is_file(), f"{name} imports missing {target}")
            for target in re.findall(r"fetch\(\s*['\"`]([^'\"`]+)", self.code(self.text(name))):
                self.assertFalse(re.match(r"^(https?:)?//", target), target)

    def test_vendored_esptool_is_the_pinned_one(self) -> None:
        note = (VENDOR / "SOURCE.txt").read_text(encoding="utf-8")
        self.assertIn("esptool-js 0.6.1", note)
        for name in ("bundle.js", "LICENSE"):
            digest = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
            self.assertIn(f"{name}".ljust(10) + f" sha256 {digest}", note, name)
        self.assertIn("Apache License", (VENDOR / "LICENSE").read_text(encoding="utf-8"))

    def test_no_reset_call_that_does_nothing_here(self) -> None:
        # loader.after() pulls RTS, which the chips' built-in USB does not wire
        # to reset (tests/test_flash_instructions.py has the history).
        self.assertNotIn("loader.after(", self.code(self.text("flasher.js")))

    def test_the_write_itself_never_erases_the_chip(self) -> None:
        script = self.code(self.text("flasher.js"))
        self.assertIn("eraseAll: false", script)
        self.assertNotIn("eraseAll: true", script)
        # The only chip erase is behind the decision that is never true for a
        # Passport (flasher_core_test.mjs checks the decision itself).
        self.assertEqual(script.count("eraseFlash("), 1)
        self.assertRegex(script, r"if \(d\.erase\) \{[^}]*eraseFlash\(", )

    def test_the_write_is_verified(self) -> None:
        self.assertIn("calculateMD5Hash", self.code(self.text("flasher.js")))
        self.assertIn("checkDigest", self.code(self.text("flasher.js")))

    def test_esptool_output_stays_out_of_the_reader_s_log(self) -> None:
        script = self.code(self.text("flasher.js"))
        self.assertNotRegex(script, r"writeLine:\s*\([^)]*\)\s*=>\s*log")

    def test_the_reader_is_told_to_restart_and_what_comes_next(self) -> None:
        script = self.text("flasher.js")
        page = self.text("index.html")
        self.assertIn("复位", script)             # Xingzhi: the bottom key
        self.assertIn("按住电源键", script)       # Passport: battery, power button
        for phrase in ("/tv/wifi", "192.168.4.1", "绑定", "不用加端口"):
            self.assertIn(phrase, page, phrase)

    def test_the_page_says_which_browsers_work(self) -> None:
        page = self.text("index.html")
        for phrase in ("Chrome", "Edge", "手机", "Safari", "Firefox"):
            self.assertIn(phrase, page)
        self.assertIn('lang="zh-CN"', page)

    def test_download_mode_is_explained_for_both_boards(self) -> None:
        page = self.text("index.html")
        self.assertIn("BOOT", page)
        self.assertIn("下载模式", page)
        self.assertIn("电源键", page)

    def test_xingzhi_erase_is_explained_and_needs_a_tick(self) -> None:
        page = self.text("index.html")
        script = self.text("flasher.js")
        self.assertIn("小智 AI 固件", page)
        self.assertIn("xiaozhi-esp32", page)     # the way back
        self.assertIn('id="agree"', page)
        self.assertIn("needsConsent", script)


if __name__ == "__main__":
    unittest.main()
