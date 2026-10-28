"""片库里的「网盘」：网盘组件（OpenList，server/selfhost/openlist.py）的接口和它在片库里的来源。
家庭服务器和电脑客户端的管理员才有；云端没有（relay.selfhost 为 None，一律 404）。

    GET  /api/admin/clouddrive      组件装了没有、版本、有没有新版、在不在跑、本机打开的地址、「网盘」来源的状态
    POST /api/admin/clouddrive      {"action": install|update|check|enable|disable|repair, "mirror": ""}
    POST /api/admin/clouddrive/reveal   管理员密码（只给「在运行小鼠电视的这台电脑上打开」的电脑版，见下）

放在 /api/admin/ 下：隧道端口上默认整个不给（server/front.py），只有管理员（admin.py 先查）。改东西的请求由
web.py 先查同源，每次改都写操作记录。

OpenList 只听 127.0.0.1，所以「打开网盘管理」只在运行小鼠电视的这台电脑上有用；这里不转发它的页面，别的设备
（手机、家里另一台电脑）打开时只说一声。它的网页把登录令牌存在自己网址的 localStorage 里，也没有带令牌的
登录地址（看过 OpenList-Frontend 的登录页：只认 redirect），所以小鼠电视没法替人登录：页面上给出用户名，
密码要点「显示」才取（reveal），也只给这台电脑上打开的电脑版（ytaccount.from_this_computer）。别的回答、
日志里从不出现密码。

维护片库里的「网盘」来源（sync，每 SYNC_S 秒和每次操作后）：组件装好、没停用、在跑（run/openlist.json 的端口
/ping 得通）时，来源指向 http://127.0.0.1:<端口>/dav、用组件的管理员；停用或卸掉了就去掉。还没起来、刚崩溃
重启的时候不动它。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

SYNC_S = 10.0
START_DELAY_S = 3.0
NOT_HERE = "只有家庭服务器和电脑客户端有网盘组件"
# OpenList 官方的说明（页面上的三行说明链到这里）。
DOCS = {"baidu": "https://doc.oplist.org/guide/drivers/baidu",
        "baidu_token": "https://api.oplist.org/",
        "quark": "https://doc.oplist.org/guide/drivers/quark",
        "proxy": "https://doc.oplist.org/guide/drivers/common#webdav-%E7%AD%96%E7%95%A5",
        "repo": "https://github.com/OpenListTeam/OpenList"}


def enabled_here(relay) -> bool:
    return relay.selfhost is not None and getattr(relay, "selfhost_data", None) is not None


def component(relay):
    """这个中转进程里唯一的网盘组件管理（下载在它的后台线程里）。"""
    found = getattr(relay, "_openlist", None)
    if found is None:
        from ..selfhost.openlist import OpenList
        found = OpenList(Path(relay.selfhost_data), log=relay.log)
        relay._openlist = found
    return found


def _proxy(relay) -> str:
    """下载经「设置 → YouTube」里填的代理（没填就直连）。"""
    from . import ytsettings
    config, _ = ytsettings._config(relay)
    return config.youtube_proxy


def running_port(relay) -> int:
    """在跑的端口（run/openlist.json 写的，/ping 得通）；没在跑 0。"""
    from ..selfhost import openlist
    parts = component(relay)
    status = openlist.read_status(parts.paths)
    port = status.get("port")
    if status.get("state") != "running" or not isinstance(port, int):
        return 0
    return port if openlist.ping(port, 1.0) else 0


def sync(relay) -> str:
    """让片库里的「网盘」来源跟上组件：返回 "added" / "updated" / "same" / "dropped" / "waiting" / ""。"""
    from ..selfhost import openlist
    library = getattr(relay, "library", None)
    if library is None or not enabled_here(relay):
        return ""
    parts = component(relay)
    if parts.binary() is None or not openlist.enabled(parts.paths):
        return "dropped" if library.drop_managed() else ""
    port = running_port(relay)
    password = openlist.read_password(parts.paths)
    if not port or not password:
        return "waiting"
    return library.ensure_managed(f"http://127.0.0.1:{port}/dav", openlist.USERNAME, password)


def _loop(relay, sleep=time.sleep) -> None:
    sleep(START_DELAY_S)
    while True:
        try:
            sync(relay)
        except Exception as error:      # noqa: BLE001 —— 后台线程不能悄悄死
            relay.log(f"网盘组件同步片库出错：{type(error).__name__}")
        sleep(SYNC_S)


def attach(relay, start: bool = True) -> None:
    """自建 / 电脑客户端：在后台维护片库里的「网盘」来源。"""
    if not enabled_here(relay) or getattr(relay, "library", None) is None:
        return
    if start:
        threading.Thread(target=_loop, args=(relay,), name="clouddrive", daemon=True).start()


def status(relay, handler) -> dict:
    """GET /api/admin/clouddrive（没有密码）。"""
    from ..selfhost import openlist
    from .ytaccount import from_this_computer
    if not enabled_here(relay):
        return {"ok": False, "error": NOT_HERE}
    parts = component(relay)
    snap = parts.snapshot()
    port = running_port(relay) if snap["installed"] and snap["enabled"] else 0
    library = getattr(relay, "library", None)
    managed = library.managed() if library is not None else None
    source = None
    if managed is not None:
        source = library._public_source(managed, library._counts().get(managed["id"], 0))
    return {"ok": True, "component": snap, "running": bool(port), "port": port,
            "url": f"http://127.0.0.1:{port}/" if port else "", "user": openlist.USERNAME,
            "has_password": bool(openlist.read_password(parts.paths)), "local": from_this_computer(handler),
            "source": source, "docs": DOCS, "license": openlist.LICENSE, "name": "OpenList"}


def action(relay, handler, body: dict) -> dict:
    """POST /api/admin/clouddrive。"""
    from ..selfhost import ytcomponents
    if not enabled_here(relay):
        return {"ok": False, "error": NOT_HERE}
    what, mirror = body.get("action"), body.get("mirror") or ""
    if what not in ("install", "update", "check", "enable", "disable", "repair") or not isinstance(mirror, str):
        return {"ok": False, "error": "请求格式不对"}
    mirror = mirror.strip()
    if mirror and not ytcomponents.valid_mirror(mirror):
        return {"ok": False, "error": "镜像要写成 https:// 开头的网址前缀"}
    parts = component(relay)
    if not parts.asset:
        return {"ok": False, "error": "这台电脑的系统没有 OpenList 的官方版本"}
    if what == "check":
        if parts.binary() is None:
            return {"ok": False, "error": "网盘组件还没装"}
        try:
            found = parts.check_update(_proxy(relay), mirror)
        except ytcomponents.ComponentError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, **found, "component": parts.snapshot()}
    if what in ("install", "update"):
        if not parts.start(what, _proxy(relay), mirror):
            return {"ok": False, "error": "正在下载，等它做完"}
        return {"ok": True, "component": parts.snapshot()}
    if what == "disable":
        parts.disable()
        sync(relay)
        return {"ok": True, "component": parts.snapshot()}
    if what == "enable":
        if not parts.enable():
            return {"ok": False, "error": "网盘组件还没装好：点「安装网盘组件」"}
        return {"ok": True, "component": parts.snapshot()}
    # repair：把 OpenList 里 admin 的密码重新设成小鼠电视记着的（在它的网页里改过密码、片库连不上时）。
    if not parts.reset_password():
        return {"ok": False, "error": "重设没成功（网盘组件装好了吗？）"}
    sync(relay)
    return {"ok": True, "component": parts.snapshot()}


def reveal(relay, handler) -> dict:
    """POST /api/admin/clouddrive/reveal：管理员密码，只给这台电脑上打开的电脑版。"""
    from ..selfhost import openlist
    from .ytaccount import from_this_computer
    if not enabled_here(relay):
        return {"ok": False, "error": NOT_HERE}
    if not from_this_computer(handler):
        return {"ok": False, "error": "只能在运行小鼠电视的这台电脑上看网盘管理的密码"}
    password = openlist.read_password(component(relay).paths)
    if not password:
        return {"ok": False, "error": "还没有密码：先安装网盘组件"}
    return {"ok": True, "user": openlist.USERNAME, "password": password}
