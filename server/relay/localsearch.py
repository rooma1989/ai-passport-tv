"""聚合搜索（媒体中心设计的 M5）：电脑版「搜索」页最上面那几段，搜的是自己家里已经有的东西。

GET /api/search/local?q=（web.py，要登录）一次回答几段，每段里按像不像排好，段和段之间也按最像的那一条排：

- 直播台（live）：频道表里的台名，加上 M3U 带来的 tvg-id / tvg-name（channels.txt.meta.json）。台名先按
  节目单对台的办法规整（epg.key_of：cctv5、CCTV-5、CCTV5 体育 都是 CCTV5），一样就算原样相同；
  再比去掉空格标点、不分大小写以后的开头和中间（「卫视」找到所有卫视）。带上现在和下一个节目。
- 节目（epg）：节目单里现在在播和之后 24 小时内的节目（内存里那一天，Guide.programmes），按片名找；
  在播的排前面，再按开始时间。点了换到那个台。
- 片库（library）：电影、其他视频按条目，剧按剧（各集一起）；找片名、文件名猜的片名、英文名（alt），
  对上豆瓣的还有豆瓣的片名、原名、又名（library-meta.json 的 title / original / aka）。点了打开它。
- 剧集与番剧（series）、UP 主（up）：自己订阅的（账号的状态），按名字找。

谁能看到什么和原来一样：访客（扫设备码进来的）只有直播台和节目单（首页、直播页他们本来就看得到）；
片库只给登录的家里人，而且只在自建 / 电脑客户端（relay.library）；订阅是每个人自己的。
全在内存里比，不上网、不写盘，页面边打字边问（停手一下就问，见 console.js 的 aggSearch）。
拼音首字母要拼音表（新依赖），没有做。
"""

from __future__ import annotations

import re
import unicodedata

from . import epg, home
from .tenants import Caller, NotFound

# 关键词最长多少个字（和 B 站搜索一样）。
QUERY_MAX = 40
# 每段最多几条。
LIVE_MAX = 12
EPG_MAX = 20
LIBRARY_MAX = 12
SERIES_MAX = 10
UP_MAX = 10
# 节目单看多远：现在到之后多少秒（内存里本来就只留现在前 1 小时到后 24 小时）。
EPG_AHEAD_S = 24 * 3600
# 节目单一次最多看这么多个台、每台这么多条（一天的节目一般一个台三四十条）。
EPG_CHANNELS_MAX = 400
EPG_PER_CHANNEL = 80
# 段的默认顺序（同样像的时候）和标题。
SECTIONS = (("live", "直播台"), ("epg", "正在播和即将播出"), ("library", "片库"),
            ("series", "剧集与番剧"), ("up", "UP 主"))

# 像不像：越小越像。EXACT 原样相同（规整后），PREFIX 开头相同，INSIDE 名字里有，WORDS 几个词都有（不连着）。
EXACT, PREFIX, INSIDE, WORDS = 0, 1, 2, 3

_PUNCT = re.compile(r"[\s\-_·•・．.,，。、|:：;；/\\'\"!！?？()（）\[\]【】《》<>〈〉「」『』+＋~～#＃&＆*]+")


def norm(text) -> str:
    """比较用的样子：全角转半角、不分大小写、去掉空格和标点（「CCTV-5 体育」→「cctv5体育」）。"""
    text = unicodedata.normalize("NFKC", text if isinstance(text, str) else "").casefold()
    return _PUNCT.sub("", text)


def clean_query(q) -> str:
    """页面送来的关键词：多个空白并成一个，去掉头尾；太长的截掉。"""
    return " ".join(str(q or "").split())[:QUERY_MAX]


