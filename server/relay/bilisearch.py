"""电脑版「订阅」页的 B 站搜索，和搜索结果封面的缩略图代理。

- GET /api/bili/search?q=…&kind=video|user|bangumi&page=N：一页结果（封面、标题、简介、UP 主、时长、播放数），
  页面据此「订阅 UP 主」（POST /api/creators）、「加为剧集」（POST /api/series），添加还是走原来的接口。
- GET /api/bili/thumb?u=<封面地址>&s=v|f|p：封面缩略图。电脑版的 CSP 只许本站的图片（img-src 'self'），
  B 站图床也拦外站的 Referer，所以由中转去取：只认 *.hdslb.com 的 /bfs/ 图片（bilibili.cover_url），
  不跟随跳转，限大小，结果在内存里缓存一会儿，浏览器也缓存一天。
  YouTube 的缩略图（首页、搜索的封面）也走这里：只认 https://i.ytimg.com/vi/<11 位 id>/<几种固定文件名>.jpg
  （youtube_thumb_url），经 YouTube 用的那个代理去取（本机模式在「设置 → YouTube」里填的；没填就直连），
  连不上时和 B 站图床分开记，互不拖累。

谁能用：和订阅接口一样，要账号（单用户时是登录过的手机或电脑；多用户时是用户，访客 404）。
别打扰 B 站：同样的搜索 10 分钟内直接用缓存；每个人每秒最多真去搜一次，全站每分钟最多 30 次；
页面输入时也会等停手后才搜（console.html）。
"""

from __future__ import annotations

import http.client
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque

from . import bilibili
from .bilibili import SourceError

PATHS = frozenset({"/api/bili/search", "/api/bili/thumb"})
QUERY_MAX = 40
# 搜索结果缓存多久、最多几条；每个人两次真去搜之间至少隔多久；全站每分钟最多搜几次。
SEARCH_CACHE_S = 600
SEARCH_CACHE_SIZE = 64
SEARCH_GAP_S = 1.0
SEARCH_PER_MINUTE = 30
# 缩略图：最大多少字节、等多久、同时最多取几张、内存里最多留多少、全站每分钟最多取几张。
THUMB_MAX_BYTES = 512 * 1024
THUMB_TIMEOUT_S = 6.0
THUMB_PARALLEL = 4
THUMB_CACHE_BYTES = 8 * 1024 * 1024
THUMB_PER_MINUTE = 240
# 图床连不上（网络错误、超时）后这么久内不再去取，直接 502：路由器拦着 B 站时不至于每张图都等一遍。
THUMB_DOWN_S = 30
THUMB_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"})
# 让图床缩好再给（B 站图床的「@宽w_高h_1c.webp」后缀）：视频封面、头像、番剧竖版海报。
THUMB_SIZES = {"v": "@320w_200h_1c.webp", "f": "@96w_96h_1c.webp", "p": "@180w_240h_1c.webp"}

# YouTube 缩略图：只认这一个主机、这几种文件名（不带查询串）。
YOUTUBE_THUMB_HOST = "i.ytimg.com"
_YOUTUBE_THUMB = re.compile(r"^/vi/[A-Za-z0-9_-]{11}/(?:default|mqdefault|hqdefault|sddefault|maxresdefault)\.jpg$")

UNREACHABLE = "B 站连不上：检查路由器或代理"
REFUSED = "B 站拒绝了这次搜索（可能是搜得太频繁），请过几分钟再试"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None     # 跳转到哪都不跟：只取白名单里的那个地址


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_image(url: str, timeout: float) -> tuple[str, bytes]:
    """(类型, 内容)；多读一个字节，好知道是不是超过了上限。"""
    request = urllib.request.Request(url, headers={"User-Agent": bilibili.UA, "Referer": bilibili.REFERER})
    with _OPENER.open(request, timeout=timeout) as answer:
        return answer.headers.get_content_type(), answer.read(THUMB_MAX_BYTES + 1)


