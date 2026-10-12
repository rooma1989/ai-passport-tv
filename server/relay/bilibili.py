"""B 站接口：只用不带签名的网页 API。

B 站对阿里云这个地址的视频网页和带签名（wbi）的列表接口返回 412，yt-dlp 的 B 站解析因此不可用；
下面这几个接口实测可用（见设计文档「验证结果」）。
"""

from __future__ import annotations

import html
import json
import re
import time
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
# 番剧链接：/bangumi/play/ss2307、/bangumi/play/ep52309、/bangumi/media/md2307。
_BANGUMI = re.compile(r"^(ss|ep|md)(\d{1,12})$")
# 剧集里一集的 id：番剧是「ep<集 id>/<cid>」，多集视频和合集是「<BV 号>/<cid>」。
_EPISODE = re.compile(r"^(ep\d{1,12}|BV[0-9A-Za-z]{10})/(\d{1,15})$")
# 从视频标题里猜剧名时去掉的东西：括号里的说明、「全 13 集」「1-125」「1987」之类。
_NAME_NOISE = re.compile(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)|《|》|『[^』]*』"
                         r"|全\s*\d+\s*[集话]|\d+\s*[集话]全?|\d+\s*全集|\d+\s*-\s*\d+"
                         r"|全集|合集|完整版|超清|高清|国语|台配|中字|\d+P|4K|修复|(?:19|20)\d\d",
                         re.IGNORECASE)
# 开头括号里是这些说明时不是剧名：「【4K/国产动画】」「【480P/美国/动画】」。
_NAME_TAGS = ("动画", "国产", "上美", "字幕", "/")
# 语音搜索：一次最多给几个结果（接口一页 20 个），每次等多久（语音结果设备只等 8 秒）。
SEARCH_MAX = 20
SEARCH_TIMEOUT_S = 4.0
_TAGS = re.compile(r"<[^>]*>")


class SourceError(RuntimeError):
    """视频来源（B 站接口或 cstone 上的 YouTube 节点）失败、拒绝或回答缺字段。

    permanent 为真表示来源明确拒绝（视频删了、充电专属等），重试无用；
    为假表示网络或解析问题，稍后可能恢复。
    """

    def __init__(self, message: str, permanent: bool = False, reason: str = ""):
        super().__init__(message)
        self.permanent = permanent
        # 语音搜索要分开说的原因：slow（超时）、busy（YouTube 节点正忙）；其余为空。
        self.reason = reason


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


def parse_series_link(text: str) -> str | None:
    """剧集链接 → 来源：番剧是「ss<季 id>」「ep<集 id>」「md<作品 id>」，多集视频或合集是 BV 号。"""
    for url in _urls(text):
        parts = [p for p in url.path.split("/") if p]
        if url.hostname not in ("www.bilibili.com", "bilibili.com", "m.bilibili.com"):
            continue
        if len(parts) >= 3 and parts[0] == "bangumi" and parts[1] in ("play", "media") \
                and _BANGUMI.match(parts[2]):
            return parts[2]
        if len(parts) >= 2 and parts[0] == "video" and _BVID.match(parts[1]):
            return parts[1]
    return None


def _bare_name(text: str) -> str:
    name = _NAME_NOISE.sub(" ", text)
    name = re.split(r"[|｜/]", name)[0]
    return " ".join(name.replace("_", " ").split()).strip(" -—:：·,，")


def series_name(title: str) -> str:
    """多集视频的标题常是「【4K修复】阿凡提的故事 (1979) 14集全【上美】」，取出剧名。

    「【舒克和贝塔】4K修复全13集」「【数码宝贝 第一季】全54集」这种，剧名在开头的括号里。
    """
    title = (title or "").strip()
    bracket = re.match(r"\s*【([^】]+)】", title)
    if bracket and not any(tag in bracket.group(1) for tag in _NAME_TAGS):
        name = _bare_name(bracket.group(1))
        if len(name) >= 2:
            return name
    return _bare_name(title) or title


def short_link(text: str) -> str | None:
    """App 分享出来的 b23.tv 短链。"""
    for url in _urls(text):
        if url.hostname == "b23.tv" and url.path.strip("/"):
            return urllib.parse.urlunsplit(("https", "b23.tv", url.path, "", ""))
    return None


def _fetch_json(url: str, cookie: str, timeout: float = TIMEOUT_S) -> dict:
    request = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": REFERER, "Cookie": cookie})
    with urllib.request.urlopen(request, timeout=timeout) as answer:
        return json.loads(answer.read(MAX_ANSWER_BYTES))


def timed_out(error: BaseException) -> bool:
    """超时（套接字超时，或 urllib 包着的超时）。"""
    reason = getattr(error, "reason", None)
    return isinstance(error, TimeoutError) or isinstance(reason, TimeoutError) \
        or "timed out" in str(error)


