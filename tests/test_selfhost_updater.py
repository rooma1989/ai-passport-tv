"""The PC client's update check and download (server/selfhost/updater.py).

A local HTTP server plays the mirror (and a second one GitHub); manifests are
signed with a throwaway ed25519 key made here. Checked: version order, the
signature (a changed byte, another key), no downgrade by an older manifest,
falling back to the next source, resuming a cut download, a wrong SHA-256,
the per-platform file, the state file and the web page's request files.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import ed25519
from server.selfhost import updater

SECRET = secrets.token_bytes(32)
PUBLIC = ed25519.public_key(SECRET).hex()
PAYLOAD = secrets.token_bytes(300_000)


def manifest(version="0.4.0-beta", date="2026-11-10", payload=PAYLOAD, key="macos-arm64", **extra):
    entry = {"name": f"RooTV-{version}-{key}.dmg", "size": len(payload),
             "sha256": hashlib.sha256(payload).hexdigest()}
    return dict({"version": version, "date": date, "notes": "新功能", "files": {key: entry}}, **extra)


def signed(data: dict, secret=SECRET) -> tuple[bytes, bytes]:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return raw, ed25519.sign(secret, raw).hex().encode("ascii")


class Site:
    """files: path -> bytes. cut: path -> how many bytes to send before dropping the connection (once)."""

    def __init__(self):
        self.files, self.cut, self.hits = {}, {}, []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                site.hits.append((path, self.headers.get("Range")))
                data = site.files.get(path)
                if data is None:
                    self.send_error(404)
                    return
                start = 0
                wanted = self.headers.get("Range")
                if wanted and wanted.startswith("bytes="):
                    start = int(wanted[6:].split("-")[0])
                body = data[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                limit = site.cut.pop(path, None)
                self.wfile.write(body if limit is None else body[:limit])

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/rootv"

    def publish(self, data: dict, payload=PAYLOAD, secret=SECRET, key="macos-arm64"):
        raw, sig = signed(data, secret)
        self.files["/rootv/latest/rootv-update.json"] = raw
        self.files["/rootv/latest/rootv-update.json.sig"] = sig
        self.files[f"/rootv/{data['version']}/{data['files'][key]['name']}"] = payload

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class VersionTests(unittest.TestCase):
    def test_order(self):
        self.assertTrue(updater.newer("0.3.9-beta", "0.3.8-beta"))
        self.assertTrue(updater.newer("0.4.0", "0.4.0-beta"))
        self.assertTrue(updater.newer("0.4.0-beta.2", "0.4.0-beta"))
        self.assertTrue(updater.newer("0.4.0-beta.10", "0.4.0-beta.9"))
        self.assertTrue(updater.newer("0.3.8-beta", "0.3.8-beta.dev"), "a release is newer than its checkout")
        self.assertFalse(updater.newer("0.3.8-beta", "0.3.8-beta"))
        self.assertFalse(updater.newer("0.3.7", "0.3.8-beta"))
        self.assertFalse(updater.newer("garbage", "0.3.8-beta"))
        self.assertTrue(updater.newer("v1.0.0", "0.9.9"))

    def test_platform(self):
        self.assertEqual(updater.platform_key("Darwin", "arm64"), "macos-arm64")
        self.assertEqual(updater.platform_key("Darwin", "x86_64"), "macos-x86_64")
        self.assertEqual(updater.platform_key("Windows", "AMD64"), "windows-x64")
        self.assertEqual(updater.platform_key("Windows", "AMD64", portable=True), "windows-x64-portable")
        self.assertEqual(updater.platform_key("Linux", "x86_64", lite=True), "linux-x86_64-lite")
        self.assertEqual(updater.platform_key("Linux", "aarch64"), "")

    def test_sources(self):
        self.assertEqual([s.kind for s in updater.default_sources({})],
                         (["mirror"] if updater.MIRROR else []) + ["github"])
        given = updater.default_sources({updater.SOURCES_ENV: "http://a/x, http://b/y"})
        self.assertEqual([s.base for s in given], ["http://a/x", "http://b/y"])
        github = updater.Source("github", updater.GITHUB)
        self.assertEqual(github.file_url("0.4.0-beta", "f.dmg"),
                         "https://github.com/rooma1989/ai-passport-tv/releases/download/v0.4.0-beta/f.dmg")
        self.assertTrue(github.manifest_url().endswith("/releases/latest/download/rootv-update.json"))
        mirror = updater.Source("mirror", "https://cdn.example.com/rootv")
        self.assertEqual(mirror.manifest_url(now=7200), "https://cdn.example.com/rootv/latest/rootv-update.json?t=2",
                         "the mirror's manifest address changes every hour (a stale CDN copy is bypassed)")


class ManifestTests(unittest.TestCase):
    def test_signature(self):
        raw, sig = signed(manifest())
        self.assertEqual(updater.verify_manifest(raw, sig, PUBLIC)["version"], "0.4.0-beta")
        changed = raw.replace(b"0.4.0", b"0.5.0")
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest(changed, sig, PUBLIC)
        other = ed25519.public_key(secrets.token_bytes(32)).hex()
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest(raw, sig, other)
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest(raw, sig, "")
        with self.assertRaises(updater.UpdateError):
            updater.verify_manifest(raw, b"not hex", PUBLIC)

    def test_shape(self):
        for bad in (manifest(version="x"), manifest(date="yesterday"),
                    dict(manifest(), files={"macos-arm64": {"name": "../x", "size": 1, "sha256": "0" * 64}}),
                    dict(manifest(), files={"macos-arm64": {"name": "a.dmg", "size": 0, "sha256": "0" * 64}})):
            raw, sig = signed(bad)
            with self.assertRaises(updater.UpdateError):
                updater.verify_manifest(raw, sig, PUBLIC)


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.site = Site()
        self.logs = []

    def tearDown(self):
        self.site.close()
        self.tmp.cleanup()

    def make(self, version="0.3.8-beta", sources=None, **kw):
        return updater.Updater(self.root, self.root / "run", version, self.logs.append, key=PUBLIC,
                               sources=sources or [updater.Source("mirror", self.site.base)],
                               platform=kw.pop("platform", "macos-arm64"), **kw)

    def test_downloads_verifies_and_reports(self):
        self.site.publish(manifest())
        u = self.make()
        u.start()
        found = u.check()
        self.assertEqual((found["state"], found["version"], found["available"]), ("ready", "0.4.0-beta", True))
        ready = u.ready()
        self.assertEqual(Path(ready["path"]).read_bytes(), PAYLOAD)
        status = json.loads((self.root / "run" / "update.json").read_text(encoding="utf-8"))
        self.assertEqual((status["state"], status["notes"]), ("ready", "新功能"))
        # A second check finds the same version already downloaded.
        hits = len(self.site.hits)
        self.assertEqual(u.check()["state"], "ready")
        self.assertEqual(len(self.site.hits), hits + 2, "only the manifest and its signature again")

    def test_already_latest_and_no_file_for_this_system(self):
        self.site.publish(manifest(version="0.3.8-beta"))
        self.assertEqual(self.make().check()["state"], "latest")
        self.site.publish(manifest())
        found = self.make(platform="windows-x64").check()
        self.assertEqual(found["state"], "failed")
        self.assertIn("windows-x64", found["error"])

    def test_tampered_manifest_and_downgrade(self):
        raw, sig = signed(manifest())
        self.site.files["/rootv/latest/rootv-update.json"] = raw.replace(b"\xe6\x96\xb0", b"xxx")
        self.site.files["/rootv/latest/rootv-update.json.sig"] = sig
        found = self.make().check()
        self.assertEqual(found["state"], "failed")
        self.assertIn("签名", found["error"])
        self.site.publish(manifest(date="2026-11-10"))
        self.assertEqual(self.make().check()["state"], "ready")
        self.site.publish(manifest(version="0.3.9-beta", date="2026-11-01"))
        found = self.make().check()
        self.assertIn("旧", found["error"], "an older manifest replayed is refused")

    def test_falls_back_to_the_next_source(self):
        other = Site()
        try:
            other.publish(manifest())
            empty = updater.Source("mirror", self.site.base)
            u = self.make(sources=[empty, updater.Source("mirror", other.base)])
            self.assertEqual(u.check()["state"], "ready")
            self.assertTrue(any("还没有版本清单" in m for m in self.logs), self.logs)
        finally:
            other.close()

    def test_resumes_a_cut_download(self):
        data = manifest()
        self.site.publish(data)
        path = f"/rootv/0.4.0-beta/{data['files']['macos-arm64']['name']}"
        self.site.cut[path] = 100_000
        u = self.make()
        self.assertEqual(u.check()["state"], "ready")
        ranges = [r for p, r in self.site.hits if p == path]
        self.assertEqual(ranges, [None, "bytes=100000-"])

    def test_wrong_hash_is_downloaded_again_then_fails(self):
        data = manifest()
        self.site.publish(data, payload=b"x" * len(PAYLOAD))
        found = self.make().check()
        self.assertEqual(found["state"], "failed")
        self.assertIn("SHA-256", found["error"])
        self.assertFalse(list((self.root / "updates" / "0.4.0-beta").glob("*.part")))

    def test_not_enough_disk(self):
        self.site.publish(manifest())
        found = self.make(free_bytes=lambda _p: 1000).check()
        self.assertIn("磁盘", found["error"])

    def test_requests_from_the_page(self):
        self.site.publish(manifest())
        u = self.make()
        u.start()
        run = self.root / "run"
        (run / updater.INSTALL_REQUEST).write_text("")
        u.take_requests()
        self.assertFalse(u.snapshot()["install"], "nothing to install yet")
        u.check()
        (run / updater.INSTALL_REQUEST).write_text("")
        u.take_requests()
        self.assertTrue(u.snapshot()["install"])
        u.clear_install()
        self.assertFalse(u.snapshot()["install"])

    def test_start_after_an_update(self):
        self.site.publish(manifest())
        old = self.make()
        old.start()
        old.check()
        # The swap script ran; the new version starts.
        new = self.make(version="0.4.0-beta")
        new.start()
        self.assertTrue(any("从 0.3.8-beta 更新到了 0.4.0-beta" in m for m in self.logs))
        self.assertIsNone(new.ready())
        self.assertFalse((self.root / "updates" / "0.4.0-beta").exists(), "the installed download is cleared")
        # A failed update reported by the swap script.
        (self.root / "updates" / "result.json").write_text(json.dumps({"ok": False, "to": "0.5.0", "error": "x"}))
        again = self.make(version="0.4.0-beta")
        again.start()
        self.assertIn("没成功", again.snapshot()["result"])

    def test_background_loop(self):
        self.site.publish(manifest())
        u = self.make()
        stop = threading.Event()
        thread = threading.Thread(target=u.run, args=(stop,), kwargs={"first_delay": 0, "poll": 0.05})
        thread.start()
        try:
            for _ in range(200):
                if u.snapshot()["state"] == "ready":
                    break
                stop.wait(0.05)
            self.assertEqual(u.snapshot()["state"], "ready")
        finally:
            stop.set()
            thread.join(5)

    def test_enabled(self):
        self.assertFalse(updater.enabled(False, {}))
        self.assertTrue(updater.enabled(True, {}))
        self.assertTrue(updater.enabled(False, {updater.FORCE_ENV: "1"}))


if __name__ == "__main__":
    unittest.main()
