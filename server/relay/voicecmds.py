"""语音的第二批命令在中转这边要记的东西和要做的事：每台设备一份「语音记忆」。

- 换台历史：最近看过的台（「回到刚才的台」「换回去」）。后台每 TICK_S 秒看一次每台设备的
  「正在播放」，台变了就记下；语音目录（GET /api/voice/catalog）被问到时也看一次。只看了不到
  DWELL_S 秒的台（▲▼ 一路翻过去的）不算「刚才的台」。
- 定时（「30 分钟后暂停」「看完这集就停」）：每台设备至多一个，新的替换旧的；后台到点执行
  （sleep()）：先把背光调到最暗一档（brightness 事件），再暂停（pause 事件，value=1 表示「定时
  暂停」：电视服务不在 30 分钟后自动继续，最长等 SLEEP_PAUSE_MAX_S）。中转推流的台（UP 主、剧集、
  投屏）另外和手机的暂停一样记下位置、掐断流；直播台只靠电视服务停在暂停画面上（关掉直播源，
  只补静音）。说「继续」（语音或手机）、换台、或设备重连（新会话）就醒了，背光恢复到定时前那一档。
- 投屏历史：投到这台设备的视频（新的在前，最多 CAST_HISTORY_MAX 个），换下来时记下播到哪：
  「重播刚才那个视频」「播放上一个投屏」「继续看刚才投屏的」。

存在哪：单用户（没有 --db）在状态文件 relay-state.json 的 "voice" 里，按设备 id（d0、d1…）分开；
多用户在数据库这台设备的 device_state 里（和投屏位一起，解绑时一起删掉）。

接口（总是带 device，多用户时按那台设备的访客处理，见 web.py 的 _DEVICE_SCOPED）：
- POST /api/voice/timer {device, action: set|end|cancel|status, minutes}
- POST /api/voice/subscribe {device, wait}     订阅正在放的投屏视频的 UP 主（YouTube 是频道）
- POST /api/voice/unsubscribe {device, id}     删掉设备主人的一个 UP 主或剧集台（设备上先确认过）
- POST /api/voice/recast {device, step, resume} 从这台设备的投屏历史再投一次
电脑版遥控面板的「定时关」用同一个定时：POST /api/sleep {device, minutes}（home.sleep → set_timer），
/api/state 的 sleep 是 timer_of() 给的还剩多少。
"""

from __future__ import annotations

import math
import threading
import time

from . import table, voice
from .bilibili import SourceError
from .state import Video
from .tenants import LEGACY, Caller

# 后台多久看一次设备（换台历史、定时）。
TICK_S = 2.0
# 换台历史留几个台；看了不到这么久的台不算「刚才的台」。
HISTORY_MAX = 8
DWELL_S = 10.0
CAST_HISTORY_MAX = 10
# 定时最长多久（分钟）。
TIMER_MAX_MIN = 12 * 60
# 「看完这集就停」：离结尾还有这么多秒就停（后台每 TICK_S 秒看一次，停在最后几秒里）。
END_MARGIN_S = 3.0
# 中转停了很久才起来：过了时刻这么久的定时不再执行（用户多半又在看了）。
LATE_MAX_S = 10 * 60
# 定时暂停最长多久（与电视服务的 SLEEP_PAUSE_MAX_S 一致）；手机的暂停是 30 分钟后自动继续。
SLEEP_PAUSE_MAX_S = 12 * 3600
REPLY_NO_DEVICE = "没有这台设备"


def _now(relay) -> float:
    return relay.clock()


# ---- 存储 ----

def _holder(relay, dev: dict):
    """(记忆所在的对象, 这台设备的记忆 dict)。对象有 lock 和 save()。"""
    if relay.tenant:
        slot = relay._cast_of(dev)
        return slot, slot.voice
    state = relay.state
    with state.lock:
        return state, state.voice.setdefault(dev["id"] or "-", {})


class _Memo:
    """with _Memo(relay, dev) as rec: 改 rec；changed=True 时退出时存下来。"""

    def __init__(self, relay, dev: dict):
        self.holder, self.rec = _holder(relay, dev)
        self.changed = False

    def __enter__(self) -> dict:
        self.holder.lock.acquire()
        return self.rec

    def __exit__(self, kind, _value, _tb):
        try:
            if self.changed and kind is None:
                self.holder.save()
        finally:
            self.holder.lock.release()
        return False


