"""The cloud-drive component: OpenList (server/selfhost/openlist.py, server/relay/clouddrive.py).

What is checked, with no network: the installer downloads the pinned release for this platform,
checks its SHA-256 before anything runs (a mismatch leaves nothing behind and runs nothing),
takes only the openlist binary out of the archive and refuses archives with traversal names,
absolute paths or links (tar and zip), writes components.json through a temporary file and
os.replace, generates the admin password once (mode 600, letters and digits) and sets it with
OpenList's own CLI; "update" asks for the latest tag, takes that release's digest from the
GitHub API, swaps the binary and record atomically and keeps the old one when the new download
does not match. The wrapper forces 127.0.0.1 (config.json and the OPENLIST_* environment), never
lets the password through to the log, writes run/openlist.json once /ping answers, and stops its
openlist child on SIGTERM and when the supervisor's pipe closes. The supervisor starts an
"openlist" child only when the component is installed, enabled and has a password. The library
keeps one managed "网盘" source that the page cannot delete, whose password is never answered,
and whose URL follows the port. The API is admin-only, the password is revealed only to the
console opened on this computer, and the cloud relay has none of it.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import clouddrive, library, web
from server.selfhost import bootstrap, config as config_module, openlist
from server.selfhost.ytcomponents import ComponentError

import test_relay_library as lib_base
import test_relay_watch as watch_base

ROOT = Path(__file__).resolve().parents[1]
NEEDS_311 = unittest.skipIf(sys.version_info < (3, 11), "tomllib needs Python 3.11")
POSIX = unittest.skipIf(os.name != "posix", "process groups and shebang scripts")
MAC_ASSET = "openlist-darwin-arm64-lite.tar.gz"
WIN_ASSET = "openlist-windows-amd64-lite.zip"


def tar_bytes(members: dict, links: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as packed:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            packed.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            packed.addfile(info)
    return buffer.getvalue()


def zip_bytes(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as packed:
        for name, data in members.items():
            packed.writestr(name, data)
    return buffer.getvalue()


class Answer:
    def __init__(self, status=200, body=b"", headers=None, url=""):
        self.status, self.body, self.headers, self.url = status, body, headers or {}, url
        self.stream = io.BytesIO(body)

    def header(self, name):
        return self.headers.get(name, "")

    def read(self, size=-1):
        return self.stream.read(size)

    def close(self):
        pass


class FakeGitHub:
    """Release downloads, releases/latest (a redirect to /tag/<v>) and the release API."""

    def __init__(self, files: dict, latest: str = "v4.2.9", digests: dict | None = None):
        self.files, self.latest, self.digests, self.urls = files, latest, digests or {}, []

    def __call__(self, url, proxy="", follow=True):
        self.urls.append(url)
        if url.endswith("/releases/latest"):
            return Answer(302, headers={"Location": f"https://github.com/{openlist.REPO}/releases/tag/{self.latest}"})
        if "/releases/tags/" in url:
            tag = url.rsplit("/", 1)[-1]
            assets = [{"name": name, "digest": digest} for name, digest in self.digests.get(tag, {}).items()]
            return Answer(200, json.dumps({"tag_name": tag, "assets": assets}).encode())
        for key, data in self.files.items():
            if url.endswith(key):
                return Answer(200, data, {"Content-Length": str(len(data))})
        return Answer(404)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "secrets").mkdir(mode=0o700)
        (self.root / "run").mkdir()
        self.logs, self.admin_calls, self.refreshes, self.versions = [], [], [], []
        self.binary = b"#!/bin/sh\necho fake openlist\n"
        self.archive = tar_bytes({"openlist": self.binary})
        self.pinned = mock.patch.dict(openlist.ASSETS, {MAC_ASSET: (sha(self.archive), len(self.archive))})
        self.pinned.start()

    def tearDown(self):
        self.pinned.stop()
        self.tmp.cleanup()

    def component(self, fetch, system="Darwin", machine="arm64", version="v4.2.9", admin_ok=True):
        def version_run(path):
            self.versions.append(path)
            return version

        def admin_run(binary, data, port, password):
            self.admin_calls.append((binary, data, port, password))
            return admin_ok

        return openlist.OpenList(self.root, log=self.logs.append, fetch=fetch, version_run=version_run,
                                 admin_run=admin_run, refresh=lambda: self.refreshes.append(1),
                                 system=system, machine=machine)

    def test_install_checks_the_hash_records_atomically_and_sets_the_password(self):
        fetch = FakeGitHub({f"/{openlist.VERSION}/{MAC_ASSET}": self.archive})
        parts = self.component(fetch)
        self.assertFalse(parts.snapshot()["installed"])
        self.assertEqual(parts.snapshot()["size"], len(self.archive))
        replaced = []
        real_replace = os.replace

        def spy(src, dst):
            replaced.append((Path(src).name, Path(dst).name))
            return real_replace(src, dst)

        with mock.patch.object(openlist.os, "replace", spy):
            self.assertTrue(parts.start("install", wait=True))
        snap = parts.snapshot()
        self.assertEqual((snap["state"], snap["installed"], snap["version"], snap["enabled"]),
                         ("done", True, "v4.2.9", True), snap)
        self.assertIn(("components.json.tmp", "components.json"), replaced)
        record = json.loads((self.root / "openlist" / "bin" / "components.json").read_text())
        self.assertEqual(record["openlist"], {"version": "v4.2.9", "file": "openlist-v4.2.9",
                                              "sha256": sha(self.archive), "asset": MAC_ASSET})
        binary = self.root / "openlist" / "bin" / "openlist-v4.2.9"
        self.assertEqual(binary.read_bytes(), self.binary)
        self.assertTrue(os.stat(binary).st_mode & stat.S_IXUSR)
        self.assertEqual(sorted(p.name for p in binary.parent.iterdir()), ["components.json", "openlist-v4.2.9"])
        secret = self.root / "secrets" / "openlist-admin"
        password = secret.read_text().strip()
        self.assertRegex(password, r"^[A-Za-z0-9]{24}$")
        self.assertEqual(stat.S_IMODE(os.stat(secret).st_mode), 0o600)
        self.assertEqual(self.admin_calls, [(binary, self.root / "openlist" / "data", openlist.PORT, password)])
        self.assertEqual(self.refreshes, [1])
        self.assertTrue(parts.ready())
        self.assertNotIn(password, "\n".join(self.logs))
        self.assertNotIn(password, json.dumps(snap))
        # Installing again keeps the same password and does not download again.
        fetch.urls.clear()
        parts.start("install", wait=True)
        self.assertEqual(fetch.urls, [])
        self.assertEqual(secret.read_text().strip(), password)

    def test_hash_mismatch_is_refused_and_nothing_runs(self):
        bad = tar_bytes({"openlist": b"something else"})
        parts = self.component(FakeGitHub({MAC_ASSET: bad}))
        parts.start("install", wait=True)
        snap = parts.snapshot()
        self.assertEqual(snap["state"], "failed")
        self.assertIn("校验值不符", snap["error"])
        self.assertEqual(self.versions, [])
        self.assertEqual(self.admin_calls, [])
        self.assertFalse((self.root / "openlist" / "bin" / "components.json").exists())
        self.assertEqual(list((self.root / "openlist" / "bin").iterdir()), [])
        self.assertFalse((self.root / "secrets" / "openlist-admin").exists())

    def test_binary_that_does_not_run_or_lies_about_its_version_is_removed(self):
        for answer in ("", "v9.9.9"):
            parts = self.component(FakeGitHub({MAC_ASSET: self.archive}), version=answer)
            parts.start("install", wait=True)
            self.assertEqual(parts.snapshot()["state"], "failed", answer)
            self.assertIsNone(parts.installed())
            self.assertEqual([p.name for p in (self.root / "openlist" / "bin").iterdir()], [])

    def test_unsafe_archives_are_refused(self):
        target = self.root / "out"
        cases = {
            "traversal.tar.gz": tar_bytes({"openlist": b"x", "../evil": b"x"}),
            "absolute.tar.gz": tar_bytes({"openlist": b"x", "/tmp/evil": b"x"}),
            "link.tar.gz": tar_bytes({"openlist": b"x"}, links={"bin/sh": "/bin/sh"}),
            "none.tar.gz": tar_bytes({"README": b"x"}),
            "two.tar.gz": tar_bytes({"openlist": b"x", "./openlist": b"y"}),
            "traversal.zip": zip_bytes({"openlist.exe": b"x", "..\\evil.dll": b"x"}),
            "drive.zip": zip_bytes({"openlist.exe": b"x", "C:/evil": b"x"}),
            "broken.tar.gz": b"\x1f\x8bnot really",
        }
        for name, data in cases.items():
            archive = self.root / name
            archive.write_bytes(data)
            exe = "openlist.exe" if name.endswith(".zip") else "openlist"
            with self.assertRaises(ComponentError, msg=name):
                openlist.extract_binary(archive, target, exe)
            self.assertFalse(target.exists(), name)
        self.assertFalse((self.root.parent / "evil").exists())
        ok = self.root / "ok.zip"
        ok.write_bytes(zip_bytes({"openlist.exe": b"MZ binary"}))
        openlist.extract_binary(ok, target, "openlist.exe")
        self.assertEqual(target.read_bytes(), b"MZ binary")
        # A refused archive during install leaves nothing behind.
        bad = tar_bytes({"openlist": b"x", "../escape": b"x"})
        with mock.patch.dict(openlist.ASSETS, {MAC_ASSET: (sha(bad), len(bad))}):
            parts = self.component(FakeGitHub({MAC_ASSET: bad}))
            parts.start("install", wait=True)
        self.assertEqual(parts.snapshot()["state"], "failed")
        self.assertIn("不安全", parts.snapshot()["error"])
        self.assertEqual(list((self.root / "openlist" / "bin").iterdir()), [])
        self.assertFalse((self.root / "openlist" / "escape").exists())

    def test_windows_zip(self):
        archive = zip_bytes({"openlist.exe": b"MZ"})
        with mock.patch.dict(openlist.ASSETS, {WIN_ASSET: (sha(archive), len(archive))}):
            parts = self.component(FakeGitHub({WIN_ASSET: archive}), system="Windows", machine="AMD64")
            parts.start("install", wait=True)
        self.assertEqual(parts.installed()["file"], "openlist-v4.2.9.exe")
        self.assertEqual(openlist.platform_asset("Windows", "AMD64"), WIN_ASSET)
        self.assertEqual(openlist.platform_asset("Linux", "arm64"), "openlist-linux-musl-arm64-lite.tar.gz")
        self.assertIsNone(openlist.platform_asset("Linux", "armv7l"))
        unsupported = self.component(FakeGitHub({}), system="Linux", machine="armv7l")
        self.assertFalse(unsupported.snapshot()["supported"])
        unsupported.start("install", wait=True)
        self.assertEqual(unsupported.snapshot()["state"], "failed")

    def test_pinned_assets_cover_the_desktop_platforms(self):
        self.pinned.stop()
        self.addCleanup(self.pinned.start)
        for key in (("Darwin", "arm64"), ("Darwin", "x86_64"), ("Windows", "AMD64"), ("Linux", "x86_64"),
                    ("Linux", "aarch64")):
            asset = openlist.PLATFORMS[key]
            digest, size = openlist.ASSETS[asset]
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertGreater(size, 10_000_000)
            self.assertIn("lite", asset)

    def install(self):
        parts = self.component(FakeGitHub({MAC_ASSET: self.archive}))
        parts.start("install", wait=True)
        self.assertEqual(parts.snapshot()["state"], "done")
        return parts

    def test_update_swaps_atomically_and_keeps_the_old_one_on_a_bad_download(self):
        self.install()
        new = tar_bytes({"openlist": b"#!/bin/sh\necho new\n"})
        new_url = f"/v4.3.0/{MAC_ASSET}"
        fetch = FakeGitHub({new_url: new}, latest="v4.3.0", digests={"v4.3.0": {MAC_ASSET: "sha256:" + "0" * 64}})
        parts = self.component(fetch, version="v4.3.0")
        self.assertEqual(parts.check_update(), {"current": "v4.2.9", "latest": "v4.3.0", "update": True})
        self.assertTrue(parts.snapshot()["update"])
        # The release says another hash: refused, v4.2.9 untouched.
        parts.start("update", wait=True)
        self.assertEqual(parts.snapshot()["state"], "failed")
        self.assertEqual(parts.installed()["version"], "v4.2.9")
        self.assertTrue((self.root / "openlist" / "bin" / "openlist-v4.2.9").is_file())
        self.assertIn("https://api.github.com/repos/OpenListTeam/OpenList/releases/tags/v4.3.0", fetch.urls)
        # No digest published: refused too.
        fetch.digests = {"v4.3.0": {MAC_ASSET: ""}}
        parts.start("update", wait=True)
        self.assertIn("没有公布", parts.snapshot()["error"])
        # The real digest: swapped, the old binary removed, the supervisor asked to restart it.
        fetch.digests = {"v4.3.0": {MAC_ASSET: "sha256:" + sha(new)}}
        refreshes = len(self.refreshes)
        parts.start("update", wait=True, mirror="https://mirror.example/https://github.com")
        snap = parts.snapshot()
        self.assertEqual((snap["state"], snap["version"], snap["update"]), ("done", "v4.3.0", False), snap)
        self.assertEqual(sorted(p.name for p in (self.root / "openlist" / "bin").iterdir()),
                         ["components.json", "openlist-v4.3.0"])
        self.assertEqual(len(self.refreshes), refreshes + 1)
        self.assertTrue(any(u.startswith("https://mirror.example/https://github.com/") for u in fetch.urls))
        # Already the newest: nothing downloaded.
        fetch.urls.clear()
        parts.start("update", wait=True)
        self.assertFalse(any(u.endswith(MAC_ASSET) for u in fetch.urls))

    def test_disable_and_enable(self):
        parts = self.install()
        parts.disable()
        self.assertFalse(parts.ready())
        self.assertFalse(parts.snapshot()["enabled"])
        self.assertTrue(parts.enable())
        self.assertTrue(parts.ready())
        empty = openlist.OpenList(self.root / "other", refresh=lambda: None)
        self.assertFalse(empty.enable())


class WrapperPieceTests(unittest.TestCase):
    def test_config_forces_loopback_and_turns_off_other_listeners(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "config.json").write_text(json.dumps({
                "force": True, "jwt_secret": "keep", "scheme": {"address": "0.0.0.0", "http_port": 5244, "https_port": 5245,
                                                                "unix_file": "/tmp/x.sock"},
                "s3": {"enable": True, "port": 5246}, "ftp": {"enable": True}, "sftp": {"enable": True}}))
            openlist.write_config(data, 47444)
            config = json.loads((data / "config.json").read_text())
            self.assertEqual(config["scheme"]["address"], "127.0.0.1")
            self.assertEqual((config["scheme"]["http_port"], config["scheme"]["https_port"]), (47444, -1))
            self.assertEqual(config["scheme"]["unix_file"], "")
            self.assertFalse(config["force"])
            self.assertEqual(config["jwt_secret"], "keep")
            self.assertFalse(any(config[k]["enable"] for k in ("s3", "ftp", "sftp")))
            self.assertEqual(config["s3"]["port"], 5246)
            self.assertEqual(stat.S_IMODE(os.stat(data / "config.json").st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(data).st_mode), 0o700)
            (data / "config.json").write_text("not json")
            self.assertEqual(openlist.write_config(data, 1234)["scheme"]["address"], "127.0.0.1")

    def test_environment(self):
        env = openlist.child_env(47444, "Pw123", base={"TV_WATCH_PARENT": "1", "PATH": "/bin",
                                                       "OPENLIST_ADDR": "0.0.0.0"})
        self.assertEqual((env["OPENLIST_ADDR"], env["OPENLIST_HTTP_PORT"], env["OPENLIST_HTTPS_PORT"]),
                         ("127.0.0.1", "47444", "-1"))
        self.assertEqual(env["OPENLIST_ADMIN_PASSWORD"], "Pw123")
        self.assertEqual(env["OPENLIST_S3_ENABLE"], "false")
        self.assertNotIn("TV_WATCH_PARENT", env)
        self.assertNotIn("OPENLIST_ADMIN_PASSWORD", openlist.child_env(1, "", base={"OPENLIST_ADMIN_PASSWORD": "x"}))

    def test_output_never_carries_the_password(self):
        pw = "AbCdEf1234567890AbCdEf12"
        self.assertIsNone(openlist.clean_line(f"Successfully created the admin user and the initial password is: {pw}\n", pw))
        self.assertIsNone(openlist.clean_line(f"something {pw}", pw))
        self.assertIsNone(openlist.clean_line("Admin token: abc"))
        self.assertEqual(openlist.clean_line("\x1b[36mINFO\x1b[0m[2026] init logrus...   \n", pw), "INFO[2026] init logrus...")
        self.assertEqual(openlist.clean_line("start HTTP server @ 127.0.0.1:47444\n", pw), "start HTTP server @ 127.0.0.1:47444")

    def test_password_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = openlist.Paths(Path(tmp))
            self.assertEqual(openlist.read_password(paths), "")
            first, fresh = openlist.ensure_password(paths)
            self.assertTrue(fresh)
            self.assertEqual(openlist.ensure_password(paths), (first, False))
            self.assertEqual(stat.S_IMODE(os.stat(paths.secret).st_mode), 0o600)
            self.assertEqual(len({openlist.new_password() for _ in range(50)}), 50)
            self.assertTrue(all(c.isalnum() for c in openlist.new_password()))

    def test_admin_set_uses_the_openlist_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.CompletedProcess([], 0, stdout="admin user has been updated:\nusername: admin\npassword: Pw\n")
            with mock.patch.object(openlist.subprocess, "run", return_value=done) as run:
                self.assertTrue(openlist.set_admin(Path("/x/openlist"), Path(tmp), 47444, "Pw"))
            argv = run.call_args.args[0]
            self.assertEqual(argv, ["/x/openlist", "admin", "set", "Pw", "--data", tmp])
            env = run.call_args.kwargs["env"]
            self.assertEqual((env["OPENLIST_ADMIN_PASSWORD"], env["OPENLIST_ADDR"]), ("Pw", "127.0.0.1"))
            self.assertTrue(run.call_args.kwargs["capture_output"])
            self.assertEqual(json.loads((Path(tmp) / "config.json").read_text())["scheme"]["address"], "127.0.0.1")
            failed = subprocess.CompletedProcess([], 0, stdout="failed get admin user")
            with mock.patch.object(openlist.subprocess, "run", return_value=failed):
                self.assertFalse(openlist.set_admin(Path("/x/openlist"), Path(tmp), 47444, "Pw"))

    def test_version_parse(self):
        done = subprocess.CompletedProcess([], 0, stdout="Built At: x\nVersion: v4.2.9\nWebVersion: v4.2.9\n")
        with mock.patch.object(openlist.subprocess, "run", return_value=done):
            self.assertEqual(openlist.run_version(Path("/x")), "v4.2.9")
        self.assertEqual(openlist.version_key("v4.10.0") > openlist.version_key("v4.9.9"), True)
        self.assertEqual(openlist.version_key("latest"), ())


FAKE_OPENLIST = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
if sys.argv[1:2] == ["version"]:
    print("Version: v4.2.9")
    sys.exit(0)
addr, port = os.environ["OPENLIST_ADDR"], int(os.environ["OPENLIST_HTTP_PORT"])
data = sys.argv[sys.argv.index("--data") + 1]
with open(os.path.join(data, "fake.json"), "w") as f:
    json.dump({"pid": os.getpid(), "addr": addr, "port": port}, f)
print("\x1b[36mINFO\x1b[0m starting", flush=True)
print("Successfully created the admin user and the initial password is: " + os.environ.get("OPENLIST_ADMIN_PASSWORD", ""), flush=True)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = b"pong" if self.path == "/ping" else b"no"
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
HTTPServer((addr, port), H).serve_forever()
'''


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def gone(pid: int, wait: float = 5.0) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@POSIX
class WrapperProcessTests(unittest.TestCase):
    """The real wrapper (python -m server.selfhost.openlist) around a fake openlist script."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.binary = root / "bin" / "openlist-v4.2.9"
        self.binary.parent.mkdir()
        self.binary.write_text(f"#!{sys.executable}\n" + FAKE_OPENLIST)
        self.binary.chmod(0o755)
        self.data = root / "data"
        self.secret = root / "secrets" / "openlist-admin"
        self.secret.parent.mkdir()
        self.password = "Zz9" * 8
        self.secret.write_text(self.password + "\n")
        self.status = root / "run" / "openlist.json"
        self.status.parent.mkdir()
        self.port = free_port()

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, watch: bool):
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
        env.pop("TV_WATCH_PARENT", None)
        if watch:
            env["TV_WATCH_PARENT"] = "1"
        process = subprocess.Popen(
            [sys.executable, "-m", "server.selfhost.openlist", "--bin", str(self.binary), "--data", str(self.data),
             "--port", str(self.port), "--password-file", str(self.secret), "--status-file", str(self.status)],
            cwd=str(ROOT), env=env, stdin=subprocess.PIPE if watch else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", start_new_session=True)
        self.addCleanup(self.kill_group, process)
        lines = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line)
            if "跑起来了" in line:
                break
        self.assertTrue(any("跑起来了" in line for line in lines), lines)
        fake = json.loads((self.data / "fake.json").read_text())
        return process, lines, fake

    @staticmethod
    def kill_group(process):
        """A failed assertion must not leave the wrapper and its fake openlist running."""
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()

    def finish(self, process, lines):
        process.wait(timeout=20)
        rest = process.stdout.read()
        process.stdout.close()
        return "".join(lines) + rest

    def test_runs_on_loopback_hides_the_password_and_stops_on_sigterm(self):
        process, lines, fake = self.start(watch=False)
        self.assertEqual((fake["addr"], fake["port"]), ("127.0.0.1", self.port))
        status = json.loads(self.status.read_text())
        self.assertEqual((status["state"], status["port"], status["version"]), ("running", self.port, "v4.2.9"))
        self.assertTrue(openlist.ping(self.port))
        config = json.loads((self.data / "config.json").read_text())
        self.assertEqual(config["scheme"]["address"], "127.0.0.1")
        process.send_signal(signal.SIGTERM)
        output = self.finish(process, lines)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(gone(fake["pid"]), "openlist stopped with the wrapper")
        self.assertNotIn(self.password, output)
        self.assertIn("INFO starting", output)
        self.assertEqual(json.loads(self.status.read_text())["state"], "stopped")

    def test_stops_openlist_when_the_supervisor_pipe_closes(self):
        process, lines, fake = self.start(watch=True)
        process.stdin.close()                       # what the kernel does when the supervisor is killed
        output = self.finish(process, lines)
        self.assertTrue(gone(fake["pid"]))
        self.assertNotIn(self.password, output)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.layout = bootstrap.Layout(Path(self.tmp.name))
        bootstrap.prepare(self.layout)
        from server.selfhost import __main__ as main
        self.main = main
        main._openlist_ports.pop(str(self.layout.root), None)

    def tearDown(self):
        self.main._openlist_ports.pop(str(self.layout.root), None)
        self.tmp.cleanup()

    def installed(self):
        paths = openlist.Paths(self.layout.root)
        paths.bin.mkdir(parents=True)
        (paths.bin / "openlist-v4.2.9").write_bytes(b"x")
        (paths.bin / "components.json").write_text(json.dumps({"openlist": {"version": "v4.2.9",
                                                                            "file": "openlist-v4.2.9"}}))
        openlist.ensure_password(paths)
        return paths

    def test_port_prefers_47444_and_falls_back(self):
        self.assertEqual(self.main.openlist_port(self.layout, free=lambda port: port), openlist.PORT)
        self.main._openlist_ports.clear()
        self.assertEqual(self.main.openlist_port(self.layout, free=lambda port: 0 if port == openlist.PORT else 50123),
                         50123)
        self.assertEqual(self.main.openlist_port(self.layout, free=lambda port: 1), 50123, "chosen once per process")

    @NEEDS_311
    def test_child_only_when_installed_enabled_and_with_a_password(self):
        def names():
            children = self.main.build_children(self.layout, config_module.Config(), "/usr/bin/ffmpeg", "", "",
                                                log=lambda _m: None)
            return {c.name: c for c in children}

        self.main._openlist_ports[str(self.layout.root)] = 47555
        self.assertNotIn("openlist", names())
        paths = self.installed()
        child = names()["openlist"]
        self.assertEqual(child.argv[-10:], ["--bin", str(paths.bin / "openlist-v4.2.9"), "--data", str(paths.data),
                                            "--port", "47555", "--password-file", str(paths.secret),
                                            "--status-file", str(paths.status)])
        self.assertIn("server.selfhost.openlist", " ".join(child.argv))
        self.assertNotIn(openlist.read_password(paths), " ".join(child.argv))
        openlist.set_enabled(paths, False)
        self.assertNotIn("openlist", names())
        openlist.set_enabled(paths, True)
        paths.secret.unlink()
        self.assertNotIn("openlist", names())

    def test_packaged_command(self):
        from tools import subcommands
        self.assertEqual(subcommands.CHILD_COMMANDS["server.selfhost.openlist"], subcommands.OPENLIST_COMMAND)
        self.assertEqual(subcommands.CORE_COMMANDS[subcommands.OPENLIST_COMMAND], "server.selfhost.openlist")
        source = (ROOT / "tools" / "packaged_entry.py").read_text()
        self.assertIn("OPENLIST_COMMAND)", source, "the wrapper watches the supervisor itself")


