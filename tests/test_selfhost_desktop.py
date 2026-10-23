"""The PC client's core: the self-host supervisor's --desktop mode (phases P1a, P1b, P1c, P3a).

docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md, sections 5.1, 5.4, 5.5, 5.7,
5.9, 5.13 and 5.15. What is checked here without starting the whole core (tools/smoke_test_core.py
does that, from a checkout or from the frozen build):

- the data and log folders per OS; children started through the frozen dispatch; the plain
  self-host path (Docker, ./run.sh --selfhost, the cloud host) unchanged;
- the LAN port fallback, log redaction, the tray's settings file, the single-instance lock;
- the control API's refusals (no token, wrong token, an Origin, a foreign Host) and its answers;
- the one-time console link through the relay: loopback only, once, never as a phone link,
  never through the public port;
- the ASR client over loopback TCP; the ffmpeg download's mirrors and its state for the tray;
- children that end with the supervisor, however it ends; the stdin pipe the tray holds;
- trusted networks (5.11): the LAN ports only on the home network, the tray's answer, the children
  rebuilt on the new address; a phone listening ("listening") in the status.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import front
from server.selfhost import bootstrap, config, control, desktop, diagnostics, network, supervisor
from server.selfhost.__main__ import build_children
from server.voice import asr
from tools import datadir, ffmpeg_fetch, subcommands

NEEDS_311 = unittest.skipIf(sys.version_info < (3, 11), "tomllib needs Python 3.11")


class DataFolderTests(unittest.TestCase):
    def test_windows_uses_local_not_roaming_app_data(self):
        env = {"LOCALAPPDATA": r"C:\Users\张三\AppData\Local", "APPDATA": r"C:\Users\张三\AppData\Roaming"}
        found = datadir.desktop_data_dir("Windows", env, Path("/home/x"))
        self.assertEqual(found, Path(env["LOCALAPPDATA"]) / "XiaozhiTV")
        self.assertEqual(datadir.desktop_logs_dir(None, "Windows", env, Path("/home/x")), found / "logs")

    def test_macos_application_support_and_library_logs(self):
        home = Path("/Users/someone")
        self.assertEqual(datadir.desktop_data_dir("Darwin", {}, home),
                         home / "Library" / "Application Support" / "XiaozhiTV")
        self.assertEqual(datadir.desktop_logs_dir(None, "Darwin", {}, home), home / "Library" / "Logs" / "XiaozhiTV")

    def test_linux_follows_xdg(self):
        home = Path("/home/someone")
        self.assertEqual(datadir.desktop_data_dir("Linux", {}, home), home / ".local" / "share" / "xiaozhi-tv")
        env = {"XDG_DATA_HOME": "/d", "XDG_STATE_HOME": "/s"}
        self.assertEqual(datadir.desktop_data_dir("Linux", env, home), Path("/d/xiaozhi-tv"))
        self.assertEqual(datadir.desktop_logs_dir(None, "Linux", env, home), Path("/s/xiaozhi-tv/logs"))

    def test_an_explicit_data_folder_keeps_its_logs_inside(self):
        self.assertEqual(datadir.desktop_logs_dir(Path("/tmp/d"), "Darwin"), Path("/tmp/d/logs"))


class ChildCommandTests(unittest.TestCase):
    def test_plain_self_host_is_unchanged(self):
        self.assertEqual(supervisor.child_command("server.relay"), [sys.executable, "-u", "-m", "server.relay"])

    def test_desktop_from_a_checkout_goes_through_the_frozen_dispatch(self):
        command = supervisor.child_command("server.front", desktop=True)
        self.assertEqual(command[-2:], [str(ROOT / "tools" / "packaged_entry.py"), "__front"])

    def test_frozen_uses_the_executable_and_a_sub_command(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", "/app/xiaozhi-tv-core"):
            self.assertEqual(supervisor.child_command("server.tv_server"), ["/app/xiaozhi-tv-core", "__tv"])
            self.assertEqual(supervisor.child_command("server.relay", desktop=True), ["/app/xiaozhi-tv-core", "__relay"])

    def test_every_child_has_a_dispatched_module_with_main(self):
        import importlib
        for module, command in subcommands.CHILD_COMMANDS.items():
            self.assertIn(command, subcommands.CORE_COMMANDS)
            self.assertTrue(subcommands.CORE_COMMANDS[command].startswith(module))
        for name in set(subcommands.CORE_COMMANDS.values()):
            self.assertTrue(callable(importlib.import_module(name).main), name)

    @NEEDS_311
    def test_desktop_children_watch_their_parent_and_run_in_the_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp))
            plain = build_children(layout, config.Config(), "/usr/bin/ffmpeg", "", "", log=lambda _m: None)
            ours = build_children(layout, config.Config(), "/usr/bin/ffmpeg", "", "", log=lambda _m: None,
                                  desktop=True)
        self.assertTrue(all(not c.watch_parent and c.cwd is None for c in plain))
        self.assertEqual(plain[0].argv[:4], [sys.executable, "-u", "-m", "server.relay"])
        self.assertTrue(all(c.watch_parent and c.cwd == layout.root for c in ours))
        self.assertEqual([c.argv[3] for c in ours], ["__relay", "__tv", "__tv", "__front"])

    @NEEDS_311
    def test_asr_over_loopback_tcp_reaches_the_media_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp))
            children = build_children(layout, config.Config(asr_socket="http://127.0.0.1:8765"), "/f", "", "",
                                      log=lambda _m: None)
        media = [c for c in children if c.name in ("xingzhi", "passport")]
        self.assertTrue(all(c.env["TV_ASR_URL"] == "http://127.0.0.1:8765" for c in media))
        self.assertTrue(all("TV_ASR_SOCKET" not in c.env for c in media))

    @NEEDS_311
    def test_config_accepts_only_loopback_tcp_for_asr(self):
        self.assertEqual(config.parse('[voice]\nasr_socket = "http://127.0.0.1:8765"\n').asr_socket,
                         "http://127.0.0.1:8765")
        with self.assertRaises(config.ConfigError):
            config.parse('[voice]\nasr_socket = "http://192.168.1.5:8765"\n')


class PortTests(unittest.TestCase):
    def test_fallbacks_in_order(self):
        taken = {8080, 8090}
        self.assertEqual(desktop.pick_lan_port("0.0.0.0", 8080, lambda _b, p: p not in taken), 18080)
        self.assertEqual(desktop.pick_lan_port("0.0.0.0", 8080, lambda _b, p: True), 8080)
        self.assertIsNone(desktop.pick_lan_port("0.0.0.0", 8080, lambda _b, p: False))

    def test_a_listening_port_is_not_free(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        try:
            self.assertFalse(desktop.port_free("127.0.0.1", sock.getsockname()[1]))
        finally:
            sock.close()


class RedactTests(unittest.TestCase):
    def test_secrets_and_addresses(self):
        line = ("GET /tv/?s=abcdefSECRET&home=1 from 192.168.1.23 to 127.0.0.1 HTTP://10.0.0.5:8080/TV/P/ABCDEFGHJKLM "
                "http://localhost:8080/tv/setup#K7P4QX2M9A 设置码：K7P4Q-X2M9A Cookie: tvs=zzzzzz; "
                "Authorization: Bearer tok123 /tv/local-login?t=QWERTYUIOP fe80:1:2:3:4:5:6:7")
        out = desktop.redact(line)
        for secret in ("abcdefSECRET", "192.168.1.23", "10.0.0.5", "ABCDEFGHJKLM", "K7P4QX2M9A", "K7P4Q-X2M9A",
                       "zzzzzz", "tok123", "QWERTYUIOP", "4:5:6:7"):
            self.assertNotIn(secret, out)
        for kept in ("192.168.1.x", "127.0.0.1", "10.0.0.x", "home=1"):
            self.assertIn(kept, out)

    def test_home_folder_becomes_tilde(self):
        self.assertEqual(desktop.redact("/Users/张三/Library/x", home="/Users/张三"), "~/Library/x")

    def test_log_file_is_redacted_and_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = desktop.CoreLog(Path(tmp) / "logs" / "core.log", echo=False)
            log("relay: phone at 192.168.3.44 used ?k=VERYSECRET")
            log.close()
            text = (Path(tmp) / "logs" / "core.log").read_text(encoding="utf-8")
        self.assertIn("192.168.3.x", text)
        self.assertNotIn("VERYSECRET", text)
        self.assertEqual((desktop.LOG_BYTES, desktop.LOG_FILES), (2 * 1024 * 1024, 5))


class SettingsAndLockTests(unittest.TestCase):
    def test_settings_default_validate_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "desktop.json"
            path.write_text('{"keep_awake": "sometimes", "autostart": "yes"}', encoding="utf-8")
            settings = desktop.Settings(path)
            self.assertEqual(settings.snapshot(), desktop.DEFAULT_SETTINGS)
            settings.update(keep_awake="always")
            self.assertEqual(desktop.Settings(path).snapshot()["keep_awake"], "always")

    def test_only_one_core_per_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = desktop.acquire_lock(Path(tmp) / "run" / "core.lock")
            self.assertIsNotNone(first)
            # A second process, as the real second core would be: locks are per process on Windows
            # and per open file on POSIX, so asking from this process would prove nothing.
            code = ("import sys; sys.path.insert(0, sys.argv[1]); from server.selfhost import desktop; "
                    "from pathlib import Path; print(desktop.acquire_lock(Path(sys.argv[2])) is None)")
            out = subprocess.run([sys.executable, "-c", code, str(ROOT), str(Path(tmp) / "run" / "core.lock")],
                                 capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()
            self.assertEqual(out, "True")
            first.close()


class FakeDesktop:
    """Enough of desktop.Desktop for the control API."""

    def __init__(self, root: Path):
        self.layout = bootstrap.Layout(root)
        self.layout.run.mkdir(parents=True, exist_ok=True)
        self.logs_dir = root / "logs"
        self.started = 1.0
        self.control_port = 0
        self.settings = desktop.Settings(root / "desktop.json")
        self.supervisor = None
        self.stop_event = threading.Event()
        self.ffmpeg = desktop.Ffmpeg(root / "bin")

    def status(self):
        return {"ok": True, "state": "starting"}

    def state(self):
        return "starting"


class ControlApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desktop = FakeDesktop(Path(self.tmp.name))
        self.server = control.ControlServer(self.desktop, self.desktop.layout.run, log=lambda _m: None)
        self.server.start()
        self.info = control.read_control(self.desktop.layout.run)

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def raw(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, json.loads(data) if data else {}

    def test_control_json_is_private_and_matches(self):
        self.assertEqual((self.info["port"], self.info["token"]), (self.server.port, self.server.token))
        self.assertEqual(self.info["api"], 1)
        if os.name == "posix":
            mode = (self.desktop.layout.run / "control.json").stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)
        self.server.close()
        self.assertFalse((self.desktop.layout.run / "control.json").exists())

    def test_refusals(self):
        self.assertEqual(self.raw("GET", "/local/v1/status")[0], 401)
        self.assertEqual(self.raw("GET", "/local/v1/status", {"Authorization": "Bearer nope"})[0], 401)
        good = {"Authorization": f"Bearer {self.server.token}"}
        self.assertEqual(self.raw("GET", "/local/v1/status", dict(good, Origin="http://x.example"))[0], 403)
        self.assertEqual(self.raw("GET", "/local/v1/status", dict(good, Host="evil.example"))[0], 403)
        status, answer = self.raw("GET", "/local/v1/status", good)
        self.assertEqual((status, answer["state"]), (200, "starting"))
        status, answer = self.raw("GET", "/local/v1/status", dict(good, Host=f"localhost:{self.server.port}"))
        self.assertEqual(status, 200)

    def test_later_phases_unknown_paths_and_wrong_methods(self):
        self.assertEqual(control.call(self.info, "POST", "remote/enable")[1]["error"], "not_implemented")
        self.assertEqual(control.call(self.info, "POST", "update/check")[0], 501)
        self.assertEqual(control.call(self.info, "POST", "nothing")[0], 404)
        self.assertEqual(control.call(self.info, "GET", "stop")[0], 405)

    def test_settings_validation(self):
        status, answer = control.call(self.info, "POST", "power", {"keep_awake": "forever"})
        self.assertEqual((status, answer["error"]), (400, "bad_request"))
        status, answer = control.call(self.info, "POST", "power", {"keep_awake": "never"})
        self.assertEqual((status, answer["settings"]["keep_awake"]), (200, "never"))
        status, answer = control.call(self.info, "POST", "autostart", {"enabled": False})
        self.assertEqual((status, answer["settings"]["autostart"]), (200, False))

    def test_not_ready_and_too_large(self):
        self.assertEqual(control.call(self.info, "POST", "console-link")[1]["error"], "not_ready")
        self.assertEqual(control.call(self.info, "POST", "restart")[0], 503)
        headers = {"Authorization": f"Bearer {self.server.token}", "Content-Length": str(control.BODY_MAX + 1)}
        self.assertEqual(self.raw("POST", "/local/v1/power", headers, b"x" * (control.BODY_MAX + 1))[0], 413)

    def test_restart_rereads_settings_then_rebuilds(self):
        """POST restart: desktop_main's reload (config.toml, the LAN address) runs first, then the children are
        made again from the new values; without a reload hook the old restart (same values)."""
        class FakeSupervisor:
            started = True

            def __init__(self):
                self.restart_requested, self.rebuild_requested = threading.Event(), threading.Event()

        order = []
        sup = self.desktop.supervisor = FakeSupervisor()
        self.assertEqual(control.call(self.info, "POST", "restart")[0], 202)
        self.assertTrue(sup.restart_requested.is_set())
        self.assertFalse(sup.rebuild_requested.is_set())
        sup = self.desktop.supervisor = FakeSupervisor()
        self.desktop.reload = lambda: order.append("reload" if not sup.rebuild_requested.is_set() else "late")
        self.assertEqual(control.call(self.info, "POST", "restart")[0], 202)
        self.assertTrue(sup.rebuild_requested.wait(5))
        self.assertEqual(order, ["reload"])
        self.assertFalse(sup.restart_requested.is_set())
        # A failing reload still restarts.
        sup = self.desktop.supervisor = FakeSupervisor()
        self.desktop.reload = mock.Mock(side_effect=OSError)
        self.desktop.log = logged = mock.Mock()
        self.assertEqual(control.call(self.info, "POST", "restart")[0], 202)
        self.assertTrue(sup.rebuild_requested.wait(5))
        self.assertIn("OSError", logged.call_args[0][0])
        self.desktop.supervisor = None
        self.desktop.reload = None

    def test_stop_without_a_supervisor_sets_the_event(self):
        self.assertEqual(control.call(self.info, "POST", "stop")[0], 202)
        self.assertTrue(self.desktop.stop_event.is_set())

    def test_ffmpeg_fetch_refuses_a_bad_mirror_and_a_second_start(self):
        status, answer = control.call(self.info, "POST", "ffmpeg/fetch", {"mirror": "ftp://x"})
        self.assertEqual(status, 400)
        with mock.patch.object(self.desktop.ffmpeg, "fetch", return_value=False):
            self.assertEqual(control.call(self.info, "POST", "ffmpeg/fetch")[1]["error"], "busy")
        self.desktop.ffmpeg.state = "ready"
        self.assertEqual(control.call(self.info, "POST", "ffmpeg/fetch")[1]["error"], "ready")


@NEEDS_311
class StatusAndDiagnosticsTests(unittest.TestCase):
    CONTRACT = {"ok", "api", "version", "pid", "started", "state", "problems", "data_dir", "logs_dir", "bind",
                "ports", "lan_url", "local_url", "setup_pending", "children", "devices", "watching", "listening",
                "network", "ffmpeg", "voice", "youtube", "hub", "update", "settings"}

    def make(self, root: Path) -> desktop.Desktop:
        layout = bootstrap.Layout(root)
        bootstrap.prepare(layout)
        config.ensure(layout.config)
        bootstrap.relay_key(layout)
        accounts = bootstrap.database(layout)
        bootstrap.setup_code(layout, accounts)
        accounts.close()
        ffmpeg = desktop.Ffmpeg(root / "bin")
        return desktop.Desktop(layout, root / "logs", lambda _m: None, "127.0.0.1", 8090, 8080,
                               {"xingzhi": 8098, "passport": 8097}, "192.168.1.5", ffmpeg,
                               desktop.Settings(root / "desktop.json"))

    def test_status_has_every_contract_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.make(Path(tmp))
            state.add_problem("lan_port_moved", "moved", **{"from": 8080, "to": 8090})
            self.assertEqual(state.status()["state"], "starting", "ffmpeg not looked for yet")
            state.ffmpeg.resolved = True
            status = state.status()
        self.assertEqual(set(status), self.CONTRACT)
        self.assertEqual(status["ports"]["lan"], 8090)
        self.assertEqual(status["lan_url"], "", "bound to loopback: no LAN address for phones")
        self.assertTrue(status["setup_pending"])
        self.assertEqual(status["state"], "error", "no ffmpeg yet")
        codes = [p["code"] for p in status["problems"]]
        self.assertEqual(codes, ["ffmpeg_missing", "lan_port_moved"])

    def test_listening_and_network_in_the_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.make(Path(tmp))
            with mock.patch.object(desktop, "devices", return_value=[
                    {"id": "a", "name": "", "model": "", "online": True, "channel": "x", "since": 0, "listening": True},
                    {"id": "b", "name": "", "model": "", "online": True, "channel": "y", "since": 0,
                     "listening": False}]):
                status = state.status()
        self.assertEqual((status["watching"], status["listening"]), (2, 1))
        self.assertEqual(status["network"], {"id": "", "trusted": None, "profile": "", "lan_open": False},
                         "bound to loopback by config: no trusted-network logic")

    def test_a_listening_phone_is_read_from_now_playing(self):
        from server import now_playing
        with tempfile.TemporaryDirectory() as tmp:
            state = self.make(Path(tmp))
            listens = {"dev1": {"route": "xingzhi", "lead": 2, "ok": True, "on": True, "lag": 0}, "dev2": None}
            for device_id, listen in listens.items():
                folder = state.layout.devices / device_id
                folder.mkdir(parents=True, exist_ok=True)
                now_playing.write(folder / "now-playing.json", "CCTV-1", True, True, listen=listen)
            with mock.patch.object(desktop.sqlite3, "connect") as connect:
                connect.return_value.execute.return_value.fetchall.return_value = [
                    (device_id, device_id, "xingzhi-cube-154") for device_id in listens]
                found = desktop.devices(state.layout)
        self.assertEqual([(d["id"], d["online"], d["listening"]) for d in found],
                         [("dev1", True, True), ("dev2", True, False)])

    def test_diagnostics_has_logs_and_config_but_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.make(Path(tmp))
            state.logs_dir.mkdir()
            (state.logs_dir / "core.log").write_text("relay: started\n", encoding="utf-8")
            (state.logs_dir / "core.log.1").write_text("older\n", encoding="utf-8")
            (state.logs_dir / "other.txt").write_text("not a log\n", encoding="utf-8")
            with mock.patch.object(diagnostics, "port_holder", return_value=""):
                path = diagnostics.build(state, Path(tmp) / "out")
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                blob = b"".join(archive.read(n) for n in names)
            secrets = [p.read_bytes().strip() for p in state.layout.secrets.iterdir() if p.is_file()]
        self.assertEqual(names, {"about.txt", "status.json", "config.toml", "logs/core.log", "logs/core.log.1"})
        self.assertTrue(secrets)
        for secret in secrets:
            self.assertNotIn(secret, blob)


class FakeAsr(BaseHTTPRequestHandler):
    seen: dict = {}

    def do_POST(self):
        FakeAsr.seen = {"key": self.headers.get(asr.KEY_HEADER), "rate": self.headers.get("X-Sample-Rate"),
                        "bytes": len(self.rfile.read(int(self.headers["Content-Length"])))}
        body = json.dumps({"text": " 换台 "}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


class AsrTcpTests(unittest.TestCase):
    def test_loopback_tcp_with_a_derived_key(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAsr)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            key = asr.asr_key("relay-secret-0123456789")
            client = asr.AsrClient(url=f"http://127.0.0.1:{server.server_address[1]}", key=key)
            self.assertEqual(client.transcribe(b"\x01\x00" * 100), "换台")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(FakeAsr.seen, {"key": key, "rate": "16000", "bytes": 200})
        self.assertNotIn("relay-secret", key, "the relay key itself never leaves")

    def test_only_loopback(self):
        for url in ("http://192.168.1.2:9000", "https://127.0.0.1:9000", "http://127.0.0.1"):
            with self.assertRaises(ValueError, msg=url):
                asr.AsrClient(url=url)

    def test_voice_service_picks_tcp_from_the_environment(self):
        from server.voice.session import VoiceService
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "relay-key"
            key_file.write_text("relay-secret-0123456789\n", encoding="utf-8")
            env = {"TV_ASR_URL": "http://127.0.0.1:8765", "TV_RELAY_KEY_FILE": str(key_file)}
            service = VoiceService.from_env(lambda _m: None, env)
        self.assertIsNotNone(service.asr)
        self.assertEqual(service.asr.key, asr.asr_key("relay-secret-0123456789"))


class FfmpegFetchMirrorTests(unittest.TestCase):
    WHEEL = "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl"

    def test_every_pinned_wheel_has_a_path(self):
        self.assertEqual(set(ffmpeg_fetch.PACKAGE_PATHS), set(ffmpeg_fetch.WHEELS))

    def test_order_given_mirror_then_pypi_or_mainland_first(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ffmpeg_fetch.MIRROR_ENV, None)
            elsewhere = ffmpeg_fetch.sources(self.WHEEL, utc_offset_s=0)
            mainland = ffmpeg_fetch.sources(self.WHEEL, utc_offset_s=8 * 3600)
            given = ffmpeg_fetch.sources(self.WHEEL, "https://mirror.example/pypi/", utc_offset_s=0)
        self.assertTrue(elsewhere[0].startswith("https://files.pythonhosted.org/packages/2c/c6/"))
        self.assertTrue(elsewhere[0].endswith("/" + self.WHEEL))
        self.assertTrue(mainland[0].startswith("https://pypi.tuna.tsinghua.edu.cn/"))
        self.assertEqual(len(set(mainland)), len(ffmpeg_fetch.MIRRORS))
        self.assertTrue(given[0].startswith("https://mirror.example/pypi/packages/"))

    def test_mirror_validation(self):
        self.assertTrue(ffmpeg_fetch.valid_mirror("https://mirrors.aliyun.com/pypi"))
        self.assertTrue(ffmpeg_fetch.valid_mirror("http://127.0.0.1:8000"))
        for bad in ("http://mirror.example", "ftp://x", "https://x/?a=1", "not a url"):
            self.assertFalse(ffmpeg_fetch.valid_mirror(bad), bad)

    def test_a_failing_source_falls_through_to_the_next(self):
        from test_ffmpeg_fetch import make_wheel
        import hashlib
        blob = make_wheel(contents=b"\x7fELF ok")
        tried = []

        def download(url, _progress=None):
            tried.append(url)
            if len(tried) == 1:
                raise ffmpeg_fetch.FetchError("first mirror down")
            return blob

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(ffmpeg_fetch.WHEELS, {self.WHEEL: (hashlib.sha256(blob).hexdigest(), len(blob))}), \
                mock.patch.object(ffmpeg_fetch, "wheel_for", return_value=self.WHEEL), \
                mock.patch.object(ffmpeg_fetch, "_download", side_effect=download):
            path = ffmpeg_fetch.fetch(Path(tmp))
            self.assertEqual(path.read_bytes(), b"\x7fELF ok")
        self.assertEqual(len(tried), 2)


@unittest.skipUnless(os.name == "posix", "a shell script stands in for ffmpeg")
class FfmpegStateTests(unittest.TestCase):
    def fake(self, folder: Path) -> Path:
        path = folder / "ffmpeg"
        path.write_text("#!/bin/sh\necho 'ffmpeg version 7.1-test'\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_given_found_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg = desktop.Ffmpeg(Path(tmp) / "bin")
            self.assertTrue(ffmpeg.resolve(str(self.fake(Path(tmp))), environ={}))
            self.assertEqual((ffmpeg.state, ffmpeg.source), ("ready", "given"))
            self.assertIn("7.1-test", ffmpeg.version)
            other = desktop.Ffmpeg(Path(tmp) / "bin")
            self.assertFalse(other.resolve(str(Path(tmp) / "nope"), environ={}))
            self.assertEqual(other.state, "failed")
            self.assertTrue(other.resolved)

    def test_nothing_found_is_not_an_error_until_the_download_starts(self):
        """CI's core smoke test saw state "error" (ffmpeg_missing) between the search and the download."""
        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg = desktop.Ffmpeg(Path(tmp) / "bin")
            self.assertFalse(ffmpeg.resolve(desktop.FFMPEG_FETCH, environ={}))
            self.assertEqual(ffmpeg.state, "missing")
            self.assertFalse(ffmpeg.resolved, "missing here means: about to download")
            with mock.patch.object(ffmpeg, "_fetch"):
                self.assertTrue(ffmpeg.fetch())
            self.assertEqual((ffmpeg.state, ffmpeg.resolved), ("fetching", True))

    def test_fetch_reports_progress_and_becomes_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = self.fake(Path(tmp))
            ffmpeg = desktop.Ffmpeg(Path(tmp) / "bin", log=lambda _m: None)

            def ensure(_dir, on_announce=None, on_progress=None, mirror=None, on_source=None):
                on_source("https://mirror.example/packages/x.whl")
                on_progress(5, 10)
                return fake

            with mock.patch.object(ffmpeg_fetch, "ensure", side_effect=ensure):
                self.assertTrue(ffmpeg.fetch(wait=True))
            snap = ffmpeg.snapshot()
            self.assertEqual((snap["state"], snap["source"], snap["done"], snap["mirror"]),
                             ("ready", "fetched", 5, "mirror.example"))
            self.assertFalse(ffmpeg.fetch(), "already there")

    def test_fetch_failure_is_a_state_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ffmpeg = desktop.Ffmpeg(Path(tmp) / "bin", log=lambda _m: None)
            with mock.patch.object(ffmpeg_fetch, "ensure", side_effect=ffmpeg_fetch.FetchError("没网\n细节")):
                ffmpeg.fetch(wait=True)
            self.assertEqual((ffmpeg.state, ffmpeg.error), ("failed", "没网"))


