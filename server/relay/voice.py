"""给电视服务的语音功能用的简短目录：GET /api/voice/catalog?device=…

语音命令必须作用在说话的那台设备上，而不是手机正在遥控的那台（Relay.target()）。所以电视服务
调中转时总是带 device（server/voice/relay_client.py）：
- 单用户（没有 --db）：device 是中转里的 id（d0、d1…）或名字（--device-name、--extra-device 的
  名字），即电视服务的 TV_RELAY_DEVICE；
- 多用户（--db）：device 是设备 id（legacy-xingzhi、legacy-passport 或 12 位 MAC），由电视服务在
  认出设备后填上；带密钥的这类请求按那台设备的访客处理（web.py 的 _DEVICE_SCOPED），只能动那一台，
  剧集和进度按设备主人的来。

目录内容：剧集（名字、网站、集数、下一次播第几集）、UP 主（全名、网站），和这台设备正在播的台、
第几集、是否暂停。网站（bilibili / youtube）用来回答「有哪些 YouTube 频道」。
设计见 docs/superpowers/specs/2026-10-12-voice-control-design.md 第 3 节。

语音搜索（「搜视频猫和老鼠」「油管搜频道 lofi girl」）另有三个接口，同样总是带 device：
- GET  /api/voice/search?device=…&site=bilibili|youtube&kind=video|up&q=…&wait=秒
  搜一页视频或 UP 主（频道）；B 站走不带签名的搜索接口，YouTube 经节点的 /yt/search。wait 是这次
  最多等多久（设备只等语音结果 8 秒，识别已经用掉一些），超时回答 ok:false 和一句给设备看的话。
  同一个词几分钟内直接用上次的结果。没说网站的 UP 主搜索由媒体服务同时发两个请求（每个网站一个），
  这里不用管（server/voice/session.py 的 _search_sites）。
- POST /api/voice/cast {device, site, id, title, duration}  投屏一个搜到的视频：和手机投屏同一条路
  （设备切到「我的投屏」，播完回到原来的台）。
- POST /api/voice/creator {device, site, id, name, action: follow|latest, wait}
  follow：像手机「添加 UP 主」一样订阅成一个台（记在设备主人名下：这是设备访客唯一能改的账号数据，
  用户决定的）；latest：投屏他最新的一个投稿。两样都要问网站（YouTube 列投稿要十几秒），wait 秒内
  没做完就先回答 pending，在后台做完（订阅后设备的频道表自己会更新，投屏照常切台）。
"""

from __future__ import annotations

import re
import threading
import weakref

from . import table
from .bilibili import SourceError
from .state import MAX_CREATORS, Video
from .tenants import LEGACY, Caller
from .youtube import channel_url

SITES = ("bilibili", "youtube")
KINDS = ("video", "up")
QUERY_MAX = 40
TITLE_MAX = 200
NAME_MAX = 40
# 没给 wait 时等多久；给了也夹在这两个数之间。
WAIT_DEFAULT_S = 4.5
WAIT_MIN_S, WAIT_MAX_S = 1.0, 6.0
# 搜索结果在中转里留多久、最多留几条。
SEARCH_CACHE_S = 5 * 60
SEARCH_CACHE_MAX = 64
_IDS = {"bilibili": re.compile(r"^BV[0-9A-Za-z]{10}$"), "youtube": re.compile(r"^[A-Za-z0-9_-]{11}$")}
_CREATOR_IDS = {"bilibili": re.compile(r"^\d{1,20}$"), "youtube": re.compile(r"^UC[0-9A-Za-z_-]{22}$")}
# 给设备看的话：面板一行约 11 个汉字，第二行放得下更长的。
REPLY_SLOW = {"bilibili": "B站搜索太慢了，稍后再试", "youtube": "YouTube 搜索太慢了，稍后再试"}
REPLY_DOWN = {"bilibili": "B站没回应，稍后再试", "youtube": "YouTube 没回应，稍后再试"}
REPLY_BUSY = "YouTube 正忙，稍后再试"
REPLY_NO_YOUTUBE = "YouTube 还没配置好"
REPLY_FORMAT = "请求格式不对"
REPLY_NO_DEVICE = "没有这台设备"

# 每个中转一份搜索缓存：(网站, 种类, 词) → (时刻, 结果)。
_caches: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_cache_lock = threading.Lock()


def catalog(relay, key, caller: Caller = LEGACY) -> dict:
    device = relay.find_device(key, caller)
    if device is None:
        return {"ok": False, "error": "没有这台设备"}
    state = relay._state_of(device)
    playing = relay._playing(device)
    cid = playing["channel"]
    with state.lock:
        series = [{"id": c.id, "name": c.name, "site": c.site, "total": len(c.videos),
                   "next": state.episode(c.id)} for c in state.creators if c.kind == "series"]
        creators = [{"id": c.id, "name": c.name, "site": c.site}
                    for c in state.creators if c.kind != "series"]
    episode = 0
    paused = False
    if playing["online"] and cid[:2] == "sr":
        episode = relay._episode_of(state, cid, relay._on_air(device, cid))
    if playing["online"]:
        paused = relay._paused_on(device, cid) is not None
    return {"ok": True, "device": {"id": device["id"], "name": device["name"], "online": playing["online"]},
            "series": series, "creators": creators,
            "playing": {"channel": cid, "episode": episode, "paused": paused}}


