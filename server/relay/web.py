"""中转的两个 HTTP 服务：给电视服务的播放流，和给手机的管理网页。

两者都只监听 127.0.0.1，且分开端口：播放流（/ch/<id>）只给本机的电视服务用，不需要密钥；
管理网页经 nginx 的 /TV/cast/ 转进来，每个请求都要带密钥，密钥不对一律 404，
外面看不出这里有服务。nginx 只转发到管理端口，所以从外网到不了播放流。
"""

from __future__ import annotations

import hmac
import http.cookies
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import channel_groups, now_playing
from . import events, stream, table, youtube as youtube_links
from .bilibili import BilibiliError, SourceError
from .state import LIST_MAX_AGE_S, RESUME_BACK_S, SESSION_S, State, Video

# 一次请求里最多跳过几个放不了的视频，之后返回 503，让电视服务稍后再来。
MAX_SKIPS = 3
# 同一视频连续几次刚起播就失败（例如地址过期、被拒），就当作放不了。
MAX_QUICK_FAILURES = 3
QUICK_FAILURE_S = 5.0
MAX_BODY_BYTES = 4096
_CHANNEL = re.compile(r"^/ch/(up\d\d|cast)$")
_CREATOR = re.compile(r"^/api/creators/(up\d\d)$")
_HISTORY = re.compile(r"^/api/history/(bilibili|youtube)/([A-Za-z0-9_-]{1,32})$")
_CODE = re.compile(r"^/r/([A-Za-z2-7]{20})$")
COOKIE = "tvs"