def youtube_thumb_url(raw) -> str:
    """i.ytimg.com 的缩略图地址 → https 地址；不是就给空串。"""
    text = str(raw or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    try:
        url = urllib.parse.urlsplit(text)
        port = url.port
    except ValueError:
        return ""
    if url.scheme not in ("http", "https") or port is not None or url.username or url.password \
            or url.query or url.fragment or (url.hostname or "").lower() != YOUTUBE_THUMB_HOST:
        return ""
    if not _YOUTUBE_THUMB.match(url.path):
        return ""
    return f"https://{YOUTUBE_THUMB_HOST}{url.path}"


def fetch_youtube_image(url: str, timeout: float, proxy: str = "") -> tuple[str, bytes]:
    """经 YouTube 的代理（http:// 或 socks5://，见 ytnode.open_tunnel）取 i.ytimg.com 的图；没有代理就直连。"""
    headers = {"User-Agent": bilibili.UA}
    if not proxy:
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout) as answer:
            return answer.headers.get_content_type(), answer.read(THUMB_MAX_BYTES + 1)
    from .ytnode import open_tunnel, parse_proxy
    tunnel = open_tunnel(parse_proxy(proxy), YOUTUBE_THUMB_HOST, 443, timeout)
    tunnel.settimeout(timeout)
    connection = http.client.HTTPSConnection(YOUTUBE_THUMB_HOST, timeout=timeout)
    connection.sock = ssl.create_default_context().wrap_socket(tunnel, server_hostname=YOUTUBE_THUMB_HOST)
    try:
        connection.request("GET", urllib.parse.urlsplit(url).path, headers=headers)
        answer = connection.getresponse()
        if answer.status != 200:
            raise OSError(f"HTTP {answer.status}")
        return answer.headers.get_content_type(), answer.read(THUMB_MAX_BYTES + 1)
    finally:
        connection.close()


class _Window:
    """最近 60 秒里做了几次（全站的次数上限）。"""

    def __init__(self, most: int):
        self.most = most
        self.times: deque[float] = deque()

    def take(self, now: float) -> bool:
        while self.times and now - self.times[0] >= 60:
            self.times.popleft()
        if len(self.times) >= self.most:
            return False
        self.times.append(now)
        return True


