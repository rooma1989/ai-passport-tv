"""在电脑上点播 YouTube：像 B 站那样秒开、拖到哪里都快（vod.py 的 YouTube 那一半）。

以前 YouTube 在电脑上只有一条路：节点（ytnode.py）的 /yt/stream 先跑 yt-dlp 取地址（要过 JS 验证），再起 ffmpeg
按实时速度出 MPEG-TS，中转的 watch.py 再转成 HLS——第一段出来之前窗口里什么都没有，每拖一次进度条整条链重来一遍。
YouTube 自己的网页放的是 DASH：画面、声音各是一个分片 MP4（只有画面的 H.264 / 只有声音的 AAC），文件头里有 sidx
（每一段的时长和字节数），浏览器按时间算出字节范围、用 Range 取、喂给 MediaSource。这里照着做：

- 取地址（Resolver）：节点的 /yt/formats 给出 yt-dlp 解析到的全部格式和直连地址。按视频 id 留在内存里（LRU，
  RESOLVE_CACHE_MAX 个），留到地址里的 expire 前 EXPIRE_MARGIN_S（最多 RESOLVE_MAX_AGE_S）。同一个视频同时只解析
  一次：起播碰上正在预取的同一个视频就等它的结果。
- 预取（Prefetcher）：电脑版把首页、搜索页上看得见的前几个 YouTube 视频、鼠标停住或焦点到的那个发过来
  （POST /api/yt/prefetch），这里排队、一次一个（节点本来就一次只跑一个 yt-dlp）、每分钟至多 PREFETCH_PER_MINUTE 个，
  顺便把默认清晰度那两条轨的索引也取好。预取绝不耽误真正的播放：有起播在取地址时队列停着；节点那边，预取的
  yt-dlp 正在跑时来了真正的请求，节点把预取杀掉让路（ytnode.Node._take）。
- 选格式（choose）：浏览器能放的一对——只有画面的 H.264（avc1，mp4）≤ 选的清晰度里最高的（360p / 480p / 720p /
  1080p，没有不超过的就取最低的那个），加上只有声音的 AAC（mp4a，m4a，原声优先、码率最高）。只有 VP9 / AV1、
  浏览器没有 MediaSource 时退到合体的 360p 文件（格式 18：H.264 + AAC 一个 MP4，<video> 直接放，和 B 站一样）；
  再没有就照旧走 watch.py 的 HLS。
- 索引（read_index）：yt-dlp 给了 init_range / index_range 就按它取；没给（现在的 yt-dlp 不给）就读文件头
  （HEAD_BYTES，不够再接着读）：ftyp、moov（初始化段到它为止）、sidx。只认一层的 sidx。按 (视频, 格式) 缓存：
  地址换了文件还是那个文件。
- 上游（Fetcher）：googlevideo 的地址绑着取地址时的出口 IP，所以本机模式下中转取字节也必须走「设置 → YouTube」
  里的同一个代理（http 或 socks5，经 ytnode.open_tunnel 打通道）。连接留着复用（POOL_IDLE_S），一段一个请求时
  不用每次都经代理握手。签名和 vod.VodService 的 opener 一样，vod.py 转发 Range 时直接用它。
- 只在本机模式（[youtube] mode = "local"）有：用 cstone 节点时地址绑的是 cstone 的 IP，这台电脑取不了，照旧 HLS。
  云端的中转没有在电脑上看。旧节点没有 /yt/formats（404 Not Found）时也照旧 HLS。

地址里有签名：从不写进日志（写「<地址>」）。只用标准库。
"""

from __future__ import annotations

import base64
import collections
import http.client
import io
import re
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from . import vod
from .bilibili import SourceError
from .ytnode import open_tunnel, parse_proxy

# 清晰度菜单：短边的像素数。
LADDER = (360, 480, 720, 1080)
QUALITIES = tuple(f"{p}p" for p in LADDER)
DEFAULT_QUALITY = "720p"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# 取地址的结果留几个、留到地址过期前多久、最多留多久；地址里没写过期时间时留多久。
RESOLVE_CACHE_MAX = 64
EXPIRE_MARGIN_S = 30 * 60
RESOLVE_MAX_AGE_S = 5 * 3600
NO_EXPIRE_S = 20 * 60
# 起播时等正在预取（或别的页面正在取）的同一个视频最多多久。
FLIGHT_WAIT_S = 90.0
# 索引：按 (视频, 格式) 留几个；先读文件头多少字节，最多读到多少还没见到 sidx 就算没有。
INDEX_CACHE_MAX = 128
HEAD_BYTES = 64 * 1024
HEAD_MAX = 4 * 1024 * 1024
# 初始化段不超过这么大时，取索引时顺手留下它的字节，清单里直接带给页面（省掉页面再经代理取一次）。
INIT_INLINE_MAX = 64 * 1024
UPSTREAM_TIMEOUT_S = vod.UPSTREAM_TIMEOUT_S
# 预取：同时几个、排队至多几个、每分钟至多几个、一次请求至多几个 id；有起播在取地址时最多等多久再接着预取。
PREFETCH_PARALLEL = 1
PREFETCH_QUEUE = 8
PREFETCH_PER_MINUTE = 12
PREFETCH_IDS_MAX = 6
PREFETCH_TIMEOUT_S = 90.0
PAUSE_WAIT_S = 60.0
# 节点正忙（正在给起播、搜索、列表跑 yt-dlp，或者刚把这次预取挤掉）：这个视频排回队首，等一会儿再试，至多几次。
BUSY_RETRY_S = 1.5
BUSY_TRIES = 3
# Resolver.prefetch 的结果：节点正忙。
BUSY = "busy"
# 到上游的连接：空闲多久以后不再用、每个主机留几条；跟几次跳转。
POOL_IDLE_S = 30.0
POOL_PER_HOST = 6
REDIRECTS = 3
_EXPIRE = re.compile(r"[?&/]expire[=/](\d{9,11})(?:\D|$)")


