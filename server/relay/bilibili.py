"""B 站接口：只用不带签名的网页 API。

B 站对阿里云这个地址的视频网页和带签名（wbi）的列表接口返回 412，yt-dlp 的 B 站解析因此不可用；
下面这几个接口实测可用（见设计文档「验证结果」）。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from .state import LIST_SIZE, Video

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"
API = "https://api.bilibili.com"
TIMEOUT_S = 10
# 接口回答的大小上限；正常回答只有几十 KB。
MAX_ANSWER_BYTES = 2 * 1024 * 1024

_URL = re.compile(r"(?:https?://)?[A-Za-z0-9.-]+(?:/[^\s]*)?")
_BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")


class SourceError(RuntimeError):
    """视频来源（B 站接口或 cstone 上的 YouTube 节点）失败、拒绝或回答缺字段。

    permanent 为真表示来源明确拒绝（视频删了、充电专属等），重试无用；
    为假表示网络或解析问题，稍后可能恢复。
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


# 最早只有 B 站时的名字，保留给已有代码。
BilibiliError = SourceError


def _urls(text: str):
    """分享文案里常夹着标题，逐个取出其中像网址的片段。"""
    for match in _URL.finditer(text or ""):
        raw = match.group(0)
        if "://" not in raw:
            raw = "https://" + raw
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            yield parsed


def parse_creator_link(text: str) -> str | None:
    """UP 主主页链接 → mid（纯数字）。"""
    for url in _urls(text):
        parts = [p for p in url.path.split("/") if p]
        if url.hostname == "space.bilibili.com" and parts and parts[0].isdigit():
            return parts[0]
        if url.hostname == "m.bilibili.com" and len(parts) >= 2 and parts[0] == "space" \
                and parts[1].isdigit():
            return parts[1]
    return None


def parse_video_link(text: str) -> str | None:
    """视频链接 → BV 号。"""
    for url in _urls(text):
        parts = [p for p in url.path.split("/") if p]
        if url.hostname in ("www.bilibili.com", "bilibili.com", "m.bilibili.com") \
                and len(parts) >= 2 and parts[0] == "video" and _BVID.match(parts[1]):
            return parts[1]
    return None


def short_link(text: str) -> str | None:
    """App 分享出来的 b23.tv 短链。"""
    for url in _urls(text):
        if url.hostname == "b23.tv" and url.path.strip("/"):
            return urllib.parse.urlunsplit(("https", "b23.tv", url.path, "", ""))
    return None


def _fetch_json(url: str, cookie: str) -> dict:
    request = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": REFERER, "Cookie": cookie})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as answer:
        return json.loads(answer.read(MAX_ANSWER_BYTES))


def _resolve(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as answer:
        return answer.geturl()


class Bilibili:
    def __init__(self, fetch_json=None, resolve=None):
        self._fetch = fetch_json or _fetch_json
        self._resolve = resolve or _resolve
        self._cookie = ""

    def _call(self, path: str, **params) -> dict:
        try:
            if not self._cookie:
                spi = self._fetch(f"{API}/x/frontend/finger/spi", "")
                self._cookie = f"buvid3={spi['data']['b_3']}"
            url = f"{API}{path}?{urllib.parse.urlencode(params)}"
            answer = self._fetch(url, self._cookie)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # 出错后换一个 buvid3 再试，旧的可能已被风控。
            self._cookie = ""
            raise BilibiliError(f"{path}: {type(error).__name__}: {error}") from None
        if not isinstance(answer, dict):
            answer = {}
        if answer.get("code") != 0:
            self._cookie = ""
            raise BilibiliError(f"{path}: code={answer.get('code')} {answer.get('message', '')}",
                                permanent=True)
        return answer.get("data") or {}

    def uploads(self, mid: str) -> list[Video]:
        data = self._call("/x/space/arc/list", mid=mid, ps=LIST_SIZE, pn=1)
        try:
            return [Video(bvid=a["bvid"], title=str(a.get("title", "")),
                          duration=int(a.get("duration", 0)), pubdate=int(a.get("pubdate", 0)))
                    for a in data.get("archives") or []]
        except (KeyError, TypeError, ValueError) as error:
            raise BilibiliError(f"投稿列表格式不对：{error}") from None

    def creator(self, mid: str) -> tuple[str, list[Video]]:
        card = self._call("/x/web-interface/card", mid=mid)
        try:
            name = str(card["card"]["name"])
        except (KeyError, TypeError):
            raise BilibiliError("拿不到 UP 主名字") from None
        return name, self.uploads(mid)

    def media_url(self, bvid: str) -> str:
        view = self._call("/x/web-interface/view", bvid=bvid)
        cid = view.get("cid")
        if not cid:
            raise BilibiliError(f"{bvid}: 没有 cid", permanent=True)
        play = self._call("/x/player/playurl", bvid=bvid, cid=cid, qn=16, fnval=1,
                          platform="html5")
        durl = play.get("durl") or []
        if not durl or not durl[0].get("url"):
            raise BilibiliError(f"{bvid}: 没有可播放的地址", permanent=True)
        return durl[0]["url"]

    def _expand(self, text: str) -> str:
        """App 分享的 b23.tv 短链先换成真实地址。"""
        short = short_link(text)
        if not short:
            return text
        try:
            return self._resolve(short)
        except (OSError, ValueError) as error:
            raise BilibiliError(f"短链解析失败：{error}") from None

    def video_id(self, text: str) -> str | None:
        return parse_video_link(self._expand(text))

    def video(self, bvid: str) -> Video:
        view = self._call("/x/web-interface/view", bvid=bvid)
        try:
            return Video(bvid=bvid, title=str(view.get("title", "")),
                         duration=int(view.get("duration", 0)), pubdate=int(view.get("pubdate", 0)))
        except (TypeError, ValueError):
            raise BilibiliError(f"{bvid}: 视频信息格式不对", permanent=True) from None

    def creator_id(self, text: str) -> str | None:
        """主页链接、视频链接（取作者）或 b23.tv 短链 → mid。"""
        text = self._expand(text)
        mid = parse_creator_link(text)
        if mid:
            return mid
        bvid = parse_video_link(text)
        if bvid:
            owner = self._call("/x/web-interface/view", bvid=bvid).get("owner") or {}
            if owner.get("mid"):
                return str(owner["mid"])
        return None
