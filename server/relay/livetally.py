"""每个人常看哪些直播台：直播页最前面的「常看」分组（自建 / 电脑客户端和云端都有）。

每 TICK_S 秒看一眼：在线的小电视在放全局频道表里的台（ch…），记给这台设备的主人；在电脑上看直播的（不算预热），
记给在看的人。记的是看了多少秒，不是点了几次（换台时一个个点过去的不算常看）；旧的按 HALF_LIFE_S 减半，
最近的习惯说了算。累计不到 MIN_S 秒的不算，至多 FAVS_MAX 个。

存在数据库旁边的 live-plays.json：{用户 id（单用户时 "0"）: {台 id: [分数（秒）, 上次更新的时刻]}}，每人至多
KEEP_MAX 个台；只有台 id 和秒数，没有地址。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .. import now_playing

TICK_S = 30.0
HALF_LIFE_S = 14 * 86400.0
MIN_S = 120.0
FAVS_MAX = 12
KEEP_MAX = 60
SAVE_EVERY_S = 300.0
_LIVE_ID = re.compile(r"^ch\d{3,6}$")


class LiveTally:
    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        self._dirty = False
        self._saved = clock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self._data: dict[str, dict[str, list]] = {
            str(user): {cid: [float(v[0]), float(v[1])] for cid, v in rows.items()
                        if _LIVE_ID.match(str(cid)) and isinstance(v, list) and len(v) == 2}
            for user, rows in data.items() if isinstance(rows, dict)} if isinstance(data, dict) else {}

    @staticmethod
    def _key(user: int | None) -> str:
        return str(user) if user is not None else "0"

    def _decayed(self, item: list, now: float) -> float:
        return item[0] * 0.5 ** (max(0.0, now - item[1]) / HALF_LIFE_S)

    def add(self, user: int | None, cid: str, seconds: float) -> None:
        if not _LIVE_ID.match(cid or "") or seconds <= 0:
            return
        now = self.clock()
        with self.lock:
            mine = self._data.setdefault(self._key(user), {})
            item = mine.get(cid)
            mine[cid] = [(self._decayed(item, now) if item else 0.0) + seconds, now]
            if len(mine) > KEEP_MAX:
                for old in sorted(mine, key=lambda k: self._decayed(mine[k], now))[:len(mine) - KEEP_MAX]:
                    del mine[old]
            self._dirty = True

    def move(self, old: str, new: str) -> int:
        """总后台合并了重复的台（channelmerge.py）：每个人在 old 上看的秒数（衰减到现在的）加到 new 上，去掉 old。
        返回改了几个人的；马上存。"""
        if not _LIVE_ID.match(old or "") or not _LIVE_ID.match(new or "") or old == new:
            return 0
        now, moved = self.clock(), 0
        with self.lock:
            for mine in self._data.values():
                item = mine.pop(old, None)
                if item is None:
                    continue
                have = mine.get(new)
                mine[new] = [self._decayed(item, now) + (self._decayed(have, now) if have else 0.0), now]
                moved += 1
            if moved:
                self._dirty = True
        if moved:
            self.save(force=True)
        return moved

    def top(self, user: int | None, n: int = FAVS_MAX) -> list[str]:
        """这个人最常看的台（台 id，从多到少），累计不到 MIN_S 秒的不算。"""
        now = self.clock()
        with self.lock:
            scored = [(self._decayed(v, now), cid) for cid, v in self._data.get(self._key(user), {}).items()]
        return [cid for score, cid in sorted(scored, key=lambda x: (-x[0], x[1])) if score >= MIN_S][:n]

    def save(self, force: bool = False) -> None:
        now = self.clock()
        with self.lock:
            if not self._dirty or (not force and now - self._saved < SAVE_EVERY_S):
                return
            body = json.dumps(self._data, separators=(",", ":"))
            self._dirty, self._saved = False, now
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            with self.lock:
                self._dirty = True


def tick(relay, tally: LiveTally, seconds: float = TICK_S) -> None:
    """这 seconds 秒里谁在看哪个直播台。"""
    now = relay.clock()
    for dev in list(relay.devices):
        playing = now_playing.read(dev.get("now"), now)
        channel = playing.get("channel")
        if playing.get("online") and isinstance(channel, str) and _LIVE_ID.match(channel):
            tally.add(dev.get("owner"), channel, seconds)
    watch = getattr(relay, "watch", None)
    if watch is not None:
        seen = set()
        for stream in watch.streams():
            key = stream.source.key
            if not stream.alive or getattr(stream, "cheap", False) or not key or key[0] != "live":
                continue
            if (stream.user, key[1]) in seen:
                continue
            seen.add((stream.user, key[1]))
            tally.add(stream.user, key[1], seconds)
    tally.save()


def attach(relay, folder: Path, start: bool = True) -> LiveTally:
    tally = LiveTally(Path(folder) / "live-plays.json")
    relay.tally = tally

    def run() -> None:
        while True:
            time.sleep(TICK_S)
            try:
                tick(relay, tally)
            except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                relay.log(f"记常看的台出错：{type(error).__name__}")

    if start:
        threading.Thread(target=run, name="livetally", daemon=True).start()
    return tally
