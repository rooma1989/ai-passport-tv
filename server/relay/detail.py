"""剧集、UP 主的资料页和统一的「看到哪儿」（媒体中心界面的 M4）。

设计见 docs/superpowers/specs/2026-10-24-media-center-ui-design.md 第 2、8、11 节。

看到哪儿原来记在好几个地方，格式各不相同。这里不迁移、不改写它们，只是一起读（Marks）：

- 小电视：每个用户的状态（state.py）里每个台的 positions（视频 id → 秒）和 watched（看完了的视频 id）。没有时间。
- 这台电脑：数据目录的 pc-progress.json（home.PcProgress）。每条记录是正在看的那一集（bvid）、position、
  duration、at；done 是电脑上看完了的各集。UP 主台、剧集台、投到电脑的视频（v:<网站>:<id>）都在这里。
- 片库：也在 pc-progress.json，键 lib:<条目 id>；在小电视上看的，记录里有 device（那台的名字）。

统一进度的键（GET /api/progress?keys=k1,k2,…，最多 PROGRESS_KEYS 个）：

- v:<网站>:<视频 id>：B 站的 BV 号、番剧的 ep…/cid，YouTube 的视频 id。在哪个台、投屏还是在电脑上看的都算。
- lib:<条目 id>：片库里的一个视频。
- ch:<台 id>：一个 UP 主台或剧集台合起来：看完了几个，剧集接着看第几集、从哪儿起。

合并的规矩：哪儿说看完了就算看完了（小电视的 watched、电脑上的 done，或者看到离结尾不到 END_S 秒）；看到哪儿取
最远的那个（小电视的进度没记时间，比不出先后；取最远的，不会让人把看过的再看一遍）。剧集「接着看第几集」：有记录的
最后一集没看完就是它（从看到的地方起）；看完了就是下一集；最后一集也看完了，整部就看完了，重看从第 1 集起。

首页的「剧集追更」「UP 主更新」（home.py）也用这里，三处的标记一样。

    GET /api/progress?keys=…          {ok, items: {键: 进度或 null}}
    GET /api/detail/series            剧集海报墙：每部剧的海报和进度
    GET /api/detail/series/<sr..>     一部剧：海报、简介、各集和每集看到哪儿
    GET /api/detail/up/<up.. | 关注的键>  一个 UP 主：头像、简介、最近的投稿和每个看到哪儿、在不在小电视上

都按调用者自己的订阅和进度；访客没有（404）。
"""

from __future__ import annotations

import re
import time

from . import home
from .tenants import Caller, NotFound

PROGRESS_KEYS = 300
KEY_MAX = 120
END_S = home.PROGRESS_END_S
# 剧集最后一集是这么多秒以内发布的（而且已经开始看、还没看完）：海报上标「更新」。
NEW_S = 7 * 24 * 3600
TV_NAME = "小电视"
_SERIES_ID = re.compile(r"^sr\d\d$")
_UP_ID = re.compile(r"^up\d\d$")
_VIDEO_KEY = re.compile(r"^v:(bilibili|youtube):([0-9A-Za-z_/-]{1,80})$")
_LIB_KEY = re.compile(r"^lib:([0-9A-Za-z_-]{1,80})$")
_CHANNEL_KEY = re.compile(r"^ch:((?:up|sr)\d\d)$")


def _finished_at(position: float, duration: float) -> bool:
    """和 web.py 的 _pc_report 一样：离结尾不到 END_S 秒（短视频是最后一成）就算看完了。"""
    return bool(duration) and position >= duration - min(END_S, duration * 0.1)


