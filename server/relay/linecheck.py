"""直播台线路的后台自检，和顺便取的直播封面（自建 / 电脑客户端；云端的中转不开）。

不用人去总后台一个个点「检测」：中转在后台按时间表把全局频道表里每个台的每条线路测一遍，结果写进
channels.txt.meta.json 的 checks（server/routes.py），和总后台的「检测线路」是同一份，所以排线路的顺序
（快的、好的先试，连不上的最后）在电脑上和电视上自动用上。

- 测法和总后台的一样轻（routecheck.probe：取列表和一个片段的头几 KB，不起 ffmpeg）。
- 时间表：没测过的线路（刚导入的）启动 START_DELAY_S 秒后、再错开 0–NEW_JITTER_S 秒就测；上次连不上的
  BAD_EVERY_S 后再测；能连上的 OK_EVERY_S 后再测。先测没测过的，再测坏的，再测最久没测的。
- 同时至多 WORKERS 条，还和总后台的「检测」排同一个队（routecheck 的两个名额）。
- 机器忙的时候不测：在电脑上看的（含预热、点播）和在线的小电视合起来有 PAUSE_STREAMS 路以上时暂停。
- 日志只写台名、第几条线路和去掉地址的原话（routes.redact），从不写地址；只在一条线路从好变坏时写一行。

直播封面：直播页的卡片原来是按台名生成的图；能连上的线路顺便取一帧真画面（ffmpeg，缩到 COVER_WIDTH
宽的 JPEG，至多 COVER_TIMEOUT_S 秒），存在数据目录的 covers/live/<台 id>.jpg，由 GET /api/art/live/<台 id>
给登录了的人看。同时至多一个 ffmpeg、两次之间至少隔 COVER_GAP_S 秒，一个台 COVER_EVERY_S 内至多取一次；
在电脑上看着东西、或者机器忙时不取。整个目录至多 COVERS_MAX_FILES 个、COVERS_MAX_BYTES 字节，多了删最旧的，
频道表里没有了的台的封面也删。画面只存在本机、只给这一家看，不上传到任何地方。
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .. import routes
from ..proxyenv import loopback_bypass
from . import channeltable, routecheck

START_DELAY_S = 60.0
NEW_JITTER_S = 120.0
OK_EVERY_S = 6 * 3600.0
BAD_EVERY_S = 30 * 60.0
TICK_S = 20.0
WORKERS = 2
# 一个名额测完一条、接着测下一条之前歇这么久（刚导入几百个台时也不一口气连个不停）。
JOB_GAP_S = 1.0
PAUSE_STREAMS = 3
COVER_EVERY_S = 4 * 3600.0
# 取不到封面（源站慢、没有画面）的台过这么久再试。
COVER_RETRY_S = 3600.0
COVER_GAP_S = 20.0
COVER_TIMEOUT_S = 15
COVER_WIDTH = 480
COVER_MAX_BYTES = 300 * 1024
COVERS_MAX_FILES = 800
COVERS_MAX_BYTES = 48 * 1024 * 1024
# 封面目录的列表（台 id → 修改时间）缓存几秒：/api/state 每几秒就问一次。
STAMPS_S = 10.0
_CID = re.compile(r"^[A-Za-z0-9_-]{1,15}$")


def grab_frame(ffmpeg: str, url: str, agent: str = "", run=subprocess.run, lenient: bool = False) -> bytes | None:
    """取一帧画面，缩到 COVER_WIDTH 宽的 JPEG（和总后台「试一下」一样只取一帧，playlist.probe）。取不到为 None。
    lenient：片段地址不标准的线路（routes.LENIENT_ARGS）。"""
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "quiet",
               "-rw_timeout", str((COVER_TIMEOUT_S - 3) * 1000000), "-user_agent", agent or routecheck.DEFAULT_USER_AGENT,
               *(routes.LENIENT_ARGS if lenient else ()),
               "-i", url, "-map", "0:v:0", "-frames:v", "1", "-threads", "1",
               "-vf", f"scale={COVER_WIDTH}:-2", "-q:v", "6", "-c:v", "mjpeg", "-f", "image2pipe", "-"]
    try:
        # 和在电脑上看一样（watch.py）：本机的地址不走代理。
        done = run(command, capture_output=True, timeout=COVER_TIMEOUT_S, env=loopback_bypass(os.environ))
    except (subprocess.TimeoutExpired, OSError):
        return None
    body = done.stdout or b""
    return body if body.startswith(b"\xff\xd8\xff") and 200 < len(body) <= COVER_MAX_BYTES else None


class CoverStore:
    """covers/live/ 目录：每个台一张 <台 id>.jpg，新的换掉旧的；总数、总大小有上限。"""

    def __init__(self, folder: Path, monotonic=time.monotonic):
        self.folder = Path(folder)
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._stamps: tuple[float, dict[str, int]] | None = None

    def path(self, cid) -> Path | None:
        return self.folder / f"{cid}.jpg" if isinstance(cid, str) and _CID.match(cid) else None

    def get(self, cid) -> bytes | None:
        path = self.path(cid)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def stamps(self) -> dict[str, int]:
        """{台 id: 封面的修改时间（秒）}：页面拿它拼地址（?v=…，换了封面浏览器就重新取）。"""
        with self._lock:
            if self._stamps is not None and self.monotonic() - self._stamps[0] < STAMPS_S:
                return self._stamps[1]
        found = {}
        try:
            for item in os.scandir(self.folder):
                cid = item.name[:-4]
                if item.name.endswith(".jpg") and _CID.match(cid):
                    found[cid] = int(item.stat().st_mtime)
        except OSError:
            pass
        with self._lock:
            self._stamps = (self.monotonic(), found)
        return found

    def stamp(self, cid: str) -> int:
        return self.stamps().get(cid, 0)

    def put(self, cid: str, body: bytes, known: set[str] | None = None) -> bool:
        path = self.path(cid)
        if path is None or not body.startswith(b"\xff\xd8\xff") or len(body) > COVER_MAX_BYTES:
            return False
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = self.folder / f".{cid}.tmp"
            tmp.write_bytes(body)
            tmp.replace(path)
        except OSError:
            return False
        self.prune(known)
        return True

    def prune(self, known: set[str] | None = None) -> None:
        """删掉频道表里没有了的台（known 给了时）的封面；超过上限时从最旧的删起。"""
        try:
            items = [(e.stat().st_mtime, e.stat().st_size, e.name) for e in os.scandir(self.folder)
                     if e.name.endswith(".jpg") or e.name.endswith(".tmp")]
        except OSError:
            return
        keep = []
        for mtime, size, name in items:
            stale = name.endswith(".tmp") and time.time() - mtime > 600
            if stale or (known is not None and name.endswith(".jpg") and name[:-4] not in known):
                self._remove(name)
            elif name.endswith(".jpg"):
                keep.append((mtime, size, name))
        keep.sort()
        total = sum(size for _, size, _ in keep)
        while keep and (len(keep) > COVERS_MAX_FILES or total > COVERS_MAX_BYTES):
            _, size, name = keep.pop(0)
            total -= size
            self._remove(name)
        with self._lock:
            self._stamps = None

    def _remove(self, name: str) -> None:
        try:
            (self.folder / name).unlink()
        except OSError:
            pass


class LineChecker:
    """后台自检。table() 给全局频道表的路径；load() 给 (现在在放的路数, 其中在电脑上放的路数)。
    tick() 每 TICK_S 秒由后台线程调一次（测试里直接调，spawn 换成同步执行）。"""

    def __init__(self, table: Callable[[], Path | None], covers: CoverStore | None = None, ffmpeg: str = "ffmpeg",
                 load: Callable[[], tuple[int, int]] = lambda: (0, 0), probe=routecheck.probe, grab=grab_frame,
                 clock=time.time, rand=random.random, log=print, spawn=None, gate=None, pause=time.sleep):
        self.table = table
        self.covers = covers
        self.ffmpeg = ffmpeg
        self.load = load
        self.probe = probe
        self.grab = grab
        self.clock = clock
        self.rand = rand
        self.log = log
        self.spawn = spawn or (lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True,
                                                                  name="linecheck").start())
        # 和总后台的「检测」共用的两个名额（routecheck._running）。
        self.gate = gate if gate is not None else routecheck._running
        self.pause = pause
        # 到点了、还没开始测的线路（tick 排好，测完一条的名额接着取下一条）。
        self._queue: list[tuple] = []
        self.started = clock()
        self._lock = threading.Lock()
        # 没测过的线路什么时候测：(台, 指纹) → 时刻（第一次看到它时定，错开一点）。
        self._first: dict[tuple[str, str], float] = {}
        self._running: set[tuple[str, str]] = set()
        # 等着取封面的台：台 → (地址, UA)（刚测过能连上的那条）；上次去取的时刻。
        self._cover_wait: dict[str, tuple[str, str]] = {}
        self._cover_tried: dict[str, float] = {}
        self._cover_busy = False
        self._cover_last = 0.0
        self._rows: tuple[tuple, list[dict]] | None = None
        # 测完了还没写进 meta 的结果：{台 id: [(指纹, ok, why, ms, at)]}，每次 tick 一起写一次（flush）。
        self._results: dict[str, list[tuple]] = {}
        # 上次 plan 读到的 meta（判断一条线路是不是刚变坏，好只在那时写一行日志）。
        self._meta: dict = {}
        # 上次 plan 时频道表里的线路 {(台, 指纹)}：测的时候管理员改了频道表，删掉的线路的结果不写回去。
        self._seen: set[tuple[str, str]] | None = None
        self._summary: dict = {"on": True, "last": 0, "dead": [], "total": 0, "checked": 0, "paused": False}
        self._last = 0
        # 线路的真实速度和画面（linequality.py）：attach 时建，和取封面一样只在机器不忙、没人在电脑上看时做。
        self.quality = None

    # ---- 读频道表（没变不重读）----

    def _table_rows(self, path: Path) -> list[dict]:
        try:
            info = path.stat()
            stamp = (str(path), info.st_mtime_ns, info.st_size)
        except OSError:
            return []
        if self._rows is None or self._rows[0] != stamp:
            self._rows = (stamp, channeltable.read(path)["rows"])
        return self._rows[1]

    @staticmethod
    def _lines(row: dict, entry: dict) -> list[tuple[str, str]]:
        """这个台的所有线路（地址, UA），和 routes.table_routes 一样，只是 meta 已经读好了。"""
        default = routecheck.DEFAULT_USER_AGENT
        found = [(row["url"], row["agent"] or default)]
        for route in routes.clean_routes(entry.get("routes"), row["url"]):
            found.append((route["url"], route.get("ua") or default))
        return found

    # ---- 时间表 ----

    def plan(self, now: float | None = None) -> list[tuple]:
        """到点了的线路，先后顺序排好：[(先后, 到点时刻, 台 id, 台名, 第几条, 地址, UA)]。顺便算 summary。"""
        now = self.clock() if now is None else now
        path = self.table()
        if path is None:
            return []
        rows = self._table_rows(path)
        meta = routes.read_meta(path)
        with self._lock:
            self._meta = meta
            # 刚测完、还没写进 meta 的：不再排进去。
            fresh = {(cid, item[0]) for cid, items in self._results.items() for item in items}
        due, dead, checked, seen = [], [], 0, set()
        for row in rows:
            entry = meta.get(row["id"], {})
            checks = entry.get("checks") if isinstance(entry.get("checks"), dict) else {}
            lines = self._lines(row, entry)
            states = []
            for n, (url, agent) in enumerate(lines, 1):
                mark = routes.fingerprint(url)
                key = (row["id"], mark)
                seen.add(key)
                found = checks.get(mark) if isinstance(checks.get(mark), dict) else None
                at = found.get("at") if found is not None and isinstance(found.get("at"), (int, float)) else None
                if found is None or at is None or not isinstance(found.get("ok"), bool):
                    states.append(None)
                    with self._lock:
                        when = self._first.setdefault(key, max(now, self.started + START_DELAY_S)
                                                      + self.rand() * NEW_JITTER_S)
                    rank = 0
                else:
                    states.append(found["ok"])
                    when = at + (OK_EVERY_S if found["ok"] else BAD_EVERY_S)
                    rank = 2 if found["ok"] else 1
                if when <= now and key not in fresh:
                    due.append((rank, when, row["id"], row["name"], n, url, agent))
            if states and None not in states:
                checked += 1
                if not any(states):
                    dead.append(row["id"])
        with self._lock:
            self._seen = seen
            for key in [k for k in self._first if k not in seen]:
                del self._first[key]
            self._summary = {"on": True, "last": self._last, "dead": dead, "total": len(rows), "checked": checked,
                             "paused": self._summary.get("paused", False)}
        due.sort()
        return due

    def flush(self) -> None:
        """攒着的检测结果一次写进 meta。"""
        path = self.table()
        with self._lock:
            found, self._results = self._results, {}
            seen = self._seen
        if seen is not None:
            found = {cid: [item for item in items if (cid, item[0]) in seen] for cid, items in found.items()}
            found = {cid: items for cid, items in found.items() if items}
        if found and path is not None:
            routes.record_many(path, found)

    def tick(self) -> int:
        """测到点了的线路（至多 WORKERS 条同时在测），再看要不要取一张封面。返回这次开始测了几条。"""
        now = self.clock()
        self.flush()
        due = self.plan(now)
        busy, pc = self.load()
        paused = busy >= PAUSE_STREAMS
        with self._lock:
            self._summary["paused"] = paused
        waiting = paused or now < self.started + START_DELAY_S
        with self._lock:
            self._queue = [] if waiting else due
        if waiting:
            return 0
        started = 0
        while self._start_next():
            started += 1
        if not pc and busy < 2:
            self._cover_step(now)
            if self.quality is not None:
                self.quality.step(now)
        return started

    def _start_next(self) -> bool:
        """有空的名额就从排好的里取下一条去测（正在测的、刚测完还没写进 meta 的跳过）。"""
        with self._lock:
            if len(self._running) >= WORKERS:
                return False
            while self._queue:
                job = self._queue.pop(0)
                key = (job[2], routes.fingerprint(job[5]))
                done = any(item[0] == key[1] for item in self._results.get(key[0], ()))
                if key not in self._running and not done:
                    break
            else:
                return False
            self._running.add(key)
        self.spawn(self._check, job, key)
        return True

    def _check(self, job: tuple, key: tuple[str, str]) -> None:
        _rank, _when, cid, name, n, url, agent = job
        try:
            path = self.table()
            if path is None:
                return
            with self.gate:
                found = self.probe(url, agent)
            at = self.clock()
            with self._lock:
                before = self._meta.get(cid, {}).get("checks", {})
            before = before.get(key[1]) if isinstance(before, dict) else None
            if not found["ok"] and not (isinstance(before, dict) and before.get("ok") is False):
                self.log(f"后台检测线路：{name} 线路 {n} 不通：{found.get('raw') or found.get('why')}")
            with self._lock:
                self._results.setdefault(cid, []).append(
                    (key[1], found["ok"], found.get("why", ""), found.get("ms", 0), at))
                self._last = int(at)
                self._summary["last"] = self._last
                if found["ok"] and self.covers is not None and self._cover_due(cid, at):
                    self._cover_wait[cid] = (url, agent)
        except Exception as error:  # noqa: BLE001 —— 后台线程不能死：记下来，下一轮再测
            self.log(f"后台检测线路出错：{type(error).__name__}")
        finally:
            with self._lock:
                self._running.discard(key)
                more = bool(self._queue)
        if more:
            # 接着测排好的下一条；机器这时忙起来了就先停，下一次 tick 再看。
            self.pause(JOB_GAP_S)
            if self.load()[0] >= PAUSE_STREAMS:
                with self._lock:
                    self._queue = []
            else:
                self._start_next()

    # ---- 封面 ----

    def _cover_due(self, cid: str, now: float) -> bool:
        stamp = self.covers.stamp(cid) if self.covers is not None else 0
        tried = self._cover_tried.get(cid, 0.0)
        return now - stamp >= COVER_EVERY_S and now - tried >= COVER_RETRY_S

    def _cover_step(self, now: float) -> None:
        with self._lock:
            if self._cover_busy or not self._cover_wait or now - self._cover_last < COVER_GAP_S:
                return
            cid = next(iter(self._cover_wait))
            url, agent = self._cover_wait.pop(cid)
            self._cover_busy = True
            self._cover_last = now
            self._cover_tried[cid] = now
        self.spawn(self._grab, cid, url, agent)

    def _grab(self, cid: str, url: str, agent: str) -> None:
        try:
            body = self.grab(self.ffmpeg, url, agent, lenient=routes.is_lenient(self.table(), url))
            if body is not None and self.covers is not None:
                path = self.table()
                known = {row["id"] for row in self._table_rows(path)} if path is not None else None
                self.covers.put(cid, body, known)
        except Exception as error:  # noqa: BLE001
            self.log(f"取直播封面出错：{type(error).__name__}")
        finally:
            with self._lock:
                self._cover_busy = False

    # ---- 给页面 ----

    def status(self) -> dict:
        """总后台和直播页看的：{on, last（上次测完一条的时刻）, dead（全部线路都不通的台）, total, checked, paused}。"""
        with self._lock:
            return dict(self._summary, dead=list(self._summary["dead"]))

    def run_forever(self, sleep=time.sleep) -> None:
        while True:
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 —— 后台线程不能死
                self.log(f"后台检测线路出错：{type(error).__name__}")
            sleep(TICK_S)


def machine_load(relay) -> tuple[int, int]:
    """(现在在放的路数, 其中在电脑上放的)：在电脑上看的（含预热的、点播的）和在线的小电视。"""
    pc = 0
    watch = getattr(relay, "watch", None)
    if watch is not None:
        pc = sum(1 for s in watch.streams() if s.alive) + len(watch.warm_streams()) + len(watch.also())
    tv = 0
    for dev in list(getattr(relay, "devices", []) or []):
        try:
            tv += 1 if relay._playing(dev).get("online") else 0
        except Exception:  # noqa: BLE001 —— 设备文件读不了就当它不在线
            continue
    return pc + tv, pc


def attach(relay, folder: Path, start: bool = True) -> LineChecker | None:
    """自建 / 电脑客户端：建后台自检和封面目录，挂在 relay.linecheck 上。云端（没有 watch）不建。"""
    if getattr(relay, "watch", None) is None or relay.main_channels is None:
        return None
    covers = CoverStore(folder)
    checker = LineChecker(lambda: relay.main_channels, covers, relay.ffmpeg, load=lambda: machine_load(relay),
                          log=lambda message: relay.log(message))
    from .linequality import Sweeper
    checker.quality = Sweeper(lambda: relay.main_channels, relay.ffmpeg, log=lambda message: relay.log(message))
    relay.linecheck = checker
    relay.watch.routes.checking = True
    if start:
        threading.Thread(target=checker.run_forever, name="linecheck", daemon=True).start()
    return checker
