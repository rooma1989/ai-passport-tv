"""把视频中转的投屏事件变成对当前会话的切台决定。

中转在投屏开始写 cast、投屏播完写 return（server/relay/events.py）。电视服务在直播会话中
每秒最多看一次事件文件；启动时文件里已有的那条只记录不执行，避免重启后把设备切走。
"""

from __future__ import annotations

from pathlib import Path

from .relay.events import read_event
from .relay.table import CAST_ID

POLL_INTERVAL_S = 1.0


class SwitchWatcher:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        current = read_event(self.path) if self.path else None
        self.seen = current.seq if current else 0
        # 投屏前设备所在的台，投屏播完切回这里。
        self.previous = ""
        self._next_poll = 0.0

    def poll(self, current: str, now: float, force: bool = False):
        """返回 ("switch", 频道) 、("restart", None)（已在投屏台时来了新投屏）或 None。

        平时每秒最多看一次；force 用于设备刚连上的那一刻，不能错过。
        """
        if self.path is None or (now < self._next_poll and not force):
            return None
        self._next_poll = now + POLL_INTERVAL_S
        event = read_event(self.path)
        if event is None or event.seq <= self.seen:
            return None
        self.seen, action = event.seq, event.action
        if action == "switch":
            # 手机遥控换台；已经在这个台就不动。
            return ("switch", event.channel) if event.channel != current else None
        if action == "cast":
            if current == CAST_ID:
                return ("restart", None)
            self.previous = current
            return ("switch", CAST_ID)
        if action == "return" and current == CAST_ID and self.previous:
            return ("switch", self.previous)
        return None
