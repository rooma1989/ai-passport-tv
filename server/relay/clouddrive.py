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

在小鼠电视里一键加网盘（不用打开 OpenList 的网页、不用从浏览器里抄 Cookie）：

    GET  /api/admin/clouddrive/storages         加了哪些网盘：挂在哪、哪家、状态（正常 / 需要扫码 / 登录过期 / 出错）
    POST /api/admin/clouddrive/quark/start      {"kind": "quark"|"uc", "mount_path": ""} 出一个扫码登录的二维码
    POST /api/admin/clouddrive/quark/check      {"mount_path"} 扫了没有：done / waiting / expired（页面每 3 秒问一次）
    POST /api/admin/clouddrive/baidu            {"refresh_token", "mount_path": ""} 用刷新令牌加百度网盘
    POST /api/admin/clouddrive/storages/delete  {"mount_path"} 删掉一个网盘（只是从 OpenList 里去掉，网盘里的文件不动）
    POST /api/admin/clouddrive/rescan           片库重新扫「网盘」来源

都是照着 OpenList v4.2.9 的源代码做的（OpenListTeam/OpenList，标签 v4.2.9），用它自己的管理接口
（POST /api/auth/login 拿令牌，放在 Authorization 头里；/api/admin/storage/list、get、create、update、delete；
/api/admin/driver/info 取驱动各项的默认值——OpenList 不替没给的项填默认值，那是它网页做的）：
- 夸克、UC 用 TV 版的驱动 QuarkTV、UCTV（drivers/quark_uc_tv）：它的 Init 没有 refresh_token、也没有
  query_token 时向夸克要一个登录二维码，把 query_token 存进存储的附加项，再以错误「need verify: <body><img
  src="data:image/jpeg;base64,…"></body>」结束——这段 HTML 就是存储的 status，二维码从这里取。v4.2.9 没有
  「重新加载」的接口：/api/admin/storage/update 会把存储丢掉再跑一遍 Init（op.UpdateStorage），这时有了
  query_token，它拿去换 code、再换 refresh_token（存回附加项）、登进去，status 变成 "work"。用户还没扫时夸克
  回答「用户未确认授权」。所以「扫了没有」= 用存着的样子原样 update 一次再看 status；「换一个二维码」= update 时把
  query_token、refresh_token 清空（登录过期了重新扫码也是这样）。
- 百度网盘用 BaiduNetdisk：附加项 use_online_api 默认开着，refresh_token 交给 OpenList 的在线接口
  （api_url_address，默认 https://api.oplist.org/baiduyun/renewapi）换 access_token，不用自己申请 client_id。
  刷新令牌在 OpenList 官方的取令牌页（https://api.oplist.org/，doc.oplist.org 的百度网盘说明里的「方法二：在线
  API」）登录百度后拿到，主人粘贴到小鼠电视里就行。
- WebDAV 策略都设成 native_proxy（本地代理）：片库读到的是 OpenList 转过来的数据，不用管网盘下载地址要什么 UA
  （百度要 pan.baidu.com，驱动自己带上），也不会把带签名的下载地址交给别的程序。
- 令牌、Cookie、二维码里的地址、附加项、管理员密码从不回给页面、不进日志：页面只拿到挂载的路径、哪家、状态和一句
  大白话的原因（原因只取短的、不像令牌和网址的那种，别的换成固定的说法）；二维码是图片本身（重新编码过的 JPEG / PNG）。
