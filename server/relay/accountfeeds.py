"""电脑版首页上「自己账号」的几排，和 B 站观看记录的回写（家庭服务器和电脑客户端；云端没有这些路径，一律 404）。

设计：docs/superpowers/specs/2026-10-24-media-center-ui-design.md 第 18 节。每个人只看得到自己的：B 站按他自己扫码登录的
账号（bililogin.py）；YouTube 的 cookies 是这台服务器的（ytsettings.py，管理员的小号），所以 YouTube 的几排只给管理员。

    GET  /api/feeds                    有哪几排、每排能不能用（没登录时页面画一张「登录后这里会显示…」的小卡片）、设置
    GET  /api/feeds/row?id=<排>[&fresh=1]
                                       一排：{state: ready|loading|login|blocked|error|hidden, items, loading, message}
    GET  /api/feeds/history[?max=&view_at=&business=][&fresh=1]
                                       「片库 → 历史」：B 站观看记录的一页（每项是首页的节目，另带 hist：剧名、第几集），
                                       more 是下一页的游标（history/cursor 的 cursor），没有了为 null
    POST /api/feeds/settings           {"writeback": bool, "autotv": bool}：「同步到 B 站历史」「新关注自动放到小电视」

几排（ROWS）和它们用的接口（都带这个人的 cookies；推荐和动态再带 wbi 签名，bilibili.wbi_sign）：

    B 站推荐        /x/web-interface/wbi/index/top/feed/rcmd        网页版首页的推荐
    B 站观看记录    /x/web-interface/history/cursor                  progress 是看到的秒数（-1 = 看完了）
    YouTube 推荐    节点的 /yt/feed?kind=rec（yt-dlp 的 :ytrec）
    YouTube 观看记录 节点的 /yt/feed?kind=history（:ythis）
    关注的更新      B 站 /x/polymer/web-dynamic/v1/feed/all?type=video（关注的人的视频动态）
                    + YouTube /yt/feed?kind=subs（:ytsubs），按时间排在一起

每一项都是首页的「节目」（home.py）：play 是 {"kind": "feed", "site", "id", "start"?, "part"?, "ep"?}，POST /api/play 交给
play_feed（小电视：投屏位，从 start 起；这台电脑：按链接在电脑上看，从 start 起）。观看记录里的番剧、影视剧
id 是整季（ss<季 id>），ep 是看的那一集（history.epid）：在电脑上看按那一集的链接（/bangumi/play/ep…）打开，
选集停在那一集，start 是那一集看到的地方。

别打扰网站：按（用户, 来源）缓存 TTL_S 秒，同一时间只取一次，在后台线程里取（页面先拿到 loading，再问）；
「刷新」至少隔 FRESH_GAP_S 秒；出错后 ERROR_GAP_S 秒内（被网络拦了是 BLOCKED_GAP_S）不再问。

回写（HistoryReport）：默认开（没动过这个开关的人都算开；自己关了就一直关，见 Store.load 的 chosen），小电视和这台
电脑上看 B 站视频的进度报给他自己的 B 站账号：投稿视频用 /x/v2/history/report（aid、cid、progress、csrf）；番剧、
影视剧的一集（ep…）用播放心跳 /x/click-interface/web/heartbeat（aid、cid、epid、sid、type=4、sub_type、
played_time、csrf；aid、cid、季 id、类型按 ep_id 问 /pgc/view/web/season）。同一个视频最多每 REPORT_GAP_S 秒一次，
停下、换台、看完时各一次。报成功了，这个人的观看记录缓存就算过期，下次问时重新取。YouTube 不回写：没有可靠的接口。

搜索标注：B 站搜索（bilisearch.py）结果里的正版剧集、番剧，在这个人最近的观看记录（bili_seasons：最多
SEASON_SCAN 条，缓存 TTL_S 秒）里出现过的，带上 watched（看到第几集、看到哪里），见 watched_seasons。

cookies 不进日志、不进回答。关注列表、设置存在数据目录的 feeds/<用户 id>.json（0600），诊断包不带。
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from pathlib import Path

from . import home
from .bilibili import API, BLOCKED_HINT, WBI_KEYS_S, _query, blocked, cover_url, parse_duration, wbi_keys, wbi_sign

PREFIX = "/api/feeds"
# 排的 id → (标题, 用到的来源)。页面按 ROW_ORDER 的顺序画。
ROWS = {
    "updates": ("关注的更新", ("bili_dyn", "yt_subs")),
    "bili_history": ("B 站观看记录", ("bili_history",)),
    "bili_rec": ("B 站推荐", ("bili_rec",)),
    "yt_history": ("YouTube 观看记录", ("yt_history",)),
    "yt_rec": ("YouTube 推荐", ("yt_rec",)),
}
ROW_ORDER = ("updates", "bili_history", "bili_rec", "yt_history", "yt_rec")
# 每个来源缓存多久（秒）。YouTube 每次要节点跑一次 yt-dlp（十几秒、一次只能跑一个），观看记录也留 5 分钟。
TTL_S = {"bili_rec": 600, "bili_dyn": 600, "bili_history": 120, "bili_seasons": 120,
         "yt_rec": 600, "yt_subs": 600, "yt_history": 300}
FRESH_GAP_S = 30
ERROR_GAP_S = 60
BLOCKED_GAP_S = 300
ROW_MAX = 30
YT_FEED_N = 30
BILI_REC_N = 20
BILI_HISTORY_N = 30
# 搜索标注用的观看记录：最多看最近几条（一页 BILI_HISTORY_N 条，至多 4 页）；搜索最多等它几秒。
SEASON_SCAN = 100
SEASON_WAIT_S = 1.5
# 观看记录：看了不到这么多秒的从头放；离结尾不到这么多秒的算看完了。
RESUME_MIN_S = 30
RESUME_END_S = 30
# 回写：同一个视频两次至少隔几秒（停下、换台、看完时不受这个限制）；不到几秒的不报。
REPORT_GAP_S = 60
REPORT_MIN_S = 5
REPORT_QUEUE = 200
VIEW_KEEP = 200

_BVID = re.compile(r"^BV[0-9A-Za-z]{10}$")
_BV_PART = re.compile(r"^(BV[0-9A-Za-z]{10})/(\d{1,15})$")
_SEASON = re.compile(r"^ss\d{1,12}$")
# 番剧、影视剧的一集：ep<集 id>/<cid>（剧集台、投到电脑的选集），或者只有 ep<集 id>。
_EP_PART = re.compile(r"^ep(\d{1,12})(?:/(\d{1,15}))?$")
# 「第3集」「第 12 话」「第5期」里的数字；观看记录的游标（history/cursor 的 cursor）。
_EP_N = re.compile(r"第\s*([0-9]{1,5}(?:\.[0-9])?)\s*[集话話期]")
_CURSOR_N = re.compile(r"^[0-9]{1,20}$")
_BUSINESS = re.compile(r"^[a-z-]{1,20}$")
_YTID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_MID = re.compile(r"^[0-9]{1,20}$")
_UCID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")

LOGIN_BILI = "登录 B 站后，这里会显示你的推荐、观看记录和关注的 UP 主的新视频"
LOGIN_YT = "导入 YouTube cookies（或从浏览器读取登录）后，这里会显示你的 YouTube 推荐和观看记录"
INVALID = "B 站登录已失效，请到「设置 → B 站账号」重新扫码"
UNREACHABLE = "B 站连不上：检查路由器或代理"
REFUSED = "B 站拒绝了这次请求（可能太频繁），过几分钟再试"
YT_LOGIN = "YouTube 说没有登录：cookies 可能过期了，在「设置 → YouTube」重新导入或从浏览器读取"
YT_DOWN = "连不上 YouTube：检查「设置 → YouTube」的代理"
YT_OLD = "YouTube 节点太旧，不认识推荐和观看记录：更新节点（ytnode.py）后再试"
YT_SLOW = "YouTube 这次太慢了，过一会儿再试"


class FeedError(Exception):
    """取一个来源失败：state 是 login / blocked / error，message 给页面看。"""

    def __init__(self, state: str, message: str, gap: float = ERROR_GAP_S):
        super().__init__(message)
        self.state = state
        self.message = message
        self.gap = gap


# ---------------------------------------------------------------- 每个人的设置和关注（feeds/<用户 id>.json）

DEFAULT_SETTINGS = {"writeback": True, "autotv": False}
# 这些设置只认这个人自己动过的（chosen 里有它）：以前存下来的值可能只是当时的默认值（回写以前默认关，改「自动放到
# 小电视」时会顺手把 writeback: false 一起存下），没动过的照现在的默认。
CHOSEN_ONLY = ("writeback",)


class Store:
    """feeds/<用户 id>.json：{"settings": {...}, "follows": {...}}。目录 0700、文件 0600。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.RLock()

    def path(self, user: int) -> Path:
        return self.root / f"{int(user)}.json"

    def load(self, user: int) -> dict:
        with self.lock:
            try:
                data = json.loads(self.path(user).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict) or data.get("user") != int(user):
                data = {}
            settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
            chosen = data.get("chosen") if isinstance(data.get("chosen"), list) else []
            data["chosen"] = sorted(k for k in set(chosen) if k in DEFAULT_SETTINGS)
            data["settings"] = {k: bool(settings.get(k, v)) if k not in CHOSEN_ONLY or k in data["chosen"] else v
                                for k, v in DEFAULT_SETTINGS.items()}
            if not isinstance(data.get("follows"), dict):
                data["follows"] = {}
            data["user"] = int(user)
            return data

    def save(self, user: int, data: dict) -> None:
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.root, 0o700)
            except OSError:
                pass
            target = self.path(user)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(dict(data, user=int(user)), handle, ensure_ascii=False)
                os.chmod(tmp, 0o600)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def update(self, user: int, change) -> dict:
        """读、改（change(data)）、存，在锁里做完。"""
        with self.lock:
            data = self.load(user)
            change(data)
            self.save(user, data)
            return data

    def settings(self, user: int) -> dict:
        return dict(self.load(user)["settings"])

    def delete(self, user: int) -> None:
        with self.lock:
            try:
                self.path(user).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- 网站回答的整理（纯函数，测试直接喂样子）

