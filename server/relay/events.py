"""中转写给电视服务的事件。

cast（投屏开始）、return（投屏播完）、switch（手机换台，带频道）、pause / resume（暂停、继续）、
volume / brightness（手机调音量、亮度，带数值）、seek（手机快进快退，带秒数，负数是后退）、
next（手机点下一个）。seek 和 next 只是让设备画提示，跳转本身由中转完成。pet（手机换宠物，带编号）。

文件保存最近 KEEP 条，seq 只增不减；电视服务按顺序处理比上次看到的 seq 更新的每一条，启动时已有的只记录
不执行。只存一条时，拖滑条连续写出的音量事件会把紧挨着的换台覆盖掉，所以改成列表。旧的单条格式仍能读。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

ACTIONS = ("cast", "return", "switch", "pause", "resume", "volume", "brightness", "seek", "next", "pet")
KEEP = 20
TEXT_MAX = 64
# 数值的合法范围；亮度档位序号由设备决定，这里只挡住明显不对的值。
_VALUE_RANGE = {"volume": (0, 100), "brightness": (0, 15), "seek": (-86400, 86400), "pet": (0, 31)}


class Event(NamedTuple):
    seq: int
    action: str
    channel: str = ""
    value: int | None = None
    # 只给 pause 用：暂停的视频标题，设备画在暂停横条上。
    text: str = ""


def _valid(event: Event) -> bool:
    if event.action not in ACTIONS:
        return False
    if event.action == "switch":
        return bool(event.channel)
    if event.action in _VALUE_RANGE:
        low, high = _VALUE_RANGE[event.action]
        return event.value is not None and low <= event.value <= high
    return True


def _parse(record) -> Event | None:
    try:
        value = record.get("value")
        event = Event(int(record["seq"]), str(record["action"]), str(record.get("channel") or ""),
                      int(value) if value is not None else None, str(record.get("text") or "")[:TEXT_MAX])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return event if _valid(event) else None


def read_events(path: Path) -> list[Event]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("events") if isinstance(data, dict) and "events" in data else [data]
    if not isinstance(records, list):
        return []
    parsed = [e for e in (_parse(r) for r in records) if e is not None]
    return sorted(parsed, key=lambda e: e.seq)


def read_event(path: Path) -> Event | None:
    """最新的一条。"""
    listed = read_events(path)
    return listed[-1] if listed else None


def write_event(path: Path, action: str, channel: str = "", value: int | None = None,
                text: str = "") -> int:
    event = Event(0, action, channel, value, text[:TEXT_MAX])
    if not _valid(event):
        raise ValueError(f"bad event {action!r} channel={channel!r} value={value!r}")
    path = Path(path)
    listed = read_events(path)
    seq = (listed[-1].seq if listed else 0) + 1
    record = {"seq": seq, "action": action}
    if channel:
        record["channel"] = channel
    if value is not None:
        record["value"] = value
    if text:
        record["text"] = text[:TEXT_MAX]
    kept = [e._asdict() for e in listed[-(KEEP - 1):]]
    for item in kept:
        if not item["channel"]:
            del item["channel"]
        if item["value"] is None:
            del item["value"]
        if not item["text"]:
            del item["text"]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"events": kept + [record]}), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return seq
