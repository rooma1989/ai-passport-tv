"""The tray program: `XiaozhiTV` (installed) or `python3 -m desktop` (checkout).

    XiaozhiTV [--data DIR] [--no-tray] [--autostarted]
    XiaozhiTV --smoke-test [--timeout S]     CI: headless start, check the control API, stop

What it does (design 5.1-5.4, 5.10, 5.11):

1. take the single-instance lock in <data>/run/; if another tray holds it, open
   that one's console and exit;
2. start the core and keep it running (restart with back-off when it dies);
3. every two seconds read `/local/v1/status` and redraw the menu, tooltip and
   icon; on the first run open the browser at the setup link the core reports;
4. hold the idle-sleep assertion while a device watches or a phone listens (until the core says it
   does so itself); keep the login item; warn about a Windows network marked
   Public; ask once whether this is the home network;
5. on Quit: stop the core through the control API, release everything.

`App` holds all of that without any GUI toolkit; `PystrayUI` and `HeadlessUI`
only draw it. Menu callbacks run actions on a worker thread so a slow control
API call never freezes the menu bar.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from pathlib import Path

from tools import netcheck

from . import APP_ID, APP_NAME, __version__, autostart, menu, paths, power
from .control import ControlClient, ControlError, ControlUnavailable, Status
from .core import CoreProcess, no_window_flags
from .instance import SingleInstance, hand_off
from .logs import log, setup as setup_logs

POLL_S = 2.0
NETWORK_CHECK_S = 60.0
NOTICE_S = 60.0
# Exit codes: 0 ok, 1 failure, 2 usage. A second instance that handed off exits 0.
EXIT_FAILED = 1


def open_url(url: str) -> None:
    webbrowser.open(url, new=2)


def open_path(path: Path) -> None:
    path = Path(path)
    if sys.platform == "win32":
        os.startfile(str(path))     # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal(path: Path) -> None:
    path = Path(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"], creationflags=no_window_flags())
    else:
        open_path(path.parent)


class Prefs:
    """<data>/tray.json: what the tray needs to remember while the core does not
    keep it ([desktop] in config.toml is the long-term home, design 5.6)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as error:
            log(f"保存托盘设置失败：{type(error).__name__}")


