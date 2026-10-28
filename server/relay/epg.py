"""节目单（XMLTV EPG）：「正在播：新闻联播 19:00–19:30」。

默认关着，云端和自建（电脑客户端）都一样：管理员在总后台「频道表」页的「节目单」里打开一个或几个来源
（presets.EPG_PRESETS 里网上公开的节目单，或者自己填的地址）以后才有。关着时这里什么都不做：没有线程、
不上网、不占内存（xml 解析器也是用到时才导入）。打开以后：

- 取：每 REFRESH_S（8 小时）问一次每个来源，带上次的 ETag / Last-Modified（没变就是 304，不重新下）；
  原样存在 <数据目录>/epg/（服务器给 gzip 就存 gzip），一份最多 DOWNLOAD_MAX，超时、太慢都算这次失败，
  RETRY_S 后再试，下好的旧文件照用。
- 读：一个后台线程（Linux 上调低优先级）用 iterparse 边读边扔，只留频道表里有的台、现在前 1 小时到
  后 24 小时的节目，每个台最多 PER_CHANNEL_MAX 条；整份换掉内存里的上一份。频道表改了（加了台）用已经
  下好的文件重新读，不上网；每 REPARSE_S 也重读一次，让留下的那一天跟着时间往后挪。
- 对台：先比 M3U 带来的 tvg-id / tvg-name（导入时存在 channels.txt.meta.json，见 channeltable.meta_of）
  和台名本身，原样相同最好；再比「规整过的名字」（key_of：CCTV-1 / CCTV1 / CCTV1综合 / CCTV-1 HD
  都是 CCTV1；湖南卫视HD、湖南卫视高清 都是湖南卫视）；最后「湖南卫视4K」这类没有自己节目单的 4K 台
  用不带 4K 的那个（CCTV4K、CCTV8K 是单独的台，不这样退）。几个来源都有的台用排在前面的来源。

给页面的：GET /api/epg?channels=ch000,ch001（web.py，和别的遥控接口一样要登录，访客也行）回答每个台的
now / next（title、start、end，Unix 秒）。加上 from / to（Unix 秒）就是电脑版「节目表」要的一段时间：每个台
再多一个 programmes（和这段时间有重叠的节目，按时间排）；时间段夹在内存里留的那一天里（现在前 1 小时到后
24 小时），一次最多 RANGE_CHANNELS_MAX 个台、每台最多 RANGE_PER_CHANNEL_MAX 条（max 可以再少）。
总后台：GET/POST /api/admin/epg（admin.py 转过来）。

推荐的节目单来源：导入或订阅的播放列表文件头写着 x-tvg-url（url-tvg）时，记下地址和是哪份列表推荐的
（SUGGEST_KEY，至多 SUGGEST_MAX 个，新的在前），总后台「节目单」里列出来，管理员点一下才加进来源；不会自己打开。
点了「不要」的记着（dismissed），同一个地址以后再被推荐也不再列。

标题：央视的节目单在标题后面带一串「-2026-248」（年份和期数）或日期，页面上没用，读的时候去掉（tidy_title）。

实测（2026-10，见 docs/selfhost/README.md「节目单」）：fanmingming 的 7 MB 文件读一遍约 1 秒 CPU，
解析时多用十来 MB 内存，留下的 128 个台一天的节目不到 1 MB。
"""

from __future__ import annotations

import calendar
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import channeltable, presets

