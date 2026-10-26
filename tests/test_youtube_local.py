"""YouTube local mode on the household server and PC client.

The pieces: proxy parsing and system-proxy detection (canned output per OS),
the yt-dlp / deno download (pinned hashes; a mismatch is refused and nothing is
run), cookies.txt validation (never echoed back), the [youtube] rewrite of
config.toml, the supervisor's ytnode child and its selective refresh, the
relay's plain-HTTP client for the local node (never proxied), the proxy tunnels
the node uses for media (HTTP CONNECT and a socks5 shim), and the console's
settings API (admin only).

No network: downloads go through a fake fetch, proxies are small local fakes.
"""

import hashlib
import http.client
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.relay import youtube, ytnode, ytsettings  # noqa: E402
from server.relay.tenants import Caller  # noqa: E402
from server.selfhost import bootstrap, config, desktop, supervisor, ytcomponents, ytlocal  # noqa: E402
from server.selfhost.__main__ import build_children  # noqa: E402

SCUTIL = """<dictionary> {
  ExceptionsList : <array> {
    0 : 127.0.0.1
    1 : localhost
  }
  HTTPEnable : 1
  HTTPPort : 7897
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 7897
  HTTPSProxy : 127.0.0.1
  SOCKSEnable : 1
  SOCKSPort : 7897
  SOCKSProxy : 127.0.0.1
}
"""
SCUTIL_SOCKS_ONLY = """<dictionary> {
  HTTPEnable : 0
  SOCKSEnable : 1
  SOCKSPort : 1080
  SOCKSProxy : 10.0.0.2
}
"""
SECRET_VALUE = "SECRETVALUE-do-not-leak-0123456789"
COOKIES = "\n".join([
    "# Netscape HTTP Cookie File",
    "# comment",
    f".youtube.com\tTRUE\t/\tTRUE\t{int(time.time()) + 86400 * 300}\tSAPISID\t{SECRET_VALUE}",
    f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t{int(time.time()) + 86400 * 300}\tLOGIN_INFO\t{SECRET_VALUE}",
    f".youtube.com\tTRUE\t/\tTRUE\t{int(time.time()) - 10}\tOLD\t{SECRET_VALUE}",
    f".google.com\tTRUE\t/\tTRUE\t{int(time.time()) + 86400}\tSID\t{SECRET_VALUE}",
    f".bank.example\tTRUE\t/\tTRUE\t{int(time.time()) + 86400}\tsession\t{SECRET_VALUE}",
    "",
])


