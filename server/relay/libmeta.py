"""片库的在线资料（M7）：从豆瓣取真正的片名、海报、年份、评分、导演 / 演员、类型、简介。

为什么这样做：主人的片子大多是网上分享来的，文件名是「Narco-Saints.S01E01.2160p.NF.WEB-DL…」或者干脆是
「4K HQ SDR 高码率 DDP DTS.mp4」，片库只认文件名和本地的 .nfo 时，卡片上是生成的占位图和看不懂的名字。

- 不登录、没有账号、不带 Cookie。先用 movie.douban.com/j/subject_suggest?q=<片名> 找候选（JSON：id、title、
  sub_title、year、img、episode），再取一个候选的详细资料。详细资料先取 m.douban.com/rexxar/api/v2/subject/<id>
  （JSON），取不到再取条目页 movie.douban.com/subject/<id>/ 里的 <script type="application/ld+json"> 和资料块：
  2026-10-28 实测条目页对不带 Cookie 的请求一律 302 到 sec.douban.com 的验证页，手机版的接口照常回答。验证页从不
  去「过」，只当成被拦了。
- 客气：一次只发一个请求，两个请求之间至少隔 GAP_S 秒（图片 IMG_GAP_S 秒）；回 403 / 429、跳到 sec.douban.com、
  页面里有「检测到有异常请求」都当被拦了，BACKOFF_S 秒内一个请求也不发。用普通桌面浏览器的 User-Agent，Referer
  是 https://movie.douban.com/（图片也要带 Referer，不然 403）。
- 只在有把握时自动认：片名（或英文名、原名）规整以后完全一样，知道年份时年份也要一样，剧集只认电视剧、电影只认
  电影，同样像的有两个以上就不认。英文名查出来的候选，片名对不上时再看它的又名（aka）里有没有这个英文名。
  没把握就留着「没对上」，等管理员手动选；没对上的过 MISS_RETRY_S 秒再试一次（豆瓣可能后来才有）。
- 海报只经中转取一次，存在 library-art/（douban-<id>.jpg），浏览器从不直接连豆瓣。取下来的要真是图片（看文件头：
  JPEG、PNG、WebP），至多 POSTER_MAX 字节。
- 资料存在 library-meta.json（600，和 library.json 一样整份换掉写），按剧（剧名）或电影（条目 id）记，重新扫描
  不会丢。手动选的、手动「不匹配」的从不被自动的盖掉。

备用的资料来源 TMDB（The Movie Database，官方的 API v3）：豆瓣没把握（没对上）、拦着、连不上时才查它，豆瓣永远
先查。要主人自己去 TMDB 免费注册一个 API 密钥（32 位的 API Key 或者长的 API Read Access Token），在片库里粘贴；
存在数据目录的 secrets/tmdb-key（600），从不写进日志、从不回给页面（只说「已设置」）。没填就不用 TMDB。

- 自动查：/3/search/tv 或 /3/search/movie（language=zh-CN，知道年份时带上 first_air_date_year /
  primary_release_year），认的规矩和豆瓣一样（pick：片名或原名规整以后一样、年份一样、种类一样、只有一个）；中文名
  对不上时用英文名再查一次（language=en-US，这时的 title 是英文名）。详细资料是 /3/tv/<id> 或 /3/movie/<id>
  （language=zh-CN&append_to_response=credits）：简介、类型、导演（剧没有导演时用主创 created_by）、主演、海报。
- 海报从 image.tmdb.org/t/p/w500<poster_path> 取一次存进 library-art/（tmdb-tv-<id>.jpg），和豆瓣的一样看文件头、
  有大小上限；手动匹配的小框里的小图用 w185，只在内存里。
- 客气：一次一个请求、至少隔 TMDB_GAP_S 秒；回 429 歇 TMDB_BACKOFF_S 秒；连着 TMDB_DOWN_AFTER 次连不上（在国内
  常被墙）歇 TMDB_DOWN_S 秒。出去的请求经「设置 → YouTube」里填的代理（proxy()；没填就和 urllib 一样看环境变量）。
- 测试和自测用环境变量 TV_TMDB_API、TV_TMDB_IMAGE 换成本机的假服务器（只认 http://127.0.0.1 / localhost）。

只用标准库。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from html import unescape
from pathlib import Path
from typing import Callable

SUGGEST_URL = "https://movie.douban.com/j/subject_suggest?q="
SUBJECT_URL = "https://movie.douban.com/subject/{}/"
# 手机版的影视搜索：subject_suggest 对不带 Cookie 的请求时常整个回空（[]，查什么都一样），这时改用它。
SEARCH_URL = "https://m.douban.com/rexxar/api/v2/search/movie?start=0&count=10&q="
SEARCH_REFERER = "https://m.douban.com/search/"
MOBILE_URL = "https://m.douban.com/rexxar/api/v2/subject/{}"
REFERER = "https://movie.douban.com/"
MOBILE_REFERER = "https://m.douban.com/movie/subject/{}/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
TIMEOUT_S = 20
# 两个请求之间至少隔这么久（图片宽一点）；被拦了这么久以内一个请求也不发。
GAP_S = 2.5
IMG_GAP_S = 0.6
BACKOFF_S = 3600.0
# 没对上的过这么久再试；详细资料没取全（被拦、出错）的过这么久再补。
MISS_RETRY_S = 3 * 86400.0
PARTIAL_RETRY_S = 6 * 3600.0
# 回答至多这么大：搜索的 JSON、详细资料、条目页。
ANSWER_MAX = 2 * 1024 * 1024
# 海报（存盘的）和搜索结果里的小图（只在内存里）。
POSTER_MAX = 2 * 1024 * 1024
THUMB_MAX = 400 * 1024
THUMBS_KEPT = 60
CANDIDATES_KEPT = 300
CANDIDATES_MAX = 10
# 一条资料里各项至多这么长、这么多个。
TEXT_MAX = 120
PLOT_MAX = 1200
PEOPLE_MAX = 8
GENRES_MAX = 6
ENTRIES_MAX = 20000
QUERY_MAX = 60
_DOUBAN_ID = re.compile(r"^\d{1,12}$")
_IMG_HOST = re.compile(r"^img\d{0,2}\.doubanio\.com$")
_BLOCK_WORDS = ("检测到有异常请求", "sec.douban.com")


class Blocked(Exception):
    """豆瓣拦了（403 / 429 / 验证页）：BACKOFF_S 秒内不再发请求。"""


class Failed(Exception):
    """这一次没取到（不是被拦）：给人看的原因。"""


# ---------------------------------------------------------------- 规整、图片

def norm(text) -> str:
    """比片名用：全角换半角、不分大小写，只留字母、数字和汉字（「Narco-Saints」「narco saints」一样）。"""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(c for c in text if c.isalnum())


def image_kind(data: bytes) -> str:
    """看文件头：jpg / png / webp；不是图片 ""。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def good_image_url(url) -> bool:
    """只认豆瓣自己的图床（https://imgN.doubanio.com/…）：中转只替页面取这一种地址。"""
    if not isinstance(url, str) or len(url) > 400:
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and bool(_IMG_HOST.match(parts.hostname or "")) and not parts.username


