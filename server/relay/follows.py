"""「UP 主」页的「我的关注」：自动同步每个人在 B 站关注的 UP 主和（管理员的）YouTube 订阅，不用一个个加。

设计：docs/superpowers/specs/2026-10-24-media-center-ui-design.md 第 18 节。家庭服务器和电脑客户端才有（accountfeeds.py
建它）；云端一律 404。

    GET  /api/follows[?fresh=1]        合在一起的关注列表（不限个数）、上次同步、哪些放在小电视上；到点了顺手开始同步
    POST /api/follows/sync             立即同步（后台做，页面再问上面那个）
    POST /api/follows/tv               {"key": "bilibili:<mid>" | "youtube:<UC…>", "on": bool}：放到小电视 / 拿下来
    GET  /api/follows/videos?key=…     这个 UP 主最新的投稿（在电脑上翻，点了播到小电视或这台电脑）

同步：B 站是 bililogin.followings（/x/relation/followings，分页取完），YouTube 是节点的 /yt/subscriptions
（ytaccount.py，只有管理员、本机模式、有 cookies 时）。登录成功时、之后每 SYNC_EVERY_S 秒（后台的 tick）、点「立即同步」
时各同步一次；两次至少隔 SYNC_GAP_S 秒。新关注的出现，取关的消失。哪个网站这次取不到，就留着它上次的列表（不当成全取关了）；
退出了 B 站登录，B 站的那些就不列了（这不是取关）。

小电视：「放到小电视」用原来的 UP 主台（state.add），受 UP 主台个数（state.MAX_CREATORS，12 个）和小电视设置包大小
（relay.room_of）的限制。手动加的 UP 主台不受影响。取关了、但还在小电视上的，不悄悄删掉台，在列表里标「已取关」，
人自己决定拿不拿下来。设置里「同步时自动把新关注放到小电视（直到放满）」默认关；第一次同步不算「新关注」。
"""

from __future__ import annotations

import re
import threading
import time

from . import home
from .bilibili import SourceError
from .youtube import channel_url

PREFIX = "/api/follows"
SYNC_EVERY_S = 6 * 3600
SYNC_GAP_S = 60
TICK_GAP_S = 300
VIDEOS_TTL_S = 600
VIDEOS_ERROR_GAP_S = 30
VIDEOS_MAX = 30
_KEY = re.compile(r"^(bilibili):([0-9]{1,20})$|^(youtube):(UC[0-9A-Za-z_-]{22})$")
_CHANNEL_URL = re.compile(r"^https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})$")
_HANDLE_URL = re.compile(r"^https://www\.youtube\.com/(@[^/?#\s]+)$")

NO_ACCOUNT = "登录 B 站（或导入 YouTube cookies）后，这里会自动列出你关注的 UP 主"
TV_FULL = "小电视内存小，最多 {max} 个 UP 主台：先关掉一个再放"
TV_NO_ROOM = "小电视的频道列表放不下了（设置包的大小有限）：先关掉一个台再放"


def parse_key(key) -> tuple[str, str] | None:
    found = _KEY.match(key) if isinstance(key, str) else None
    if found is None:
        return None
    return (found.group(1), found.group(2)) if found.group(1) else (found.group(3), found.group(4))


def tv_map(state, items: dict | None = None) -> dict:
    """这份状态里的 UP 主台 → {关注的键: 台 id}。YouTube 的台是 /channel/UC… 或 @handle（按关注里记的 handle 对上）。"""
    handles = {}
    for key, e in (items or {}).items():
        if e.get("site") == "youtube" and e.get("handle"):
            handles[e["handle"].lower()] = key
    found = {}
    with state.lock:
        creators = [(c.id, c.site, c.source) for c in state.creators if c.kind == "up"]
    for cid, site, source in creators:
        if site == "bilibili":
            found[f"bilibili:{source}"] = cid
            continue
        match = _CHANNEL_URL.match(source or "")
        if match:
            found[f"youtube:{match.group(1)}"] = cid
            continue
        match = _HANDLE_URL.match(source or "")
        if match and match.group(1).lower() in handles:
            found[handles[match.group(1).lower()]] = cid
    return found