class ProxyTests(unittest.TestCase):
    def test_parse_proxy(self):
        self.assertEqual(ytnode.parse_proxy("http://127.0.0.1:7897"), ytnode.Proxy("http", "127.0.0.1", 7897))
        found = ytnode.parse_proxy("socks5h://me:pa%40ss@proxy.lan:1080")
        self.assertEqual((found.scheme, found.host, found.port, found.user, found.password),
                         ("socks5h", "proxy.lan", 1080, "me", "pa@ss"))
        for bad in ("", "127.0.0.1:7897", "https://h:1", "socks4://h:1", "http://h", "http://h:0",
                    "http://h:1/path", 'http://h:1"', "http://h :1"):
            with self.assertRaises(ValueError, msg=bad):
                ytnode.parse_proxy(bad)

    def test_errors_never_show_the_password(self):
        with self.assertRaises(ytlocal.SettingsError) as caught:
            ytlocal.check_proxy("ftp://user:hunter2@h:1")
        self.assertNotIn("hunter2", str(caught.exception))
        self.assertEqual(ytnode.hide_password("Unable to connect to proxy http://u:hunter2@h:1 x"),
                         "Unable to connect to proxy http://***@h:1 x")
        self.assertNotIn("hunter2", desktop.redact('proxy = "socks5://u:hunter2@10.1.2.3:1080"'))

    def test_config_rejects_a_bad_proxy(self):
        with self.assertRaises(config.ConfigError):
            config.parse('[youtube]\nmode = "local"\nproxy = "127.0.0.1:7897"\n')
        parsed = config.parse('[youtube]\nmode = "local"\nproxy = "socks5://127.0.0.1:7897"\n')
        self.assertEqual((parsed.youtube_mode, parsed.youtube_proxy), ("local", "socks5://127.0.0.1:7897"))

    def test_macos_scutil(self):
        self.assertEqual(ytlocal.parse_scutil(SCUTIL), "http://127.0.0.1:7897")
        self.assertEqual(ytlocal.parse_scutil(SCUTIL_SOCKS_ONLY), "socks5://10.0.0.2:1080")
        self.assertEqual(ytlocal.parse_scutil("<dictionary> {\n  HTTPEnable : 0\n}\n"), "")
        found = ytlocal.detect_proxy("Darwin", run=lambda command: SCUTIL if command[0] == "scutil" else "",
                                     environ={})
        self.assertEqual(found, {"proxy": "http://127.0.0.1:7897", "source": "系统的网络代理设置"})

    def test_windows_registry(self):
        self.assertEqual(ytlocal.parse_windows(1, "127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(ytlocal.parse_windows(1, "http=127.0.0.1:7890;https=127.0.0.1:7891;socks=127.0.0.1:7892"),
                         "http://127.0.0.1:7891")
        self.assertEqual(ytlocal.parse_windows(1, "socks=127.0.0.1:7892"), "socks5://127.0.0.1:7892")
        self.assertEqual(ytlocal.parse_windows(0, "127.0.0.1:7890"), "")
        found = ytlocal.detect_proxy("Windows", environ={}, registry=lambda: (1, "127.0.0.1:7890"))
        self.assertEqual(found["proxy"], "http://127.0.0.1:7890")

    def test_linux_environment(self):
        self.assertEqual(ytlocal.detect_proxy("Linux", environ={"https_proxy": "http://10.0.0.1:3128/"})["proxy"],
                         "http://10.0.0.1:3128")
        self.assertEqual(ytlocal.detect_proxy("Linux", environ={"ALL_PROXY": "socks://10.0.0.1:1080"})["proxy"],
                         "socks5://10.0.0.1:1080")
        self.assertEqual(ytlocal.detect_proxy("Linux", environ={}), {"proxy": "", "source": ""})
        # In a container the host's system settings are not visible: environment only.
        self.assertEqual(ytlocal.detect_proxy("Darwin", run=lambda _c: SCUTIL, environ={}, in_container=True)["proxy"],
                         "")

    def test_ffmpeg_env_keeps_loopback_off_the_proxy(self):
        env = ytnode.ffmpeg_proxy_env({"no_proxy": "corp.lan", "PATH": "/bin"}, "http://127.0.0.1:7897")
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:7897")
        for host in ("corp.lan", "127.0.0.1", "localhost", "::1"):
            self.assertIn(host, env["no_proxy"].split(","))
        self.assertEqual(env["no_proxy"], env["NO_PROXY"])


def _fake_fetch(files: dict, redirects: dict | None = None):
    """url → bytes; redirects: url → Location (for releases/latest)."""
    class Answer:
        def __init__(self, status, body=b"", location=""):
            self.status, self._body, self._location = status, io.BytesIO(body), location
            self.url = ""

        def header(self, name):
            if name == "Location":
                return self._location
            if name == "Content-Length":
                return str(len(self._body.getvalue()))
            return ""

        def read(self, size=-1):
            return self._body.read(size)

        def close(self):
            pass

    calls = []

    def fetch(url, proxy="", follow=True):
        calls.append((url, proxy, follow))
        if redirects and url in redirects:
            return Answer(302, location=redirects[url])
        if url in files:
            return Answer(200, files[url])
        return Answer(404)

    fetch.calls = calls
    return fetch


def _deno_zip(name="deno") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, b"#!/bin/sh\necho deno\n")
    return buffer.getvalue()