class Marks:
    """一个人在各处记下的进度，读一次，按视频 id 查。不改任何存的东西。"""

    def __init__(self, relay, caller: Caller | None, account):
        self._best: dict[str, dict] = {}
        self._done: set[str] = set()
        clock = getattr(relay, "clock", None)
        self.now = clock() if callable(clock) else time.time()
        store = getattr(relay, "pc_progress", None)
        user = caller.user if caller is not None else None
        if store is not None and user is not None:
            for key, rec in store.rows(user):
                # 片库的条目 id 和视频 id 不是一回事：前面加上 lib:，免得撞上。
                space = "lib:" if key.startswith("lib:") else ""
                self._done.update(space + b for b in rec.get("done") or [] if isinstance(b, str))
                vid = rec.get("bvid")
                if isinstance(vid, str) and vid:
                    self._offer(space + vid, rec.get("position"), rec.get("duration"), rec.get("at"),
                                rec.get("device") or home.PC_NAME)
        if account is not None:
            with account.lock:
                for c in account.creators:
                    lengths = {v.bvid: v.duration for v in c.videos}
                    self._done.update(c.watched)
                    for vid, position in c.positions.items():
                        self._offer(vid, position, lengths.get(vid), 0, TV_NAME)

    def _offer(self, vid: str, position, duration, at, device: str) -> None:
        try:
            position, duration, at = float(position or 0), float(duration or 0), float(at or 0)
        except (TypeError, ValueError):
            return
        old = self._best.get(vid)
        if old is None or position > old["position"] or (position == old["position"] and at > old["at"]):
            self._best[vid] = {"position": position, "duration": duration or (old or {}).get("duration", 0),
                               "at": at, "device": device}
        elif not old["duration"] and duration:
            old["duration"] = duration

    def finished(self, vid: str) -> bool:
        found = self.get(vid)
        return bool(found and found["finished"])

    def get(self, vid: str, duration: float = 0) -> dict | None:
        """{position, duration, finished, device, at}；哪儿都没记过时 None。"""
        found = self._best.get(vid)
        done = vid in self._done
        if found is None and not done:
            return None
        length = float(duration or 0) or (found["duration"] if found else 0)
        position = found["position"] if found else 0.0
        return {"position": int(position), "duration": int(length),
                "finished": done or _finished_at(position, length),
                "device": found["device"] if found else "", "at": int(found["at"]) if found else 0}

    def channel(self, creator) -> dict:
        """一个台合起来（调用者拿着状态的锁或传进来的是副本）：看完了几个；剧集接着看第几集、从哪儿起。"""
        videos = list(creator.videos)
        rows = [self.get(v.bvid, v.duration) for v in videos]
        total = len(videos)
        watched = sum(1 for m in rows if m and m["finished"])
        last = max((n for n, m in enumerate(rows, 1) if m), default=0)
        answer = {"id": creator.id, "kind": creator.kind, "total": total, "watched": watched,
                  "started": last > 0, "finished": False, "episode": 1 if total else 0,
                  "position": 0, "duration": videos[0].duration if videos else 0, "device": ""}
        if creator.kind != "series":
            # UP 主台：新的在前，没有「第几集」；left 是还没看的几个，episode 是最新的没看的那个。
            fresh = next((n for n, m in enumerate(rows, 1) if not (m and m["finished"])), 0)
            answer.update(left=total - watched, episode=fresh)
            return answer
        if last:
            mark = rows[last - 1]
            if not mark["finished"]:
                answer.update(episode=last, position=mark["position"], duration=mark["duration"], device=mark["device"])
            elif last < total:
                after = rows[last]
                answer.update(episode=last + 1, duration=videos[last].duration, device=mark["device"],
                              position=after["position"] if after else 0)
            else:
                answer.update(finished=True, episode=1, device=mark["device"])
        latest = max((v.pubdate or 0 for v in videos), default=0)
        answer["new"] = bool(last and not answer["finished"] and latest and self.now - latest < NEW_S
                             and not (rows[-1] and rows[-1]["finished"]))
        return answer


def _account(relay, caller: Caller):
    """调用者自己的状态；访客、云端没登录的一律 404（NotFound）。"""
    if caller.guest:
        raise NotFound("account")
    return relay._account(caller)