class Query:
    """一个关键词，算好比较要用的几种样子。"""

    def __init__(self, text: str):
        self.text = text
        self.flat = norm(text)
        self.words = [w for w in (norm(part) for part in text.split()) if w]
        key, _dropped = epg.key_of(text)
        # 台名的规整只在像台名的时候用（「CCTV5」「湖南卫视」）；去掉的太多（只剩一两个字）就不用。
        self.key = key if key and len(key) >= 2 else ""

    def __bool__(self) -> bool:
        return bool(self.flat)

    def score(self, names, channel: bool = False) -> int | None:
        """names 里最像的那个的分数；都不像是 None。channel：台名，多比一次 epg.key_of 规整过的。"""
        best = None
        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            flat = norm(name)
            if not flat:
                continue
            if flat == self.flat or (channel and self.key and epg.key_of(name)[0] == self.key):
                found = EXACT
            elif flat.startswith(self.flat):
                found = PREFIX
            elif self.flat in flat:
                found = INSIDE
            elif len(self.words) > 1 and all(word in flat for word in self.words):
                found = WORDS
            else:
                continue
            if best is None or found < best:
                best = found
            if best == EXACT:
                break
        return best


def _is_family(relay, caller: Caller) -> bool:
    """登录的家里人（不是访客）。单用户的中转（没有账号库）一直是。"""
    if not relay.tenant:
        return True
    return not caller.guest and caller.user is not None


# ---- 直播台、节目单 ----

def _channels(relay) -> list[tuple[str, list[str]]]:
    """[(台 id, [tvg-id, tvg-name, 台名])]：台名在最后（epg.table_rows）。"""
    if relay.main_channels is None:
        return []
    return epg.table_rows(relay.main_channels)


def _live(relay, query: Query, rows, guide_on: bool, now: float) -> list[tuple[int, int, dict]]:
    found = []
    for order, (cid, names) in enumerate(rows):
        score = query.score(names, channel=True)
        if score is not None:
            found.append((score, order, cid, names[-1]))
    found.sort(key=lambda row: row[:2])
    found = found[:LIVE_MAX]
    guide = getattr(relay, "guide", None)
    epg_now = guide.lookup([row[2] for row in found], now) if guide_on and found else {}
    logos = getattr(relay, "logos", None)
    known = logos.known() if logos is not None and found else set()
    checker = getattr(relay, "linecheck", None)
    covers = checker.covers.stamps() if checker is not None and checker.covers is not None and found else {}
    items = []
    for score, order, cid, name in found:
        row = epg_now.get(cid) or {}
        item = {"id": f"live:{cid}", "kind": "live", "title": name, "channel": {"id": cid, "name": name},
                "logo": f"api/art/logo/{cid}" if cid in known else "",
                "art": f"api/art/live/{cid}?v={covers[cid]}" if cid in covers else "",
                "now": row.get("now"), "next": row.get("next"),
                "play": {"kind": "live", "channel": cid}}
        items.append((score, order, item))
    return items


def _programmes(relay, query: Query, rows, now: float) -> list[tuple[int, int, dict]]:
    guide = getattr(relay, "guide", None)
    if guide is None or not guide.enabled or not rows:
        return []
    names = {cid: names[-1] for cid, names in rows}
    order = {cid: n for n, (cid, _names) in enumerate(rows)}
    start, end = int(now), int(now) + EPG_AHEAD_S
    found = []
    for cid, row in guide.programmes(list(names)[:EPG_CHANNELS_MAX], start, end, EPG_PER_CHANNEL, now).items():
        for p in row.get("programmes") or ():
            score = query.score([p["title"]])
            if score is None:
                continue
            on_air = p["start"] <= now < p["end"]
            found.append((score, not on_air, p["start"], order.get(cid, 0), cid, p))
    found.sort(key=lambda row: row[:4])
    logos = getattr(relay, "logos", None)
    known = logos.known() if logos is not None and found else set()
    items = []
    for n, (score, later, _start, _order, cid, p) in enumerate(found[:EPG_MAX]):
        items.append((score, n, {"id": f"epg:{cid}:{p['start']}", "kind": "programme", "title": p["title"],
                                 "start": p["start"], "end": p["end"], "on_air": not later,
                                 "channel": {"id": cid, "name": names.get(cid, cid)},
                                 "logo": f"api/art/logo/{cid}" if cid in known else "",
                                 "play": {"kind": "live", "channel": cid}}))
    return items


# ---- 片库 ----

