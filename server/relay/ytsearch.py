"""电脑版「搜索」页的 YouTube 搜索：GET /api/yt/search?q=…&kind=video|channel|playlist。

经 YouTube 节点的 /yt/search（youtube.YouTube.browse → ytnode.py：yt-dlp --flat-playlist -J），
云端是 cstone 上的节点，电脑客户端、家庭服务器的本机模式是本机的 __ytnode 子进程（用户的代理和 cookies）。
页面拿到结果后：「播放」走 POST /api/play（{kind: "cast", url: 视频链接}，投到小电视或这台电脑），
「订阅频道」走 POST /api/creators（频道链接），「加为剧集」走 POST /api/series（播放列表链接）。

谁能用：和 B 站搜索（bilisearch.py）一样要账号（访客、没登录的 404）。

别拖慢播放：节点一次只跑一个 yt-dlp（起播也要用它），所以——
- 同样的搜索 10 分钟内直接用缓存；同一个搜索正在搜时，后来的人等它的结果，不再去问节点；
- 整个中转同时只有一个 YouTube 搜索在节点上跑，别的搜索马上回答「稍等」（不排队）；
- 每个人两次真去搜之间至少隔 GAP_S 秒，全站每分钟最多 PER_MINUTE 次；
- 节点正在给电视解析视频时它自己回答 503 busy（ytnode.SEARCH_WAIT_S），这里照说「节点正忙」。

失败时 reason 让页面分开说：off（没开 YouTube）、login（要导入或更新 cookies）、tools（组件没装好）、
busy（节点正忙或别的搜索在跑）、slow（超时；本机模式另有 slow：ytdlp = yt-dlp 自己启动就慢、proxy = 慢在代理）、down（连不上）、unsupported（旧节点不会搜播放列表）、
rate（搜得太快）、bad（请求格式不对）。

封面：视频的缩略图是 i.ytimg.com 的固定地址，经 /api/bili/thumb 代理（只认 i.ytimg.com）。
云端不给封面——阿里云从不直连 YouTube（README「YouTube」一节），页面画占位图。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from .bilibili import SourceError
from .bilisearch import _Window, youtube_thumb_url
from .youtube import _ID, _PLAYLIST, _UCID, channel_url

PATHS = frozenset({"/api/yt/search"})
KINDS = ("video", "channel", "playlist")
QUERY_MAX = 60
COUNT = 20
TIMEOUT_S = 25.0
CACHE_S = 600
CACHE_SIZE = 64
GAP_S = 3.0
PER_MINUTE = 12
# 节点正忙、别的搜索在跑时，页面等几秒再试一次。
BUSY_RETRY_S = 5
TITLE_MAX = 200
NAME_MAX = 60

# 去哪儿开 YouTube：电脑客户端、家庭服务器的管理员在「设置 → YouTube」。
OFF_SELFHOST = "YouTube 还没开启：去「设置 → YouTube」开启本机模式并填代理"
OFF_CLOUD = "这台服务器还没有配置 YouTube"
LOGIN_LOCAL = "YouTube 要求登录：需要导入 cookies（「设置 → YouTube」里导入 cookies.txt）"
LOGIN_CLOUD = "YouTube 需要更新 cookies（请管理员用小号重新导出），B 站不受影响"
TOOLS = "YouTube 组件还没装好：去「设置 → YouTube」安装"
BUSY = "YouTube 节点正忙，稍后再试"
RUNNING = "上一个 YouTube 搜索还没完，稍等几秒"
SLOW = f"YouTube 搜索超时了（等了 {int(TIMEOUT_S)} 秒）：代理慢或连不上，过一会儿再试"
# 本机模式超时时分开说：节点量过 yt-dlp --version 要几秒（ytnode 的 /yt/health 的 startup），
# 超过 SLOW_START_S 秒就是 yt-dlp 自己慢（以前的单文件版在 Mac 上要 37 秒），否则慢在代理或 YouTube。
SLOW_START_S = 5.0
HEALTH_TIMEOUT_S = 3.0
SLOW_YTDLP = ("YouTube 搜索超时了（等了 {wait} 秒）：是本机的 yt-dlp 自己慢（光启动就要 {s} 秒），不是代理的问题。"
              "电脑很忙或安全软件在检查它时会这样；在「设置 → YouTube」点「检查更新」可以重新装 yt-dlp")
SLOW_PROXY = ("YouTube 搜索超时了（等了 {wait} 秒）：yt-dlp 启动正常（{s} 秒），慢在代理或 YouTube："
              "检查「设置 → YouTube」里的代理，或点那里的「测试」")
DOWN_LOCAL = "YouTube 连不上：检查「设置 → YouTube」里的代理，或点那里的「测试」"
DOWN_LOCAL_NODE = "本机的 YouTube 组件没有响应：稍后再试，或在「设置 → YouTube」点「测试」"
DOWN_CLOUD = "YouTube 暂时搜不了（连不上海外节点），稍后再试"
UNSUPPORTED = "这个 YouTube 节点还不会搜播放列表（节点更新后就能用）；可以搜视频或频道"


def _text(value, most: int) -> str:
    return " ".join(str(value or "").split())[:most]


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def shape(kind: str, answer: dict, covers: bool = True) -> list[dict]:
    """节点的回答 → 页面的结果。视频 {"kind": "video", "id", "title", "channel", "channel_id",
    "channel_link", "duration", "views", "date", "cover", "link"}；频道 {"kind": "channel", "id", "name",
    "fans", "videos", "link"}；播放列表 {"kind": "playlist", "id", "title", "channel", "count", "cover",
    "link"}。不合规的 id、空标题丢掉；旧节点没有的项（播放数、上传时间、频道 id）为 0 / 空串。"""
    items, seen = [], set()
    if kind == "video":
        for v in answer.get("videos") or []:
            if not isinstance(v, dict) or not _ID.match(str(v.get("id", ""))) or v["id"] in seen:
                continue
            title = _text(v.get("title"), TITLE_MAX)
            duration = _count(v.get("duration"))
            if not title or not duration:
                continue
            seen.add(v["id"])
            cid = str(v.get("channel_id") or "")
            cid = cid if _UCID.match(cid) else ""
            items.append({"kind": "video", "id": v["id"], "title": title,
                          "channel": _text(v.get("uploader"), NAME_MAX), "channel_id": cid,
                          "channel_link": channel_url(cid) if cid else "",
                          "duration": duration, "views": _count(v.get("views")), "date": _count(v.get("date")),
                          "cover": f"https://i.ytimg.com/vi/{v['id']}/mqdefault.jpg" if covers else "",
                          "link": f"https://www.youtube.com/watch?v={v['id']}"})
    elif kind == "channel":
        for c in answer.get("channels") or []:
            if not isinstance(c, dict) or not _UCID.match(str(c.get("id", ""))) or c["id"] in seen:
                continue
            name = _text(c.get("name"), NAME_MAX)
            if not name:
                continue
            seen.add(c["id"])
            items.append({"kind": "channel", "id": c["id"], "name": name, "fans": _count(c.get("fans")),
                          "videos": _count(c.get("videos")), "link": channel_url(c["id"])})
    else:
        for p in answer.get("playlists") or []:
            if not isinstance(p, dict) or not _PLAYLIST.match(str(p.get("id", ""))) or p["id"] in seen:
                continue
            title = _text(p.get("title"), TITLE_MAX)
            if not title:
                continue
            seen.add(p["id"])
            items.append({"kind": "playlist", "id": p["id"], "title": title,
                          "channel": _text(p.get("channel"), NAME_MAX), "count": _count(p.get("count")),
                          "cover": youtube_thumb_url(p.get("thumb")) if covers else "",
                          "link": f"https://www.youtube.com/playlist?list={p['id']}"})
    return items[:COUNT]


def slow_message(startup, wait: float = TIMEOUT_S) -> str:
    """超时的说法。startup：本机节点量的 yt-dlp 启动秒数（不知道时 None）。"""
    if isinstance(startup, bool) or not isinstance(startup, (int, float)) or startup < 0:
        return SLOW if wait == TIMEOUT_S else f"YouTube 搜索超时了（等了 {int(wait)} 秒）：代理慢或连不上，过一会儿再试"
    template = SLOW_YTDLP if startup >= SLOW_START_S else SLOW_PROXY
    return template.format(wait=int(wait), s=f"{startup:g}")


def explain(error: SourceError, kind: str, place: str, startup=None) -> dict:
    """节点的失败 → {"ok": False, "reason", "error"[, "retry"]}。place：local（本机模式）、selfhost（家庭服务器
    用自己的节点）、cloud。startup：本机节点量的 yt-dlp 启动秒数（超时时用来分开说）。"""
    status = getattr(error, "status", None)
    text = str(error)
    cloud = place == "cloud"
    if error.reason == "busy":
        return {"ok": False, "reason": "busy", "error": BUSY, "retry": BUSY_RETRY_S}
    if error.reason == "slow":
        if place == "local" and startup is not None:
            slow_reason = "ytdlp" if startup >= SLOW_START_S else "proxy"
            return {"ok": False, "reason": "slow", "slow": slow_reason, "error": slow_message(startup)}
        return {"ok": False, "reason": "slow", "error": SLOW}
    if status == 503 and "tools missing" in text:
        return {"ok": False, "reason": "tools", "error": TOOLS}
    if status == 503:
        # yt-dlp 的「Sign in to confirm you're not a bot」（ytnode.error_status）。
        return {"ok": False, "reason": "login", "error": LOGIN_CLOUD if cloud else LOGIN_LOCAL}
    if status == 400 and kind == "playlist":
        return {"ok": False, "reason": "unsupported", "error": UNSUPPORTED}
    if status is None:
        # 节点本身连不上（本机的 __ytnode 没起来，或连不上 cstone）。
        return {"ok": False, "reason": "down", "error": DOWN_CLOUD if cloud else DOWN_LOCAL_NODE}
    return {"ok": False, "reason": "down", "error": DOWN_CLOUD if cloud else DOWN_LOCAL}


class YtSearch:
    def __init__(self, clock=time.monotonic, log=lambda _m: None):
        self.clock = clock
        self.log = log
        self.lock = threading.Lock()
        self._results: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._last: dict[tuple, float] = {}
        self._window = _Window(PER_MINUTE)
        # 正在节点上跑的那一个：(键, 搜完时置位的 Event, 放回答的格子)。
        self._running: tuple[tuple, threading.Event, list] | None = None

    def search(self, client, who: tuple, query, kind, place: str = "cloud") -> dict:
        query = " ".join(str(query or "").split())
        kind = kind or "video"
        if not query:
            return {"ok": False, "reason": "bad", "error": "请输入要搜的内容"}
        if len(query) > QUERY_MAX or kind not in KINDS or any(ord(ch) < 32 or ord(ch) == 127 for ch in query):
            return {"ok": False, "reason": "bad", "error": "请求格式不对"}
        if client is None:
            return {"ok": False, "reason": "off", "error": OFF_CLOUD if place == "cloud" else OFF_SELFHOST}
        key = (kind, query.lower())
        now = self.clock()
        with self.lock:
            cached = self._results.get(key)
            if cached is not None and now - cached[0] < CACHE_S:
                self._results.move_to_end(key)
                return cached[1]
            running = self._running
            if running is not None and running[0] == key:
                waiter = running
            elif running is not None:
                return {"ok": False, "reason": "busy", "error": RUNNING, "retry": BUSY_RETRY_S}
            else:
                waiter = None
                if now - self._last.get(who, -GAP_S) < GAP_S:
                    return {"ok": False, "reason": "rate", "error": "搜得太快了，稍等一下", "retry": GAP_S}
                if not self._window.take(now):
                    return {"ok": False, "reason": "rate", "error": "大家搜得太频繁了，请过一分钟再试"}
                self._last[who] = now
                if len(self._last) > 1000:
                    self._last = {k: v for k, v in self._last.items() if now - v < GAP_S}
                mine = (key, threading.Event(), [])
                self._running = mine
        if waiter is not None:
            # 同一个搜索别人刚发出去：等它的结果。
            if not waiter[1].wait(TIMEOUT_S + 2) or not waiter[2]:
                return {"ok": False, "reason": "slow", "error": SLOW}
            return waiter[2][0]
        try:
            answer = self._ask(client, query, kind, place)
        finally:
            with self.lock:
                self._running = None
        mine[2].append(answer)
        mine[1].set()
        return answer

    def _ask(self, client, query: str, kind: str, place: str) -> dict:
        try:
            found = client.browse(query, kind, COUNT, TIMEOUT_S)
        except SourceError as error:
            self.log(f"YouTube 搜索失败：{error}")
            startup = startup_seconds(client) if error.reason == "slow" and place == "local" else None
            if startup is not None:
                self.log(f"YouTube 搜索超时；yt-dlp 启动要 {startup} 秒")
            return explain(error, kind, place, startup)
        except Exception as error:  # noqa: BLE001 —— 节点回答了看不懂的东西：说搜不了，不让请求 500
            self.log(f"YouTube 搜索失败：{type(error).__name__}: {error}")
            return explain(SourceError(str(error)), kind, place)
        answer = {"ok": True, "q": query, "kind": kind,
                  "items": shape(kind, found if isinstance(found, dict) else {}, covers=place != "cloud")}
        with self.lock:
            self._results[(kind, query.lower())] = (self.clock(), answer)
            while len(self._results) > CACHE_SIZE:
                self._results.popitem(last=False)
        return answer


def startup_seconds(client) -> float | None:
    """本机节点量过的 yt-dlp 启动秒数（/yt/health 的 startup）；问不到、没量过时 None。"""
    try:
        value = client.health(timeout=HEALTH_TIMEOUT_S).get("startup")
    except Exception:   # noqa: BLE001 —— 只是为了把超时说清楚，问不到就照旧说
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def place_of(relay) -> str:
    """local：本机模式；selfhost：家庭服务器、电脑客户端用自己的节点或没开；cloud：云端。"""
    if getattr(relay, "youtube_kind", "") == "local":
        return "local"
    return "selfhost" if getattr(relay, "selfhost", None) is not None else "cloud"


def get(handler, relay, caller, path: str) -> None:
    """web.py 的 GET 转过来（path 在 PATHS 里）。访客、没登录的 NotFound（404 页），和 B 站搜索一样。"""
    relay._account(caller)
    query = handler._query()
    handler._json(relay.yt_search.search(relay.clients.get("youtube"), caller.key, query.get("q"),
                                         query.get("kind"), place_of(relay)))