# ---------------------------------------------------------------- sidx

class BadIndex(ValueError):
    """文件头里找不到能用的 sidx（没有、分层的、坏了）：这一对格式不能按 DASH 放。"""


@dataclass(frozen=True)
class Index:
    """一条轨的索引。init：初始化段（ftyp + moov）的字节范围（含两头）；times、offsets 各 n + 1 项：
    第 i 段从 times[i] 秒放到 times[i + 1] 秒，字节从 offsets[i] 到 offsets[i + 1] − 1。
    head：初始化段本身（读文件头时已经取到了；太大或没取全时为空）。"""
    init: tuple[int, int]
    times: tuple[float, ...]
    offsets: tuple[int, ...]
    head: bytes = field(default=b"", repr=False, compare=False)

    @property
    def count(self) -> int:
        return len(self.times) - 1

    @property
    def duration(self) -> float:
        return self.times[-1]


def _box_header(data: bytes, at: int) -> tuple[int, bytes, int] | None:
    """data[at:] 开头的盒子：(大小, 类型, 头长)；字节不够看出头时 None。"""
    if len(data) < at + 8:
        return None
    size, kind = struct.unpack_from(">I4s", data, at)
    if size == 1:
        if len(data) < at + 16:
            return None
        return struct.unpack_from(">Q", data, at + 8)[0], kind, 16
    return size, kind, 8


def parse_sidx(box: bytes, at: int) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """一个完整的 sidx 盒子（在文件的 at 处）→ (times, offsets)。分层的（引用另一个 sidx）不认。"""
    head = _box_header(box, 0)
    if head is None or head[1] != b"sidx" or head[0] > len(box):
        raise BadIndex("sidx 不完整")
    size, _, hdr = head
    try:
        version = box[hdr]
        pos = hdr + 4
        _ref, timescale = struct.unpack_from(">II", box, pos)
        pos += 8
        if version == 0:
            earliest, first = struct.unpack_from(">II", box, pos)
            pos += 8
        else:
            earliest, first = struct.unpack_from(">QQ", box, pos)
            pos += 16
        count = struct.unpack_from(">H", box, pos + 2)[0]
        pos += 4
        if not timescale or not count or pos + 12 * count > size:
            raise BadIndex("sidx 是空的")
        offset, ticks = at + size + first, earliest
        times, offsets = [ticks / timescale], [offset]
        for n in range(count):
            word, duration, _sap = struct.unpack_from(">III", box, pos + 12 * n)
            if word >> 31:
                raise BadIndex("分层的 sidx")
            offset += word & 0x7FFFFFFF
            ticks += duration
            times.append(ticks / timescale)
            offsets.append(offset)
    except struct.error:
        raise BadIndex("sidx 坏了") from None
    return tuple(times), tuple(offsets)


def _head(data: bytes, size: int) -> bytes:
    """文件头里的初始化段（前 size 个字节）：取全了、不太大时给出，否则空。"""
    return bytes(data[:size]) if 0 < size <= min(len(data), INIT_INLINE_MAX) else b""