"""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import re
import threading
import time
import urllib.parse
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


# ---------------------------------------------------------------- 在小鼠电视里加网盘（OpenList 的管理接口）

API_TIMEOUT_S = 30.0
ANSWER_MAX = 4 * 1024 * 1024
QR_MAX = 256 * 1024
# 夸克的二维码过期时它自己说（「授权码Code二维码过期」，真跑时出码后三到五分钟）；这里只是兜底：
# 出码这么久还没扫成，就让页面「换一个」。
QR_TTL_S = 600.0
# 页面每 3 秒问一次「扫了没有」；两次真去问夸克之间至少隔这么久。
CHECK_GAP_S = 2.0
NOT_RUNNING = "网盘组件还没在运行"
# OpenList 的驱动名 → 页面上的叫法；能在小鼠电视里扫码 / 粘贴令牌重新登录的只有前三个。
KINDS = {"QuarkTV": "夸克", "UCTV": "UC 网盘", "BaiduNetdisk": "百度网盘", "Quark": "夸克（Cookie）",
         "UC": "UC 网盘（Cookie）", "QuarkOpen": "夸克（开放平台）", "BaiduPhoto": "一刻相册", "189CloudTV": "天翼云盘"}
QR_DRIVERS = {"quark": ("QuarkTV", "/夸克"), "uc": ("UCTV", "/UC网盘")}
BAIDU = ("BaiduNetdisk", "/百度网盘")
MOUNT_MAX = 60
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,1024}")
_QR = re.compile(r'data:image/(jpeg|jpg|png);base64,([A-Za-z0-9+/=\s]+)')
_NET = re.compile(r"(?i)deadline exceeded|timeout|timed out|dial tcp|no such host|connection refused|connection reset"
                  r"|network is unreachable|tls handshake|\bEOF\b")
_EXPIRED = re.compile(r"(?i)refresh.?token|access.?token|token|expired|过期|失效|未登录|重新登录|invalid_grant|unauthori")
_UNSAFE = re.compile(r"(?i)https?:|base64|token|cookie|password|[=&?<>{}\"']|[A-Za-z0-9_-]{16,}")
_API_LOCK = threading.Lock()


class DriveError(Exception):
    """给页面的一句大白话。"""


class Api:
    """和本机 OpenList 说话（只连 127.0.0.1，不走代理）。登录令牌按端口记在 relay 上，过期了重新登录一次。"""

    def __init__(self, relay, port: int, password: str, timeout: float = API_TIMEOUT_S):
        self.relay, self.port, self.password, self.timeout = relay, port, password, timeout

    def _raw(self, method: str, path: str, body=None, token: str = "") -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        headers = {"User-Agent": "RooTV", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = token
        try:
            connection.request(method, path, body=data, headers=headers)
            raw = connection.getresponse().read(ANSWER_MAX + 1)
        except (OSError, http.client.HTTPException):
            raise DriveError("连不上网盘组件") from None
        finally:
            connection.close()
        if len(raw) > ANSWER_MAX:
            raise DriveError("网盘组件的回答大得不正常")
        try:
            answer = json.loads(raw)
        except ValueError:
            raise DriveError("网盘组件的回答看不懂") from None
        if not isinstance(answer, dict):
            raise DriveError("网盘组件的回答看不懂")
        return answer

    def _login(self) -> str:
        from ..selfhost import openlist
        answer = self._raw("POST", "/api/auth/login", {"username": openlist.USERNAME, "password": self.password})
        data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
        token = data.get("token")
        if answer.get("code") != 200 or not isinstance(token, str) or not token:
            raise DriveError("网盘组件的管理员密码对不上：在「高级」里点「改回来」")
        self.relay._clouddrive_token = (self.port, token)
        return token

    def call(self, method: str, path: str, body=None) -> dict:
        """OpenList 的回答 {"code", "message", "data"}（code 不是 200 时也照样返回，调用的人看）。"""
        cached = getattr(self.relay, "_clouddrive_token", None)
        token = cached[1] if isinstance(cached, tuple) and cached[0] == self.port else self._login()
        answer = self._raw(method, path, body, token)
        if answer.get("code") == 401:
            answer = self._raw(method, path, body, self._login())
            if answer.get("code") == 401:
                raise DriveError("网盘组件不让小鼠电视管理：在「高级」里点「改回来」")
        return answer

    def storages(self) -> list[dict]:
        answer = self.call("GET", "/api/admin/storage/list?page=1&per_page=500")
        data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
        content = data.get("content")
        if answer.get("code") != 200 or not isinstance(content, list):
            raise DriveError("取不到网盘列表")
        return [s for s in content if isinstance(s, dict) and isinstance(s.get("mount_path"), str)]

    def storage(self, sid: int) -> dict:
        answer = self.call("GET", f"/api/admin/storage/get?id={int(sid)}")
        if answer.get("code") != 200 or not isinstance(answer.get("data"), dict):
            raise DriveError("这个网盘不见了（刷新一下）")
        return answer["data"]

    def defaults(self, driver: str) -> dict:
        """这个驱动附加项的默认值（OpenList 不替没给的项填默认值，它的网页加存储时也是先填这些）。"""
        answer = self.call("GET", "/api/admin/driver/info?driver=" + urllib.parse.quote(driver))
        info = answer.get("data") if isinstance(answer.get("data"), dict) else None
        if answer.get("code") != 200 or info is None:
            raise DriveError("这个版本的网盘组件没有这种网盘")
        found = {}
        for item in info.get("additional") or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            value, kind = str(item.get("default") or ""), item.get("type")
            if kind == "bool":
                found[item["name"]] = value == "true"
            elif kind == "number":
                found[item["name"]] = int(value) if re.fullmatch(r"-?\d{1,9}", value) else 0
            else:
                found[item["name"]] = value
        return found


def api(relay) -> Api:
    from ..selfhost import openlist
    port = running_port(relay)
    password = openlist.read_password(component(relay).paths)
    if not port or not password:
        raise DriveError(NOT_RUNNING)
    return Api(relay, port, password)


def _addition(storage: dict) -> dict:
    try:
        found = json.loads(storage.get("addition") or "{}")
    except (TypeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _reason(text: str) -> str:
    """OpenList 的 status / message 里能给页面看的一句：短的、不像令牌和网址的原样给，别的换成固定的说法。"""
    text = re.sub(r"^(failed init storage( but storage is already created)?: )+", "", (text or "").strip())
    if _NET.search(text):
        return "连不上网盘（网络不通或太慢）"
    first = text.splitlines()[0].strip() if text else ""
    if first and len(first) <= 40 and not _UNSAFE.search(first):
        return first
    if _EXPIRED.search(text):
        return "令牌不对或过期了"
    return "网盘那边出错了"


def _state(storage: dict) -> tuple[str, str]:
    """(状态, 原因)：ok 正常、scan 需要扫码、expired 登录过期、error 出错、off 停用了。"""
    status = str(storage.get("status") or "")
    if storage.get("disabled"):
        return "off", ""
    if status == "work":
        return "ok", ""
    if status.startswith("need verify") or "用户未确认授权" in status:
        return "scan", ""
    if "二维码" in status:
        return "scan", "二维码过期了" if _EXPIRED.search(status) else ""
    if _NET.search(status):
        return "error", _reason(status)
    if _EXPIRED.search(status):
        return "expired", _reason(status)
    return "error", _reason(status)


LABELS = {"ok": "正常", "scan": "需要扫码", "expired": "登录过期", "error": "出错", "off": "停用了"}


def _public(storage: dict) -> dict:
    """页面看到的一个网盘：没有附加项、status 原文、令牌。"""
    driver = str(storage.get("driver") or "")
    state, reason = _state(storage)
    relogin = "quark" if driver == "QuarkTV" else "uc" if driver == "UCTV" else "baidu" if driver == BAIDU[0] else ""
    return {"mount_path": storage["mount_path"], "name": storage["mount_path"].strip("/") or "/", "driver": driver,
            "kind": KINDS.get(driver, driver), "state": state, "label": LABELS[state], "reason": reason,
            "relogin": relogin}


def _qr(status: str) -> str:
    """status 里的二维码 → 重新编码过的 data: 地址（只认 JPEG、PNG）；没有 ""。"""
    match = _QR.search(status or "")
    if match is None:
        return ""
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError):
        return ""
    if not raw or len(raw) > QR_MAX:
        return ""
    if raw.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    else:
        return ""
    return f"data:image/{kind};base64," + base64.b64encode(raw).decode("ascii")


def _free_mount(storages: list[dict], base: str) -> str:
    taken = {str(s.get("mount_path") or "").rstrip("/").casefold() for s in storages}
    if base.casefold() not in taken:
        return base
    for n in range(2, 100):
        if f"{base}{n}".casefold() not in taken:
            return f"{base}{n}"
    raise DriveError("网盘太多了")


def _mount_arg(body: dict) -> str:
    value = body.get("mount_path")
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not value.startswith("/") or len(value) > MOUNT_MAX or "\n" in value:
        raise DriveError("请求格式不对")
    return value


def _find(storages: list[dict], mount: str) -> dict:
    found = next((s for s in storages if s["mount_path"] == mount), None)
    if found is None:
        raise DriveError("没有这个网盘（刷新一下）")
    return found


def _memo(relay, name: str) -> dict:
    found = getattr(relay, name, None)
    if found is None:
        found = {}
        setattr(relay, name, found)
    return found


def _after_added(relay) -> None:
    """加好、删掉一个网盘以后：片库的「网盘」来源跟上，再扫一遍它。"""
    try:
        sync(relay)
    except Exception as error:      # noqa: BLE001 —— 加好了就是加好了，片库下次同步再跟上
        relay.log(f"网盘组件同步片库出错：{type(error).__name__}")
    library = getattr(relay, "library", None)
    managed = library.managed() if library is not None else None
    if managed is not None:
        library.scan(managed["id"])


def _reinit(client: Api, storage: dict, **addition) -> dict:
    """用 /api/admin/storage/update 让 OpenList 丢掉这个存储、重跑一遍 Init（v4.2.9 的「重新加载」）；
    addition 是要改的附加项。返回改完以后的存储（status 是新的）。"""
    sid = storage.get("id")
    if not isinstance(sid, int):
        raise DriveError("这个网盘不见了（刷新一下）")
    full = client.storage(sid)
    if addition:
        merged = _addition(full)
        merged.update(addition)
        full["addition"] = json.dumps(merged, ensure_ascii=False)
    client.call("POST", "/api/admin/storage/update", full)      # Init 没过时 code 500，结果看 status
    return client.storage(sid)


def storages(relay) -> dict:
    """GET /api/admin/clouddrive/storages。"""
    if not enabled_here(relay):
        return {"ok": False, "error": NOT_HERE}
    try:
        found = api(relay).storages()
    except DriveError as error:
        return {"ok": True, "running": False, "storages": [], "error": str(error)}
    found.sort(key=lambda s: (s.get("order") or 0, s.get("id") or 0))
    return {"ok": True, "running": True, "storages": [_public(s) for s in found]}


def _create(client: Api, listed: list[dict], driver: str, base: str, **addition) -> tuple[str, dict]:
    """新挂一个（挂载路径取第一个空着的 /夸克、/夸克2……）。返回 (挂载路径, 加好以后的存储)。"""
    mount = _free_mount(listed, base)
    merged = client.defaults(driver)
    merged.update(addition)
    answer = client.call("POST", "/api/admin/storage/create", {
        "mount_path": mount, "driver": driver, "addition": json.dumps(merged, ensure_ascii=False),
        "webdav_policy": "native_proxy", "web_proxy": True, "cache_expiration": 30, "remark": "小鼠电视添加",
        "order": len(listed)})
    data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
    sid = data.get("id")
    if not isinstance(sid, int) or not sid:
        raise DriveError("网盘组件没加上：" + _reason(str(answer.get("message") or "")))
    return mount, client.storage(sid)


def quark_start(relay, body: dict) -> dict:
    """POST /api/admin/clouddrive/quark/start：新加（或接着用一个还没登上的）夸克 / UC，回答二维码。"""
    kind = body.get("kind") or "quark"
    if kind not in QR_DRIVERS:
        raise DriveError("请求格式不对")
    driver, base = QR_DRIVERS[kind]
    mount = _mount_arg(body)
    client = api(relay)
    with _API_LOCK:
        listed = client.storages()
        if mount:
            target = _find(listed, mount)
            if target.get("driver") not in ("QuarkTV", "UCTV"):
                raise DriveError("这个网盘不是扫码登录的")
            driver = target["driver"]
        else:
            # 上次没扫完的同一家（从没登上过）接着用，不再多挂一个。
            target = next((s for s in listed if s.get("driver") == driver and not s.get("disabled")
                           and _state(s)[0] != "ok" and not _addition(s).get("refresh_token")), None)
        if target is not None:
            if target.get("disabled"):
                raise DriveError("这个网盘在网盘管理里停用了")
            mount = target["mount_path"]
            after = _reinit(client, target, query_token="", refresh_token="")
        else:
            mount, after = _create(client, listed, driver, base, query_token="", refresh_token="")
            relay.log(f"网盘：挂了「{mount.strip('/')}」，等扫码登录")
    state, reason = _state(after)
    if state == "ok":
        _after_added(relay)
        return {"ok": True, "state": "done", "mount_path": mount, "kind": KINDS[driver]}
    qr = _qr(str(after.get("status") or ""))
    if not qr:
        raise DriveError("没拿到二维码：" + (reason or "网盘那边出错了") + "，再试一次")
    _memo(relay, "_clouddrive_qr")[mount] = time.monotonic()
    return {"ok": True, "state": "waiting", "mount_path": mount, "kind": KINDS[driver], "qr": qr,
            "ttl": int(QR_TTL_S)}


def quark_check(relay, body: dict) -> dict:
    """POST /api/admin/clouddrive/quark/check：重跑一次 Init，看扫了没有。done / waiting / expired。"""
    mount = _mount_arg(body)
    if not mount:
        raise DriveError("请求格式不对")
    client = api(relay)
    issued_at, checked_at = _memo(relay, "_clouddrive_qr"), _memo(relay, "_clouddrive_checked")
    with _API_LOCK:
        target = _find(client.storages(), mount)
        if target.get("driver") not in ("QuarkTV", "UCTV"):
            raise DriveError("这个网盘不是扫码登录的")
        if _state(target)[0] != "ok":
            issued = issued_at.get(mount)
            if issued is None or time.monotonic() - issued > QR_TTL_S:
                # 过期了，或者二维码不是这次出的（小鼠电视重启过）：让页面换一个。
                issued_at.pop(mount, None)
                return {"ok": True, "state": "expired", "mount_path": mount}
            if time.monotonic() - checked_at.get(mount, 0.0) < CHECK_GAP_S:
                return {"ok": True, "state": "waiting", "mount_path": mount}
            checked_at[mount] = time.monotonic()
            after = _reinit(client, target)
            state, reason = _state(after)
            if state != "ok":
                qr = _qr(str(after.get("status") or ""))
                if qr:
                    # 存着的 query_token 没了，它自己又出了一个码：换上新的。
                    issued_at[mount] = time.monotonic()
                    return {"ok": True, "state": "waiting", "mount_path": mount, "qr": qr, "ttl": int(QR_TTL_S)}
                if state == "expired" or reason == "二维码过期了":
                    issued_at.pop(mount, None)
                    return {"ok": True, "state": "expired", "mount_path": mount}
                return {"ok": True, "state": "waiting", "mount_path": mount, "note": reason if state == "error" else ""}
            relay.log(f"网盘：「{mount.strip('/')}」扫码登录好了")
        issued_at.pop(mount, None)
    _after_added(relay)
    return {"ok": True, "state": "done", "mount_path": mount, "kind": KINDS.get(str(target.get("driver")), "")}


def baidu_add(relay, body: dict) -> dict:
    """POST /api/admin/clouddrive/baidu：用 OpenList 取令牌页拿到的刷新令牌加百度网盘（或给登录过期的那个换令牌）。"""
    token = body.get("refresh_token")
    if not isinstance(token, str):
        raise DriveError("请求格式不对")
    token = token.strip().strip('"').strip()
    if not _TOKEN.fullmatch(token):
        raise DriveError("这不像刷新令牌：在取令牌页复制 refresh_token 那一栏的全部内容")
    mount = _mount_arg(body)
    driver, base = BAIDU
    client = api(relay)
    with _API_LOCK:
        listed = client.storages()
        if mount:
            target = _find(listed, mount)
            if target.get("driver") != driver:
                raise DriveError("这个网盘不是百度网盘")
        else:
            # 上次没加成的百度网盘接着用（换个令牌），不再多挂一个。
            target = next((s for s in listed if s.get("driver") == driver and not s.get("disabled")
                           and _state(s)[0] in ("expired", "error")), None)
        if target is not None:
            if target.get("disabled"):
                raise DriveError("这个网盘在网盘管理里停用了")
            mount = target["mount_path"]
            after = _reinit(client, target, refresh_token=token, use_online_api=True)
        else:
            mount, after = _create(client, listed, driver, base, refresh_token=token, use_online_api=True)
    state, reason = _state(after)
    if state != "ok":
        relay.log(f"网盘：「{mount.strip('/')}」没登录上")
        if state == "expired":
            raise DriveError("百度网盘没登录上：令牌不对或过期了，回取令牌页重新取一个")
        raise DriveError("百度网盘没登录上：" + (reason or "网盘那边出错了"))
    relay.log(f"网盘：加好了「{mount.strip('/')}」")
    _after_added(relay)
    return {"ok": True, "state": "done", "mount_path": mount, "kind": KINDS[driver]}


def delete(relay, body: dict) -> dict:
    """POST /api/admin/clouddrive/storages/delete：从 OpenList 里去掉（网盘里的文件不动），片库重新扫。"""
    mount = _mount_arg(body)
    if not mount:
        raise DriveError("请求格式不对")
    client = api(relay)
    with _API_LOCK:
        target = _find(client.storages(), mount)
        sid = target.get("id")
        if not isinstance(sid, int):
            raise DriveError("这个网盘不见了（刷新一下）")
        answer = client.call("POST", f"/api/admin/storage/delete?id={sid}")
        if answer.get("code") != 200:
            raise DriveError("没删掉：" + _reason(str(answer.get("message") or "")))
    _memo(relay, "_clouddrive_qr").pop(mount, None)
    relay.log(f"网盘：删掉了「{mount.strip('/')}」")
    _after_added(relay)
    return {"ok": True, "mount_path": mount}


def rescan(relay) -> dict:
    """POST /api/admin/clouddrive/rescan：片库的「网盘」来源跟上组件，再扫一遍。"""
    library = getattr(relay, "library", None)
    if library is None:
        return {"ok": False, "error": "这里没有片库"}
    _after_added(relay)
    if library.managed() is None:
        return {"ok": False, "error": "片库里还没有「网盘」来源（网盘组件在运行吗？）"}
    return {"ok": True}


ROUTES = {"/api/admin/clouddrive/quark/start": quark_start, "/api/admin/clouddrive/quark/check": quark_check,
          "/api/admin/clouddrive/baidu": baidu_add, "/api/admin/clouddrive/storages/delete": delete,
          "/api/admin/clouddrive/rescan": lambda relay, _body: rescan(relay)}


def drives(relay, path: str, body: dict) -> tuple[dict, str]:
    """admin.py 转来的 POST → (回答, 操作记录的动作；不用记时 "")。只在真加上、删掉、出了二维码、扫描时记。"""
    if not enabled_here(relay):
        return {"ok": False, "error": NOT_HERE}, ""
    work = ROUTES.get(path)
    if work is None:
        return {"ok": False, "error": "请求格式不对"}, ""
    try:
        answer = work(relay, body)
    except DriveError as error:
        return {"ok": False, "error": str(error)}, ""
    if not answer.get("ok"):
        return answer, ""
    if path.endswith("/delete"):
        return answer, "clouddrive.drive_delete"
    if path.endswith("/rescan"):
        return answer, "clouddrive.rescan"
    if answer.get("state") == "done":
        return answer, "clouddrive.drive_add"
    return answer, "clouddrive.drive_qr" if path.endswith("/start") else ""