class Relay:
    """播放与管理共用的逻辑；HTTP 处理类只做解析和回写。"""

    def __init__(self, state: State, client, channels_out: Path, ffmpeg: str, key: str,
                 events_out: Path | None = None, youtube=None,
                 main_channels: Path | None = None, groups_out: Path | None = None,
                 now_playing_in: Path | None = None, remote_url_out: Path | None = None,
                 public_url: str = "",
                 log=print, clock=time.time, monotonic=time.monotonic,
                 starter=stream.start, pumper=stream.pump):
        self.state = state
        # 每个网站一个客户端；YouTube 只经 cstone 节点，没配置时为 None。
        self.clients = {"bilibili": client, "youtube": youtube}
        self.events_out = Path(events_out) if events_out is not None else None
        self.channels_out = Path(channels_out)
        self.ffmpeg = ffmpeg
        self.key = key
        self.log = log
        self.clock = clock
        self.monotonic = monotonic
        self.starter = starter
        self.pumper = pumper
        self._quick_failures: dict[tuple[str, str], int] = {}
        # 刷新列表要访问 B 站，同一时间只让一个请求去做。
        self._refresh_lock = threading.Lock()
        # 手机遥控：直播频道表（只读）、分组调整（只由这里写）、电视服务写的「正在播放」。
        self.main_channels = Path(main_channels) if main_channels else None
        self.groups_out = Path(groups_out) if groups_out else None
        self.now_playing_in = Path(now_playing_in) if now_playing_in else None
        self.remote_url_out = Path(remote_url_out) if remote_url_out else None
        self.public_url = public_url.rstrip("/")
        # 每个台最后起播的视频标题，给手机上的「正在播放」用。
        self._titles: dict[str, str] = {}

    # ---- 频道表 ----

    def write_table(self) -> None:
        with self.state.lock:
            text = table.render(self.state.creators)
        if table.write_if_changed(self.channels_out, text):
            self.log(f"频道表已更新：{len(self.state.creators)} 个 UP 主")

    # ---- 播放 ----

    def refresh_if_stale(self, cid: str) -> None:
        creator = self.state.get(cid)
        if creator is None or self.clock() - creator.fetched_at < LIST_MAX_AGE_S:
            return
        with self._refresh_lock:
            creator = self.state.get(cid)
            if creator is None or self.clock() - creator.fetched_at < LIST_MAX_AGE_S:
                return
            try:
                videos = self._client(creator.site).uploads(creator.source)
            except SourceError as error:
                # 拿不到就继续用缓存的列表。
                self.log(f"{cid} 刷新投稿列表失败，用缓存：{error}")
                return
            if videos:
                self.state.set_videos(cid, videos, self.clock())
                self.state.save()

    def open_channel(self, cid: str):
        """选出下一个视频并启动 ffmpeg；返回 (进程, 视频, 起点) 或说明原因的 HTTP 码。"""
        if self.state.get(cid) is None:
            return 404
        self.refresh_if_stale(cid)
        for _ in range(MAX_SKIPS):
            pick = self.state.next_video(cid)
            if pick is None:
                return 503
            video, offset = pick
            try:
                return self._start(video, offset), video, offset
            except SourceError as error:
                if not error.permanent:
                    self.log(f"{cid} {video.bvid} 暂时取不到：{error}")
                    return 503
                self.log(f"{cid} {video.bvid} 放不了，跳过：{error}")
                self.state.mark_watched(cid, video.bvid)
                self.state.save()
        return 503

    def _client(self, site: str):
        client = self.clients.get(site)
        if client is None:
            raise SourceError(f"{site} 还没配置")
        return client

    def _start(self, video, offset: float):
        """起播：B 站在本机用 ffmpeg 换封装；YouTube 由 cstone 节点转好码，这里只转发。"""
        if video.site == "youtube":
            return self._client("youtube").open(video.bvid, offset)
        url = self._client("bilibili").media_url(video.bvid)
        return self.starter(self.ffmpeg, url, offset)

    def finish(self, cid: str, video, offset: float, outcome: str, elapsed: float) -> None:
        key = (cid, video.bvid)
        if outcome == "ended":
            self._quick_failures.pop(key, None)
            self.state.mark_watched(cid, video.bvid)
        elif outcome == "failed" and elapsed < QUICK_FAILURE_S:
            count = self._quick_failures.get(key, 0) + 1
            self._quick_failures[key] = count
            if count >= MAX_QUICK_FAILURES:
                self.log(f"{cid} {video.bvid} 连续 {count} 次起播失败，跳过")
                self._quick_failures.pop(key, None)
                self.state.mark_watched(cid, video.bvid)
        else:
            self._quick_failures.pop(key, None)
            self.state.save_position(cid, video.bvid, offset + elapsed)
        self.state.save()

    def _event(self, action: str, channel: str = "") -> None:
        if self.events_out is None:
            return
        try:
            seq = events.write_event(self.events_out, action, channel=channel)
            self.log(f"事件 {seq} {action} {channel}".rstrip())
        except OSError as error:
            self.log(f"写事件失败：{error}")

    def open_cast(self):
        """投屏视频：从保存的进度往回退一点续播；已播完的从头重播。"""
        with self.state.lock:
            video, done, position = self.state.cast, self.state.cast_done, self.state.cast_position
        if video is None:
            return 404
        offset = 0.0 if done else max(0.0, position - RESUME_BACK_S)
        try:
            return self._start(video, offset), video, offset
        except SourceError as error:
            self.log(f"cast {video.bvid} 取不到：{error}")
            return 503

    def finish_cast(self, video, offset: float, outcome: str, elapsed: float) -> None:
        if outcome == "ended":
            self.state.cast_ended(video.bvid)
            self.state.save()
            # 让电视服务把设备切回投屏前的台。
            self._event("return")
            return
        self.state.save_cast_position(video.bvid, offset + elapsed)
        self.state.save()

    def play(self, cid: str, handler) -> None:
        cast = cid == table.CAST_ID
        opened = self.open_cast() if cast else self.open_channel(cid)
        if isinstance(opened, int):
            handler.send_error(opened)
            return
        proc, video, offset = opened
        self._titles[cid] = video.title
        self.log(f"{cid} 开始 {video.bvid} 从 {int(offset)} 秒 《{video.title}》")
        began = self.monotonic()
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "video/mp2t")
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
        except OSError:
            proc.kill()
            proc.wait()
            return

        ticked = [0.0]

        def tick(elapsed: float) -> None:
            ticked[0] = elapsed
            if cast:
                self.state.save_cast_position(video.bvid, offset + elapsed)
            else:
                self.state.save_position(cid, video.bvid, offset + elapsed)
            self.state.save()

        outcome = self.pumper(proc, handler.wfile.write, tick)
        elapsed = max(self.monotonic() - began, ticked[0])
        if cast:
            self.finish_cast(video, offset, outcome, elapsed)
        else:
            self.finish(cid, video, offset, outcome, elapsed)
        self.log(f"{cid} 结束 {video.bvid} {outcome} 播了 {int(elapsed)} 秒")

    # ---- 管理 ----

    def key_ok(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.key.encode())

    def code_ok(self, given: str | None) -> bool:
        code = self.state.ensure_code()
        return bool(given) and hmac.compare_digest(given.upper().encode(), code.encode())

    def remote_url(self) -> str:
        """二维码地址：全大写，二维码可用字母数字模式（第 2 版就装得下）。"""
        return f"{self.public_url.upper()}/TV/R/{self.state.ensure_code()}"

    def write_remote_url(self) -> None:
        if self.remote_url_out is None or not self.public_url:
            return
        if table.write_if_changed(self.remote_url_out, self.remote_url() + "\n"):
            self.log("遥控二维码地址已更新")

    def new_code(self) -> dict:
        self.state.new_code()
        self.state.save()
        self.write_remote_url()
        self.log("换了新的扫码码，已登录的手机需要重新扫码")
        return {"ok": True}

    # ---- 频道与遥控 ----

    def _overrides(self) -> dict:
        return channel_groups.load_overrides(self.groups_out)

    def channel_rows(self) -> list[tuple[str, str]]:
        """(id, 显示名)：投屏台、UP 主，再接直播频道表。"""
        with self.state.lock:
            rows = [(table.CAST_ID, table.CAST_LABEL)]
            rows += [(c.id, table.label(c)) for c in self.state.creators]
        if self.main_channels is not None:
            rows += channel_groups.read_table(self.main_channels)
        return rows

    def groups(self) -> list[dict]:
        overrides = self._overrides()
        grouped: dict[str, list[dict]] = {g: [] for g in channel_groups.GROUPS}
        for cid, name in self.channel_rows():
            group = channel_groups.group_of(cid, name, overrides)
            grouped[group].append({"id": cid, "name": name, "n": len(grouped[group]) + 1})
        return [{"name": g, "channels": grouped[g]} for g in channel_groups.GROUPS if grouped[g]]

    def now(self, groups: list[dict]) -> dict:
        playing = now_playing.read(self.now_playing_in, self.clock())
        cid = playing["channel"]
        found = next(((g["name"], c) for g in groups for c in g["channels"] if c["id"] == cid), None)
        title = ""
        if cid == table.CAST_ID and self.state.cast is not None:
            title = self.state.cast.title
        elif cid.startswith("up"):
            title = self._titles.get(cid, "")
        return {"channel": cid, "online": playing["online"], "switch": playing["switch"],
                "group": found[0] if found else "", "name": found[1]["name"] if found else "",
                "n": found[1]["n"] if found else 0, "title": title}

    def switch(self, channel: str) -> dict:
        if channel not in {cid for cid, _ in self.channel_rows()}:
            return {"ok": False, "error": "没有这个台"}
        playing = now_playing.read(self.now_playing_in, self.clock())
        if not playing["online"]:
            return {"ok": False, "error": "小智没在线"}
        if not playing["switch"]:
            return {"ok": False, "error": "小电视的固件不支持遥控，请在小电视上手动换台"}
        self._event("switch", channel=channel)
        return {"ok": True}

    def set_group(self, channel: str, group) -> dict:
        names = dict(self.channel_rows())
        if channel not in names or channel_groups.classify(channel, names[channel]) in (
                channel_groups.CAST, channel_groups.CREATORS):
            return {"ok": False, "error": "这个台不能调整分组"}
        if group is not None and group not in channel_groups.GROUPS[2:]:
            return {"ok": False, "error": "没有这个分组"}
        if self.groups_out is None:
            return {"ok": False, "error": "没有配置分组文件"}
        overrides = self._overrides()
        if group is None or group == channel_groups.classify(channel, names[channel]):
            overrides.pop(channel, None)
        else:
            overrides[channel] = group
        channel_groups.save_overrides(self.groups_out, overrides)
        self.log(f"分组 {channel} {names[channel]} → {group or '自动'}")
        return {"ok": True}

    # ---- 管理 ----

    def summary(self) -> dict:
        groups = self.groups()
        with self.state.lock:
            creators = [{"id": c.id, "name": c.name, "site": c.site,
                         "left": sum(v.bvid not in c.watched for v in c.videos),
                         "total": len(c.videos)} for c in self.state.creators]
            cast = ({"title": self.state.cast.title, "done": self.state.cast_done}
                    if self.state.cast is not None else None)
            history = [dict(h) for h in self.state.history]
        return {"now": self.now(groups), "groups": groups, "creators": creators,
                "cast": cast, "history": history,
                "group_names": list(channel_groups.GROUPS[2:])}

    def add_creator(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "请先粘贴链接"}
        site = _site_of(text)
        if self.clients.get(site) is None:
            return {"ok": False, "error": "YouTube 还没配置好，暂时只能添加 B 站 UP 主"}
        try:
            mid = self.clients[site].creator_id(text)
            if mid is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴 UP 主（频道）主页链接，或他的任意一个视频链接"}
            name, videos = self.clients[site].creator(mid)
        except SourceError as error:
            self.log(f"添加 UP 主失败：{error}")
            return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时连不上或拒绝了请求，请稍后再试"}
        try:
            creator = self.state.add(site, mid, name, videos, self.clock())
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        self.state.save()
        self.write_table()
        self.log(f"添加 UP 主 {creator.id} {name}（mid {mid}，{len(videos)} 个投稿）")
        return {"ok": True, "creator": {"id": creator.id, "name": name}}

    def cast(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "请先粘贴视频链接"}
        site = _site_of(text)
        if self.clients.get(site) is None:
            return {"ok": False, "error": "YouTube 还没配置好，暂时只能投 B 站视频"}
        try:
            bvid = self.clients[site].video_id(text)
            if bvid is None:
                return {"ok": False, "error": "看不懂这个链接。请粘贴视频链接（App 里“分享 → 复制链接”）"}
            video = self.clients[site].video(bvid)
        except SourceError as error:
            self.log(f"投屏失败：{error}")
            if error.permanent:
                return {"ok": False, "error": "这个视频放不了（可能已删除、私享或需要会员）"}
            return {"ok": False, "error": f"{_SITE_NAMES[site]}暂时取不到，请稍后再试"}
        return self._cast_video(video)

    def _cast_video(self, video) -> dict:
        self.state.set_cast(video)
        self.state.remember(video, at=self.clock())
        self.state.save()
        self.write_table()
        self.log(f"投屏 {video.bvid} 《{video.title}》")
        self._event("cast")
        return {"ok": True, "title": video.title}

    def replay(self, site: str, video_id: str) -> dict:
        entry = self.state.history_entry(site, video_id)
        if entry is None:
            return {"ok": False, "error": "历史里没有这个视频"}
        if self.clients.get(site) is None:
            return {"ok": False, "error": "YouTube 还没配置好"}
        return self._cast_video(Video(bvid=video_id, title=entry.get("title", ""), duration=0,
                                      pubdate=0, site=site))

    def forget(self, site: str, video_id: str) -> dict:
        if not self.state.forget(site, video_id):
            return {"ok": False, "error": "历史里没有这个视频"}
        self.state.save()
        return {"ok": True}

    def remove_creator(self, cid: str) -> dict:
        if not self.state.remove(cid):
            return {"ok": False, "error": "没有这个 UP 主"}
        self.state.save()
        self.write_table()
        self.log(f"删除 UP 主 {cid}")
        return {"ok": True}


