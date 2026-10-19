"""The PC client's tray (desktop/): menu state, control-API client, first run, power, autostart.

Runs without pystray, Pillow or a display: everything tested here is the part of
the tray that does not draw. The control API is the stand-in core
(desktop/devcore.py) serving on 127.0.0.1 in this process.
"""

from __future__ import annotations

import collections
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop import autostart, menu, paths, power  # noqa: E402
from tools import netcheck  # noqa: E402
from desktop.control import (ControlClient, ControlError, ControlUnavailable, Device, Ffmpeg,  # noqa: E402
                             Network, Problem, Status, read_control_file)
from desktop.core import CoreProcess, redact  # noqa: E402
from desktop.devcore import DevCore  # noqa: E402
from desktop.instance import SingleInstance, hand_off  # noqa: E402
from desktop.tray import App, Prefs, phone_page  # noqa: E402


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def running(status: Status | None = None, **kw) -> menu.TrayState:
    return menu.TrayState(core="running", status=status or Status(), **kw)


# ----------------------------------------------------------------------------- menu

class MenuTest(unittest.TestCase):
    def test_headline_follows_the_core(self):
        self.assertIn("正在启动", menu.headline(menu.TrayState()))
        self.assertIn("正在启动", menu.headline(menu.TrayState(core="running", status=None)))
        self.assertIn("没有在运行", menu.headline(menu.TrayState(core="restarting")))
        self.assertIn("没有小电视在看", menu.headline(running()))
        two = Status(devices=(Device("客厅", watching=True, online=True), Device("卧室", watching=True, online=True),
                              Device("书房", online=False)))
        self.assertEqual(menu.headline(running(two)), "小智电视 · 正在为 2 台小电视推流")
        self.assertIn("第一次设置", menu.headline(running(Status(setup_pending=True))))
        self.assertIn("出了问题", menu.headline(running(Status(state="error"))))
        self.assertIn("视频组件", menu.headline(running(Status(state="fetching_ffmpeg"))))

    def test_open_console_is_the_default_and_disabled_until_running(self):
        starting = menu.build(menu.TrayState())
        console = next(i for i in starting if i.action == menu.OPEN_CONSOLE)
        self.assertFalse(console.enabled)
        self.assertTrue(console.default)
        ready = menu.build(running())
        console = next(i for i in ready if i.action == menu.OPEN_CONSOLE)
        self.assertTrue(console.enabled and console.default)
        # Quit and the logs always work, even with no core at all.
        for action in (menu.QUIT, menu.OPEN_LOGS):
            self.assertTrue(next(i for i in starting if i.action == action).enabled)

    def test_first_run_puts_setup_first(self):
        items = menu.build(running(Status(setup_pending=True)))
        setup = next(i for i in items if i.action == menu.OPEN_SETUP)
        self.assertTrue(setup.default)
        self.assertFalse(next(i for i in items if i.action == menu.OPEN_CONSOLE).default)
        self.assertFalse(next(i for i in items if i.action == menu.CONNECT_PHONE).enabled)

    def test_devices_submenu(self):
        status = Status(devices=(Device("客厅", online=True, watching=True, channel="CCTV-1"), Device("卧室")))
        item = next(i for i in menu.build(running(status)) if i.submenu)
        texts = [d.text for d in item.submenu]
        self.assertEqual(texts, ["客厅 —— CCTV-1（在看）", "卧室 —— 离线"])
        self.assertTrue(all(not d.enabled for d in item.submenu))
        self.assertFalse(any(i.submenu for i in menu.build(running())))

    def test_ffmpeg_download_progress_and_failure(self):
        downloading = running(Status(ffmpeg=Ffmpeg("downloading", 0.42)))
        self.assertIn("正在下载视频组件（ffmpeg）… 42%", [i.text for i in menu.build(downloading)])
        self.assertEqual(menu.icon_kind(downloading), "idle")       # news, not a problem
        failed = running(Status(ffmpeg=Ffmpeg("failed", error="网络超时")))
        self.assertTrue(any("下载失败：网络超时" in i.text for i in menu.build(failed)))
        self.assertEqual(menu.icon_kind(failed), "problem")
        self.assertIn(menu.FETCH_FFMPEG, menu.actions_in(menu.build(failed)))
        # The core's own message replaces the tray's wording, not doubled.
        told = running(Status(ffmpeg=Ffmpeg("missing"), problems=(Problem("ffmpeg_missing", "缺少 ffmpeg"),)))
        texts = [i.text for i in menu.build(told)]
        self.assertIn("缺少 ffmpeg", texts)
        self.assertFalse(any("缺少视频组件" in t for t in texts))

    def test_icon_kind(self):
        self.assertEqual(menu.icon_kind(running()), "idle")
        self.assertEqual(menu.icon_kind(running(Status(devices=(Device("a", watching=True),)))), "watching")
        self.assertEqual(menu.icon_kind(running(Status(problems=(Problem("port", "8098 被占用"),)))), "problem")
        self.assertEqual(menu.icon_kind(menu.TrayState(core="restarting")), "problem")

    def test_toggles_reflect_state(self):
        items = menu.build(running(keep_awake="always", autostart=True))
        self.assertTrue(next(i for i in items if i.action == menu.KEEP_AWAKE).checked)
        self.assertTrue(next(i for i in items if i.action == menu.AUTOSTART).checked)
        items = menu.build(running(keep_awake="watching", autostart=None))
        self.assertFalse(next(i for i in items if i.action == menu.KEEP_AWAKE).checked)
        login = next(i for i in items if i.action == menu.AUTOSTART)
        self.assertFalse(login.enabled)     # source checkout or DMG: cannot register

    def test_network_items(self):
        ask = running(Status(network=Network(id="abc", trusted=None)))
        self.assertIn(menu.TRUST_NETWORK, menu.actions_in(menu.build(ask)))
        trusted = running(Status(network=Network(id="abc", trusted=True)))
        self.assertNotIn(menu.TRUST_NETWORK, menu.actions_in(menu.build(trusted)))
        away = running(Status(network=Network(id="xyz", trusted=False, lan_open=False)))
        self.assertTrue(any("不在家里的网络" in i.text for i in menu.build(away)))
        public = running(windows_public="家里的WiFi")
        texts = [i.text for i in menu.build(public)]
        self.assertTrue(any("公用网络" in t for t in texts))
        self.assertIn(menu.NETWORK_HELP, menu.actions_in(menu.build(public)))

    def test_network_problems_from_the_core_are_not_repeated(self):
        away = running(Status(network=Network(id="xyz", trusted=None, lan_open=False),
                              problems=(Problem("network_away", "不在家里的网络上：核心说的"),)))
        texts = [i.text for i in menu.build(away)]
        self.assertEqual(sum("不在家里的网络" in t for t in texts), 1)
        self.assertIn(menu.TRUST_NETWORK, menu.actions_in(menu.build(away)), "a new router can still be trusted")
        public = running(Status(problems=(Problem("network_public", "Windows 把现在连着的网络标成了「公用」"),)),
                         windows_public="家里的WiFi")
        texts = [i.text for i in menu.build(public)]
        self.assertEqual(sum("公用" in t for t in texts), 1)
        only_core = running(Status(problems=(Problem("network_public", "公用"),)))
        self.assertIn(menu.NETWORK_HELP, menu.actions_in(menu.build(only_core)))

    def test_a_listening_phone_is_shown(self):
        state = running(Status(devices=(Device("客厅", online=True, watching=True, channel="CCTV-1", listening=True),)))
        devices = next(i for i in menu.build(state) if i.submenu)
        self.assertIn("手机在听", devices.submenu[0].text)
        status = Status.from_json({"devices": [{"name": "a", "online": True, "channel": "x", "listening": True},
                                               {"name": "b", "online": False, "listening": True}],
                                   "listening": 1})
        self.assertEqual([d.listening for d in status.devices], [True, False])
        self.assertEqual(status.listening, 1)

    def test_translocated_app_is_told_to_move(self):
        self.assertTrue(any("应用程序" in i.text for i in menu.build(running(translocated=True))))

    def test_tooltip_is_short(self):
        state = running(Status(problems=tuple(Problem("x", "很长的问题" * 40) for _ in range(3))))
        self.assertLessEqual(len(menu.tooltip(state)), 127)

    def test_every_menu_action_has_a_handler(self):
        state = running(Status(setup_pending=True, network=Network("n", None), ffmpeg=Ffmpeg("failed"),
                               devices=(Device("a", watching=True),)), windows_public="x", autostart=True)
        reachable = menu.actions_in(menu.build(state)) | menu.actions_in(menu.build(running()))
        self.assertLessEqual(reachable, set(App.ACTIONS))
        for name in App.ACTIONS.values():
            self.assertTrue(callable(getattr(App, name)))


