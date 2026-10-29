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

只用标准库。
"""

from __future__ import annotations

import json
import os
import re
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


# ---------------------------------------------------------------- 存

class MetaStore:
    """library-meta.json：{"titles": {键: 资料}}。键是 s<剧名的哈希>（剧）或 m<条目 id>（电影、其他视频）。
    资料：state（matched / miss / none）、manual、q（自动查时用的片名，改了就重查）、at、豆瓣的 id、title、
    original、aka（又名，聚合搜索用，localsearch.py）、year、type、rating、votes、genres、directors、actors、countries、
    plot、episodes、img（豆瓣的海报地址）、poster（library-art/ 里的文件名）、partial（详细资料没取全）。"""

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
    """豆瓣的资料 → 存下来的一条（还没有海报文件）。"""
    keep = ("id", "title", "original", "aka", "year", "type", "rating", "votes", "genres", "directors", "actors",
            "countries", "plot", "episodes", "img")
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
    row.update(state="matched", manual=bool(entry.get("manual")), partial=bool(entry.get("partial")))
    return row
