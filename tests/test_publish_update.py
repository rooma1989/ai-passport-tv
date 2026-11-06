"""tools/publish_update.py: building, signing and uploading an update manifest,
against a fake release folder, a fake gh and a fake Qiniu (no network)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import publish_update as pub  # noqa: E402
from server import ed25519  # noqa: E402
from server.selfhost import updater  # noqa: E402

VERSION = "0.3.9-beta"
NAMES = [f"RooTV-{VERSION}-macos-arm64.dmg", f"RooTV-{VERSION}-macos-x86_64.dmg",
         f"RooTV-{VERSION}-windows-x64-setup.exe", f"RooTV-{VERSION}-windows-x64.zip",
         f"rootv-{VERSION}-linux-x86_64.tar.gz"]


class Done:
    def __init__(self, code=0):
        self.returncode, self.stdout, self.stderr = code, "", ""


class Answer:
    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.files = self.root / "release"
        self.files.mkdir()
        sums = []
        for name in NAMES + ["tv-server-macos-arm64.zip"]:
            data = secrets.token_bytes(64)
            (self.files / name).write_bytes(data)
            sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
        (self.files / pub.SUMS).write_text("\n".join(sums) + "\n")
        self.secret = secrets.token_bytes(32)
        self.key = self.root / "update.key"
        self.key.write_text(self.secret.hex())
        self.qiniu = self.root / "qiniu.json"
        self.qiniu.write_text(json.dumps({"access_key": "AK", "secret_key": "SK", "bucket": "b",
                                          "domain": "https://dl.example.com"}))
        patcher = mock.patch.object(updater, "PUBLIC_KEY", ed25519.public_key(self.secret).hex())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh, self.requests = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kw):
        return argparse.Namespace(**dict({"key": self.key, "qiniu": self.qiniu, "files": str(self.files),
                                          "dry_run": False, "skip_qiniu": False}, **kw))

    def run_gh(self, command, **_):
        self.gh.append(command)
        return Done()

    def open(self, request, timeout=None):
        self.requests.append(request)
        return Answer()

    def test_platform_names(self):
        self.assertEqual([pub.platform_of(n, VERSION) for n in NAMES],
                         ["macos-arm64", "macos-x86_64", "windows-x64", "windows-x64-portable", "linux-x86_64"])
        self.assertEqual(pub.platform_of(f"RooTV-{VERSION}-lite-macos-arm64.dmg", VERSION), "macos-arm64-lite")
        self.assertIsNone(pub.platform_of("tv-server-macos-arm64.zip", VERSION))
        self.assertIsNone(pub.platform_of(f"RooTV-0.3.8-beta-macos-arm64.dmg", VERSION))
        self.assertIsNone(pub.platform_of(f"RooTV-{VERSION}-windows-x64-setup.zip", VERSION))

    def test_notes(self):
        text = "# Changelog\n\n## 未发布\n\n- **新的**：`x` 好了\n\n## v0.3.9-beta\n\n- 那一版的\n\n## v0.3.8-beta\n- 旧\n"
        self.assertEqual(pub.notes_for("0.3.9-beta", text), "- 那一版的")
        self.assertEqual(pub.notes_for("0.4.0", text), "- 新的：x 好了")

    def test_publish_signs_and_uploads_in_order(self):
        self.assertEqual(pub.publish("v" + VERSION, self.args(), run=self.run_gh, qiniu_opener=self.open,
                                     today="2026-11-10"), 0)
        upload = self.gh[-1]
        self.assertEqual(upload[:4], ["gh", "release", "upload", "v" + VERSION])
        keys = []
        for request in self.requests[:-1]:
            body = request.data
            keys.append(body.split(b'name="key"\r\n\r\n', 1)[1].split(b"\r\n", 1)[0].decode())
        self.assertEqual(keys[-2:], ["rootv/latest/rootv-update.json.sig", "rootv/latest/rootv-update.json"],
                         "the manifest goes last")
        self.assertEqual(sorted(keys[:-2]), sorted(f"rootv/{VERSION}/{n}" for n in NAMES))
        manifest_body = self.requests[-2].data
        data = manifest_body.split(b'filename="rootv-update.json"\r\nContent-Type: application/octet-stream\r\n\r\n', 1)[1]
        data = data.rsplit(b"\r\n--", 1)[0]
        sig = self.requests[-3].data.split(b"application/octet-stream\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
        manifest = updater.verify_manifest(data, sig, updater.PUBLIC_KEY)
        self.assertEqual(set(manifest["files"]), {"macos-arm64", "macos-x86_64", "windows-x64",
                                                  "windows-x64-portable", "linux-x86_64"})
        self.assertEqual(manifest["date"], "2026-11-10")
        refresh = self.requests[-1]
        self.assertTrue(refresh.full_url.endswith("/v2/tune/refresh"))
        self.assertTrue(refresh.get_header("Authorization").startswith("QBox AK:"))

    def test_upload_token(self):
        token = pub.upload_token("AK", "SK", "b", "rootv/x", now=1000)
        access, _sig, policy = token.split(":")
        self.assertEqual(access, "AK")
        self.assertEqual(json.loads(base64.urlsafe_b64decode(policy)), {"scope": "b:rootv/x", "deadline": 4600})

    def test_dry_run_and_skip_qiniu(self):
        self.assertEqual(pub.publish("v" + VERSION, self.args(dry_run=True), run=self.run_gh,
                                     qiniu_opener=self.open), 0)
        self.assertEqual((self.gh, self.requests), ([], []))
        pub.publish("v" + VERSION, self.args(skip_qiniu=True), run=self.run_gh, qiniu_opener=self.open)
        self.assertEqual(len(self.gh), 1)
        self.assertEqual(self.requests, [])

    def test_refusals(self):
        with self.assertRaises(pub.PublishError):
            pub.publish("0.3.9", self.args(), run=self.run_gh)
        other = self.root / "other.key"
        other.write_text(secrets.token_bytes(32).hex())
        with self.assertRaises(pub.PublishError) as caught:
            pub.publish("v" + VERSION, self.args(key=other), run=self.run_gh)
        self.assertIn("PUBLIC_KEY", str(caught.exception))
        (self.files / NAMES[0]).write_bytes(b"changed")
        with self.assertRaises(pub.PublishError):
            pub.publish("v" + VERSION, self.args(), run=self.run_gh)

    def test_keygen(self):
        out = self.root / "new" / "k.key"
        self.assertEqual(pub.keygen(out), 0)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(pub.keygen(out), 1, "never overwrites")

    def test_built_in_key_is_set(self):
        mock.patch.stopall()
        self.assertRegex(updater.PUBLIC_KEY, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