def parse_duration(value) -> int:
    """搜索结果的时长：「12:34」「1704:3」（分可以过 60）「1:02:03」，或秒数。看不懂为 0。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    parts = str(value or "").strip().split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def clean_title(text) -> str:
    """去掉搜索结果标题里的 <em class="keyword"> 高亮和 HTML 转义。"""
    return " ".join(html.unescape(_TAGS.sub("", str(text or ""))).split())


def search_hits(data: dict, limit: int = SEARCH_MAX) -> list[dict]:
    """搜索接口的 data → [{"id", "title", "uploader", "duration"}]。

    只留能投屏的普通视频：付费（is_pay、badgepay）、充电专属（is_charge_video）、课程（ketang）、
    番剧和直播间不要；时长看不懂的也不要。"""
    hits, seen = [], set()
    for item in (data or {}).get("result") or []:
        if not isinstance(item, dict) or item.get("type") != "video":
            continue
        bvid = str(item.get("bvid") or "")
        if not _BVID.match(bvid) or bvid in seen or not _playable(item):
            continue
        title, duration = clean_title(item.get("title")), parse_duration(item.get("duration"))
        if not title or duration <= 0:
            continue
        seen.add(bvid)
        hits.append({"id": bvid, "title": title, "uploader": clean_title(item.get("author"))[:40],
                     "duration": duration})
        if len(hits) >= limit:
            break
    return hits


def _playable(item: dict) -> bool:
    """搜索结果里的一个视频能不能投屏：付费、充电专属、番剧、课程、直播间都不行。"""
    url = str(item.get("arcurl") or "")
    return not (item.get("is_pay") or item.get("is_charge_video") or item.get("badgepay") is True
                or item.get("is_live_room_inline") or "/bangumi/" in url or "/cheese/" in url
                or "live.bilibili.com" in url)


def creator_hits(data: dict, limit: int = SEARCH_MAX) -> list[dict]:
    """UP 主搜索的 data → [{"id": mid, "name", "fans", "videos", "latest"}]。

    没有投稿的不要（订阅了也没得播）。latest 是搜索结果顺带给的最新一个能投屏的投稿
    （{"id", "title", "duration"}），「播放最新视频」用它就不必再问一次 B 站；没有时为 None。"""
    hits, seen = [], set()
    for item in (data or {}).get("result") or []:
        if not isinstance(item, dict) or item.get("type") != "bili_user":
            continue
        mid, name = str(item.get("mid") or ""), clean_title(item.get("uname"))
        videos, fans = item.get("videos"), item.get("fans")
        if not mid.isdigit() or len(mid) > 20 or mid in seen or not name:
            continue
        if not isinstance(videos, int) or isinstance(videos, bool) or videos <= 0:
            continue
        latest = None
        for video in item.get("res") or []:
            if isinstance(video, dict) and _BVID.match(str(video.get("bvid") or "")) and _playable(video):
                title, duration = clean_title(video.get("title")), parse_duration(video.get("duration"))
                if title and duration > 0:
                    latest = {"id": video["bvid"], "title": title, "duration": duration}
                break
        seen.add(mid)
        hits.append({"id": mid, "name": name[:40], "videos": videos,
                     "fans": fans if isinstance(fans, int) and not isinstance(fans, bool) else 0,
                     "latest": latest})
        if len(hits) >= limit:
            break
    return hits


def _resolve(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as answer:
        return answer.geturl()


class Bilibili:
    def __init__(self, fetch_json=None, resolve=None):
        self._fetch = fetch_json or _fetch_json
        self._resolve = resolve or _resolve
        self._cookie = ""

    def _call(self, path: str, _timeout: float | None = None, **params) -> dict:
        # 只有语音搜索给 _timeout：取 buvid3 和搜索两次请求加起来不超过它（设备只等 8 秒）；
        # 其余用默认的 TIMEOUT_S。
        fetch = self._fetch
        if _timeout is not None:
            deadline = time.monotonic() + _timeout

            def fetch(url, cookie):
                left = deadline - time.monotonic()
                if left <= 0.05:
                    raise TimeoutError("timed out")
                return self._fetch(url, cookie, timeout=left)
        try:
            if not self._cookie:
                spi = fetch(f"{API}/x/frontend/finger/spi", "")
                self._cookie = f"buvid3={spi['data']['b_3']}"
            url = f"{API}{path}?{urllib.parse.urlencode(params)}"
            answer = fetch(url, self._cookie)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # 出错后换一个 buvid3 再试，旧的可能已被风控。
            self._cookie = ""
            raise BilibiliError(f"{path}: {type(error).__name__}: {error}",
                                reason="slow" if timed_out(error) else "") from None
        if not isinstance(answer, dict):
            answer = {}
        if answer.get("code") != 0:
            self._cookie = ""
            raise BilibiliError(f"{path}: code={answer.get('code')} {answer.get('message', '')}",
                                permanent=True)
        # 番剧（pgc）接口的内容在 result 里，其余在 data 里。
        return answer.get("data") or answer.get("result") or {}

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
        episode = _EPISODE.match(bvid)
        if episode:
            return self._episode_url(episode.group(1), episode.group(2))
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

    def _episode_url(self, part: str, cid: str) -> str:
        if part.startswith("ep"):
            play = self._call("/pgc/player/web/playurl", ep_id=part[2:], cid=cid, qn=16, fnval=1)
            # 会员集在没登录时只给几分钟试看。
            if play.get("is_preview"):
                raise BilibiliError(f"{part}: 只能试看（需要会员）", permanent=True)
        else:
            play = self._call("/x/player/playurl", bvid=part, cid=cid, qn=16, fnval=1,
                              platform="html5")
        durl = play.get("durl") or []
        if not durl or not durl[0].get("url"):
            raise BilibiliError(f"{part}/{cid}: 没有可播放的地址", permanent=True)
        return durl[0]["url"]

    # ---- 剧集 ----

    def series_id(self, text: str) -> str | None:
        """剧集链接 → 来源（番剧统一成「ss<季 id>」，多集视频或合集是 BV 号）。"""
        source = parse_series_link(self._expand(text))
        if source is None or source.startswith(("ss", "BV")):
            return source
        if source.startswith("ep"):
            season = self._call("/pgc/view/web/season", ep_id=source[2:])
        else:
            season = self._call("/pgc/review/user", media_id=source[2:]).get("media") or {}
        if not season.get("season_id"):
            raise BilibiliError(f"{source}: 找不到这部番剧", permanent=True)
        return f"ss{season['season_id']}"

    def series(self, source: str) -> tuple[str, list[Video]]:
        """(剧名, 按顺序的各集)。番剧只留不要会员的集。"""
        try:
            if source.startswith("ss"):
                return self._season(source[2:])
            return self._multipart(source)
        except (KeyError, TypeError, ValueError) as error:
            raise BilibiliError(f"{source}: 剧集信息格式不对：{error}") from None

    def _season(self, season_id: str) -> tuple[str, list[Video]]:
        season = self._call("/pgc/view/web/season", season_id=season_id)
        name = str(season.get("season_title") or season.get("title") or "")
        episodes = []
        for e in season.get("episodes") or []:
            # status 2 是免费；13 是会员专享。
            if e.get("status") != 2 or "会员" in str(e.get("badge") or ""):
                continue
            title = str(e.get("show_title") or "") or f"第{e.get('title')}集 {e.get('long_title') or ''}"
            episodes.append(Video(bvid=f"ep{int(e['id'])}/{int(e['cid'])}", title=title.strip(),
                                  duration=int(e.get("duration") or 0) // 1000, pubdate=0))
        if not episodes:
            raise BilibiliError(f"ss{season_id}: 没有免费的集（都要会员）", permanent=True)
        return name, episodes

    def _multipart(self, bvid: str) -> tuple[str, list[Video]]:
        view = self._call("/x/web-interface/view", bvid=bvid)
        pages = view.get("pages") or []
        if len(pages) > 1:
            return series_name(str(view.get("title", ""))), [
                Video(bvid=f"{bvid}/{int(p['cid'])}",
                      title=str(p.get("part") or f"P{p.get('page')}"),
                      duration=int(p.get("duration") or 0), pubdate=0)
                for p in pages]
        # 一集一个视频、由 UP 主编成「合集」的。
        season = view.get("ugc_season") or {}
        episodes = [Video(bvid=f"{e['bvid']}/{int(e['cid'])}", title=str(e.get("title", "")),
                          duration=int((e.get("arc") or {}).get("duration") or 0), pubdate=0)
                    for section in season.get("sections") or []
                    for e in section.get("episodes") or []]
        if len(episodes) > 1:
            return str(season.get("title") or series_name(str(view.get("title", "")))), episodes
        raise BilibiliError(f"{bvid}: 只有一集，不是剧集", permanent=True)

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

    def search(self, query: str, limit: int = SEARCH_MAX, timeout: float = SEARCH_TIMEOUT_S) -> list[dict]:
        """语音搜索视频：综合排序的第一页（不带签名的搜索接口）。"""
        data = self._call("/x/web-interface/search/type", _timeout=timeout, search_type="video",
                          keyword=query, page=1)
        return search_hits(data, limit)

    def search_creators(self, query: str, limit: int = SEARCH_MAX,
                        timeout: float = SEARCH_TIMEOUT_S) -> list[dict]:
        """语音搜索 UP 主：[{"id": mid, "name", "fans", "videos", "latest"}]。"""
        data = self._call("/x/web-interface/search/type", _timeout=timeout, search_type="bili_user",
                          keyword=query, page=1)
        return creator_hits(data, limit)

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