# ----------------------------------------------------------------------------- status parsing

class StatusTest(unittest.TestCase):
    def test_garbage_is_unknown_not_an_error(self):
        for raw in (None, [], "x", {"devices": "no", "ffmpeg": 3, "problems": {}, "network": [],
                                    "listening": True, "watching": -1, "setup_pending": "yes",
                                    "ports": {"lan": 70000}}):
            status = Status.from_json(raw)
            self.assertEqual(status.devices, ())
            self.assertFalse(status.setup_pending)
            self.assertIsNone(status.network)
            self.assertEqual(status.listening, 0)
            self.assertEqual(status.watching, 0)
            self.assertEqual(status.lan_port, 0)

    def test_p1c_status(self):
        # The example of design 5.13, "The contract as implemented (P1c)".
        status = Status.from_json({
            "ok": True, "api": 1, "version": "0.2.0-beta", "pid": 4242, "started": 1760870000.0,
            "state": "running", "problems": [{"code": "port_in_use", "message": "8098 被 foo 占用", "port": 8098}],
            "data_dir": "/d", "logs_dir": "/l", "bind": "0.0.0.0",
            "ports": {"lan": 8090, "lan_wanted": 8080, "xingzhi": 8098, "passport": 8097, "control": 51234},
            "lan_url": "http://192.168.1.5:8090/tv/", "local_url": "http://localhost:8090/tv/",
            "setup_pending": True,
            "children": {"relay": {"running": True, "restarts": 0}},
            "devices": [{"id": "a", "name": "客厅", "model": "xingzhi-cube-154", "online": True, "channel": "CCTV-1",
                         "since": 1760870100.0},
                        {"id": "b", "name": "", "model": "ai-passport", "online": True, "channel": ""}],
            "watching": 1,
            "ffmpeg": {"state": "fetching", "path": "", "source": "", "done": 15, "total": 60, "error": ""},
            "voice": {"mode": "off"}, "hub": {"state": "off"}, "update": {"available": False},
            "settings": {"keep_awake": "always", "autostart": False}})
        self.assertEqual(status.watching, 1)
        self.assertEqual([d.watching for d in status.devices], [True, False])
        self.assertEqual(status.devices[1].name, "b")
        self.assertTrue(status.setup_pending)
        self.assertEqual(status.ffmpeg, Ffmpeg("downloading", 0.25, ""))
        self.assertEqual(status.problems, (Problem("port_in_use", "8098 被 foo 占用"),))
        self.assertIs(status.autostart, False)
        self.assertEqual(status.keep_awake, "always")
        self.assertEqual(status.logs_dir, "/l")
        self.assertEqual(status.console_base, "http://localhost:8090")
        self.assertFalse(status.power_held_by_core)
        self.assertIsNone(status.network)
        self.assertEqual(Status.from_json({"ffmpeg": {"state": "ready"}}).ffmpeg.state, "ok")

    def test_proposed_fields(self):
        status = Status.from_json({"power": {"managed_by": "core"},
                                   "network": {"id": "abc", "trusted": True, "profile": "Private", "lan_open": True}})
        self.assertTrue(status.power_held_by_core)
        self.assertEqual(status.network, Network("abc", True, "private", True))

    def test_console_base_without_local_url(self):
        self.assertEqual(Status.from_json({"ports": {"lan": 18080}}).console_base, "http://localhost:18080")