class App:
    def __init__(self, data: Path, client: ControlClient, core: CoreProcess, keep_awake: power.KeepAwake,
                 login_item=None, prefs: Prefs | None = None, opener=open_url, path_opener=open_path,
                 revealer=reveal, autostarted: bool = False, translocated: bool = False,
                 windows_profiles=None, logs_dir: Path | None = None, clock=time.monotonic):
        self.data = Path(data)
        self.client = client
        self.core = core
        self.keep_awake = keep_awake
        self.login_item = login_item            # an autostart entry, or None where unsupported
        self.prefs = prefs or Prefs(paths.prefs_file(self.data))
        self.open_url = opener
        self.open_path = path_opener
        self.reveal = revealer
        self.autostarted = autostarted
        self.windows_profiles = windows_profiles  # callable -> list[dict], Windows only
        self.logs_dir = Path(logs_dir) if logs_dir else paths.logs_dir(self.data)
        self.clock = clock
        self.state = menu.TrayState(translocated=translocated,
                                    keep_awake=self.prefs.get("keep_awake", "watching"))
        self.last_status_raw: dict | None = None
        self.setup_opened = False
        self.quitting = threading.Event()
        self._notices: list[tuple[float, str]] = []
        self._next_network_check = 0.0
        self._lock = threading.RLock()
        self.on_change = None                   # set by the UI: redraw
        self.on_quit = None                     # set by the UI: leave its loop

    # ------------------------------------------------------------ the poll

    def start(self) -> None:
        self._initialise_login_item()
        self.core.start()
        self.tick()

    def tick(self) -> menu.TrayState:
        """One poll: watch the core, read its status, apply policies, return the new state."""
        if self.quitting.is_set():
            return self.state
        event = self.core.watch()
        if event == "exited" and self.core.last_exit != 3:
            self._notice("电视服务意外退出了，正在重新启动")
        # Asked even when our own child is not running: a core that exited with
        # 3 found another core on this data folder, and that one is answering.
        status = None
        try:
            raw = self.client.request("GET", "/status")
            status = Status.from_json(raw)
            self.last_status_raw = raw
        except (ControlUnavailable, ControlError):
            status = None
        if status is not None:
            core_state = "running"
        elif self.core.running:
            core_state = "starting"
        else:
            core_state = self.core.state
        with self._lock:
            keep_awake = status.keep_awake if status is not None and status.keep_awake in power.KeepAwake.MODES \
                else self.state.keep_awake
            self.state = replace(self.state, core=core_state, status=status, keep_awake=keep_awake,
                                 autostart=self._login_item_enabled(), notices=self._current_notices())
        self._first_run(status)
        self._power(status)
        self._network()
        self._changed()
        return self.state

    def _first_run(self, status: Status | None) -> None:
        # Opened once per run of the tray, and never at login: someone who has
        # not finished setting up gets the menu item instead of a tab every boot.
        # The setup link (with the one-time code in its fragment) comes from
        # console-link and is never written to the log.
        if status is None or not status.setup_pending or self.setup_opened or self.autostarted:
            return
        try:
            url = self.client.console_link()
        except (ControlUnavailable, ControlError):
            return      # the web front is not up yet (503 not_ready): next poll
        self.setup_opened = True
        log("第一次运行：在浏览器里打开设置页")
        self._open(url)

    def _power(self, status: Status | None) -> None:
        if status is not None and status.power_held_by_core:
            held = self.keep_awake.update("never", False)
        else:
            active = status is not None and (status.watching > 0 or status.listening > 0)
            held = self.keep_awake.update(self.state.keep_awake, active)
        if held != self.state.awake_held:
            log("阻止电脑闲置睡眠" if held else "不再阻止电脑睡眠")
            with self._lock:
                self.state = replace(self.state, awake_held=held)

    def _network(self) -> None:
        if self.windows_profiles is None or self.clock() < self._next_network_check:
            return
        status = self.state.status
        if status is not None and status.network is not None and status.network.profile:
            # The core checks the Windows network category itself (problem
            # network_public); one PowerShell a minute is enough.
            if self.state.windows_public:
                with self._lock:
                    self.state = replace(self.state, windows_public="")
            return
        self._next_network_check = self.clock() + NETWORK_CHECK_S
        try:
            name = netcheck.public_profile_name(self.windows_profiles())
        except Exception:       # noqa: BLE001 - a hint, never a failure
            name = ""
        if name != self.state.windows_public:
            if name:
                log("家里的网络被标成了公用网络（Windows）")
            with self._lock:
                self.state = replace(self.state, windows_public=name)

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self.state)
            except Exception as error:      # noqa: BLE001 - drawing must not stop the poll
                log(f"刷新托盘失败：{type(error).__name__}: {error}")

    def _notice(self, text: str) -> None:
        self._notices.append((self.clock() + NOTICE_S, text))
        log(text)

    def _current_notices(self) -> tuple[str, ...]:
        now = self.clock()
        self._notices = [(until, text) for until, text in self._notices if until > now]
        return tuple(text for _, text in self._notices[-2:])

    def _open(self, url: str) -> None:
        try:
            self.open_url(url)
        except Exception as error:      # noqa: BLE001
            log(f"打不开浏览器：{type(error).__name__}")

    # ------------------------------------------------------------ login item

    def _login_item_enabled(self) -> bool | None:
        if self.login_item is None:
            return None
        try:
            return self.login_item.is_enabled()
        except OSError:
            return None

    def _initialise_login_item(self) -> None:
        """On by default (Q8): registered once, on the first run of an installed copy.
        Afterwards the OS entry is the truth, so a switch-off in Task Manager or
        Login Items is respected."""
        if self.login_item is None or self.prefs.get("autostart_initialised"):
            return
        wanted = self.prefs.get("autostart", True)
        try:
            if wanted:
                self.login_item.enable()
            else:
                self.login_item.disable()
            self.prefs.set("autostart_initialised", True)
            log("已设置登录时启动" if wanted else "登录时不启动")
        except OSError as error:
            log(f"设置登录时启动失败：{type(error).__name__}")

    # ------------------------------------------------------------ actions

    ACTIONS = {
        menu.OPEN_CONSOLE: "open_console",
        menu.OPEN_SETUP: "open_setup",
        menu.CONNECT_PHONE: "connect_phone",
        menu.SET_UP_TV: "set_up_tv",
        menu.FETCH_FFMPEG: "fetch_ffmpeg",
        menu.TRUST_NETWORK: "trust_network",
        menu.NETWORK_HELP: "network_help",
        menu.KEEP_AWAKE: "toggle_keep_awake",
        menu.AUTOSTART: "toggle_autostart",
        menu.OPEN_LOGS: "open_logs",
        menu.DIAGNOSTICS: "diagnostics",
        menu.RESTART: "restart",
        menu.QUIT: "quit",
    }

    def run_action(self, action: str) -> None:
        name = self.ACTIONS.get(action)
        if name is None:
            return
        try:
            getattr(self, name)()
        except ControlError as error:
            self._notice(error.message or f"电视服务拒绝了这个操作（{error}）")
        except ControlUnavailable:
            self._notice("电视服务没有回应，请稍后再试")
        except Exception as error:      # noqa: BLE001 - a menu click must never kill the tray
            log(f"{action} 出错：{type(error).__name__}: {error}")
            self._notice("出错了，详情见日志")
        self._refresh()

    def _refresh(self) -> None:
        with self._lock:
            self.state = replace(self.state, notices=self._current_notices(), autostart=self._login_item_enabled())
        self._changed()

    def open_console(self) -> None:
        self._open(self.client.console_link())

    def open_setup(self) -> None:
        # console-link answers with the setup page while there is no admin.
        self._open(self.client.console_link())

    def connect_phone(self) -> None:
        answer = self.client.phone_qr()
        page = answer.get("page")
        if isinstance(page, str) and page.startswith(("http://", "https://")):
            self._open(page)
            return
        link = answer.get("url") or answer.get("link")
        if not isinstance(link, str) or not link.startswith("http"):
            raise ControlError(200, "phone-qr: no link")
        target = paths.run_dir(self.data) / "phone-qr.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(phone_page(link, answer.get("svg") if isinstance(answer.get("svg"), str) else ""),
                          encoding="utf-8")
        self._open(target.resolve().as_uri())

    def set_up_tv(self) -> None:
        status = self.state.status
        base = (status.console_base if status else "") or "http://localhost:8080"
        # localhost, not the LAN address: Web Serial needs a secure context.
        self._open(base + "/flash/")

    def fetch_ffmpeg(self) -> None:
        self.client.fetch_ffmpeg()
        self._notice("正在重新下载视频组件…")

    def trust_network(self) -> None:
        network_id = menu.network_trust_needed(self.state)
        if network_id:
            self.client.trust_network(network_id, True)
            self._notice("已记住这是你家的网络")

    def network_help(self) -> None:
        # Settings > Network & Internet > (the connection) > Network profile type.
        self._open("ms-settings:network-status" if sys.platform == "win32" else "https://support.microsoft.com/")

    def toggle_keep_awake(self) -> None:
        new = "watching" if self.state.keep_awake == "always" else "always"
        try:
            self.client.set_power(new)
        except ControlError as error:
            if error.status != 404:
                raise
        except ControlUnavailable:
            pass
        self.prefs.set("keep_awake", new)
        with self._lock:
            self.state = replace(self.state, keep_awake=new)
        self._power(self.state.status)
        self._notice("小智电视运行时电脑不会闲置睡眠（合上笔记本盖子仍会睡）" if new == "always"
                     else "只在有小电视在看时阻止电脑睡眠")

    def toggle_autostart(self) -> None:
        if self.login_item is None:
            return
        new = not self.login_item.is_enabled()
        if new:
            self.login_item.enable()
        else:
            self.login_item.disable()
        self.prefs.set("autostart", new)
        self.prefs.set("autostart_initialised", True)
        try:
            self.client.set_autostart(new)
        except (ControlError, ControlUnavailable):
            pass        # the OS entry is what counts; the core only mirrors it in config.toml
        log("登录时启动：开" if new else "登录时启动：关")

    def open_logs(self) -> None:
        # The core's logs folder (core.log) when it says where; tray.log is there too
        # on every OS (both follow design 5.5).
        status = self.state.status
        folder = Path(status.logs_dir) if status and status.logs_dir else self.logs_dir
        folder.mkdir(parents=True, exist_ok=True)
        self.open_path(folder)

    def diagnostics(self) -> None:
        with self._lock:
            self.state = replace(self.state, busy="正在导出诊断信息…")
        self._changed()
        try:
            try:
                answer = self.client.diagnostics()
                made = Path(answer["path"]) if isinstance(answer.get("path"), str) else None
            except (ControlError, ControlUnavailable, KeyError):
                made = None
            if made is None or not made.exists():
                from . import diagnostics
                made = diagnostics.export(self.logs_dir, self.last_status_raw, list(self.core.tail))
            self._notice(f"诊断信息已保存：{made.name}")
            self.reveal(made)
        finally:
            with self._lock:
                self.state = replace(self.state, busy="")

    def restart(self) -> None:
        self._notice("正在重启电视服务…")
        try:
            self.client.restart()       # the core restarts its children, keeps its API
        except (ControlUnavailable, ControlError):
            self.core.restart(self.client)

    def quit(self) -> None:
        self.shutdown()
        if self.on_quit is not None:
            self.on_quit()

    def shutdown(self) -> None:
        if self.quitting.is_set():
            return
        self.quitting.set()
        log("正在退出…")
        self.keep_awake.close()
        self.core.stop(self.client)
        try:
            (paths.run_dir(self.data) / "phone-qr.html").unlink()
        except OSError:
            pass