class Follows:
    def __init__(self, relay, feeds, background: bool = True, clock=time.time, monotonic=time.monotonic):
        self.relay = relay
        self.feeds = feeds
        self.store = feeds.store
        self.background = background
        self.clock = clock
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self._syncing: set[int] = set()
        self._started: dict[int, float] = {}
        self._ticked = -1e9
        self._tv_lock = threading.Lock()
        # 投稿列表（大家共用，公开的）：键 → (时刻, 名字, [Video])；出错 → 时刻；正在取的。
        self._videos: dict[str, tuple] = {}
        self._video_errors: dict[str, tuple] = {}
        self._video_busy: set[str] = set()
        # 测试替身可以换掉：这个人的 YouTube 订阅能不能取（""）或为什么不能。
        self.yt_gate = self._yt_gate

    # ---- 每个网站能不能用 ----

    def _admin(self, user: int) -> bool:
        row = self.relay.accounts.user(user) if self.relay.accounts is not None else None
        return bool(row and row.get("role") == "admin" and row.get("status", "active") == "active")

    def _yt_gate(self, user: int) -> str:
        """"" 能取；"hidden" 不是管理员或 YouTube 没开；其余是给页面看的原因。"""
        if not self._admin(user) or self.relay.clients.get("youtube") is None:
            return "hidden"
        from . import ytaccount
        try:
            return ytaccount.subscriptions(self.relay).gate()
        except (OSError, ValueError, TypeError):
            return ytaccount.NO_COOKIES

    def _bili_state(self, user: int) -> str:
        accounts = self.relay.bili_accounts
        record = accounts.record(user) if accounts is not None else None
        return "off" if record is None else "invalid" if record.get("invalid") else "ok"

    # ---- 同步 ----

    def due(self, user: int) -> bool:
        record = self.store.load(user)["follows"]
        if self._bili_state(user) == "off" and self.yt_gate(user) and not record.get("items"):
            return False
        return self.clock() - float(record.get("synced_at") or 0) >= SYNC_EVERY_S

    def kick(self, user: int, force: bool = False) -> bool:
        """开始同步（后台）。正在同步、两次隔不到 SYNC_GAP_S 秒时不开始（force 只跳过后一条）。"""
        now = self.monotonic()
        with self.lock:
            if user in self._syncing:
                return False
            if not force and now - self._started.get(user, -1e9) < SYNC_GAP_S:
                return False
            self._syncing.add(user)
            self._started[user] = now
        if self.background:
            threading.Thread(target=self._run, args=(user,), daemon=True, name="follows-sync").start()
        else:
            self._run(user)
        return True

    def tick(self) -> None:
        now = self.monotonic()
        if now - self._ticked < TICK_GAP_S:
            return
        self._ticked = now
        for row in self.relay.accounts.users():
            if row.get("status", "active") != "active":
                continue
            try:
                if self.due(row["id"]):
                    self.kick(row["id"])
            except Exception as error:  # noqa: BLE001 —— 后台线程不能死
                self.relay.log(f"检查用户 {row['id']} 的关注同步出错：{type(error).__name__}")

    def _run(self, user: int) -> None:
        try:
            self.sync(user)
        except Exception as error:  # noqa: BLE001 —— 后台线程：记下来
            self.relay.log(f"同步用户 {user} 的关注出错：{type(error).__name__}")
        finally:
            with self.lock:
                self._syncing.discard(user)

    def _fetch_bili(self, user: int):
        """([条目], 错误, 登录失效了吗)。"""
        answer = self.relay.bili_accounts.followings(user, fresh=True)
        if not answer.get("ok"):
            return None, answer.get("error") or "取不到关注列表", answer.get("login") is False
        items = [{"site": "bilibili", "id": f["mid"], "name": f["name"], "face": f.get("face") or "",
                  "sign": f.get("sign") or "", "handle": ""} for f in answer.get("items") or []]
        return items, "", False

    def _fetch_youtube(self):
        from . import ytaccount
        client = self.relay.clients.get("youtube")
        try:
            found = client.subscriptions()
        except SourceError as error:
            login = getattr(error, "reason", "") == "login"
            self.relay.log(f"同步 YouTube 订阅失败：{'没有登录' if login else type(error).__name__}")
            return None, ytaccount.LOGIN if login else ytaccount.UNREACHABLE, login
        items = [{"site": "youtube", "id": c["id"], "name": c["name"], "face": "", "sign": "",
                  "handle": c.get("handle") or ""} for c in found]
        return items, "", False

    def sync(self, user: int) -> dict:
        """同步一次：返回 {"added": [键], "removed": [键], "unfollowed": [键]}。"""
        now = self.clock()
        old = self.store.load(user)["follows"]
        old_items = dict(old.get("items") or {})
        sites = {k: dict(v) for k, v in (old.get("sites") or {}).items() if isinstance(v, dict)}
        fresh: dict[str, dict] = {}
        fetched: set[str] = set()      # 这次真取到了的网站：只有它们的「不见了」才算取关

        def keep(site):
            for key, e in old_items.items():
                if e.get("site") == site:
                    fresh[key] = e

        def take(site, items):
            fetched.add(site)
            for e in items:
                key = f"{site}:{e['id']}"
                before = old_items.get(key) or {}
                fresh[key] = dict(e, since=before.get("since") or now, latest=before.get("latest"))

        bili = self._bili_state(user)
        if bili == "ok":
            items, error, lapsed = self._fetch_bili(user)
            if items is None:
                keep("bilibili")
                sites["bilibili"] = dict(sites.get("bilibili") or {}, state="login" if lapsed else "error",
                                         error=error)
            else:
                take("bilibili", items)
                sites["bilibili"] = {"state": "ok", "error": "", "at": now, "count": len(items)}
        elif bili == "invalid":
            keep("bilibili")
            sites["bilibili"] = dict(sites.get("bilibili") or {}, state="login", error="B 站登录已失效，请重新扫码")
        else:
            sites["bilibili"] = {"state": "off", "error": "", "count": 0}
        gate = self.yt_gate(user)
        if not gate:
            items, error, lapsed = self._fetch_youtube()
            if items is None:
                keep("youtube")
                sites["youtube"] = dict(sites.get("youtube") or {}, state="login" if lapsed else "error", error=error)
            else:
                take("youtube", items)
                sites["youtube"] = {"state": "ok", "error": "", "at": now, "count": len(items)}
        elif gate == "hidden":
            sites["youtube"] = {"state": "hidden", "error": "", "count": 0}
        else:
            keep("youtube")
            sites["youtube"] = dict(sites.get("youtube") or {}, state="off", error=gate)
        removed = [k for k, e in old_items.items() if k not in fresh and e.get("site") in fetched]
        added = [k for k in fresh if k not in old_items]
        state = self.relay.user_state(user)
        on_tv = tv_map(state, dict(old_items, **fresh))
        first = not old.get("first_done")
        unfollowed_now = []

        def change(data):
            follows = data["follows"]
            marks = dict(follows.get("unfollowed") or {})
            for key in removed:
                if key in on_tv:
                    e = old_items[key]
                    marks[key] = {"site": e.get("site"), "id": e.get("id"), "name": e.get("name") or "",
                                  "face": e.get("face") or "", "handle": e.get("handle") or "", "at": now}
                    unfollowed_now.append(key)
            for key in fresh:
                marks.pop(key, None)
            follows.update(items=fresh, sites=sites, unfollowed=marks, synced_at=now,
                           first_done=bool(old.get("first_done") or fetched))
        self.store.update(user, change)
        if added or removed:
            self.relay.log(f"用户 {user} 的关注同步了：{len(fresh)} 个，新关注 {len(added)} 个，取关 {len(removed)} 个"
                           + (f"（{len(unfollowed_now)} 个还在小电视上，标成已取关）" if unfollowed_now else ""))
        auto = []
        if added and not first and self.store.settings(user)["autotv"]:
            for key in added:
                if key in on_tv:
                    continue
                answer = self._tv_on(user, state, key, fresh[key])
                if answer.get("ok"):
                    auto.append(key)
                elif answer.get("full"):
                    break
        return {"added": added, "removed": removed, "unfollowed": unfollowed_now, "auto_tv": auto}

    def note_latest(self, user: int, entries: list[dict]) -> None:
        """「关注的更新」取到了：顺手记下每个关注的人最新的投稿（列表里的「最新：…」），不另外访问网站。"""
        newest = {}
        for e in entries:
            if not e.get("author_id"):
                continue
            key = f"{e['site']}:{e['author_id']}"
            if key not in newest or (e.get("date") or 0) > (newest[key].get("at") or 0):
                newest[key] = {"title": e["title"], "at": e.get("date") or 0, "id": e["id"]}
        if not newest:
            return
        changed = False
        with self.store.lock:
            data = self.store.load(user)
            items = data["follows"].get("items") or {}
            for key, latest in newest.items():
                e = items.get(key)
                if e is not None and (not e.get("latest") or latest["at"] > (e["latest"].get("at") or 0)):
                    e["latest"] = latest
                    changed = True
            if changed:
                self.store.save(user, data)

    # ---- 列表 ----

    def listing(self, caller, fresh: bool = False) -> dict:
        from .state import MAX_CREATORS
        user = caller.user
        if fresh:
            self.kick(user)
        elif self.due(user):
            self.kick(user)
        data = self.store.load(user)
        follows = data["follows"]
        items = follows.get("items") or {}
        state = self.relay._account(caller)
        on_tv = tv_map(state, dict(follows.get("unfollowed") or {}, **items))
        marks = {k: v for k, v in (follows.get("unfollowed") or {}).items() if k in on_tv and k not in items}
        if marks != (follows.get("unfollowed") or {}):
            # 人自己把台拿下来了（或又关注了）：不再标「已取关」。
            self.store.update(user, lambda d: d["follows"].update(unfollowed=marks))
        rows = []
        for key, e in list(items.items()) + list(marks.items()):
            face = e.get("face") or ""
            rows.append({"key": key, "site": e.get("site"), "id": e.get("id"), "name": e.get("name") or "",
                         "face": home.thumb(face, "f") if face else "", "sign": e.get("sign") or "",
                         "handle": e.get("handle") or "", "latest": e.get("latest"),
                         "since": e.get("since") or 0, "tv": on_tv.get(key), "unfollowed": key in marks})
        with state.lock:
            count = sum(c.kind == "up" for c in state.creators)
        sites = follows.get("sites") or {}
        bili = self._bili_state(user)
        gate = self.yt_gate(user)
        bili_row = dict(sites.get("bilibili") or {})
        if bili == "ok":
            bili_row["state"] = bili_row.get("state") if bili_row.get("state") in ("ok", "error", "login") else "ok"
        else:
            bili_row["state"] = "login" if bili == "invalid" else "off"
        if gate == "hidden":
            yt_row = {"state": "hidden"}
        elif gate:
            yt_row = dict(sites.get("youtube") or {}, state="off", error=gate)
        else:
            yt_row = dict(sites.get("youtube") or {})
            yt_row["state"] = yt_row.get("state") or "ok"
        site_rows = {"bilibili": bili_row, "youtube": yt_row}
        with self.lock:
            syncing = user in self._syncing
        synced = float(follows.get("synced_at") or 0)
        answer = {"ok": True, "syncing": syncing, "synced_at": synced,
                  "next_at": synced + SYNC_EVERY_S if synced else 0, "sites": site_rows, "items": rows,
                  "tv": {"count": count, "max": MAX_CREATORS, "room": self.relay.room(caller=caller)},
                  "settings": dict(data["settings"])}
        if bili == "off" and gate:
            answer["message"] = NO_ACCOUNT
        return answer

    # ---- 放到小电视 ----

    def set_tv(self, caller, key, on) -> dict:
        found = parse_key(key)
        if found is None or not isinstance(on, bool):
            return {"ok": False, "error": "请求格式不对"}
        user = caller.user
        follows = self.store.load(user)["follows"]
        items = follows.get("items") or {}
        marks = follows.get("unfollowed") or {}
        state = self.relay._account(caller)
        with self._tv_lock:
            on_tv = tv_map(state, dict(marks, **items))
            if on:
                if key in on_tv:
                    return {"ok": True, "tv": on_tv[key]}
                e = items.get(key)
                if e is None:
                    return {"ok": False, "error": "这个 UP 主不在你的关注里（先同步一次）"}
                return self._tv_on(user, state, key, e, locked=True)
            cid = on_tv.get(key)
            if cid is not None:
                state.remove(cid)
                state.save()
                self.relay._tables_changed(state)
                self.relay.log(f"用户 {user} 把 {cid} 从小电视上拿下来了")
            if key in marks:
                self.store.update(user, lambda d: (d["follows"].get("unfollowed") or {}).pop(key, None))
            return {"ok": True, "tv": None}

    def _tv_on(self, user: int, state, key: str, e: dict, locked: bool = False) -> dict:
        if not locked:
            with self._tv_lock:
                return self._tv_on(user, state, key, e, locked=True)
        from .state import MAX_CREATORS
        from .table import label_for
        site, vid, name = e.get("site"), e.get("id"), e.get("name") or e.get("id")
        with state.lock:
            count = sum(c.kind == "up" for c in state.creators)
        if count >= MAX_CREATORS:
            return {"ok": False, "full": True, "error": TV_FULL.format(max=MAX_CREATORS)}
        if not self.relay.room_of(state, label_for(name, site))["fits"]:
            return {"ok": False, "full": True, "error": TV_NO_ROOM}
        client = self.relay.clients.get(site)
        if client is None:
            return {"ok": False, "error": self.relay._youtube_off("YouTube 还没配置好")}
        source = vid if site == "bilibili" else channel_url(vid)
        try:
            videos = client.uploads(vid) if site == "bilibili" else client.creator(source)[1]
        except SourceError as error:
            self.relay.log(f"把 {key} 放到小电视失败：{type(error).__name__}")
            if error.permanent:
                return {"ok": False, "error": "网站说这个 UP 主的投稿看不了"}
            return {"ok": False, "error": ("B 站" if site == "bilibili" else "YouTube") + "暂时连不上，过一会儿再试"}
        if not videos:
            return {"ok": False, "error": "这个 UP 主还没有投稿"}
        try:
            creator = state.add(site, source, name, videos, self.relay.clock())
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        state.set_total(creator.id, self.relay._upload_total(site, source))
        self.relay._cache_videos(creator, creator.videos, creator.fetched_at)
        state.save()
        self.relay._tables_changed(state)
        self.relay.log(f"用户 {user} 把关注的 {key} 放到小电视：{creator.id}（{len(videos)} 个投稿）")
        return {"ok": True, "tv": creator.id}

    # ---- 翻一个 UP 主的投稿 ----

    def videos(self, caller, key) -> dict:
        found = parse_key(key)
        if found is None:
            return {"ok": False, "error": "请求格式不对"}
        site, vid = found
        follows = self.store.load(caller.user)["follows"]
        e = (follows.get("items") or {}).get(key) or (follows.get("unfollowed") or {}).get(key)
        if e is None:
            return {"ok": False, "error": "这个 UP 主不在你的关注里"}
        if self.relay.clients.get(site) is None:
            return {"ok": False, "error": self.relay._youtube_off("YouTube 还没配置好")}
        now = self.monotonic()
        start = False
        with self.lock:
            cached = self._videos.get(key)
            error = self._video_errors.get(key)
            if key not in self._video_busy and (cached is None or now - cached[0] >= VIDEOS_TTL_S) and \
                    not (error is not None and now - error[0] < VIDEOS_ERROR_GAP_S):
                self._video_busy.add(key)
                start = True
        if start:
            if self.background:
                threading.Thread(target=self._fetch_videos, args=(key, site, vid), daemon=True,
                                 name="follows-videos").start()
            else:
                self._fetch_videos(key, site, vid)
        with self.lock:
            cached = self._videos.get(key)
            error = self._video_errors.get(key)
            loading = key in self._video_busy
        name = e.get("name") or vid
        face = home.thumb(e.get("face") or "", "f") if e.get("face") else ""
        answer = {"ok": True, "key": key, "name": name, "site": site, "loading": loading, "items": []}
        if cached is not None:
            answer["items"] = [self._item(key, site, name, face, v) for v in cached[1]]
        elif error is not None and not loading:
            answer["error"] = error[1]
        return answer

    def _fetch_videos(self, key: str, site: str, vid: str) -> None:
        client = self.relay.clients.get(site)
        try:
            videos = client.uploads(vid) if site == "bilibili" else client.creator(channel_url(vid))[1]
        except SourceError as error:
            message = "网站说这个 UP 主的投稿看不了" if error.permanent else \
                ("B 站" if site == "bilibili" else "YouTube") + "暂时连不上，过一会儿再试"
            with self.lock:
                self._video_errors[key] = (self.monotonic(), message)
                self._video_busy.discard(key)
            return
        except Exception as error:  # noqa: BLE001 —— 后台线程
            self.relay.log(f"取 {key} 的投稿出错：{type(error).__name__}")
            with self.lock:
                self._video_errors[key] = (self.monotonic(), "取不到，看日志")
                self._video_busy.discard(key)
            return
        with self.lock:
            self._videos[key] = (self.monotonic(), list(videos[:VIDEOS_MAX]))
            self._video_errors.pop(key, None)
            self._video_busy.discard(key)
            while len(self._videos) > 200:
                self._videos.pop(next(iter(self._videos)))

    def _item(self, key: str, site: str, name: str, face: str, v) -> dict:
        return {"id": f"cv:{key}:{v.bvid}", "kind": "video", "title": v.title, "subtitle": name, "source": site,
                "duration": v.duration, "pubdate": v.pubdate,
                "art": {"url": home._cover(self.relay, site, v.bvid), "ratio": "wide"},
                "creator": {"name": name, "face": face},
                "play": {"kind": "feed", "site": site, "id": v.bvid, "title": v.title}}


