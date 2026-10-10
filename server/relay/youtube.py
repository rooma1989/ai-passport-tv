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

from .bilibili import SourceError, _urls
from .state import Video

HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
SHORT_HOST = "youtu.be"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]+$")
# 列投稿要让节点跑一次 yt-dlp，十几秒是常态。
JSON_TIMEOUT_S = 90
# 流在节点起播前要先取地址、开转码，之后每次读之间不应超过这个时间。
STREAM_TIMEOUT_S = 60
MAX_ANSWER_BYTES = 1024 * 1024


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

    def _fetch_json(self, path: str, params: dict) -> dict:
        try:
            with urllib.request.urlopen(self._request(path, params), timeout=JSON_TIMEOUT_S,
                                        context=self._context) as answer:
                return json.loads(answer.read(MAX_ANSWER_BYTES))
        except urllib.error.HTTPError as error:
            raise NodeError(error.code, error.read(2000).decode("utf-8", "replace")) from None

    def _call(self, path: str, **params) -> dict:
        if self._fetch is None:
            raise SourceError("YouTube 还没配置（缺少 cstone 节点）")
        try:
            answer = self._fetch(path, params)
        except NodeError as error:
            # 404：视频或频道不存在、不可看；其余（含 503 被 YouTube 拦）稍后可能恢复。
            raise SourceError(f"youtube {path}: {error}", permanent=error.status == 404) from None
        except (OSError, ValueError) as error:
            raise SourceError(f"youtube {path}: {type(error).__name__}: {error}") from None
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