def _library(relay, query: Query) -> list[tuple[int, int, dict]]:
    library = getattr(relay, "library", None)
    if library is None:
        return []
    entries = library.meta.all()
    groups: dict[str, dict] = {}
    for item in library.items():
        key = library.meta_key(item)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"first": item, "count": 0, "names": set(), "added": 0, "year": item.get("year")}
        group["count"] += 1
        group["added"] = max(group["added"], item.get("added") or 0)
        group["year"] = group["year"] or item.get("year")
        episode = item.get("kind") == "episode"
        group["names"].update(n for n in ((item.get("show") if episode else item.get("title")), item.get("alt"))
                              if isinstance(n, str) and n)
    found = []
    for key, group in groups.items():
        entry = entries.get(key) or {}
        names = list(group["names"])
        if entry.get("state") == "matched":
            names += [entry.get("title"), entry.get("original")] + list(entry.get("aka") or [])
        score = query.score(names)
        if score is None:
            continue
        item = group["first"]
        episode = item.get("kind") == "episode"
        shown = library.shown(item)
        year = group["year"] or (entry.get("year") if entry.get("state") == "matched" else None)
        if episode:
            row = {"id": f"libs:{key}", "kind": "show", "title": shown["name"], "year": year,
                   "count": group["count"], "art": shown["art"], "open": {"show": library.show_key(item.get("show") or "")}}
        else:
            row = {"id": f"lib:{item['id']}", "kind": item.get("kind") or "video", "title": shown["name"], "year": year,
                   "art": shown["art"], "open": {"item": item["id"]}}
        found.append((score, -group["added"], row))
    found.sort(key=lambda row: (row[0], row[1], row[2]["title"].casefold()))
    return [(score, n, row) for n, (score, _added, row) in enumerate(found[:LIBRARY_MAX])]


# ---- 自己订阅的剧集、UP 主 ----

def _subscriptions(relay, caller: Caller, query: Query) -> tuple[list, list]:
    try:
        account = relay._account(caller)
    except NotFound:
        return [], []
    with account.lock:
        rows = [(c.id, c.name, c.kind, c.site, c.source, len(c.videos),
                 account.episode(c.id) if c.kind == "series" else 0) for c in account.creators]
    series, ups = [], []
    for order, (cid, name, kind, site, source, total, episode) in enumerate(rows):
        score = query.score([name])
        if score is None:
            continue
        if kind == "series":
            series.append((score, order, {"id": f"sr:{cid}", "kind": "series", "title": name, "site": site,
                                          "episode": episode, "total": total,
                                          "art": home._cover(relay, site, source, "p"),
                                          "series": {"id": cid},
                                          "play": {"kind": "episode", "channel": cid, "episode": episode or 1}}))
        elif kind == "up":
            ups.append((score, order, {"id": f"up:{cid}", "kind": "up", "title": name, "site": site, "total": total,
                                       "art": home._face(relay, site, source),
                                       "play": {"kind": "channel", "channel": cid}}))
    series.sort(key=lambda row: row[:2])
    ups.sort(key=lambda row: row[:2])
    return series[:SERIES_MAX], ups[:UP_MAX]


# ---- 接口 ----

def search(relay, caller: Caller, q) -> dict:
    """GET /api/search/local?q=：{ok, q, sections: [{id, title, items}]}；没有结果的段不给，段按最像的那条排。"""
    text = clean_query(q)
    query = Query(text)
    if not query:
        return {"ok": True, "q": text, "sections": []}
    now = relay.clock()
    guide = getattr(relay, "guide", None)
    guide_on = bool(guide is not None and guide.enabled)
    rows = _channels(relay)
    found = {"live": _live(relay, query, rows, guide_on, now), "epg": _programmes(relay, query, rows, now)}
    if _is_family(relay, caller):
        if getattr(relay, "library", None) is not None:
            found["library"] = _library(relay, query)
        found["series"], found["up"] = _subscriptions(relay, caller, query)
    sections = []
    for rank, (sid, title) in enumerate(SECTIONS):
        items = found.get(sid) or []
        if items:
            sections.append((min(score for score, _order, _item in items), rank,
                             {"id": sid, "title": title, "items": [item for _score, _order, item in items]}))
    sections.sort(key=lambda row: row[:2])
    return {"ok": True, "q": text, "sections": [row[2] for row in sections]}
