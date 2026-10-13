"""电视服务写、中转读的「正在播放」状态：哪个台、设备在不在线、支不支持切台。

会话开始、结束时写，播放中每 10 秒刷新一次；标为在线但超过 30 秒没刷新，就当作已离线
（设备断网时会话可能要过一会儿才结束）。

总后台（server/relay/admin.py）还要看这次会话的流量：bytes_10s（最近一段时间发出的字节，折算成 10 秒）、
fps（每秒发出的画面数）、target_rate（字节率控制器此刻的目标，字节/秒）。电视服务用 Meter 量，
read() 不管这三项（手机遥控用不着），read_metrics() 单独读。
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
# 总后台看的流量三项（见上）。
METRIC_KEYS = ("bytes_10s", "fps", "target_rate")
# 流量至少隔这么久量一次：设备报音量、亮度时也会写这个文件，间隔太短量出来的数跳得厉害。
METER_MIN_S = 5.0


class Meter:
    """量一次会话的流量：给累计发出的字节数和画面数，算出最近这段时间的速率。

    计数在会话之间归零（tv_server 每个会话从 0 数起），变小了就当作从 0 开始。
    """

    def __init__(self):
        self._last: tuple[float, int, int] | None = None
        self._result: dict = {"bytes_10s": 0, "fps": 0.0}

    def sample(self, now: float, wire_bytes: int, frames: int) -> dict:
        """{"bytes_10s", "fps"}；离上次量不到 METER_MIN_S 秒时给上次的结果。"""
        last = self._last
        if last is None:
            self._last = (now, wire_bytes, frames)
            return dict(self._result)
        elapsed = now - last[0]
        if elapsed < METER_MIN_S:
            return dict(self._result)
        sent = wire_bytes - last[1] if wire_bytes >= last[1] else wire_bytes
        shown = frames - last[2] if frames >= last[2] else frames
        self._result = {"bytes_10s": int(round(sent * 10 / elapsed)), "fps": round(shown / elapsed, 1)}
        self._last = (now, wire_bytes, frames)
        return dict(self._result)


def write(path: Path | None, channel: str, connected: bool, switch: bool,
          now: float | None = None, since: float = 0.0, device: dict | None = None,
          metrics: dict | None = None) -> None:
    """since：这次会话开始的时间。手机据此分辨小智是不是已经切到它要的台（新会话）。
    device：设备报告的 {"volume", "brightness", "steps"}；旧固件不报，为 None。
    metrics：总后台看的 {"bytes_10s", "fps", "target_rate"}，没有时不写。"""
    if path is None:
        return
    path = Path(path)
    record = {"channel": channel, "connected": bool(connected), "switch": bool(switch),
              "at": time.time() if now is None else now, "since": float(since), "device": device}
    for key in METRIC_KEYS:
        value = (metrics or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            record[key] = value
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        # 只是给手机网页看的状态，写不了不影响播放。
        pass


def read_metrics(path: Path | None, now: float | None = None) -> dict | None:
    """总后台用：在线时 {"bytes_10s", "fps", "target_rate"}（电视服务没写的项为 None）；不在线时 None。"""
    if not read(path, now)["online"]:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {key: data.get(key) if isinstance(data.get(key), (int, float)) and not isinstance(data.get(key), bool)
            else None for key in METRIC_KEYS}


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
            "switch": bool(data.get("switch")) and online, "since": since, "device": kept}