class TrustedNetworkTests(unittest.TestCase):
    HOME, CAFE = "0123456789abcdef", "fedcba9876543210"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = desktop.Settings(Path(self.tmp.name) / "desktop.json")
        self.seen = self.HOME
        self.changes = []

    def make(self, bind="0.0.0.0", profiles=None):
        nets = network.TrustedNetworks(bind, self.settings, lambda _m: None, identify=lambda: self.seen,
                                       profiles=profiles, platform_name="darwin", clock=lambda: 1000.0)
        nets.on_change = lambda: self.changes.append(nets.bind)
        nets.check(initial=True)
        return nets

    def test_first_run_is_open_and_asks(self):
        nets = self.make()
        self.assertEqual(nets.decision(), "ask")
        self.assertEqual(nets.bind, "0.0.0.0")
        self.assertEqual(nets.snapshot(), {"id": self.HOME, "trusted": None, "profile": "", "lan_open": True})
        self.assertEqual(nets.problems(), [])

    def test_away_from_a_remembered_home_only_loopback(self):
        nets = self.make()
        nets.trust(self.HOME, True)
        self.assertEqual(self.changes, [], "trusting the network we are on changes nothing")
        self.seen = self.CAFE
        self.assertFalse(nets.check(), "one reading is not enough (ARP, Wi-Fi reconnecting)")
        self.assertEqual(nets.bind, "0.0.0.0")
        self.assertTrue(nets.check())
        self.assertEqual(nets.bind, "127.0.0.1")
        self.assertEqual(self.changes, ["127.0.0.1"])
        self.assertEqual([p["code"] for p in nets.problems()], ["network_away"])
        self.assertEqual(nets.snapshot()["trusted"], None, "a new router can still be trusted")
        self.seen = ""
        nets.check()
        nets.check()
        self.assertEqual(nets.bind, "127.0.0.1", "an unknown router is not home")
        self.seen = self.HOME
        nets.check()
        self.assertTrue(nets.check())
        self.assertEqual(self.changes, ["127.0.0.1", "0.0.0.0"])

    def test_not_my_home_network_and_a_new_router(self):
        nets = self.make()
        nets.trust(self.HOME, False)
        self.assertEqual(nets.bind, "127.0.0.1")
        self.assertEqual(nets.snapshot()["trusted"], False)
        nets.trust(self.HOME, True)
        self.assertEqual(nets.bind, "0.0.0.0")
        self.assertEqual(self.changes, ["127.0.0.1", "0.0.0.0"])
        again = desktop.Settings(Path(self.tmp.name) / "desktop.json")
        self.assertEqual(again.networks(), {"trusted": [self.HOME], "untrusted": []})
        self.assertEqual(set(again.snapshot()), {"keep_awake", "autostart"}, "router ids stay out of settings")

    def test_configured_loopback_never_opens(self):
        nets = self.make(bind="127.0.0.1")
        nets.trust(self.HOME, True)
        self.assertEqual(nets.bind, "127.0.0.1")
        self.assertEqual(nets.problems(), [])

    def test_windows_public_only_matters_at_home(self):
        nets = self.make(profiles=lambda: [{"name": "x", "category": "public"}])
        nets.check_profile()
        self.assertEqual(nets.snapshot()["profile"], "public")
        problems = nets.problems()
        self.assertEqual([p["code"] for p in problems], ["network_public"])
        self.assertNotIn("x", problems[0]["message"].replace("Internet", ""), "no network names")
        nets.trust(self.HOME, False)
        self.assertEqual([p["code"] for p in nets.problems()], ["network_away"])

    def test_bad_ids_in_the_file_are_dropped(self):
        (Path(self.tmp.name) / "desktop.json").write_text(json.dumps(
            {"trusted_networks": [self.HOME, "../x", 5], "untrusted_networks": "nope"}), encoding="utf-8")
        self.assertEqual(desktop.Settings(Path(self.tmp.name) / "desktop.json").networks(),
                         {"trusted": [self.HOME], "untrusted": []})

    def test_control_api_trust(self):
        fake = FakeDesktop(Path(self.tmp.name))
        server = control.ControlServer(fake, fake.layout.run, log=lambda _m: None)
        server.start()
        self.addCleanup(server.close)
        info = control.read_control(fake.layout.run)
        fake.network = None
        self.assertEqual(control.call(info, "POST", "network/trust", {"id": self.HOME, "trusted": True})[0], 409)
        fake.network = self.make()
        self.assertEqual(control.call(info, "POST", "network/trust", {"id": "nope", "trusted": True})[0], 400)
        self.assertEqual(control.call(info, "POST", "network/trust", {"id": self.HOME, "trusted": 1})[0], 400)
        self.assertEqual(control.call(info, "GET", "network/trust")[0], 405)
        status, answer = control.call(info, "POST", "network/trust", {"id": self.HOME, "trusted": True})
        self.assertEqual((status, answer["network"]["trusted"]), (200, True))