# ----------------------------------------------------------------------------- control client

class ControlClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "数据 with space"
        self.core = DevCore(self.data, devices=1, network=True).start()
        self.client = ControlClient(paths.control_file(self.data), timeout=3)

    def tearDown(self):
        self.core.stop()
        self.tmp.cleanup()

    def test_control_file_is_private_and_valid(self):
        info = read_control_file(paths.control_file(self.data))
        self.assertIsNotNone(info)
        if os.name == "posix":
            self.assertEqual(paths.control_file(self.data).stat().st_mode & 0o777, 0o600)

    def test_read_control_file_rejects_bad_files(self):
        path = Path(self.tmp.name) / "c.json"
        for text in ("", "[]", "{", json.dumps({"port": 0, "token": "x" * 20}),
                     json.dumps({"port": 8080, "token": "short"}), json.dumps({"port": True, "token": "x" * 20}),
                     json.dumps({"port": "8080", "token": "x" * 20})):
            path.write_text(text, encoding="utf-8")
            self.assertIsNone(read_control_file(path), text)
        self.assertIsNone(read_control_file(Path(self.tmp.name) / "missing.json"))

    def test_status_and_actions(self):
        status = self.client.status()
        self.assertEqual(status.watching, 1)
        self.assertTrue(self.client.console_link().startswith("http://localhost:"))
        self.client.set_power("always")
        self.assertEqual(self.client.status().keep_awake, "always")
        self.client.set_autostart(False)
        self.assertIs(self.client.status().autostart, False)
        self.client.trust_network("0123456789abcdef", True)
        self.assertTrue(self.client.status().network.trusted)
        with self.assertRaises(ControlError) as caught:
            self.client.set_power("sometimes")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.message, "keep_awake 不对")
        with self.assertRaises(ControlError) as caught:
            self.client.request("POST", "/remote/enable")
        self.assertEqual((caught.exception.status, caught.exception.code), (501, "not_implemented"))
        self.assertTrue(self.client.diagnostics()["path"].endswith(".zip"))

    def test_wrong_token_is_refused(self):
        path = paths.control_file(self.data)
        info = json.loads(path.read_text(encoding="utf-8"))
        info["token"] = "not-the-token-at-all"
        path.write_text(json.dumps(info), encoding="utf-8")
        with self.assertRaises(ControlError) as caught:
            self.client.status()
        self.assertEqual(caught.exception.status, 401)

    def test_unavailable_without_file_or_listener(self):
        self.core.stop()
        with self.assertRaises(ControlUnavailable):
            self.client.status()
        # A stale file pointing at a closed port is "unavailable" too, not a crash.
        paths.control_file(self.data).write_text(json.dumps({"port": 9, "token": "x" * 32}), encoding="utf-8")
        with self.assertRaises(ControlUnavailable):
            ControlClient(paths.control_file(self.data), timeout=1).status()

    def test_restarted_core_is_found_again(self):
        self.core.stop()
        self.core = DevCore(self.data).start()      # new port, new token
        self.assertEqual(self.client.status().watching, 0)


# ----------------------------------------------------------------------------- single instance

class SingleInstanceTest(unittest.TestCase):
    def test_second_lock_fails_until_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "run" / "tray.lock"
            first, second = SingleInstance(lock), SingleInstance(lock)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    def test_lock_held_by_another_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "tray.lock"
            code = ("import sys,time; sys.path.insert(0, sys.argv[1]); from desktop.instance import SingleInstance;"
                    "s=SingleInstance(sys.argv[2]); print(s.acquire(), flush=True); time.sleep(30)")
            holder = subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(lock)], stdout=subprocess.PIPE,
                                      text=True, encoding="utf-8")
            try:
                self.assertEqual(holder.stdout.readline().strip(), "True")
                self.assertFalse(SingleInstance(lock).acquire())
            finally:
                holder.kill()
                holder.wait()
                holder.stdout.close()
            # The OS dropped the lock with the process: no stale "already running".
            mine = SingleInstance(lock)
            self.assertTrue(mine.acquire())
            mine.release()

    def test_hand_off_waits_for_the_core_then_opens_the_console(self):
        class Client:
            calls = 0

            def console_link(self):
                Client.calls += 1
                if Client.calls < 3:
                    raise ControlUnavailable("starting")
                return "http://localhost:8080/tv/console?login=x"
        opened, clock = [], Clock()

        def sleep(seconds):
            clock.now += seconds
        self.assertTrue(hand_off(Client(), opened.append, sleep=sleep, clock=clock, log=lambda m: None))
        self.assertEqual(opened, ["http://localhost:8080/tv/console?login=x"])

    def test_hand_off_gives_up(self):
        class Client:
            def console_link(self):
                raise ControlUnavailable("never")
        clock = Clock()

        def sleep(seconds):
            clock.now += seconds
        opened = []
        self.assertFalse(hand_off(Client(), opened.append, wait_s=5, sleep=sleep, clock=clock, log=lambda m: None))
        self.assertEqual(opened, [])