class ComponentTests(unittest.TestCase):
    YTDLP = b"fake yt-dlp binary"
    DENO = _deno_zip()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / "bin"
        self.ran = []
        self.pins = mock.patch.multiple(
            ytcomponents,
            YTDLP_SHA256={"yt-dlp_macos": hashlib.sha256(self.YTDLP).hexdigest()},
            DENO_SHA256={"deno-aarch64-apple-darwin.zip": hashlib.sha256(self.DENO).hexdigest()})
        self.pins.start()

    def tearDown(self):
        self.pins.stop()
        self.tmp.cleanup()

    def urls(self):
        ytdlp = ytcomponents.release_url(ytcomponents.YTDLP_REPO, ytcomponents.YTDLP_VERSION, "yt-dlp_macos")
        deno = ytcomponents.release_url(ytcomponents.DENO_REPO, f"v{ytcomponents.DENO_VERSION}",
                                        "deno-aarch64-apple-darwin.zip")
        return ytdlp, deno

    def make(self, files, redirects=None, runs=True):
        fetch = _fake_fetch(files, redirects)

        def check_run(path):
            self.ran.append(Path(path).name)
            return "1.0" if runs else ""

        parts = ytcomponents.Components(self.bin, log=lambda _m: None, fetch=fetch, check_run=check_run,
                                        status_file=Path(self.tmp.name) / "status.json",
                                        system="Darwin", machine="arm64")
        return parts, fetch

    def test_install_verifies_and_records(self):
        ytdlp, deno = self.urls()
        parts, fetch = self.make({ytdlp: self.YTDLP, deno: self.DENO})
        self.assertTrue(parts.start("install", proxy="http://127.0.0.1:7897", wait=True))
        snap = parts.snapshot()
        self.assertEqual(snap["state"], "done", snap["error"])
        self.assertTrue(snap["ready"])
        self.assertEqual((snap["ytdlp"], snap["deno"]), (ytcomponents.YTDLP_VERSION, ytcomponents.DENO_VERSION))
        found = ytnode.read_components(str(self.bin))
        self.assertEqual(Path(found[0]).read_bytes(), self.YTDLP)
        self.assertEqual(Path(found[1]).read_bytes(), b"#!/bin/sh\necho deno\n")
        self.assertTrue(os.stat(found[0]).st_mode & stat.S_IXUSR)
        self.assertEqual({proxy for _, proxy, _ in fetch.calls}, {"http://127.0.0.1:7897"})
        self.assertEqual(list(self.bin.glob("*.part")), [], "no partial files left")
        self.assertTrue(json.loads((Path(self.tmp.name) / "status.json").read_text())["ready"])

    def test_hash_mismatch_is_refused_and_nothing_runs(self):
        ytdlp, deno = self.urls()
        parts, _ = self.make({ytdlp: b"tampered", deno: self.DENO})
        parts.start("install", wait=True)
        snap = parts.snapshot()
        self.assertEqual(snap["state"], "failed")
        self.assertIn("校验值不符", snap["error"])
        self.assertFalse(snap["ready"])
        self.assertEqual(self.ran, [], "a file that failed the check is never run")
        self.assertEqual([p.name for p in self.bin.iterdir()], [], "and not kept")

    def test_mirror_and_unsupported_platform(self):
        ytdlp, deno = self.urls()
        mirror = "https://mirror.example/https://github.com"
        files = {u.replace("https://github.com", mirror): b for u, b in ((ytdlp, self.YTDLP), (deno, self.DENO))}
        parts, fetch = self.make(files)
        parts.start("install", mirror=mirror, wait=True)
        self.assertTrue(parts.ready())
        self.assertTrue(all(url.startswith(mirror) for url, _, _ in fetch.calls))
        self.assertFalse(ytcomponents.valid_mirror("http://mirror.example"))
        self.assertTrue(ytcomponents.valid_mirror(mirror))
        self.assertIsNone(ytcomponents.platform_assets("Windows", "x86"))
        self.assertEqual(ytcomponents.platform_assets("Linux", "aarch64")[0], "yt-dlp_linux_aarch64")
        odd = ytcomponents.Components(self.bin, log=lambda _m: None, system="Linux", machine="armv7l")
        odd.start("install", wait=True)
        self.assertEqual(odd.snapshot()["state"], "failed")
        self.assertFalse(odd.snapshot()["supported"])

    def test_update_is_checked_against_the_release_sums(self):
        ytdlp, deno = self.urls()
        new = b"newer yt-dlp"
        latest = f"https://github.com/{ytcomponents.YTDLP_REPO}/releases/latest"
        tag = "2099.01.02"
        sums_url = ytcomponents.release_url(ytcomponents.YTDLP_REPO, tag, "SHA2-256SUMS")
        new_url = ytcomponents.release_url(ytcomponents.YTDLP_REPO, tag, "yt-dlp_macos")
        sums = f"{hashlib.sha256(new).hexdigest()}  yt-dlp_macos\n{'0' * 64}  yt-dlp.exe\n".encode()
        files = {ytdlp: self.YTDLP, deno: self.DENO, sums_url: sums, new_url: new}
        redirects = {latest: f"https://github.com/{ytcomponents.YTDLP_REPO}/releases/tag/{tag}"}
        parts, _ = self.make(files, redirects)
        parts.start("install", wait=True)
        self.assertEqual(parts.check_update(), {"current": ytcomponents.YTDLP_VERSION, "latest": tag, "update": True})
        old_file = parts.installed()["yt-dlp"]["file"]
        parts.start("update", wait=True)
        self.assertEqual(parts.snapshot()["state"], "done", parts.snapshot()["error"])
        self.assertEqual(parts.installed()["yt-dlp"]["version"], tag)
        self.assertFalse((self.bin / old_file).exists(), "the old version is removed after the switch")
        self.assertEqual(Path(ytnode.read_components(str(self.bin))[0]).read_bytes(), new)
        # A release whose file does not match its own SHA2-256SUMS is refused; the current one stays.
        files[new_url.replace(tag, "2099.02.03")] = b"evil"
        files[sums_url.replace(tag, "2099.02.03")] = sums
        redirects[latest] = redirects[latest].replace(tag, "2099.02.03")
        parts.start("update", wait=True)
        self.assertEqual(parts.snapshot()["state"], "failed")
        self.assertEqual(parts.installed()["yt-dlp"]["version"], tag)

    def test_sums_parsing(self):
        self.assertEqual(ytcomponents.parse_sums(f"{'a' * 64}  yt-dlp\n{'B' * 64} *yt-dlp.exe\nnoise\n"),
                         {"yt-dlp": "a" * 64, "yt-dlp.exe": "b" * 64})
        self.assertGreater(ytcomponents.version_key("2026.10.21.1"), ytcomponents.version_key("2026.10.21"))