def _view(relay, dev: dict) -> dict:
    """记忆的一份拷贝（只读用）。"""
    holder, rec = _holder(relay, dev)
    with holder.lock:
        return {"channels": [dict(c) for c in rec.get("channels") or []],
                "timer": dict(rec["timer"]) if isinstance(rec.get("timer"), dict) else None,
                "asleep": dict(rec["asleep"]) if isinstance(rec.get("asleep"), dict) else None,
                "casts": [dict(c) for c in rec.get("casts") or []]}


# ---- 换台历史 ----

def observe(relay, dev: dict, playing: dict | None = None) -> None:
    """看一眼设备在哪个台：变了就记进历史（新的在前）。不在线时什么都不记。"""
    playing = playing if playing is not None else relay._playing(dev)
    cid = playing.get("channel") or ""
    if not playing.get("online") or not cid:
        return
    memo = _Memo(relay, dev)
    with memo as rec:
        history = [c for c in rec.get("channels") or [] if isinstance(c, dict) and c.get("id")]
        if history and history[0]["id"] == cid:
            return
        now = _now(relay)
        if history and history[0].get("to") is None:
            history[0]["to"] = now
        history = [{"id": cid, "from": now, "to": None}] + [c for c in history if c["id"] != cid]
        rec["channels"] = history[:HISTORY_MAX]
        memo.changed = True


def back_channels(rec: dict, current: str) -> list[str]:
    """刚才看过的台（新的在前，不含 current），看了不到 DWELL_S 秒的不算。"""
    out = []
    for entry in rec.get("channels") or []:
        cid = entry.get("id")
        if not cid or cid == current or cid in out:
            continue
        start, end = entry.get("from") or 0.0, entry.get("to")
        if end is not None and end - start < DWELL_S:
            continue
        out.append(cid)
    return out


# ---- 目录（GET /api/voice/catalog 的补充） ----

def catalog(relay, key, caller: Caller = LEGACY) -> dict:
    """voice.catalog 加上：正在放的视频（标题、进度、时长、共几集）、刚才的台、定时、投屏历史、
    每部剧看到第几集的哪里。"""
    answer = voice.catalog(relay, key, caller)
    if not answer.get("ok"):
        return answer
    dev = relay.find_device(key, caller)
    if dev is None:
        return answer
    playing = relay._playing(dev)
    observe(relay, dev, playing)
    cid = playing["channel"]
    state = relay._state_of(dev)
    rec = _view(relay, dev)
    with state.lock:
        positions = {}
        for c in state.creators:
            if c.kind != "series":
                continue
            n = state.episode(c.id)
            video = c.videos[n - 1] if 1 <= n <= len(c.videos) else None
            positions[c.id] = int(c.positions.get(video.bvid, 0)) if video is not None else 0
    for row in answer["series"]:
        row["position"] = positions.get(row["id"], 0)
    now = _now_playing(relay, dev, playing, state)
    if now:
        answer["playing"].update(now)
    asleep = rec["asleep"]
    if asleep is not None and asleep.get("channel") == cid and playing["online"]:
        answer["playing"]["paused"] = True
    answer["back"] = back_channels(rec, cid)
    answer["timer"] = _timer_view(relay, rec["timer"])
    answer["casts"] = [c.get("title") or "" for c in _casts(relay, dev, rec)[:3]]
    return answer


def _now_playing(relay, dev: dict, playing: dict, state) -> dict:
    """正在放的中转视频：{"title", "position", "duration", "total"}；直播台、不在线为空。"""
    cid = playing["channel"]
    if not playing["online"] or not (cid == table.CAST_ID or cid[:2] in ("up", "sr")):
        return {}
    if cid == table.CAST_ID:
        slot = relay._cast_of(dev)
        with slot.lock:
            video = slot.cast
    else:
        video = relay._on_air(dev, cid)
    if video is None:
        return {}
    out = {"title": video.title, "duration": int(video.duration or 0)}
    progress = relay.video_progress(dev, cid)
    if progress is not None:
        out["position"] = int(progress["position"])
        out["duration"] = int(progress["duration"] or out["duration"])
    creator = state.get(cid) if cid[:2] == "sr" else None
    if creator is not None:
        with state.lock:
            out["total"] = len(creator.videos)
    return out


def _timer_view(relay, timer: dict | None) -> dict | None:
    if not timer:
        return None
    if timer.get("kind") == "end":
        return {"kind": "end", "what": timer.get("what") or "video"}
    return {"kind": "time", "left": max(0, int(math.ceil(float(timer.get("at") or 0) - _now(relay))))}