# ----------------------------------------------------------------------------- autostart

class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ, REG_BINARY = 1, 2, 1, 3

    def __init__(self):
        self.keys: dict[str, dict] = {}

    class _Key:
        def __init__(self, store, path):
            self.store, self.path = store, path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def CreateKeyEx(self, root, path, reserved, access):
        self.keys.setdefault(path, {})
        return self._Key(self, path)

    def OpenKey(self, root, path, reserved, access):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return self._Key(self, path)

    def QueryValueEx(self, key, name):
        try:
            return self.keys[key.path][name], self.REG_SZ
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, reserved, kind, value):
        self.keys[key.path][name] = value

    def DeleteValue(self, key, name):
        try:
            del self.keys[key.path][name]
        except KeyError:
            raise FileNotFoundError(name) from None


class AutostartTest(unittest.TestCase):
    def test_mac_launch_agent(self):
        with tempfile.TemporaryDirectory() as home:
            program = Path("/Applications/Xiaozhi TV.app/Contents/MacOS/XiaozhiTV")
            agent = autostart.MacLaunchAgent(program, home=Path(home))
            self.assertFalse(agent.is_enabled())
            agent.enable()
            self.assertTrue(agent.is_enabled())
            data = plistlib.loads(agent.path.read_bytes())
            self.assertEqual(data["Label"], "io.github.ai-passport-tv.XiaozhiTV")
            self.assertEqual(data["ProgramArguments"], [str(program), "--autostarted"])
            self.assertTrue(data["RunAtLoad"])
            self.assertNotIn("KeepAlive", data)     # quitting from the menu must stay quit
            self.assertEqual(agent.path.parent, Path(home) / "Library" / "LaunchAgents")
            # An entry for another copy of the app (moved, renamed) does not count.
            self.assertFalse(autostart.MacLaunchAgent(Path("/elsewhere/XiaozhiTV"), home=Path(home)).is_enabled())
            agent.disable()
            agent.disable()
            self.assertFalse(agent.path.exists())

    def test_windows_run_key(self):
        reg = FakeWinreg()
        entry = autostart.WindowsRunKey(Path(r"C:\Program Files\Xiaozhi TV\XiaozhiTV.exe"), winreg=reg)
        self.assertFalse(entry.is_enabled())
        entry.enable()
        self.assertEqual(reg.keys[autostart.RUN_KEY]["XiaozhiTV"],
                         r'"C:\Program Files\Xiaozhi TV\XiaozhiTV.exe" --autostarted')
        self.assertTrue(entry.is_enabled())
        # Switched off in Task Manager: present but not enabled; enabling clears the mark.
        reg.keys[autostart.APPROVED_KEY]["XiaozhiTV"] = bytes([3] + [0] * 11)
        self.assertFalse(entry.is_enabled())
        entry.enable()
        self.assertTrue(entry.is_enabled())
        entry.disable()
        self.assertFalse(entry.is_enabled())
        self.assertNotIn("XiaozhiTV", reg.keys[autostart.RUN_KEY])

    @unittest.skipIf(os.name == "nt", "XDG autostart is for Linux desktops; Windows paths differ")
    def test_xdg_autostart(self):
        with tempfile.TemporaryDirectory() as config:
            entry = autostart.XdgAutostart(Path("/opt/xiaozhi-tv/xiaozhi-tv"), config_home=Path(config))
            entry.enable()
            text = entry.path.read_text(encoding="utf-8")
            self.assertIn('Exec="/opt/xiaozhi-tv/xiaozhi-tv" --autostarted', text)
            self.assertIn("Type=Application", text)
            self.assertTrue(entry.is_enabled())
            entry.disable()
            self.assertFalse(entry.is_enabled())

    def test_only_installed_copies_register(self):
        self.assertFalse(autostart.supported(frozen=False, translocated=False))
        self.assertFalse(autostart.supported(frozen=True, translocated=True))
        self.assertTrue(autostart.supported(frozen=True, translocated=False))
        self.assertTrue(paths.translocated(Path("/private/var/folders/x/AppTranslocation/ABC/d/Xiaozhi TV.app"
                                                "/Contents/MacOS/XiaozhiTV")))
        self.assertTrue(paths.translocated(Path("/Volumes/Xiaozhi TV/Xiaozhi TV.app/Contents/MacOS/XiaozhiTV")))
        self.assertFalse(paths.translocated(Path("/Applications/Xiaozhi TV.app/Contents/MacOS/XiaozhiTV")))


# ----------------------------------------------------------------------------- power

class FakeBackend:
    def __init__(self, works=True):
        self.works, self.calls = works, []

    def acquire(self):
        self.calls.append("acquire")
        return self.works

    def release(self):
        self.calls.append("release")

    def close(self):
        self.calls.append("close")


