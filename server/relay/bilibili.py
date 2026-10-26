"""B 站接口：只用不带签名的网页 API。

B 站对阿里云这个地址的视频网页和带签名（wbi）的列表接口返回 412，yt-dlp 的 B 站解析因此不可用；
下面这几个接口实测可用（见设计文档「验证结果」）。

家庭服务器 / 电脑客户端上，用户在「设置 → B 站账号」扫码登录后（bililogin.py），取播放地址时带上他的
cookies（login 参数）：电脑上看要更高的清晰度（wbi 签名的 /x/player/wbi/playurl），大会员能放会员剧集。
不登录时走原来不带签名的 html5 接口，和以前一样；带 cookies 的请求出错时退回不登录的请求。
"""

from __future__ import annotations

import hashlib
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


class MemberOnly(SourceError):
    """番剧的这一集要大会员：没登录（或不是大会员）时 B 站只给几分钟试看。"""

    def __init__(self, message: str):
        super().__init__(message, permanent=True, reason="vip")


# 「账号未登录」：带的 cookies 被 B 站拒绝了（过期、在别处退出了）。
CODE_NOT_LOGGED_IN = -101


class LoginRejected(SourceError):
    """带登录 cookies 的请求被拒绝（code -101）：登录失效了，调用方退回不登录。"""

    def __init__(self, message: str):
        super().__init__(message, permanent=False, reason="login")


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


# ---- 电脑版「订阅」页的 B 站搜索：带封面、简介的结果，可以翻页 ----

# 一页最多给几个；每次等多久（电脑上的人可以多等一会儿，语音只等 4 秒）；最多翻到第几页。
BROWSE_SIZE = 20
BROWSE_TIMEOUT_S = 8.0
BROWSE_PAGES = 20
# 搜索的种类 → 接口的 search_type。
BROWSE_KINDS = {"video": "video", "user": "bili_user", "bangumi": "media_bangumi"}
DESC_MAX = 160
# 封面和头像只认 B 站图床（*.hdslb.com）上 /bfs/ 下的图片；缩略图代理（bilisearch.py）用同一条规则。
_IMAGE_HOST = re.compile(r"^[a-z0-9-]{1,20}\.hdslb\.com$")
_IMAGE_PATH = re.compile(r"^/bfs/[A-Za-z0-9_./-]{1,200}\.(?:jpe?g|png|webp|gif)$", re.IGNORECASE)
# 标题像多集视频或合集（「全13集」「合集」「第1集」「P1」「EP01」）：给「加为剧集」，真不是时添加会说「只有一集」。
_SERIES_HINT = re.compile(r"全\s*\d+\s*[集话期]|\d+\s*[集话]全|合集|全集|第\s*\d+\s*[集话期季]|(?<![A-Za-z])P\d{1,3}(?!\d)"
                          r"|(?<![A-Za-z])EP\s*\d+"
                          r"|连载|系列|动画片|动画全", re.IGNORECASE)


