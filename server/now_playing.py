"""电视服务写、中转读的「正在播放」状态：哪个台、设备在不在线、支不支持切台。

会话开始、结束时写，播放中每 10 秒刷新一次；标为在线但超过 30 秒没刷新，就当作已离线
（设备断网时会话可能要过一会儿才结束）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

REFRESH_S = 10.0
STALE_S = 30.0
# 会话刚结束的这段时间仍算在线：换台、断流重连时设备几秒内就会回来，
# 这时手机上的换台先记下，设备连上时直接给它新台（见 tv_server 的 pending_switch）。
GRACE_S = 15.0


def write(path: Path | None, channel: str, connected: bool, switch: bool,
          now: float | None = None, since: float = 0.0, device: dict | None = None,
          listen: dict | None = None) -> None:
    """since：这次会话开始的时间。手机据此分辨小智是不是已经切到它要的台（新会话）。
    device：设备报告的 {"volume", "brightness", "steps"}；旧固件不报，为 None。
    listen：电视服务开了「用手机听」时的 {"route", "lead", "ok", "on", "lag"}（server/listen.py 的
    ListenService.status）；没开为 None。"""
    if path is None:
        return
    path = Path(path)
    record = {"channel": channel, "connected": bool(connected), "switch": bool(switch),
              "at": time.time() if now is None else now, "since": float(since), "device": device}
    if listen is not None:
        record["listen"] = listen
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        # 只是给手机网页看的状态，写不了不影响播放。
        pass


def read(path: Path | None, now: float | None = None) -> dict:
    """{"channel", "online", "switch", "since"}；读不到就是不在线。"""
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        at = float(data.get("at", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        data, at = {}, 0.0
    age = now - at
    online = age < (STALE_S if data.get("connected") else GRACE_S)
    try:
        since = float(data.get("since") or 0.0)
    except (TypeError, ValueError):
        since = 0.0
    device = data.get("device")
    if not (isinstance(device, dict) and all(isinstance(device.get(k), int)
                                             for k in ("volume", "brightness", "steps"))):
        device = None
    kept = None
    if device:
        kept = {k: device[k] for k in ("volume", "brightness", "steps")}
        # 宠物：会换宠物的固件才报。
        for key in ("pet", "audio_only"):
            if isinstance(device.get(key), int):
                kept[key] = device[key]
    return {"channel": str(data.get("channel") or ""), "online": online,
            "switch": bool(data.get("switch")) and online, "since": since, "device": kept,
            "listen": _listen(data.get("listen")) if online else None}


def _listen(record) -> dict | None:
    """「用手机听」的那一项，只留认得的、类型对的；不对就当作没开。"""
    if not isinstance(record, dict) or not isinstance(record.get("route"), str):
        return None
    try:
        lead, lag = float(record.get("lead") or 0), float(record.get("lag") or 0)
    except (TypeError, ValueError):
        return None
    return {"route": record["route"], "lead": lead, "ok": record.get("ok") is True,
            "on": record.get("on") is True, "lag": lag}
