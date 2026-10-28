"""直播线路的质量（自建 / 电脑客户端，跟着后台自检 linecheck.py 走）：真实速度，和画面是不是真的。

「检测线路」（routecheck.probe）只量连上要多久。量过的教训：一条 CCTV1 连得很快、实际只有 0.2–0.7 倍实时；
公开列表里还有台名标错的（「CGTN 阿语」其实是西语台，「CCTV5」其实是 CCTV5+）、只放购物广告的、反复放一段
旧录像的。这里每 SWEEP_EVERY_S 秒把一个台的线路过一遍（一次一个台，两个台之间至少隔 SWEEP_GAP_S 秒；
机器忙、有人在电脑上看时不做，和取封面一样）：

- 真实速度：PACE_EVERY_S 内没量过的线路，完整下载最新的一段（routecheck.measure），记进 meta 的 quality
  （routes.remember_pace；电脑上真放时量到的也记在那里）。不到 routes.PACE_WEAK 倍的排到能用的线路后面。
- 画面：同时从这个台的每条线路（还在往下走的直播，录像不算）取 THUMB_SECONDS 秒、每 THUMB_EVERY_S 秒一帧，
  缩成 THUMB_W×THUMB_H 的灰度小图（几百字节一张，只在内存里），按逐个像素差的平均（mad）比。几条线路之间
  常差十几秒（各自从自己列表的末尾起），只取一帧比，同一个台也常常是两个镜头（量过：一次里五条有三条「对不上」）；
  两组画面里最像的那一对（distance）在同一个台总能对上，不同的台怎么都对不上。几乎一色的画面（黑场、换节目时的
  纯色）不拿来比（flat）。
  - still：这 THUMB_SECONDS 秒里画面没动过，并且和这条线路 STILL_GAP_S 秒以前取的几乎一样（< STILL_MAD）：
    静止画面或反复放的录像。真直播十几秒里总会动（滚动字幕、时钟），隔这么久也不会一模一样；「信号中断」
    这类画面本来也是坏线路。
  - odd：别的线路里有两条彼此对得上（< AGREE_MAD），这一条和它们都差得多（> DIFFER_MAD），也不和谁对得上：
    这一次算对不上。量过 12 个 CCTV 台 49 条线路：同一个台两组画面的距离在 0–18，对不上的在 53–84；可是
    对不上的里既有冒充的（放购物广告），也有真的同一个台、只是晚了一截（截到另一场戏）。所以要连着 ODD_MIN 次
    （每次隔 SWEEP_EVERY_S）都对不上才算可疑；可疑的只排到后面、在总后台标出来，管理员「看一眼」再决定删不删。
  可疑的线路（routes.suspect_in）排到最后，总后台的线路编辑里标出来，不自动删。

日志只写台名和第几条线路，不写地址。
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .. import routes
from . import channeltable, routecheck

THUMB_W, THUMB_H = 32, 18
THUMB_BYTES = THUMB_W * THUMB_H
THUMB_SECONDS = 20
THUMB_EVERY_S = 2
THUMB_TIMEOUT_S = 45
# 像素的标准差不到这么多的画面算「几乎一色」（黑场、纯色），不拿来比。
FLAT_STD = 8.0
SWEEP_EVERY_S = 6 * 3600.0
SWEEP_GAP_S = 60.0
START_DELAY_S = 600.0
PACE_EVERY_S = 24 * 3600.0
STILL_GAP_S = 600.0
STILL_MAD = 2.0
AGREE_MAD = 20.0
DIFFER_MAD = 40.0


def grab_thumbs(ffmpeg: str, url: str, agent: str = "", lenient: bool = False, run=subprocess.run) -> list[bytes] | None:
    """取 THUMB_SECONDS 秒、每 THUMB_EVERY_S 秒一帧，缩成 THUMB_W×THUMB_H 的灰度原始像素（每张 THUMB_BYTES 字节）。
    一张也取不到为 None。"""
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "quiet",
               "-rw_timeout", "15000000", "-user_agent", agent or routecheck.DEFAULT_USER_AGENT,
               *(routes.LENIENT_ARGS if lenient else ()),
               "-i", url, "-map", "0:v:0", "-t", str(THUMB_SECONDS), "-threads", "1",
               "-vf", f"fps=1/{THUMB_EVERY_S},scale={THUMB_W}:{THUMB_H},format=gray", "-f", "rawvideo", "-"]
    try:
        done = run(command, capture_output=True, timeout=THUMB_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    body = done.stdout or b""
    frames = [body[i:i + THUMB_BYTES] for i in range(0, len(body) - THUMB_BYTES + 1, THUMB_BYTES)]
    return frames or None


def mad(a: bytes, b: bytes) -> float:
    """两张同样大小的灰度小图逐个像素差的平均（0–255）。"""
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, min(len(a), len(b)))


def flat(frame: bytes) -> bool:
    """几乎一色的画面（黑场、纯色）。"""
    mean = sum(frame) / max(1, len(frame))
    return (sum((x - mean) ** 2 for x in frame) / max(1, len(frame))) ** 0.5 < FLAT_STD


def distance(a: list[bytes], b: list[bytes]) -> float | None:
    """两组画面里最像的那一对差多少；有一组全是一色的画面时 None（比不了）。"""
    a, b = [f for f in a if not flat(f)], [f for f in b if not flat(f)]
    if not a or not b:
        return None
    return min(mad(x, y) for x in a for y in b)


def frozen(frames: list[bytes]) -> bool:
    """这几秒里画面没动过（至少两张、都不是一色的、每两张相邻的几乎一样）。"""
    frames = [f for f in frames if not flat(f)]
    return len(frames) >= 2 and all(mad(x, y) < STILL_MAD for x, y in zip(frames, frames[1:]))


def judge_odd(thumbs: dict[str, list[bytes]]) -> dict[str, bool | None]:
    """同一个台、同一时段各条线路的画面 → 每条这一次对不对得上（True 对不上，False 对得上，None 判断不了）。"""
    marks = list(thumbs)
    if len(marks) < 3:
        return {m: None for m in marks}
    diff = {(a, b): distance(thumbs[a], thumbs[b]) for a in marks for b in marks if a < b}
    d = lambda a, b: diff[(a, b) if a < b else (b, a)]  # noqa: E731
    agree = lambda a, b: d(a, b) is not None and d(a, b) < AGREE_MAD  # noqa: E731
    differ = lambda a, b: d(a, b) is not None and d(a, b) > DIFFER_MAD  # noqa: E731
    found: dict[str, bool | None] = {}
    for mark in marks:
        others = [m for m in marks if m != mark]
        if any(agree(mark, o) for o in others):
            found[mark] = False
            continue
        pairs = [(a, b) for a in others for b in others if a < b and agree(a, b)]
        found[mark] = True if any(differ(mark, a) and differ(mark, b) for a, b in pairs) else None
    return found


class Sweeper:
    """一个台一个台地量。table() 给频道表；step() 由 LineChecker.tick 在机器不忙时调。"""

    def __init__(self, table: Callable[[], Path | None], ffmpeg: str = "ffmpeg", measure=routecheck.measure,
                 grab=grab_thumbs, clock=time.time, log=print, spawn=None):
        self.table = table
        self.ffmpeg = ffmpeg
        self.measure = measure
        self.grab = grab
        self.clock = clock
        self.log = log
        self.spawn = spawn or (lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True,
                                                                  name="linequality").start())
        self.started = clock()
        self._swept: dict[str, float] = {}
        self._thumbs: dict[tuple[str, str], tuple[list[bytes], float]] = {}
        self._busy = False
        self._last = 0.0
        self._lock = threading.Lock()

    def _channels(self) -> list[tuple[dict, dict]]:
        path = self.table()
        if path is None:
            return []
        rows = channeltable.read(path)["rows"]
        meta = routes.read_meta(path)
        return [(row, meta.get(row["id"], {}) if isinstance(meta.get(row["id"]), dict) else {}) for row in rows]

    def due(self, now: float) -> str | None:
        """下一个该量的台（最久没量过的；量过不到 SWEEP_EVERY_S 的不算）。"""
        waiting = [(self._swept.get(row["id"], 0.0), i, row["id"]) for i, (row, _e) in enumerate(self._channels())
                   if now - self._swept.get(row["id"], 0.0) >= SWEEP_EVERY_S]
        return min(waiting)[2] if waiting else None

    def step(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        with self._lock:
            if self._busy or now - self._last < SWEEP_GAP_S or now < self.started + START_DELAY_S:
                return False
        cid = self.due(now)
        if cid is None:
            return False
        with self._lock:
            self._busy, self._last = True, now
            self._swept[cid] = now
        self.spawn(self.sweep, cid)
        return True

    def sweep(self, cid: str) -> dict:
        """量这个台的每条线路，结论写进 meta。回答 {指纹: 要记的字段}（给测试看）。"""
        try:
            return self._sweep(cid)
        except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
            self.log(f"后台检查线路质量出错：{type(error).__name__}")
            return {}
        finally:
            with self._lock:
                self._busy = False

    def _sweep(self, cid: str) -> dict:
        path = self.table()
        found = next(((row, entry) for row, entry in self._channels() if row["id"] == cid), None)
        if path is None or found is None:
            return {}
        row, entry = found
        default = routecheck.DEFAULT_USER_AGENT
        lines = [(row["url"], row["agent"] or default)] + [
            (r["url"], r.get("ua") or default) for r in routes.clean_routes(entry.get("routes"), row["url"])]
        bad, lenient, quality = routes.bad_in(entry), routes.lenient_in(entry), routes.quality_in(entry)
        now = self.clock()
        fields: dict[str, dict] = {}
        live: dict[str, tuple[str, str]] = {}
        for url, agent in lines:
            mark = routes.fingerprint(url)
            if mark in bad:
                continue
            q = quality.get(mark, {})
            is_live = q.get("live")
            if not isinstance(q.get("pace_at"), (int, float)) or now - q["pace_at"] >= PACE_EVERY_S:
                measured = self.measure(url, agent)
                is_live = measured["live"]
                fields.setdefault(mark, {})["live"] = is_live
                if measured["pace"] is not None:
                    routes.remember_pace(path, cid, url, measured["pace"], now)
            if is_live:
                live[mark] = (url, agent)
        thumbs: dict[str, list[bytes]] = {}
        grabbers = []
        for mark, (url, agent) in live.items():
            def take(mark=mark, url=url, agent=agent):
                found = self.grab(self.ffmpeg, url, agent, lenient=mark in lenient)
                if found is not None:
                    thumbs[mark] = found
            grabber = threading.Thread(target=take, daemon=True)
            grabber.start()
            grabbers.append(grabber)
        for grabber in grabbers:
            grabber.join(THUMB_TIMEOUT_S + 5)
        at = self.clock()
        odd = judge_odd(thumbs)
        names = {routes.fingerprint(url): n for n, (url, _a) in enumerate(lines, 1)}
        for mark, thumb in thumbs.items():
            mine = fields.setdefault(mark, {})
            before = self._thumbs.get((cid, mark))
            if before is not None and at - before[1] >= STILL_GAP_S:
                apart = distance(thumb, before[0])
                mine["still"] = frozen(thumb) and apart is not None and apart < STILL_MAD
                self._thumbs[(cid, mark)] = (thumb, at)
            elif before is None:
                self._thumbs[(cid, mark)] = (thumb, at)
            mine["odd"] = odd.get(mark)
            q = quality.get(mark, {})
            if mine.get("still") and not q.get("still"):
                self.log(f"后台检查线路质量：{row['name']} 线路 {names[mark]} 画面一直不变，排到最后")
            if mine["odd"] and routes.odd_count(q) == routes.ODD_MIN - 1:
                self.log(f"后台检查线路质量：{row['name']} 线路 {names[mark]} 和同台其他线路的画面对不上，排到最后")
        if fields:
            routes.record_quality(path, {cid: fields})
        return fields