def _wait(value) -> float:
    try:
        wait = float(value) if value not in (None, "") else WAIT_DEFAULT_S
    except (TypeError, ValueError):
        wait = WAIT_DEFAULT_S
    if wait != wait:        # NaN
        wait = WAIT_DEFAULT_S
    return max(WAIT_MIN_S, min(WAIT_MAX_S, wait))


def _client(relay, site):
    """(客户端, None) 或 (None, 给设备看的话)。"""
    if site not in SITES:
        return None, REPLY_FORMAT
    client = relay.clients.get(site)
    if client is None:
        return None, REPLY_NO_YOUTUBE if site == "youtube" else REPLY_DOWN[site]
    return client, None


def _cached(relay, key: tuple) -> list | None:
    with _cache_lock:
        cache = _caches.get(relay) or {}
        hit = cache.get(key)
        return hit[1] if hit and relay.clock() - hit[0] < SEARCH_CACHE_S else None


def _remember(relay, key: tuple, hits: list) -> None:
    now = relay.clock()
    with _cache_lock:
        cache = {k: v for k, v in (_caches.get(relay) or {}).items() if now - v[0] < SEARCH_CACHE_S}
        while len(cache) >= SEARCH_CACHE_MAX:
            cache.pop(min(cache, key=lambda k: cache[k][0]))
        cache[key] = (now, hits)
        _caches[relay] = cache


def _failure(relay, site: str, what: str, error: SourceError) -> dict:
    relay.log(f"语音{what}失败（{site}）：{error}")
    reason = getattr(error, "reason", "")
    reply = (REPLY_SLOW[site] if reason == "slow" else REPLY_BUSY if reason == "busy" and site == "youtube"
             else REPLY_DOWN[site])
    return {"ok": False, "error": reply, "reason": reason or "down"}


def search(relay, key, caller: Caller = LEGACY, site=None, query=None, wait=None, kind="video") -> dict:
    """搜一页：{"ok": true, "site", "kind", "results": [...]}。

    视频：{"id", "title", "uploader", "duration"}；UP 主：{"id", "name", "fans", "videos", "latest"}
    （latest 只有 B 站给：搜索结果里顺带的最新一个能投屏的投稿，或 None）。"""
    if relay.find_device(key, caller) is None:
        return {"ok": False, "error": REPLY_NO_DEVICE}
    query = " ".join(str(query or "").split())[:QUERY_MAX]
    if kind not in KINDS or not query:
        return {"ok": False, "error": REPLY_FORMAT}
    client, refusal = _client(relay, site)
    if client is None:
        return {"ok": False, "error": refusal}
    cache_key = (site, kind, query.lower())
    hits = _cached(relay, cache_key)
    if hits is None:
        try:
            find = client.search if kind == "video" else client.search_creators
            hits = find(query, timeout=_wait(wait))
        except SourceError as error:
            return _failure(relay, site, "搜索", error)
        _remember(relay, cache_key, hits)
    return {"ok": True, "site": site, "kind": kind, "results": hits}


def cast(relay, key, caller: Caller = LEGACY, body: dict | None = None) -> dict:
    """投屏一个搜到的视频（按网站和 id），作用在点名的设备上。"""
    body = body or {}
    device = relay.find_device(key, caller)
    if device is None:
        return {"ok": False, "error": REPLY_NO_DEVICE}
    site, video_id = body.get("site"), body.get("id")
    title, duration = body.get("title"), body.get("duration")
    client, refusal = _client(relay, site)
    if client is None:
        return {"ok": False, "error": refusal}
    if not isinstance(video_id, str) or not _IDS[site].match(video_id):
        return {"ok": False, "error": REPLY_FORMAT}
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
        duration = 0
    title = " ".join(title.split())[:TITLE_MAX] if isinstance(title, str) else ""
    if title:
        # 搜索结果里已经有标题和时长：不必再问网站，投屏马上开始。
        video = Video(bvid=video_id, title=title, duration=duration, pubdate=0, site=site)
    else:
        try:
            found = client.video(video_id)
        except SourceError as error:
            relay.log(f"语音投屏取不到 {video_id}：{error}")
            return {"ok": False, "error": "这个视频放不了" if error.permanent else REPLY_DOWN[site]}
        video = Video(bvid=video_id, title=found.title, duration=found.duration, pubdate=0, site=site)
    return relay._cast_video(video, caller, device)