def phone_page(link: str, svg: str) -> str:
    """A local page with the phone sign-in QR (the link is valid for minutes and once)."""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>连接手机 · {APP_NAME}</title>
<style>body{{margin:0;background:#1c1915;color:#e6dcc8;font:16px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:520px;margin:40px auto;padding:0 16px;text-align:center}}h1{{font-weight:700;letter-spacing:3px}}
.qr{{display:inline-block;background:#efe5d0;padding:12px;margin:12px 0}}.qr svg{{width:280px;height:280px}}
code{{word-break:break-all;color:#ffb547}}</style></head>
<body><main><h1>用手机扫一扫</h1>
<p>手机要连着和这台电脑同一个 Wi-Fi。用手机自带的相机扫下面的码，打开后点「在这台手机上登录」，再按提示把遥控加到主屏幕。</p>
<div class="qr">{svg}</div>
<p>扫不了时，在手机浏览器里打开：<br><code>{html.escape(link)}</code></p>
<p>这个码几分钟内有效、只能用一次；过期了就在托盘菜单里再点一次「连接手机…」。</p>
</main></body></html>
"""


# ------------------------------------------------------------------ user interfaces

class HeadlessUI:
    """No tray (Linux without one, `--no-tray`, CI): print the headline when it changes."""

    def __init__(self, app: App):
        self.app = app
        browser = app.open_url

        def say_and_open(url: str) -> None:
            # Printed (not logged: the setup link carries the one-time code), for a
            # machine without a desktop browser -- open it from another computer.
            if sys.stdout is not None:
                print(f"在浏览器里打开：{url}", flush=True)
            try:
                browser(url)
            except Exception:       # noqa: BLE001
                pass
        app.open_url = say_and_open
        self._last = ""
        self._stop = threading.Event()
        app.on_change = self._draw
        app.on_quit = self._stop.set

    def _draw(self, state: menu.TrayState) -> None:
        lines = [menu.headline(state)] + menu.problems(state) + list(state.notices)
        text = " | ".join(lines)
        if text != self._last:
            self._last = text
            log(text)

    def run(self) -> int:
        def stop(*_):
            self._stop.set()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, stop)
            except ValueError:
                pass
        self.app.start()
        while not self._stop.wait(POLL_S):
            self.app.tick()
        self.app.shutdown()
        return 0


class PystrayUI:
    """The real tray icon. pystray runs the toolkit's loop on the main thread; the
    poll runs on its own thread; on macOS every change to the status item is
    sent to the main thread (AppKit objects are main-thread only)."""

    def __init__(self, app: App):
        import pystray      # noqa: F401 - fail here, before anything starts, if missing
        self.app = app
        self.icon = None
        self._drawn = None
        self._images = {}
        self._mac = sys.platform == "darwin"
        app.on_change = self._draw
        app.on_quit = self._stop

    def _image(self, kind: str):
        if kind not in self._images:
            from .icons import draw
            self._images[kind] = draw(kind, template=self._mac)
        return self._images[kind]

    def _menu(self, items):
        import pystray

        def on_click(action: str):
            return lambda icon, _item: self._click(action)

        def is_checked(value: bool):
            return lambda _item: value

        def convert(item: menu.Item):
            if item is menu.SEPARATOR or item.text == "-":
                return pystray.Menu.SEPARATOR
            if item.submenu is not None:
                return pystray.MenuItem(item.text, pystray.Menu(*[convert(i) for i in item.submenu]),
                                        enabled=item.enabled)
            # pystray inspects the callables' argument counts (0-2 for an
            # action, exactly 1 for `checked`), so no default-argument tricks.
            return pystray.MenuItem(item.text, on_click(item.action) if item.action else None,
                                    checked=is_checked(item.checked) if item.checked is not None else None,
                                    enabled=item.enabled, default=item.default)
        return pystray.Menu(*[convert(i) for i in items])

    def _click(self, action: str) -> None:
        threading.Thread(target=self.app.run_action, args=(action,), daemon=True, name=f"action-{action}").start()

    def _on_main(self, fn) -> None:
        if self._mac:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(fn)
        else:
            fn()

    def _draw(self, state: menu.TrayState) -> None:
        items = menu.build(state)
        kind = menu.icon_kind(state)
        title = menu.tooltip(state)
        drawn = (items, kind, title)
        if drawn == self._drawn or self.icon is None:
            return
        self._drawn = drawn

        def apply():
            icon = self.icon
            if icon is None:
                return
            icon.icon = self._image(kind)
            icon.title = title
            icon.menu = self._menu(items)
            if self._mac:
                self._template()
        self._on_main(apply)

    def _template(self) -> None:
        try:
            self.icon._status_item.button().image().setTemplate_(True)     # noqa: SLF001
        except Exception:       # noqa: BLE001 - cosmetic
            pass

    def _poll(self) -> None:
        try:
            self.app.start()
        except Exception as error:      # noqa: BLE001
            log(f"启动电视服务失败：{type(error).__name__}: {error}")
        while not self.app.quitting.wait(POLL_S):
            self.app.tick()

    def _setup(self, icon) -> None:
        icon.visible = True
        if self._mac:
            self._template()
        threading.Thread(target=self._poll, daemon=True, name="poll").start()

    def _stop(self) -> None:
        if self.icon is not None:
            self._on_main(self.icon.stop)

    def run(self) -> int:
        import pystray
        if self._mac:
            # No Dock icon even from a checkout (the .app has LSUIElement).
            try:
                import AppKit
                AppKit.NSApplication.sharedApplication().setActivationPolicy_(
                    AppKit.NSApplicationActivationPolicyAccessory)
            except Exception:       # noqa: BLE001
                pass
        state = self.app.state
        self.icon = pystray.Icon(APP_ID, self._image("idle"), menu.tooltip(state), self._menu(menu.build(state)))
        try:
            self.icon.run(setup=self._setup)
        finally:
            self.app.shutdown()
        return 0


# ------------------------------------------------------------------ entry point

def tray_available() -> bool:
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except Exception:       # noqa: BLE001 - any import problem means: no tray
        return False
    return True


def build_app(args, data: Path, logs_dir: Path, opener=open_url) -> App:
    control = ControlClient(paths.control_file(data))
    if args.dev_core:
        command = [sys.executable, "-m", "desktop.devcore", "--data", str(data)] + (
            ["--setup"] if os.environ.get("DEVCORE_SETUP") == "1" else [])
    else:
        def command():
            return paths.core_command(data)
    env = None
    if not paths.is_frozen():
        # From a checkout `-m server.selfhost` / `-m desktop.devcore` need the
        # repository on the path whatever folder the tray was started from.
        root = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONPATH=root + (os.pathsep + os.environ["PYTHONPATH"]
                                                  if os.environ.get("PYTHONPATH") else ""))
    core = CoreProcess(command, env=env, log=log)
    # A check run (CI, --tray-check) changes nothing on the machine: no sleep
    # assertion, no login item, no PowerShell.
    check = args.smoke_test or bool(args.tray_check)
    keep_awake = power.KeepAwake(power.NullBackend() if check else power.default_backend())
    translocated = sys.platform == "darwin" and paths.is_frozen() and paths.translocated()
    login_item = None
    if autostart.supported(paths.is_frozen(), translocated) and not check:
        login_item = autostart.for_this_os(paths.program_path())
    windows_profiles = netcheck.windows_profiles if sys.platform == "win32" and not check else None
    return App(data, control, core, keep_awake, login_item=login_item, opener=opener,
               autostarted=args.autostarted, translocated=translocated, windows_profiles=windows_profiles,
               logs_dir=logs_dir)


def smoke_test(app: App, timeout: float, data: Path) -> int:
    """CI: start the core headless, check the control API answers, hand-off works, stop."""
    # log() writes tray.log and, when there is a console, stdout. A windowed
    # Windows build has no stdout, so CI prints tray.log after the run; the
    # "::error::" lines then become annotations there.
    def fail(message: str) -> int:
        log(f"::error::smoke test: {message}")
        for line in list(app.core.tail)[-20:]:
            log(f"  core: {line}")
        app.shutdown()
        return EXIT_FAILED

    app.start()
    deadline = time.monotonic() + timeout
    while app.state.status is None:
        if time.monotonic() > deadline:
            return fail(f"no status from the control API within {timeout:.0f}s (core: {app.core.state})")
        time.sleep(1.0)
        app.tick()
    log(f"smoke test: status ok -- {menu.headline(app.state)}")
    items = menu.build(app.state)
    if not {menu.OPEN_CONSOLE, menu.QUIT, menu.RESTART} <= menu.actions_in(items):
        return fail("menu incomplete")
    # The core answers 503 not_ready until its web front is up: ask until the deadline.
    while True:
        try:
            link = app.client.console_link()
            break
        except (ControlError, ControlUnavailable) as error:
            if time.monotonic() > deadline:
                return fail(f"console-link: {error}")
            time.sleep(1.0)
    if not link.startswith("http://"):
        return fail("console-link is not a local http URL")
    log("smoke test: console-link ok")
    # A second launch must hand off to this one and exit without starting a core.
    second = [sys.executable] if paths.is_frozen() else [sys.executable, "-m", "desktop"]
    second += ["--data", str(data), "--smoke-second"]
    marker = paths.run_dir(data) / "handoff-ok"
    try:
        marker.unlink()
    except OSError:
        pass
    try:
        result = subprocess.run(second, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=60, creationflags=no_window_flags())
    except subprocess.TimeoutExpired:
        return fail("second instance did not exit within 60s")
    if result.returncode != 0 or not marker.exists():
        return fail(f"second instance: exit {result.returncode}, hand-off marker "
                    f"{'present' if marker.exists() else 'missing'}")
    log("smoke test: single instance ok")
    app.shutdown()
    if app.core.running:
        return fail("core still running after stop")
    log("smoke test: PASS")
    return 0


def tray_check(app: App, seconds: float, timeout: float) -> int:
    """CI and local checks: show the real tray icon for a few seconds with the core
    running, redraw the menu from its status, then quit through the menu action."""
    if not tray_available():
        log("::error::tray check: no tray available (pystray, Pillow or a display missing)")
        return EXIT_FAILED
    ui = PystrayUI(app)
    errors: list[str] = []
    result = {"status": False, "visible": False}
    original_change = app.on_change

    def on_change(state):
        try:
            original_change(state)
        except Exception as error:      # noqa: BLE001
            errors.append(f"{type(error).__name__}: {error}")

    app.on_change = on_change

    def later():
        deadline = time.monotonic() + timeout
        while app.state.status is None and time.monotonic() < deadline and not app.quitting.is_set():
            time.sleep(0.5)
        time.sleep(seconds)
        result["status"] = app.state.status is not None
        result["visible"] = bool(ui.icon is not None and ui.icon.visible)
        log(f"tray check: icon visible={result['visible']}, status={result['status']}, "
            f"menu items={len(menu.build(app.state))}, headline={menu.headline(app.state)}")
        app.run_action(menu.QUIT)

    threading.Thread(target=later, daemon=True, name="tray-check").start()
    ui.run()
    if errors or not result["status"] or not result["visible"]:
        log(f"::error::tray check failed: {result} {errors[:3]}")
        return EXIT_FAILED
    log("tray check: PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=APP_ID, description=f"{APP_NAME}电脑客户端")
    parser.add_argument("--data", type=Path, help="数据文件夹（默认：每个用户自己的应用数据文件夹）")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘图标（没有托盘的 Linux、服务器）")
    parser.add_argument("--autostarted", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-second", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dev-core", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=90.0, help=argparse.SUPPRESS)
    parser.add_argument("--tray-check", type=float, default=0.0, metavar="SECONDS", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    if args.data:
        os.environ[paths.DATA_ENV] = str(args.data)
    data = paths.data_dir()
    logs_dir = paths.logs_dir(data)
    setup_logs(logs_dir, echo=True)
    log(f"{APP_NAME} {__version__} 启动（数据文件夹：{data}）")

    instance = SingleInstance(paths.lock_file(data))
    if not instance.acquire():
        client = ControlClient(paths.control_file(data))
        if args.smoke_second:
            def record(url: str) -> None:
                (paths.run_dir(data) / "handoff-ok").write_text("ok\n", encoding="ascii")
            opened = hand_off(client, record, wait_s=20, log=log)
            return 0 if opened else EXIT_FAILED
        return 0 if hand_off(client, open_url, log=log) else EXIT_FAILED
    if args.smoke_second:
        log("::error::smoke test: the second instance got the lock")
        instance.release()
        return EXIT_FAILED

    try:
        if args.smoke_test:
            app = build_app(args, data, logs_dir, opener=lambda url: log("smoke test: would open a URL"))
            return smoke_test(app, args.timeout, data)
        if args.tray_check:
            app = build_app(args, data, logs_dir, opener=lambda url: log("tray check: would open a URL"))
            return tray_check(app, args.tray_check, args.timeout)
        app = build_app(args, data, logs_dir)
        if args.no_tray or not tray_available():
            if not args.no_tray:
                log("这台电脑上没有可用的托盘，改用无托盘模式（Ctrl+C 退出）")
            return HeadlessUI(app).run()
        return PystrayUI(app).run()
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
