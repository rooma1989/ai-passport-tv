"""在电脑上点播：B 站的视频直接放原文件，浏览器自带的 <video> 放，中转只按 Range 转发字节。

为什么：watch.py 那条路（ffmpeg → 分片 MP4 → 内存里的 HLS）是给直播做的。点播的视频走它，起播要等 ffmpeg
连片源、出第一段；跳到内存窗口外就得重起 ffmpeg；也没法倍速、画中画。B 站给 html5 平台的地址本来就是一个
完整的 MP4（H.264 + AAC，最高 720p），浏览器自己就能放、能跳、能倍速——只是要 Referer 和 UA，跨域也取不到，
所以由中转同源转发：

    GET /tv/vod/<编号>/<令牌>/media     （前端去掉 /tv）

- 浏览器发的 Range 原样带给 B 站的 CDN，回答的 206、Content-Range、Content-Length、Content-Type 原样回给浏览器，
  再加上 Accept-Ranges: bytes。边收边发（每次 64 KB），不落盘；浏览器断开（跳转时它会掐掉旧的请求）就关掉上游。
- B 站的地址几个小时后过期：上游回 403 / 404 / 410 时重新取一次地址（VodSource.resolve），这一段请求再试一次；
  地址取到超过 URL_REFRESH_S 秒也先换新的。备用地址（backup_url）在主地址连不上时用。
- 名额和 watch.py 的一起算（每个用户 MAX_PER_USER 路，全中转 MAX_STREAMS 路）：一个页面一路，同一个页面换成
  HLS 时这一路关掉，反过来也一样（web.py）。每一路同时至多 MAX_CONNECTIONS 个请求，再来一个就掐掉最早的
  （浏览器跳转时有时还没关旧的连接）。没有请求、没有进度报告 IDLE_S 秒就收掉；页面关闭时马上收（/api/watch/stop）。
- 令牌和 watch.py 的一样（位置 + 编号 + 到期时间的 HMAC），密钥派生得不同：两种地址的令牌不能互换。
- 只在自建 / 电脑客户端（--selfhost）有，和 watch.py 一样；云端没有这个接口。
- 放不了原文件的（HEVC、FLV、分成好几段的老视频、登录后要 1080P 的、YouTube、直播）照旧走 watch.py
  （web.py 的 _vod_source 决定，playable 说原因）。

只用标准库；地址里有签名，日志里只写「<地址>」。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .. import listen as listen_audio
from . import watch as watch_pc

# 多久没有请求、没有进度报告就收掉这一路（暂停时浏览器不取数据；页面每分钟报一次进度）。
IDLE_S = 180.0
TOKEN_LIFETIME_S = watch_pc.TOKEN_LIFETIME_S
# 地址取到多久以后先换新的（B 站的地址大约两小时过期）。
URL_REFRESH_S = 90 * 60
# 每一路同时至多几个请求。
MAX_CONNECTIONS = 4
CHUNK = 64 * 1024
UPSTREAM_TIMEOUT_S = 20
# 上游这些回答说明地址过期了（或被换了节点）：重新取地址再试一次。
EXPIRED = (403, 404, 410)
_SID = re.compile(r"^[0-9a-f]{16}$")
_PATH = re.compile(r"^/vod/([0-9a-f]{16})/([0-9]{1,12}\.[A-Za-z0-9_-]{16,32})/media$")
_RANGE = re.compile(r"^bytes=(\d{0,15})-(\d{0,15})$")
# B 站 playurl 的 video_codecid：7 是 H.264，12 是 HEVC，13 是 AV1。
CODEC_AVC = 7
_CODECS = {12: "HEVC", 13: "AV1"}


def derive_secret(key: str) -> bytes:
    """令牌的密钥：从中转的密钥派生，和 watch.py、「用手机听」的都不同。"""
    return hashlib.sha256(b"ai-passport-tv vod v1\0" + key.strip().encode("utf-8")).digest()


# ---------------------------------------------------------------- 片源

@dataclass(frozen=True)
class Media:
    """取到的一个原文件：地址、备用地址、大小（字节，不知道为 0）、时长（秒）。"""
    url: str
    backups: tuple[str, ...] = ()
    size: int = 0
    duration: float = 0.0


@dataclass(frozen=True)
class VodSource:
    """一个点播的视频。key 和 watch.Source 的一样（("video", 台, 网站, id)），认「是不是同一个」。

    resolve：取（或重新取）地址，返回 Media；first：决定走点播时已经取到的那一次（第一次请求不用再取）。
    headers：上游要的头（B 站：Referer、User-Agent）。start：从第几秒起（播放器自己跳过去）。
    quality：给人看的清晰度（"720p"）；hd：这个人还能选 1080P（登录了 B 站，走 watch.py 那条路）。"""
    key: tuple
    resolve: Callable[[], Media] = field(compare=False)
    headers: tuple[tuple[str, str], ...] = ()
    label: str = ""
    site: str = ""
    start: float = 0.0
    duration: float = 0.0
    first: Media | None = field(default=None, compare=False)
    quality: str = "720p"
    hd: bool = False


def playable(info: dict) -> str:
    """浏览器能不能直接放这个文件（info 是 bilibili.play_info 的结果）：能放是空串，否则是原因（给日志看的，
    不含地址）。要求只有一段、是 MP4、画面是 H.264（B 站 html5 平台给的就是这样）。"""
    url = str(info.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return "没有播放地址"
    if info.get("preview"):
        return "只能试看"
    if info.get("segments") != 1:
        return f"分成了 {info.get('segments')} 段"
    codec = info.get("codec") or 0
    if codec and codec != CODEC_AVC:
        return f"画面是 {_CODECS.get(codec, codec)}"
    kind = str(info.get("format") or "").lower()
    if kind and not kind.startswith("mp4"):
        return f"格式是 {kind}"
    if not kind and not urllib.parse.urlsplit(url).path.lower().endswith(".mp4"):
        return "不是 MP4"
    return ""


def media_of(info: dict) -> Media:
    return Media(url=info["url"], backups=tuple(info.get("backups") or ()), size=int(info.get("size") or 0),
                 duration=float(info.get("duration") or 0))


class NotPlayable(RuntimeError):
    """重新取到的地址浏览器放不了了（少见：B 站换了格式）。"""


# ---------------------------------------------------------------- 一路

class _Conn:
    """一个正在转发的请求：掐掉它（cancel）就关掉到浏览器的连接，转发的线程下一次写时退出、关上游。"""

    def __init__(self, sock):
        self.sock = sock
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class VodSession:
    """一路点播：一个页面（device 是 web.py 的「pc<用户>-<页面>」）在放的一个视频。"""

    def __init__(self, sid: str, device: str, source: VodSource, user: int | None, clock=time.monotonic):
        self.sid = sid
        self.device = device
        self.source = source
        self.user = user
        self.clock = clock
        self.created = self.seen = clock()
        self.stopped = False
        self.error = ""
        self._media = source.first
        self._resolved = clock() if source.first is not None else 0.0
        self._lock = threading.Lock()
        self._conns: list[_Conn] = []
        # 一共转了多少字节（给日志和测试看）。
        self.sent = 0

    @property
    def alive(self) -> bool:
        return not self.stopped

    def touch(self) -> None:
        self.seen = self.clock()

    def busy(self) -> bool:
        with self._lock:
            return bool(self._conns)

    def media(self, refresh: bool = False) -> Media:
        """现在用的地址；refresh 或者取到太久了就重新取（可能抛 SourceError、NotPlayable）。"""
        with self._lock:
            stale = self._media is None or self.clock() - self._resolved > URL_REFRESH_S
        if not refresh and not stale:
            return self._media
        fresh = self.source.resolve()
        with self._lock:
            self._media, self._resolved = fresh, self.clock()
        return fresh

    def admit(self, sock, limit: int = MAX_CONNECTIONS) -> _Conn:
        conn = _Conn(sock)
        with self._lock:
            self._conns.append(conn)
            oldest = self._conns[:-limit] if len(self._conns) > limit else []
            self._conns = self._conns[len(oldest):]
        for old in oldest:
            old.cancel()
        return conn

    def release(self, conn: _Conn) -> None:
        with self._lock:
            if conn in self._conns:
                self._conns.remove(conn)
        self.touch()

    def stop(self) -> None:
        self.stopped = True
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            conn.cancel()


# ---------------------------------------------------------------- 管所有路

def _open(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)


class VodService:
    """所有页面的点播。watch：watch.WatchService（名额一起算：它的 max_streams、max_per_user）。"""

    def __init__(self, secret: bytes, watch=None, log=print, clock=time.monotonic, wall=time.time,
                 opener=_open, idle_s: float = IDLE_S, background: bool = True,
                 max_streams: int = watch_pc.MAX_STREAMS, max_per_user: int = watch_pc.MAX_PER_USER,
                 max_connections: int = MAX_CONNECTIONS):
        self.secret = secret
        self.watch = watch
        self.log = log
        self.clock = clock
        self.wall = wall
        self.opener = opener
        self.idle_s = idle_s
        self.background = background
        self.max_connections = max_connections
        self._max_streams = max_streams
        self._max_per_user = max_per_user
        self._by_device: dict[str, VodSession] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        if watch is not None:
            watch.also = self.load

    @property
    def max_streams(self) -> int:
        return self.watch.max_streams if self.watch is not None else self._max_streams

    @property
    def max_per_user(self) -> int:
        return self.watch.max_per_user if self.watch is not None else self._max_per_user

    def load(self) -> list[tuple[str, int | None]]:
        """正在点播的各路：(位置, 用户)。watch.WatchService.open 数名额时把它们也算上。"""
        with self._lock:
            return [(d, s.user) for d, s in self._by_device.items() if s.alive]

    def open(self, device: str, source: VodSource, user: int | None = None) -> VodSession:
        """device 这个位置要点播 source：同一个视频接着用这一路（编号、令牌不变；从第几秒起由播放器自己跳），
        别的就换一路。这个位置上的 HLS（watch.py）先关掉。满了时 watch.Busy。"""
        with self._lock:
            current = self._by_device.get(device)
            if current is not None and current.alive and current.source.key == source.key:
                current.touch()
                return current
            others = [s.user for d, s in self._by_device.items() if d != device and s.alive]
        if self.watch is not None:
            others += [s.user for s in self.watch.streams() if s.device != device and s.alive]
        if len(others) >= self.max_streams:
            raise watch_pc.Busy()
        if user is not None and others.count(user) >= self.max_per_user:
            raise watch_pc.Busy(per_user=True)
        if self.watch is not None:
            self.watch.close(device)
        session = VodSession(secrets.token_hex(8), device, source, user, clock=self.clock)
        with self._lock:
            old = self._by_device.pop(device, None)
            self._by_device[device] = session
            self._ensure_reaper()
        if old is not None:
            old.stop()
        self.log(f"在电脑上点播 {device}：开始 {source.label or source.key[-1]}")
        return session

    def token(self, session: VodSession) -> str:
        return listen_audio.issue_token(self.secret, f"{session.device}/{session.sid}", now=self.wall(),
                                        lifetime_s=TOKEN_LIFETIME_S)

    def find(self, sid: str, token: str | None) -> VodSession | None:
        if not _SID.match(sid or ""):
            return None
        with self._lock:
            session = next((s for s in self._by_device.values() if s.sid == sid), None)
        if session is None or not listen_audio.check_token(self.secret, f"{session.device}/{sid}", token,
                                                          now=self.wall()):
            return None
        return session

    def get(self, device: str) -> VodSession | None:
        with self._lock:
            return self._by_device.get(device)

    def close(self, device: str) -> bool:
        with self._lock:
            session = self._by_device.pop(device, None)
        if session is None:
            return False
        session.stop()
        self.log(f"在电脑上点播 {device}：关了")
        return True

    def sessions(self) -> list[VodSession]:
        with self._lock:
            return list(self._by_device.values())

    def reap(self) -> None:
        """收掉 idle_s 秒没有请求、也没有正在转发的。"""
        now = self.clock()
        with self._lock:
            idle = [(d, s) for d, s in self._by_device.items() if now - s.seen > self.idle_s and not s.busy()]
            for device, _ in idle:
                del self._by_device[device]
        for device, session in idle:
            self.log(f"在电脑上点播 {device}：{int(self.idle_s)} 秒没人看，收了")
            session.stop()

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._by_device.values())
            self._by_device.clear()
        for session in sessions:
            session.stop()

    def _ensure_reaper(self) -> None:
        if not self.background or (self._reaper is not None and self._reaper.is_alive()):
            return

        def run() -> None:
            while True:
                time.sleep(1.0)
                try:
                    self.reap()
                except Exception as error:  # noqa: BLE001 —— 后台线程不能死
                    self.log(f"在电脑上点播：清理出错 {type(error).__name__}: {error}")
                with self._lock:
                    if not self._by_device:
                        self._reaper = None
                        return

        self._reaper = threading.Thread(target=run, name="vod-reaper", daemon=True)
        self._reaper.start()

    # ---- 转发一个请求 ----

    def _upstream(self, session: VodSession, wanted: str | None):
        """连上游：返回 (回答, None) 或 (None, (状态码, 给人看的原因, 多给的头))。地址过期时重新取一次。"""
        refreshed = False
        last = (502, "片源连不上", {})
        while True:
            try:
                media = session.media(refresh=refreshed)
            except Exception as error:  # noqa: BLE001 —— 取地址失败（B 站不通、下架了、换了格式）
                reason = watch_pc.explain(session.source.site, f"取不到片源地址：{error}")
                return None, (502, reason, {})
            expired = False
            for url in (media.url, *media.backups):
                headers = dict(session.source.headers)
                headers["Accept-Encoding"] = "identity"
                if wanted:
                    headers["Range"] = wanted
                request = urllib.request.Request(url, headers=headers)
                try:
                    return self.opener(request, UPSTREAM_TIMEOUT_S), None
                except urllib.error.HTTPError as error:
                    code = error.code
                    extra = {}
                    if code == 416 and error.headers.get("Content-Range"):
                        extra["Content-Range"] = error.headers["Content-Range"]
                    error.close()
                    if code == 416:
                        return None, (416, "超出了文件的范围", extra)
                    if code in EXPIRED:
                        expired = True
                        last = (502, watch_pc.explain(session.source.site, f"HTTP {code}"), {})
                        break
                    last = (502, watch_pc.explain(session.source.site, f"HTTP {code}"), {})
                except (OSError, ValueError) as error:
                    last = (502, watch_pc.explain(session.source.site, f"片源：{error}"), {})
            if expired and not refreshed:
                refreshed = True
                self.log(f"在电脑上点播 {session.device}：地址过期了，重新取 <地址>")
                continue
            return None, last

    def serve(self, handler, session: VodSession) -> None:
        """把上游的字节转给浏览器（handler 是 web.py 的 ManageHandler）。"""
        wanted = handler.headers.get("Range") or ""
        match = _RANGE.match(wanted.strip())
        wanted = wanted.strip() if match and (match.group(1) or match.group(2)) else None
        conn = session.admit(getattr(handler, "connection", None), self.max_connections)
        upstream = None
        try:
            upstream, failed = self._upstream(session, wanted)
            if upstream is None:
                code, reason, extra = failed
                if code != 416:
                    session.error = reason
                    self.log(f"在电脑上点播 {session.device}：{reason}")
                handler._send(code, reason.encode(), "text/plain; charset=utf-8", extra)
                return
            session.error = ""
            status = getattr(upstream, "status", None) or upstream.getcode()
            kind = upstream.headers.get("Content-Type") or ""
            if not kind.startswith(("video/", "audio/")):
                kind = "video/mp4"
            length = upstream.headers.get("Content-Length")
            handler.send_response(206 if status == 206 else 200)
            handler.send_header("Content-Type", kind)
            if length and length.isdigit():
                handler.send_header("Content-Length", length)
            else:
                handler.send_header("Connection", "close")
                handler.close_connection = True
            if status == 206 and upstream.headers.get("Content-Range"):
                handler.send_header("Content-Range", upstream.headers["Content-Range"])
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.end_headers()
            read = getattr(upstream, "read1", None) or upstream.read
            total, expected = 0, int(length) if length and length.isdigit() else None
            while not conn.cancelled and not session.stopped:
                chunk = read(CHUNK)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                total += len(chunk)
                session.sent += len(chunk)
                session.touch()
            if conn.cancelled or session.stopped or (expected is not None and total < expected):
                handler.close_connection = True
        except (OSError, ValueError, AttributeError):
            # 浏览器断开（跳转、关页面）或上游半路断了：这条连接不能再用。
            handler.close_connection = True
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except (OSError, ValueError, AttributeError):
                    pass
            session.release(conn)


# ---------------------------------------------------------------- HTTP（中转的网页端口）

def get(handler, relay, path: str) -> bool:
    """GET /vod/<编号>/<令牌>/media（前端去掉了 /tv）：只认令牌，不看登录。处理了返回 True。"""
    if not path.startswith("/vod/"):
        return False
    service = getattr(relay, "vod", None)
    match = _PATH.match(path)
    session = service.find(match.group(1), match.group(2)) if service is not None and match else None
    if session is None:
        handler._not_found()
        return True
    session.touch()
    service.serve(handler, session)
    return True
