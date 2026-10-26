"""YouTube 登录的两个方便功能（家庭服务器和电脑客户端的管理员；云端没有这些路径）：

1. 从这台电脑的浏览器读取 YouTube 登录（server/selfhost/ytbrowser.py），代替手动导出 cookies.txt：

    GET  /api/youtube/browsers          这台电脑上有的浏览器和配置、上一次读取的结果
    POST /api/youtube/browsers/read     {"browser", "profile"}：在后台读（钥匙串窗口要等人点），页面再问上面那个

   读的是运行小鼠电视的这台电脑上的浏览器，所以只给「在这台电脑上打开的电脑版」：对端是回环、前端
   （server/front.py）说是局域网端口上直接打开的（X-Front-Local，没有任何转发的头）、Host 是本机的名字、
   X-Real-IP 是回环。经隧道、局域网的别的设备来的一律 404（和不是管理员一样），页面上也就没有这个按钮。

2. 导入 cookies 那个账号订阅的 YouTube 频道（「UP 主」页，和 B 站的导入关注一个样子，bililogin.py）：

    GET  /api/youtube/subscriptions[?fresh=1]   订阅的频道（节点的 /yt/subscriptions，缓存 10 分钟），标出已订阅的；
                                                 第一次要等节点跑 yt-dlp，先回答 {"state": "loading"}，页面再问
    POST /api/youtube/subscriptions/import      {"ids": ["UC…", …]}：在后台一个个加（每个要取一次投稿列表，十几秒）
    GET  /api/youtube/subscriptions/import      导入进行到哪了

   只在本机模式、有 cookies 时能用；cookies 是这台服务器的（管理员的小号），所以只给管理员，加进管理员自己的订阅。
   受 UP 主个数（state.MAX_CREATORS）和小电视频道列表大小（relay.room）的限制，和 B 站的导入一样。

cookies 的值不进日志、不进回答；回答里只有条数、浏览器配置名和配置里登录的 Google 账号。
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time

from . import ytsettings
from .bilibili import SourceError
from .youtube import channel_url

PREFIX = "/api/youtube/"
GETS = frozenset({"browsers", "subscriptions", "subscriptions/import"})
POSTS = frozenset({"browsers/read", "subscriptions/import"})
LOCAL_HEADER = "X-Front-Local"
# 两次「读取」至少隔几秒。
READ_GAP_S = 3.0
# 订阅列表缓存多久；「刷新」至少隔多久真去问；出错后多久内不再问。
SUBS_CACHE_S = 600
SUBS_FRESH_GAP_S = 60
SUBS_ERROR_GAP_S = 20
# 导入：一次最多选这么多；每个之间歇一下（每个都要节点跑一次 yt-dlp）。
IMPORT_MAX = 200
IMPORT_GAP_S = 0.5
_UCID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_HANDLE_URL = re.compile(r"^https://www\.youtube\.com/(@[^/?#\s]+)$")
_CHANNEL_URL = re.compile(r"^https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})$")

NOT_LOCAL_MODE = "YouTube 要是「本机」模式才能导入：在「设置 → YouTube」选「本机」并保存"
NO_COOKIES = "先在「设置 → YouTube」导入 cookies，或从浏览器读取 YouTube 登录"
LOGIN = "YouTube 说没有登录：cookies 可能过期了，在「设置 → YouTube」重新导入或从浏览器读取（用小号）"
UNREACHABLE = "连不上 YouTube：检查「设置 → YouTube」的代理"


def _loopback(address: str) -> bool:
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def from_this_computer(handler) -> bool:
    """这个请求是不是在运行服务器的这台电脑上直接打开的电脑版（见模块说明）。"""
    from ..front import local_host
    peer = handler.client_address[0] if getattr(handler, "client_address", None) else ""
    headers = handler.headers
    return (_loopback(peer) and headers.get(LOCAL_HEADER) == "1" and local_host(headers.get("Host") or "")
            and _loopback(headers.get("X-Real-IP") or ""))


# ---------------------------------------------------------------- 从浏览器读取

class BrowserReader:
    """一个中转一个：同时只读一个浏览器，结果留给页面来问。"""

    def __init__(self, relay, detect=None, read=None, background: bool = True, clock=time.monotonic):
        from ..selfhost import ytbrowser
        self.relay = relay
        self.detect = detect or ytbrowser.detect
        self.read = read or ytbrowser.read
        self.background = background
        self.clock = clock
        self.lock = threading.Lock()
        self.job = {"state": "idle"}
        self._started = -1e9

    def browsers(self) -> list[dict]:
        try:
            return self.detect()
        except OSError:
            return []

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.job)

    def start(self, browser, profile, caller=None, ip: str = "") -> dict:
        from ..selfhost import ytbrowser, ytlocal
        from .ytnode import read_components
        if not isinstance(browser, str) or not isinstance(profile, str) or len(profile) > 300:
            return {"ok": False, "error": "请求格式不对"}
        found = self.browsers()
        try:
            spec, chosen = ytbrowser.spec_for(found, browser, profile)
        except ytbrowser.BrowserError as error:
            return {"ok": False, "error": str(error)}
        layout = ytsettings._paths(self.relay)
        tools = read_components(str(layout.youtube_bin))
        if tools is None:
            return {"ok": False, "error": "读取要用到 yt-dlp：先在上面「安装」YouTube 组件"}
        config, _ = ytsettings._config(self.relay)
        dest = ytlocal.cookies_path(layout.root, config.youtube_cookies)
        with self.lock:
            if self.job.get("state") == "working":
                return {"ok": False, "error": "正在读取，等它做完"}
            if self.clock() - self._started < READ_GAP_S:
                return {"ok": False, "error": "点得太快了，过几秒再试"}
            self._started = self.clock()
            name = ytbrowser.NAMES[browser]
            self.job = {"state": "working", "browser": browser, "browser_name": name,
                        "profile": chosen["id"], "profile_name": chosen["name"], "account": chosen["account"],
                        "message": "", "count": 0, "at": time.time()}
            job = dict(self.job)
        args = (tools[0], spec, browser, dest, caller, ip)
        if self.background:
            threading.Thread(target=self._run, args=args, daemon=True, name="yt-browser-read").start()
        else:
            self._run(*args)
        return {"ok": True, "job": job if self.background else self.snapshot()}

    def _run(self, ytdlp, spec, browser, dest, caller, ip) -> None:
        from ..selfhost import ytbrowser
        name = ytbrowser.NAMES[browser]
        try:
            result = self.read(ytdlp, spec, browser, dest)
        except ytbrowser.BrowserError as error:
            self._finish("failed", str(error))
            self.relay.log(f"从 {name} 读取 YouTube 登录失败：{error.kind or 'failed'}")
            return
        except Exception as error:  # noqa: BLE001 —— 后台线程：任何意外都要变成页面上的一句话
            self._finish("failed", f"从 {name} 读取失败：{type(error).__name__}")
            self.relay.log(f"从 {name} 读取 YouTube 登录失败：{type(error).__name__}")
            return
        self._finish("done", "", result.get("count", 0))
        self.relay.log(f"从 {name} 读取了 YouTube 登录（{result.get('count', 0)} 条 youtube.com cookies）")
        self.relay._youtube_health = None
        subs = getattr(self.relay, "_yt_subs", None)
        if subs is not None:
            subs.forget()
        accounts = getattr(self.relay, "accounts", None)
        if accounts is not None and caller is not None:
            try:
                accounts.audit("youtube.cookies.browser", caller.user, ip, "browser", browser)
            except Exception:  # noqa: BLE001 —— 记不了操作记录不影响读取的结果
                pass

    def _finish(self, state: str, message: str, count: int = 0) -> None:
        with self.lock:
            self.job = dict(self.job, state=state, message=message, count=count, at=time.time())


def reader(relay) -> BrowserReader:
    found = getattr(relay, "_yt_browser", None)
    if found is None:
        found = BrowserReader(relay)
        relay._yt_browser = found
    return found


def _system_name() -> str:
    import platform
    return {"Darwin": "mac", "Windows": "windows"}.get(platform.system(), "linux")


def browsers_answer(relay) -> dict:
    from ..selfhost import ytbrowser
    from .ytnode import read_components
    job = reader(relay)
    layout = ytsettings._paths(relay)
    return {"ok": True, "system": _system_name(), "browsers": ytbrowser.public(job.browsers()),
            "ytdlp": read_components(str(layout.youtube_bin)) is not None, "job": job.snapshot()}


# ---------------------------------------------------------------- 订阅的频道

def _creator_keys(state) -> set[str]:
    """已经订阅的 YouTube 频道：频道 id（UC…）和小写的 @handle。"""
    keys = set()
    with state.lock:
        sources = [c.source for c in state.creators if c.site == "youtube" and c.kind == "up"]
    for source in sources:
        for pattern in (_CHANNEL_URL, _HANDLE_URL):
            found = pattern.match(source or "")
            if found:
                keys.add(found.group(1) if found.group(1).startswith("UC") else found.group(1).lower())
    return keys


def _subscribed(item: dict, keys: set[str]) -> bool:
    return item["id"] in keys or bool(item.get("handle")) and item["handle"].lower() in keys


class Subscriptions:
    """管理员的 YouTube 订阅列表（缓存）和导入任务。"""

    def __init__(self, relay, background: bool = True, clock=time.monotonic, sleep=time.sleep):
        self.relay = relay
        self.background = background
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self._list: tuple[float, list[dict], str] | None = None
        self._loading = False
        self._error: tuple[float, str, bool] | None = None
        self._fetched = -1e9
        self.job = {"state": "idle"}

    def forget(self) -> None:
        with self.lock:
            self._list, self._error = None, None
            self._fetched = -1e9

    def gate(self) -> str:
        """能不能用；不能时说明原因。"""
        from ..selfhost import ytlocal
        if getattr(self.relay, "youtube_kind", "") != "local" or self.relay.clients.get("youtube") is None:
            return NOT_LOCAL_MODE
        config, _ = ytsettings._config(self.relay)
        path = ytlocal.cookies_path(ytsettings._paths(self.relay).root, config.youtube_cookies)
        if not ytlocal.cookies_summary(path)["present"]:
            return NO_COOKIES
        return ""

    def listing(self, fresh: bool = False) -> dict:
        """{"ok", "state": loading|ready, "items", "source"} 或 {"ok": False, "error", "login"?}。"""
        reason = self.gate()
        if reason:
            return {"ok": False, "error": reason, "gate": True}
        now = self.clock()
        with self.lock:
            cached = self._list
            if self._loading:
                return {"ok": True, "state": "loading"}
            if cached is not None and now - cached[0] < SUBS_CACHE_S and \
                    (not fresh or now - self._fetched < SUBS_FRESH_GAP_S):
                return {"ok": True, "state": "ready", "items": cached[1], "source": cached[2],
                        "age": int(now - cached[0])}
            if self._error is not None and now - self._error[0] < SUBS_ERROR_GAP_S:
                answer = {"ok": False, "error": self._error[1]}
                if self._error[2]:
                    answer["login"] = False
                return answer
            self._loading, self._fetched, self._error = True, now, None
        if self.background:
            threading.Thread(target=self._fetch, daemon=True, name="yt-subscriptions").start()
            return {"ok": True, "state": "loading"}
        self._fetch()
        return self.listing()

    def _fetch(self) -> None:
        client = self.relay.clients.get("youtube")
        try:
            items = client.subscriptions()
        except SourceError as error:
            login = getattr(error, "reason", "") == "login"
            message = LOGIN if login else UNREACHABLE if not error.permanent else "YouTube 拒绝了这次请求，过几分钟再试"
            self.relay.log(f"取 YouTube 订阅失败：{'没有登录' if login else type(error).__name__}")
            if login:
                self.relay._youtube_health = None
            with self.lock:
                self._loading, self._error = False, (self.clock(), message, login)
            return
        except Exception as error:  # noqa: BLE001 —— 后台线程
            self.relay.log(f"取 YouTube 订阅失败：{type(error).__name__}")
            with self.lock:
                self._loading, self._error = False, (self.clock(), "取不到订阅列表，看日志", False)
            return
        with self.lock:
            self._loading = False
            self._list = (self.clock(), items, "")
        self.relay.log(f"取到 YouTube 订阅 {len(items)} 个频道")

    def import_snapshot(self) -> dict:
        with self.lock:
            return dict(self.job, added=list(self.job.get("added", [])), skipped=list(self.job.get("skipped", [])))

    def start_import(self, caller, ids) -> dict:
        reason = self.gate()
        if reason:
            return {"ok": False, "error": reason}
        if not isinstance(ids, list) or not ids or len(ids) > IMPORT_MAX \
                or not all(isinstance(i, str) and _UCID.match(i) for i in ids):
            return {"ok": False, "error": "请求格式不对"}
        with self.lock:
            cached = self._list
            if cached is None or self.clock() - cached[0] >= SUBS_CACHE_S:
                return {"ok": False, "error": "订阅列表过期了：关掉再打开一次"}
            if self.job.get("state") == "working":
                return {"ok": False, "error": "正在导入，稍等"}
            names = {item["id"]: item for item in cached[1]}
            wanted = list(dict.fromkeys(ids))
            self.job = {"state": "working", "total": len(wanted), "done": 0, "current": "",
                        "added": [], "skipped": []}
        if self.background:
            threading.Thread(target=self._import, args=(caller, wanted, names), daemon=True,
                             name="yt-subscriptions-import").start()
        else:
            self._import(caller, wanted, names)
        return {"ok": True, "job": self.import_snapshot()}

    def _step(self, **changes) -> None:
        with self.lock:
            self.job.update(changes)

    def _skip(self, cid: str, name: str, why: str) -> None:
        with self.lock:
            self.job["skipped"].append({"id": cid, "name": name, "reason": why})
            self.job["done"] += 1

    def _import(self, caller, wanted: list[str], names: dict) -> None:
        from .state import MAX_CREATORS
        from .table import label_for
        relay = self.relay
        client = relay.clients.get("youtube")
        added = 0
        state = None
        try:
            state = relay._account(caller)
            keys = _creator_keys(state)
            with state.lock:
                count = sum(c.kind == "up" for c in state.creators)
            for n, cid in enumerate(wanted):
                item = names.get(cid)
                if item is None:
                    self._skip(cid, cid, "不在你的 YouTube 订阅里")
                    continue
                name = item["name"]
                self._step(current=name)
                if _subscribed(item, keys):
                    self._skip(cid, name, "已经订阅了")
                    continue
                if count >= MAX_CREATORS:
                    self._skip(cid, name, f"最多 {MAX_CREATORS} 个 UP 主")
                    continue
                if not relay.room(label_for(name, "youtube"), caller)["fits"]:
                    self._skip(cid, name, "小电视的频道列表满了")
                    continue
                if added or n:
                    self.sleep(IMPORT_GAP_S)
                source = channel_url(cid)
                try:
                    _, videos = client.creator(source)
                except SourceError as error:
                    relay.log(f"导入 YouTube 频道 {cid} 失败：{type(error).__name__} {getattr(error, 'reason', '')}")
                    if error.permanent:
                        self._skip(cid, name, "YouTube 说这个频道看不了")
                        continue
                    self._skip(cid, name, UNREACHABLE)
                    # 连不上：后面的也不会好，别一个个等超时。
                    for rest in wanted[n + 1:]:
                        self._skip(rest, (names.get(rest) or {}).get("name", rest), UNREACHABLE)
                    break
                if not videos:
                    self._skip(cid, name, "没有投稿")
                    continue
                try:
                    creator = state.add("youtube", source, name, videos, relay.clock())
                except ValueError as error:
                    self._skip(cid, name, str(error))
                    continue
                state.set_total(creator.id, relay._upload_total("youtube", source))
                relay._cache_videos(creator, creator.videos, creator.fetched_at)
                keys.add(cid)
                count += 1
                added += 1
                with self.lock:
                    self.job["added"].append({"id": cid, "name": name, "creator": creator.id})
                    self.job["done"] += 1
        except Exception as error:  # noqa: BLE001 —— 后台线程：记下来，页面上说出错了
            relay.log(f"导入 YouTube 订阅出错：{type(error).__name__}")
            self._step(error="导入出错了，看日志")
        finally:
            if added and state is not None:
                state.save()
                relay._tables_changed(state)
                relay.log(f"从 YouTube 订阅导入了 {added} 个频道")
            self._step(state="done", current="")


def subscriptions(relay) -> Subscriptions:
    found = getattr(relay, "_yt_subs", None)
    if found is None:
        found = Subscriptions(relay)
        relay._yt_subs = found
    return found


def listing_answer(relay, caller, fresh: bool) -> dict:
    from .state import MAX_CREATORS
    answer = subscriptions(relay).listing(fresh)
    if answer.get("ok") and answer.get("state") == "ready":
        state = relay._account(caller)
        keys = _creator_keys(state)
        with state.lock:
            count = sum(c.kind == "up" for c in state.creators)
        answer = dict(answer, items=[dict(item, subscribed=_subscribed(item, keys)) for item in answer["items"]],
                      total=len(answer["items"]), count=count, max=MAX_CREATORS, room=relay.room(caller=caller))
    return answer


# ---------------------------------------------------------------- ytsettings.py 转过来的请求

def _local_only(name: str) -> bool:
    return name.startswith("browsers")


def get(handler, relay, path: str) -> bool:
    if not path.startswith(PREFIX) or path[len(PREFIX):] not in GETS:
        return False
    name = path[len(PREFIX):]
    if not ytsettings._allowed(handler, relay) or (_local_only(name) and not from_this_computer(handler)):
        handler._not_found()
        return True
    if name == "browsers":
        handler._json(browsers_answer(relay))
    elif name == "subscriptions":
        handler._json(listing_answer(relay, handler._caller(), handler._query().get("fresh") == "1"))
    else:
        handler._json({"ok": True, "job": subscriptions(relay).import_snapshot()})
    return True


def post(handler, relay, path: str) -> bool:
    if not path.startswith(PREFIX) or path[len(PREFIX):] not in POSTS:
        return False
    name = path[len(PREFIX):]
    body = ytsettings._read_body(handler)
    if not ytsettings._allowed(handler, relay) or (_local_only(name) and not from_this_computer(handler)):
        handler._not_found()
        return True
    if body is None:
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return True
    caller = handler._caller()
    if name == "browsers/read":
        handler._json(reader(relay).start(body.get("browser"), body.get("profile") or "", caller, handler._ip()))
    else:
        handler._json(subscriptions(relay).start_import(caller, body.get("ids")))
    return True