# ---------------------------------------------------------------- web.py 转过来的请求

def get(handler, relay, path: str) -> bool:
    from .accountfeeds import caller_of
    if path not in (PREFIX, PREFIX + "/videos"):
        return False
    caller = caller_of(handler, relay)
    if caller is None:
        handler._not_found()
        return True
    follows = relay.account_feeds.follows
    query = handler._query()
    if path == PREFIX:
        handler._json(follows.listing(caller, fresh=query.get("fresh") == "1"))
    else:
        handler._json(follows.videos(caller, query.get("key")))
    return True


def post(handler, relay, path: str) -> bool:
    from .accountfeeds import caller_of, read_body
    if path not in (PREFIX + "/sync", PREFIX + "/tv"):
        return False
    body = read_body(handler)
    caller = caller_of(handler, relay)
    if caller is None:
        handler._not_found()
        return True
    if body is None:
        handler._json({"ok": False, "error": "请求格式不对"}, 400)
        return True
    follows = relay.account_feeds.follows
    if path.endswith("/sync"):
        started = follows.kick(caller.user)
        answer = follows.listing(caller)
        if not started and not answer["syncing"]:
            answer["note"] = "刚同步过，过一分钟再点"
        handler._json(answer)
    else:
        answer = follows.set_tv(caller, body.get("key"), body.get("on"))
        if answer.get("ok"):
            relay.forget_home(caller)
        handler._json(answer)
    return True