_SITE_NAMES = {"bilibili": "B 站", "youtube": "YouTube "}


def _site_of(text: str) -> str:
    return "youtube" if youtube_links.is_youtube(text) else "bilibili"


def _stream_handler(relay: Relay):
    class StreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):
            match = _CHANNEL.match(urllib.parse.urlsplit(self.path).path)
            if not match:
                self.send_error(404)
                return
            relay.play(match.group(1), self)

        def log_message(self, *_args):
            pass

    return StreamHandler


def _manage_handler(relay: Relay):
    class ManageHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _path(self) -> str:
            path = urllib.parse.urlsplit(self.path).path
            # nginx 把 /tv/ 去掉前缀转进来；直接访问本端口时也接受带前缀的路径。
            if path == "/tv" or path.startswith("/tv/"):
                path = path[3:] or "/"
            return path

        def _query(self) -> dict:
            return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

        def _cookie(self) -> str:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except http.cookies.CookieError:
                return ""
            morsel = jar.get(COOKIE)
            return morsel.value if morsel else ""

        def _authorized(self) -> bool:
            return (relay.key_ok(self.headers.get("X-Key"))
                    or relay.state.session_ok(self._cookie(), relay.clock()))

        def _cookie_header(self, token: str) -> str:
            return (f"{COOKIE}={token}; HttpOnly; Secure; SameSite=Lax; Path=/tv/; "
                    f"Max-Age={SESSION_S}")

        def _send(self, code: int, body: bytes, kind: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # 网址里可能带着密钥或扫码码，不能经 Referer 带到别处。
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: dict, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _not_found(self) -> None:
            self._send(404, NOT_FOUND.encode(), "text/html; charset=utf-8")

        def _login(self) -> None:
            """验证过密钥或扫码码：发一个 180 天的会话 cookie，跳到遥控页。"""
            token = relay.state.new_session(relay.clock())
            relay.state.save()
            # 会话也放进跳转后的网址：iPhone 加到桌面的网页应用不和 Safari 共用 cookie，
            # 从这个页面「添加到主屏幕」，图标会记住带会话的网址。它只是可作废的会话，不是密钥。
            self._send(302, b"", "text/plain",
                       {"Location": f"/tv/?s={token}", "Set-Cookie": self._cookie_header(token)})

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                return None
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                return None
            return body if isinstance(body, dict) else None

        def do_GET(self):
            path = self._path()
            if path in ASSETS:
                kind, data = ASSETS[path]
                self._send(200, data, kind, {"Cache-Control": "public, max-age=86400"})
                return
            match = _CODE.match(path)
            if match:
                if relay.code_ok(match.group(1)):
                    self._login()
                else:
                    self._not_found()
                return
            if path == "/" and relay.key_ok(self._query().get("k")):
                self._login()
                return
            session = self._query().get("s")
            if path == "/" and relay.state.session_ok(session, relay.clock()):
                # 从桌面图标打开：网址里的会话有效，补上 cookie，后续接口调用就带着它。
                self._send(200, PAGE.encode(), "text/html; charset=utf-8",
                           {"Set-Cookie": self._cookie_header(session)})
                return
            if not self._authorized():
                self._not_found()
                return
            if path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(relay.summary())
            else:
                self._not_found()

        def do_POST(self):
            if not self._authorized():
                self._not_found()
                return
            path = self._path()
            body = self._body()
            if body is None:
                self._json({"ok": False, "error": "请求格式不对"}, 400)
                return
            if path == "/api/code":
                self._json(relay.new_code())
                return
            if path == "/api/switch":
                self._json(relay.switch(str(body.get("channel") or "")))
                return
            if path == "/api/groups":
                group = body.get("group")
                self._json(relay.set_group(str(body.get("channel") or ""),
                                           group if isinstance(group, str) else None))
                return
            if path == "/api/history/play":
                self._json(relay.replay(str(body.get("site") or ""), str(body.get("id") or "")))
                return
            if path in ("/api/creators", "/api/cast"):
                url = body.get("url")
                if not isinstance(url, str):
                    self._json({"ok": False, "error": "请求格式不对"}, 400)
                    return
                self._json(relay.add_creator(url) if path == "/api/creators" else relay.cast(url))
                return
            self._not_found()

        def do_DELETE(self):
            if not self._authorized():
                self._not_found()
                return
            path = self._path()
            match = _CREATOR.match(path)
            if match:
                self._json(relay.remove_creator(match.group(1)))
                return
            match = _HISTORY.match(path)
            if match:
                self._json(relay.forget(match.group(1), match.group(2)))
                return
            self._not_found()

        def log_message(self, *_args):
            pass

    return ManageHandler


def make_servers(relay: Relay, stream_port: int, manage_port: int,
                 bind: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    streams = ThreadingHTTPServer((bind, stream_port), _stream_handler(relay))
    manage = ThreadingHTTPServer((bind, manage_port), _manage_handler(relay))
    streams.daemon_threads = manage.daemon_threads = True
    return streams, manage


_ASSET_DIR = Path(__file__).resolve().parent / "assets"
PAGE = (_ASSET_DIR / "remote.html").read_text(encoding="utf-8")
# 清单和图标不带登录也要能取：浏览器取清单时不带 cookie。
ASSETS = {
    "/manifest.webmanifest": ("application/manifest+json",
                              (_ASSET_DIR / "manifest.webmanifest").read_bytes()),
    "/icon-180.png": ("image/png", (_ASSET_DIR / "icon-180.png").read_bytes()),
    "/icon-512.png": ("image/png", (_ASSET_DIR / "icon-512.png").read_bytes()),
}
NOT_FOUND = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
             "<body style='background:#1c1915;color:#e6dcc8;font:16px/1.6 serif;padding:24px'>"
             "<p>打不开。请用小电视状态页上的二维码重新扫一下。</p>")