class PowerTest(unittest.TestCase):
    def test_held_while_watching_released_two_minutes_after(self):
        clock, backend = Clock(), FakeBackend()
        awake = power.KeepAwake(backend, clock=clock)
        self.assertFalse(awake.update("watching", False))
        self.assertTrue(awake.update("watching", True))
        clock.now += 60
        self.assertTrue(awake.update("watching", False))
        clock.now += 61
        self.assertFalse(awake.update("watching", False))
        self.assertEqual(backend.calls, ["acquire", "release"])

    def test_always_and_never(self):
        clock, backend = Clock(), FakeBackend()
        awake = power.KeepAwake(backend, clock=clock)
        self.assertTrue(awake.update("always", False))
        self.assertFalse(awake.update("never", True))
        self.assertTrue(awake.update("bogus", True))        # unknown mode = the default
        awake.close()
        self.assertEqual(backend.calls, ["acquire", "release", "acquire", "release", "close"])

    def test_failed_acquire_is_retried(self):
        backend = FakeBackend(works=False)
        awake = power.KeepAwake(backend, clock=Clock())
        self.assertFalse(awake.update("watching", True))
        self.assertFalse(awake.update("watching", True))
        self.assertEqual(backend.calls, ["acquire", "acquire"])

    def test_windows_backend_uses_one_thread_and_never_the_display(self):
        class Kernel32:
            def __init__(self):
                self.calls = []

            def SetThreadExecutionState(self, flags):
                self.calls.append((flags, threading.get_ident()))
                return 0x80000000
        kernel32 = Kernel32()
        backend = power.WindowsBackend(kernel32=kernel32)
        self.assertTrue(backend.acquire())
        backend.release()
        backend.acquire()
        backend.close()
        flags = [f for f, _ in kernel32.calls]
        self.assertEqual(flags[0], power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED)
        self.assertEqual(flags[1], power.ES_CONTINUOUS)
        self.assertEqual(flags[-1], power.ES_CONTINUOUS)
        self.assertTrue(all(not f & power.ES_DISPLAY_REQUIRED for f in flags))
        threads = {t for _, t in kernel32.calls}
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads.pop(), threading.get_ident())

    def test_mac_backend_creates_and_releases_one_assertion(self):
        calls = []

        class CF:
            def CFStringCreateWithCString(self, alloc, text, encoding):
                calls.append(("cfstring", text.decode()))
                return len(calls)

            def CFRelease(self, ref):
                calls.append(("cfrelease", ref))

        class IOKit:
            def IOPMAssertionCreateWithName(self, kind, level, name, out):
                calls.append(("create", level))
                out._obj.value = 77
                return 0

            def IOPMAssertionRelease(self, assertion):
                calls.append(("release", assertion))
                return 0
        backend = power.MacBackend(iokit=IOKit(), corefoundation=CF())
        self.assertTrue(backend.acquire())
        self.assertTrue(backend.acquire())       # already held: no second assertion
        backend.release()
        backend.release()
        self.assertIn(("cfstring", "PreventUserIdleSystemSleep"), calls)
        self.assertEqual([c for c in calls if c[0] in ("create", "release")], [("create", 255), ("release", 77)])
        self.assertEqual(sum(1 for c in calls if c[0] == "cfrelease"), 2)

    def test_child_backend(self):
        started = []

        class Process:
            def __init__(self, argv, **kw):
                started.append(argv)
                self.alive = True

            def poll(self):
                return None if self.alive else 0

            def terminate(self):
                self.alive = False

            def wait(self, timeout=None):
                return 0
        backend = power.ChildBackend(["caffeinate", "-i", "-w", "1"], "caffeinate", popen=Process)
        self.assertTrue(backend.acquire())
        self.assertTrue(backend.acquire())
        backend.release()
        self.assertEqual(len(started), 1)
        self.assertNotIn("-d", started[0])       # never the display

    def test_default_backend_without_helpers(self):
        self.assertIsInstance(power.default_backend("linux", which=lambda name: None), power.NullBackend)
        linux = power.default_backend("linux", which=lambda name: "/usr/bin/" + name)
        self.assertIn("--what=idle:sleep", linux.argv)


# ----------------------------------------------------------------------------- network check

