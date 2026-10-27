"""YouTube：只经 cstone 上的节点（server/relay/ytnode.py）访问，阿里云从不直连 YouTube。

阿里云这边负责认链接、调用节点的 JSON 接口、把节点的 MPEG-TS 流原样转给电视服务。
节点用自签证书，这里只信任那一张证书；每个请求带 X-Key。

家庭服务器、电脑客户端的本机模式（[youtube] mode = "local"）：同一个节点作为监督进程的子进程跑在
127.0.0.1 上，明文 HTTP、不要证书（config 里没有 cert，地址只许是回环）。这条连接永远不走代理——
不看环境变量，也不看系统代理设置（macOS 上 urllib 会去读系统代理）。
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .bilibili import SourceError, _urls, timed_out
from .state import Video

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
SHORT_HOST = "youtu.be"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]+$")
# 播放列表 id（剧集用）；与节点 ytnode._PLAYLIST 一致。
_PLAYLIST = re.compile(r"^(?:PL|OL|UU|FL|RD)[A-Za-z0-9_-]{8,64}$")
# 列投稿要让节点跑一次 yt-dlp，十几秒是常态。
JSON_TIMEOUT_S = 90
# 流在节点起播前要先取地址、开转码，之后每次读之间不应超过这个时间。
STREAM_TIMEOUT_S = 60
MAX_ANSWER_BYTES = 1024 * 1024
# 语音搜索：要几个结果，等节点多久。节点一次只跑一个 yt-dlp，正在解析别的视频时会回答 busy。
SEARCH_MAX = 12
SEARCH_TIMEOUT_S = 5.0
# 电脑版的搜索页：一次要几个（节点最多给 20 个），等多久（本机的 yt-dlp 经代理要好几秒）。
BROWSE_MAX = 20
BROWSE_TIMEOUT_S = 25.0
# 订阅的频道：节点要跑一到两次 yt-dlp（几百个订阅要翻好几页）。
SUBSCRIPTIONS_TIMEOUT_S = 260
# 账号的推荐、观看记录、订阅的新视频（accountfeeds.py）：节点跑一次 yt-dlp，经代理要十几到几十秒。
FEED_TIMEOUT_S = 150
FEED_KINDS = ("rec", "history", "subs")
# 电脑上点播取格式（/yt/formats）：没缓存时节点要跑一次 yt-dlp。
FORMATS_TIMEOUT_S = 90


# 频道 id（UC 加 22 位）：语音搜到的频道按它订阅，地址是 /channel/<id>。
_UCID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")


def channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class NodeError(Exception):
    """节点回答了非 200。"""

    def __init__(self, status: int, message: str):
        super().__init__(f"{status} {message}")
        self.status = status


def is_youtube(text: str) -> bool:
    return any(url.hostname in HOSTS or url.hostname == SHORT_HOST for url in _urls(text))


def parse_channel_link(text: str) -> str | None:
    """频道链接 → 规范的频道地址（@handle、/channel/UC…、/c/…、/user/…）。"""
    for url in _urls(text):
        if url.hostname not in HOSTS:
            continue
        parts = [p for p in url.path.split("/") if p]
        if not parts:
            continue
        if parts[0].startswith("@") and len(parts[0]) > 1:
            return f"https://www.youtube.com/{parts[0]}"
        if parts[0] in ("channel", "c", "user") and len(parts) >= 2 \
                and _CHANNEL_ID.match(parts[1]):
            return f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    return None


def parse_video_link(text: str) -> str | None:
    """视频链接 → 11 位视频 id。"""
    for url in _urls(text):
        parts = [p for p in url.path.split("/") if p]
        candidate = None
        if url.hostname == SHORT_HOST and parts:
            candidate = parts[0]
        elif url.hostname in HOSTS:
            if parts == ["watch"]:
                candidate = (urllib.parse.parse_qs(url.query).get("v") or [""])[0]
            elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed"):
                candidate = parts[1]
        if candidate and _ID.match(candidate):
            return candidate
    return None


def parse_playlist_link(text: str) -> str | None:
    """带 list= 的链接（播放列表页，或列表里某个视频的播放页）→ 播放列表 id。"""
    for url in _urls(text):
        if url.hostname not in HOSTS:
            continue
        playlist = (urllib.parse.parse_qs(url.query).get("list") or [""])[0]
        if _PLAYLIST.match(playlist):
            return playlist
    return None


class _Pipe:
    """把节点的分块响应包成 ffmpeg 输出管道的样子；断在半路记为失败。"""

    def __init__(self, response):
        self.response = response
        self.failed = False

    def read1(self, size: int = -1) -> bytes:
        try:
            return self.response.read1(size)
        except (http.client.IncompleteRead, OSError, ValueError):
            # 节点那边转码出错会不发结束块直接断开，这里因此能和正常播完分开。
            self.failed = True
            return b""


class StreamProc:
    """进程样的对象，供 stream.pump() 使用：stdout.read1()、wait()、kill()。"""

    def __init__(self, response):
        self.stdout = _Pipe(response)
        self._response = response

    def wait(self, timeout=None) -> int:
        return 1 if self.stdout.failed else 0

    def kill(self) -> None:
        try:
            self._response.close()
        except OSError:
            pass


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def is_local_url(url: str) -> bool:
    """本机节点的地址：http://127.0.0.1:端口（或 localhost、[::1]）。"""
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS and parts.port is not None
    except ValueError:
        return False