# ---------------------------------------------------------------- the library and the API

class PingDav(lib_base.DavHandler):
    """The test WebDAV server plus OpenList's /ping; the password is the component's."""
    password = lib_base.PASSWORD

    def _ok(self) -> bool:
        good = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return self.headers.get("Authorization") == good

    def do_GET(self):
        if self.path == "/ping":
            self._plain(200, b"pong")
            return
        super().do_GET()


def drive_server(files, password, user="admin"):
    handler = type("Drive", (PingDav,), {"files": dict(files), "redirect": set(), "seen": [], "user": user,
                                         "password": password})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


class ManagedSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = []
        self.library = lib_base.quiet(Path(self.tmp.name), self.logs)
        self.server = drive_server({"电影/Inception.2010.mp4": lib_base.MP4}, lib_base.PASSWORD)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dav"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_managed_source_lifecycle(self):
        self.assertEqual(self.library.ensure_managed(self.url, "admin", lib_base.PASSWORD), "added")
        managed = self.library.managed()
        self.assertEqual((managed["name"], managed["managed"]), ("网盘", "openlist"))
        items = self.library.items()
        self.assertEqual([i["path"] for i in items], ["电影/Inception.2010.mp4"])
        self.assertEqual(self.library.ensure_managed(self.url, "admin", lib_base.PASSWORD), "same")
        # The port changed: the URL follows, the ids stay.
        moved = self.url.replace(str(self.server.server_address[1]), "1")
        self.assertEqual(self.library.ensure_managed(moved, "admin", lib_base.PASSWORD), "updated")
        self.assertEqual(self.library.managed()["url"], moved)
        self.assertEqual([i["id"] for i in self.library.items()], [items[0]["id"]])
        # The page cannot delete it; disabling the component can.
        answer = self.library.remove(managed["id"])
        self.assertFalse(answer["ok"])
        self.assertIn("停用网盘组件", answer["error"])
        for view in (self.library.view(), self.library.admin_view()):
            self.assertNotIn(lib_base.PASSWORD, json.dumps(view, ensure_ascii=False))
            self.assertTrue(view["sources"][0]["managed"])
        self.assertNotIn(lib_base.PASSWORD, "\n".join(self.logs))
        self.assertTrue(self.library.drop_managed())
        self.assertIsNone(self.library.managed())
        self.assertEqual(self.library.items(), [])
        self.assertFalse(self.library.drop_managed())

    def test_no_storage_yet_reads_plainly(self):
        class Empty(PingDav):
            def do_PROPFIND(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._plain(500, b'{"code":500,"message":"storage not found"}')
        server = ThreadingHTTPServer(("127.0.0.1", 0), Empty)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            self.library.ensure_managed(f"http://127.0.0.1:{server.server_address[1]}/dav", "admin", "x")
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("还没有添加网盘", self.library.managed()["error"])


class ApiTests(lib_base.ApiTests):
    """Through the relay's real HTTP port, like the library's own API tests."""

    def setUp(self):
        super().setUp()
        self.data = Path(self.accounts.path).parent
        self.relay.selfhost_data = self.data
        self.paths = openlist.Paths(self.data)
        self.paths.bin.mkdir(parents=True)
        (self.paths.bin / "openlist-v4.2.9").write_bytes(b"x")
        (self.paths.bin / "components.json").write_text(json.dumps({"openlist": {"version": "v4.2.9",
                                                                                 "file": "openlist-v4.2.9"}}))
        self.password, _ = openlist.ensure_password(self.paths)
        self.drive = drive_server({"电影/Inception.2010.mp4": lib_base.MP4}, self.password)
        self.set_running(self.drive.server_address[1])

    def tearDown(self):
        self.drive.shutdown()
        self.drive.server_close()
        super().tearDown()

    def set_running(self, port):
        self.paths.status.parent.mkdir(parents=True, exist_ok=True)
        self.paths.status.write_text(json.dumps({"state": "running", "port": port, "version": "v4.2.9"}))

    def test_drive_api_is_admin_only_and_never_answers_the_password(self):
        for who in ("alice", "guest", None):
            self.assertEqual(self.request("GET", "/tv/api/admin/clouddrive", who)[0], 404, who)
            self.assertEqual(self.request("POST", "/tv/api/admin/clouddrive/reveal", who, {})[0], 404, who)
            self.assertEqual(self.request("POST", "/tv/api/admin/clouddrive", who, {"action": "disable"})[0], 404, who)
        status, answer = self.call("admin", "GET", "/tv/api/admin/clouddrive")
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"] and answer["running"] and answer["component"]["installed"], answer)
        self.assertEqual(answer["url"], f"http://127.0.0.1:{self.drive.server_address[1]}/")
        self.assertEqual(answer["user"], "admin")
        self.assertFalse(answer["local"])
        self.assertEqual(clouddrive.sync(self.relay), "added")
        self.assertEqual([i["path"] for i in self.relay.library.items()], ["电影/Inception.2010.mp4"])
        item = self.relay.library.items()[0]["id"]
        status, response, raw = self.request("GET", f"/tv/api/library/file/{item}", "alice",
                                             headers={"Range": "bytes=10-19"})
        self.assertEqual((status, raw), (206, lib_base.MP4[10:20]))
        managed = self.relay.library.managed()
        refused = self.call("admin", "POST", "/tv/api/admin/library/delete", {"id": managed["id"]})[1]
        self.assertFalse(refused["ok"])
        answers = [self.call("admin", "GET", "/tv/api/admin/clouddrive")[1], self.call("admin", "GET", "/tv/api/admin/library")[1],
                   self.call("alice", "GET", "/tv/api/library")[1]]
        for one in answers:
            self.assertNotIn(self.password, json.dumps(one, ensure_ascii=False))
        self.assertTrue(answers[0]["source"]["managed"])
        # Reveal: not from another device ...
        denied = self.call("admin", "POST", "/tv/api/admin/clouddrive/reveal", {})[1]
        self.assertFalse(denied["ok"])
        # ... only from the console opened on this computer (the front's headers, loopback all along).
        port = self.manage.server_address[1]
        status, _, raw = self.request("POST", "/tv/api/admin/clouddrive/reveal", "admin", {},
                                      headers={"X-Front-Local": "1", "X-Real-IP": "127.0.0.1", "Host": f"127.0.0.1:{port}"})
        shown = json.loads(raw)
        self.assertEqual((status, shown["ok"], shown["password"]), (200, True, self.password))
        self.assertNotIn(self.password, "\n".join(self.logs))
        actions = [r["action"] for r in self.accounts.audit_rows(100) if r["action"].startswith("clouddrive.")]
        self.assertEqual(actions, ["clouddrive.reveal"])
        # The port moves: the source follows.
        self.set_running(self.drive.server_address[1])
        self.assertEqual(clouddrive.sync(self.relay), "same")
        # Disable: the child goes (refresh request), the source goes.
        answer = self.call("admin", "POST", "/tv/api/admin/clouddrive", {"action": "disable"})[1]
        self.assertTrue(answer["ok"])
        self.assertFalse(openlist.enabled(self.paths))
        self.assertTrue((self.data / "run" / "refresh-request").exists())
        self.assertIsNone(self.relay.library.managed())
        self.assertEqual(self.relay.library.items(), [])
        self.assertIn("clouddrive.disable", [r["action"] for r in self.accounts.audit_rows(100)])
        self.assertEqual(clouddrive.sync(self.relay), "")
        bad = self.call("admin", "POST", "/tv/api/admin/clouddrive", {"action": "rm -rf"})[1]
        self.assertFalse(bad["ok"])

    def test_not_running_leaves_the_source_alone(self):
        self.paths.status.write_text(json.dumps({"state": "stopped", "port": self.drive.server_address[1]}))
        self.assertEqual(clouddrive.sync(self.relay), "waiting")
        self.assertIsNone(self.relay.library.managed())
        self.assertFalse(self.call("admin", "GET", "/tv/api/admin/clouddrive")[1]["running"])

    def test_cloud_relay_has_no_drive(self):
        cloud = web.Relay(None, watch_base.FakeClient(), None, "/opt/ff", watch_base.KEY, accounts=self.accounts,
                          devices_dir=self.devices_dir, log=lambda _m: None)
        self.assertFalse(clouddrive.enabled_here(cloud))
        clouddrive.attach(cloud, start=False)
        self.assertEqual(clouddrive.sync(cloud), "")
        self.relay.selfhost = None
        self.assertFalse(self.call("admin", "GET", "/tv/api/admin/clouddrive")[1]["ok"])

    # The library's own API tests are inherited; they run once in test_relay_library.
    test_admin_only_management_and_family_only_reading = None
    test_cloud_relay_has_no_library = None


for _name in [n for n in dir(lib_base.ApiTests) if n.startswith("test_")]:
    if _name not in ApiTests.__dict__:
        setattr(ApiTests, _name, None)


if __name__ == "__main__":
    unittest.main()
