"""设备上的播放进度条：视频多长、现在播到哪。

只发给 HELLO 里带 "progress": 1 的固件，只对中转推流的台（剧集、UP 主、投屏）；电视台直播什么都不发。
线上是一个 SWITCH 包，见 docs/local-tv-prototype.md：

    {"progress": {"dur": 总长毫秒, "pos": 位置毫秒, "at": 会话音频时间戳毫秒}}

意思是设备的音频时钟（播到的音频 PTS）走到 at 时，视频正好播到 pos；之后设备按自己的音频时钟往前推，
所以不用每秒发。多带 "stop": 毫秒 表示片源在设备时钟走到 stop 时停了（断流、暂停），进度停在
pos + (stop - at)。{"progress": 0}：新片源不知道总长，设备收起进度条。

什么时候发：

- 片源开始供数据（会话开始 at=0；断流、跳转、暂停后、换集后新片源接上，at = 接上后第一块声音的时间戳）：
  在后台问中转的 /info/<台>（和「正在加载」同一个地址），它答 duration（秒）和 offset（这一路流从
  第几秒推起，秒），问到就发。新片源的第一块声音就是 offset 处的内容，所以 at 在接上那一刻就定了，
  问得慢一点也不影响准不准。
- 断流、暂停开始时：同一个锚点加 stop（最后一块真声音之后的时间戳）。
- 原片源自己缓过来（没换新的）：从停下的位置接着走，at 换成接上后的时间戳，不用再问。

误差：B 站流按关键帧起播，实际比 offset 早最多一个关键帧间隔（几秒）；够画一条进度条。
"""

from __future__ import annotations

import time

# 中转刚开新流时 /info 可能还没有 offset（极少：电视服务等到预缓冲完才问）：隔这么久再问，最多问这么多次。
RETRY_S = 1.0
TRIES = 3
# 超过这个长度的「总长」不当真。
DURATION_MAX_S = 24 * 3600


def _seconds(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


def from_relay(answer) -> dict | None:
    """中转 /info 的回答 → {"dur": 毫秒, "pos": 毫秒}；还没在推流时只有 dur；答了但没有总长时是 {}；
    没答（出错、404）是 None。"""
    if not isinstance(answer, dict):
        return None
    duration = _seconds(answer.get("duration"))
    if duration is None or not 0 < duration <= DURATION_MAX_S:
        return {}
    out = {"dur": int(round(duration * 1000))}
    offset = _seconds(answer.get("offset"))
    if offset is not None and offset >= 0:
        out["pos"] = min(int(round(offset * 1000)), out["dur"])
    return out


class Tracker:
    """一个会话里的进度锚点。`fetcher(url)` 返回后台的问询（有 .done、.result，result 是 from_relay 的结果）。

    各方法返回要发给设备的 SWITCH 内容（{"progress": ...}），没有要发的时为 None。
    """

    def __init__(self, fetcher, clock=time.monotonic):
        self._fetcher = fetcher
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        """会话开始：设备那边也从「不知道」开始（CONFIG 清掉它的进度）。"""
        # 当前片源的锚点 {"dur", "pos", "at"}；None 是还不知道。
        self.anchor: dict | None = None
        # 片源停在设备时钟的哪一刻；None 是在走。
        self.stop: int | None = None
        # 设备上这次会话是不是挂着进度条（决定要不要发 {"progress": 0}）。
        self.shown = False
        self._url: str | None = None
        self._fetch = None
        self._at = 0
        self._tries = 0
        self._retry_at = 0.0

    def begin(self, url: str | None, at: int) -> None:
        """新片源开始供数据，它第一块声音的线上时间戳是 at；url 是中转的 /info 地址。"""
        self.anchor = None
        self.stop = None
        self._url, self._at, self._tries = url, at, 0
        self._fetch = None
        self._retry_at = 0.0
        if url:
            self._ask()

    def _ask(self) -> None:
        self._tries += 1
        self._fetch = self._fetcher(self._url)

    def stopped(self, stop_at: int) -> dict | None:
        """片源停了（断流、暂停）：设备时钟走到 stop_at 时进度停下。"""
        self.stop = stop_at
        if self.anchor is None:
            return None
        return {"progress": dict(self.anchor, stop=max(stop_at, self.anchor["at"]))}

    def resumed(self, at: int) -> dict | None:
        """原片源自己接上了（内容接着停下的地方）：at 是接上后第一块声音的时间戳。"""
        stop, self.stop = self.stop, None
        if stop is None:
            return None
        gap = max(0, at - stop)
        if self.anchor is None:
            # 锚点还在问：问到时按停顿顺延。
            if self._url:
                self._at += gap
            return None
        self.anchor = dict(self.anchor, at=self.anchor["at"] + gap)
        return {"progress": dict(self.anchor)}

    def poll(self) -> dict | None:
        """发送循环里每轮调：后台问到了就给出要发的。"""
        if self._fetch is None:
            if self._url and self._retry_at and self._clock() >= self._retry_at:
                self._retry_at = 0.0
                self._ask()
            return None
        if not self._fetch.done:
            return None
        result, self._fetch = self._fetch.result, None
        if result is None:
            self._url = None
            return None
        if "dur" in result and "pos" not in result:
            if self._tries < TRIES:
                self._retry_at = self._clock() + RETRY_S
            else:
                self._url = None
            return None
        self._url = None
        if "pos" not in result:
            # 中转答了，但这个视频不知道多长：设备上要是还挂着上一个的，收起来。
            if self.shown:
                self.shown = False
                return {"progress": 0}
            return None
        self.anchor = {"dur": result["dur"], "pos": result["pos"], "at": self._at}
        self.shown = True
        payload = dict(self.anchor)
        if self.stop is not None:
            payload["stop"] = max(self.stop, self._at)
        return {"progress": payload}