def timer_of(relay, dev: dict) -> dict | None:
    """这台设备现在的定时（给 /api/state 的 sleep：电脑版遥控面板的「定时关」显示还剩几分钟）。"""
    return _timer_view(relay, _view(relay, dev)["timer"])


# ---- 定时 ----

def set_timer(relay, key, caller: Caller = LEGACY, body: dict | None = None) -> dict:
    """POST /api/voice/timer：{action: set（minutes）| end（看完这个视频）| cancel | status}。
    回答 {"ok": true, "timer": 现在的（cancel 时是取消掉的）或 null}。"""
    body = body or {}
    dev = relay.find_device(key, caller)
    if dev is None:
        return {"ok": False, "error": REPLY_NO_DEVICE}
    action = body.get("action")
    memo = _Memo(relay, dev)
    if action == "status":
        return {"ok": True, "timer": _timer_view(relay, _view(relay, dev)["timer"])}
    if action == "cancel":
        with memo as rec:
            old = rec.pop("timer", None)
            memo.changed = old is not None
        if old is not None:
            relay.log(f"{dev['name']} 定时取消")
        return {"ok": True, "timer": _timer_view(relay, old)}
    if action == "set":
        minutes = body.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= TIMER_MAX_MIN:
            return {"ok": False, "error": f"定时最长 {TIMER_MAX_MIN // 60} 小时"}
        timer = {"kind": "time", "at": _now(relay) + minutes * 60, "set": _now(relay)}
    elif action == "end":
        playing = relay._playing(dev)
        cid = playing["channel"]
        if not playing["online"]:
            return {"ok": False, "error": f"{dev['name']}没在线"}
        if not (cid == table.CAST_ID or cid[:2] in ("up", "sr")):
            return {"ok": False, "error": "直播台没有「这集」，说「30分钟后暂停」吧"}
        video = relay._cast_of(dev).cast if cid == table.CAST_ID else relay._on_air(dev, cid)
        if video is None or not video.duration:
            return {"ok": False, "error": "不知道这个视频有多长，说「30分钟后暂停」吧"}
        timer = {"kind": "end", "channel": cid, "video": video.bvid, "duration": int(video.duration),
                 "what": "episode" if cid[:2] == "sr" else "video", "set": _now(relay)}
    else:
        return {"ok": False, "error": "请求格式不对"}
    with memo as rec:
        rec["timer"] = timer
        memo.changed = True
    relay.log(f"{dev['name']} 定时：" + (f"{body.get('minutes')} 分钟后暂停" if timer["kind"] == "time"
                                       else f"{timer['channel']} 这个视频放完就暂停"))
    return {"ok": True, "timer": _timer_view(relay, timer)}


def _due(relay, dev: dict, timer: dict, playing: dict) -> str:
    """定时现在该不该执行："fire"、"wait"，或 "drop"（作废：设备不在线、换走了、中转停太久）。"""
    now = _now(relay)
    if timer.get("kind") != "end":
        at = float(timer.get("at") or 0)
        if now < at:
            return "wait"
        if now - at > LATE_MAX_S or not playing["online"]:
            return "drop"
        return "fire"
    if not playing["online"]:
        # 设备一时断开（换片源、重连）：等它回来；断太久就算了。
        return "drop" if now - float(timer.get("set") or now) > 6 * 3600 else "wait"
    cid = playing["channel"]
    if cid != timer.get("channel"):
        # 投屏播完自己回到原来的台：那正是「看完了」。手动换了台就不算了。
        return "fire" if timer.get("channel") == table.CAST_ID else "drop"
    _, video, position, _ = relay._current(dev)
    if video is None:
        return "wait"           # 暂停着、或新流还没起来
    if video.bvid != timer.get("video"):
        return "fire"           # 下一集已经开始了
    duration = float(video.duration or timer.get("duration") or 0)
    return "fire" if duration and position >= duration - END_MARGIN_S else "wait"