# 账号库 meta 表里的设置：打开的来源，JSON 列表 ["https://…", …]；没有或空列表就是关着。
META_KEY = "epg.sources"
SOURCES_MAX = 3
# 推荐的节目单来源（播放列表文件头的 x-tvg-url）：JSON 列表 [{url, from, at, dismissed?}]。
SUGGEST_KEY = "epg.suggested"
SUGGEST_MAX = 6
# 不要了的至多记这么多个（只为了不再列出）。
DISMISSED_MAX = 30
URL_MAX = 2000
REFRESH_S = 8 * 3600
RETRY_S = 30 * 60
REPARSE_S = 8 * 3600
# 后台线程多久看一次频道表变没变。
TABLE_CHECK_S = 60
# 管理员点「现在更新」最多这么频繁（每次照样带 ETag 问，没变不下载）。
MANUAL_MIN_S = 5 * 60
DOWNLOAD_MAX = 50 * 1024 * 1024
# 解开 gzip 以后最多读这么多（防压缩炸弹）；真实的节目单解开是 3–7 MB。
XML_MAX = 256 * 1024 * 1024
FETCH_TIMEOUT_S = 30
FETCH_TOTAL_S = 300
WINDOW_BEFORE_S = 3600
WINDOW_AFTER_S = 24 * 3600
PER_CHANNEL_MAX = 200
TITLE_MAX = 60
# 节目单里认识的台名最多记这么多个（一份全国的节目单有一两千个台）。
CHANNELS_SEEN_MAX = 20000
# 时间没写时区的（「20261024190000」）按北京时间：国内的节目单都是这样写的。
DEFAULT_OFFSET_S = 8 * 3600
# /api/epg 一次最多问这么多个台。
QUERY_MAX = 200
# 带 from / to 的（节目表）：台少一点、每台的条数有上限，一次的回答最多几百 KB。
RANGE_CHANNELS_MAX = 60
RANGE_PER_CHANNEL_MAX = 64
RANGE_PER_CHANNEL_DEFAULT = 48
USER_AGENT = "xiaozhi-tv-epg/1"
_CHUNK = 64 * 1024


# ---- 时间 ----

_TIME = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?\s*"
                   r"(?:([+-])(\d{2}):?(\d{2})|(Z|UTC|GMT))?\s*$", re.IGNORECASE)


def parse_time(text) -> int | None:
    """XMLTV 的时间「20261024190000 +0800」→ Unix 秒；看不懂的 None。没写时区按北京时间。"""
    match = _TIME.match(text or "") if isinstance(text, str) else None
    if not match:
        return None
    year, month, day, hour, minute = (int(match.group(n)) for n in range(1, 6))
    second = int(match.group(6) or 0)
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 60):
        return None
    if match.group(7):
        offset = (int(match.group(8)) * 3600 + int(match.group(9)) * 60) * (1 if match.group(7) == "+" else -1)
        if abs(offset) > 14 * 3600:
            return None
    elif match.group(10):
        offset = 0
    else:
        offset = DEFAULT_OFFSET_S
    try:
        stamp = calendar.timegm((year, month, day, hour, minute, min(second, 59), 0, 0, 0))
        time.gmtime(stamp)
    except (OverflowError, ValueError):
        return None
    # timegm 不查日期：2 月 31 日会滚到 3 月，这里挡掉。
    if time.gmtime(stamp)[:3] != (year, month, day):
        return None
    return stamp - offset


_YEAR_ISSUE = re.compile(r"[-_ ]?(?:19|20)\d\d(?:-\d{1,2}-\d{1,2}|-\d{1,4})$")


def tidy_title(title: str) -> str:
    """「第一动画乐园-2026-248」→「第一动画乐园」（央视节目单的年份和期数，或者「-2026-10-26」这样的日期）；
    别的原样。去掉后空了也原样。"""
    shorter = _YEAR_ISSUE.sub("", title).rstrip(" -_")
    return shorter or title


# ---- 对台 ----

_BRACKETS = re.compile(r"[(\[（【《][^)\]）】》]*[)\]）】》]")
_PUNCT = re.compile(r"[\s\-_·•・．.、|:：/\\'\"]+")
_TAIL = re.compile(r"(?:超高清|高清|超清|标清|蓝光|频道|HEVC|H26[45]|FHD|UHD|HDR|HD|SD|\d{3,4}P|\d{2,3}FPS|IPV[46])$")
_CCTV = re.compile(r"^CCTV(\d{1,2})(\+|PLUS)?(.*)$")
_REGIONS = ("欧洲", "美洲", "亚洲")
_UHD = re.compile(r"^(.+?)([48]K)$")


def key_of(name) -> tuple[str, bool]:
    """(规整过的台名, 是不是去掉了说明文字)。

    全角转半角、大写、去掉括号里的说明和空格横线，去掉结尾的「高清」「HD」「1080P」之类；
    央视只留台号（CCTV-1 综合 → CCTV1，CCTV-5+ 体育赛事 → CCTV5+），CCTV4 的欧洲、美洲版和
    CCTV4K、CCTV8K 各算各的。第二项给「同一个键有几个节目单台」时挑原样的那个用。"""
    text = unicodedata.normalize("NFKC", name if isinstance(name, str) else "").upper()
    bare = _BRACKETS.sub("", text)
    dropped = bare != text
    text = _PUNCT.sub("", bare)
    while True:
        shorter = _TAIL.sub("", text)
        if shorter == text or not shorter:
            break
        text = shorter
    match = _CCTV.match(text)
    if not match:
        return text, dropped
    number, plus, rest = str(int(match.group(1))), match.group(2), match.group(3)
    if not plus and number in ("4", "8") and rest.startswith("K"):
        return f"CCTV{number}K", dropped or len(rest) > 1
    base = f"CCTV{number}" + ("+" if plus else "")
    for region in _REGIONS:
        if region in rest:
            return base + region, dropped
    if rest.endswith(("4K", "8K")):
        return base + rest[-2:], True
    return base, dropped or bool(rest)