class NetcheckTest(unittest.TestCase):
    def test_macos(self):
        route = "   route to: default\ndestination: default\n    gateway: 192.168.1.1\n  interface: en0\n"
        arp = "? (192.168.1.1) at 0:11:2:33:44:5 on en0 ifscope [ethernet]\n"
        run = {("route", "-n", "get", "default"): route, ("arp", "-n", "192.168.1.1"): arp}
        ident = netcheck.network_identity("darwin", run=lambda argv: run.get(tuple(argv), ""))
        self.assertEqual(ident, netcheck.identity_of("00:11:02:33:44:05"))
        self.assertEqual(len(ident), 16)

    def test_windows(self):
        route = ("IPv4 Route Table\n===\nActive Routes:\nNetwork Destination        Netmask          Gateway"
                 "       Interface  Metric\n          0.0.0.0          0.0.0.0      192.168.31.1    192.168.31.20"
                 "     35\n          0.0.0.0          0.0.0.0       10.8.0.1        10.8.0.5     25\n")
        self.assertEqual(netcheck.parse_windows_route(route), "10.8.0.1")
        arp = "\nInterface: 192.168.31.20 --- 0x5\n  Internet Address      Physical Address      Type\n" \
              "  192.168.31.1          a4-39-b3-01-02-03     动态\n  192.168.31.10         a4-39-b3-01-02-04     动态\n"
        self.assertEqual(netcheck.parse_arp(arp, "192.168.31.1"), "a4:39:b3:01:02:03")
        self.assertEqual(netcheck.parse_arp(arp, "192.168.31.100"), "")

    def test_linux(self):
        route = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
                 "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n"
                 "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n")
        self.assertEqual(netcheck.parse_linux_route(route), "192.168.1.1")
        arp = ("IP address       HW type     Flags       HW address            Mask     Device\n"
               "192.168.1.1      0x1         0x2         00:11:22:33:44:55     *        eth0\n")
        self.assertEqual(netcheck.parse_linux_arp(arp, "192.168.1.1"), "00:11:22:33:44:55")

    def test_unusable_macs(self):
        for mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "01:00:5e:00:00:01", "(incomplete)", "1:2:3"):
            self.assertEqual(netcheck.normalise_mac(mac), "", mac)
        self.assertEqual(netcheck.identity_of(""), "")

    def test_windows_profiles(self):
        ps51 = '[{"Name":"家里的WiFi","InterfaceAlias":"WLAN","NetworkCategory":0},' \
               '{"Name":"vpn","InterfaceAlias":"tun","NetworkCategory":1}]'
        profiles = netcheck.parse_windows_profiles(ps51)
        self.assertEqual(netcheck.public_profile_name(profiles), "家里的WiFi")
        ps7 = '{"Name":"Home","InterfaceAlias":"Wi-Fi","NetworkCategory":"Private"}'
        self.assertEqual(netcheck.public_profile_name(netcheck.parse_windows_profiles(ps7)), "")
        self.assertEqual(netcheck.parse_windows_profiles("not json"), [])

    def test_lan_decision(self):
        self.assertEqual(netcheck.lan_decision([], "abc"), "ask")
        self.assertEqual(netcheck.lan_decision(["abc"], "abc"), "open")
        self.assertEqual(netcheck.lan_decision(["abc"], "cafe"), "closed")
        self.assertEqual(netcheck.lan_decision(["abc"], ""), "closed")
        self.assertEqual(netcheck.lan_decision([], "cafe", ["cafe"]), "closed", "said: not my home network")
        self.assertEqual(netcheck.lan_decision(["abc"], "abc", ["cafe"]), "open")
        self.assertEqual(netcheck.profile_category([{"category": "private"}, {"category": "public"}]), "public")
        self.assertEqual(netcheck.profile_category([{"category": "domain"}]), "domain")
        self.assertEqual(netcheck.profile_category([]), "")


# ----------------------------------------------------------------------------- paths

class PathsTest(unittest.TestCase):
    def test_data_and_logs_per_os(self):
        home = Path("/home/u")
        self.assertEqual(paths.data_dir({}, home, "Darwin"), home / "Library/Application Support/XiaozhiTV")
        self.assertEqual(paths.logs_dir(Path("/d"), {}, home, "Darwin"), home / "Library/Logs/XiaozhiTV")
        win = paths.data_dir({"LOCALAPPDATA": r"C:\Users\张三\AppData\Local", "APPDATA": r"C:\roaming"}, home,
                             "Windows")
        self.assertEqual(win, Path(r"C:\Users\张三\AppData\Local") / "XiaozhiTV")
        self.assertEqual(paths.logs_dir(win, {}, home, "Windows"), win / "logs")
        self.assertEqual(paths.data_dir({}, home, "Linux"), home / ".local/share/xiaozhi-tv")
        self.assertEqual(paths.data_dir({"XDG_DATA_HOME": "/x"}, home, "Linux"), Path("/x/xiaozhi-tv"))
        self.assertEqual(paths.logs_dir(Path("/d"), {}, home, "Linux"), home / ".local/state/xiaozhi-tv/logs")
        # An explicit folder keeps everything inside it.
        self.assertEqual(paths.data_dir({paths.DATA_ENV: "/t/d"}, home, "Darwin"), Path("/t/d"))
        self.assertEqual(paths.logs_dir(Path("/t/d"), {paths.DATA_ENV: "/t/d"}, home, "Darwin"), Path("/t/d/logs"))

    def test_bundled_core(self):
        # resolve(): on Windows "/Applications" becomes "C:\\Applications"; the layout is what matters.
        app = Path("/Applications/Xiaozhi TV.app").resolve()
        mac = paths.bundled_core(app / "Contents" / "MacOS" / "XiaozhiTV", "Darwin")
        self.assertEqual(mac, app / "Contents" / "Resources" / "core" / "xiaozhi-tv-core")
        opt = Path("/opt/xiaozhi-tv").resolve()
        self.assertEqual(paths.bundled_core(opt / "xiaozhi-tv", "Linux"), opt / "core" / "xiaozhi-tv-core")
        self.assertEqual(paths.bundled_core(opt / "XiaozhiTV.exe", "Windows"), opt / "core" / "xiaozhi-tv-core.exe")

    def test_core_command(self):
        data = Path("/d")
        # The default folder: the core finds the same one itself (and keeps its logs per OS).
        self.assertEqual(paths.core_command(data, env={})[-3:], ["-m", "server.selfhost", "--desktop"])
        explicit = {paths.DATA_ENV: str(data)}
        self.assertEqual(paths.core_command(data, env=explicit)[-5:],
                         ["-m", "server.selfhost", "--desktop", "--data", str(data)])
        self.assertEqual(paths.core_command(data, env=dict(explicit, **{paths.CORE_ENV: "my-core --flag"})),
                         ["my-core", "--flag", "selfhost", "--desktop", "--data", str(data)])


# ----------------------------------------------------------------------------- core process