class PinTests(unittest.TestCase):
    def test_pinned_hashes_cover_every_platform(self):
        for ytdlp, deno in ytcomponents.PLATFORMS.values():
            self.assertRegex(ytcomponents.YTDLP_SHA256[ytdlp], r"^[0-9a-f]{64}$")
            self.assertRegex(ytcomponents.DENO_SHA256[deno], r"^[0-9a-f]{64}$")


class CookiesTests(unittest.TestCase):
    def test_valid_file_keeps_only_youtube_and_google(self):
        content, count = ytlocal.parse_cookies(COOKIES)
        self.assertEqual(count, 2, "two unexpired youtube.com cookies")
        self.assertIn("SAPISID", content)
        self.assertIn(".google.com", content)
        self.assertNotIn("bank.example", content)
        self.assertTrue(content.startswith("# Netscape HTTP Cookie File"))

    def test_errors_do_not_echo_the_contents(self):
        for text in ("", "not a cookie file " + SECRET_VALUE,
                     f".bank.example\tTRUE\t/\tTRUE\t0\tsession\t{SECRET_VALUE}\n",
                     "x" * (ytlocal.COOKIES_MAX + 1)):
            with self.assertRaises(ytlocal.SettingsError) as caught:
                ytlocal.parse_cookies(text)
            self.assertNotIn(SECRET_VALUE, str(caught.exception))

    def test_saved_private_and_summarised_without_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ytlocal.cookies_path(Path(tmp), "youtube/cookies.txt")
            self.assertEqual(ytlocal.save_cookies(path, COOKIES), 2)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            summary = ytlocal.cookies_summary(path)
            self.assertEqual((summary["present"], summary["count"]), (True, 2))
            self.assertNotIn(SECRET_VALUE, json.dumps(summary))
            self.assertTrue(ytlocal.delete_cookies(path))
            self.assertFalse(ytlocal.cookies_summary(path)["present"])


class ConfigRewriteTests(unittest.TestCase):
    def test_default_file_is_rewritten_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config.ensure(path)
            ytlocal.set_youtube(path, "local", "socks5://127.0.0.1:7897")
            text = path.read_text(encoding="utf-8")
            parsed = config.parse(text)
            self.assertEqual((parsed.youtube_mode, parsed.youtube_proxy), ("local", "socks5://127.0.0.1:7897"))
            self.assertIn("# 小鼠电视自建服务器的设置", text, "comments kept")
            self.assertIn('cookies = "youtube/cookies.txt"', text)
            self.assertEqual(text.count("[youtube]"), 1)
            self.assertEqual(len(text.splitlines()), len(config.DEFAULT_TEXT.splitlines()))
            line = next(l for l in text.splitlines() if l.startswith("proxy"))
            default = next(l for l in config.DEFAULT_TEXT.splitlines() if l.startswith("proxy"))
            quoted = len('"socks5://127.0.0.1:7897"')
            self.assertEqual(line.index("#"), max(default.index("#"), len("proxy = ") + quoted + 1),
                             "comments stay aligned (one space minimum)")
            self.assertEqual(next(l for l in text.splitlines() if l.startswith("mode")).index("#"),
                             next(l for l in config.DEFAULT_TEXT.splitlines() if l.startswith("mode")).index("#"))
            ytlocal.set_youtube(path, "off", "")
            self.assertEqual(config.parse(path.read_text(encoding="utf-8")).youtube_mode, "off")

    def test_missing_section_and_keys_are_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[server]\nname = "家"\n\n[youtube]\ncookies = "c.txt"\n\n[voice]\nasr_socket = ""\n',
                            encoding="utf-8")
            ytlocal.set_youtube(path, "local", "http://10.0.0.1:3128")
            parsed = config.parse(path.read_text(encoding="utf-8"))
            self.assertEqual((parsed.youtube_mode, parsed.youtube_proxy, parsed.youtube_cookies),
                             ("local", "http://10.0.0.1:3128", "c.txt"))
            path.write_text('[server]\nname = "家"\n', encoding="utf-8")
            ytlocal.set_youtube(path, "local", "")
            self.assertEqual(config.parse(path.read_text(encoding="utf-8")).youtube_mode, "local")
            with self.assertRaises(ytlocal.SettingsError):
                ytlocal.set_youtube(path, "local", "nonsense")
            with self.assertRaises(ytlocal.SettingsError):
                ytlocal.set_youtube(path, "proxy", "")