def uhd_base(key: str) -> str:
    """「湖南卫视4K」→「湖南卫视」（4K 台没有自己的节目单时用）；CCTV4K、CCTV8K 和不带 4K 的为空。"""
    match = _UHD.match(key)
    if not match or re.fullmatch(r"CCTV[48]K", key):
        return ""
    return match.group(1)


class Matcher:
    """频道表的台 → 节目单里的台。rows：[(台 id, [tvg-id, tvg-name, 台名…])]。

    match(节目单台的名字们) → {台 id: 分数}，分数越小越像：0 原样相同，2/3 规整后相同（3 是节目单那边
    去掉了说明文字），4/5 是 4K 台退到不带 4K 的。"""

    def __init__(self, rows):
        self.raw: dict[str, list[str]] = {}
        self.keys: dict[str, list[str]] = {}
        self.uhd: dict[str, list[str]] = {}
        for cid, names in rows:
            for name in names:
                if not isinstance(name, str) or not name.strip():
                    continue
                _add(self.raw, name.strip().casefold(), cid)
                key, _dropped = key_of(name)
                if key:
                    _add(self.keys, key, cid)
                    base = uhd_base(key)
                    if base:
                        _add(self.uhd, base, cid)

    def match(self, names) -> dict[str, int]:
        found: dict[str, int] = {}

        def better(cids, score):
            for cid in cids:
                if score < found.get(cid, 99):
                    found[cid] = score

        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            better(self.raw.get(name.strip().casefold(), ()), 0)
            key, dropped = key_of(name)
            if key:
                better(self.keys.get(key, ()), 2 + dropped)
                better(self.uhd.get(key, ()), 4 + dropped)
        return found


def _add(index: dict, key: str, cid: str) -> None:
    bucket = index.setdefault(key, [])
    if cid not in bucket:
        bucket.append(cid)


def table_rows(table: Path | None) -> list[tuple[str, list[str]]]:
    """频道表（channeltable.read）加上旁边的 tvg 属性 → Matcher 要的 rows。"""
    if table is None:
        return []
    meta = channeltable.meta_of(table)
    rows = []
    for row in channeltable.read(table)["rows"]:
        extra = meta.get(row["id"], {})
        rows.append((row["id"], [extra.get("tvg_id", ""), extra.get("tvg_name", ""), row["name"]]))
    return rows


# ---- 读 XMLTV ----

class _Capped:
    """读到 limit 字节就停（ValueError）：gzip 解开以后可能大得离谱。"""

    def __init__(self, stream, limit: int):
        self.stream, self.limit, self.used = stream, limit, 0

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size if size and size > 0 else _CHUNK)
        self.used += len(data)
        if self.used > self.limit:
            raise ValueError("节目单解开以后太大")
        return data


def open_xml(path: Path):
    """打开下好的文件；gzip（看开头两个字节，不看扩展名）就边读边解。"""
    handle = open(path, "rb")
    try:
        head = handle.read(2)
        handle.seek(0)
    except OSError:
        handle.close()
        raise
    return handle, (gzip.GzipFile(fileobj=handle, mode="rb") if head == b"\x1f\x8b" else handle)