class CoreProcessTest(unittest.TestCase):
    def test_restart_with_backoff_and_stop(self):
        clock = Clock()
        logs = []
        core = CoreProcess([sys.executable, "-c", "print('hello, setup#SECRETCODE'); raise SystemExit(4)"],
                           log=logs.append, attach=lambda p: None, clock=clock)
        core.start()
        core.process.wait(timeout=30)
        self.assertEqual(core.watch(), "exited")
        self.assertEqual(core.state, "restarting")
        self.assertEqual(core.last_exit, 4)
        self.assertIsNone(core.watch())                 # not yet time
        clock.now += core.delay / 2 + 0.01              # delay already doubled for next time
        self.assertEqual(core.watch(), "restarted")
        self.assertEqual(core.restarts, 1)
        core.process.wait(timeout=30)
        core.stop()
        self.assertEqual(core.state, "stopped")
        self.assertFalse(any("SECRETCODE" in line for line in list(core.tail) + logs))

    def test_another_core_on_the_folder_is_not_a_crash_loop(self):
        clock = Clock()
        core = CoreProcess([sys.executable, "-c", "raise SystemExit(3)"], log=lambda m: None,
                           attach=lambda p: None, clock=clock)
        core.start()
        core.process.wait(timeout=30)
        self.assertEqual(core.watch(), "exited")
        clock.now += 30
        self.assertIsNone(core.watch())                 # waits the long delay, not 2 s
        core.stop()

    def test_stop_terminates_a_core_that_does_not_listen(self):
        core = CoreProcess([sys.executable, "-c", "import time; time.sleep(60)"], log=lambda m: None,
                           attach=lambda p: None)
        core.start()
        self.assertTrue(core.running)
        core.stop(client=None, wait_s=5)
        self.assertFalse(core.running)
        self.assertIsNone(core.watch())                 # a wanted stop is not a crash

    def test_redact(self):
        self.assertEqual(redact("open http://localhost:8080/tv/setup#K7P4QX2M9A now"),
                         "open http://localhost:8080/tv/setup#… now")
        self.assertEqual(redact("设置码：K7P4Q-X2M9A （只能用一次）"), "设置码：… （只能用一次）")
        self.assertEqual(redact("GET /TV/P/abcDEF123 token=xyz"), "GET /TV/P/… token=…")


# ----------------------------------------------------------------------------- the app

class FakeCore:
    def __init__(self):
        self.running, self.state, self.calls, self.last_exit = True, "running", [], None
        self.tail = collections.deque(["core line"], maxlen=10)

    def start(self):
        self.calls.append("start")

    def watch(self):
        return None

    def stop(self, client=None):
        self.calls.append("stop")
        if client is not None:
            client.stop()

    def restart(self, client=None):
        self.calls.append("restart")