def _within(relay, site: str, job, wait: float) -> dict:
    """在后台做 job，最多等 wait 秒；没做完回答 {"ok": true, "pending": true}，做完的结果只记日志。"""
    box: dict = {}

    def run():
        try:
            answer = job()
        except SourceError as error:
            answer = _failure(relay, site, "订阅或播放", error)
        except Exception as error:      # 后台线程出错不能悄悄没了
            relay.log(f"语音订阅或播放出错：{type(error).__name__}: {error}")
            answer = {"ok": False, "error": "没做成，稍后再试"}
        box["answer"] = answer
        if late.is_set() and not answer.get("ok"):
            relay.log(f"语音订阅或播放（后台）没做成：{answer.get('error')}")

    late = threading.Event()
    worker = threading.Thread(target=run, name="voice-creator", daemon=True)
    worker.start()
    worker.join(wait)
    if "answer" in box:
        return box["answer"]
    late.set()
    return {"ok": True, "pending": True}


def creator(relay, key, caller: Caller = LEGACY, body: dict | None = None) -> dict:
    """语音搜到的 UP 主（频道）：follow 订阅成台，latest 投屏最新投稿。"""
    body = body or {}
    device = relay.find_device(key, caller)
    if device is None:
        return {"ok": False, "error": REPLY_NO_DEVICE}
    site, cid, action = body.get("site"), body.get("id"), body.get("action")
    client, refusal = _client(relay, site)
    if client is None:
        return {"ok": False, "error": refusal}
    if not isinstance(cid, str) or not _CREATOR_IDS[site].match(cid) or action not in ("follow", "latest"):
        return {"ok": False, "error": REPLY_FORMAT}
    name = body.get("name")
    name = " ".join(name.split())[:NAME_MAX] if isinstance(name, str) else ""
    source = cid if site == "bilibili" else channel_url(cid)
    if action == "latest":
        return _within(relay, site, lambda: _latest(relay, device, caller, client, site, source),
                       _wait(body.get("wait")))
    if relay.tenant and device.get("owner") is None:
        return {"ok": False, "error": "这台电视还没绑定账号，不能订阅"}
    state = relay._state_of(device)
    refusal = follow_refusal(relay, state, site, source, name, cid)
    if refusal:
        return {"ok": False, "error": refusal}
    return _within(relay, site, lambda: _follow(relay, state, client, site, source),
                   _wait(body.get("wait")))


def follow_refusal(relay, state, site: str, source: str, name: str, fallback: str = "") -> str:
    """语音订阅（搜到的 UP 主，或正在放的投屏视频的作者，见 voicecmds.py）不能做时的话；能做为空。
    和手机「添加 UP 主」一样：最多 MAX_CREATORS 个，设备的频道列表还得放得进 CONFIG。"""
    with state.lock:
        ups = [c for c in state.creators if c.kind == "up"]
        if any(c.site == site and c.source == source for c in ups):
            return f"已经订阅过「{name}」了" if name else "已经订阅过了"
        if len(ups) >= MAX_CREATORS:
            return f"最多只能订阅 {MAX_CREATORS} 个 UP 主"
    if not relay.room_of(state, table.label_for(name, site, fallback or source))["fits"]:
        return "频道列表满了，先删掉几个台"
    return ""


def _follow(relay, state, client, site: str, source: str) -> dict:
    name, videos = client.creator(source)
    try:
        added = state.add(site, source, name, videos, relay.clock())
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    state.save()
    relay._cache_videos(added, added.videos, added.fetched_at)
    relay._tables_changed(state)
    relay.log(f"语音订阅 UP 主 {added.id} {name}（{site} {source}，{len(videos)} 个投稿）")
    return {"ok": True, "creator": {"id": added.id, "name": name}}


def _latest(relay, device, caller, client, site: str, source: str) -> dict:
    videos = client.uploads(source)        # 两个网站都是新的在前
    if not videos:
        return {"ok": False, "error": "这个 UP 主还没有能放的视频"}
    newest = videos[0]
    return relay._cast_video(Video(bvid=newest.bvid, title=newest.title, duration=newest.duration,
                                   pubdate=newest.pubdate, site=site), caller, device)


# ---- 语音跳转（Relay.seek 的 fraction、from_end、within） ----

def duration_words(seconds: float) -> str:
    """「23 分 40 秒」「1 小时 5 分」「40 秒」。"""
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    parts = ([f"{hours} 小时"] if hours else []) + ([f"{minutes} 分"] if minutes else []) + \
        ([f"{secs} 秒"] if secs or not (hours or minutes) else [])
    return " ".join(parts)


def seek_target(duration: int, position=None, fraction=None, from_end=None) -> tuple[float, str]:
    """(要跳到的秒, 拒绝的话)：按比例和到结尾要知道时长（总按正在播的视频算，不会过时）；
    语音说的时间（position）超过结尾时不跳，回答这个视频有多长。"""
    if fraction is not None or from_end is not None:
        if not duration:
            return 0.0, "不知道这个视频有多长，没法按比例跳"
        if fraction is not None:
            # 「跳到 99%」也停在结尾前，不让它变成下一个。
            return min(max(0.0, float(fraction)) * duration, max(0.0, duration - 10.0)), ""
        return max(0.0, duration - max(0.0, float(from_end))), ""
    position = max(0.0, float(position or 0))
    if duration and position >= duration - 3:
        return 0.0, f"这个视频只有 {duration_words(duration)}"
    return position, ""
