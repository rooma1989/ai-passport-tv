"""中转写给电视服务的事件：投屏开始（cast）与投屏播完（return）。

文件只有一条最新事件，seq 只增不减；电视服务按 seq 判断是否有新事件，启动时看到的那条只记录不执行。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ACTIONS = ("cast", "return")


def read_event(path: Path) -> tuple[int, str] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        seq, action = int(data["seq"]), str(data["action"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (seq, action) if action in ACTIONS else None


def write_event(path: Path, action: str) -> int:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    path = Path(path)
    previous = read_event(path)
    seq = (previous[0] if previous else 0) + 1
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"seq": seq, "action": action}), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return seq
