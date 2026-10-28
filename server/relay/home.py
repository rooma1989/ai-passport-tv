"""电脑版（媒体中心界面）的首页、「播放到」和遥控面板多出来的接口。

设计见 docs/superpowers/specs/2026-10-24-media-center-ui-design.md 第 4、5、8、11 节。原来的接口都还在，
这里只是把它们拼起来，页面画首页不用发十个请求：

- GET /api/home：首页的几排（大图 hero、继续观看、直播·正在播、UP 主更新、剧集追更、我的片库）。每一项都是
  同一种「节目」：{id, kind, title, subtitle, art, progress?, live?, source, play}。play 原样交给
  POST /api/play。按调用者算，HOME_CACHE_S 秒内同一个人再问直接给上次的；访客只有直播。
- GET /api/targets：「播放到」的每一行：调用者能遥控的每台小电视（在线、最后在线、型号、正在播什么、能做什么），
  加上「这台电脑」（只有家庭服务器、电脑客户端才有：云端没有在电脑上看）。
- POST /api/play {target, item, viewer}：一个入口，按目标和节目分到原来的换台、订阅播放、选集、投屏、
  重播和「在电脑上看」上。失败时 reason 是 offline / needs_login / youtube_off / no_device / bad。
- POST /api/sleep {device, minutes}：遥控面板的「定时关」，用语音命令那个定时（voicecmds.set_timer：到点调暗
  背光再暂停）；0 取消。剩下多少分钟在 /api/state 的 sleep 里。

电脑上看的进度（PcProgress）：设计的 Q4——电脑上看到哪儿另记，「继续观看」里显示，但从不往后推小电视的
剧集进度、不标看过。web.Relay 在内存里跟着每一路（_pc_cursor），这里把它存进数据目录的 pc-progress.json，
按用户分开，重启后电脑上接着看也从这里起。片库的视频（键 lib:<id>）例外：小电视上看的也记在这里（记录里有
device，小电视的名字），电脑上、小电视上都从这里接着放（library.tv_progress）。

B 站的封面、头像（CoverWarm）：图床地址只记在客户端内存里（bilibili.Bilibili.cover/face），是取投稿列表、
剧集时 B 站顺带给的；订阅的列表本身存在状态文件和共享缓存里，半小时内、没在小电视上放过的台不会重新去取。
所以重启以后「UP 主更新」「剧集追更」全是占位图。首页发现某个 B 站的台缺封面时，在后台一个一个重新取一次
它的列表（只为封面，列表照旧），同一个台 COVER_WARM_AGAIN_S 秒内不再取。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from pathlib import Path

from .. import channel_groups
from . import table, voicecmds
from .tenants import Caller, NotFound

HOME_CACHE_S = 4.0
ROW_MAX = 20
LIVE_MAX = 16
CONTINUE_MAX = 12
PER_CREATOR = 3
# 电脑上的进度：每个用户最多记这么多条；看了不到 MIN_S 秒、离结尾不到 END_S 秒的不算「看到一半」。
PROGRESS_MAX = 40
PROGRESS_MIN_S = 30
PROGRESS_END_S = 30
# 进度变化不到这么多秒就不重写文件。
PROGRESS_STEP_S = 5
# 每条记录最多记多少个「电脑上看完了」的集（选集里打 ✓ 的）。
DONE_MAX = 1000
PC_NAME = "这台电脑"
_TV_SITES = {"bilibili": "B 站", "youtube": "YouTube"}


def thumb(url: str, size: str = "v") -> str:
    """封面经本站的缩略图代理（bilisearch.py；B 站图床和 i.ytimg.com）。"""
    if not url:
        return ""
    return "api/bili/thumb?" + urllib.parse.urlencode({"s": size, "u": url})


def youtube_cover(video_id: str) -> str:
    return thumb(f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg")


def _cover(relay, site: str, key: str, size: str = "v") -> str:
    if site == "youtube":
        return youtube_cover(key.split("/", 1)[0]) if len(key.split("/", 1)[0]) == 11 else ""
    client = relay.clients.get("bilibili")
    getter = getattr(client, "cover", None)
    if getter is None:
        return ""
    try:
        url = getter(key) or (getter(key.split("/", 1)[0]) if "/" in key else "")
    except Exception:  # noqa: BLE001 —— 测试替身、旧客户端：没有封面就用占位图
        return ""
    return thumb(url, size) if isinstance(url, str) else ""


def _face(relay, site: str, source: str) -> str:
    if site != "bilibili":
        return ""
    getter = getattr(relay.clients.get("bilibili"), "face", None)
    try:
        url = getter(source) if getter is not None else ""
    except Exception:  # noqa: BLE001
        return ""
    return thumb(url, "f") if isinstance(url, str) else ""


# 缺封面时补取：两次之间隔几秒、同一个台多久内不再取、排队最多几个。
COVER_WARM_GAP_S = 1.0
COVER_WARM_AGAIN_S = 30 * 60
COVER_WARM_QUEUE = 64


class CoverWarm:
    """B 站的台缺封面（重启后内存里的图床地址没了）：后台重新取一次它的列表，B 站客户端顺带记下封面和头像。
    start：怎么开后台线程（测试换成先记下来、自己叫）。"""

    def __init__(self, relay, start=None, gap_s: float = COVER_WARM_GAP_S,
                 again_s: float = COVER_WARM_AGAIN_S, clock=time.monotonic, sleep=time.sleep):
        self.relay = relay
        self.start = start or (lambda work: threading.Thread(target=work, daemon=True, name="cover-warm").start())
        self.gap_s, self.again_s = gap_s, again_s
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self._asked: dict[tuple, float] = {}
        self._queue: list[tuple] = []
        self._running = False

    def want(self, kind: str, source: str) -> None:
        """kind 是 "up"（投稿列表）或 "series"（剧集）；source 是 UP 主的 mid、剧集的 ss…/BV 号。"""
        key = (kind, str(source))
        now = self.clock()
        with self.lock:
            at = self._asked.get(key)
            if (at is not None and now - at < self.again_s) or len(self._queue) >= COVER_WARM_QUEUE:
                return
            self._asked[key] = now
            if len(self._asked) > 1000:
                self._asked = {k: v for k, v in self._asked.items() if now - v < self.again_s}
            self._queue.append(key)
            if self._running:
                return
            self._running = True
        self.start(self._work)

    def pending(self) -> bool:
        with self.lock:
            return self._running or bool(self._queue)

    def _work(self) -> None:
        first = True
        while True:
            with self.lock:
                if not self._queue:
                    self._running = False
                    return
                kind, source = self._queue.pop(0)
            if not first:
                self.sleep(self.gap_s)
            first = False
            client = self.relay.clients.get("bilibili")
            try:
                if kind == "series":
                    client.series(source)
                else:
                    client.uploads(source)
            except Exception as error:  # noqa: BLE001 —— 后台线程：取不到下次再说，页面照旧用占位图
                self.relay.log(f"补取 {source} 的封面失败：{type(error).__name__}")


def _warm(relay, kind: str, site: str, source: str) -> None:
    if site != "bilibili" or not source:
        return
    warm = getattr(relay, "_cover_warm", None)
    if warm is None:
        warm = relay._cover_warm = CoverWarm(relay)
    warm.want(kind, source)


# ---------------------------------------------------------------- 电脑上的进度

class PcProgress:
    """{用户 id: {键: 记录}}，键是 UP 主 / 剧集的台 id（up01、sr03），投屏的视频是 v:<网站>:<id>。
    记录：channel、bvid（正在看的那一集 / 那个视频）、site、title、name（台名）、position、duration、index、
    total、series、at；投屏的多集视频（多 P、合集、番剧、YouTube 播放列表）再有 item（投的那个视频的 id）、
    part（第几集）、kind（"pages" 等）、list（YouTube 播放列表）。done 是电脑上看完了的各集的 bvid（选集里的 ✓）。
    片库的视频键是 lib:<条目 id>，小电视上看的再有 device（那台的名字）。"""

    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self._data: dict[str, dict] = {str(k): dict(v) for k, v in data.items()
                                       if isinstance(v, dict)} if isinstance(data, dict) else {}

    def get(self, user: int, key: str) -> dict | None:
        with self.lock:
            found = self._data.get(str(user), {}).get(key)
            return dict(found) if isinstance(found, dict) else None

    def rows(self, user: int) -> list[tuple[str, dict]]:
        with self.lock:
            mine = self._data.get(str(user), {})
            items = [(k, dict(v)) for k, v in mine.items() if isinstance(v, dict)]
        return sorted(items, key=lambda kv: -float(kv[1].get("at") or 0))

    def put(self, user: int, key: str, record: dict) -> None:
        with self.lock:
            mine = self._data.setdefault(str(user), {})
            old = mine.get(key)
            if isinstance(old, dict) and old.get("bvid") == record.get("bvid") and \
                    abs(float(old.get("position") or 0) - float(record.get("position") or 0)) < PROGRESS_STEP_S:
                return
            fresh = dict(record, at=self.clock())
            if isinstance(old, dict) and old.get("done") and "done" not in record:
                fresh["done"] = old["done"]
            mine[key] = fresh
            while len(mine) > PROGRESS_MAX:
                oldest = min(mine, key=lambda k: float(mine[k].get("at") or 0))
                mine.pop(oldest)
            self._save()

    def mark_done(self, user: int, key: str, bvid: str) -> None:
        """电脑上看完了 key 这一项里的 bvid（一集）：记进 done，选集里打 ✓。从不碰小电视的「看过」。"""
        with self.lock:
            mine = self._data.setdefault(str(user), {})
            record = mine.get(key)
            if not isinstance(record, dict):
                record = mine[key] = {"at": self.clock()}
            done = [b for b in record.get("done") or [] if isinstance(b, str) and b != bvid]
            record["done"] = (done + [bvid])[-DONE_MAX:]
            self._save()

    def done(self, user: int, key: str) -> set[str]:
        found = self.get(user, key)
        return {b for b in (found or {}).get("done") or [] if isinstance(b, str)}

    def forget(self, user: int, key: str) -> None:
        with self.lock:
            if self._data.get(str(user), {}).pop(key, None) is not None:
                self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


# ---------------------------------------------------------------- 播放到

def _last_seen(relay, caller: Caller) -> dict[str, float]:
    if not relay.tenant:
        return {}
    if caller.guest:
        row = relay.accounts.device(caller.device) if caller.device else None
        return {row["id"]: row.get("last_seen") or 0} if row else {}
    if caller.user is None:
        return {}
    return {row["id"]: row.get("last_seen") or 0 for row in relay.accounts.devices_of(caller.user)}


def _pc_allowed(relay, caller: Caller) -> bool:
    return relay.watch is not None and not caller.guest and caller.user is not None


def targets(relay, caller: Caller) -> dict:
    from ..tenancy.accounts import MODEL_NAMES
    seen = _last_seen(relay, caller)
    auto = relay.target(caller)["id"]
    rows = []
    for dev in relay.devices_for(caller):
        playing = relay._playing(dev)
        online = playing["online"]
        row = {"id": dev["id"], "kind": "tv", "name": dev["name"], "online": online,
               "last_seen": seen.get(dev["id"], 0), "auto": dev["id"] == auto,
               "model": dev.get("model") or "",
               "model_name": MODEL_NAMES.get(dev.get("model") or "", dev.get("model") or "小电视"),
               "mine": (not relay.tenant) or (not caller.guest and dev.get("owner") == caller.user),
               "now": None, "caps": {"switch": bool(playing["switch"])}}
        if online:
            now = relay.now(relay.groups(dev), dev)
            video = now.get("video") or {}
            status = playing.get("device") or {}
            row["now"] = {"channel": now["channel"], "name": now["name"], "group": now["group"], "n": now["n"],
                          "title": now["title"], "episode": now["episode"], "total": now["total"],
                          "paused": bool(video.get("paused"))}
            row["caps"].update(seek=bool(video), listen="audio_only" in status, pet="pet" in status,
                               dials=bool(status))
            row["listen"] = status.get("audio_only") == 1
        rows.append(row)
    if _pc_allowed(relay, caller):
        rows.append({"id": "pc", "kind": "pc", "name": PC_NAME, "online": True, "mine": True,
                     "caps": {"seek": True}})
    return {"ok": True, "targets": rows, "auto": auto or ("pc" if _pc_allowed(relay, caller) else "")}


def _reason(error: str) -> str:
    text = error or ""
    if "没在线" in text:
        return "offline"
    if "YouTube" in text and ("没配置" in text or "还没开启" in text or "没开" in text):
        return "youtube_off"
    if "会员" in text or "登录" in text:
        return "needs_login"
    if "还没有绑定" in text:
        return "no_device"
    return "failed"


def _bad() -> dict:
    return {"ok": False, "reason": "bad", "error": "请求格式不对"}


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pc_body(item: dict) -> dict | None:
    """节目 → 「在电脑上看」（Relay.watch_direct）的请求体。"""
    kind = item.get("kind")
    channel = item.get("channel")
    if kind in ("live", "channel") and isinstance(channel, str) and channel:
        return {"channel": channel}
    if kind in ("episode", "video") and isinstance(channel, str) and channel[:2] in ("up", "sr"):
        n = _int(item.get("episode") if kind == "episode" else item.get("index"))
        return {"channel": channel, "episode": n} if n else {"channel": channel}
    if kind == "cast" and isinstance(item.get("url"), str):
        return {"url": item["url"]}
    if kind == "history" and isinstance(item.get("site"), str) and isinstance(item.get("id"), str):
        return {"video": {"site": item["site"], "id": item["id"]}}
    if kind == "library" and isinstance(item.get("id"), str):
        # 我的片库（library.py）：按条目 id，浏览器直接放文件。
        return {"library": item["id"]}
    if kind == "feed":
        # 首页自己账号的几排、「我的关注」里的视频（accountfeeds.py）：按链接在电脑上看；观看记录从看到的地方起。
        from .accountfeeds import play_url, start_of
        url = play_url(item)
        if url is None:
            return None
        body = {"url": url}
        if start_of(item) is not None:
            body["start"] = start_of(item)
        return body
    return None


def play(relay, caller: Caller, body: dict) -> dict:
    item = body.get("item")
    target = body.get("target")
    if not isinstance(item, dict) or not isinstance(item.get("kind"), str) or \
            (target is not None and not isinstance(target, str)):
        return _bad()
    if target == "pc":
        if relay.watch is None:
            raise NotFound("watch")
        request = _pc_body(item)
        if request is None:
            return _bad()
        request["viewer"] = body.get("viewer")
        # 页面会放点播（vod.py）、要哪个清晰度、能不能放 DASH（ytvod.py）：原样带给 watch_direct（web.py 的 _prefer）。
        # 片库：caps 是页面能不能放 HEVC（library.plan）。
        request.update({k: body[k] for k in ("vod", "quality", "dash", "ytq", "caps") if k in body})
        device = body.get("device") if isinstance(body.get("device"), str) and body.get("device") else None
        answer = relay.watch_direct(caller, device, request)
        answer["target"] = "pc"
        if not answer.get("ok"):
            answer["reason"] = _reason(answer.get("error", ""))
        relay.forget_home(caller)
        return answer
    dev = relay.find_device(target, caller) if target else relay.target(caller)
    if dev is None:
        raise NotFound(target)
    if not dev["id"]:
        return {"ok": False, "reason": "no_device", "error": "还没有绑定小电视"}
    if not relay._playing(dev)["online"]:
        return {"ok": False, "reason": "offline", "error": f"{dev['name']}没在线", "target": dev["id"]}
    kind, cid = item["kind"], item.get("channel")
    note = ""
    if kind == "live" or (kind == "channel" and cid == table.CAST_ID):
        if not isinstance(cid, str):
            return _bad()
        answer = relay.switch(cid, caller, dev["id"])
    elif kind in ("channel", "video") and isinstance(cid, str) and cid[:2] in ("up", "sr"):
        answer = relay.play_subscription(cid, caller, dev["id"])
        index = _int(item.get("index"))
        if answer.get("ok") and kind == "video" and index:
            state = relay._account(caller)
            creator = state.get(cid)
            upcoming = state.peek_video(cid)
            if creator is not None and upcoming is not None and 1 <= index <= len(creator.videos) and \
                    creator.videos[index - 1].bvid != upcoming.bvid:
                note = f"小电视从「{creator.name}」下一个没看的视频播起"
    elif kind == "episode" and isinstance(cid, str) and cid[:2] == "sr":
        n = _int(item.get("episode"))
        if not n:
            return _bad()
        answer = relay.play_episode(cid, n, caller, dev["id"])
    elif kind == "cast" and isinstance(item.get("url"), str):
        answer = relay.cast(item["url"], caller, dev["id"])
        cid = table.CAST_ID
    elif kind == "history" and isinstance(item.get("site"), str) and isinstance(item.get("id"), str):
        answer = relay.replay(item["site"], item["id"], caller, dev["id"])
        cid = table.CAST_ID
    elif kind == "feed":
        from .accountfeeds import play_tv
        answer = play_tv(relay, caller, dev, item)
        cid = table.CAST_ID
    elif kind == "library":
        # 我的片库：放进小电视的投屏位，从上次看到的地方接着放（library.play_tv；访客不行）。
        from .library import play_tv as library_tv
        answer = library_tv(relay, caller, dev, item.get("id"))
        cid = table.CAST_ID
    else:
        return _bad()
    answer = dict(answer, target=dev["id"], target_name=dev["name"])
    if answer.get("ok"):
        answer.setdefault("channel", cid)
        if note:
            answer.setdefault("note", note)
    else:
        answer["reason"] = _reason(answer.get("error", ""))
    relay.forget_home(caller)
    return answer


def sleep(relay, caller: Caller, device: str | None, minutes) -> dict:
    """定时关：minutes 分钟后调暗再暂停（0 取消）。和换台一样作用在点名的那台（或遥控的那台）上。"""
    minutes = _int(minutes)
    if minutes is None or not 0 <= minutes <= voicecmds.TIMER_MAX_MIN:
        return {"ok": False, "error": f"定时最长 {voicecmds.TIMER_MAX_MIN // 60} 小时"}
    dev = relay._pick(caller, device)
    if not dev["id"]:
        return {"ok": False, "error": "还没有绑定小电视"}
    if minutes and not relay._playing(dev)["online"]:
        return {"ok": False, "error": f"{dev['name']}没在线"}
    body = {"action": "set", "minutes": minutes} if minutes else {"action": "cancel"}
    answer = voicecmds.set_timer(relay, dev["id"], caller, body)
    if answer.get("ok") and not minutes:
        answer["timer"] = None
    return dict(answer, device=dev["id"])


# ---------------------------------------------------------------- 首页

def _live_rows(relay, caller: Caller, dev: dict, account) -> list[dict]:
    if relay.main_channels is None:
        return []
    live = [(cid, name) for cid, name in channel_groups.read_table(relay.main_channels)]
    if not live:
        return []
    state = account if relay.tenant and not dev["id"] and account is not None else None
    place = {}
    for group in relay.groups(dev, state):
        for c in group["channels"]:
            place[c["id"]] = (group["name"], c["n"])
    guide = getattr(relay, "guide", None)
    now = relay.clock()
    epg = guide.lookup([cid for cid, _ in live][:400], now) if guide is not None and guide.enabled else {}
    logos = getattr(relay, "logos", None)
    known = logos.known() if logos is not None else set()
    # 真画面封面（linecheck.py 后台检测线路时取的）：有就用它当卡片的图，台标照旧叠在左下角。
    checker = getattr(relay, "linecheck", None)
    covers = checker.covers.stamps() if checker is not None and checker.covers is not None else {}
    playing = relay._playing(dev) if dev["id"] else {"online": False, "channel": ""}
    on_tv = playing["channel"] if playing["online"] else ""
    order = sorted(range(len(live)), key=lambda i: (live[i][0] != on_tv,
                                                    not (epg.get(live[i][0]) or {}).get("now"), i))
    rows = []
    for i in order[:LIVE_MAX]:
        cid, name = live[i]
        group, n = place.get(cid, ("", 0))
        guide_row = epg.get(cid) or {}
        current, upcoming = guide_row.get("now"), guide_row.get("next")
        item = {"id": f"live:{cid}", "kind": "live", "title": current["title"] if current else name,
                "subtitle": name, "source": "m3u", "play": {"kind": "live", "channel": cid},
                "channel": {"id": cid, "name": name, "group": group, "n": n,
                            "logo": f"api/art/logo/{cid}" if cid in known else ""},
                "art": {"url": f"api/art/live/{cid}?v={covers[cid]}" if cid in covers else "", "ratio": "wide"},
                "on_tv": cid == on_tv}
        if current or upcoming:
            item["live"] = {"title": current["title"] if current else "", "start": current["start"] if current else 0,
                            "end": current["end"] if current else 0,
                            "next_title": upcoming["title"] if upcoming else "",
                            "next_start": upcoming["start"] if upcoming else 0}
        rows.append(item)
    return rows


def _continue_rows(relay, caller: Caller, account) -> list[dict]:
    rows = []
    store = getattr(relay, "pc_progress", None)
    names = {}
    library = getattr(relay, "library", None) if not caller.guest else None
    shows: set[str] = set()
    with account.lock:
        creators = list(account.creators)
        for c in creators:
            names[c.id] = (c.name, c.kind, c.site, {v.bvid: n for n, v in enumerate(c.videos, 1)})
    if store is not None:
        for key, rec in store.rows(caller.user):
            position, duration = float(rec.get("position") or 0), float(rec.get("duration") or 0)
            if position < PROGRESS_MIN_S or (duration and position > duration - PROGRESS_END_S):
                continue
            site = rec.get("site") or "bilibili"
            if key.startswith("lib:"):
                # 片库的视频（library.py）：条目还在才显示；海报有就用。在电脑上、小电视上看的都记在这里
                # （device 是小电视的名字；没有时是电脑上看的）。一部剧只显示最近看的那一集。
                found = library.item(key[4:]) if library is not None else None
                if found is None:
                    continue
                episode = found["kind"] == "episode"
                if episode:
                    show = library.meta_key(found)
                    if show in shows:
                        continue
                    shows.add(show)
                shown = library.shown(found)
                rows.append({"id": f"pc:{key}", "kind": "video", "title": shown["name"],
                             "subtitle": found["title"] if episode else "片库", "source": "library",
                             "art": {"url": shown["art"], "ratio": "wide"},
                             "progress": {"position": int(position), "duration": int(duration),
                                          "device": rec.get("device") or PC_NAME, "updated_at": rec.get("at") or 0},
                             "play": {"kind": "library", "id": found["id"]}})
                continue
            if key.startswith("v:"):
                # 投到电脑的视频：键里是投的那个视频的 id（多 P、合集、番剧时 bvid 是正在看的那一集）。
                play = {"kind": "history", "site": site, "id": key.split(":", 2)[2]}
                subtitle = rec.get("name") or "投屏"
                if rec.get("part") and rec.get("total"):
                    n = int(rec["part"])
                    subtitle += f" · P{n}" if rec.get("kind") == "pages" else f" · 第 {n} 集"
            else:
                found = names.get(key)
                if found is None:
                    continue
                index = found[3].get(rec.get("bvid"))
                if not index:
                    continue
                series = found[1] == "series"
                play = {"kind": "episode" if series else "video", "channel": key,
                        ("episode" if series else "index"): index}
                subtitle = f"第 {index} 集" if series else found[0]
            title = rec.get("title") or ""
            if key.startswith("v:") and title.strip().isdigit() and rec.get("name"):
                # 番剧、电视剧的集名常常只是「1」：标题用剧名，下面写第几集。
                title, subtitle = rec["name"], f"第 {title.strip()} 集"
            rows.append({"id": f"pc:{key}", "kind": "episode" if key[:2] == "sr" else "video",
                         "title": title, "subtitle": subtitle, "source": site,
                         "art": {"url": _cover(relay, site, rec.get("bvid") or ""), "ratio": "wide"},
                         "progress": {"position": int(position), "duration": int(duration), "device": PC_NAME,
                                      "updated_at": rec.get("at") or 0},
                         "play": play})
    # 小电视那边看到一半的（存着进度的视频）：按主人的状态，设备名写「小电视」。
    with account.lock:
        for c in creators:
            for n, video in enumerate(c.videos, 1):
                position = c.positions.get(video.bvid)
                if position is None or position < PROGRESS_MIN_S:
                    continue
                if video.duration and position > video.duration - PROGRESS_END_S:
                    continue
                series = c.kind == "series"
                rows.append({"id": f"tv:{c.id}:{n}", "kind": "episode" if series else "video",
                             "title": video.title, "subtitle": f"{c.name} · 第 {n} 集" if series else c.name,
                             "source": c.site, "art": {"url": _cover(relay, c.site, video.bvid), "ratio": "wide"},
                             "progress": {"position": int(position), "duration": int(video.duration or 0),
                                          "device": "小电视", "updated_at": 0},
                             "play": {"kind": "episode", "channel": c.id, "episode": n} if series
                             else {"kind": "video", "channel": c.id, "index": n}})
    return rows[:CONTINUE_MAX]


def _creator_rows(relay, account) -> list[dict]:
    found = []
    with account.lock:
        for c in account.creators:
            if c.kind != "up":
                continue
            watched = set(c.watched)
            fresh = [(n, v) for n, v in enumerate(c.videos, 1) if v.bvid not in watched][:PER_CREATOR]
            for n, video in fresh:
                found.append((video.pubdate or 0, c.id, c.name, c.site, c.source, n, video))
    found.sort(key=lambda row: -row[0])
    rows = []
    for pubdate, cid, name, site, source, n, video in found[:ROW_MAX]:
        art, face = _cover(relay, site, video.bvid), _face(relay, site, source)
        if not art or not face:
            _warm(relay, "up", site, source)
        rows.append({"id": f"v:{cid}:{video.bvid}", "kind": "video", "title": video.title, "subtitle": name,
                     "source": site, "new": True, "duration": video.duration, "pubdate": pubdate,
                     "creator": {"id": cid, "name": name, "face": face},
                     "art": {"url": art, "ratio": "wide"},
                     "play": {"kind": "video", "channel": cid, "index": n}})
    return rows


def _series_rows(relay, account) -> list[dict]:
    rows = []
    with account.lock:
        series = [c for c in account.creators if c.kind == "series"]
        for c in series:
            total = len(c.videos)
            watched = len(set(c.watched) & {v.bvid for v in c.videos})
            done = total > 0 and watched >= total
            upcoming = account.episode(c.id)
            rows.append({"id": f"sr:{c.id}", "kind": "series", "title": c.name, "source": c.site,
                         "subtitle": (f"看完了 · 共 {total} 集" if done else
                                      f"看到第 {upcoming} 集 · 共 {total} 集" if watched else f"共 {total} 集"),
                         "series": {"id": c.id, "episode": upcoming, "total": total, "watched": watched,
                                    "done": done, "fresh": watched == 0},
                         "art": {"url": _cover(relay, c.site, c.source, "p"), "ratio": "poster"},
                         "play": {"kind": "episode", "channel": c.id, "episode": upcoming or 1}})
            if not rows[-1]["art"]["url"]:
                _warm(relay, "series", c.site, c.source)
    rows.sort(key=lambda r: (r["series"]["done"], r["series"]["fresh"]))
    return rows[:ROW_MAX]


def _hero(series: list[dict], live: list[dict], creators: list[dict]) -> list[dict]:
    hero = []
    going = next((s for s in series if s["series"]["watched"] and not s["series"]["done"]), None) or \
        next((s for s in series if not s["series"]["done"]), None)
    if going is not None:
        n = going["series"]["episode"] or 1
        hero.append(dict(going, kicker="剧集追更", button=f"{'继续' if going['series']['watched'] else '播放'}第 {n} 集"))
    on_air = next((x for x in live if x.get("on_tv")), None) or next((x for x in live if x.get("live")), None) or \
        (live[0] if live else None)
    if on_air is not None:
        hero.append(dict(on_air, kicker="直播 · 正在播", button="看直播"))
    if creators:
        hero.append(dict(creators[0], kicker="UP 主 · 新视频", button="播放"))
    return hero[:3]


def home(relay, caller: Caller) -> dict:
    cache = relay._home_cache
    now = time.monotonic()
    with relay._home_lock:
        hit = cache.get(caller.key)
        if hit is not None and now - hit[0] < HOME_CACHE_S:
            return hit[1]
    dev = relay.target(caller)
    try:
        account = relay._account(caller)
    except NotFound:
        account = None
    live = _live_rows(relay, caller, dev, account)
    answer = {"ok": True, "guest": account is None, "hero": [], "continue": [], "live_now": live,
              "creator_updates": [], "series_updates": [], "library_recent": [],
              "library": {"enabled": False}, "epg": bool(getattr(relay, "guide", None) and relay.guide.enabled)}
    if account is not None:
        if caller.user is not None:
            relay.pc_sync(caller.user)
        creators = _creator_rows(relay, account)
        series = _series_rows(relay, account)
        answer.update(**{"continue": _continue_rows(relay, caller, account) if caller.user is not None or
                         not relay.tenant else [],
                         "creator_updates": creators, "series_updates": series,
                         "hero": _hero(series, live, creators)})
        library = getattr(relay, "library", None)
        if library is not None and not caller.guest and caller.user is not None:
            # 我的片库（library.py）：最近加进来的；还没有来源时页面放一个「去添加」。
            from .library import home_rows
            answer["library_recent"] = home_rows(relay, caller)
            answer["library"] = {"enabled": True, "count": len(library.items()), "admin": caller.role == "admin"}
        warm = getattr(relay, "_cover_warm", None)
        if warm is not None and warm.pending():
            # 正在后台补封面：页面过几秒再问一次（console.js 的 loadHome）。
            answer["covers_pending"] = True
    else:
        answer["hero"] = _hero([], live, [])[:1]
    with relay._home_lock:
        cache[caller.key] = (now, answer)
        if len(cache) > 200:
            for key in [k for k, (at, _) in cache.items() if now - at >= HOME_CACHE_S]:
                cache.pop(key, None)
    return answer