def poster_url(url: str) -> str:
    """搜索结果里的小图（s_ratio_poster，约 270 宽）换成中等大小的（m_ratio_poster，约 540 宽）。"""
    return url.replace("/s_ratio_poster/", "/m_ratio_poster/") if good_image_url(url) else ""


def _text(value, cap: int = TEXT_MAX) -> str:
    # 豆瓣的片名有时夹着看不见的方向标记（U+200E 之类）：显示、搜索、比片名时都碍事，去掉格式字符。
    text = "".join(c for c in unescape(str(value or "")) if unicodedata.category(c) != "Cf")
    return " ".join(text.split())[:cap]


def _year(value) -> int | None:
    found = re.search(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", str(value or ""))
    return int(found.group(1)) if found else None


def _names(rows) -> list[str]:
    found = []
    for row in rows if isinstance(rows, list) else []:
        name = _text(row.get("name") if isinstance(row, dict) else row, 60)
        if name and name not in found:
            found.append(name)
        if len(found) >= PEOPLE_MAX:
            break
    return found


def _rating(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 1) if 0 < number <= 10 else None


# ---------------------------------------------------------------- 解析豆瓣的回答

def parse_suggest(data: bytes | str) -> list[dict]:
    """subject_suggest 的 JSON → 候选 [{id, title, sub_title, year, type, episodes, img}]（电影、电视剧，别的不要）。
    这个接口的 type 对电视剧也常写 movie，有集数（episode）的算电视剧。"""
    try:
        rows = json.loads(data)
    except (TypeError, ValueError):
        return []
    found = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        did = str(row.get("id") or "")
        title = _text(row.get("title"))
        if not _DOUBAN_ID.match(did) or not title or row.get("type") not in ("movie", "tv", None):
            continue
        episodes = str(row.get("episode") or "").strip()
        kind = "tv" if row.get("type") == "tv" or (episodes.isdigit() and int(episodes) > 0) else "movie"
        found.append({"id": did, "title": title, "sub_title": _text(row.get("sub_title")),
                      "year": _year(row.get("year")), "type": kind,
                      "episodes": int(episodes) if episodes.isdigit() else None,
                      "img": row["img"] if good_image_url(row.get("img")) else ""})
        if len(found) >= CANDIDATES_MAX:
            break
    return found


def parse_search(data: bytes | str) -> list[dict]:
    """手机版影视搜索的 JSON（{items: [{target_type, target: {id, title, year, cover_url}}]}）→ 和 parse_suggest
    一样的候选；只要电影、电视剧。"""
    try:
        answer = json.loads(data)
    except (TypeError, ValueError):
        return []
    found = []
    rows = answer.get("items") if isinstance(answer, dict) else None
    for row in rows if isinstance(rows, list) else []:
        target = row.get("target") if isinstance(row, dict) else None
        kind = row.get("target_type") if isinstance(row, dict) else None
        if not isinstance(target, dict) or kind not in ("movie", "tv"):
            continue
        did = str(target.get("id") or "")
        title = _text(target.get("title"))
        if not _DOUBAN_ID.match(did) or not title:
            continue
        found.append({"id": did, "title": title, "sub_title": "", "year": _year(target.get("year")), "type": kind,
                      "episodes": None, "img": target["cover_url"] if good_image_url(target.get("cover_url")) else ""})
        if len(found) >= CANDIDATES_MAX:
            break
    return found


def parse_mobile(data: bytes | str) -> dict | None:
    """m.douban.com/rexxar/api/v2/subject/<id> 的 JSON → 资料；看不懂 None。"""
    try:
        row = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(row, dict) or not _DOUBAN_ID.match(str(row.get("id") or "")) or not _text(row.get("title")):
        return None
    pic = row.get("pic") if isinstance(row.get("pic"), dict) else {}
    img = next((u for u in (pic.get("large"), pic.get("normal"), row.get("cover_url")) if good_image_url(u)), "")
    rating = row.get("rating") if isinstance(row.get("rating"), dict) else {}
    tv = bool(row.get("is_tv")) or row.get("type") == "tv" or row.get("subtype") == "tv"
    episodes = row.get("episodes_count")
    return {"id": str(row["id"]), "title": _text(row.get("title")), "original": _text(row.get("original_title")),
            "year": _year(row.get("year")) or _year((row.get("pubdate") or [""])[0] if isinstance(row.get("pubdate"), list) else ""),
            "type": "tv" if tv else "movie", "rating": _rating(rating.get("value")),
            "votes": int(rating.get("count") or 0) if str(rating.get("count") or "0").isdigit() else 0,
            "genres": [_text(g, 20) for g in (row.get("genres") or [])[:GENRES_MAX] if _text(g, 20)]
            if isinstance(row.get("genres"), list) else [],
            "directors": _names(row.get("directors")), "actors": _names(row.get("actors")),
            "countries": [_text(c, 20) for c in (row.get("countries") or [])[:4] if _text(c, 20)]
            if isinstance(row.get("countries"), list) else [],
            "plot": _text(row.get("intro"), PLOT_MAX),
            "episodes": int(episodes) if isinstance(episodes, int) and episodes > 0 else None,
            "aka": [_text(a) for a in (row.get("aka") or [])[:12] if _text(a)] if isinstance(row.get("aka"), list) else [],
            "img": img}


_LD_JSON = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_INFO_EPISODES = re.compile(r'<span class="pl">\s*集数:?\s*</span>:?\s*(\d{1,4})')
_INFO_GENRE = re.compile(r'<span property="v:genre">([^<]{1,20})</span>')
_INFO_YEAR = re.compile(r'<span class="year">\(\s*(\d{4})\s*\)</span>')
_INFO_AKA = re.compile(r'<span class="pl">\s*又名:?\s*</span>:?\s*([^<]{1,600})')


def parse_subject(html: bytes | str, did: str = "") -> dict | None:
    """条目页 → 资料：ld+json（name、image、director、actor、genre、datePublished、description、aggregateRating）
    加上资料块里的集数、类型、年份、又名。ld+json 里常有没转义的换行（strict=False）。看不懂 None。"""
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    found = _LD_JSON.search(html)
    if found is None:
        return None
    try:
        row = json.loads(found.group(1), strict=False)
    except ValueError:
        return None
    if not isinstance(row, dict) or not _text(row.get("name")):
        return None
    url = str(row.get("url") or "")
    sid = re.search(r"/subject/(\d+)", url)
    name = _text(row.get("name"))
    # name 是「中文名 原名」：页面标题里中文名在前；原名留给 original。
    title, _, original = name.partition(" ")
    rating = row.get("aggregateRating") if isinstance(row.get("aggregateRating"), dict) else {}
    episodes = _INFO_EPISODES.search(html)
    genres = row.get("genre") if isinstance(row.get("genre"), list) else []
    genres = [_text(g, 20) for g in genres if _text(g, 20)] or [_text(g, 20) for g in _INFO_GENRE.findall(html)]
    year = _INFO_YEAR.search(html)
    aka = _INFO_AKA.search(html)
    count = str(rating.get("ratingCount") or "0")
    img = row.get("image")
    return {"id": sid.group(1) if sid else did, "title": title, "original": original.strip(),
            "year": int(year.group(1)) if year else _year(row.get("datePublished")),
            "type": "tv" if episodes or row.get("@type") == "TVSeries" else "movie",
            "rating": _rating(rating.get("ratingValue")), "votes": int(count) if count.isdigit() else 0,
            "genres": genres[:GENRES_MAX], "directors": _names(row.get("director")), "actors": _names(row.get("actor")),
            "countries": [], "plot": _text(row.get("description"), PLOT_MAX),
            "episodes": int(episodes.group(1)) if episodes else None,
            "aka": [_text(a) for a in unescape(aka.group(1)).split("/") if _text(a)][:12] if aka else [],
            "img": poster_url(img) or (img if good_image_url(img) else "")}


def blocked_page(status: int, location: str, body: bytes) -> bool:
    """豆瓣拦了：403 / 429、跳到 sec.douban.com、页面里说「检测到有异常请求」。"""
    if status in (403, 429) or "sec.douban.com" in (location or ""):
        return True
    return any(word.encode("utf-8") in body[:20000] for word in _BLOCK_WORDS[:1])


# ---------------------------------------------------------------- 认不认

def pick(candidates: list[dict], names: list[str], year: int | None, want: str | None) -> dict | None:
    """有把握的那一个：片名（或原名）规整以后和 names 里的一个完全一样；want（tv / movie）对得上；year 给了就要一样。
    这样的有两个以上（不同的条目）就不认。"""
    wanted = {norm(n) for n in names if norm(n)}
    good = []
    for c in candidates:
        if want and c.get("type") != want:
            continue
        if year is not None and c.get("year") != year:
            continue
        if norm(c.get("title")) in wanted or (c.get("sub_title") and norm(c["sub_title"]) in wanted):
            good.append(c)
    ids = {c["id"] for c in good}
    return good[0] if len(ids) == 1 else None


# ---------------------------------------------------------------- 取

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """跳转不跟：豆瓣的跳转多半是去验证页，要看到它。"""

    def redirect_request(self, *args, **kwargs):
        return None


def default_fetch(url: str, headers: dict, cap: int, timeout: float = TIMEOUT_S) -> tuple[int, str, bytes]:
    """GET → (状态, 跳转地址, 至多 cap+1 字节)。跟着环境里的代理（和 urllib 一样），跳转不跟。"""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as answer:
            return getattr(answer, "status", 200), "", answer.read(cap + 1)
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location") or ""
        try:
            body = error.read(cap + 1) if error.code not in (301, 302, 303, 307, 308) else b""
        except OSError:
            body = b""
        error.close()
        return error.code, location, body
    except (OSError, ValueError) as error:
        raise Failed(f"连不上豆瓣（{type(error).__name__}）") from None


class Douban:
    """豆瓣：一次一个请求、请求之间有间隔、被拦了就歇。fetch(url, headers, cap) → (状态, 跳转地址, 内容)。"""

    def __init__(self, fetch: Callable | None = None, clock=time.time, sleep=time.sleep, log: Callable[[str], None] = print):
        self.fetch = fetch or default_fetch
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self._lock = threading.Lock()
        self._last = 0.0
        self.blocked_until = 0.0
        # 最近搜到的候选（只给页面看小图用：中转只替页面取这些图）和取过的小图。
        self._seen: OrderedDict[str, dict] = OrderedDict()
        self._thumbs: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self.requests = 0

    def blocked(self) -> bool:
        return self.clock() < self.blocked_until

    def _get(self, url: str, referer: str, accept: str, cap: int, gap: float) -> bytes:
        headers = {"User-Agent": UA, "Referer": referer, "Accept": accept,
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"}
        with self._lock:
            if self.blocked():
                raise Blocked()
            wait = self._last + gap - self.clock()
            if wait > 0:
                self.sleep(wait)
            try:
                self.requests += 1
                status, location, body = self.fetch(url, headers, cap)
            finally:
                self._last = self.clock()
            if blocked_page(status, location, body):
                self.blocked_until = self.clock() + BACKOFF_S
                self.log(f"豆瓣拦了请求（{status}），歇 {int(BACKOFF_S // 60)} 分钟")
                raise Blocked()
        if status != 200:
            raise Failed(f"豆瓣回答 {status}")
        if len(body) > cap:
            raise Failed("豆瓣的回答太大")
        return body

    def suggest(self, query: str) -> list[dict]:
        query = " ".join(str(query or "").split())[:QUERY_MAX]
        if not query:
            return []
        found = parse_suggest(self._get(SUGGEST_URL + urllib.parse.quote(query), REFERER,
                                        "application/json, text/plain, */*", ANSWER_MAX, GAP_S))
        if not found:
            found = self.search(query, remember=False)
        with self._lock:
            for c in found:
                self._seen[c["id"]] = c
                self._seen.move_to_end(c["id"])
            while len(self._seen) > CANDIDATES_KEPT:
                self._seen.popitem(last=False)
        return found

    def search(self, query: str, remember: bool = True) -> list[dict]:
        """手机版的影视搜索（parse_search）：候选比 subject_suggest 全（「龙」查得到 2013 年的电影）。出错时 []。"""
        query = " ".join(str(query or "").split())[:QUERY_MAX]
        if not query:
            return []
        try:
            found = parse_search(self._get(SEARCH_URL + urllib.parse.quote(query), SEARCH_REFERER,
                                           "application/json, text/plain, */*", ANSWER_MAX, GAP_S))
        except Failed:
            return []
        if remember:
            with self._lock:
                for c in found:
                    self._seen[c["id"]] = c
                    self._seen.move_to_end(c["id"])
                while len(self._seen) > CANDIDATES_KEPT:
                    self._seen.popitem(last=False)
        return found

    def seen(self, did: str) -> dict | None:
        with self._lock:
            found = self._seen.get(did)
            return dict(found) if found else None

    def details(self, did: str) -> dict:
        """一个条目的资料：先手机版的接口，取不到再条目页。被拦了抛 Blocked，都没取到抛 Failed。"""
        if not _DOUBAN_ID.match(str(did)):
            raise Failed("豆瓣的编号不对")
        try:
            found = parse_mobile(self._get(MOBILE_URL.format(did), MOBILE_REFERER.format(did),
                                           "application/json, text/plain, */*", ANSWER_MAX, GAP_S))
            if found is not None:
                return found
        except Failed:
            pass
        found = parse_subject(self._get(SUBJECT_URL.format(did), REFERER, "text/html,application/xhtml+xml",
                                        ANSWER_MAX, GAP_S), did)
        if found is None:
            raise Failed("豆瓣的条目页看不懂")
        return found

    def image(self, url: str, cap: int = POSTER_MAX) -> tuple[str, bytes]:
        """取一张豆瓣的图（带 Referer）：(jpg / png / webp, 内容)。不是图片、太大抛 Failed。"""
        if not good_image_url(url):
            raise Failed("不是豆瓣的图片地址")
        data = self._get(url, REFERER, "image/avif,image/webp,image/*,*/*;q=0.8", cap, IMG_GAP_S)
        kind = image_kind(data)
        if not kind:
            raise Failed("豆瓣给的不是图片")
        return kind, data

    def thumb(self, did: str) -> tuple[str, bytes] | None:
        """搜索结果里一个候选的小图（只认最近搜到过的候选）；内存里留 THUMBS_KEPT 张。"""
        with self._lock:
            cached = self._thumbs.get(did)
            candidate = self._seen.get(did)
        if cached is not None:
            return cached
        if candidate is None or not candidate.get("img"):
            return None
        try:
            found = self.image(candidate["img"], THUMB_MAX)
        except (Blocked, Failed):
            return None
        with self._lock:
            self._thumbs[did] = found
            while len(self._thumbs) > THUMBS_KEPT:
                self._thumbs.popitem(last=False)
        return found


# ---------------------------------------------------------------- TMDB（备用）

TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p"
TMDB_POSTER_SIZE = "w500"
TMDB_THUMB_SIZE = "w185"
TMDB_TIMEOUT_S = 15
TMDB_GAP_S = 1.0
TMDB_BACKOFF_S = 600.0
TMDB_DOWN_AFTER = 3
TMDB_DOWN_S = 1800.0
TMDB_KEY_FILE = "tmdb-key"
# 密钥的两种样子：v3 的 API Key（32 位十六进制，放在地址的 api_key 里）、API Read Access Token（JWT，放在
# Authorization: Bearer 里，v3 v4 都认）。别的不收（防止粘贴错了东西，例如整段说明文字）。
_TMDB_V3_KEY = re.compile(r"^[0-9a-f]{32}$")
_TMDB_TOKEN = re.compile(r"^eyJ[A-Za-z0-9_-]{8,600}\.[A-Za-z0-9_-]{8,1200}\.[A-Za-z0-9_-]{8,600}$")
_TMDB_ID = re.compile(r"^\d{1,10}$")
_TMDB_PATH = re.compile(r"^/[A-Za-z0-9_-]{4,80}\.(?:jpg|jpeg|png|webp)$")
_TMDB_PICK = re.compile(r"^tmdb:(tv|movie):(\d{1,10})$")
_LOCAL_TEST_BASE = re.compile(r"^http://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?(?:/[A-Za-z0-9_./-]*)?$")
# TMDB 的国家是 ISO 3166-1 的两个字母（zh-CN 也不给中文名）：常见的换成中文，别的不显示。
_COUNTRIES = {"CN": "中国大陆", "HK": "中国香港", "TW": "中国台湾", "US": "美国", "GB": "英国", "KR": "韩国",
              "JP": "日本", "FR": "法国", "DE": "德国", "IT": "意大利", "ES": "西班牙", "CA": "加拿大",
              "AU": "澳大利亚", "IN": "印度", "TH": "泰国", "RU": "俄罗斯", "SG": "新加坡", "NZ": "新西兰"}


def tmdb_key_ok(text) -> bool:
    """粘贴进来的像不像 TMDB 的 API Key / Read Access Token。"""
    return isinstance(text, str) and bool(_TMDB_V3_KEY.match(text) or _TMDB_TOKEN.match(text))


def tmdb_pick_id(value) -> tuple[str, str] | None:
    """手动匹配时页面送来的 TMDB 候选「tmdb:tv:1399」→ (tv, 1399)；不是 None。"""
    found = _TMDB_PICK.match(str(value or ""))
    return (found.group(1), found.group(2)) if found else None


def _tmdb_year(row: dict) -> int | None:
    return _year(row.get("release_date") or row.get("first_air_date") or "")


def _tmdb_path(value) -> str:
    return value if isinstance(value, str) and _TMDB_PATH.match(value) else ""


def _tmdb_id(value) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) and _TMDB_ID.match(str(value)) else ""


def parse_tmdb_search(data: bytes | str, kind: str) -> list[dict]:
    """/3/search/movie、/3/search/tv 的 JSON（{results: [{id, title|name, original_title|original_name,
    release_date|first_air_date, poster_path}]}）→ 和 parse_suggest 一样的候选（多 src = "tmdb"、tid 是 TMDB 的
    编号，id 是「tmdb:tv:<编号>」这样带来源和种类的，img 是 poster_path）。"""
    try:
        answer = json.loads(data)
    except (TypeError, ValueError):
        return []
    rows = answer.get("results") if isinstance(answer, dict) else None
    found = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("adult") is True:
            continue
        tid = _tmdb_id(row.get("id"))
        title = _text(row.get("title") if kind == "movie" else row.get("name"))
        if not tid or not title:
            continue
        original = _text(row.get("original_title") if kind == "movie" else row.get("original_name"))
        found.append({"id": f"tmdb:{kind}:{tid}", "tid": tid, "src": "tmdb", "title": title,
                      "sub_title": original if original != title else "", "year": _tmdb_year(row), "type": kind,
                      "episodes": None, "img": _tmdb_path(row.get("poster_path"))})
        if len(found) >= CANDIDATES_MAX:
            break
    return found


def parse_tmdb_details(data: bytes | str, kind: str) -> dict | None:
    """/3/movie/<id>、/3/tv/<id>（append_to_response=credits）的 JSON → 和 parse_mobile 一样的资料
    （多 src = "tmdb"，id 是 TMDB 的编号，img 是 poster_path）。看不懂 None。"""
    try:
        row = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    tid = _tmdb_id(row.get("id"))
    title = _text(row.get("title") if kind == "movie" else row.get("name"))
    if not tid or not title:
        return None
    credits = row.get("credits") if isinstance(row.get("credits"), dict) else {}
    crew = credits.get("crew") if isinstance(credits.get("crew"), list) else []
    cast = credits.get("cast") if isinstance(credits.get("cast"), list) else []
    directors = _names([c for c in crew if isinstance(c, dict) and c.get("job") == "Director"])
    if not directors and kind == "tv":
        directors = _names(row.get("created_by"))
    cast = sorted((c for c in cast if isinstance(c, dict)),
                  key=lambda c: c["order"] if isinstance(c.get("order"), int) else 9999)
    votes = row.get("vote_count") if isinstance(row.get("vote_count"), int) else 0
    countries = row.get("origin_country") if isinstance(row.get("origin_country"), list) else []
    if not countries and isinstance(row.get("production_countries"), list):
        countries = [c.get("iso_3166_1") for c in row["production_countries"] if isinstance(c, dict)]
    episodes = row.get("number_of_episodes")
    original = _text(row.get("original_title") if kind == "movie" else row.get("original_name"))
    genres = row.get("genres") if isinstance(row.get("genres"), list) else []
    return {"id": tid, "src": "tmdb", "title": title, "original": original if original != title else "",
            "year": _tmdb_year(row), "type": kind,
            "rating": _rating(row.get("vote_average")) if votes > 0 else None, "votes": max(0, votes),
            "genres": [_text(g.get("name"), 20) for g in genres
                       if isinstance(g, dict) and _text(g.get("name"), 20)][:GENRES_MAX],
            "directors": directors, "actors": _names(cast),
            "countries": [_COUNTRIES[c] for c in countries if c in _COUNTRIES][:4],
            "plot": _text(row.get("overview"), PLOT_MAX),
            "episodes": episodes if kind == "tv" and isinstance(episodes, int) and episodes > 0 else None,
            "aka": [original] if original and original != title else [],
            "img": _tmdb_path(row.get("poster_path"))}


def tmdb_fetch(url: str, headers: dict, cap: int, timeout: float = TMDB_TIMEOUT_S, proxy: str = "") -> tuple[int, bytes]:
    """GET → (状态, 至多 cap+1 字节)。填了代理（「设置 → YouTube」的 http:// 或 socks5://）就经它连
    （ytnode.open_tunnel），没填就和 urllib 一样看环境变量；本机的假服务器（http://）直连。跳转不跟。
    出错时的原因只写错误的种类，从不带地址（地址里可能有 api_key）。"""
    parts = urllib.parse.urlsplit(url)
    try:
        if parts.scheme == "http" or not proxy:
            handlers = [_NoRedirect()] + ([urllib.request.ProxyHandler({})] if parts.scheme == "http" else [])
            opener = urllib.request.build_opener(*handlers)
            try:
                with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as answer:
                    return getattr(answer, "status", 200), answer.read(cap + 1)
            except urllib.error.HTTPError as error:
                error.close()
                return error.code, b""
        from .ytnode import open_tunnel, parse_proxy
        host = parts.hostname or ""
        tunnel = open_tunnel(parse_proxy(proxy), host, 443, timeout)
        tunnel.settimeout(timeout)
        connection = http.client.HTTPSConnection(host, timeout=timeout)
        connection.sock = ssl.create_default_context().wrap_socket(tunnel, server_hostname=host)
        try:
            connection.request("GET", parts.path + ("?" + parts.query if parts.query else ""), headers=headers)
            answer = connection.getresponse()
            return answer.status, answer.read(cap + 1) if answer.status == 200 else b""
        finally:
            connection.close()
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise Failed(f"连不上 TMDB（{type(error).__name__}）") from None


class Tmdb:
    """TMDB：密钥存在 secrets/tmdb-key（600）；一次一个请求、请求之间有间隔、429 和连不上时歇。
    fetch(url, headers, cap, timeout, proxy) → (状态, 内容)。出错一律抛 Failed（不用豆瓣的 Blocked）。"""

    def __init__(self, secrets_dir: Path, fetch: Callable | None = None, clock=time.time, sleep=time.sleep,
                 log: Callable[[str], None] = print, proxy: Callable[[], str] = lambda: "", env=None):
        env = os.environ if env is None else env
        self.key_path = Path(secrets_dir) / TMDB_KEY_FILE
        self.fetch = fetch or tmdb_fetch
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.proxy = proxy
        # 换成本机的假服务器（测试、自测）：只认 http://127.0.0.1 / localhost，别的照旧连 TMDB。
        api, image = env.get("TV_TMDB_API") or "", env.get("TV_TMDB_IMAGE") or ""
        self.api = api.rstrip("/") if _LOCAL_TEST_BASE.match(api) else TMDB_API
        self.image_base = image.rstrip("/") if _LOCAL_TEST_BASE.match(image) else TMDB_IMAGE
        self._lock = threading.Lock()
        self._last = 0.0
        self.blocked_until = 0.0
        self._down = 0
        # 密钥不对（401）：换密钥以前不再用它发请求。
        self.bad_key = False
        self._seen: OrderedDict[str, dict] = OrderedDict()
        self._thumbs: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
        self.requests = 0

    # 密钥

    def _key(self) -> str:
        try:
            text = self.key_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
        return text if tmdb_key_ok(text) else ""

    def configured(self) -> bool:
        return bool(self._key())

    def on(self) -> bool:
        """填了密钥、TMDB 没拒过这个密钥、没在歇着。"""
        return not self.bad_key and self.clock() >= self.blocked_until and self.configured()

    def state(self) -> dict:
        """给管理员看的（从不带密钥本身）：set 填了没有，error 大白话。"""
        configured, error = self.configured(), ""
        if configured and self.bad_key:
            error = "TMDB 不认这个密钥，重新粘贴一个"
        elif configured and self.clock() < self.blocked_until:
            error = "TMDB 暂时连不上，过一会儿自动再试"
        return {"set": configured, "error": error}

    def set_key(self, text) -> dict:
        """存密钥（"" 是清掉，不用 TMDB）：secrets/ 是 700，文件 600，整份换掉写。"""
        text = "".join(str(text or "").split())
        if text and not tmdb_key_ok(text):
            return {"ok": False, "error": "这不像 TMDB 的密钥：粘贴「API 密钥」（32 位）或「API 读访问令牌」（eyJ 开头的一长串）"}
        with self._lock:
            try:
                if not text:
                    self.key_path.unlink(missing_ok=True)
                else:
                    self.key_path.parent.mkdir(parents=True, exist_ok=True)
                    os.chmod(self.key_path.parent, 0o700)
                    tmp = self.key_path.with_name(self.key_path.name + ".tmp")
                    tmp.unlink(missing_ok=True)
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as out:
                        out.write(text + "\n")
                    os.replace(tmp, self.key_path)
            except OSError:
                return {"ok": False, "error": "存不下密钥（数据目录写不了）"}
            self.bad_key = False
            self.blocked_until = 0.0
            self._down = 0
        self.log("片库的 TMDB 密钥" + ("设好了" if text else "清掉了"))
        return {"ok": True, "tmdb": self.state()}

    # 请求

    def _get(self, url: str, cap: int, image: bool = False) -> bytes:
        headers = {"User-Agent": UA, "Accept": "image/*" if image else "application/json"}
        if not image:
            # 图片地址不带密钥（image.tmdb.org 不要）。
            key = self._key()
            if not key:
                raise Failed("没有 TMDB 的密钥")
            if _TMDB_V3_KEY.match(key):
                url += ("&" if "?" in url else "?") + "api_key=" + key
            else:
                headers["Authorization"] = "Bearer " + key
        with self._lock:
            if self.bad_key:
                raise Failed("TMDB 不认这个密钥")
            if self.clock() < self.blocked_until:
                raise Failed("TMDB 暂时连不上")
            wait = self._last + TMDB_GAP_S - self.clock()
            if wait > 0:
                self.sleep(wait)
            try:
                self.requests += 1
                status, body = self.fetch(url, headers, cap, TMDB_TIMEOUT_S, self.proxy() or "")
                self._down = 0
            except Failed:
                self._down += 1
                if self._down >= TMDB_DOWN_AFTER:
                    self._down = 0
                    self.blocked_until = self.clock() + TMDB_DOWN_S
                    self.log(f"TMDB 连着几次连不上，歇 {int(TMDB_DOWN_S // 60)} 分钟（在国内可能要在「设置 → YouTube」里填代理）")
                raise
            finally:
                self._last = self.clock()
            if status == 401 and not image:
                self.bad_key = True
                self.log("TMDB 不认片库里填的密钥")
                raise Failed("TMDB 不认这个密钥")
            if status == 429:
                self.blocked_until = self.clock() + TMDB_BACKOFF_S
                self.log(f"TMDB 说请求太多，歇 {int(TMDB_BACKOFF_S // 60)} 分钟")
                raise Failed("TMDB 让歇一会儿")
        if status != 200:
            raise Failed(f"TMDB 回答 {status}")
        if len(body) > cap:
            raise Failed("TMDB 的回答太大")
        return body

    def search(self, query: str, kind: str, year: int | None = None, language: str = "zh-CN") -> list[dict]:
        """搜一种（tv / movie）；year 给了按首播 / 首映的年份筛。"""
        query = " ".join(str(query or "").split())[:QUERY_MAX]
        if not query or kind not in ("tv", "movie"):
            return []
        params = {"query": query, "language": language, "include_adult": "false", "page": "1"}
        if year:
            params["first_air_date_year" if kind == "tv" else "primary_release_year"] = str(year)
        found = parse_tmdb_search(self._get(f"{self.api}/search/{kind}?" + urllib.parse.urlencode(params),
                                            ANSWER_MAX), kind)
        with self._lock:
            for c in found:
                self._seen[c["id"]] = c
                self._seen.move_to_end(c["id"])
            while len(self._seen) > CANDIDATES_KEPT:
                self._seen.popitem(last=False)
        return found

    def seen(self, pick_id: str) -> dict | None:
        with self._lock:
            found = self._seen.get(pick_id)
            return dict(found) if found else None

    def details(self, kind: str, tid: str) -> dict:
        if kind not in ("tv", "movie") or not _TMDB_ID.match(str(tid)):
            raise Failed("TMDB 的编号不对")
        params = urllib.parse.urlencode({"language": "zh-CN", "append_to_response": "credits"})
        found = parse_tmdb_details(self._get(f"{self.api}/{kind}/{tid}?{params}", ANSWER_MAX), kind)
        if found is None or found["id"] != str(tid):
            raise Failed("TMDB 的资料看不懂")
        return found

    def image(self, path: str, size: str = TMDB_POSTER_SIZE, cap: int = POSTER_MAX) -> tuple[str, bytes]:
        """取一张 TMDB 的图：(jpg / png / webp, 内容)。不是图片、太大抛 Failed。"""
        if not _tmdb_path(path):
            raise Failed("不是 TMDB 的图片地址")
        data = self._get(f"{self.image_base}/{size}{path}", cap, image=True)
        kind = image_kind(data)
        if not kind:
            raise Failed("TMDB 给的不是图片")
        return kind, data

    def thumb(self, pick_id: str) -> tuple[str, bytes] | None:
        """搜索结果里一个候选的小图（只认最近搜到过的）；内存里留 THUMBS_KEPT 张。"""
        with self._lock:
            cached = self._thumbs.get(pick_id)
            candidate = self._seen.get(pick_id)
        if cached is not None:
            return cached
        if candidate is None or not candidate.get("img"):
            return None
        try:
            found = self.image(candidate["img"], TMDB_THUMB_SIZE, THUMB_MAX)
        except Failed:
            return None
        with self._lock:
            self._thumbs[pick_id] = found
            while len(self._thumbs) > THUMBS_KEPT:
                self._thumbs.popitem(last=False)
        return found


# ---------------------------------------------------------------- 存

class MetaStore:
    """library-meta.json：{"titles": {键: 资料}}。键是 s<剧名的哈希>（剧）或 m<条目 id>（电影、其他视频）。
    资料：state（matched / miss / none）、manual、q（自动查时用的片名，改了就重查）、at、豆瓣的 id、title、
    original、aka（又名，聚合搜索用，localsearch.py）、year、type、rating、votes、genres、directors、actors、countries、
    plot、episodes、img（豆瓣的海报地址；TMDB 的是 poster_path）、poster（library-art/ 里的文件名）、partial（详细
    资料没取全）、src（"tmdb" 是 TMDB 的条目，id 是 TMDB 的编号；没有就是豆瓣的）。没对上的（miss）还记 tmdb：
    TMDB 也查过了（答了没有）；tmdb_failed：TMDB 没查成（过 PARTIAL_RETRY_S 再试）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._titles = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        titles = data.get("titles") if isinstance(data, dict) else None
        return {k: v for k, v in titles.items() if isinstance(k, str) and isinstance(v, dict)} \
            if isinstance(titles, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"titles": self._titles}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, key: str) -> dict | None:
        with self._lock:
            found = self._titles.get(key)
            return dict(found) if found is not None else None

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._titles.items()}

    def put(self, key: str, entry: dict) -> None:
        with self._lock:
            self._titles[key] = entry
            if len(self._titles) > ENTRIES_MAX:
                # 太多了：先丢自动的、最旧的（手动选的留着）。
                order = sorted(self._titles, key=lambda k: (bool(self._titles[k].get("manual")), self._titles[k].get("at") or 0))
                for gone in order[:len(self._titles) - ENTRIES_MAX]:
                    del self._titles[gone]
            self._save()

    def drop(self, key: str) -> bool:
        with self._lock:
            if self._titles.pop(key, None) is None:
                return False
            self._save()
            return True


def entry_of(details: dict, query: str, manual: bool, now: float, partial: bool = False) -> dict:
    """豆瓣（或 TMDB，src = "tmdb"）的资料 → 存下来的一条（还没有海报文件）。"""
    keep = ("id", "src", "title", "original", "aka", "year", "type", "rating", "votes", "genres", "directors",
            "actors", "countries", "plot", "episodes", "img")
    entry = {k: details.get(k) for k in keep if details.get(k) not in (None, "", [])}
    entry.update(state="matched", manual=bool(manual), q=query, at=int(now), partial=bool(partial))
    return entry


def public(entry: dict | None) -> dict | None:
    """给页面的（没有豆瓣的图片地址、没有文件名）。"""
    if not entry:
        return None
    if entry.get("state") != "matched":
        return {"state": entry.get("state") or "miss", "manual": bool(entry.get("manual"))}
    row = {k: entry.get(k) for k in ("id", "title", "original", "year", "type", "rating", "votes", "genres",
                                     "directors", "actors", "countries", "plot", "episodes")
           if entry.get(k) not in (None, "", [])}
    row.update(state="matched", manual=bool(entry.get("manual")), partial=bool(entry.get("partial")),
               src="tmdb" if entry.get("src") == "tmdb" else "douban")
    return row
