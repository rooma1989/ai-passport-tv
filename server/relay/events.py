"""中转写给电视服务的事件：投屏开始（cast）、投屏播完（return）、手机换台（switch，带频道）。

文件只有一条最新事件，seq 只增不减；电视服务按 seq 判断是否有新事件，启动时看到的那条只记录不执行。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

ACTIONS = ("cast", "return", "switch")


class Event(NamedTuple):
    seq: int
    action: str
    channel: str = ""


def read_event(path: Path) -> Event | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        event = Event(int(data["seq"]), str(data["action"]), str(data.get("channel") or ""))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if event.action not in ACTIONS or (event.action == "switch" and not event.channel):
        return None
    return event


def write_event(path: Path, action: str, channel: str = "") -> int:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if action == "switch" and not channel:
        raise ValueError("switch needs a channel")
    path = Path(path)
    previous = read_event(path)
    seq = (previous.seq if previous else 0) + 1
    tmp = path.with_name(path.name + ".tmp")
    record = {"seq": seq, "action": action}
    if channel:
        record["channel"] = channel
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return seq