class ChildrenTests(unittest.TestCase):
    def build(self, root: Path, **values):
        layout = bootstrap.Layout(root)
        bootstrap.prepare(layout)
        children = build_children(layout, config.Config(**values), "/usr/bin/ffmpeg", "192.168.1.20",
                                  "http://192.168.1.20:8080", log=lambda _m: None, desktop=True)
        return layout, {child.name: child for child in children}

    def test_local_mode_starts_a_loopback_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout, children = self.build(Path(tmp), youtube_mode="local",
                                          youtube_proxy="socks5://u:hunter2@127.0.0.1:7897")
            self.assertEqual(list(children), ["relay", "xingzhi", "passport", "front", "ytnode"])
            node, relay = children["ytnode"], children["relay"].argv
            self.assertIn("__ytnode", node.argv)
            for flag in ("--plain", "--remux", "--no-check"):
                self.assertIn(flag, node.argv)
            self.assertEqual(node.argv[node.argv.index("--bind") + 1], "127.0.0.1")
            self.assertEqual(node.argv[node.argv.index("--tools-dir") + 1], str(layout.youtube_bin))
            self.assertEqual(node.argv[node.argv.index("--cookies") + 1], str(layout.youtube / "cookies.txt"))
            self.assertNotIn("hunter2", " ".join(node.argv), "the proxy (and its password) is not on the command line")
            self.assertEqual(node.env["TV_YT_PROXY"], "socks5://u:hunter2@127.0.0.1:7897")
            self.assertIn("127.0.0.1", node.env.get("no_proxy", "127.0.0.1"))
            port = node.argv[node.argv.index("--port") + 1]
            self.assertEqual(relay[relay.index("--ytlocal") + 1], f"http://127.0.0.1:{port}")
            self.assertEqual(relay[relay.index("--ytlocal-key-file") + 1], str(layout.ytnode_key))
            self.assertGreaterEqual(len(layout.ytnode_key.read_text().strip()), 16)
            self.assertEqual(relay[relay.index("--selfhost-data") + 1], str(layout.root))
            self.assertNotIn("--cstone", relay)
            # The same data folder keeps the same port, so a proxy change restarts only the node.
            _, again = self.build(Path(tmp), youtube_mode="local", youtube_proxy="http://127.0.0.1:1")
            self.assertEqual(again["relay"].argv, children["relay"].argv)
            self.assertNotEqual(again["ytnode"].env, node.env)

    def test_off_and_node_modes_have_no_local_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, children = self.build(Path(tmp))
            self.assertNotIn("ytnode", children)
            self.assertNotIn("--ytlocal", children["relay"].argv)
            layout, children = self.build(Path(tmp), youtube_mode="node")
            self.assertNotIn("ytnode", children)
            layout.youtube_node.write_text("{}")
            _, children = self.build(Path(tmp), youtube_mode="node")
            self.assertIn("--cstone", children["relay"].argv)


class RefreshTests(unittest.TestCase):
    """run/refresh-request: only the children whose arguments changed are restarted."""

    def child(self, name, extra=""):
        argv = [sys.executable, "-c", "import time; time.sleep(60)"]
        return supervisor.Child(name, argv, {"PATH": os.environ.get("PATH", ""), "X": extra}, lambda _m: None)

    def test_selective_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp))
            bootstrap.prepare(layout)
            wanted = {"names": [("relay", ""), ("front", "")]}
            reloads = []
            sup = supervisor.Supervisor(layout, [], 14, log=lambda _m: None,
                                        pending=lambda: [self.child(n, e) for n, e in wanted["names"]],
                                        reload=lambda: reloads.append(1))
            runner = threading.Thread(target=sup.run, daemon=True)
            runner.start()
            try:
                deadline = time.monotonic() + 10
                while not (sup.started and all(c.running for c in sup.children)) and time.monotonic() < deadline:
                    time.sleep(0.05)
                first = {c.name: c.pid for c in sup.children}
                wanted["names"] = [("relay", ""), ("front", ""), ("ytnode", "proxy-a")]
                (layout.run / ytlocal.REFRESH_REQUEST).write_text("1")
                while len(sup.children) < 3 and time.monotonic() < deadline:
                    time.sleep(0.05)
                second = {c.name: c.pid for c in sup.children}
                self.assertEqual(reloads, [1])
                self.assertEqual((second["relay"], second["front"]), (first["relay"], first["front"]),
                                 "unchanged children keep running")
                self.assertIsNotNone(second["ytnode"])
                wanted["names"] = [("relay", ""), ("front", "")]
                ytlocal.request_refresh(layout.run)
                while len(sup.children) > 2 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual([c.name for c in sup.children], ["relay", "front"])
                self.assertEqual(sup.children[0].pid, first["relay"])
                self.assertFalse((layout.run / ytlocal.REFRESH_REQUEST).exists())
            finally:
                sup.stopping.set()
                runner.join(15)