class BiliSearch:
    def __init__(self, clock=time.monotonic, fetch=fetch_image, log=lambda _m: None,
                 fetch_youtube=fetch_youtube_image):
        self.clock = clock
        self.fetch = fetch
        self.fetch_youtube = fetch_youtube
        # YouTube 缩略图用的代理（web.Relay 设成读「设置 → YouTube」的那个函数）；返回空串就是直连。
        self.youtube_proxy = lambda: ""
        self.log = log
        self.lock = threading.Lock()
        self._results: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._last: dict[tuple, float] = {}
        self._searches = _Window(SEARCH_PER_MINUTE)
        self._thumbs: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self._thumb_bytes = 0
        self._thumb_window = _Window(THUMB_PER_MINUTE)
        self._gate = threading.BoundedSemaphore(THUMB_PARALLEL)
        self._down_until = 0.0
        self._yt_down_until = 0.0

    # ---- 搜索 ----

    def search(self, client, who: tuple, query, kind, page) -> dict:
        query = " ".join(str(query or "").split())
        kind = kind or "video"
        try:
            page = int(page or 1)
        except (TypeError, ValueError):
            page = 0
        if not query:
            return {"ok": False, "error": "请输入要搜的内容"}
        if len(query) > QUERY_MAX or kind not in bilibili.BROWSE_KINDS or not 1 <= page <= bilibili.BROWSE_PAGES:
            return {"ok": False, "error": "请求格式不对"}
        if client is None:
            return {"ok": False, "error": "B 站还没配置好"}
        key = (kind, query.lower(), page)
        now = self.clock()
        with self.lock:
            cached = self._results.get(key)
            if cached is not None and now - cached[0] < SEARCH_CACHE_S:
                self._results.move_to_end(key)
                return cached[1]
            if now - self._last.get(who, -SEARCH_GAP_S) < SEARCH_GAP_S:
                return {"ok": False, "error": "搜得太快了，稍等一下", "retry": SEARCH_GAP_S}
            if not self._searches.take(now):
                return {"ok": False, "error": "大家搜得太频繁了，请过一分钟再试"}
            self._last[who] = now
            if len(self._last) > 1000:
                self._last = {k: v for k, v in self._last.items() if now - v < SEARCH_GAP_S}
        try:
            found = client.browse(query, kind, page)
        except SourceError as error:
            self.log(f"B 站搜索失败：{error}")
            return {"ok": False, "error": REFUSED if error.permanent else UNREACHABLE}
        answer = {"ok": True, "q": query, "kind": kind, **found}
        with self.lock:
            self._results[key] = (now, answer)
            while len(self._results) > SEARCH_CACHE_SIZE:
                self._results.popitem(last=False)
        return answer

    # ---- 缩略图 ----

    def thumb(self, raw, size) -> tuple[int, str, bytes]:
        """(HTTP 码, 类型, 内容)。不在白名单里的地址 404；取不到 502；太多了 503。"""
        youtube = youtube_thumb_url(raw)
        url = youtube or bilibili.cover_url(raw)
        suffix = "" if youtube else THUMB_SIZES.get(size or "v")
        if not url or suffix is None:
            return 404, "", b""
        target = url + suffix
        with self.lock:
            hit = self._thumbs.get(target)
            if hit is not None:
                self._thumbs.move_to_end(target)
                return 200, hit[0], hit[1]
            if self.clock() < (self._yt_down_until if youtube else self._down_until):
                return 502, "", b""
            if not self._thumb_window.take(self.clock()):
                return 503, "", b""
        if not self._gate.acquire(timeout=THUMB_TIMEOUT_S):
            return 503, "", b""
        try:
            if youtube:
                kind, body = self.fetch_youtube(target, THUMB_TIMEOUT_S, self.youtube_proxy() or "")
            else:
                kind, body = self.fetch(target, THUMB_TIMEOUT_S)
        except (OSError, ValueError) as error:
            if not isinstance(error, urllib.error.HTTPError):
                with self.lock:
                    if youtube:
                        self._yt_down_until = self.clock() + THUMB_DOWN_S
                    else:
                        self._down_until = self.clock() + THUMB_DOWN_S
                self.log(f"{'YouTube 缩略图' if youtube else 'B 站图床'}连不上（{type(error).__name__}），"
                         f"{THUMB_DOWN_S} 秒内不再取")
            return 502, "", b""
        finally:
            self._gate.release()
        if kind not in THUMB_TYPES or not body or len(body) > THUMB_MAX_BYTES:
            return 502, "", b""
        with self.lock:
            if target not in self._thumbs:
                self._thumbs[target] = (kind, body)
                self._thumb_bytes += len(body)
            while self._thumb_bytes > THUMB_CACHE_BYTES and self._thumbs:
                _, (_, old) = self._thumbs.popitem(last=False)
                self._thumb_bytes -= len(old)
        return 200, kind, body


def get(handler, relay, caller, path: str) -> None:
    """web.py 的 GET 转过来（path 在 PATHS 里）。访客、没登录的 NotFound（404 页），和订阅接口一样。"""
    relay._account(caller)
    query = handler._query()
    if path == "/api/bili/search":
        handler._json(relay.bili_search.search(relay.clients.get("bilibili"), caller.key, query.get("q"),
                                               query.get("kind"), query.get("page")))
        return
    code, kind, body = relay.bili_search.thumb(query.get("u"), query.get("s"))
    if code != 200:
        handler._send(code, b"", "text/plain; charset=utf-8")
        return
    handler._send(200, body, kind, {"Cache-Control": "private, max-age=86400"})