class YouTube:
    def __init__(self, fetch_json=None, config: dict | None = None):
        """config：云端 {"url": "https://ip:port", "key": "…", "cert": "/path/cstone.pem"}；
        本机模式 {"url": "http://127.0.0.1:端口", "key": "…"}（没有 cert）。"""
        self.config = config
        self._context = None
        self.local = bool(config) and not config.get("cert")
        if config and self.local and not is_local_url(config["url"]):
            raise ValueError("没有证书的 YouTube 节点只能在本机（http://127.0.0.1:端口）")
        if config and not self.local:
            self._context = ssl.create_default_context(cafile=config["cert"])
        # 本机节点：不经任何代理（ProxyHandler({}) 盖掉环境变量和系统代理）。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if self.local else None
        self._fetch = fetch_json or (self._fetch_json if config else None)

    def _request(self, path: str, params: dict) -> urllib.request.Request:
        url = f"{self.config['url'].rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
        return urllib.request.Request(url, headers={"X-Key": self.config["key"]})

    def _urlopen(self, request: urllib.request.Request, timeout: float):
        if self._opener is not None:
            return self._opener.open(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout, context=self._context)

    def _fetch_json(self, path: str, params: dict, timeout: float = JSON_TIMEOUT_S) -> dict:
        try:
            with self._urlopen(self._request(path, params), timeout) as answer:
                return json.loads(answer.read(MAX_ANSWER_BYTES))
        except urllib.error.HTTPError as error:
            raise NodeError(error.code, error.read(2000).decode("utf-8", "replace")) from None

    def _call(self, path: str, _timeout: float | None = None, **params) -> dict:
        if self._fetch is None:
            raise SourceError("YouTube 还没配置（缺少 cstone 节点）")
        try:
            # 只有语音搜索给 _timeout；其余用 JSON_TIMEOUT_S（列投稿要十几秒）。
            answer = (self._fetch(path, params) if _timeout is None
                      else self._fetch(path, params, timeout=_timeout))
        except NodeError as error:
            # 404：视频或频道不存在、不可看；其余（含 503 被 YouTube 拦）稍后可能恢复。
            # 503 busy：节点正在跑别的 yt-dlp（搜索不排队等太久）；504：yt-dlp 超时。
            reason = ("busy" if error.status == 503 and "busy" in str(error)
                      else "slow" if error.status == 504 else "")
            failure = SourceError(f"youtube {path}: {error}", permanent=error.status == 404, reason=reason)
            # 节点回答的 HTTP 码（电脑版的搜索页据此分开说：要登录、组件没装好、节点不支持）；
            # 连不上节点时没有这一项。
            failure.status = error.status
            raise failure from None
        except (OSError, ValueError) as error:
            raise SourceError(f"youtube {path}: {type(error).__name__}: {error}",
                              reason="slow" if timed_out(error) else "") from None
        if not isinstance(answer, dict):
            raise SourceError(f"youtube {path}: 回答格式不对")
        return answer

    def uploads(self, channel: str) -> list[Video]:
        return self.creator(channel)[1]

    def creator(self, channel: str) -> tuple[str, list[Video]]:
        answer = self._call("/yt/list", channel=channel)
        try:
            videos = [Video(bvid=v["id"], title=str(v.get("title") or ""),
                            duration=int(v.get("duration") or 0), pubdate=0, site="youtube")
                      for v in answer.get("videos") or [] if _ID.match(str(v.get("id", "")))]
            return str(answer.get("name") or channel), videos
        except (KeyError, TypeError, ValueError) as error:
            raise SourceError(f"youtube 投稿列表格式不对：{error}") from None

    def video_id(self, text: str) -> str | None:
        return parse_video_link(text)

    def search(self, query: str, limit: int = SEARCH_MAX, timeout: float = SEARCH_TIMEOUT_S) -> list[dict]:
        """语音搜索，经节点的 /yt/search：[{"id", "title", "uploader", "duration"}]。
        直播、预告和 Shorts 节点已经滤掉了。"""
        answer = self._call("/yt/search", _timeout=timeout, q=query, n=limit)
        hits = []
        for v in answer.get("videos") or []:
            if not isinstance(v, dict) or not _ID.match(str(v.get("id", ""))):
                continue
            try:
                duration = int(v.get("duration") or 0)
            except (TypeError, ValueError):
                continue
            title = " ".join(str(v.get("title") or "").split())
            if title and duration > 0:
                hits.append({"id": v["id"], "title": title,
                             "uploader": " ".join(str(v.get("uploader") or "").split())[:40],
                             "duration": duration})
        return hits[:limit]

    def search_creators(self, query: str, limit: int = SEARCH_MAX,
                        timeout: float = SEARCH_TIMEOUT_S) -> list[dict]:
        """语音搜索频道，经节点的 /yt/search?kind=channel：[{"id": UC…, "name", "fans", "videos"}]。"""
        answer = self._call("/yt/search", _timeout=timeout, q=query, n=limit, kind="channel")
        hits = []
        for c in answer.get("channels") or []:
            if not isinstance(c, dict) or not _UCID.match(str(c.get("id", ""))):
                continue
            name = " ".join(str(c.get("name") or "").split())[:40]
            if name:
                hits.append({"id": c["id"], "name": name, "fans": _count(c.get("fans")),
                             "videos": _count(c.get("videos")), "latest": None})
        return hits[:limit]

    def browse(self, query: str, kind: str = "video", limit: int = BROWSE_MAX,
               timeout: float = BROWSE_TIMEOUT_S) -> dict:
        """电脑版搜索页（ytsearch.py）：节点 /yt/search 的回答原样给出（{"videos"} / {"channels"} /
        {"playlists"}），整理、过滤在 ytsearch.py。full=1 让节点多给播放数等；旧节点不认它，照常回答。"""
        return self._call("/yt/search", _timeout=timeout, q=query, n=limit, kind=kind, full=1)

    def subscriptions(self, timeout: float = SUBSCRIPTIONS_TIMEOUT_S) -> list[dict]:
        """cookies 那个账号订阅的频道，经节点的 /yt/subscriptions：[{"id": UC…, "name", "handle", "fans"}]。
        没登录时 SourceError(reason="login")。"""
        try:
            answer = self._call("/yt/subscriptions", _timeout=timeout)
        except SourceError as error:
            if ": 401 " in str(error):
                raise SourceError(str(error), reason="login") from None
            raise
        found, seen = [], set()
        for c in answer.get("channels") or []:
            if not isinstance(c, dict) or not _UCID.match(str(c.get("id", ""))) or c["id"] in seen:
                continue
            name = " ".join(str(c.get("name") or "").split())[:60]
            handle = str(c.get("handle") or "")
            if not name:
                continue
            seen.add(c["id"])
            found.append({"id": c["id"], "name": name,
                          "handle": handle if re.match(r"^@[^/?#\s]{1,100}$", handle) else "",
                          "fans": _count(c.get("fans"))})
        return found

    def feed(self, kind: str, limit: int = 30, timeout: float = FEED_TIMEOUT_S) -> list[dict]:
        """cookies 那个账号的推荐（rec）、观看记录（history）、订阅的新视频（subs），经节点的 /yt/feed：
        [{"id", "title", "duration", "channel", "channel_id", "thumb", "date", "views"}]。
        没登录时 SourceError(reason="login")；节点太旧、没有这个路径时 SourceError(reason="unsupported")。"""
        if kind not in FEED_KINDS:
            raise ValueError(kind)
        try:
            answer = self._call("/yt/feed", _timeout=timeout, kind=kind, n=limit)
        except SourceError as error:
            status = getattr(error, "status", 0)
            if status == 401:
                raise SourceError(str(error), reason="login") from None
            if status == 404:
                raise SourceError(str(error), permanent=True, reason="unsupported") from None
            raise
        found, seen = [], set()
        for v in answer.get("videos") or []:
            if not isinstance(v, dict) or not _ID.match(str(v.get("id", ""))) or v["id"] in seen:
                continue
            title = " ".join(str(v.get("title") or "").split())[:200]
            if not title:
                continue
            seen.add(v["id"])
            cid = str(v.get("channel_id") or "")
            found.append({"id": v["id"], "title": title, "duration": _count(v.get("duration")),
                          "channel": " ".join(str(v.get("channel") or "").split())[:60],
                          "channel_id": cid if _UCID.match(cid) else "", "date": _count(v.get("date")),
                          "views": _count(v.get("views"))})
        return found[:limit]

    def series_id(self, text: str) -> str | None:
        return parse_playlist_link(text)

    def series(self, playlist: str) -> tuple[str, list[Video]]:
        """(列表名, 按列表顺序的视频)。"""
        answer = self._call("/yt/playlist", list=playlist)
        try:
            videos = [Video(bvid=v["id"], title=str(v.get("title") or ""),
                            duration=int(v.get("duration") or 0), pubdate=0, site="youtube")
                      for v in answer.get("videos") or [] if _ID.match(str(v.get("id", "")))]
        except (KeyError, TypeError, ValueError) as error:
            raise SourceError(f"youtube 播放列表格式不对：{error}") from None
        if len(videos) < 2:
            raise SourceError(f"{playlist}: 播放列表里不到两个能看的视频", permanent=True)
        return str(answer.get("name") or playlist), videos

    def video(self, video_id: str) -> Video:
        answer = self._call("/yt/info", id=video_id)
        try:
            return Video(bvid=video_id, title=str(answer.get("title") or ""),
                         duration=int(answer.get("duration") or 0), pubdate=0, site="youtube")
        except (TypeError, ValueError):
            raise SourceError(f"{video_id}: 视频信息格式不对") from None

    def formats(self, video_id: str, prefetch: bool = False, fresh: bool = False,
                timeout: float = FORMATS_TIMEOUT_S) -> dict:
        """电脑上点播（ytvod.py）：节点 /yt/formats 的回答 {"id", "title", "duration", "formats": [...]}，地址带签名，
        不进日志。prefetch：预取（节点忙时 SourceError(reason="busy")，真正的请求来了它让路）；fresh：节点不用缓存。
        旧节点没有这个路径：SourceError(reason="unsupported")，中转照旧走 /yt/stream。"""
        params = {"id": video_id}
        if prefetch:
            params["prefetch"] = 1
        if fresh:
            params["fresh"] = 1
        try:
            answer = self._call("/yt/formats", _timeout=timeout, **params)
        except SourceError as error:
            if getattr(error, "status", 0) == 404 and str(error).endswith("404 Not Found"):
                failure = SourceError(str(error), reason="unsupported")
                failure.status = 404
                raise failure from None
            raise
        if not isinstance(answer.get("formats"), list):
            raise SourceError(f"youtube /yt/formats {video_id}: 回答格式不对")
        return answer

    def creator_id(self, text: str) -> str | None:
        """频道链接 → 频道地址；视频链接 → 它所属频道的地址。"""
        channel = parse_channel_link(text)
        if channel:
            return channel
        video_id = parse_video_link(text)
        if video_id:
            channel = self._call("/yt/info", id=video_id).get("channel_url")
            return str(channel) if channel else None
        return None

    def health(self, timeout: float | None = None) -> dict:
        """节点最近一次解析 YouTube 的结果：{"state": ok|login|error|unknown, "at", "version"}；本机节点还有
        "tools"（组件装好没有）和 "startup"（yt-dlp --version 要几秒，没量过时 null）。"""
        return self._call("/yt/health", _timeout=timeout)

    def check(self, timeout: float) -> dict:
        """不走缓存解析一次测试视频（设置页的「测试」，本机节点才有）：
        {"state": ok|login|network|slow|error|missing, "seconds", "error", "version"}。"""
        return self._call("/yt/check", _timeout=timeout)

    def open(self, video_id: str, offset: float) -> StreamProc:
        if self.config is None:
            raise SourceError("YouTube 还没配置（缺少 cstone 节点）")
        request = self._request("/yt/stream", {"id": video_id, "ss": int(offset)})
        try:
            response = self._urlopen(request, STREAM_TIMEOUT_S)
        except urllib.error.HTTPError as error:
            raise SourceError(f"youtube stream {video_id}: {error.code}",
                              permanent=error.code == 404) from None
        except OSError as error:
            raise SourceError(f"youtube stream {video_id}: {error}") from None
        return StreamProc(response)
