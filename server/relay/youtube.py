"""YouTube：只经 cstone 上的节点（server/relay/ytnode.py）访问，阿里云从不直连 YouTube。

阿里云这边负责认链接、调用节点的 JSON 接口、把节点的 MPEG-TS 流原样转给电视服务。
节点用自签证书，这里只信任那一张证书；每个请求带 X-Key。
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


class YouTube:
    def __init__(self, fetch_json=None, config: dict | None = None):
        """config：{"url": "https://ip:port", "key": "…", "cert": "/path/cstone.pem"}。"""
        self.config = config
        self._context = None
        if config:
            self._context = ssl.create_default_context(cafile=config["cert"])
        self._fetch = fetch_json or (self._fetch_json if config else None)

    def _request(self, path: str, params: dict) -> urllib.request.Request:
        url = f"{self.config['url'].rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
        return urllib.request.Request(url, headers={"X-Key": self.config["key"]})

    def _fetch_json(self, path: str, params: dict, timeout: float = JSON_TIMEOUT_S) -> dict:
        try:
            with urllib.request.urlopen(self._request(path, params), timeout=timeout,
                                        context=self._context) as answer:
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
            raise SourceError(f"youtube {path}: {error}", permanent=error.status == 404,
                              reason=reason) from None
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

    def health(self) -> dict:
        """节点最近一次解析 YouTube 的结果：{"state": ok|login|error|unknown, "at", "version"}。"""
        return self._call("/yt/health")

    def open(self, video_id: str, offset: float) -> StreamProc:
        if self.config is None:
            raise SourceError("YouTube 还没配置（缺少 cstone 节点）")
        request = self._request("/yt/stream", {"id": video_id, "ss": int(offset)})
        try:
            response = urllib.request.urlopen(request, timeout=STREAM_TIMEOUT_S, context=self._context)
        except urllib.error.HTTPError as error:
            raise SourceError(f"youtube stream {video_id}: {error.code}",
                              permanent=error.code == 404) from None
        except OSError as error:
            raise SourceError(f"youtube stream {video_id}: {error}") from None
        return StreamProc(response)
