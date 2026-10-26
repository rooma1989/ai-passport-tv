"""yt-dlp's pure-Python zipapp in local YouTube mode (replacing the slow single-file builds).

Covers: the command the node runs yt-dlp with (frozen core: `rootv-core __ytdlp`, from a
checkout: `python -I -X utf8`, the unpacked folder preferred, anything else run as it is); the
`__ytdlp` runner itself through both entry points, with a stand-in zipapp (exit codes, standard
output and error passed through, `--check`, `--compile`); unpacking (unsafe names refused); the
migration of an installed single-file yt-dlp (no downgrade, a failed migration keeps the old one
working, update migrates first, the relay starts it on start-up and on saving the settings); and
the search / test timeout messages that tell a slow yt-dlp from a slow proxy.

No network, no real yt-dlp.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.relay import youtube, ytnode, ytsearch, ytsettings  # noqa: E402
from server.relay.bilibili import SourceError  # noqa: E402
from server.relay.youtube import NodeError, YouTube  # noqa: E402
from server.selfhost import bootstrap, config, ytbrowser, ytcomponents  # noqa: E402
from tools import subcommands, ytdlp_run  # noqa: E402

FAKE_INIT = '''
import sys
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--version"]:
        print("{version}")
        sys.exit(0)
    if argv[:1] == ["--fail"]:
        print("ERROR: 出错了 on stderr", file=sys.stderr)
        sys.exit(1)
    if argv[:1] == ["--bogus"]:
        print("yt-dlp: error: no such option: --bogus", file=sys.stderr)
        sys.exit(2)
    print("args=" + " ".join(argv))
    sys.exit(0)
'''


def zipapp_bytes(version="2026.10.21", extra=None) -> bytes:
    buffer = io.BytesIO()
    buffer.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("__main__.py", "import yt_dlp\nyt_dlp.main()\n")
        archive.writestr("yt_dlp/__init__.py", FAKE_INIT.replace("{version}", version))
        for name, body in (extra or {}).items():
            archive.writestr(name, body)
    return buffer.getvalue()


class CommandTests(unittest.TestCase):
    def test_subcommand_names_agree(self):
        self.assertEqual(ytnode.YTDLP_SUBCOMMAND, ytdlp_run.COMMAND)
        self.assertNotIn(ytdlp_run.COMMAND, subcommands.CORE_COMMANDS, "not a supervisor child")

    def test_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            pyz = os.path.join(tmp, "yt-dlp-2026.10.21.pyz")
            Path(pyz).write_bytes(zipapp_bytes())
            self.assertEqual(ytnode.ytdlp_command(pyz, frozen=True, executable="/app/rootv-core"),
                             ["/app/rootv-core", "__ytdlp", pyz])
            self.assertEqual(ytnode.ytdlp_command(pyz, frozen=False, executable="/usr/bin/python3"),
                             ["/usr/bin/python3", "-I", "-X", "utf8", pyz])
            # The unpacked folder, once it is there, is what runs (compiled bytecode).
            folder = os.path.join(tmp, "yt-dlp-2026.10.21")
            os.makedirs(os.path.join(folder, "yt_dlp"))
            self.assertEqual(ytnode.ytdlp_command(pyz, frozen=True, executable="c")[-1], pyz, "no yt_dlp yet")
            Path(folder, "yt_dlp", "__init__.py").write_text("")
            self.assertEqual(ytnode.ytdlp_command(pyz, frozen=True, executable="c"), ["c", "__ytdlp", folder])
            self.assertEqual(ytnode.ytdlp_command(pyz, frozen=False, executable="p")[-1], folder)
        # The cloud node's system yt-dlp and an old single-file build run as they are.
        self.assertEqual(ytnode.ytdlp_command("/opt/yt-relay/yt-dlp", frozen=True), ["/opt/yt-relay/yt-dlp"])
        self.assertEqual(ytnode.ytdlp_command("/d/bin/yt-dlp-2026.10.21.exe"), ["/d/bin/yt-dlp-2026.10.21.exe"])
        cloud = ytnode.Tools("/opt/yt-relay/yt-dlp", "/opt/yt-relay/deno", "/usr/bin/ffmpeg", "")
        self.assertEqual(ytnode.search_args(cloud, "q", 5)[0], "/opt/yt-relay/yt-dlp")

    def test_node_and_browser_read_use_the_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            pyz = os.path.join(tmp, "yt-dlp-1.pyz")
            tools = ytnode.Tools(pyz, "/d/deno", "/usr/bin/ffmpeg", "")
            with mock.patch.object(sys, "frozen", True, create=True):
                args = ytnode.search_args(tools, "老高", 5)
                self.assertEqual(args[:3], [sys.executable, "__ytdlp", pyz])
                self.assertIn("deno:/d/deno", args)
                read = ytbrowser.extract_args(pyz, "chrome:Default", Path(tmp) / "c.txt")
                self.assertEqual(read[:4], [sys.executable, "__ytdlp", pyz, "--ignore-config"])
            self.assertEqual(ytnode.info_args(tools, "jNQXAC9IVRw")[:2], [sys.executable, "-I"])

    def test_compile_command(self):
        self.assertEqual(ytcomponents.compile_command(Path("/d/f"), frozen=True, executable="/app/rootv-core"),
                         ["/app/rootv-core", "__ytdlp", "--compile", str(Path("/d/f"))])
        self.assertEqual(ytcomponents.compile_command(Path("/d/f"), frozen=False, executable="py"),
                         ["py", "-I", "-m", "compileall", "-q", str(Path("/d/f"))])


class RunnerTests(unittest.TestCase):
    """The __ytdlp runner, through tools/packaged_entry.py and tools/core_entry.py from this checkout
    (the frozen core runs the same code), and the checkout's own `python <zipapp>`."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pyz = os.path.join(cls.tmp.name, "yt-dlp-2026.10.21.pyz")
        Path(cls.pyz).write_bytes(zipapp_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_entry(self, entry: str, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("TV_WATCH_PARENT", None)
        return subprocess.run([sys.executable, str(ROOT / "tools" / entry), "__ytdlp", *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                              stdin=subprocess.DEVNULL, env=env)

    def test_exit_codes_and_output_pass_through(self):
        for entry in ("packaged_entry.py", "core_entry.py"):
            with self.subTest(entry=entry):
                done = self.run_entry(entry, self.pyz, "--version")
                self.assertEqual((done.returncode, done.stdout.strip()), (0, "2026.10.21"), done.stderr)
                done = self.run_entry(entry, self.pyz, "--fail")
                self.assertEqual(done.returncode, 1)
                self.assertIn("出错了", done.stderr)
                self.assertEqual(self.run_entry(entry, self.pyz, "--bogus").returncode, 2)
                done = self.run_entry(entry, self.pyz, "-J", "--", "ytsearch5:-x")
                self.assertEqual(done.stdout.strip(), "args=-J -- ytsearch5:-x")
                self.assertEqual(self.run_entry(entry, os.path.join(self.tmp.name, "missing.pyz")).returncode, 2)

    def test_source_command_runs_the_zipapp(self):
        done = subprocess.run(ytnode.ytdlp_command(self.pyz, frozen=False) + ["--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "2026.10.21"), done.stderr)

    def test_check_reports_what_is_missing(self):
        done = self.run_entry("packaged_entry.py", "--check", self.pyz)
        self.assertEqual(done.returncode, 1, "the stand-in has no extractors and no JS solver")
        report = json.loads(done.stdout)
        self.assertTrue(any("yt_dlp_ejs" in item for item in report["missing"]))
        self.assertIn("sys", ytdlp_run.stdlib_imports(self.pyz))

    def test_compile_and_run_the_folder(self):
        folder = Path(self.tmp.name) / "yt-dlp-2026.10.21"
        ytcomponents.unpack_zipapp(Path(self.pyz), folder)
        self.assertEqual(self.run_entry("core_entry.py", "--compile", str(folder)).returncode, 0)
        self.assertTrue(list((folder / "yt_dlp" / "__pycache__").glob("*.pyc")))
        done = subprocess.run(ytnode.ytdlp_command(self.pyz) + ["--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(done.stdout.strip(), "2026.10.21", done.stderr)
        self.assertEqual(self.run_entry("core_entry.py", "--compile", self.tmp.name).returncode, 2)

    def test_stdlib_lists_name_real_modules(self):
        import importlib.util
        for name in ytdlp_run.STDLIB_PACKAGES + ytdlp_run.STDLIB_MODULES:
            self.assertIn(name.split(".")[0], sys.stdlib_module_names, name)
            self.assertIsNotNone(importlib.util.find_spec(name), name)


class UnpackTests(unittest.TestCase):
    def test_unsafe_or_wrong_archives_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (zipapp_bytes(extra={"../evil.py": "x"}), zipapp_bytes(extra={"/abs.py": "x"}),
                        b"not a zip at all"):
                pyz = Path(tmp) / "y.pyz"
                pyz.write_bytes(bad)
                with self.assertRaises(ytcomponents.ComponentError):
                    ytcomponents.unpack_zipapp(pyz, Path(tmp) / "y")
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["y.pyz"], "nothing left")
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("other/__init__.py", "")
            (Path(tmp) / "y.pyz").write_bytes(buffer.getvalue())
            with self.assertRaises(ytcomponents.ComponentError):
                ytcomponents.unpack_zipapp(Path(tmp) / "y.pyz", Path(tmp) / "y")


def fake_fetch(files: dict, redirects: dict | None = None):
    class Answer:
        def __init__(self, status, body=b"", location=""):
            self.status, self._body, self._location, self.url = status, io.BytesIO(body), location, ""

        def header(self, name):
            return {"Location": self._location, "Content-Length": str(len(self._body.getvalue()))}.get(name, "")

        def read(self, size=-1):
            return self._body.read(size)

        def close(self):
            pass

    calls = []

    def fetch(url, proxy="", follow=True):
        calls.append(url)
        if redirects and url in redirects:
            return Answer(302, location=redirects[url])
        return Answer(200, files[url]) if url in files else Answer(404)

    fetch.calls = calls
    return fetch


class MigrationTests(unittest.TestCase):
    ZIPAPP = zipapp_bytes()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.pin = mock.patch.object(ytcomponents, "YTDLP_SHA256", hashlib.sha256(self.ZIPAPP).hexdigest())
        self.pin.start()
        self.logs = []

    def tearDown(self):
        self.pin.stop()
        self.tmp.cleanup()

    def legacy(self, version="2026.10.21", exe=""):
        (self.bin / f"yt-dlp-{version}{exe}").write_bytes(b"old single-file yt-dlp")
        (self.bin / "deno-2.9.8").write_bytes(b"deno")
        (self.bin / "components.json").write_text(json.dumps({
            "yt-dlp": {"version": version, "file": f"yt-dlp-{version}{exe}", "sha256": "0" * 64},
            "deno": {"version": "2.9.8", "file": "deno-2.9.8", "sha256": "1" * 64}}))

    def make(self, files, redirects=None):
        fetch = fake_fetch(files, redirects)
        parts = ytcomponents.Components(self.bin, log=self.logs.append, fetch=fetch, check_run=lambda _p: "ok",
                                        system="Darwin", machine="arm64", compile_run=lambda _f: True)
        return parts, fetch

    def pinned_url(self, tag=ytcomponents.YTDLP_VERSION):
        return ytcomponents.release_url(ytcomponents.YTDLP_REPO, tag, "yt-dlp")

    def test_single_file_build_is_replaced(self):
        self.legacy(exe=".exe")
        parts, fetch = self.make({self.pinned_url(): self.ZIPAPP})
        self.assertTrue(parts.ready(), "the old one keeps working until it is replaced")
        self.assertTrue(parts.legacy())
        parts.start("install", wait=True)
        self.assertEqual(parts.snapshot()["state"], "done", parts.snapshot()["error"])
        self.assertFalse(parts.legacy())
        self.assertEqual(fetch.calls, [self.pinned_url()], "deno is not downloaded again")
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()),
                         ["components.json", "deno-2.9.8", "yt-dlp-2026.10.21", "yt-dlp-2026.10.21.pyz"])
        recorded = json.loads((self.bin / "components.json").read_text())["yt-dlp"]
        self.assertEqual(recorded, {"version": "2026.10.21", "file": "yt-dlp-2026.10.21.pyz",
                                    "sha256": hashlib.sha256(self.ZIPAPP).hexdigest()})

    def test_a_newer_single_file_build_is_not_downgraded(self):
        self.legacy(version="2099.01.02")
        newer = zipapp_bytes("2099.01.02")
        sums = f"{hashlib.sha256(newer).hexdigest()}  yt-dlp\n{'0' * 64}  yt-dlp_macos\n".encode()
        parts, _ = self.make({self.pinned_url("2099.01.02"): newer,
                              ytcomponents.release_url(ytcomponents.YTDLP_REPO, "2099.01.02", "SHA2-256SUMS"): sums})
        parts.start("install", wait=True)
        self.assertEqual(parts.snapshot()["state"], "done", parts.snapshot()["error"])
        self.assertEqual(parts.installed()["yt-dlp"]["file"], "yt-dlp-2099.01.02.pyz")

    def test_a_failed_migration_keeps_the_old_one(self):
        self.legacy()
        parts, _ = self.make({self.pinned_url(): b"tampered"})
        parts.start("install", wait=True)
        self.assertEqual(parts.snapshot()["state"], "failed")
        self.assertTrue(parts.ready() and parts.legacy())
        self.assertTrue((self.bin / "yt-dlp-2026.10.21").is_file())

    def test_update_migrates_first(self):
        self.legacy()
        tag_url = f"https://github.com/{ytcomponents.YTDLP_REPO}/releases/latest"
        parts, fetch = self.make({self.pinned_url(): self.ZIPAPP},
                                 {tag_url: f"https://github.com/{ytcomponents.YTDLP_REPO}/releases/tag/2026.10.21"})
        parts.start("update", wait=True)
        self.assertEqual(parts.snapshot()["state"], "done", parts.snapshot()["error"])
        self.assertEqual(parts.installed()["yt-dlp"]["file"], "yt-dlp-2026.10.21.pyz",
                         "same version, but the zipapp now")

    def test_unpack_failure_still_installs_the_zipapp(self):
        bad = b"#!/usr/bin/env python3\nnot a zip"
        with mock.patch.object(ytcomponents, "YTDLP_SHA256", hashlib.sha256(bad).hexdigest()):
            parts, _ = self.make({self.pinned_url(): bad})
            (self.bin / "deno-2.9.8").write_bytes(b"deno")
            (self.bin / "components.json").write_text(json.dumps(
                {"deno": {"version": "2.9.8", "file": "deno-2.9.8", "sha256": "1" * 64}}))
            parts.start("install", wait=True)
        self.assertEqual(parts.snapshot()["state"], "done", parts.snapshot()["error"])
        self.assertFalse((self.bin / "yt-dlp-2026.10.21").exists())
        self.assertTrue(any("直接用压缩包" in line for line in self.logs))

    def test_stale_versions_are_pruned_on_the_next_run(self):
        self.legacy()
        parts, _ = self.make({self.pinned_url(): self.ZIPAPP})
        parts.start("install", wait=True)
        (self.bin / "yt-dlp-2026.09.01.exe").write_bytes(b"left behind (was running on Windows)")
        (self.bin / "yt-dlp-2026.09.01").mkdir()
        parts.start("install", wait=True)
        self.assertFalse((self.bin / "yt-dlp-2026.09.01.exe").exists())
        self.assertFalse((self.bin / "yt-dlp-2026.09.01").exists())
        self.assertTrue((self.bin / "yt-dlp-2026.10.21").is_dir())


class _Relay:
    def __init__(self, root):
        self.selfhost = object()
        self.selfhost_data = root
        self.youtube_kind = "local"
        self.clients = {"youtube": None}
        self.logs = []

    def log(self, message):
        self.logs.append(message)


class RelayTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.layout = bootstrap.Layout(Path(self.tmp.name))
        bootstrap.prepare(self.layout)
        config.ensure(self.layout.config)
        self.relay = _Relay(Path(self.tmp.name))
        self.parts = mock.Mock(assets=("yt-dlp", "deno.zip"))
        self.relay._yt_components = self.parts

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_up_migrates_a_single_file_build(self):
        self.parts.ready.return_value, self.parts.legacy.return_value = True, True
        ytsettings.autostart(self.relay)
        self.parts.start.assert_called_once_with("install", "")
        self.assertIn("纯 Python", self.relay.logs[-1])
        self.parts.reset_mock()
        self.parts.legacy.return_value = False
        ytsettings.autostart(self.relay)
        self.parts.start.assert_not_called()

    def test_saving_the_settings_migrates(self):
        from server.selfhost import ytlocal
        ytlocal.set_youtube(self.layout.config, "local", "")
        self.parts.ready.return_value, self.parts.legacy.return_value = True, True
        self.assertEqual(ytsettings._save(self.relay, {"mode": "local", "proxy": ""})["changed"], False)
        self.parts.start.assert_called_once_with("install", "")
        self.parts.reset_mock()
        self.assertTrue(ytsettings._save(self.relay, {"mode": "local", "proxy": "http://127.0.0.1:7897"})["ok"])
        self.parts.start.assert_called_once_with("install", "http://127.0.0.1:7897")


class SlowMessageTests(unittest.TestCase):
    def node(self, startup, fail=None):
        def fetch(path, params, timeout=None):
            if path == "/yt/health":
                return {"state": "ok", "tools": True, "startup": startup}
            raise fail or NodeError(504, '{"error": "yt-dlp timed out"}')
        return YouTube(fetch_json=fetch)

    def ask(self, client, place="local"):
        return ytsearch.YtSearch(clock=lambda: 1000.0).search(client, ("u", 1), "老高", "video", place)

    def test_slow_ytdlp_versus_slow_proxy(self):
        answer = self.ask(self.node(37.2))
        self.assertEqual((answer["reason"], answer["slow"]), ("slow", "ytdlp"))
        self.assertIn("yt-dlp 自己慢", answer["error"])
        self.assertIn("37.2 秒", answer["error"])
        answer = self.ask(self.node(0.9))
        self.assertEqual(answer["slow"], "proxy")
        self.assertIn("启动正常（0.9 秒）", answer["error"])
        self.assertIn("代理", answer["error"])
        answer = self.ask(self.node(0.9, fail=TimeoutError("timed out")))
        self.assertEqual(answer["slow"], "proxy", "the relay's own wait ran out")

    def test_unknown_or_cloud_keeps_the_old_message(self):
        for client, place in ((self.node(None), "local"), (self.node("x"), "local"), (self.node(40), "cloud")):
            answer = self.ask(client, place)
            self.assertEqual((answer["reason"], answer["error"]), ("slow", ytsearch.SLOW))
            self.assertNotIn("slow", {k for k in answer if k != "reason"})

    def test_health_failure_is_not_an_error(self):
        def fetch(path, params, timeout=None):
            raise NodeError(504, "timed out") if path == "/yt/search" else ConnectionRefusedError(61, "x")
        self.assertEqual(self.ask(YouTube(fetch_json=fetch))["error"], ytsearch.SLOW)

    def test_settings_test_message(self):
        slow = ytsettings.describe_check({"state": "slow", "startup": 30.0}, False)["message"]
        self.assertIn("yt-dlp 自己慢", slow)
        self.assertIn("检查代理", ytsettings.describe_check({"state": "network", "startup": 0.8}, False)["message"])
        self.assertIn("检查代理", ytsettings.describe_check({"state": "network"}, False)["message"])


class NodeStartupTests(unittest.TestCase):
    def test_measured_once_per_yt_dlp_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("yt-dlp-1.pyz", "deno-1"):
                Path(tmp, name).write_text("x")
            Path(tmp, "components.json").write_text(json.dumps(
                {"yt-dlp": {"file": "yt-dlp-1.pyz"}, "deno": {"file": "deno-1"}}))
            runs, done = [], threading.Event()

            def run(args):
                runs.append(args)
                done.set()
                return 0, "2026.10.21\n", ""

            node = ytnode.Node(ytnode.Tools("u", "u", "ffmpeg", ""), "k" * 20, run=run, log=lambda _m: None,
                               tools_dir=tmp, measure=True)
            node.current_tools()
            self.assertTrue(done.wait(5))
            for _ in range(100):
                if node.startup_seconds() is not None:
                    break
                threading.Event().wait(0.01)
            self.assertIsNotNone(node.startup_seconds())
            node.current_tools()
            self.assertEqual(len(runs), 1, "measured once per yt-dlp")
            self.assertEqual(runs[0][-1], "--version")
            self.assertIn(os.path.join(tmp, "yt-dlp-1.pyz"), runs[0])
            unmeasured = ytnode.Node(ytnode.Tools("u", "u", "ffmpeg", ""), "k" * 20, run=run, tools_dir=tmp)
            unmeasured.current_tools()
            self.assertIsNone(unmeasured.startup_seconds())
            self.assertEqual(len(runs), 1, "only when asked to (the tests' fake runs stay untouched)")


if __name__ == "__main__":
    unittest.main()