class SupervisorTests(unittest.TestCase):
    SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]

    def test_waits_for_pending_children_then_restarts_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp))
            layout.run.mkdir(parents=True)
            calls = []

            def pending():
                calls.append(1)
                return None if len(calls) < 3 else [supervisor.Child("a", self.SLEEPER, dict(os.environ),
                                                                     lambda _m: None, watch=True)]

            sup = supervisor.Supervisor(layout, [], 14, lambda _m: None, pending=pending)
            thread = threading.Thread(target=sup.run)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not (sup.started and sup.children[0].running) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(sup.started)
                first = sup.children[0].pid
                sup.restart_requested.set()
                deadline = time.monotonic() + 15
                while (sup.children[0].pid in (None, first)) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertNotEqual(sup.children[0].pid, first)
            finally:
                sup.stopping.set()
                thread.join(20)
            self.assertFalse(thread.is_alive())
            self.assertFalse(sup.children[0].running)
            self.assertGreaterEqual(len(calls), 3)

    def test_rebuild_makes_new_children_from_pending(self):
        """A network change: the children are made again (new --bind) and started."""
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp))
            layout.run.mkdir(parents=True)
            made = []

            def pending():
                child = supervisor.Child(f"a{len(made)}", self.SLEEPER, dict(os.environ), lambda _m: None, watch=True)
                made.append(child)
                return [child]

            sup = supervisor.Supervisor(layout, [], 14, lambda _m: None, pending=pending)
            thread = threading.Thread(target=sup.run)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not (sup.started and made[0].running) and time.monotonic() < deadline:
                    time.sleep(0.1)
                sup.rebuild_requested.set()
                deadline = time.monotonic() + 15
                while not (len(made) == 2 and made[1].running) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual([c.name for c in sup.children], ["a1"])
                self.assertFalse(made[0].running)
                self.assertTrue(made[1].running)
            finally:
                sup.stopping.set()
                thread.join(20)
            self.assertFalse(made[1].running)

    def test_stdin_pipe_closing_sets_the_event(self):
        read, write = os.pipe()
        event = threading.Event()
        with mock.patch.dict(os.environ, {supervisor.WATCH_ENV: "1"}), os.fdopen(read, "rb") as stream:
            self.assertTrue(supervisor.watch_stdin(event, stream))
            self.assertFalse(event.wait(0.3))
            os.close(write)
            self.assertTrue(event.wait(5))

    def test_no_watch_without_the_variable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(supervisor.WATCH_ENV, None)
            self.assertFalse(supervisor.watch_stdin(threading.Event(), io.StringIO()))

    @unittest.skipUnless(os.name == "posix", "process groups")
    def test_a_watched_child_takes_its_own_children_with_it(self):
        script = ("import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', "
                  "'import time; time.sleep(60)']); print(p.pid, flush=True); time.sleep(60)")
        lines = []
        child = supervisor.Child("a", [sys.executable, "-c", script], dict(os.environ), lines.append, watch=True)
        child.start()
        deadline = time.monotonic() + 10
        while not lines and time.monotonic() < deadline:
            time.sleep(0.05)
        grandchild = int(lines[0].split()[-1])
        child.stop()
        child.wait_or_kill(time.monotonic() + 10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the grandchild survived its parent's stop")


@NEEDS_311
class LocalLoginTests(unittest.TestCase):
    """The relay half of console-link (server/relay/setup.py, /local-login)."""

    def setUp(self):
        from test_selfhost_relay import CODE, RelayHarness  # noqa: F401
        self.harness = RelayHarness("setUp")
        self.harness.setUp()
        self.harness.setup_admin()
        self.accounts = self.harness.accounts
        from server.tenancy.phonelinks import PhoneLinks
        self.links = PhoneLinks(self.accounts)

    def tearDown(self):
        self.harness.tearDown()

    def test_signs_in_the_local_browser_once(self):
        token = self.links.create_local(self.accounts.admin_id())["token"]
        status, response, _ = self.harness.request("GET", f"/tv/local-login?t={token}")
        cookie = response.getheader("Set-Cookie") or ""
        self.assertEqual((status, response.getheader("Location")), (302, "/tv/console"))
        self.assertTrue(cookie.startswith("tvs="))
        session = self.accounts.session(cookie.split(";")[0][4:])
        self.assertEqual((session["kind"], session["role"]), ("pc", "admin"))
        status, response, _ = self.harness.request("GET", f"/tv/local-login?t={token}")
        self.assertIsNone(response.getheader("Set-Cookie"), "one use")

    def test_not_from_another_computer(self):
        token = self.links.create_local(self.accounts.admin_id())["token"]
        status, _, _ = self.harness.request("GET", f"/tv/local-login?t={token}", headers={"X-Real-IP": "192.168.1.9"})
        self.assertEqual(status, 404)

    def test_not_as_a_phone_link_and_not_a_phone_link_here(self):
        admin = self.accounts.admin_id()
        token = self.links.create_local(admin)["token"]
        answer = self.harness.request("POST", "/tv/api/phone-link/accept", {"token": token})[2]
        self.assertFalse(answer["ok"])
        answer = self.harness.request("GET", f"/tv/api/phone-link/info?t={token}")[2]
        self.assertFalse(answer["ok"])
        phone = self.links.create_for_admin(admin)["token"]
        status, _, _ = self.harness.request("GET", f"/tv/local-login?t={phone}")
        self.assertEqual(status, 404)

    def test_expires(self):
        token = self.links.create_local(self.accounts.admin_id(), ttl=-1)["token"]
        _, response, _ = self.harness.request("GET", f"/tv/local-login?t={token}")
        self.assertIsNone(response.getheader("Set-Cookie"))

    def test_front_never_forwards_it_on_the_public_port(self):
        self.assertEqual(front.route("/tv/local-login", "t=X", True, front.Options()).kind, "refuse")
        self.assertEqual(front.route("/tv/local-login", "t=X", False, front.Options()).kind, "proxy")


if __name__ == "__main__":
    unittest.main()