def read_index(fetch: Callable[[int, int], bytes], init_range=None, index_range=None) -> Index:
    """fetch(起, 止) 取文件的一段（含两头，到文件尾会短）。给了 init_range、index_range（yt-dlp 的）就一次取完；
    否则从头读盒子：moov 以前是初始化段，接着找 sidx，先见到 moof / mdat 就是没有。"""
    if init_range and index_range and init_range[0] == 0 and index_range[0] > init_range[1]:
        data = fetch(0, index_range[1])
        box = data[index_range[0]:index_range[1] + 1]
        times, offsets = parse_sidx(box, index_range[0])
        return Index((0, int(init_range[1])), times, offsets, _head(data, int(init_range[1]) + 1))
    data, eof = b"", False

    def more(upto: int) -> bool:
        nonlocal data, eof
        while len(data) < upto:
            if eof or len(data) >= HEAD_MAX:
                return False
            want = max(upto, len(data) + HEAD_BYTES) - len(data)
            chunk = fetch(len(data), len(data) + want - 1)
            eof = len(chunk) < want      # 文件比要的短：到头了，不用再问
            data += chunk
        return True

    pos, moov_end = 0, None
    more(HEAD_BYTES)
    while True:
        if not more(pos + 16) and len(data) < pos + 8:
            raise BadIndex("文件头里没有 sidx")
        head = _box_header(data, pos)
        if head is None:
            raise BadIndex("文件头里没有 sidx")
        size, kind, _hdr = head
        if size < 8:
            raise BadIndex("盒子的大小不对")
        if kind == b"moov":
            moov_end = pos + size
        elif kind == b"sidx":
            if moov_end is None:
                raise BadIndex("sidx 在 moov 前面")
            if not more(pos + size):
                raise BadIndex("sidx 不完整")
            times, offsets = parse_sidx(data[pos:pos + size], pos)
            return Index((0, moov_end - 1), times, offsets, _head(data, moov_end))
        elif kind in (b"moof", b"mdat"):
            raise BadIndex("文件头里没有 sidx")
        pos += size
        if pos >= HEAD_MAX:
            raise BadIndex("文件头太长")


# ---------------------------------------------------------------- 选格式

@dataclass(frozen=True)
class Choice:
    """选中的放法。kind："dash"（video + audio 两条轨）或 "progressive"（muxed 一个合体文件）。
    current：给人看的清晰度（"720p"）；choices：菜单里能选的（只有 DASH 有）。"""
    kind: str
    video: dict | None = None
    audio: dict | None = None
    muxed: dict | None = None
    current: str = ""
    choices: tuple[str, ...] = ()

    @property
    def variant(self) -> str:
        if self.kind == "dash":
            return f"dash:{self.video.get('format_id')}+{self.audio.get('format_id')}"
        return f"file:{self.muxed.get('format_id')}"

    @property
    def headers(self) -> dict:
        for f in (self.video, self.audio, self.muxed):
            if f and isinstance(f.get("headers"), dict):
                return f["headers"]
        return {}


def _none(codec) -> bool:
    return str(codec or "none") == "none"


def _lines(f: dict) -> int:
    """格式的「多少 p」：format_note 里写的（1080p60 → 1080），没有时取短边（竖屏视频的 1080x1920 是 1080p）。"""
    found = re.match(r"^(\d{3,4})p", str(f.get("format_note") or ""))
    if found:
        return int(found.group(1))
    width, height = int(f.get("width") or 0), int(f.get("height") or 0)
    return min(width, height) if width and height else height


def _direct(f: dict) -> bool:
    return str(f.get("url") or "").startswith(("https://", "http://")) and str(f.get("protocol") or "https") in ("https", "http")


def is_video(f: dict) -> bool:
    return _direct(f) and str(f.get("vcodec") or "").startswith("avc1") and _none(f.get("acodec")) \
        and f.get("ext") == "mp4"


def is_audio(f: dict) -> bool:
    return _direct(f) and str(f.get("acodec") or "").startswith("mp4a") and _none(f.get("vcodec")) \
        and f.get("ext") in ("m4a", "mp4")


def is_muxed(f: dict) -> bool:
    return _direct(f) and str(f.get("vcodec") or "").startswith("avc1") and str(f.get("acodec") or "").startswith("mp4a") \
        and f.get("ext") == "mp4"


def label(lines: int) -> str:
    return f"{lines}p" if lines else ""


def quality_lines(quality: str) -> int:
    return int(quality[:-1]) if quality in QUALITIES else int(DEFAULT_QUALITY[:-1])


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def pick_video(formats: list[dict], quality: str) -> dict | None:
    """≤ quality 的里最高的（同样高时帧率高、码率高的）；都比它高时取最低的（同样高时帧率低、码率低的：省）。"""
    videos = [f for f in formats if is_video(f) and _lines(f)]
    if not videos:
        return None
    want = quality_lines(quality)
    under = [f for f in videos if _lines(f) <= want]
    if under:
        return max(under, key=lambda f: (_lines(f), _num(f.get("fps")), _num(f.get("tbr"))))
    return min(videos, key=lambda f: (_lines(f), _num(f.get("fps")), _num(f.get("tbr"))))


def pick_audio(formats: list[dict]) -> dict | None:
    """原声优先（yt-dlp 的 language_preference），不要动态范围压缩过的（-drc），码率最高的。"""
    audios = [f for f in formats if is_audio(f)]
    if not audios:
        return None
    return max(audios, key=lambda f: (_num(f.get("language_preference")), "drc" not in str(f.get("format_id") or ""),
                                      _num(f.get("abr") or f.get("tbr"))))


