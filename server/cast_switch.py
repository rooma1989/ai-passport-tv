"""把视频中转的投屏事件变成对当前会话的切台决定。

中转在投屏开始写 cast、投屏播完写 return（server/relay/events.py）。电视服务在直播会话中
每秒最多看一次事件文件；启动时文件里已有的那条只记录不执行，避免重启后把设备切走。
"""

from __future__ import annotations

from pathlib import Path

from . import episode_info
from .relay.events import read_event, read_events
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
        # 暂停、继续、音量、亮度：按顺序排队，由会话在合适的地方取走（take_controls）。
        self._controls: list[tuple[str, int | None]] = []

    def take_controls(self) -> list[tuple[str, int | None]]:
        taken, self._controls = self._controls, []
        return taken

    def take_loading(self) -> tuple[str, dict] | None:
        """最新的「正在加载」(台, 给设备的 payload)，连同排在队里的「下一个」提示一起取走：
        会画「正在加载」的固件不需要那个提示框。没有时为 None，队列不动。"""
        loads = [value for kind, value in self._controls if kind == "loading"]
        if not loads:
            return None
        self._controls = [c for c in self._controls if c[0] not in ("loading", "next")]
        return loads[-1]

    def requeue(self, controls: list[tuple[str, int | None]]) -> None:
        """取走后暂时处理不了的，放回队首。"""
        self._controls = list(controls) + self._controls

    def poll(self, current: str, now: float, force: bool = False):
        """处理所有新事件；返回换台类的决定：("switch", 频道)、("restart", None)（已在投屏台时来了
        新投屏）或 None。同一批里有多个时以最后一个为准。其余事件进 take_controls() 的队列。

        平时每秒最多看一次；force 用于设备刚连上的那一刻，不能错过。
        """
        if self.path is None or (now < self._next_poll and not force):
            return None
        self._next_poll = now + POLL_INTERVAL_S
        decision = None
        for event in read_events(self.path):
            if event.seq <= self.seen:
                continue
            self.seen = event.seq
            action = event.action
            if action in ("pause", "resume", "volume", "brightness", "seek", "next", "pet", "audio"):
                # 同类的数值只留最新的，免得拖滑条时一个个依次发过去。
                if action in ("volume", "brightness", "pet", "audio"):
                    self._controls = [c for c in self._controls if c[0] != action]
                # 暂停带的是标题（text），其余带数值。
                self._controls.append((action, event.text if action == "pause" else event.value))
                continue
            if action == "wifi":
                # 加密好的 Wi-Fi 修改：原样转给设备（只有它解得开），连同写入时刻，过期的不发。
                self._controls.append(("wifi", (event.text, event.value)))
                continue
            if action == "loading":
                # 中转马上要换片源：(台, 设备要画的 {"title", "episode", "total"})；只留最新的。
                info = episode_info.payload(event.text, event.value or 0, event.total)
                self._controls = [c for c in self._controls if c[0] != "loading"]
                if info is not None:
                    self._controls.append(("loading", (event.channel, info)))
                continue
            if action == "switch":
                # 手机遥控换台；要换回当前这个台就等于不换。
                decision = ("switch", event.channel) if event.channel != current else None
            elif action == "cast":
                if current == CAST_ID:
                    decision = ("restart", None)
                else:
                    self.previous = current
                    decision = ("switch", CAST_ID)
            elif action == "return" and self.previous:
                # 投屏播完：设备在投屏台（或这一批里正要切过去）才切回原台。
                heading_to_cast = decision is not None and decision[1] == CAST_ID
                if current == CAST_ID or heading_to_cast:
                    decision = ("switch", self.previous) if self.previous != current else None
        return decision