def parse(stream, matcher: Matcher, start_s: int, end_s: int, limit: int = XML_MAX) -> dict:
    """边读边扔地读一份 XMLTV。回答 {"programmes": {台 id: [(开始, 结束, 标题)]}, "channels": 节目单里的台数,
    "seen": 读到的节目数, "error": 半路出错的原因（前面读到的照样用）}。

    只留 matcher 认得的台、和 [start_s, end_s) 有重叠的节目。没写结束时间的用下一个节目的开始时间。
    xml.etree 不解析外部实体；实体展开的放大攻击由 expat（2.4 起）自己挡，解开的总字节数另有 limit。"""
    from xml.etree import ElementTree

    names: dict[str, list[str]] = {}
    resolved: dict[str, dict[str, int]] = {}
    kept: dict[str, list[list]] = {}
    order: dict[str, int] = {}
    seen = 0
    error = ""
    depth = 0
    root = None
    try:
        for event, elem in ElementTree.iterparse(_Capped(stream, limit), events=("start", "end")):
            if event == "start":
                depth += 1
                if root is None:
                    root = elem
                continue
            depth -= 1
            if depth != 1:
                continue
            if elem.tag == "channel":
                epg_id = (elem.get("id") or "").strip()
                if epg_id and len(names) < CHANNELS_SEEN_MAX:
                    shown = [(e.text or "").strip() for e in elem.findall("display-name")][:6]
                    names[epg_id] = [epg_id] + [n for n in shown if n]
                    resolved.pop(epg_id, None)
            elif elem.tag == "programme":
                seen += 1
                _programme(elem, names, resolved, kept, order, matcher, start_s, end_s)
            root.clear()
    except (ElementTree.ParseError, ValueError, OSError, EOFError) as problem:
        error = f"{type(problem).__name__}: {problem}"[:200]
    channels = {epg_id: _finish(items, start_s, end_s) for epg_id, items in kept.items()}
    best: dict[str, tuple] = {}
    for epg_id, items in channels.items():
        if not items:
            continue
        for cid, score in resolved.get(epg_id, {}).items():
            rank = (score, order[epg_id])
            if cid not in best or rank < best[cid][0]:
                best[cid] = (rank, epg_id)
    return {"programmes": {cid: channels[epg_id] for cid, (_rank, epg_id) in best.items()},
            "channels": len(names), "seen": seen, "error": error}


def _programme(elem, names, resolved, kept, order, matcher, start_s, end_s) -> None:
    epg_id = (elem.get("channel") or "").strip()
    if not epg_id:
        return
    found = resolved.get(epg_id)
    if found is None:
        if len(resolved) >= CHANNELS_SEEN_MAX:
            return
        found = resolved[epg_id] = matcher.match(names.get(epg_id, [epg_id]))
    if not found:
        return
    start = parse_time(elem.get("start"))
    stop = parse_time(elem.get("stop")) if elem.get("stop") else None
    if start is None or start >= end_s or (stop is not None and (stop <= start_s or stop <= start)):
        return
    title = ""
    for node in elem.findall("title"):
        title = " ".join((node.text or "").split())
        if title:
            break
    if not title:
        return
    items = kept.setdefault(epg_id, [])
    order.setdefault(epg_id, len(order))
    if stop is None and start < start_s - 12 * 3600:
        return          # 很早以前的、又没写结束时间：用不上
    if len(items) < PER_CHANNEL_MAX * 2:
        items.append([start, stop, tidy_title(title)[:TITLE_MAX]])


def _finish(items: list[list], start_s: int, end_s: int) -> list[tuple]:
    """按开始时间排，补上没写的结束时间，去掉同一时刻重复的，再按时间窗口筛一遍。"""
    items.sort(key=lambda item: item[0])
    out = []
    for index, (start, stop, title) in enumerate(items):
        if out and out[-1][0] == start:
            continue
        if stop is None:
            later = next((s for s, _e, _t in items[index + 1:] if s > start), None)
            if later is None:
                continue
            stop = later
        if stop <= start_s or start >= end_s:
            continue
        out.append((start, stop, title))
    return out[:PER_CHANNEL_MAX]


# ---- 取 ----