def sleep(relay, dev: dict, playing: dict | None = None) -> None:
    """定时到了：背光调到最暗，暂停（中转推流的台连流一起停），记下「睡着了」。"""
    playing = playing if playing is not None else relay._playing(dev)
    cid = playing["channel"]
    status = playing.get("device")
    before = None
    if status is not None and status.get("steps"):
        dim = int(status["steps"]) - 1
        if status.get("brightness") != dim:
            before = status.get("brightness")
            # 先调暗再暂停：电视服务暂停后要到继续时才发亮度。
            relay._event(dev, "brightness", value=dim)
    done = False
    if cid == table.CAST_ID or cid[:2] in ("up", "sr"):
        paused = relay._paused_on(dev, cid)
        if paused is not None:
            # 手机已经暂停了：改成定时暂停（不自动继续），电视服务那边也再说一次。
            paused["sleep"] = True
            relay._event(dev, "pause", value=1, text=paused["video"].title)
            done = True
        else:
            # 和手机的暂停一样：记下位置、掐断流、写 pause 事件（value=1）。
            done = bool(relay.pause_device(dev, sleep=True).get("ok"))
    if not done:
        # 直播台（或中转台的流还没起来）：只让电视服务停在暂停画面上，关掉直播源。
        names = dict(relay.channel_rows(dev))
        relay._event(dev, "pause", value=1, text=names.get(cid, ""))
    memo = _Memo(relay, dev)
    with memo as rec:
        rec.pop("timer", None)
        rec["asleep"] = {"channel": cid, "at": _now(relay), "brightness": before}
        memo.changed = True
    relay.log(f"{dev['name']} 定时到了：{cid} 暂停，背光调暗")


def wake(relay, dev: dict, restore: bool = True) -> bool:
    """醒来（说了继续、换了台、重连了）：清掉「睡着了」，背光恢复到定时前那一档。睡着过返回 True。"""
    memo = _Memo(relay, dev)
    with memo as rec:
        asleep = rec.pop("asleep", None)
        memo.changed = asleep is not None
    if not isinstance(asleep, dict):
        return False
    before = asleep.get("brightness")
    if restore and isinstance(before, int):
        relay._event(dev, "brightness", value=before)
    return True


def _asleep_over(relay, asleep: dict, playing: dict) -> bool:
    """定时暂停已经不在了：设备换了台，或开始了新会话（重连、重启）。"""
    if not playing["online"]:
        return False
    return playing["channel"] != asleep.get("channel") or playing.get("since", 0) > float(asleep.get("at") or 0) + 1


def tick(relay) -> None:
    """后台每 TICK_S 秒一次：记换台、执行到点的定时、发现醒来了。"""
    for dev in list(relay.devices):
        if not dev.get("id"):
            continue
        playing = relay._playing(dev)
        observe(relay, dev, playing)
        rec = _view(relay, dev)
        if rec["asleep"] is not None and _asleep_over(relay, rec["asleep"], playing):
            wake(relay, dev)
            relay.log(f"{dev['name']} 定时暂停结束（换台或重连）")
        timer = rec["timer"]
        if timer is None:
            continue
        verdict = _due(relay, dev, timer, playing)
        if verdict == "fire":
            sleep(relay, dev, playing)
        elif verdict == "drop":
            memo = _Memo(relay, dev)
            with memo as current:
                if current.get("timer") == timer:
                    current.pop("timer")
                    memo.changed = True
            relay.log(f"{dev['name']} 定时作废（不在线、换了台或过时太久）")


def start(relay, interval: float = TICK_S) -> threading.Thread:
    def run() -> None:
        while True:
            time.sleep(interval)
            try:
                tick(relay)
            except Exception as error:      # 后台线程不能死：记下来，下一轮再试
                relay.log(f"语音后台出错：{type(error).__name__}: {error}")

    worker = threading.Thread(target=run, name="voice-tick", daemon=True)
    worker.start()
    return worker


# ---- 投屏历史 ----

def _entry(video) -> dict:
    return {"site": video.site, "id": video.bvid, "title": video.title, "duration": int(video.duration or 0),
            "position": 0.0}


def _casts(relay, dev: dict, rec: dict) -> list[dict]:
    """投屏历史（新的在前）；投屏位里的视频不在历史里时（这个功能之前投的）补在最前面。"""
    casts = [c for c in rec.get("casts") or [] if isinstance(c, dict) and c.get("id")]
    slot = relay._cast_of(dev)
    with slot.lock:
        video, position, done = slot.cast, slot.cast_position, slot.cast_done
    if video is not None:
        if not casts or (casts[0].get("site"), casts[0]["id"]) != (video.site, video.bvid):
            casts = [_entry(video)] + [c for c in casts if (c.get("site"), c["id"]) != (video.site, video.bvid)]
        casts[0] = dict(casts[0], position=0.0 if done else float(position), done=done)
    return casts