class FakeLoginItem:
    def __init__(self, enabled=False):
        self.enabled = enabled

    def is_enabled(self):
        return self.enabled

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.opened, self.revealed = [], []
        self.backend = FakeBackend()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, core: DevCore, autostarted=False, login_item=None, **kw) -> App:
        self.devcore = core.start()
        self.addCleanup(core.stop)
        return App(self.data, ControlClient(paths.control_file(self.data), timeout=3), FakeCore(),
                   power.KeepAwake(self.backend, clock=Clock()), login_item=login_item,
                   opener=self.opened.append, path_opener=self.opened.append, revealer=self.revealed.append,
                   autostarted=autostarted, logs_dir=self.data / "logs", **kw)

    def test_first_run_opens_setup_once(self):
        app = self.make(DevCore(self.data, setup=True))
        app.start()
        app.tick()
        self.assertEqual(self.opened, ["http://localhost:8080/tv/setup#DEVC0DE123"])
        self.assertIn("第一次设置", menu.headline(app.state))

    def test_no_setup_tab_at_login(self):
        app = self.make(DevCore(self.data, setup=True), autostarted=True)
        app.start()
        app.tick()
        self.assertEqual(self.opened, [])
        app.run_action(menu.OPEN_SETUP)
        self.assertEqual(self.opened, ["http://localhost:8080/tv/setup#DEVC0DE123"])

    def test_watching_holds_the_assertion(self):
        app = self.make(DevCore(self.data, devices=2))
        app.start()
        self.assertTrue(app.state.awake_held)
        self.assertEqual(menu.headline(app.state), "小智电视 · 正在为 2 台小电视推流")
        self.devcore.devices.clear()
        app.tick()          # still within the two minutes
        self.assertTrue(app.state.awake_held)
        app.shutdown()
        self.assertIn("release", self.backend.calls)

    def test_a_listening_phone_holds_the_assertion(self):
        core = DevCore(self.data)
        app = self.make(core)
        app.start()
        self.assertFalse(app.state.awake_held)
        original = core.status
        core.status = lambda: dict(original(), watching=0, listening=1)
        app.tick()
        self.assertTrue(app.state.awake_held, "a phone listening keeps the computer awake (design 5.10)")

    def test_core_managed_power_releases_ours(self):
        core = DevCore(self.data, devices=1)
        app = self.make(core)
        app.start()
        self.assertTrue(app.state.awake_held)
        original = core.status
        core.status = lambda: dict(original(), power={"managed_by": "core"})
        app.tick()
        self.assertFalse(app.state.awake_held)

    def test_keep_awake_toggle_goes_to_the_core(self):
        app = self.make(DevCore(self.data))
        app.start()
        self.assertFalse(app.state.awake_held)
        app.run_action(menu.KEEP_AWAKE)
        self.assertEqual(self.devcore.settings["keep_awake"], "always")
        self.assertTrue(app.state.awake_held)
        app.tick()
        self.assertEqual(app.state.keep_awake, "always")
        app.run_action(menu.KEEP_AWAKE)
        self.assertEqual(self.devcore.settings["keep_awake"], "watching")
        self.assertEqual(Prefs(paths.prefs_file(self.data)).get("keep_awake"), "watching")

    def test_login_item_registered_once_then_respected(self):
        item = FakeLoginItem()
        app = self.make(DevCore(self.data), login_item=item)
        app.start()
        self.assertTrue(item.enabled)
        self.assertTrue(app.state.autostart)
        app.run_action(menu.AUTOSTART)
        self.assertFalse(item.enabled)
        self.assertIs(self.devcore.settings["autostart"], False)
        # Next run: switched off stays off.
        again = App(self.data, ControlClient(paths.control_file(self.data)), FakeCore(),
                    power.KeepAwake(FakeBackend()), login_item=item, opener=self.opened.append)
        again._initialise_login_item()
        self.assertFalse(item.enabled)

    def test_console_phone_tv_and_network(self):
        app = self.make(DevCore(self.data, network=True))
        app.start()
        app.run_action(menu.OPEN_CONSOLE)
        self.assertTrue(self.opened[-1].startswith("http://localhost:8080/tv/local-login?t="))
        app.run_action(menu.CONNECT_PHONE)
        self.assertTrue(self.opened[-1].startswith("file:"))
        page = (self.data / "run" / "phone-qr.html").read_text(encoding="utf-8")
        self.assertIn("/TV/P/", page)
        app.run_action(menu.SET_UP_TV)
        self.assertEqual(self.opened[-1], "http://localhost:8080/flash/")
        self.assertIn(menu.TRUST_NETWORK, menu.actions_in(menu.build(app.state)))
        app.run_action(menu.TRUST_NETWORK)
        app.tick()
        self.assertNotIn(menu.TRUST_NETWORK, menu.actions_in(menu.build(app.state)))
        app.shutdown()
        self.assertFalse((self.data / "run" / "phone-qr.html").exists())

    def test_diagnostics_from_the_core_or_the_tray(self):
        app = self.make(DevCore(self.data))
        app.start()
        app.run_action(menu.DIAGNOSTICS)
        self.assertEqual(Path(self.revealed[-1]).name, "diagnostics-dev.zip")
        self.devcore.stop()
        app.run_action(menu.DIAGNOSTICS)
        made = Path(self.revealed[-1])
        self.addCleanup(lambda: made.unlink(missing_ok=True))
        self.assertTrue(made.name.startswith("xiaozhi-tv-diagnostics-"))
        import zipfile
        with zipfile.ZipFile(made) as archive:
            self.assertIn("versions.json", archive.namelist())
            self.assertIn("status.json", archive.namelist())
        self.assertEqual(app.state.busy, "")

    def test_phone_before_setup_shows_the_cores_message(self):
        app = self.make(DevCore(self.data, setup=True), autostarted=True)
        app.start()
        app.run_action(menu.CONNECT_PHONE)
        self.assertIn("请先完成第一次设置", app.state.notices)

    def test_restart_and_ffmpeg_go_through_the_api(self):
        app = self.make(DevCore(self.data, ffmpeg="failed"))
        app.start()
        self.assertIn(menu.FETCH_FFMPEG, menu.actions_in(menu.build(app.state)))
        self.assertEqual(menu.icon_kind(app.state), "problem")
        app.run_action(menu.FETCH_FFMPEG)
        app.tick()
        self.assertEqual(app.state.status.ffmpeg.state, "ok")
        app.run_action(menu.RESTART)
        self.assertIn(("POST", "/local/v1/restart", {}), self.devcore.calls)
        self.assertNotIn("restart", app.core.calls)     # the process stays, its children restart
        self.devcore.stop()
        app.run_action(menu.RESTART)
        self.assertIn("restart", app.core.calls)        # no API: restart the process

    def test_an_existing_core_is_used(self):
        # Our core exited with 3 (another core holds run/core.lock); that one answers.
        app = self.make(DevCore(self.data))
        app.core.running, app.core.state, app.core.last_exit = False, "restarting", 3
        app.start()
        self.assertEqual(app.state.core, "running")

    def test_open_logs_prefers_the_cores_folder(self):
        app = self.make(DevCore(self.data))
        app.start()
        app.run_action(menu.OPEN_LOGS)
        self.assertEqual(self.opened[-1], self.data / "logs")

    def test_unanswered_action_becomes_a_notice(self):
        app = self.make(DevCore(self.data))
        app.start()
        self.devcore.stop()
        app.run_action(menu.OPEN_CONSOLE)
        self.assertTrue(any("没有回应" in n for n in app.state.notices))

    def test_windows_public_network_warning(self):
        app = self.make(DevCore(self.data), windows_profiles=lambda: [{"name": "家里的WiFi", "category": "public"}])
        app.start()
        self.assertEqual(app.state.windows_public, "家里的WiFi")
        self.assertIn(menu.NETWORK_HELP, menu.actions_in(menu.build(app.state)))

    def test_no_own_windows_check_when_the_core_reports_the_profile(self):
        asked = []
        core = DevCore(self.data, network=True)
        core.network["profile"] = "private"
        app = self.make(core, windows_profiles=lambda: asked.append(1) or [{"name": "x", "category": "public"}])
        app.start()
        app.tick()
        self.assertEqual(asked, [])
        self.assertEqual(app.state.windows_public, "")

    def test_quit_stops_the_core_through_the_api(self):
        app = self.make(DevCore(self.data))
        quit_called = []
        app.on_quit = lambda: quit_called.append(True)
        app.start()
        app.run_action(menu.QUIT)
        self.assertEqual(quit_called, [True])
        self.assertIn("stop", app.core.calls)
        self.assertTrue(self.devcore.stopped.wait(5))

    def test_phone_page_escapes_the_link(self):
        self.assertIn("&lt;x&gt;", phone_page("http://h/TV/P/<x>", ""))


if __name__ == "__main__":
    unittest.main()
