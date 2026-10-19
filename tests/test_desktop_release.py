"""The PC client's release: the set of installers, the release text, the Chinese installer.

docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md, 5.2 and 5.12; the workflow is
.github/workflows/build-desktop.yml. Checked here without building anything:

- packaging/desktop/release.py: the tag must name the client's version; the set is complete and
  every file matches the SHA256SUMS it was built with; SHA256SUMS-desktop, not SHA256SUMS (the
  firmware's, for the web flasher, is in the same release);
- packaging/desktop/fetch_firmware.py waits for the tag's firmware and refuses another version;
- the release text names every file a tag publishes, Chinese first, without upstream's
  single-user instructions;
- the Inno Setup script: Simplified Chinese (vendored, pinned, unedited) first, English selectable.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging" / "desktop"))
import fetch_firmware  # noqa: E402
import release  # noqa: E402

VERSION = "9.8.7-beta"


def quiet(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = function(*args, **kwargs)
    return result, out.getvalue()


class ReleaseSetTests(unittest.TestCase):
    PLATFORMS = {
        "desktop-windows-x64": ["windows-x64-setup.exe", "windows-x64.zip"],
        "desktop-macos-arm64": ["macos-arm64.dmg"],
        "desktop-macos-x86_64": ["macos-x86_64.dmg"],
        "desktop-linux-x86_64": ["linux-x86_64.tar.gz"],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parts = Path(self.tmp.name) / "parts"
        for artifact, suffixes in self.PLATFORMS.items():
            folder = self.parts / artifact
            folder.mkdir(parents=True)
            lines = []
            for suffix in suffixes:
                name = ("xiaozhi-tv-" if suffix.startswith("linux") else "XiaozhiTV-") + f"{VERSION}-{suffix}"
                data = f"{artifact} {suffix}".encode()
                (folder / name).write_bytes(data)
                lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
            (folder / "SHA256SUMS").write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
        firmware = self.parts / "desktop-firmware"
        firmware.mkdir()
        (firmware / "SHA256SUMS").write_text(f"{'0' * 64}  FoloToy-AI-Passport-tv.bin\n", encoding="ascii")

    def test_the_whole_set_with_its_own_checksums(self):
        out = Path(self.tmp.name) / "release"
        copied = release.collect(self.parts, out, VERSION)
        self.assertEqual([p.name for p in copied], release.expected_files(VERSION) + ["SHA256SUMS-desktop"])
        self.assertFalse((out / "SHA256SUMS").exists(), "SHA256SUMS is the firmware's, for the web flasher")
        sums = release.read_sums(out / "SHA256SUMS-desktop")
        self.assertEqual(list(sums), release.expected_files(VERSION))
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)

    def test_a_file_that_changed_after_its_build(self):
        dmg = self.parts / "desktop-macos-arm64" / f"XiaozhiTV-{VERSION}-macos-arm64.dmg"
        dmg.write_bytes(b"something else")
        with self.assertRaisesRegex(release.ReleaseError, "macos-arm64.dmg: sha256"):
            release.collect(self.parts, Path(self.tmp.name) / "release", VERSION)

    def test_a_missing_platform(self):
        for path in (self.parts / "desktop-linux-x86_64").iterdir():
            path.unlink()
        with self.assertRaisesRegex(release.ReleaseError, "linux-x86_64.tar.gz"):
            release.collect(self.parts, Path(self.tmp.name) / "release", VERSION)
        code, out = quiet(release.main, ["collect", str(self.parts), "--out", str(Path(self.tmp.name) / "r"),
                                         "--version", VERSION])
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)

    def test_the_tag_names_the_version(self):
        self.assertEqual(release.check_version(None, VERSION), VERSION)
        self.assertEqual(release.check_version(f"v{VERSION}", VERSION), VERSION)
        for tag in (VERSION, "v9.8.7", "v9.8.7-beta.1"):
            with self.subTest(tag=tag), self.assertRaises(release.ReleaseError):
                release.check_version(tag, VERSION)

    def test_stamping_the_core(self):
        target = Path(self.tmp.name) / "version.py"
        target.write_text('"""doc"""\n\nVERSION = "0.2.0-beta.dev"\n', encoding="utf-8")
        release.stamp_core("0.2.0-beta", target)
        self.assertEqual(target.read_text(encoding="utf-8"), '"""doc"""\n\nVERSION = "0.2.0-beta"\n')
        self.assertRegex((ROOT / "server/selfhost/version.py").read_text(encoding="utf-8"),
                         r'(?m)^VERSION = "[^"]*"$', "the line release.py stamps")


class FirmwareWaitTests(unittest.TestCase):
    def test_waits_for_the_tags_bundle_then_checks_its_version(self):
        calls, slept = [], []
        now = [0.0]

        def download(url, archive):
            calls.append(url)
            if len(calls) < 3:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            archive.write_bytes(b"zip")

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        target = Path(tempfile.mkdtemp()) / "firmware"
        self.addCleanup(lambda: target.parent.exists() and __import__("shutil").rmtree(target.parent))

        def install(_archive, folder):
            folder.mkdir(parents=True, exist_ok=True)
            return {"version": "v1.0.0", "builds": [{"board": "ai-passport"}]}

        with mock.patch.object(fetch_firmware, "download", download), \
                mock.patch.object(fetch_firmware, "TARGET", target), \
                mock.patch.object(fetch_firmware.firmware_manifest, "install_bundle", install):
            code, out = quiet(fetch_firmware.main, ["--required", "--wait", "600", "--expect-version", "v1.0.0",
                                                    "--url", "https://example.invalid/x.zip"],
                              sleep=sleep, clock=lambda: now[0])
            self.assertEqual((code, len(calls), slept), (0, 3, [30, 30]))
            self.assertIn("bundled into the core", out)
            code, out = quiet(fetch_firmware.main, ["--required", "--expect-version", "v2.0.0"],
                              sleep=sleep, clock=lambda: now[0])
            self.assertEqual(code, 1)
            self.assertIn("::error::firmware bundle is v1.0.0, expected v2.0.0", out)
            self.assertFalse(target.exists(), "firmware of another version is not left for the core build")

    def test_gives_up_after_the_wait(self):
        now = [0.0]

        def download(url, _archive):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        def sleep(seconds):
            now[0] += seconds

        with mock.patch.object(fetch_firmware, "download", download):
            code, out = quiet(fetch_firmware.main, ["--required", "--wait", "60"], sleep=sleep, clock=lambda: now[0])
            self.assertEqual(code, 1)
            self.assertIn("::error::no firmware bundled: HTTP 404", out)
            code, out = quiet(fetch_firmware.main, [], sleep=sleep, clock=lambda: now[0])
            self.assertEqual(code, 0, "best effort on a branch")


class ReleaseTextTests(unittest.TestCase):
    BODY = ROOT / "packaging" / "release" / "release-body.txt"

    def test_names_every_file_a_tag_publishes(self):
        text = self.BODY.read_text(encoding="utf-8")
        for name in ("windows-x64-setup.exe", "windows-x64.zip", "macos-arm64.dmg", "macos-x86_64.dmg",
                     "linux-x86_64.tar.gz", "SHA256SUMS-desktop", "FoloToy-AI-Passport-tv.bin",
                     "Xingzhi-Cube-154-tv-public.bin", "manifest.json", "manifest-*.json", "SHA256SUMS",
                     "xiaozhi-tv-flasher.zip", "tv-server-*"):
            self.assertIn(name, text)

    def test_pc_client_first_chinese_first(self):
        text = self.BODY.read_text(encoding="utf-8")
        english = text.index("## Xiaozhi TV: start with the PC client")
        self.assertLess(text.index("电脑客户端"), english)
        self.assertLess(text.index("windows-x64-setup.exe"), text.index("FoloToy-AI-Passport-tv.bin"))
        self.assertLess(text.index("FoloToy-AI-Passport-tv.bin"), text.index("tv-server-*"))
        for words in (("SmartScreen", "仍要运行"), ("隐私与安全性", "仍要打开")):
            for word in words:
                self.assertIn(word, text[:english])
        for word in ("SmartScreen", "Run anyway", "Privacy & Security", "Open Anyway", "Move to Trash"):
            self.assertIn(word, text[english:])

    def test_none_of_upstreams_single_user_text(self):
        text = self.BODY.read_text(encoding="utf-8")
        for stale in ("8096", "FoloToy-XXXX", "192.168.4.1", "zpdeng1020-coder", "v1.2.1"):
            self.assertNotIn(stale, text)


class ChineseInstallerTests(unittest.TestCase):
    ISS = ROOT / "packaging" / "desktop" / "xiaozhi-tv.iss"
    ISL = ROOT / "packaging" / "desktop" / "ChineseSimplified.isl"
    NOTE = ROOT / "packaging" / "desktop" / "ChineseSimplified-SOURCE.txt"

    def test_vendored_unedited_and_pinned(self):
        note = self.NOTE.read_text(encoding="utf-8")
        pinned = re.search(r"sha256:\s+([0-9a-f]{64})", note).group(1)
        self.assertEqual(hashlib.sha256(self.ISL.read_bytes()).hexdigest(), pinned,
                         "ChineseSimplified.isl differs from the vendored file; update the note when updating it")
        tag = re.search(r"tag:\s+is-(\d+)_(\d+)_(\d+)", note)
        workflow = (ROOT / ".github" / "workflows" / "build-desktop.yml").read_text(encoding="utf-8")
        self.assertIn(f'$pinned = "{".".join(tag.groups())}"', workflow, "CI warns against the same version")
        self.assertIn("LanguageName=简体中文", self.ISL.read_text(encoding="utf-8"))

    def test_chinese_first_english_selectable(self):
        text = self.ISS.read_bytes()
        self.assertTrue(text.startswith(b"\xef\xbb\xbf"), "Inno Setup needs the BOM for the Chinese texts")
        section = text.decode("utf-8-sig").split("[Languages]", 1)[1].split("\n[", 1)[0]
        names = re.findall(r'(?m)^Name: "(\w+)"; MessagesFile: "([^"]+)"', section)
        self.assertEqual(names, [("zh", "ChineseSimplified.isl"), ("en", "compiler:Default.isl")])
        setup = text.decode("utf-8-sig")
        self.assertIn("ShowLanguageDialog=yes", setup)
        self.assertIn("LanguageDetectionMethod=none", setup)


class WorkflowTests(unittest.TestCase):
    TEXT = (ROOT / ".github" / "workflows" / "build-desktop.yml").read_text(encoding="utf-8")

    def test_tags_publish_and_everything_else_is_a_dry_run(self):
        self.assertRegex(self.TEXT, r'(?m)^    tags:\n      - "\*\*"$')
        self.assertRegex(self.TEXT, r"(?m)^      publish:\n(?:        .*\n)*?        default: false$")
        job = self.TEXT[self.TEXT.index("\n  release:\n"):]
        self.assertIn("if: github.ref_type == 'tag' && (github.event_name == 'push' || inputs.publish)", job)
        self.assertIn("needs: release-set", job)
        self.assertIn("files: release/*", job)
        self.assertNotIn("contents: write", self.TEXT[:self.TEXT.index("\n  release:\n")],
                         "only the publishing job may write")


if __name__ == "__main__":
    unittest.main()