def remember_cast(relay, dev: dict, video, previous=None, position: float = 0.0, done: bool = False) -> None:
    """投屏了 video（web.py 的 _cast_video）：记进这台设备的投屏历史，换下来的那个记下播到哪。"""
    if not dev.get("id") and relay.tenant:
        return
    memo = _Memo(relay, dev)
    with memo as rec:
        casts = [c for c in rec.get("casts") or [] if isinstance(c, dict) and c.get("id")]
        if previous is not None:
            key = (previous.site, previous.bvid)
            for entry in casts:
                if (entry.get("site"), entry["id"]) == key:
                    entry["position"] = 0.0 if done else float(position)
                    break
            else:
                casts.insert(0, dict(_entry(previous), position=0.0 if done else float(position)))
        key = (video.site, video.bvid)
        casts = [_entry(video)] + [c for c in casts if (c.get("site"), c["id"]) != key]
        rec["casts"] = casts[:CAST_HISTORY_MAX]
        memo.changed = True


def recast(relay, key, caller: Caller = LEGACY, body: dict | None = None) -> dict:
    """POST /api/voice/recast {step, resume}：step 0 是最近一次投屏，1 是「上一个投屏」（正在看投屏时
    是它前面那个，没在看投屏时就是最近一次），2 是上上个。resume：从上次停下的地方接着放。"""
    body = body or {}
    dev = relay.find_device(key, caller)
    if dev is None:
        return {"ok": False, "error": REPLY_NO_DEVICE}
    step, resume = body.get("step", 0), body.get("resume") is True
    if not isinstance(step, int) or isinstance(step, bool) or not 0 <= step < CAST_HISTORY_MAX:
        return {"ok": False, "error": "请求格式不对"}
    playing = relay._playing(dev)
    on_cast = playing["online"] and playing["channel"] == table.CAST_ID
    index = step - 1 if step >= 1 and not on_cast else step
    casts = _casts(relay, dev, _view(relay, dev))
    if not casts:
        return {"ok": False, "error": "还没有投屏过"}
    if index >= len(casts):
        return {"ok": False, "error": "投屏历史里没有更早的了"}
    entry = casts[index]
    if resume and index == 0 and not entry.get("done") and _same_as_slot(relay, dev, entry):
        # 投屏位里就是它、还没放完：换到「我的投屏」就从停下的地方接着放。
        if on_cast:
            return {"ok": True, "title": entry.get("title") or "", "note": "正在播"}
        if not playing["online"]:
            return {"ok": False, "error": f"{dev['name']}没在线"}
        if not playing["switch"]:
            return {"ok": False, "error": "小电视的固件不支持遥控，请在小电视上手动换台"}
        relay._event(dev, "switch", channel=table.CAST_ID)
        return {"ok": True, "title": entry.get("title") or "", "resumed": True, "at": relay.clock()}
    site = entry.get("site") or "bilibili"
    if relay.clients.get(site) is None:
        return {"ok": False, "error": "YouTube 还没配置好"}
    video = Video(bvid=str(entry["id"]), title=str(entry.get("title") or ""),
                  duration=int(entry.get("duration") or 0), pubdate=0, site=site)
    position = float(entry.get("position") or 0) if resume else 0.0
    if video.duration and position >= video.duration - 10:
        position = 0.0
    answer = relay._cast_video(video, caller, dev, position=position)
    if answer.get("ok"):
        answer["resumed"] = position > 0
    return answer


def _same_as_slot(relay, dev: dict, entry: dict) -> bool:
    slot = relay._cast_of(dev)
    with slot.lock:
        video = slot.cast
    return video is not None and (video.site, video.bvid) == (entry.get("site"), entry.get("id"))


# ---- 订阅、取消订阅 ----

def _video_link(video) -> str:
    if video.site == "youtube":
        return f"https://www.youtube.com/watch?v={video.bvid}"
    return f"https://www.bilibili.com/video/{video.bvid}"