def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _clean(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def entry(site: str, vid: str, title: str, **extra) -> dict:
    """一项的内部样子：site、id、title、author、author_id、face、cover、duration、date、reason、position、done、
    part、ep（番剧看的那一集）、tv（能不能投到小电视）。"""
    found = {"site": site, "id": vid, "title": title, "author": "", "author_id": "", "face": "", "cover": "",
             "duration": 0, "date": 0, "reason": "", "position": 0, "done": False, "part": 0, "ep": 0, "tv": True}
    found.update(extra)
    return found


def bili_rcmd(data: dict) -> list[dict]:
    """推荐接口的 data.item → 视频（goto 是 av 的；广告、直播、番剧推广不要）。"""
    found, seen = [], set()
    for it in (data or {}).get("item") or []:
        if not isinstance(it, dict) or it.get("goto") not in ("av", None):
            continue
        bvid = str(it.get("bvid") or "")
        title = _clean(it.get("title"), 120)
        if not _BVID.match(bvid) or bvid in seen or not title:
            continue
        seen.add(bvid)
        owner = it.get("owner") if isinstance(it.get("owner"), dict) else {}
        reason = it.get("rcmd_reason") if isinstance(it.get("rcmd_reason"), dict) else {}
        mid = str(owner.get("mid") or "")
        found.append(entry("bilibili", bvid, title, author=_clean(owner.get("name"), 40),
                           author_id=mid if _MID.match(mid) else "", face=cover_url(owner.get("face")),
                           cover=cover_url(it.get("pic")), duration=_int(it.get("duration")),
                           date=_int(it.get("pubdate")), reason=_clean(reason.get("content"), 20)))
    return found


def bili_history(data: dict) -> list[dict]:
    """观看记录（history/cursor 的 data.list）→ 视频和番剧。progress：看到的秒数，-1 是看完了。
    番剧（business = pgc）用整季（ss<kid>），只能在电脑上看（小电视要加成剧集）；看的那一集（history.epid）
    记在 ep 里，progress、duration 都是那一集的。直播、专栏不要。"""
    found, seen = [], set()
    for it in (data or {}).get("list") or []:
        if not isinstance(it, dict):
            continue
        hist = it.get("history") if isinstance(it.get("history"), dict) else {}
        business = hist.get("business")
        duration = _int(it.get("duration"))
        progress = it.get("progress")
        done = progress == -1 or (isinstance(progress, int) and duration and progress >= duration - RESUME_END_S)
        position = progress if isinstance(progress, int) and not isinstance(progress, bool) and progress > 0 else 0
        common = dict(author=_clean(it.get("author_name"), 40), face=cover_url(it.get("author_face")),
                      cover=cover_url(it.get("cover") or ((it.get("covers") or [None])[0])),
                      duration=duration, date=_int(it.get("view_at")), position=0 if done else position,
                      done=bool(done))
        mid = str(it.get("author_mid") or "")
        common["author_id"] = mid if _MID.match(mid) else ""
        if business == "archive":
            bvid = str(hist.get("bvid") or "")
            title = _clean(it.get("title"), 120)
            if not _BVID.match(bvid) or not title or bvid in seen:
                continue
            seen.add(bvid)
            page = _int(hist.get("page"))
            part = page if page > 1 and _int(it.get("videos")) > 1 else 0
            head, label = title, ""
            if part:
                label = _clean(f"P{part} {hist.get('part') or ''}", 80)
                title = _clean(f"{title} {label}", 120)
            found.append(entry("bilibili", bvid, title, part=part, head=head, label=label, **common))
        elif business == "pgc":
            kid = _int(it.get("kid"))
            head = _clean(it.get("title"), 80)
            label, number = episode_label(it)
            title = _clean(f"{head} {label}", 120)
            vid = f"ss{kid}"
            if not kid or not head or vid in seen:
                continue
            seen.add(vid)
            found.append(entry("bilibili", vid, title, tv=False, ep=_int(hist.get("epid")), head=head,
                               label=label, n=number, badge=_clean(it.get("badge"), 8),
                               **dict(common, author=common["author"] or _clean(it.get("badge"), 8) or "番剧")))
    return found


def episode_label(it: dict) -> tuple[str, str]:
    """番剧、影视剧那一条的（「第3集 标题」, 「3」）：show_title 是「第3集」「第3话」（电影是「正片」），
    long_title 是这一集的标题；show_title 没有时按 history.part（集号）拼。"""
    hist = it.get("history") if isinstance(it.get("history"), dict) else {}
    show = _clean(it.get("show_title"), 40)
    part = _clean(hist.get("part"), 20)
    if not show and part:
        show = f"第{part}集" if part.isdigit() else part
    long = _clean(it.get("long_title"), 60)
    label = show if not long or long in show else _clean(f"{show} {long}", 100)
    found = _EP_N.search(show)
    number = found.group(1) if found else (part if part.isdigit() else show)
    return label, number[:20]


def history_cursor(data: dict) -> dict | None:
    """观看记录下一页的游标 {"max", "view_at", "business"}；这一页是空的、游标不全时 None（没有了）。"""
    cursor = (data or {}).get("cursor")
    if not (data or {}).get("list") or not isinstance(cursor, dict):
        return None
    top, at, business = cursor.get("max"), cursor.get("view_at"), cursor.get("business")
    if not isinstance(top, int) or isinstance(top, bool) or top < 0 or not _int(at):
        return None
    if not isinstance(business, str) or not _BUSINESS.match(business):
        business = ""
    return {"max": top, "view_at": at, "business": business}


def watched_seasons(entries: list[dict]) -> dict:
    """观看记录（bili_history 的项）→ {"ss<季 id>": {"ep", "n", "progress", "duration"}}：每季最近看的那一集；
    progress 是那一集看到的秒数，-1 是看完了。搜索结果据此写「看到第 3 集 12:30」。"""
    found = {}
    for e in entries or []:
        if e.get("site") != "bilibili" or not _SEASON.match(str(e.get("id") or "")) or e["id"] in found:
            continue
        found[e["id"]] = {"ep": int(e.get("ep") or 0), "n": str(e.get("n") or ""),
                          "progress": -1 if e.get("done") else int(e.get("position") or 0),
                          "duration": int(e.get("duration") or 0)}
    return found


def annotate_search(answer: dict, seasons: dict) -> dict:
    """B 站搜索的回答（大家共用的缓存）→ 这个人的副本：正版剧集、番剧（pgc 和影视、番剧分类的 items）里看过的
    那几季带上 watched。原来的回答不动。"""
    if not seasons or not isinstance(answer, dict) or not answer.get("ok"):
        return answer
    out = dict(answer)
    for key in ("pgc", "items"):
        rows = answer.get(key)
        if not isinstance(rows, list) or not any(isinstance(r, dict) and r.get("kind") == "pgc" for r in rows):
            continue
        out[key] = [dict(r, watched=dict(seasons[r["id"]]))
                    if isinstance(r, dict) and r.get("kind") == "pgc" and r.get("id") in seasons else r
                    for r in rows]
    return out


def bili_dynamic(data: dict) -> list[dict]:
    """关注的人的视频动态（polymer feed/all?type=video 的 data.items）→ 投稿的视频。"""
    found, seen = [], set()
    for it in (data or {}).get("items") or []:
        if not isinstance(it, dict):
            continue
        modules = it.get("modules") if isinstance(it.get("modules"), dict) else {}
        author = modules.get("module_author") if isinstance(modules.get("module_author"), dict) else {}
        dynamic = modules.get("module_dynamic") if isinstance(modules.get("module_dynamic"), dict) else {}
        major = dynamic.get("major") if isinstance(dynamic.get("major"), dict) else {}
        archive = major.get("archive") if isinstance(major.get("archive"), dict) else None
        if archive is None:
            continue
        bvid = str(archive.get("bvid") or "")
        title = _clean(archive.get("title"), 120)
        if not _BVID.match(bvid) or bvid in seen or not title:
            continue
        seen.add(bvid)
        mid = str(author.get("mid") or "")
        found.append(entry("bilibili", bvid, title, author=_clean(author.get("name"), 40),
                           author_id=mid if _MID.match(mid) else "", face=cover_url(author.get("face")),
                           cover=cover_url(archive.get("cover")),
                           duration=parse_duration(archive.get("duration_text")), date=_int(author.get("pub_ts"))))
    return found


def youtube_entries(videos: list[dict]) -> list[dict]:
    """YouTube 客户端 feed() 的结果 → 内部的样子。"""
    found = []
    for v in videos or []:
        if not isinstance(v, dict) or not _YTID.match(str(v.get("id") or "")):
            continue
        found.append(entry("youtube", v["id"], _clean(v.get("title"), 120), author=_clean(v.get("channel"), 40),
                           author_id=v.get("channel_id") or "", duration=_int(v.get("duration")),
                           date=_int(v.get("date"))))
    return found


def media(row: str, e: dict) -> dict:
    """内部的一项 → 首页的节目（home.py 的样子）。"""
    site, vid = e["site"], e["id"]
    art = home.youtube_cover(vid) if site == "youtube" else home.thumb(e.get("cover") or "")
    item = {"id": f"{row}:{site}:{vid}", "kind": "video", "title": e["title"], "subtitle": e.get("author") or "",
            "source": site, "duration": e.get("duration") or 0, "pubdate": e.get("date") or 0,
            "art": {"url": art, "ratio": "wide"},
            "creator": {"name": e.get("author") or "", "face": home.thumb(e.get("face") or "", "f")},
            "play": {"kind": "feed", "site": site, "id": vid, "title": e["title"]}}
    if e.get("reason"):
        item["reason"] = e["reason"]
    if e.get("part"):
        item["play"]["part"] = e["part"]
    if e.get("ep"):
        item["play"]["ep"] = e["ep"]
    if not e.get("tv", True):
        item["pc_only"] = True
    if row.endswith("history"):
        item["watched_at"] = e.get("date") or 0
        duration = e.get("duration") or 0
        position = duration if e.get("done") else e.get("position") or 0
        item["progress"] = {"position": int(position), "duration": int(duration),
                            "device": "B 站" if site == "bilibili" else "YouTube", "updated_at": e.get("date") or 0,
                            "done": bool(e.get("done"))}
        if position >= RESUME_MIN_S and not e.get("done") and (not duration or position < duration - RESUME_END_S):
            item["play"]["start"] = int(position)
        if e.get("head"):
            # 「片库 → 历史」分两行写：剧名（视频标题）和「第3集 标题」（多 P 的「P3 标题」）。
            item["hist"] = {"type": "pgc" if _SEASON.match(vid) else "video", "head": e["head"],
                            "ep": e.get("label") or "", "badge": e.get("badge") or ""}
    return item


# ---------------------------------------------------------------- 播放（home.play 转过来）

def play_url(item: dict) -> str | None:
    """{"kind": "feed", "site", "id", "part"?, "ep"?} → 在电脑上看用的链接（只按 id 拼，不信页面给的地址）。
    番剧带着 ep 时是那一集的链接：web.py 按它找到整季、选中那一集。"""
    site, vid = item.get("site"), item.get("id")
    if not isinstance(vid, str):
        return None
    if site == "bilibili" and _BVID.match(vid):
        part = item.get("part")
        tail = f"?p={part}" if isinstance(part, int) and not isinstance(part, bool) and 1 < part < 10000 else ""
        return f"https://www.bilibili.com/video/{vid}{tail}"
    if site == "bilibili" and _SEASON.match(vid):
        ep = item.get("ep")
        if isinstance(ep, int) and not isinstance(ep, bool) and 0 < ep < 10 ** 12:
            return f"https://www.bilibili.com/bangumi/play/ep{ep}"
        return f"https://www.bilibili.com/bangumi/play/{vid}"
    if site == "youtube" and _YTID.match(vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def start_of(item: dict) -> float | None:
    start = item.get("start")
    if isinstance(start, (int, float)) and not isinstance(start, bool) and 0 < start < 100 * 3600:
        return float(start)
    return None


def play_tv(relay, caller, dev: dict, item: dict) -> dict:
    """播到小电视：放进它的投屏位（和投屏一样，记进投屏历史），观看记录从看到的地方起。"""
    from .bilibili import SourceError
    url = play_url(item)
    if url is None:
        return {"ok": False, "reason": "bad", "error": "请求格式不对"}
    site, vid = item["site"], item["id"]
    if site == "bilibili" and vid.startswith("ss"):
        return {"ok": False, "reason": "failed",
                "error": "番剧一整季在小电视上要加成剧集：在电脑上看，或者到「剧集」里加上它"}
    client = relay.clients.get(site)
    if client is None:
        return {"ok": False, "reason": "youtube_off", "error": relay._youtube_off("YouTube 还没配置好")}
    try:
        video = client.video(vid)
    except SourceError as error:
        relay.log(f"播放 {site} {vid} 到小电视失败：{type(error).__name__}")
        if error.permanent:
            return {"ok": False, "reason": "failed", "error": "这个视频放不了（可能已删除、私享或需要会员）"}
        return {"ok": False, "reason": "failed", "error": ("B 站" if site == "bilibili" else "YouTube") + "暂时取不到，请稍后再试"}
    start = start_of(item) if not item.get("part") else None
    return relay._cast_video(video, caller, dev, position=start or 0.0)


# ---------------------------------------------------------------- 几排

class Feeds:
    """一个中转一个：每个人的几排（缓存、后台取）、设置、观看记录的回写，和关注的同步（follows.py）。"""

    def __init__(self, relay, root: Path, background: bool = True, clock=time.time, monotonic=time.monotonic):
        from .follows import Follows
        self.relay = relay
        self.store = Store(root)
        self.background = background
        self.clock = clock
        self.monotonic = monotonic
        self.lock = threading.RLock()
        # (用户, 来源) → (取到的时刻, [内部的项])；出错的 → (时刻, state, message, gap)；正在取的。
        self._cache: dict[tuple, tuple] = {}
        self._errors: dict[tuple, tuple] = {}
        self._busy: set[tuple] = set()
        # YouTube 节点一次只跑一个 yt-dlp：几排排着队取。
        self._yt_lock = threading.Lock()
        self._wbi: tuple[float, str, str] | None = None
        # 回写：(用户, BV 号) → (时刻, 位置)；视频的 aid、cid（番剧的一集 ep… → aid、cid、季 id、类型）。
        self._reported: dict[tuple, tuple] = {}
        self._views: dict[str, tuple] = {}
        # 观看记录第一页之后的游标（用户 → cursor），「片库 → 历史」的「加载更多」从这里接着取。
        self._history_more: dict[int, dict | None] = {}
        self._queue: queue.Queue | None = None
        # 测试替身可以换掉：YouTube cookies 在不在（本机模式看 ytaccount 的 gate；节点模式先当作在）。
        self.yt_cookies = self._yt_cookies
        self.follows = Follows(relay, self, background=background, clock=clock, monotonic=monotonic)

    # ---- 能不能用 ----

    def _bili(self, user: int) -> tuple[str, str]:
        """("ok", "") / ("login", 说明)。"""
        accounts = self.relay.bili_accounts
        record = accounts.record(user) if accounts is not None else None
        if record is None:
            return "login", LOGIN_BILI
        if record.get("invalid"):
            return "login", INVALID
        return "ok", ""

    def _yt_cookies(self) -> bool:
        if getattr(self.relay, "youtube_kind", "") != "local":
            return True
        from . import ytaccount
        try:
            return not ytaccount.subscriptions(self.relay).gate()
        except (OSError, ValueError, TypeError):
            return False

    def _youtube(self, caller) -> tuple[str, str]:
        """("ok", "") / ("login", 说明) / ("hidden", "")：不是管理员、YouTube 没开时不显示。"""
        if caller.role != "admin" or self.relay.clients.get("youtube") is None:
            return "hidden", ""
        if not self.yt_cookies():
            return "login", LOGIN_YT
        return "ok", ""

    def _avail(self, caller, source: str) -> tuple[str, str]:
        return self._bili(caller.user) if source.startswith("bili") else self._youtube(caller)

    def overview(self, caller) -> dict:
        rows = []
        for rid in ROW_ORDER:
            title, sources = ROWS[rid]
            states = [self._avail(caller, s) for s in sources]
            ok = [s for s in states if s[0] == "ok"]
            if ok:
                state, message = "ready", ""
            else:
                shown = [s for s in states if s[0] != "hidden"]
                state, message = (shown[0] if shown else ("hidden", ""))
            need = "" if state != "login" else ("bili" if sources[0].startswith("bili") else "youtube")
            rows.append({"id": rid, "title": title, "state": state, "need": need, "message": message})
        bili = self._bili(caller.user)
        yt = self._youtube(caller)
        return {"ok": True, "rows": rows, "settings": self.store.settings(caller.user),
                "bili": {"login": bili[0] == "ok", "message": bili[1]},
                "youtube": {"shown": yt[0] != "hidden", "ok": yt[0] == "ok", "message": yt[1]}}

    # ---- 一排 ----

    def row(self, caller, rid: str, fresh: bool = False) -> dict:
        if rid not in ROWS:
            return {"ok": False, "error": "没有这一排"}
        title, sources = ROWS[rid]
        parts = [(s, self._source(caller, s, fresh)) for s in sources]
        live = [(s, p) for s, p in parts if p["state"] != "hidden"]
        answer = {"ok": True, "id": rid, "title": title, "items": [], "loading": False, "message": ""}
        if not live:
            return dict(answer, state="hidden")
        usable = [(s, p) for s, p in live if p["state"] != "login"]
        if not usable:
            return dict(answer, state="login", message=live[0][1]["message"],
                        need="bili" if live[0][0].startswith("bili") else "youtube")
        items = []
        for _, p in usable:
            items.extend(p["items"])
        if rid == "updates":
            items.sort(key=lambda e: -(e.get("date") or 0))
        answer["items"] = [media(rid, e) for e in items[:ROW_MAX]]
        answer["loading"] = any(p["state"] == "loading" for _, p in usable)
        errors = [p for _, p in usable if p["state"] in ("blocked", "error")]
        ages = [p["age"] for _, p in usable if p.get("age") is not None]
        if ages:
            answer["age"] = int(max(ages))
        if errors:
            answer["message"] = errors[0]["message"]
        if answer["items"]:
            state = "ready"
        elif answer["loading"]:
            state = "loading"
        elif errors:
            state = errors[0]["state"]
        else:
            state = "ready"
        return dict(answer, state=state)

    def _source(self, caller, source: str, fresh: bool, sync: bool = False) -> dict:
        """一个来源的缓存（过期了就去取：后台线程里取，sync 时就在这个线程里取，等它取完）。"""
        avail, message = self._avail(caller, source)
        if avail != "ok":
            return {"state": avail, "message": message, "items": []}
        key = (caller.user, source)
        now = self.monotonic()
        start = False
        with self.lock:
            cached = self._cache.get(key)
            age = now - cached[0] if cached is not None else None
            stale = cached is None or age >= TTL_S[source] or (fresh and age >= FRESH_GAP_S)
            error = self._errors.get(key)
            backing_off = error is not None and now - error[0] < error[3]
            if key not in self._busy and stale and not backing_off:
                self._busy.add(key)
                start = True
        if start:
            if self.background and not sync:
                threading.Thread(target=self._run, args=(caller.user, caller.role == "admin", source),
                                 daemon=True, name=f"feed-{source}").start()
            else:
                self._run(caller.user, caller.role == "admin", source)
        with self.lock:
            cached = self._cache.get(key)
            error = self._errors.get(key)
            loading = key in self._busy
        items = list(cached[1]) if cached is not None else []
        answer = {"state": "loading" if loading else "ready", "items": items, "message": "",
                  "age": (self.monotonic() - cached[0]) if cached is not None else None}
        if error is not None and not loading:
            answer.update(state=error[1], message=error[2])
            if error[1] == "login":
                answer["state"] = "login"
        return answer

    def _run(self, user: int, admin: bool, source: str) -> None:
        key = (user, source)
        try:
            items = FETCHERS[source](self, user)
        except FeedError as error:
            with self.lock:
                self._errors[key] = (self.monotonic(), error.state, error.message, error.gap)
            return
        except Exception as error:  # noqa: BLE001 —— 后台线程：任何意外都变成页面上的一句话
            self.relay.log(f"取用户 {user} 的 {source} 出错：{type(error).__name__}")
            with self.lock:
                self._errors[key] = (self.monotonic(), "error", "取不到，看日志", ERROR_GAP_S)
            return
        finally:
            with self.lock:
                self._busy.discard(key)
        with self.lock:
            self._cache[key] = (self.monotonic(), items)
            self._errors.pop(key, None)
        if source in ("bili_dyn", "yt_subs"):
            self.follows.note_latest(user, items)

    # ---- B 站 ----

    def _bili_get(self, user: int, path: str, params: dict, signed: bool = False) -> dict:
        accounts = self.relay.bili_accounts
        login = accounts.login_for(user) if accounts is not None else None
        if login is None:
            raise FeedError("login", LOGIN_BILI)
        cookie = login.header()
        try:
            if signed:
                img, sub = self._wbi_keys(cookie)
                params = wbi_sign(params, img, sub, self.clock())
            answer, _ = accounts.fetch(f"{API}{path}?{_query(params)}", cookie)
        except (OSError, ValueError) as error:
            self._wbi = None if signed else self._wbi
            self.relay.log(f"用户 {user} 取 B 站 {path} 失败：{type(error).__name__}")
            if blocked(error):
                raise FeedError("blocked", BLOCKED_HINT, BLOCKED_GAP_S) from None
            raise FeedError("error", UNREACHABLE) from None
        if not isinstance(answer, dict):
            raise FeedError("error", REFUSED)
        if answer.get("code") == -101:
            accounts.mark_invalid(user)
            raise FeedError("login", INVALID)
        if answer.get("code") != 0:
            if signed:
                self._wbi = None
            self.relay.log(f"用户 {user} 取 B 站 {path}：code={answer.get('code')}")
            raise FeedError("error", REFUSED)
        data = answer.get("data")
        return data if isinstance(data, dict) else {}

    def _wbi_keys(self, cookie: str) -> tuple[str, str]:
        cached = self._wbi
        if cached is not None and self.monotonic() - cached[0] < WBI_KEYS_S:
            return cached[1], cached[2]
        answer, _ = self.relay.bili_accounts.fetch(f"{API}/x/web-interface/nav", cookie)
        keys = wbi_keys(answer.get("data") if isinstance(answer, dict) else None)
        if keys is None:
            raise ValueError("导航接口没给 wbi key")
        self._wbi = (self.monotonic(), keys[0], keys[1])
        return keys

    def _fetch_bili_rec(self, user: int) -> list[dict]:
        data = self._bili_get(user, "/x/web-interface/wbi/index/top/feed/rcmd",
                              {"web_location": 1430650, "y_num": 4, "fresh_type": 4, "feed_version": "V8",
                               "fresh_idx_1h": 1, "fetch_row": 1, "fresh_idx": 1, "brush": 1, "homepage_ver": 1,
                               "ps": BILI_REC_N}, signed=True)
        return bili_rcmd(data)

    def _fetch_bili_history(self, user: int) -> list[dict]:
        data = self._bili_get(user, "/x/web-interface/history/cursor", {"ps": BILI_HISTORY_N, "type": "all"})
        with self.lock:
            self._history_more[user] = history_cursor(data)
        return bili_history(data)

    def _fetch_bili_seasons(self, user: int) -> list[dict]:
        """搜索标注用：最近 SEASON_SCAN 条观看记录（按游标往下翻，至多 4 页）。"""
        found, seen, params = [], 0, {"ps": BILI_HISTORY_N, "type": "all"}
        for _ in range(-(-SEASON_SCAN // BILI_HISTORY_N)):
            data = self._bili_get(user, "/x/web-interface/history/cursor", params)
            found.extend(bili_history(data))
            seen += len(data.get("list") or [])
            more = history_cursor(data)
            if more is None or seen >= SEASON_SCAN:
                break
            params = {"ps": BILI_HISTORY_N, "type": "all", **{k: v for k, v in more.items() if v != ""}}
        return found

    # ---- 「片库 → 历史」、搜索标注 ----

    def history_page(self, caller, query: dict) -> dict:
        """GET /api/feeds/history：没有游标是第一页（和首页那一排同一份缓存，fresh 时至少隔 FRESH_GAP_S 秒
        重新取，在这个线程里取完再回答）；带着游标（max、view_at、business）是往下的一页，每次都去问 B 站。"""
        answer = {"ok": True, "items": [], "more": None, "message": "",
                  "writeback": self.store.settings(caller.user)["writeback"]}
        avail, message = self._bili(caller.user)
        if avail != "ok":
            return dict(answer, state="login", message=message)
        top, at = str(query.get("max") or ""), str(query.get("view_at") or "")
        if top or at:
            business = str(query.get("business") or "")
            if not _CURSOR_N.match(top) or not _CURSOR_N.match(at) or business and not _BUSINESS.match(business):
                return {"ok": False, "error": "请求格式不对"}
            params = {"ps": BILI_HISTORY_N, "type": "all", "max": int(top), "view_at": int(at)}
            if business:
                params["business"] = business
            try:
                data = self._bili_get(caller.user, "/x/web-interface/history/cursor", params)
            except FeedError as error:
                return dict(answer, state=error.state, message=error.message)
            items, more = bili_history(data), history_cursor(data)
            state = "ready"
        else:
            part = self._source(caller, "bili_history", query.get("fresh") == "1", sync=True)
            items, state = part["items"], part["state"]
            with self.lock:
                more = self._history_more.get(caller.user)
            answer["message"] = part["message"]
            if part.get("age") is not None:
                answer["age"] = int(part["age"])
        return dict(answer, state=state, more=more if items else None,
                    items=[media("bili_history", e) for e in items])

    def watched(self, caller, wait: float = 0.0) -> dict:
        """这个人最近看过的剧集、番剧：{"ss<季 id>": {"ep", "n", "progress", "duration"}}（watched_seasons）。
        缓存过期了在后台重新取；还一次都没取到时最多等 wait 秒（搜索先叫一次 wait=0 让它先取着）。"""
        if caller is None or getattr(caller, "guest", False) or caller.user is None or self._bili(caller.user)[0] != "ok":
            return {}
        part = self._source(caller, "bili_seasons", False)
        deadline = time.monotonic() + wait
        while not part["items"] and part["state"] == "loading" and time.monotonic() < deadline:
            time.sleep(0.05)
            part = self._source(caller, "bili_seasons", False)
        return watched_seasons(part["items"])

    def _expire(self, user: int, sources: tuple) -> None:
        """这些来源的缓存算过期（内容留着先用，下次问时重新取）：刚回写过，B 站那边的观看记录变了。"""
        with self.lock:
            for source in sources:
                cached = self._cache.get((user, source))
                if cached is not None:
                    self._cache[(user, source)] = (cached[0] - TTL_S[source] - FRESH_GAP_S, cached[1])

    def _fetch_bili_dyn(self, user: int) -> list[dict]:
        data = self._bili_get(user, "/x/polymer/web-dynamic/v1/feed/all",
                              {"type": "video", "page": 1, "timezone_offset": -480, "features": "itemOpusStyle"},
                              signed=True)
        return bili_dynamic(data)

    # ---- YouTube ----

    def _youtube_feed(self, kind: str) -> list[dict]:
        from .bilibili import SourceError
        client = self.relay.clients.get("youtube")
        if client is None or not hasattr(client, "feed"):
            raise FeedError("error", "YouTube 还没开启")
        with self._yt_lock:
            try:
                videos = client.feed(kind, YT_FEED_N)
            except SourceError as error:
                reason = getattr(error, "reason", "")
                self.relay.log(f"取 YouTube {kind} 失败：{reason or type(error).__name__}")
                if reason == "login":
                    self.relay._youtube_health = None
                    raise FeedError("login", YT_LOGIN) from None
                if reason == "unsupported":
                    raise FeedError("error", YT_OLD, BLOCKED_GAP_S) from None
                if reason == "slow":
                    raise FeedError("error", YT_SLOW) from None
                raise FeedError("error", YT_DOWN) from None
        return youtube_entries(videos)

    def _fetch_yt_rec(self, user: int) -> list[dict]:
        return self._youtube_feed("rec")

    def _fetch_yt_history(self, user: int) -> list[dict]:
        return self._youtube_feed("history")

    def _fetch_yt_subs(self, user: int) -> list[dict]:
        return self._youtube_feed("subs")

    # ---- 设置、登录变了、删用户 ----

    def set_settings(self, caller, body: dict) -> dict:
        changes = {k: body[k] for k in DEFAULT_SETTINGS if k in body}
        if not changes or not all(isinstance(v, bool) for v in changes.values()):
            return {"ok": False, "error": "请求格式不对"}
        def change(d):
            d["settings"].update(changes)
            d["chosen"] = sorted(set(d["chosen"]) | set(changes))     # 自己动过的：以后照他的（CHOSEN_ONLY）
        data = self.store.update(caller.user, change)
        if "writeback" in changes:
            self.relay.log(f"用户 {caller.user} {'打开' if changes['writeback'] else '关掉'}了「同步到 B 站历史」")
        return {"ok": True, "settings": dict(data["settings"])}

    def logged_in(self, user: int, admin: bool = False) -> None:
        """扫码登录 B 站成功：作废他的 B 站几排，马上同步一次关注。"""
        self._drop(user, "bili")
        self.follows.kick(user, force=True)

    def logged_out(self, user: int) -> None:
        self._drop(user, "bili")
        self.follows.kick(user, force=True)

    def youtube_changed(self) -> None:
        """cookies 换了（导入、删除、从浏览器读取）：YouTube 的几排都作废。"""
        with self.lock:
            for key in [k for k in self._cache if k[1].startswith("yt")]:
                self._cache.pop(key, None)
            for key in [k for k in self._errors if k[1].startswith("yt")]:
                self._errors.pop(key, None)

    def _drop(self, user: int, prefix: str) -> None:
        with self.lock:
            for table in (self._cache, self._errors):
                for key in [k for k in table if k[0] == user and k[1].startswith(prefix)]:
                    table.pop(key, None)

    def forget(self, user: int) -> None:
        """删用户：他的设置、关注和缓存都删掉。"""
        self._drop(user, "")
        self.store.delete(user)

    def tick(self) -> None:
        """后台每隔一会儿叫一次（__main__ 的同步线程）：到点的同步关注。"""
        self.follows.tick()

    # ---- 回写到 B 站历史 ----

    def report(self, user: int | None, site: str, vid: str, position: float, final: bool = False) -> bool:
        """在小电视或这台电脑上看到了 position 秒：这个人打开了回写、登录着 B 站时，报给他的 B 站历史。
        返回这次有没有报（排进队里）。"""
        if user is None or site != "bilibili" or not isinstance(vid, str):
            return False
        match = _BV_PART.match(vid) or _EP_PART.match(vid)
        if match and vid.startswith("ep"):
            bvid, cid = f"ep{int(match.group(1))}", int(match.group(2) or 0)   # 番剧、影视剧的一集：报心跳
        else:
            bvid, cid = (match.group(1), int(match.group(2))) if match else (vid, 0)
            if not _BVID.match(bvid):
                return False      # 整季（ss…）、别的：不知道是哪一集，不报
        try:
            position = float(position)
        except (TypeError, ValueError):
            return False
        if position < REPORT_MIN_S:
            return False
        if not self.store.settings(user)["writeback"]:
            return False
        login = self.relay.bili_accounts.login_for(user) if self.relay.bili_accounts is not None else None
        if login is None:
            return False
        now = self.monotonic()
        key = (user, vid)
        with self.lock:
            last = self._reported.get(key)
            if last is not None and (not final and now - last[0] < REPORT_GAP_S or abs(last[1] - position) < 1):
                return False
            self._reported[key] = (now, position)
            while len(self._reported) > 2000:
                self._reported.pop(next(iter(self._reported)))
        job = (user, login, bvid, cid, int(position))
        if not self.background:
            self._send_report(*job)
            return True
        with self.lock:
            if self._queue is None:
                self._queue = queue.Queue(REPORT_QUEUE)
                threading.Thread(target=self._reporter, daemon=True, name="bili-history-report").start()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            return False
        return True

    def _reporter(self) -> None:
        while True:
            job = self._queue.get()
            try:
                self._send_report(*job)
            except Exception as error:  # noqa: BLE001 —— 后台线程不能死
                self.relay.log(f"回写 B 站历史出错：{type(error).__name__}")

    def _send_report(self, user: int, login, bvid: str, cid: int, progress: int) -> bool:
        if bvid.startswith("ep"):
            sent = self._send_heartbeat(user, login, int(bvid[2:]), cid, progress)
        else:
            sent = self._send_archive(user, login, bvid, cid, progress)
        if sent:
            self._expire(user, ("bili_history", "bili_seasons"))
        return sent

    def _send_archive(self, user: int, login, bvid: str, cid: int, progress: int) -> bool:
        accounts = self.relay.bili_accounts
        cookie = login.header()
        try:
            known = self._views.get(bvid)
            if known is None:
                answer, _ = accounts.fetch(f"{API}/x/web-interface/view?{_query({'bvid': bvid})}", cookie)
                data = answer.get("data") if isinstance(answer, dict) and answer.get("code") == 0 else None
                aid, first = _int((data or {}).get("aid")), _int((data or {}).get("cid"))
                if not aid or not first:
                    if isinstance(answer, dict) and answer.get("code") == -101:
                        accounts.mark_invalid(user)
                    return False
                known = (aid, first)
                with self.lock:
                    self._views[bvid] = known
                    while len(self._views) > VIEW_KEEP:
                        self._views.pop(next(iter(self._views)))
            aid, cid = known[0], cid or known[1]
            answer, _ = accounts.fetch(f"{API}/x/v2/history/report", cookie,
                                       data={"aid": aid, "cid": cid, "progress": progress, "platform": "web",
                                             "csrf": login.csrf})
        except (OSError, ValueError) as error:
            self.relay.log(f"回写 B 站历史失败：{type(error).__name__}")
            return False
        return self._sent(user, answer)

    def _sent(self, user: int, answer) -> bool:
        code = answer.get("code") if isinstance(answer, dict) else None
        if code == -101:
            self.relay.bili_accounts.mark_invalid(user)
        if code != 0:
            self.relay.log(f"回写 B 站历史：B 站回答 code={code}")
            return False
        return True

    def _episode(self, user: int, cookie: str, epid: int) -> tuple | None:
        """番剧、影视剧的一集 → (aid, cid, 季 id, 类型)：问 /pgc/view/web/season?ep_id=，记住。
        类型（season.type）就是心跳的 sub_type：1 番剧、2 电影、3 纪录片、4 国创、5 电视剧、7 综艺。"""
        key = f"ep{epid}"
        known = self._views.get(key)
        if known is not None:
            return known
        answer, _ = self.relay.bili_accounts.fetch(f"{API}/pgc/view/web/season?{_query({'ep_id': epid})}", cookie)
        result = answer.get("result") if isinstance(answer, dict) and answer.get("code") == 0 else None
        if not isinstance(result, dict):
            if isinstance(answer, dict) and answer.get("code") == -101:
                self.relay.bili_accounts.mark_invalid(user)
            return None
        episodes = list(result.get("episodes") or [])
        for section in result.get("section") or []:
            if isinstance(section, dict):
                episodes.extend(section.get("episodes") or [])
        found = next((e for e in episodes if isinstance(e, dict) and e.get("id") == epid), None)
        sid, kind = _int(result.get("season_id")), _int(result.get("type"))
        if found is None or not sid or not _int(found.get("aid")) or not _int(found.get("cid")):
            return None
        known = (_int(found["aid"]), _int(found["cid"]), sid, kind)
        with self.lock:
            self._views[key] = known
            while len(self._views) > VIEW_KEEP:
                self._views.pop(next(iter(self._views)))
        return known

    def _send_heartbeat(self, user: int, login, epid: int, cid: int, progress: int) -> bool:
        """番剧、影视剧的一集：网页播放器的播放心跳（type 4 是剧集），B 站据此把这一集和看到的地方记进观看记录。"""
        cookie = login.header()
        try:
            known = self._episode(user, cookie, epid)
            if known is None:
                return False
            aid, first, sid, kind = known
            answer, _ = self.relay.bili_accounts.fetch(
                f"{API}/x/click-interface/web/heartbeat", cookie,
                data={"aid": aid, "cid": cid or first, "epid": epid, "sid": sid, "type": 4, "sub_type": kind or 1,
                      "played_time": progress, "real_played_time": progress, "realtime": progress,
                      "start_ts": int(self.clock()) - progress, "dt": 2, "play_type": 0, "csrf": login.csrf})
        except (OSError, ValueError) as error:
            self.relay.log(f"回写 B 站历史失败：{type(error).__name__}")
            return False
        return self._sent(user, answer)


FETCHERS = {"bili_rec": Feeds._fetch_bili_rec, "bili_history": Feeds._fetch_bili_history,
            "bili_seasons": Feeds._fetch_bili_seasons,
            "bili_dyn": Feeds._fetch_bili_dyn, "yt_rec": Feeds._fetch_yt_rec,
            "yt_history": Feeds._fetch_yt_history, "yt_subs": Feeds._fetch_yt_subs}


def attach(relay) -> "Feeds | None":
    """web.Relay 建好后：家庭服务器和电脑客户端才有（B 站登录在、能在电脑上看），云端为 None。"""
    if relay.selfhost is None or relay.accounts is None or getattr(relay, "bili_accounts", None) is None:
        return None
    return Feeds(relay, Path(relay.accounts.path).parent / "feeds", clock=relay.clock)


# ---------------------------------------------------------------- web.py 转过来的请求

def caller_of(handler, relay):
    """能用的调用者（自建、有账号、不是访客），否则 None（回答 404）。"""
    if getattr(relay, "account_feeds", None) is None:
        return None
    caller = handler._caller()
    if caller is None or caller.guest or caller.user is None:
        return None
    return caller


def get(handler, relay, path: str) -> bool:
    if path not in (PREFIX, PREFIX + "/row", PREFIX + "/history"):
        return False
    caller = caller_of(handler, relay)
    if caller is None:
        handler._not_found()
        return True
    feeds = relay.account_feeds
    if path == PREFIX:
        handler._json(feeds.overview(caller))
    elif path == PREFIX + "/history":
        handler._json(feeds.history_page(caller, handler._query()))
    else:
        query = handler._query()
        handler._json(feeds.row(caller, query.get("id") or "", fresh=query.get("fresh") == "1"))
    return True


def read_body(handler) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None
    if length == 0:
        return {}
    if not 0 < length <= 16 * 1024:
        return None
    try:
        handler._read_body = True
        body = json.loads(handler.rfile.read(length))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def post(handler, relay, path: str) -> bool:
    if path != PREFIX + "/settings":
        return False
    body = read_body(handler)
    caller = caller_of(handler, relay)
    if caller is None:
        handler._not_found()
        return True
    if body is None:
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return True
    handler._json(relay.account_feeds.set_settings(caller, body))
    return True