def cover_url(raw) -> str:
    """搜索结果里的封面地址（常是「//i0.hdslb.com/bfs/...」或 http://）→ https 地址；不是 B 站图床的给空串。"""
    text = str(raw or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    try:
        url = urllib.parse.urlsplit(text)
        port = url.port
    except ValueError:
        return ""
    if url.scheme not in ("http", "https") or port is not None or url.username or url.password \
            or url.query or url.fragment or not url.hostname:
        return ""
    host, path = url.hostname.lower(), url.path
    if not _IMAGE_HOST.match(host) or not _IMAGE_PATH.match(path) or ".." in path or "//" in path:
        return ""
    return f"https://{host}{path}"


def _number(value) -> int:
    """播放数之类：整数，或「12.3万」「--」这种字符串；看不懂为 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    text = str(value or "").strip()
    scale = 1
    if text.endswith("万"):
        text, scale = text[:-1], 10_000
    elif text.endswith("亿"):
        text, scale = text[:-1], 100_000_000
    try:
        return max(0, int(float(text) * scale))
    except ValueError:
        return 0


def _text(value, limit: int) -> str:
    text = clean_title(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def browse_videos(data: dict) -> list[dict]:
    """视频搜索的 data → [{"kind": "video", "id", "title", "desc", "cover", "author", "mid", "duration",
    "play", "pubdate", "series", "link"}]。

    直播间和课程不要（订阅不了，也不是剧）；付费、充电专属的留着（还能订阅它的 UP 主）。番剧的一集
    给的是番剧链接（link），「加为剧集」用它。series：像不像多集视频或合集（只看标题，添加时由 B 站确认）。"""
    items, seen = [], set()
    for item in (data or {}).get("result") or []:
        if not isinstance(item, dict) or item.get("type") != "video":
            continue
        bvid = str(item.get("bvid") or "")
        url = str(item.get("arcurl") or "")
        if not _BVID.match(bvid) or bvid in seen or item.get("is_live_room_inline") \
                or "live.bilibili.com" in url or "/cheese/" in url:
            continue
        title = _text(item.get("title"), 80)
        if not title:
            continue
        mid = str(item.get("mid") or "")
        bangumi = "/bangumi/" in url and parse_series_link(url) is not None
        seen.add(bvid)
        items.append({"kind": "video", "id": bvid, "title": title,
                      "desc": _text(item.get("description"), DESC_MAX),
                      "cover": cover_url(item.get("pic")), "author": _text(item.get("author"), 40),
                      "mid": mid if mid.isdigit() and len(mid) <= 20 else "",
                      "duration": parse_duration(item.get("duration")), "play": _number(item.get("play")),
                      "pubdate": _number(item.get("pubdate")),
                      "series": bangumi or bool(_SERIES_HINT.search(title)),
                      "link": url if bangumi else f"https://www.bilibili.com/video/{bvid}"})
    return items[:BROWSE_SIZE]


def browse_users(data: dict) -> list[dict]:
    """UP 主搜索的 data → [{"kind": "user", "id": mid, "name", "desc", "cover", "fans", "videos"}]。
    没有投稿的不要（订阅了也没得播）。"""
    items, seen = [], set()
    for item in (data or {}).get("result") or []:
        if not isinstance(item, dict) or item.get("type") != "bili_user":
            continue
        mid, name = str(item.get("mid") or ""), _text(item.get("uname"), 40)
        videos = _number(item.get("videos"))
        if not mid.isdigit() or len(mid) > 20 or mid in seen or not name or videos <= 0:
            continue
        seen.add(mid)
        items.append({"kind": "user", "id": mid, "name": name, "desc": _text(item.get("usign"), DESC_MAX),
                      "cover": cover_url(item.get("upic")), "fans": _number(item.get("fans")), "videos": videos})
    return items[:BROWSE_SIZE]


def browse_bangumi(data: dict) -> list[dict]:
    """番剧搜索的 data → [{"kind": "bangumi", "id": "ss<季 id>", "title", "desc", "cover", "eps", "styles",
    "badge", "link"}]。"""
    items, seen = [], set()
    for item in (data or {}).get("result") or []:
        if not isinstance(item, dict):
            continue
        season = item.get("season_id")
        if not isinstance(season, int) or isinstance(season, bool) or season <= 0 or season in seen:
            continue
        title = _text(item.get("title"), 80)
        if not title:
            continue
        seen.add(season)
        items.append({"kind": "bangumi", "id": f"ss{season}", "title": title,
                      "desc": _text(item.get("desc"), DESC_MAX), "cover": cover_url(item.get("cover")),
                      "eps": _number(item.get("ep_size")), "styles": _text(item.get("styles"), 40),
                      "badge": _text(item.get("season_type_name"), 10),
                      "link": f"https://www.bilibili.com/bangumi/play/ss{season}"})
    return items[:BROWSE_SIZE]


_BROWSE_SHAPERS = {"video": browse_videos, "user": browse_users, "bangumi": browse_bangumi}


def _resolve(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as answer:
        return answer.geturl()


# 不登录时 html5 平台给的清晰度：16 = 360p，64 = 720p（B 站按能给的最高档往下给）。
# 云主机 CPU 和带宽都紧，用 360p；家庭服务器在家宽上，用 720p（server/selfhost 传 --bilibili-quality）。
QUALITIES = (16, 32, 64)
DEFAULT_QUALITY = 16
SELFHOST_QUALITY = 64
# 登录了的人在电脑上看（watch.py 的片源）：普通账号 1080P（80），大会员 1080P+（112，高码率）。
# 小电视那块屏很小，还是用上面的 quality（自建 720p），登录只是让会员剧集能放。
PC_QUALITY = 80
PC_QUALITY_VIP = 112
# 不登录时 html5 平台最高给到 720p；比它高的清晰度要登录、用 FLV（fnval=0）。
HTML5_MAX_QUALITY = 64

# ---- wbi 签名（网页版播放器现在用的 /x/player/wbi/playurl 要它） ----
# 算法见 bilibili-API-collect 的 docs/misc/sign/wbi.md：导航接口（/x/web-interface/nav）给的两张图片的文件名
# 拼起来按这张表打乱、取前 32 个字符是 mixin key；参数加上 wts（秒）、按名字排序、去掉值里的 !'()*、
# 编码后接上 mixin key 取 MD5 就是 w_rid。tests/test_relay_bililogin.py 有文档里的例子。
_MIXIN_KEY_ENC_TAB = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
                      33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
                      61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
                      36, 20, 34, 44, 52)
_WBI_KEY = re.compile(r"^[0-9a-f]{32}$")
_WBI_DROP = str.maketrans("", "", "!'()*")
# wbi 的两个 key 每天换一次；取一次用这么久。
WBI_KEYS_S = 3600


def _query(params: dict) -> str:
    """和网页版一样的编码（encodeURIComponent：空格是 %20，斜杠也编码）；签名和真发出去的是同一个串。"""
    return urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="")


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def wbi_sign(params: dict, img_key: str, sub_key: str, now: float) -> dict:
    """带签名的参数（加了 wts、w_rid），按名字排好序。"""
    signed = {key: str(value).translate(_WBI_DROP) for key, value in params.items()}
    signed["wts"] = str(int(now))
    signed = dict(sorted(signed.items()))
    query = _query(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key(img_key, sub_key)).encode("utf-8")).hexdigest()
    return signed


def wbi_keys(nav: dict) -> tuple[str, str] | None:
    """导航接口的 data → (img_key, sub_key)：wbi_img 里两张图片的文件名（不带扩展名）。没登录时也有。"""
    images = (nav or {}).get("wbi_img") if isinstance(nav, dict) else None
    if not isinstance(images, dict):
        return None
    keys = []
    for name in ("img_url", "sub_url"):
        stem = str(images.get(name) or "").rsplit("/", 1)[-1].split(".", 1)[0]
        if not _WBI_KEY.match(stem):
            return None
        keys.append(stem)
    return keys[0], keys[1]


def _single_url(play: dict) -> str:
    """播放地址：只认只有一段的（FLV 老视频会分成好几段，只放第一段就放不完）。"""
    durl = play.get("durl") or []
    if len(durl) != 1 or not isinstance(durl[0], dict) or not durl[0].get("url"):
        return ""
    return str(durl[0]["url"])


def _count(data, *keys) -> int:
    """回答里的一个非负整数（投稿总数之类）；没有或看不懂时为 0。"""
    if not isinstance(data, dict):
        return 0
    for key in keys:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return 0


# 内存里最多记多少个封面、头像地址（一个地址约 80 字节）。
COVERS_MAX = 4000


# 视频信息留多久、最多留几个（parts 用，见上）。
VIEW_KEEP_S = 600
VIEWS_MAX = 32


def view_parts(bvid: str, view: dict) -> tuple[str, str, list[Video]] | None:
    """视频信息（/x/web-interface/view）→ ("pages", 名字, 各 P) 或 ("season", 合集名, 合集里的各集)；
    一集的 id 是「<BV 号>/<cid>」（media_url 认这种 id）。只有一集时 None。"""
    pages = view.get("pages") or []
    if len(pages) > 1:
        return "pages", series_name(str(view.get("title", ""))), [
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
        return "season", str(season.get("title") or series_name(str(view.get("title", "")))), episodes
    return None


class Bilibili:
    def __init__(self, fetch_json=None, resolve=None, quality: int = DEFAULT_QUALITY, clock=time.time):
        self.quality = quality if quality in QUALITIES else DEFAULT_QUALITY
        self._fetch = fetch_json or _fetch_json
        self._resolve = resolve or _resolve
        self._clock = clock
        self._cookie = ""
        # 每个 UP 主（mid）一共有多少个投稿：取投稿列表时 B 站顺带给的（upload_total）。
        self._totals: dict[str, int] = {}
        # wbi 签名的两个 key 和取到的时刻（monotonic）。
        self._wbi: tuple[float, str, str] | None = None
        # 电脑版首页的封面和头像（只在内存里，取列表时 B 站顺带给的图床地址，经 /api/bili/thumb 给页面）：
        # 视频（BV 号、剧集的「ep…/cid」）和番剧（ss…）→ 封面；UP 主（mid）→ 头像。最多各留 COVERS_MAX 个。
        self._covers: dict[str, str] = {}
        self._faces: dict[str, str] = {}
        # 刚取过的视频信息（BV 号 → (时刻, view)）：投到电脑时 video() 和 parts() 只访问一次 B 站。
        self._views: dict[str, tuple[float, dict]] = {}

    def _keep(self, table: dict, key: str, raw) -> None:
        url = cover_url(raw)
        if not url:
            return
        table.pop(key, None)
        table[key] = url
        while len(table) > COVERS_MAX:
            table.pop(next(iter(table)))

    def cover(self, key: str) -> str:
        """视频、剧集的封面地址（hdslb 图床）；没记下来时为空串。"""
        return self._covers.get(str(key), "")

    def face(self, mid: str) -> str:
        """UP 主的头像地址；没记下来时为空串。"""
        return self._faces.get(str(mid), "")

    def _call(self, path: str, _timeout: float | None = None, _login=None, _signed: bool = False,
              **params) -> dict:
        """_login：用户的 B 站登录（bililogin.Login），带上他的 cookies，不用匿名的 buvid3；
        _signed：wbi 签名（只和 _login 一起用：网页版播放器登录后取高清晰度就是这样）。"""
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
            if _login is not None:
                cookie = _login.header()
                if _signed:
                    img, sub = self._wbi_keys(cookie)
                    params = wbi_sign(params, img, sub, self._clock())
                url = f"{API}{path}?{_query(params)}"
            else:
                if not self._cookie:
                    spi = fetch(f"{API}/x/frontend/finger/spi", "")
                    self._cookie = f"buvid3={spi['data']['b_3']}"
                cookie = self._cookie
                url = f"{API}{path}?{urllib.parse.urlencode(params)}"
            answer = fetch(url, cookie)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # 出错后换一个 buvid3 再试，旧的可能已被风控。
            if _login is None:
                self._cookie = ""
            else:
                self._wbi = None
            raise BilibiliError(f"{path}: {type(error).__name__}: {error}",
                                reason="slow" if timed_out(error) else "") from None
        if not isinstance(answer, dict):
            answer = {}
        if answer.get("code") != 0:
            if _login is None:
                self._cookie = ""
            elif answer.get("code") == CODE_NOT_LOGGED_IN:
                _login.rejected()
                raise LoginRejected(f"{path}: 登录已失效")
            else:
                self._wbi = None
            raise BilibiliError(f"{path}: code={answer.get('code')} {answer.get('message', '')}",
                                permanent=True)
        # 番剧（pgc）接口的内容在 result 里，其余在 data 里。
        return answer.get("data") or answer.get("result") or {}

    def _wbi_keys(self, cookie: str) -> tuple[str, str]:
        """wbi 的两个 key：导航接口给（没登录也给，code 是 -101），取一次用 WBI_KEYS_S 秒。"""
        cached = self._wbi
        if cached is not None and time.monotonic() - cached[0] < WBI_KEYS_S:
            return cached[1], cached[2]
        answer = self._fetch(f"{API}/x/web-interface/nav", cookie)
        keys = wbi_keys(answer.get("data") if isinstance(answer, dict) else None)
        if keys is None:
            raise ValueError("导航接口没给 wbi key")
        self._wbi = (time.monotonic(), keys[0], keys[1])
        return keys

    def uploads(self, mid: str) -> list[Video]:
        """最新的 LIST_SIZE 个投稿（只取第一页）。UP 主一共有多少个投稿顺带记下（upload_total）。"""
        data = self._call("/x/space/arc/list", mid=mid, ps=LIST_SIZE, pn=1)
        try:
            videos = [Video(bvid=a["bvid"], title=str(a.get("title", "")),
                            duration=int(a.get("duration", 0)), pubdate=int(a.get("pubdate", 0)))
                      for a in data.get("archives") or []]
        except (KeyError, TypeError, ValueError) as error:
            raise BilibiliError(f"投稿列表格式不对：{error}") from None
        total = _count(data.get("page"), "count", "total")
        if total:
            self._totals[str(mid)] = max(total, len(videos))
        for a in data.get("archives") or []:
            if isinstance(a, dict) and a.get("bvid"):
                self._keep(self._covers, str(a["bvid"]), a.get("pic"))
        return videos

    def creator(self, mid: str) -> tuple[str, list[Video]]:
        card = self._call("/x/web-interface/card", mid=mid)
        try:
            name = str(card["card"]["name"])
        except (KeyError, TypeError):
            raise BilibiliError("拿不到 UP 主名字") from None
        self._keep(self._faces, str(mid), (card.get("card") or {}).get("face"))
        videos = self.uploads(mid)
        # 列表接口没给总数时，名片里的投稿数（archive_count）也行。
        if str(mid) not in self._totals:
            total = _count(card, "archive_count")
            if total:
                self._totals[str(mid)] = max(total, len(videos))
        return name, videos

    def upload_total(self, mid: str) -> int:
        """UP 主一共有多少个投稿（最近一次取列表时 B 站说的）；不知道时为 0。中转只取最新的 LIST_SIZE 个。"""
        return self._totals.get(str(mid), 0)

    def media_url(self, bvid: str, login=None, quality: int | None = None) -> str:
        """播放地址。login：用户的 B 站登录（bililogin.Login），quality：要的清晰度（不给就是 self.quality）。
        不登录时 quality 不起作用：html5 接口按 self.quality 给，和以前一样。"""
        episode = _EPISODE.match(bvid)
        if episode:
            return self._episode_url(episode.group(1), episode.group(2), login, quality)
        view = self._call("/x/web-interface/view", bvid=bvid)
        cid = view.get("cid")
        if not cid:
            raise BilibiliError(f"{bvid}: 没有 cid", permanent=True)
        return self._ugc_url(bvid, cid, login, quality)

    def _html5_url(self, bvid: str, cid, login=None, quality: int | None = None) -> str:
        play = self._call("/x/player/playurl", _login=login, bvid=bvid, cid=cid,
                          qn=min(quality or self.quality, HTML5_MAX_QUALITY), fnval=1, platform="html5")
        durl = play.get("durl") or []
        if not durl or not durl[0].get("url"):
            raise BilibiliError(f"{bvid}: 没有可播放的地址", permanent=True)
        return durl[0]["url"]

    def _ugc_url(self, bvid: str, cid, login=None, quality: int | None = None) -> str:
        """普通视频（和多集视频的一 P）。登录了：要 720p 以上时用 wbi 签名的接口取 FLV（单段的才要），
        否则带着 cookies 走 html5；带 cookies 的请求出错时退回不登录的 html5（和以前一样）。"""
        if login is not None:
            wanted = quality or self.quality
            try:
                if wanted > HTML5_MAX_QUALITY:
                    play = self._call("/x/player/wbi/playurl", _login=login, _signed=True, bvid=bvid, cid=cid,
                                      qn=wanted, fnval=0, fnver=0, fourk=0)
                    url = _single_url(play)
                    if url:
                        return url
            except LoginRejected:
                return self._html5_url(bvid, cid)
            except BilibiliError:
                pass
            try:
                return self._html5_url(bvid, cid, login, wanted)
            except BilibiliError:
                pass
        return self._html5_url(bvid, cid)

    def _episode_url(self, part: str, cid: str, login=None, quality: int | None = None) -> str:
        if not part.startswith("ep"):
            return self._ugc_url(part, cid, login, quality)
        play = None
        if login is not None:
            wanted = quality or self.quality
            try:
                if wanted > HTML5_MAX_QUALITY:
                    play = self._call("/pgc/player/web/playurl", _login=login, ep_id=part[2:], cid=cid,
                                      qn=wanted, fnval=0, fnver=0, fourk=0)
                    if not play.get("is_preview") and not _single_url(play):
                        play = None     # 分了好几段：要一个单段的 MP4
                if play is None:
                    play = self._call("/pgc/player/web/playurl", _login=login, ep_id=part[2:], cid=cid,
                                      qn=min(wanted, HTML5_MAX_QUALITY), fnval=1)
            except BilibiliError as error:
                if isinstance(error, LoginRejected) or not error.permanent:
                    play = None         # 登录失效、网络问题：退回不登录
                else:
                    raise
        if play is None:
            play = self._call("/pgc/player/web/playurl", ep_id=part[2:], cid=cid, qn=self.quality, fnval=1)
        # 会员集在没登录（或不是大会员）时只给几分钟试看。
        if play.get("is_preview"):
            raise MemberOnly(f"{part}: 只能试看（需要大会员）")
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

    def series(self, source: str, login=None) -> tuple[str, list[Video]]:
        """(剧名, 按顺序的各集)。番剧只留不要会员的集；login 是大会员的登录时，会员集也留着。"""
        try:
            if source.startswith("ss"):
                return self._season(source[2:], vip=bool(login is not None and login.vip))
            return self._multipart(source)
        except (KeyError, TypeError, ValueError) as error:
            raise BilibiliError(f"{source}: 剧集信息格式不对：{error}") from None

    def _season(self, season_id: str, vip: bool = False) -> tuple[str, list[Video]]:
        season = self._call("/pgc/view/web/season", season_id=season_id)
        name = str(season.get("season_title") or season.get("title") or "")
        self._keep(self._covers, f"ss{season_id}", season.get("cover"))
        episodes = []
        for e in season.get("episodes") or []:
            # status 2 是免费；13 是会员专享（大会员登录了才留）；别的（付费、下架）都不要。
            member = e.get("status") == 13 or "会员" in str(e.get("badge") or "")
            if member and not vip:
                continue
            if not member and e.get("status") != 2:
                continue
            title = str(e.get("show_title") or "") or f"第{e.get('title')}集 {e.get('long_title') or ''}"
            episodes.append(Video(bvid=f"ep{int(e['id'])}/{int(e['cid'])}", title=title.strip(),
                                  duration=int(e.get("duration") or 0) // 1000, pubdate=0))
            self._keep(self._covers, episodes[-1].bvid, e.get("cover"))
        if not episodes:
            raise BilibiliError(f"ss{season_id}: 没有免费的集（都要会员）", permanent=True)
        return name, episodes

    def _multipart(self, bvid: str) -> tuple[str, list[Video]]:
        view = self._call("/x/web-interface/view", bvid=bvid)
        self._keep(self._covers, bvid, view.get("pic"))
        found = view_parts(bvid, view)
        if found is None:
            raise BilibiliError(f"{bvid}: 只有一集，不是剧集", permanent=True)
        return found[1], found[2]

    def parts(self, bvid: str) -> tuple[str, str, list[Video]] | None:
        """电脑上看一个投过来的视频时的「选集」：多 P 视频的各 P（"pages"）或它所在的合集（"season"），
        (种类, 名字, 各集)；只有一集时 None。刚取过这个视频的信息（video()）就不再访问 B 站。"""
        kept = self._views.get(bvid)
        if kept is not None and self._clock() - kept[0] < VIEW_KEEP_S:
            view = kept[1]
        else:
            view = self._call("/x/web-interface/view", bvid=bvid)
            self._keep_view(bvid, view)
        try:
            return view_parts(bvid, view)
        except (KeyError, TypeError, ValueError) as error:
            raise BilibiliError(f"{bvid}: 分 P 信息格式不对：{error}") from None

    def _keep_view(self, bvid: str, view: dict) -> None:
        self._views.pop(bvid, None)
        self._views[bvid] = (self._clock(), view)
        while len(self._views) > VIEWS_MAX:
            self._views.pop(next(iter(self._views)))

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

    def browse(self, query: str, kind: str = "video", page: int = 1,
               timeout: float = BROWSE_TIMEOUT_S) -> dict:
        """电脑版的搜索：{"items": [...], "page", "pages", "total"}。kind 是 video、user 或 bangumi。"""
        if kind not in BROWSE_KINDS:
            raise ValueError(kind)
        data = self._call("/x/web-interface/search/type", _timeout=timeout, search_type=BROWSE_KINDS[kind],
                          keyword=query, page=page)
        pages = min(_count(data, "numPages"), BROWSE_PAGES)
        return {"items": _BROWSE_SHAPERS[kind](data), "page": page, "pages": pages,
                "total": _count(data, "numResults")}

    def video(self, bvid: str) -> Video:
        view = self._call("/x/web-interface/view", bvid=bvid)
        self._keep(self._covers, bvid, view.get("pic"))
        self._keep_view(bvid, view)
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