def subscribe(relay, key, caller: Caller = LEGACY, body: dict | None = None) -> dict:
    """POST /api/voice/subscribe：订阅正在放的投屏视频的作者（B 站按视频的 owner mid，YouTube 按视频
    信息里的频道），和语音搜到 UP 主后的「订阅」同一套限制（voice.follow_refusal），记在设备主人名下。"""
    body = body or {}
    dev = relay.find_device(key, caller)
    if dev is None:
        return {"ok": False, "error": REPLY_NO_DEVICE}
    playing = relay._playing(dev)
    cid = playing["channel"]
    if not playing["online"]:
        return {"ok": False, "error": f"{dev['name']}没在线"}
    state = relay._state_of(dev)
    if cid[:2] in ("up", "sr"):
        creator = state.get(cid)
        name = creator.name if creator is not None else cid
        return {"ok": False, "error": f"「{table.label_text(name, 16)}」已经在频道列表里了"}
    if cid != table.CAST_ID:
        return {"ok": False, "error": "现在放的不是投屏的视频，没有 UP 主可订阅"}
    slot = relay._cast_of(dev)
    with slot.lock:
        video = slot.cast
    if video is None:
        return {"ok": False, "error": "现在没有在放投屏的视频"}
    client, refusal = voice._client(relay, video.site)
    if client is None:
        return {"ok": False, "error": refusal}
    if relay.tenant and dev.get("owner") is None:
        return {"ok": False, "error": "这台电视还没绑定账号，不能订阅"}
    site = video.site

    def job() -> dict:
        source = client.creator_id(_video_link(video))
        if not source:
            return {"ok": False, "error": "找不到这个视频的作者"}
        name, videos = client.creator(source)
        refusal = voice.follow_refusal(relay, state, site, source, name)
        if refusal:
            return {"ok": False, "error": refusal}
        try:
            added = state.add(site, source, name, videos, relay.clock())
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        state.save()
        relay._cache_videos(added, added.videos, added.fetched_at)
        relay._tables_changed(state)
        relay.log(f"语音订阅正在放的视频的作者 {added.id} {name}（{site} {source}）")
        return {"ok": True, "creator": {"id": added.id, "name": name}}

    return voice._within(relay, site, job, voice._wait(body.get("wait")))


def unsubscribe(relay, key, caller: Caller = LEGACY, body: dict | None = None, keyed: bool = False) -> dict:
    """POST /api/voice/unsubscribe {id}：删掉设备主人的一个 UP 主或剧集台。

    设备上先问过「确定删除？」（server/voice/session.py）。多用户时只有电视服务替设备发的（带密钥、
    点名设备）和设备主人自己可以删：扫码进来的访客手机不行。设备正在放这个台时换回刚才的台。"""
    body = body or {}
    dev = relay.find_device(key, caller)
    if dev is None:
        return {"ok": False, "error": REPLY_NO_DEVICE}
    if relay.tenant and not keyed and (caller.guest or caller.user != dev.get("owner")):
        return {"ok": False, "error": "只有设备主人能删台"}
    cid = body.get("id")
    if not isinstance(cid, str) or cid[:2] not in ("up", "sr"):
        return {"ok": False, "error": "这个台不能删"}
    state = relay._state_of(dev)
    creator = state.get(cid)
    if creator is None or not state.remove(cid):
        return {"ok": False, "error": "没有这个台了"}
    state.save()
    relay._tables_changed(state)
    relay.log(f"语音删除 {cid} {creator.name}（{dev['name']}）")
    playing = relay._playing(dev)
    if playing["online"] and playing["channel"] == cid and playing["switch"]:
        rows = [row_id for row_id, _ in relay.channel_rows(dev)]
        rec = _view(relay, dev)
        target = next((c for c in back_channels(rec, cid) if c in rows and c != table.CAST_ID), None)
        target = target or next((c for c in rows if c != table.CAST_ID and c[:2] not in ("up", "sr")), None)
        if target:
            relay._event(dev, "switch", channel=target)
    return {"ok": True, "id": cid, "name": creator.name, "kind": creator.kind}


# ---- 管理网页的 POST 分派（web.py 只调这一个） ----

PATHS = {"/api/voice/timer": set_timer, "/api/voice/subscribe": subscribe,
         "/api/voice/unsubscribe": unsubscribe, "/api/voice/recast": recast}


def post(relay, path: str, device, caller: Caller, body: dict, keyed: bool = False) -> dict | None:
    handler = PATHS.get(path)
    if handler is None:
        return None
    if handler is unsubscribe:
        return unsubscribe(relay, device, caller, body, keyed=keyed)
    try:
        return handler(relay, device, caller, body)
    except SourceError as error:
        return voice._failure(relay, "bilibili", "订阅", error)