def _key_mark(marks: Marks, account, key: str):
    found = _VIDEO_KEY.match(key)
    if found:
        return marks.get(found.group(2))
    found = _LIB_KEY.match(key)
    if found:
        return marks.get("lib:" + found.group(1))
    found = _CHANNEL_KEY.match(key)
    if found:
        with account.lock:
            creator = account.get(found.group(1))
            return marks.channel(creator) if creator is not None else None
    return None


def progress(relay, caller: Caller, keys) -> dict:
    """GET /api/progress?keys=k1,k2,…：每个键的进度（见模块说明）；没记过、认不出的键是 null。"""
    account = _account(relay, caller)
    wanted = [k for k in (keys or "").split(",") if k][:PROGRESS_KEYS] if isinstance(keys, str) else []
    if not wanted or any(len(k) > KEY_MAX for k in wanted):
        return {"ok": False, "error": "请求格式不对"}
    marks = Marks(relay, caller, account)
    return {"ok": True, "items": {k: _key_mark(marks, account, k) for k in wanted}}


# ---------------------------------------------------------------- 剧集

def _series_art(relay, creator, size: str = "p") -> str:
    """剧集的海报：番剧是季的封面（ss…），多集视频是那个视频的封面；YouTube 播放列表用第一集的缩略图。"""
    art = home._cover(relay, creator.site, creator.source, size)
    if not art and creator.videos:
        art = home._cover(relay, creator.site, creator.videos[0].bvid, size)
    return art


def _desc(relay, creator) -> str:
    getter = getattr(relay.clients.get(creator.site), "desc", None)
    try:
        text = getter(creator.source) if getter is not None else ""
    except Exception:  # noqa: BLE001 —— 测试替身、旧客户端：没有简介就不显示
        return ""
    return text if isinstance(text, str) else ""


def _on_tv(relay, caller: Caller, cid: str) -> list[str]:
    """调用者的哪几台小电视正在播这个台（名字）。"""
    names = []
    for dev in relay.devices_for(caller):
        playing = relay._playing(dev)
        if playing["online"] and playing["channel"] == cid:
            names.append(dev["name"])
    return names


def _poster(relay, creator, marks: Marks) -> dict:
    """海报墙的一张（拿着状态的锁调；在哪台小电视上播由调用者放锁以后补上 on_tv）。"""
    summary = marks.channel(creator)
    art = _series_art(relay, creator)
    if not art:
        home._warm(relay, "series", creator.site, creator.source)
    return {"id": creator.id, "name": creator.name, "site": creator.site, "key": "ch:" + creator.id,
            "art": art, "progress": summary, "on_tv": []}


def series_list(relay, caller: Caller) -> dict:
    """GET /api/detail/series：剧集海报墙，按添加的顺序。"""
    account = _account(relay, caller)
    marks = Marks(relay, caller, account)
    with account.lock:
        series = [c for c in account.creators if c.kind == "series"]
        rows = [_poster(relay, c, marks) for c in series]
    for row in rows:
        row["on_tv"] = _on_tv(relay, caller, row["id"])
    return {"ok": True, "items": rows}


def series_detail(relay, caller: Caller, cid: str) -> dict:
    """GET /api/detail/series/<sr..>：一部剧的资料页。"""
    account = _account(relay, caller)
    if not _SERIES_ID.match(cid or ""):
        return {"ok": False, "error": "没有这部剧"}
    marks = Marks(relay, caller, account)
    with account.lock:
        creator = account.get(cid)
        if creator is None or creator.kind != "series":
            return {"ok": False, "error": "没有这部剧"}
        poster = _poster(relay, creator, marks)
        episodes = [{"n": n, "id": v.bvid, "key": f"v:{v.site}:{v.bvid}", "title": v.title, "duration": v.duration,
                     "art": home._cover(relay, v.site, v.bvid), "mark": marks.get(v.bvid, v.duration)}
                    for n, v in enumerate(creator.videos, 1)]
        tv_next = account.episode(cid)
        desc = _desc(relay, creator)
    return dict(poster, ok=True, desc=desc, total=len(episodes), episodes=episodes, tv_next=tv_next,
                on_tv=_on_tv(relay, caller, cid))