def choose(formats: list[dict], quality: str = DEFAULT_QUALITY, dash: bool = True) -> tuple[Choice | None, str]:
    """(选中的放法, 原因)：放不了时选中的是 None，原因给日志看（不含地址）。dash：页面能用 MediaSource 放
    H.264 + AAC 的分片 MP4；不能时只看合体文件。"""
    formats = [f for f in formats or [] if isinstance(f, dict)]
    why = ""
    if dash:
        video, audio = pick_video(formats, quality), pick_audio(formats)
        if video is not None and audio is not None:
            choices = tuple(q for q in QUALITIES
                            if any(is_video(f) and _lines(f) == quality_lines(q) for f in formats))
            return Choice("dash", video=video, audio=audio, current=label(_lines(video)), choices=choices), ""
        if video is None:
            others = sorted({str(f.get("vcodec") or "").split(".")[0] for f in formats
                             if not _none(f.get("vcodec")) and _none(f.get("acodec")) and f.get("vcodec")})
            why = f"画面只有 {'/'.join(others)}" if others else "没有单独的画面"
        else:
            why = "没有 AAC 的声音"
    muxed = [f for f in formats if is_muxed(f)]
    if muxed:
        under = [f for f in muxed if _lines(f) <= 720] or muxed
        best = max(under, key=lambda f: (_lines(f), _num(f.get("tbr"))))
        return Choice("progressive", muxed=best, current=label(_lines(best))), why
    return None, (why + "，" if why else "") + "也没有 H.264 + AAC 的合体文件"


def same_choice(formats: list[dict], choice: Choice) -> Choice | None:
    """重新取到的格式里找同样的那几个（按格式编号）：地址换了，文件还是那些。找不到时 None。"""
    by_id = {str(f.get("format_id")): f for f in formats or [] if isinstance(f, dict) and _direct(f)}
    if choice.kind == "dash":
        video, audio = by_id.get(str(choice.video.get("format_id"))), by_id.get(str(choice.audio.get("format_id")))
        if video is None or audio is None:
            return None
        return Choice("dash", video=video, audio=audio, current=choice.current, choices=choice.choices)
    muxed = by_id.get(str(choice.muxed.get("format_id")))
    return Choice("progressive", muxed=muxed, current=choice.current) if muxed is not None else None


def expires_at(url: str) -> float | None:
    """googlevideo 地址里的过期时刻（Unix 秒）；没写时 None。"""
    found = _EXPIRE.search(url or "")
    return float(found.group(1)) if found else None


def keep_until(info: dict, now: float) -> float:
    """这次取地址的结果能用到什么时候：地址里最早的过期时刻 − EXPIRE_MARGIN_S，至多 RESOLVE_MAX_AGE_S 后。"""
    times = [t for t in (expires_at(str(f.get("url") or "")) for f in info.get("formats") or []
                         if isinstance(f, dict)) if t]
    if not times:
        return now + NO_EXPIRE_S
    return min(min(times) - EXPIRE_MARGIN_S, now + RESOLVE_MAX_AGE_S)


# ---------------------------------------------------------------- 到上游（经代理，连接复用）