class _FakeNodeTools:
    def __init__(self, root: Path):
        self.root = root
        (root / "yt-dlp-1").write_text("x")
        (root / "deno-1").write_text("x")
        (root / "components.json").write_text(json.dumps({"yt-dlp": {"file": "yt-dlp-1"}, "deno": {"file": "deno-1"}}))


INFO = {"id": "jNQXAC9IVRw", "title": "Me at the zoo", "duration": 19, "_version": {"version": "2026.10.21"},
        "channel_url": "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A",
        "url": "https://v.example/both", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2"}


class LocalNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = []
        self.answer = (0, json.dumps(INFO), "")

        def run(args):
            self.runs.append(args)
            return self.answer

        tools = ytnode.Tools("unused", "unused", "/usr/bin/ffmpeg", str(self.root / "cookies.txt"),
                             "socks5://127.0.0.1:7897")
        self.node = ytnode.Node(tools, "k" * 20, run=run, log=lambda _m: None, tools_dir=str(self.root),
                                remux=True)
        self.server = ytnode.make_server(self.node, "127.0.0.1", 0, context=None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = youtube.YouTube(config={"url": f"http://127.0.0.1:{self.server.server_address[1]}",
                                              "key": "k" * 20})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_tools_are_found_at_each_call(self):
        self.assertEqual(self.client.check(timeout=5)["state"], "missing")
        with self.assertRaises(youtube.SourceError):
            self.client.video("jNQXAC9IVRw")
        self.assertEqual(self.runs, [], "nothing runs before the components are installed")
        _FakeNodeTools(self.root)
        video = self.client.video("jNQXAC9IVRw")
        self.assertEqual(video.title, "Me at the zoo")
        args = self.runs[-1]
        self.assertEqual(args[0], str(self.root / "yt-dlp-1"))
        self.assertIn(f"deno:{self.root / 'deno-1'}", args)
        self.assertEqual(args[args.index("--proxy") + 1], "socks5://127.0.0.1:7897")
        self.assertNotIn("--cookies", args, "no cookies file yet")
        (self.root / "cookies.txt").write_text("# Netscape HTTP Cookie File\n")
        self.client.check(timeout=5)
        self.assertIn("--cookies", self.runs[-1], "an imported file is used without a restart")

    def test_check_explains_the_failure(self):
        _FakeNodeTools(self.root)
        self.assertEqual(self.client.check(timeout=5)["state"], "ok")
        self.answer = (1, "", "ERROR: [youtube] x: Sign in to confirm you're not a bot.")
        self.assertEqual(self.client.check(timeout=5)["state"], "login")
        self.answer = (1, "", "ERROR: Unable to download API page: <urlopen error [Errno 61] Connection refused> "
                              "(proxy socks5://u:hunter2@127.0.0.1:1)")
        result = self.client.check(timeout=5)
        self.assertEqual(result["state"], "network")
        self.assertNotIn("hunter2", result["error"])
        self.assertIn("检查代理", ytsettings.describe_check(result, False)["message"])
        self.assertIn("导入 cookies", ytsettings.describe_check({"state": "login"}, False)["message"])
        self.assertIn("成功", ytsettings.describe_check({"state": "ok", "seconds": 3.2}, False)["message"])

    def test_the_loopback_link_never_uses_a_proxy(self):
        _FakeNodeTools(self.root)
        dead = "http://127.0.0.1:9"     # nothing listens: a proxied request would fail
        with mock.patch.dict(os.environ, {"http_proxy": dead, "HTTP_PROXY": dead, "all_proxy": dead,
                                          "no_proxy": "", "NO_PROXY": ""}):
            self.assertEqual(self.client.video("jNQXAC9IVRw").title, "Me at the zoo")
        # ProxyHandler({}) replaces the default one that reads the environment (and macOS's system settings).
        self.assertNotIn("ProxyHandler", [type(h).__name__ for h in self.client._opener.handlers])

    def test_only_loopback_without_a_certificate(self):
        with self.assertRaises(ValueError):
            youtube.YouTube(config={"url": "http://203.0.113.5:47443", "key": "k" * 20})
        self.assertTrue(youtube.is_local_url("http://127.0.0.1:47443"))
        self.assertFalse(youtube.is_local_url("https://127.0.0.1:47443"))

    def test_h264_aac_is_remuxed_not_transcoded(self):
        self.assertTrue(ytnode.copyable(INFO))
        args = ytnode.ffmpeg_args(self.node.tools, ytnode.stream_inputs(INFO), 0, copy=True)
        self.assertEqual(args[args.index("-c") + 1], "copy")
        self.assertNotIn("libx264", args)
        vp9 = dict(INFO, vcodec="vp9")
        self.assertFalse(ytnode.copyable(vp9))
        self.assertIn("libx264", ytnode.ffmpeg_args(self.node.tools, ytnode.stream_inputs(vp9), 0))

    def test_plain_needs_loopback(self):
        key = self.root / "key"
        key.write_text("k" * 20)
        self.assertEqual(ytnode.main(["--plain", "--bind", "0.0.0.0", "--key-file", str(key)]), 2)
        self.assertEqual(ytnode.main(["--key-file", str(key)]), 2, "the cloud node still needs its certificate")


class _Echo:
    """A TCP echo server: the far end of a tunnel."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.echo, args=(conn,), daemon=True).start()

    @staticmethod
    def echo(conn):
        with conn:
            while data := conn.recv(1024):
                conn.sendall(data)


class _Socks5:
    """A minimal socks5 server with user/password auth that records where it was asked to go."""

    def __init__(self, user=b"u", password=b"p"):
        self.user, self.password, self.targets = user, password, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        def exact(n):
            data = b""
            while len(data) < n:
                data += conn.recv(n - len(data))
            return data
        version, count = exact(2)
        methods = exact(count)
        assert 2 in methods
        conn.sendall(b"\x05\x02")
        exact(1)
        user = exact(exact(1)[0])
        password = exact(exact(1)[0])
        if (user, password) != (self.user, self.password):
            conn.sendall(b"\x01\x01")
            conn.close()
            return
        conn.sendall(b"\x01\x00")
        _v, _c, _r, kind = exact(4)
        host = exact(exact(1)[0]).decode() if kind == 3 else socket.inet_ntoa(exact(4))
        port = int.from_bytes(exact(2), "big")
        self.targets.append((host, port))
        upstream = socket.create_connection(("127.0.0.1", port))
        conn.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + port.to_bytes(2, "big"))
        threading.Thread(target=ytnode._splice, args=(upstream, conn), daemon=True).start()
        ytnode._splice(conn, upstream)


class TunnelTests(unittest.TestCase):
    def test_socks5_shim_gives_ffmpeg_an_http_connect_proxy(self):
        echo, socks = _Echo(), _Socks5()
        proxy = ytnode.parse_proxy(f"socks5h://u:p@127.0.0.1:{socks.port}")
        shim = ytnode.ConnectShim(proxy, log=lambda _m: None).start()
        try:
            port = int(shim.url.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                conn.sendall(f"CONNECT localhost:{echo.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                self.assertIn(b" 200 ", ytnode._read_head(conn))
                conn.sendall(b"media bytes")
                self.assertEqual(conn.recv(100), b"media bytes")
            self.assertEqual(socks.targets, [("localhost", echo.port)], "socks5h sends the name, not an address")
            with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
                conn.sendall(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
                self.assertIn(b" 405 ", conn.recv(200))
        finally:
            shim.close()

    def test_wrong_socks_password_fails_cleanly(self):
        socks = _Socks5()
        proxy = ytnode.parse_proxy(f"socks5://u:wrong@127.0.0.1:{socks.port}")
        with self.assertRaises(OSError) as caught:
            ytnode.open_tunnel(proxy, "127.0.0.1", 1, timeout=5)
        self.assertNotIn("wrong", str(caught.exception))

    def test_http_connect_tunnel(self):
        echo = _Echo()
        seen = []
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def proxy_server():
            conn, _ = listener.accept()
            head = ytnode._read_head(conn)
            seen.append(head)
            upstream = socket.create_connection(("127.0.0.1", echo.port))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            threading.Thread(target=ytnode._splice, args=(upstream, conn), daemon=True).start()
            ytnode._splice(conn, upstream)

        threading.Thread(target=proxy_server, daemon=True).start()
        proxy = ytnode.parse_proxy(f"http://me:pw@127.0.0.1:{listener.getsockname()[1]}")
        with ytnode.open_tunnel(proxy, "www.youtube.com", 443, timeout=5) as sock:
            sock.sendall(b"hello")
            self.assertEqual(sock.recv(10), b"hello")
        self.assertTrue(seen[0].startswith(b"CONNECT www.youtube.com:443 HTTP/1.1"))
        self.assertIn(b"Proxy-Authorization: Basic bWU6cHc=", seen[0])
        listener.close()


class _Handler:
    """Just enough of web.py's handler for ytsettings."""

    def __init__(self, caller, body=None):
        self.caller = caller
        raw = json.dumps(body).encode() if body is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.answers = []
        self.close_connection = False

    def _caller(self):
        return self.caller

    def _json(self, data, code=200, headers=None):
        self.answers.append((code, data))

    def _not_found(self):
        self.answers.append((404, None))


class _Relay:
    def __init__(self, root, selfhost=True):
        self.selfhost = object() if selfhost else None
        self.selfhost_data = root
        self.youtube_kind = ""
        self.clients = {"bilibili": object(), "youtube": None}
        self.logs = []
        self._youtube_health = None

    def log(self, message):
        self.logs.append(message)


class SettingsApiTests(unittest.TestCase):
    ADMIN = Caller(user=1, role="admin", name="me")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        layout = bootstrap.Layout(self.root)
        bootstrap.prepare(layout)
        config.ensure(layout.config)
        self.layout = layout

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, relay, caller):
        handler = _Handler(caller)
        self.assertTrue(ytsettings.get(handler, relay, "/api/youtube/settings"))
        return handler.answers[0]

    def post(self, relay, caller, name, body=None):
        handler = _Handler(caller, body if body is not None else {})
        self.assertTrue(ytsettings.post(handler, relay, "/api/youtube/" + name))
        return handler.answers[0]

    def test_only_the_admin_of_a_household_server(self):
        relay = _Relay(self.root)
        for caller in (None, Caller(user=2, role="user"), Caller(role="guest", device="d1")):
            self.assertEqual(self.get(relay, caller), (404, None))
            for name in ytsettings.POSTS:
                self.assertEqual(self.post(relay, caller, name, {"mode": "local", "proxy": ""}), (404, None))
        self.assertFalse((self.layout.run / ytlocal.REFRESH_REQUEST).exists(), "nothing changed")
        cloud = _Relay(self.root, selfhost=False)
        self.assertEqual(self.get(cloud, self.ADMIN), (404, None), "the cloud relay has no such page")
        self.assertFalse(ytsettings.get(_Handler(self.ADMIN), relay, "/api/other"))

    def test_settings_saved_and_refresh_requested(self):
        relay = _Relay(self.root)
        code, answer = self.get(relay, self.ADMIN)
        self.assertEqual((code, answer["mode"], answer["running"]), (200, "off", "off"))
        code, answer = self.post(relay, self.ADMIN, "settings", {"mode": "local", "proxy": "bad proxy"})
        self.assertFalse(answer["ok"])
        code, answer = self.post(relay, self.ADMIN, "settings",
                                 {"mode": "local", "proxy": "http://127.0.0.1:7897"})
        self.assertEqual(answer, {"ok": True, "changed": True, "restart": True})
        parsed = config.load(self.layout.config)
        self.assertEqual((parsed.youtube_mode, parsed.youtube_proxy), ("local", "http://127.0.0.1:7897"))
        self.assertTrue((self.layout.run / ytlocal.REFRESH_REQUEST).exists())
        self.assertEqual(self.post(relay, self.ADMIN, "settings",
                                   {"mode": "local", "proxy": "http://127.0.0.1:7897"})[1]["changed"], False)
        code, answer = self.post(relay, self.ADMIN, "settings", {"mode": "node", "proxy": ""})
        self.assertIn("youtube-node.json", answer["error"])

    def test_cookies_upload_never_echoes_values(self):
        relay = _Relay(self.root)
        code, answer = self.post(relay, self.ADMIN, "cookies", {"text": COOKIES})
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["cookies"]["count"], 2)
        self.assertNotIn(SECRET_VALUE, json.dumps(answer))
        self.assertNotIn(SECRET_VALUE, " ".join(relay.logs))
        stored = (self.layout.youtube / "cookies.txt").read_text()
        self.assertNotIn("bank.example", stored)
        code, answer = self.post(relay, self.ADMIN, "cookies", {"text": "garbage " + SECRET_VALUE})
        self.assertFalse(answer["ok"])
        self.assertNotIn(SECRET_VALUE, json.dumps(answer, ensure_ascii=False))
        self.assertEqual(self.get(relay, self.ADMIN)[1]["cookies"]["count"], 2)
        self.assertFalse(self.post(relay, self.ADMIN, "cookies/delete")[1]["cookies"]["present"])

    def test_test_without_youtube_says_what_to_do(self):
        relay = _Relay(self.root)
        self.assertIn("保存", self.post(relay, self.ADMIN, "test")[1]["message"])
        code, answer = self.post(relay, self.ADMIN, "components", {"action": "install", "mirror": "ftp://x"})
        self.assertFalse(answer["ok"])


if __name__ == "__main__":
    unittest.main()