# ---------------------------------------------------------------- UP 主

def _follows(relay):
    feeds = getattr(relay, "account_feeds", None)
    return getattr(feeds, "follows", None) if feeds is not None else None


def _video_item(relay, site: str, name: str, face: str, v, play: dict, marks: Marks) -> dict:
    return {"id": f"up:{site}:{v.bvid}", "kind": "video", "title": v.title, "subtitle": name, "source": site,
            "duration": v.duration, "pubdate": v.pubdate, "key": f"v:{site}:{v.bvid}",
            "art": {"url": home._cover(relay, site, v.bvid), "ratio": "wide"},
            "creator": {"name": name, "face": face}, "play": play, "mark": marks.get(v.bvid, v.duration)}


def up_detail(relay, caller: Caller, ident: str) -> dict:
    """GET /api/detail/up/<up..>（小电视上的 UP 主台）或 /api/detail/up/<bilibili:mid | youtube:UC…>（我的关注）。

    台的投稿是中转留着的最新 LIST_SIZE 个，点了按台播（小电视从下一个没看的播起）；只是关注、没放到小电视的，
    投稿是 follows.videos 取的（后台取，loading 时页面再问），点了按视频播。"""
    from .follows import parse_key, tv_map
    account = _account(relay, caller)
    follows = _follows(relay)
    marks = Marks(relay, caller, account)
    key, cid = "", ""
    if _UP_ID.match(ident or ""):
        cid = ident
    elif parse_key(ident) is not None:
        key = ident
    else:
        return {"ok": False, "error": "没有这个 UP 主"}
    entry, followed = None, False
    if follows is not None:
        data = follows.store.load(caller.user)["follows"]
        known = dict(data.get("unfollowed") or {}, **(data.get("items") or {}))
        on_tv = tv_map(account, known)
        if key:
            cid = on_tv.get(key, "")
        else:
            key = next((k for k, c in on_tv.items() if c == cid), "")
        entry = known.get(key) if key else None
        followed = bool(key) and key in (data.get("items") or {})
    answer = {"ok": True, "key": key, "id": cid, "followed": followed,
              "can_follow_tv": follows is not None and bool(entry), "loading": False}
    if cid:
        with account.lock:
            creator = account.get(cid)
            if creator is None or creator.kind != "up":
                return {"ok": False, "error": "没有这个 UP 主"}
            face = home._face(relay, creator.site, creator.source)
            if not face and entry and entry.get("face"):
                face = home.thumb(entry["face"], "f")
            summary = marks.channel(creator)
            videos = [_video_item(relay, creator.site, creator.name, face, v,
                                  {"kind": "video", "channel": cid, "index": n}, marks)
                      for n, v in enumerate(creator.videos, 1)]
            count = max(account.totals.get(cid) or 0, len(creator.videos))
            answer.update(name=creator.name, site=creator.site, face=face, count=count, videos=videos,
                          progress=summary)
        answer["on_tv"] = _on_tv(relay, caller, cid)
    else:
        if entry is None:
            return {"ok": False, "error": "这个 UP 主不在你的关注里"}
        listing = follows.videos(caller, key)
        if not listing.get("ok"):
            return listing
        site = entry.get("site") or listing.get("site") or "bilibili"
        face = home.thumb(entry.get("face") or "", "f") if entry.get("face") else ""
        videos = []
        for x in listing.get("items") or []:
            vid = (x.get("play") or {}).get("id") or ""
            videos.append(dict(x, key=f"v:{site}:{vid}", mark=marks.get(vid, x.get("duration") or 0)))
        answer.update(name=entry.get("name") or listing.get("name") or key, site=site, face=face, count=0,
                      videos=videos, loading=bool(listing.get("loading")), on_tv=[],
                      progress={"total": len(videos), "watched": sum(1 for x in videos if x["mark"] and x["mark"]["finished"])})
        if listing.get("error"):
            answer["error_note"] = listing["error"]
    if entry:
        answer["sign"] = entry.get("sign") or ""
    return answer