class _Answer:
    """一个上游回答，样子像 urllib 的：status、headers、getcode()、read1()、read()、close()。
    读完了、上游没说要关时 close() 把连接放回池子。"""

    def __init__(self, fetcher: "Fetcher", key: tuple, conn: http.client.HTTPConnection,
                 response: http.client.HTTPResponse):
        self._fetcher, self._key, self._conn, self._response = fetcher, key, conn, response
        self.status = response.status
        self.reason = response.reason
        self.headers = response.headers
        self._closed = False

    def getcode(self) -> int:
        return self.status

    def read1(self, size: int = -1) -> bytes:
        try:
            return self._response.read1(size)
        except http.client.HTTPException as error:
            raise OSError(f"上游断了：{type(error).__name__}") from None

    def read(self, size: int | None = None) -> bytes:
        try:
            return self._response.read(size) if size is not None else self._response.read()
        except http.client.HTTPException as error:
            raise OSError(f"上游断了：{type(error).__name__}") from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._response.isclosed() and not self._response.will_close:
            self._fetcher._give(self._key, self._conn)
        else:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class Fetcher:
    """到 googlevideo 的 HTTP(S) GET：proxy 是「设置 → YouTube」里的代理（http:// 或 socks5://，空串直连）。
    调用的样子和 vod.VodService 的 opener 一样：fetcher(urllib.request.Request, timeout) → 回答；
    400 以上抛 urllib.error.HTTPError；跳转（同样经代理）至多 REDIRECTS 次。"""

    def __init__(self, proxy: str = "", clock=time.monotonic):
        self.proxy_url = proxy or ""
        self.proxy = parse_proxy(proxy) if proxy else None
        self.clock = clock
        self._idle: dict[tuple, list[tuple[float, http.client.HTTPConnection]]] = {}
        self._lock = threading.Lock()
        self._context = ssl.create_default_context()
        # 新建了几条连接（测试看复用了没有）。
        self.connects = 0

    def _connect(self, scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        if self.proxy is not None:
            sock = open_tunnel(self.proxy, host, port, timeout)
            sock.settimeout(timeout)
        else:
            sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if scheme == "https":
                sock = self._context.wrap_socket(sock, server_hostname=host)
        except BaseException:
            sock.close()
            raise
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = sock
        self.connects += 1
        return conn

    def _take(self, key: tuple) -> http.client.HTTPConnection | None:
        now = self.clock()
        with self._lock:
            idle = self._idle.get(key) or []
            while idle:
                at, conn = idle.pop()
                if now - at < POOL_IDLE_S:
                    return conn
                conn.close()
        return None

    def _give(self, key: tuple, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            idle = self._idle.setdefault(key, [])
            idle.append((self.clock(), conn))
            while len(idle) > POOL_PER_HOST:
                idle.pop(0)[1].close()

    def close(self) -> None:
        with self._lock:
            pools, self._idle = self._idle, {}
        for idle in pools.values():
            for _, conn in idle:
                conn.close()

    def _once(self, url: str, headers: dict, timeout: float) -> _Answer:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("上游地址不对")
        port = parts.port or (443 if scheme == "https" else 80)
        host = parts.hostname
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        key = (scheme, host, port)
        for attempt in (0, 1):
            conn = self._take(key) if attempt == 0 else None
            reused = conn is not None
            try:
                if conn is None:
                    conn = self._connect(scheme, host, port, timeout)
                else:
                    conn.sock.settimeout(timeout)
                conn.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", host if port == (443 if scheme == "https" else 80) else f"{host}:{port}")
                for name, value in headers.items():
                    if name.lower() not in ("host", "connection"):
                        conn.putheader(name, value)
                conn.endheaders()
                return _Answer(self, key, conn, conn.getresponse())
            except (OSError, http.client.HTTPException) as error:
                if conn is not None:
                    conn.close()
                if reused:
                    continue        # 池子里的连接上游早关了：换一条新的再试一次
                if isinstance(error, OSError):
                    raise
                raise OSError(f"上游：{type(error).__name__}") from None
        raise OSError("上游连不上")

    def __call__(self, request: urllib.request.Request, timeout: float = UPSTREAM_TIMEOUT_S) -> _Answer:
        url = request.full_url
        headers = dict(request.header_items())
        for _hop in range(REDIRECTS + 1):
            answer = self._once(url, headers, timeout)
            location = answer.headers.get("Location")
            if answer.status in (301, 302, 303, 307, 308) and location:
                answer.read(64 * 1024)
                answer.close()
                url = urllib.parse.urljoin(url, location)
                continue
            if answer.status >= 400:
                headers_seen = answer.headers
                answer.read(64 * 1024)
                answer.close()
                raise urllib.error.HTTPError(url, answer.status, answer.reason or "", headers_seen, io.BytesIO(b""))
            return answer
        raise OSError("上游跳转太多次")


# ---------------------------------------------------------------- 取地址的缓存

class _Flight:
    def __init__(self, prefetch: bool):
        self.prefetch = prefetch
        self.done = threading.Event()
        self.result: dict | None = None


class Resolver:
    """按视频 id 缓存节点 /yt/formats 的回答（见模块说明）。client()：现在的 YouTube 客户端（youtube.YouTube）。"""

    def __init__(self, client: Callable[[], object], clock=time.time, log=print, size: int = RESOLVE_CACHE_MAX):
        self.client = client
        self.clock = clock
        self.log = log
        self.size = size
        self._cache: collections.OrderedDict[str, tuple[float, dict]] = collections.OrderedDict()
        self._flights: dict[str, _Flight] = {}
        self._lock = threading.Lock()
        self._plays = 0

    def busy(self) -> bool:
        """有起播正在取地址：预取先停着。"""
        with self._lock:
            return self._plays > 0

    def cached(self, video_id: str) -> dict | None:
        with self._lock:
            hit = self._cache.get(video_id)
            if hit is None:
                return None
            if self.clock() >= hit[0]:
                del self._cache[video_id]
                return None
            self._cache.move_to_end(video_id)
            return hit[1]

    def forget(self, video_id: str) -> None:
        with self._lock:
            self._cache.pop(video_id, None)

    def _store(self, video_id: str, info: dict) -> None:
        until = keep_until(info, self.clock())
        with self._lock:
            self._cache[video_id] = (until, info)
            self._cache.move_to_end(video_id)
            while len(self._cache) > self.size:
                self._cache.popitem(last=False)

    def resolve(self, video_id: str, fresh: bool = False) -> dict:
        """起播（或地址过期后重取，fresh）：缓存里有就用；同一个视频正在取（预取或别的页面）就等它；
        否则马上问节点（节点那边正在跑的预取会让路）。失败抛 SourceError。"""
        if not fresh:
            hit = self.cached(video_id)
            if hit is not None:
                return hit
        flight = None
        with self._lock:
            self._plays += 1
            existing = None if fresh else self._flights.get(video_id)
            if existing is None:
                flight = _Flight(prefetch=False)
                self._flights.setdefault(video_id, flight)
        try:
            if existing is not None:
                existing.done.wait(FLIGHT_WAIT_S)
                if existing.result is not None:
                    return existing.result
                flight = _Flight(prefetch=False)      # 它没成（预取被挤掉了、节点忙）：自己去取
            client = self.client()
            if client is None or getattr(client, "formats", None) is None:
                raise SourceError("YouTube 还没配置", reason="unsupported")
            info = client.formats(video_id, fresh=fresh)
            self._store(video_id, info)
            flight.result = info
            return info
        finally:
            with self._lock:
                self._plays -= 1
                if flight is not None and self._flights.get(video_id) is flight:
                    del self._flights[video_id]
            if flight is not None:
                flight.done.set()

    def prefetch(self, video_id: str) -> dict | str | None:
        """预取一个：已经有了、正在取时不做（None）；节点忙（或被真正的请求挤掉）时是 BUSY；成功时给结果。"""
        if self.cached(video_id) is not None:
            return None
        with self._lock:
            if video_id in self._flights:
                return None
            flight = self._flights[video_id] = _Flight(prefetch=True)
        try:
            client = self.client()
            if client is None or getattr(client, "formats", None) is None:
                return None
            try:
                info = client.formats(video_id, prefetch=True, timeout=PREFETCH_TIMEOUT_S)
            except SourceError as error:
                if getattr(error, "reason", "") == "busy":
                    return BUSY
                self.log(f"YouTube 预取 {video_id} 没成：{str(error)[:120]}")
                return None
            self._store(video_id, info)
            flight.result = info
            return info
        finally:
            with self._lock:
                if self._flights.get(video_id) is flight:
                    del self._flights[video_id]
            flight.done.set()


class _Lru:
    def __init__(self, size: int):
        self.size = size
        self._items: collections.OrderedDict = collections.OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self._items:
                return None
            self._items.move_to_end(key)
            return self._items[key]

    def put(self, key, value) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)


# ---------------------------------------------------------------- 预取

class Prefetcher:
    """排队预取（见模块说明）。work(id, 清晰度)：做一个，节点正忙时返回 BUSY（排回队首，过 BUSY_RETRY_S 再试）；
    busy()：有起播在取地址时为真（先停着）。"""

    def __init__(self, work: Callable[[str, str], None], busy: Callable[[], bool] = lambda: False,
                 clock=time.monotonic, log=print, parallel: int = PREFETCH_PARALLEL, queue_max: int = PREFETCH_QUEUE,
                 per_minute: int = PREFETCH_PER_MINUTE, sleep=time.sleep):
        self.work = work
        self.busy = busy
        self.clock = clock
        self.log = log
        self.parallel = parallel
        self.queue_max = queue_max
        self.per_minute = per_minute
        self.sleep = sleep
        self._queue: collections.deque[tuple[str, str]] = collections.deque()
        self._recent: collections.deque[float] = collections.deque()
        self._workers = 0
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def want(self, ids: list[str], quality: str = DEFAULT_QUALITY) -> int:
        """排上这些（排在前面的先做；新来的排在旧的前面，太多时丢最旧的）。返回排上了几个。"""
        ids = [v for v in dict.fromkeys(ids) if isinstance(v, str) and _VIDEO_ID.match(v)][:PREFETCH_IDS_MAX]
        with self._lock:
            for vid in ids:
                for item in [x for x in self._queue if x[0] == vid]:
                    self._queue.remove(item)
            self._queue.extendleft(reversed([(vid, quality, 0) for vid in ids]))
            while len(self._queue) > self.queue_max:
                self._queue.pop()
            start = min(self.parallel - self._workers, len(self._queue))
            self._workers += max(0, start)
            if self._workers:
                self._idle.clear()
        for _ in range(max(0, start)):
            threading.Thread(target=self._run, name="yt-prefetch", daemon=True).start()
        return len(ids)

    def pending(self) -> list[str]:
        with self._lock:
            return [item[0] for item in self._queue]

    def wait_idle(self, timeout: float) -> bool:
        return self._idle.wait(timeout)

    def _next(self) -> tuple[str, str, int] | None:
        waited = 0.0
        while self.busy() and waited < PAUSE_WAIT_S:
            self.sleep(0.2)
            waited += 0.2
        with self._lock:
            now = self.clock()
            while self._recent and now - self._recent[0] >= 60:
                self._recent.popleft()
            if not self._queue or len(self._recent) >= self.per_minute:
                if self._queue:
                    self._queue.clear()     # 这一分钟预取得够多了：剩下的不做（页面过一会儿还会再要）
                self._workers -= 1
                if not self._workers:
                    self._idle.set()
                return None
            self._recent.append(now)
            return self._queue.popleft()

    def _run(self) -> None:
        while True:
            item = self._next()
            if item is None:
                return
            vid, quality, tries = item
            try:
                outcome = self.work(vid, quality)
            except Exception as error:  # noqa: BLE001 —— 预取只是锦上添花，后台线程不能死
                self.log(f"YouTube 预取出错：{type(error).__name__}")
                continue
            if outcome == BUSY and tries + 1 < BUSY_TRIES:
                with self._lock:
                    if not any(x[0] == vid for x in self._queue):
                        self._queue.appendleft((vid, quality, tries + 1))
                    if self._recent:
                        self._recent.pop()      # 没做成的不算进每分钟的次数
                self.sleep(BUSY_RETRY_S)


# ---------------------------------------------------------------- 合起来

def _range(value) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, int) for v in value):
        return int(value[0]), int(value[1])
    return None