class _HttpOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """跳转也只跟到 http(s)。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener(request, timeout):
    return urllib.request.build_opener(_HttpOnlyRedirect).open(request, timeout=timeout)


def cache_name(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def valid_url(url) -> bool:
    if not isinstance(url, str) or not url or len(url) > URL_MAX or any(c in url for c in " \"'<>\\") \
            or any(ord(c) < 33 for c in url):
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def fetch(url: str, folder: Path, opener=None, clock=time.time, monotonic=time.monotonic) -> dict:
    """取一个来源到 folder/<名字>.xml（原样，可能是 gzip），记下 ETag / Last-Modified 下次带上。
    回答这个来源的状态 {ok, changed, error, bytes, fetched_at, checked_at, …}（也存在 <名字>.json）。"""
    folder = Path(folder)
    name = cache_name(url)
    data_path, info_path = folder / f"{name}.xml", folder / f"{name}.json"
    info = _read_info(info_path, url)
    if not valid_url(url):
        info.update(ok=False, error="地址要以 http:// 或 https:// 开头")
        return info
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if data_path.is_file():
        if info.get("etag"):
            headers["If-None-Match"] = info["etag"]
        if info.get("last_modified"):
            headers["If-Modified-Since"] = info["last_modified"]
    now = clock()
    info["checked_at"] = int(now)
    tmp = data_path.with_name(data_path.name + ".part")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers=headers)
        deadline = monotonic() + FETCH_TOTAL_S
        size = 0
        with (opener or _default_opener)(request, timeout=FETCH_TIMEOUT_S) as response, open(tmp, "wb") as out:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > DOWNLOAD_MAX:
                    raise _Refused(f"节目单太大（超过 {DOWNLOAD_MAX // (1024 * 1024)} MB）")
                if monotonic() > deadline:
                    raise _Refused(f"太慢（{FETCH_TOTAL_S} 秒没下完）")
                out.write(chunk)
            got = getattr(response, "headers", None)
            etag = got.get("ETag", "") if got is not None else ""
            modified = got.get("Last-Modified", "") if got is not None else ""
        if size == 0:
            raise _Refused("下载到的是空的")
        os.replace(tmp, data_path)
        info.update(ok=True, changed=True, error="", bytes=size, fetched_at=int(now),
                    etag=etag[:200], last_modified=modified[:100])
    except urllib.error.HTTPError as problem:
        if problem.code == 304 and data_path.is_file():
            info.update(ok=True, changed=False, error="")
        else:
            info.update(ok=False, changed=False, error=f"HTTP {problem.code}")
    except _Refused as problem:
        info.update(ok=False, changed=False, error=str(problem))
    except urllib.error.URLError as problem:
        info.update(ok=False, changed=False, error=f"连不上：{problem.reason}"[:200])
    except (OSError, ValueError) as problem:
        info.update(ok=False, changed=False, error=f"{type(problem).__name__}: {problem}"[:200])
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        _write_json(info_path, {k: v for k, v in info.items() if k != "changed"})
    except OSError:
        pass
    return info


class _Refused(Exception):
    pass


def _read_info(path: Path, url: str) -> dict:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    if not isinstance(info, dict) or info.get("url") != url:
        info = {}
    info["url"] = url
    return info


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---- 后台 ----

class Guide:
    """一个中转一份：设置、后台线程、内存里的节目。sources 为空时没有线程、没有数据。"""

    def __init__(self, folder: Path, table: Path | None, log=print, clock=time.time, monotonic=time.monotonic,
                 opener=None):
        self.folder = Path(folder)
        self.table = Path(table) if table is not None else None
        self.log, self.clock, self.monotonic, self.opener = log, clock, monotonic, opener
        self.sources: list[str] = []
        self._data: dict[str, list[tuple]] = {}
        self._status: dict[str, dict] = {}
        self._coverage = {"matched": 0, "total": 0, "missing": []}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_fetch = 0.0
        self._parsed_at = 0.0
        self._signature = None
        self._manual_at = -MANUAL_MIN_S
        # 打开时起后台线程；测试关掉它，自己调 tick()。
        self.autostart = True

    @property
    def enabled(self) -> bool:
        return bool(self.sources)

    def configure(self, sources: list[str], start: bool | None = None) -> None:
        start = self.autostart if start is None else start
        with self._lock:
            changed = sources != self.sources
            self.sources = list(sources)
            if changed:
                self._next_fetch = 0.0
                self._signature = None
            if not self.sources:
                self._data, self._status = {}, {}
                self._coverage = {"matched": 0, "total": 0, "missing": []}
            elif start and self._thread is None:
                self._thread = threading.Thread(target=self._run, name="epg", daemon=True)
                self._thread.start()
        self._wake.set()

    def refresh_now(self) -> bool:
        """管理员点了「现在更新」：MANUAL_MIN_S 内只算一次。"""
        now = self.monotonic()
        if now - self._manual_at < MANUAL_MIN_S:
            return False
        self._manual_at = now
        self._next_fetch = 0.0
        self._wake.set()
        return True

    def _run(self) -> None:
        if sys.platform.startswith("linux"):
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            except (OSError, AttributeError):
                pass
        while True:
            with self._lock:
                if not self.sources:
                    self._thread = None
                    return
            try:
                self.tick()
            except Exception as problem:  # noqa: BLE001 —— 后台线程不能死，下一轮再试
                self.log(f"节目单：{type(problem).__name__}: {problem}")
            self._wake.wait(TABLE_CHECK_S)
            self._wake.clear()

    def _table_signature(self):
        parts = []
        for path in (self.table, channeltable.meta_path(self.table) if self.table else None):
            try:
                stat = path.stat() if path is not None else None
                parts.append((stat.st_mtime_ns, stat.st_size) if stat else None)
            except OSError:
                parts.append(None)
        return tuple(parts) + tuple(self.sources)

    def tick(self) -> None:
        """一轮：到时间了就取（带 ETag 问），然后该重读就重读。后台线程每分钟一次；测试直接调。"""
        sources = list(self.sources)
        if not sources:
            return
        now = self.clock()
        changed = False
        if now >= self._next_fetch:
            results = {url: fetch(url, self.folder, self.opener, self.clock, self.monotonic) for url in sources}
            with self._lock:
                for url, info in results.items():
                    self._status[url] = dict(self._status.get(url, {}), **info)
            changed = any(info.get("changed") for info in results.values())
            failed = [url for url, info in results.items() if not info.get("ok")]
            for url in failed:
                self.log(f"节目单：{url} 没取到（{results[url].get('error')}），{RETRY_S // 60} 分钟后再试")
            self._next_fetch = now + (RETRY_S if failed else REFRESH_S)
        signature = self._table_signature()
        if changed or signature != self._signature or now - self._parsed_at >= REPARSE_S:
            self._signature = signature
            self.reparse(now)

    def reparse(self, now: float | None = None) -> None:
        """用下好的文件重读一遍（不上网），整份换掉内存里的节目。"""
        now = self.clock() if now is None else now
        rows = table_rows(self.table)
        matcher = Matcher(rows)
        start_s, end_s = int(now) - WINDOW_BEFORE_S, int(now) + WINDOW_AFTER_S
        merged: dict[str, list[tuple]] = {}
        notes: dict[str, dict] = {}
        for url in list(self.sources):
            path = self.folder / f"{cache_name(url)}.xml"
            if not path.is_file():
                continue
            began = time.process_time()
            try:
                handle, stream = open_xml(path)
            except OSError as problem:
                notes[url] = {"parse_error": f"{type(problem).__name__}"}
                continue
            try:
                result = parse(stream, matcher, start_s, end_s)
            finally:
                handle.close()
            for cid, items in result["programmes"].items():
                merged.setdefault(cid, items)
            notes[url] = {"channels": result["channels"], "programmes": result["seen"],
                          "matched": len(result["programmes"]), "parse_error": result["error"],
                          "parse_cpu_s": round(time.process_time() - began, 2), "parsed_at": int(now)}
            if result["error"]:
                self.log(f"节目单：{url} 读到一半出错（{result['error']}），前面的照用")
        names = {cid: names[-1] for cid, names in rows}
        with self._lock:
            if not self.sources:
                return
            self._data = merged
            for url, note in notes.items():
                self._status[url] = dict(self._status.get(url, {"url": url}), **note)
            self._coverage = {"matched": sum(cid in merged for cid in names), "total": len(names),
                              "missing": [names[cid] for cid in names if cid not in merged][:40]}
        self._parsed_at = now

    def lookup(self, cids, now: float | None = None) -> dict[str, dict]:
        """{台 id: {"now": {title, start, end} 或 None, "next": …}}；没有节目单的台不在里面。"""
        now = self.clock() if now is None else now
        with self._lock:
            data = self._data
        out = {}
        for cid in cids:
            items = data.get(cid)
            if not items:
                continue
            current = next((item for item in items if item[0] <= now < item[1]), None)
            after = current[1] if current else now
            upcoming = next((item for item in items if item[0] >= after), None)
            if current or upcoming:
                out[cid] = {"now": _shown(current), "next": _shown(upcoming)}
        return out

    def programmes(self, cids, start_s: int, end_s: int, limit: int, now: float | None = None) -> dict[str, dict]:
        """节目表：{台 id: {"now", "next", "programmes": [和 [start_s, end_s) 有重叠的节目，最多 limit 条]}}；
        没有节目单的台不在里面。"""
        now = self.clock() if now is None else now
        with self._lock:
            data = self._data
        out = self.lookup(cids, now)
        for cid in list(out):
            shown = [_shown(item) for item in data.get(cid, ()) if item[1] > start_s and item[0] < end_s]
            out[cid]["programmes"] = shown[:limit]
        return out

    def status(self) -> dict:
        with self._lock:
            return {"sources": [dict(self._status.get(url, {"url": url})) for url in self.sources],
                    "coverage": dict(self._coverage), "channels": len(self._data)}


def _shown(item) -> dict | None:
    return {"title": item[2], "start": item[0], "end": item[1]} if item else None


# ---- 设置（账号库的 meta 表） ----

def load_sources(accounts) -> list[str]:
    try:
        data = json.loads(accounts.meta(META_KEY, "[]") or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [url for url in data if valid_url(url)][:SOURCES_MAX]


def load_suggested(accounts) -> list[dict]:
    try:
        data = json.loads(accounts.meta(SUGGEST_KEY, "[]") or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and valid_url(item.get("url"))]


def suggest(relay, urls, origin: str) -> int:
    """播放列表推荐的节目单地址记下来（只记，不打开）：已经记着的挪到前面、更新是谁推荐的；不要了的不再记。
    返回新记了几个。没有账号库（单用户）时什么都不做。"""
    accounts = getattr(relay, "accounts", None)
    urls = [url for url in dict.fromkeys(urls or []) if valid_url(url)]
    if accounts is None or not urls:
        return 0
    origin = " ".join(str(origin or "").split())[:40]
    current = load_suggested(accounts)
    dismissed = [item for item in current if item.get("dismissed")]
    kept = [item for item in current if not item.get("dismissed")]
    skip = {item["url"] for item in dismissed}
    known = {item["url"] for item in kept}
    fresh = [{"url": url, "from": origin, "at": int(relay.clock())} for url in urls if url not in skip]
    added = sum(1 for item in fresh if item["url"] not in known)
    again = {item["url"] for item in fresh}
    kept = (fresh + [item for item in kept if item["url"] not in again])[:SUGGEST_MAX]
    accounts.set_meta(SUGGEST_KEY, json.dumps(kept + dismissed[-DISMISSED_MAX:], ensure_ascii=False))
    return added


def attach(relay, folder: Path) -> Guide:
    """中转启动时（多用户）：建一个 Guide，按库里的设置打开或不打开。"""
    guide = Guide(folder, relay.main_channels, log=lambda message: relay.log(message), clock=relay.clock)
    relay.guide = guide
    guide.configure(load_sources(relay.accounts))
    return guide


# ---- 接口 ----

_CID = re.compile(r"^[!-~]{1,15}$")


def _int(text) -> int | None:
    if not isinstance(text, str) or not re.fullmatch(r"-?\d{1,12}", text.strip()):
        return None
    return int(text)


def api(relay, query) -> dict:
    """GET /api/epg?channels=ch000,ch001：每个台现在和下一个节目。没开节目单时 enabled 为 False。

    再加 from=&to=（Unix 秒，max= 每台最多几条）：节目表，每个台多一个 programmes。时间段夹在内存里留的那一天
    （window：现在前 WINDOW_BEFORE_S 到后 WINDOW_AFTER_S）里，回答里的 range 是夹过的；看不懂的数是 ok: False。
    query：查询参数的字典；旧的调用方式（只给 channels 字符串）也认。"""
    if not isinstance(query, dict):
        query = {"channels": query}
    guide = getattr(relay, "guide", None)
    if guide is None or not guide.enabled:
        return {"ok": True, "enabled": False, "channels": {}}
    ranged = "from" in query or "to" in query
    cap = RANGE_CHANNELS_MAX if ranged else QUERY_MAX
    channels = query.get("channels")
    if channels:
        wanted = list(dict.fromkeys(cid for cid in channels.split(",")[:QUERY_MAX] if _CID.match(cid)))[:cap]
    else:
        wanted = [cid for cid, _name in table_rows(relay.main_channels)][:cap]
    now = relay.clock()
    answer = {"ok": True, "enabled": True, "now": int(now),
              "window": {"before": WINDOW_BEFORE_S, "after": WINDOW_AFTER_S}}
    if not ranged:
        answer["channels"] = guide.lookup(wanted, now)
        return answer
    start, end = _int(query.get("from", str(int(now)))), _int(query.get("to", str(int(now) + 3600)))
    limit = _int(query.get("max", str(RANGE_PER_CHANNEL_DEFAULT)))
    if start is None or end is None or limit is None:
        return {"ok": False, "error": "from / to / max 要是整数（Unix 秒）"}
    start = max(start, int(now) - WINDOW_BEFORE_S)
    end = min(end, int(now) + WINDOW_AFTER_S)
    end = max(end, start)
    limit = max(1, min(limit, RANGE_PER_CHANNEL_MAX))
    answer.update(range={"from": start, "to": end}, max=limit, channels=guide.programmes(wanted, start, end, limit, now))
    return answer


def admin_view(relay) -> dict:
    """总后台「频道表」页的「节目单」一块：打开了哪些来源、各自怎么样、频道表里几个台对上了。"""
    guide = getattr(relay, "guide", None)
    sources = guide.sources if guide is not None else []
    known = {p["url"]: p for p in presets.EPG_PRESETS}
    view = {"ok": True, "available": guide is not None, "sources": sources, "presets": presets.epg_public(),
            "max": SOURCES_MAX, "refresh_hours": REFRESH_S // 3600,
            # 播放列表推荐的、还没加上也没说不要的（suggest）；和预设的节目单是同一个地址的也不列。
            "suggested": [{k: item.get(k) for k in ("url", "from", "at")}
                          for item in (load_suggested(relay.accounts) if getattr(relay, "accounts", None) else [])
                          if not item.get("dismissed") and item["url"] not in sources and item["url"] not in known]}
    if guide is not None:
        status = guide.status()
        for item in status["sources"]:
            preset = known.get(item.get("url"))
            item["name"] = preset["name"] if preset else ""
            for key in ("etag", "last_modified"):
                item.pop(key, None)
        view.update(status=status["sources"], coverage=status["coverage"])
    return view


def admin_save(relay, body: dict, caller, ip: str) -> dict:
    """POST /api/admin/epg {sources: [地址…]}（空列表是关掉）。"""
    guide = getattr(relay, "guide", None)
    if guide is None:
        return {"ok": False, "error": "这个中转没有节目单功能（单用户模式）"}
    sources = body.get("sources")
    if not isinstance(sources, list) or not all(isinstance(url, str) for url in sources):
        return {"ok": False, "error": "请求格式不对"}
    sources = list(dict.fromkeys(url.strip() for url in sources if url.strip()))
    if len(sources) > SOURCES_MAX:
        return {"ok": False, "error": f"最多 {SOURCES_MAX} 个节目单来源"}
    bad = [url for url in sources if not valid_url(url)]
    if bad:
        return {"ok": False, "error": f"地址不对：{bad[0][:80]}（要以 http:// 或 https:// 开头）"}
    relay.accounts.set_meta(META_KEY, json.dumps(sources, ensure_ascii=False))
    guide.configure(sources)
    relay.accounts.audit("epg.save", caller.user, ip, "epg", "", f"{len(sources)} 个来源" if sources else "关闭")
    relay.log(f"总后台：节目单{'打开，' + str(len(sources)) + ' 个来源' if sources else '关闭'}")
    return {"ok": True, "sources": sources}


def admin_dismiss(relay, body: dict, caller, ip: str) -> dict:
    """POST /api/admin/epg/dismiss {url}：推荐的节目单来源里不要的一个，不再列出。"""
    url = body.get("url")
    current = load_suggested(relay.accounts)
    found = next((item for item in current if item["url"] == url and not item.get("dismissed")), None)
    if found is None:
        return {"ok": False, "error": "没有这个推荐的来源（页面可能是旧的，请刷新）"}
    kept = [item for item in current if not item.get("dismissed") and item is not found]
    dismissed = [item for item in current if item.get("dismissed")] + [dict(found, dismissed=True)]
    relay.accounts.set_meta(SUGGEST_KEY, json.dumps(kept + dismissed[-DISMISSED_MAX:], ensure_ascii=False))
    relay.accounts.audit("epg.dismiss", caller.user, ip, "epg", "", found.get("from") or "")
    return {"ok": True}


def admin_refresh(relay, caller, ip: str) -> dict:
    guide = getattr(relay, "guide", None)
    if guide is None or not guide.enabled:
        return {"ok": False, "error": "节目单没有打开"}
    if not guide.refresh_now():
        return {"ok": False, "error": f"刚更新过，{MANUAL_MIN_S // 60} 分钟内只能点一次"}
    relay.accounts.audit("epg.refresh", caller.user, ip, "epg")
    return {"ok": True}
