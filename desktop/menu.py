"""The tray menu as data (design 5.3), without any GUI toolkit.

`build(state)` returns a tuple of `Item`s; `tray.py` turns it into a pystray
menu, the headless mode prints its headline. Keeping it pure is what makes the
menu testable on a CI machine without a display, and what option B's shell can
copy item for item.

v0.2.0-beta shows what exists in this release: no away-from-home control (P5),
no update check (P8), no built-in flashing (P7).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import APP_NAME
from .control import Status


@dataclass(frozen=True)
class Item:
    text: str
    action: str | None = None        # an action id handled by the tray; None = information only
    checked: bool | None = None      # None = not a check item
    enabled: bool = True
    default: bool = False            # the item a left click runs (Windows, Linux)
    submenu: tuple["Item", ...] | None = None


SEPARATOR = Item("-")

# Action ids (tray.App.ACTIONS maps them to methods).
OPEN_CONSOLE = "open_console"
OPEN_SETUP = "open_setup"
CONNECT_PHONE = "connect_phone"
SET_UP_TV = "set_up_tv"
FETCH_FFMPEG = "fetch_ffmpeg"
TRUST_NETWORK = "trust_network"
NETWORK_HELP = "network_help"
KEEP_AWAKE = "toggle_keep_awake"
AUTOSTART = "toggle_autostart"
OPEN_LOGS = "open_logs"
DIAGNOSTICS = "diagnostics"
RESTART = "restart"
QUIT = "quit"


@dataclass(frozen=True)
class TrayState:
    core: str = "starting"              # "starting" | "running" | "restarting" | "stopped"
    status: Status | None = None        # last status from the control API
    autostart: bool | None = None       # None = cannot be changed here (source checkout, DMG)
    keep_awake: str = "watching"        # "watching" | "always" | "never"
    awake_held: bool = False
    windows_public: str = ""            # name of a Windows network marked Public, "" if none
    translocated: bool = False          # macOS: running from the DMG / a translocated path
    busy: str = ""                      # a short note while an action runs ("正在导出诊断信息…")
    notices: tuple[str, ...] = field(default_factory=tuple)


# The core may report several problems; the first ones matter, a long list does not fit a menu.
MAX_PROBLEMS = 3


def watching_count(state: TrayState) -> int:
    return state.status.watching if state.status else 0


def problems(state: TrayState) -> list[str]:
    """Things the user has to know about, most important first."""
    out = []
    if state.translocated:
        out.append("请先把「小智电视」拖进「应用程序」文件夹，再从那里打开")
    if state.core == "restarting":
        out.append("电视服务停了，正在重新启动…")
    elif state.core == "stopped":
        out.append("电视服务没有在运行")
    status = state.status
    if status is not None:
        ff = status.ffmpeg
        # The core reports a missing or failed ffmpeg as a problem with its own
        # message (codes ffmpeg_missing / ffmpeg_failed); the tray's wording is
        # only for a core that does not.
        core_says_ffmpeg = any(p.code.startswith("ffmpeg") for p in status.problems)
        if ff.state == "downloading":
            pct = f" {int(round(ff.progress * 100))}%" if ff.progress is not None else ""
            out.append(f"正在下载视频组件（ffmpeg）…{pct}")
        elif ff.state == "missing" and not core_says_ffmpeg:
            out.append("缺少视频组件（ffmpeg），小电视暂时放不了")
        elif ff.state == "failed" and not core_says_ffmpeg:
            out.append("视频组件下载失败" + (f"：{ff.error}" if ff.error else ""))
        for problem in status.problems[:MAX_PROBLEMS]:
            out.append(problem.message)
        network = status.network
        if network is not None and network.lan_open is False:
            out.append("不在家里的网络上：小电视和手机暂时连不进来")
    if state.windows_public:
        out.append(f"「{state.windows_public}」被标成了公用网络，小电视连不进来")
    return out


def headline(state: TrayState) -> str:
    if state.core == "starting" or (state.core == "running" and state.status is None):
        return f"{APP_NAME} · 正在启动…"
    if state.core != "running":
        return f"{APP_NAME} · 电视服务没有在运行"
    status = state.status
    if status.state == "error":
        return f"{APP_NAME} · 电视服务出了问题"
    if status.state == "fetching_ffmpeg":
        return f"{APP_NAME} · 正在准备视频组件…"
    if status.setup_pending:
        return f"{APP_NAME} · 等你完成第一次设置"
    n = watching_count(state)
    if n:
        return f"{APP_NAME} · 正在为 {n} 台小电视推流"
    return f"{APP_NAME} · 运行中，没有小电视在看"


def tooltip(state: TrayState) -> str:
    """Windows limits the tooltip to 127 characters; keep it short."""
    text = headline(state)
    issues = problems(state)
    if issues:
        text += "\n" + issues[0]
    return text[:120]


def icon_kind(state: TrayState) -> str:
    """"problem" (badge), "watching" (filled), "idle" (outline)."""
    issues = len(problems(state))
    if state.status is not None and state.status.ffmpeg.state == "downloading":
        issues -= 1     # a download in progress is news, not a problem
    if issues > 0:
        return "problem"
    return "watching" if watching_count(state) else "idle"


def _devices(status: Status) -> tuple[Item, ...]:
    items = []
    for device in status.devices:
        if device.watching:
            what = f"{device.channel}（在看）" if device.channel else "在看"
        else:
            what = "在线" if device.online else "离线"
        items.append(Item(f"{device.name} —— {what}", enabled=False))
    return tuple(items)


def network_trust_needed(state: TrayState) -> str:
    """The router id to offer "this is my home network" for, or ""."""
    status = state.status
    if status is not None and status.network is not None:
        net = status.network
        return net.id if net.id and net.trusted is None else ""
    return ""


def build(state: TrayState) -> tuple[Item, ...]:
    running = state.core == "running" and state.status is not None
    status = state.status
    items: list[Item] = [Item(headline(state), enabled=False)]
    items += [Item(text, enabled=False) for text in problems(state)]
    if state.busy:
        items.append(Item(state.busy, enabled=False))
    items += [Item(text, enabled=False) for text in state.notices]

    if running and status.setup_pending:
        items.append(Item("继续第一次设置…", OPEN_SETUP, default=True))
        items.append(Item("打开电脑版", OPEN_CONSOLE, enabled=running))
    else:
        items.append(Item("打开电脑版", OPEN_CONSOLE, enabled=running, default=True))
    items.append(Item("连接手机…", CONNECT_PHONE, enabled=running and not status.setup_pending))
    items.append(Item("设置一台电视…", SET_UP_TV, enabled=running))
    items.append(SEPARATOR)

    if running and status.ffmpeg.state in ("missing", "failed"):
        items.append(Item("重新下载视频组件（ffmpeg）", FETCH_FFMPEG))
    if running and status.devices:
        items.append(Item(f"设备（{len(status.devices)}）", submenu=_devices(status)))
    if network_trust_needed(state):
        items.append(Item("这是我家的网络（允许小电视和手机连进来）", TRUST_NETWORK))
    if state.windows_public:
        items.append(Item("怎么把家里的网络改成「专用」…", NETWORK_HELP))
    items.append(Item("不让电脑睡眠（小智电视运行时一直保持唤醒）", KEEP_AWAKE,
                      checked=state.keep_awake == "always"))
    items.append(Item("登录时启动", AUTOSTART, checked=bool(state.autostart),
                      enabled=state.autostart is not None))
    items.append(SEPARATOR)
    items.append(Item("打开日志文件夹", OPEN_LOGS))
    items.append(Item("导出诊断信息…", DIAGNOSTICS, enabled=not state.busy))
    items.append(Item("重启电视服务", RESTART, enabled=state.core in ("running", "restarting", "stopped")))
    items.append(Item("退出小智电视", QUIT))
    return tuple(items)


def actions_in(items) -> set[str]:
    """Every action id reachable in a menu (for tests)."""
    found = set()
    for item in items:
        if item.action:
            found.add(item.action)
        if item.submenu:
            found |= actions_in(item.submenu)
    return found