def track_of(f: dict, kind: str) -> vod.Track:
    codec = str(f.get("vcodec") if kind == "v" else f.get("acodec"))
    return vod.Track(url=str(f["url"]), mime=f'{"video" if kind == "v" else "audio"}/mp4; codecs="{codec}"',
                     size=int(_num(f.get("filesize"))), itag=str(f.get("format_id") or ""),
                     init=_range(f.get("init_range")), index=_range(f.get("index_range")),
                     height=_lines(f) if kind == "v" else 0, bitrate=int(_num(f.get("tbr")) * 1000))


def media_of(choice: Choice, info: dict) -> vod.Media:
    duration = _num(info.get("duration"))
    if choice.kind == "dash":
        return vod.Media(url="", duration=duration,
                         tracks={"v": track_of(choice.video, "v"), "a": track_of(choice.audio, "a")})
    return vod.Media(url=str(choice.muxed["url"]), size=int(_num(choice.muxed.get("filesize"))), duration=duration)


def upstream_headers(choice: Choice) -> tuple[tuple[str, str], ...]:
    """yt-dlp 给这些格式的 http 头（User-Agent 之类，取地址时用的哪个客户端就是哪个）；Range 等由转发时加。"""
    return tuple((str(k), str(v)) for k, v in choice.headers.items()
                 if str(k).lower() not in ("range", "accept-encoding", "host", "connection", "cookie"))


class DashIndexes:
    """取 DASH 索引、给页面清单的那一半（YouTube 的 YtVod 和 B 站长视频的 bilidash.BiliDash 共用）：
    indexes 是按 (视频, 格式) 缓存的索引（_Lru），fetcher() 是上游的连接。清单的样子见 manifest。"""

    indexes: "_Lru"

    def fetcher(self):
        raise NotImplementedError

    def _index(self, video_id: str, track: vod.Track, current: Callable, headers: dict, opener) -> Index:
        """一条轨的索引（缓存）。current(refresh)：现在的那条轨（refresh 为真时先重新取地址）。"""
        cached = self.indexes.get((video_id, track.itag))
        if cached is not None:
            return cached

        def fetch(start: int, end: int) -> bytes:
            for attempt in (0, 1):
                url = current(refresh=attempt == 1).url
                request = urllib.request.Request(url, headers={**headers, "Range": f"bytes={start}-{end}",
                                                               "Accept-Encoding": "identity"})
                try:
                    answer = opener(request, UPSTREAM_TIMEOUT_S)
                except urllib.error.HTTPError as error:
                    if error.code == 416:
                        return b""
                    if error.code in vod.EXPIRED and attempt == 0:
                        continue
                    raise OSError(f"HTTP {error.code}") from None
                try:
                    return answer.read(end - start + 1)
                finally:
                    answer.close()
            return b""

        found = read_index(fetch, track.init, track.index)
        self.indexes.put((video_id, track.itag), found)
        return found

    def manifest(self, session: vod.VodSession) -> dict:
        """DASH 的清单：{"duration", "tracks": {"v"|"a": {"mime", "init": [起, 止], "t": [...], "o": [...],
        "height", "bitrate", "ib"（初始化段的字节，base64；没留下时没有）}}}。两条轨的索引同时取。"""
        media = session.media()
        video_id = session.source.key[3]
        opener = session.source.opener or self.fetcher()
        headers = dict(session.source.headers)
        found: dict[str, Index] = {}
        errors: list[Exception] = []

        def one(kind: str) -> None:
            def current(refresh: bool = False) -> vod.Track:
                return session.media(refresh=refresh).tracks[kind]
            try:
                found[kind] = self._index(video_id, media.tracks[kind], current, headers, opener)
            except Exception as error:  # noqa: BLE001 —— 交给 manifest 的调用方说原因
                errors.append(error)

        threads = [threading.Thread(target=one, args=(kind,), daemon=True) for kind in ("v", "a")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(UPSTREAM_TIMEOUT_S * 3)
        if errors or len(found) < 2:
            raise errors[0] if errors else OSError("取索引超时")
        tracks = {}
        for kind in ("v", "a"):
            track, index = media.tracks[kind], found[kind]
            tracks[kind] = {"mime": track.mime, "init": list(index.init),
                            "t": [round(t, 3) for t in index.times], "o": list(index.offsets),
                            "height": track.height, "bitrate": track.bitrate}
            if index.head:
                # 初始化段的字节（base64）：页面直接喂给 MediaSource，不用再取 init 那一段。
                tracks[kind]["ib"] = base64.b64encode(index.head).decode("ascii")
        duration = max(found["v"].duration, found["a"].duration)
        return {"duration": round(duration, 3), "tracks": tracks}


class YtVod(DashIndexes):
    """web.py 用的这一套：client() 是现在的 YouTube 客户端，proxy() 是现在的 YouTube 代理（空串直连）。"""

    def __init__(self, client: Callable[[], object], proxy: Callable[[], str] = lambda: "", log=print,
                 clock=time.time, fetcher=Fetcher):
        self.log = log
        self.proxy = proxy
        self.resolver = Resolver(client, clock=clock, log=log)
        self.indexes = _Lru(INDEX_CACHE_MAX)
        self.prefetcher = Prefetcher(self._prefetch_one, self.resolver.busy, log=log)
        self._make_fetcher = fetcher
        self._fetchers: dict[str, Fetcher] = {}
        self._lock = threading.Lock()
        self.clock = clock

    def fetcher(self) -> Fetcher:
        """现在的代理对应的上游连接（代理改了就换一套；同一个代理的连接复用）。"""
        try:
            proxy = self.proxy() or ""
        except Exception:  # noqa: BLE001 —— 读不了设置就直连（和缩略图一样）
            proxy = ""
        with self._lock:
            found = self._fetchers.get(proxy)
            if found is None:
                for old in self._fetchers.values():
                    old.close()
                self._fetchers = {proxy: self._make_fetcher(proxy)}
                found = self._fetchers[proxy]
            return found

    def prefetch(self, ids: list[str], quality: str = DEFAULT_QUALITY) -> int:
        return self.prefetcher.want(ids, quality if quality in QUALITIES else DEFAULT_QUALITY)

    def _prefetch_one(self, video_id: str, quality: str):
        info = self.resolver.prefetch(video_id)
        if info is None or info == BUSY:
            return info
        choice, _ = choose(info.get("formats") or [], quality, dash=True)
        if choice is None or choice.kind != "dash":
            return
        opener, headers = self.fetcher(), dict(upstream_headers(choice))
        for kind, f in (("v", choice.video), ("a", choice.audio)):
            if self.resolver.busy():
                return          # 有人起播了：索引留给起播时取
            track = track_of(f, kind)
            try:
                self._index(video_id, track, lambda refresh=False, t=track: t, headers, opener)
            except (OSError, ValueError, urllib.error.URLError):
                return

    # ---- 起播 ----

    def source(self, key: tuple, video, position: float, quality: str, dash: bool):
        """这个 YouTube 视频在电脑上点播的片源（vod.VodSource）；放不了（没有浏览器能放的格式）时 None，
        取地址失败抛 SourceError（web.py 据此决定说原因还是退回 HLS）。"""
        video_id = video.bvid
        info = self.resolver.resolve(video_id)
        choice, why = choose(info.get("formats") or [], quality, dash=dash)
        if choice is None:
            self.log(f"在电脑上点播 YouTube {video_id}：{why}，转 HLS")
            return None
        if why:
            self.log(f"在电脑上点播 YouTube {video_id}：{why}，放 {choice.current} 的合体文件")
        resolver = self.resolver

        def resolve() -> vod.Media:
            again = resolver.resolve(video_id, fresh=True)
            same = same_choice(again.get("formats") or [], choice)
            if same is None:
                raise vod.NotPlayable("格式变了")
            return media_of(same, again)

        dashed = choice.kind == "dash"
        return vod.VodSource(key, resolve=resolve, headers=upstream_headers(choice), label=video.title,
                             site="youtube", start=position,
                             duration=float(video.duration or info.get("duration") or 0),
                             first=media_of(choice, info), quality=choice.current,
                             choices=choice.choices if dashed else (), opener=self.fetcher(), dash=dashed,
                             manifest=self.manifest if dashed else None, variant=choice.variant,
                             refresh_s=max(600.0, keep_until(info, self.clock()) - self.clock()))
