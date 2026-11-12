"""片库网盘文件的「预读开头」：点播放之前先把要放的那一段下到本机（整集缓存，libraryfull.py），点下去马上有画面。

为什么：4K 的片子码率 10–25 Mbit/s，夸克一个连接给 4–78 Mbit/s；网盘比码率慢的时候电脑播放器要先攒 12 秒
（console.js 的 VOD_GATE），等 30–40 秒。先下好开头的 PRE_S 秒，起播从本机读，后面边放边由整集缓存的下载线程接着下。

预读什么（数目固定，和片库多大无关）：
- 后台（why "idle"）：「继续观看」里最近的 CONTINUE_MAX 部，从上次看到的地方起；正在追的剧的下一集，至多 NEXT_MAX
  部，从头起。只在没人放、没人看、没在扫描、整集缓存的下载线程歇着时做（Library.warm_blocked），有人开始放就停下。
- 打开详情页（why "detail"）：那一部（剧是「播放」会放的那一集），从这个人上次看到的地方起。有人正在放片库的片子时不做。
- 一集快放完（why "next"，离结尾不到 NEXT_NEAR_S 秒）：下一集从头起。
detail、next 排在后台的前面；新的 detail 顶掉还没开始的旧 detail。

怎么读：起一个 ffmpeg，和真放时一样从 -ss 那一秒（往前多 LEAD_S 秒）读 PRE_S 秒、只复制不转码、输出丢掉（-f null），
它读文件走 /api/library/raw 的 p=1（Library._serve_pre）：本机有的从本机给，没有的从网盘读并存进整集缓存。读到的
字节就是真放时要读的那些（索引区、跳到的那个关键帧、后面 PRE_S 秒）。CPU 几乎不用。
直接放原文件的（浏览器自己按 Range 取，不经整集缓存，Library.likely_direct）不预读；只预读网盘上的。

预读过的（文件、位置按 BUCKET_S 归一）记在内存里，不重复读；重启后再读一遍时多半都在本机，几乎不读网盘。
失败了 RETRY_S 内不再试。每一次至多 RUN_MAX_S 秒、BYTES_MAX 字节。

日志只写片名、位置、读了多少、网盘多快，从不写地址、令牌、路径。

只用标准库。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable

from ..proxyenv import loopback_bypass

MB = 1024 * 1024
# 预读多少秒（起播要攒 12 秒，多留一点）；续播位置往前多读几秒（-ss 落在之前的关键帧上）。
PRE_S = 20
LEAD_S = 5
# 后台预读：「继续观看」的前几部、正在追的剧的下一集至多几部。
CONTINUE_MAX = 8
NEXT_MAX = 4
# 一集离结尾不到这么多秒时预读下一集。
NEXT_NEAR_S = 240
# 继续观看的规则（和 home.PROGRESS_MIN_S、PROGRESS_END_S 一样）：看了这么久才算，离结尾这么近算看完了。
PROGRESS_MIN_S = 30
PROGRESS_END_S = 30
# 后台隔这么久看一次（详情页、快放完的请求马上叫醒）；中转起来后先等这么久（网盘组件还在起）。
TICK_S = 60.0
START_S = 90.0
# 一次预读至多这么久、这么多字节（-t 已经限住了，这是兜底）。
RUN_MAX_S = 120.0
BYTES_MAX = 400 * MB
# 位置按这么多秒归一（续播位置挪了一点不重读）。
BUCKET_S = 30
# 失败了这么久不再试。
RETRY_S = 600.0
# 预读过的至多记这么多个（最旧的先忘）。
DONE_MAX = 500


class PreRead:
    """library 是 Library（item、media_of、raw_url、ffmpeg、full、warm_blocked、remote、likely_direct、episodes_of、
    _full_name、_serving）；progress() 给 home.PcProgress（attach 接上；没有时 None）。"""

    def __init__(self, library, log: Callable[[str], None] = print, clock=time.time, background: bool = True):
        self.library = library
        self.log = log
        self.clock = clock
        self.background = background
        self.progress: Callable[[], object] = lambda: None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._urgent: list[tuple[str, float, str]] = []      # (条目 id, 秒, why)，先来的先做
        self._done: dict[str, float] = {}
        self._failed: dict[str, float] = {}
        self._last: dict[str, dict] = {}                      # 条目 id → 最近一次预读好的 {at, bytes, secs}
        self._job: dict | None = None                         # 正在做的 {iid, why, stop}
        self._proc: subprocess.Popen | None = None
        self._read = 0
        self._closed = False
        self._thread: threading.Thread | None = None
        self._since = time.monotonic()

    # ---- 给 Library._serve_pre ----

    def tally(self, count: int) -> None:
        with self._lock:
            self._read += count

    # 只数从网盘读的（和预热一样，_serve_pre 从本机给的不数）。
    tally.drive_only = True

    def busy(self) -> bool:
        """正在预读（预热不和它抢网）。"""
        with self._lock:
            return self._job is not None

    def stopping(self) -> bool:
        with self._lock:
            return self._closed or self._job is None or self._job["stop"]

    # ---- 外面叫的 ----

    def yield_to_playback(self, iid: str) -> None:
        """有人开始真放（ffmpeg 或浏览器读片库的文件）：正在做的预读停下，别和它抢网盘。只有「下一集」的预读在
        放的是别的（就是快放完的这一集）时接着做；放的正是在预读的这个时也停（真放的接着读，预读好的从本机给）。"""
        with self._lock:
            job = self._job
            if job is not None and (job["why"] != "next" or job["iid"] == iid):
                job["stop"] = True

    def want(self, iid: str, at: float, why: str) -> bool:
        """detail / next：排在后台的前面马上做。新的 detail 顶掉还没开始的 detail。返回排上没有。"""
        item = self.library.item(iid)
        if item is None or not self._eligible(item):
            return False
        with self._lock:
            if why == "detail":
                self._urgent = [u for u in self._urgent if u[2] != "detail"]
            self._urgent = [u for u in self._urgent if u[0] != iid]
            self._urgent.append((iid, max(0.0, float(at or 0)), why))
            job = self._job
            if job is not None and job["why"] == "idle":
                job["stop"] = True                  # 后台的让给它
        self._wake.set()
        self.start()
        return True

    def near_end(self, iid: str, position: float, duration: float) -> None:
        """一集放到了 position（共 duration 秒）：离结尾不到 NEXT_NEAR_S 秒时预读下一集的开头。"""
        if not duration or duration - position > NEXT_NEAR_S:
            return
        nxt = self.next_of(iid)
        if nxt is not None and not self._is_done(nxt, 0.0):
            self.want(nxt["id"], 0.0, "next")

    def next_of(self, iid: str) -> dict | None:
        item = self.library.item(iid)
        if item is None or item.get("kind") != "episode":
            return None
        return self.library.neighbor(item, 1)        # 跳过同一集的其他版本

    def resume_at(self, user, iid: str) -> float:
        """这个人上次看到哪（pc-progress.json 的 lib:<id>）；没看过、看完了时 0。"""
        store = self.progress()
        rec = store.get(user, "lib:" + iid) if store is not None and user is not None else None
        if not rec:
            return 0.0
        position, duration = float(rec.get("position") or 0), float(rec.get("duration") or 0)
        if position < PROGRESS_MIN_S or (duration and position > duration - PROGRESS_END_S):
            return 0.0
        return position

    def pick(self, user, show: str) -> str | None:
        """打开一部剧的详情页时预读哪一集（show 是 Library.show_key）：这个人最近看的那一集；看完了是下一集（最后一集
        看完了不读）；这部剧一集都没看过是第一集。没有这部剧时 None。"""
        episodes = [i for i in self.library.items()
                    if i.get("kind") == "episode" and self.library.show_key(i.get("show") or "") == show]
        if not episodes:
            return None
        episodes = self.library.episodes_of(episodes[0])
        store = self.progress()
        ids = {"lib:" + ep["id"] for ep in episodes}
        rows = [(key, rec) for key, rec in store.rows(user) if key in ids] if store is not None and user is not None else []
        if not rows:
            return episodes[0]["id"]
        key, rec = rows[0]                          # rows 是最近的在前
        position, duration = float(rec.get("position") or 0), float(rec.get("duration") or 0)
        if duration and position > duration - PROGRESS_END_S:
            nxt = self.next_of(key[4:])
            return nxt["id"] if nxt is not None else None
        return key[4:]

    def last(self, iid: str) -> dict | None:
        """这个条目最近一次预读好的 {at, bytes, secs}（起播的日志说「开头预读过」）。"""
        with self._lock:
            found = self._last.get(iid)
            return dict(found) if found else None

    # ---- 后台挑哪些 ----

    def targets(self) -> list[tuple[str, float]]:
        """后台要预读的 (条目 id, 秒)：所有人「继续观看」里最近的 CONTINUE_MAX 部（一部剧只算最近的那一集），
        再是最近看的剧的下一集至多 NEXT_MAX 部。"""
        store = self.progress()
        if store is None:
            return []
        rows = []
        for user in store.users():
            rows += [(rec, key[4:]) for key, rec in store.rows(user) if key.startswith("lib:")]
        rows.sort(key=lambda r: -float(r[0].get("at") or 0))
        found: list[tuple[str, float]] = []
        shows, seen = set(), set()
        for rec, iid in rows:
            if len(found) >= CONTINUE_MAX:
                break
            item = self.library.item(iid)
            if item is None or iid in seen:
                continue
            position, duration = float(rec.get("position") or 0), float(rec.get("duration") or 0)
            if position < PROGRESS_MIN_S or (duration and position > duration - PROGRESS_END_S):
                continue
            if item.get("kind") == "episode":
                show = self.library.show_key(item.get("show") or "")
                if show in shows:
                    continue
                shows.add(show)
            seen.add(iid)
            found.append((iid, position))
        nexts, shows = 0, set()
        for rec, iid in rows:
            if nexts >= NEXT_MAX:
                break
            item = self.library.item(iid)
            if item is None or item.get("kind") != "episode":
                continue
            show = self.library.show_key(item.get("show") or "")
            if show in shows:
                continue
            shows.add(show)
            nxt = self.next_of(iid)
            if nxt is not None and nxt["id"] not in seen:
                seen.add(nxt["id"])
                found.append((nxt["id"], 0.0))
                nexts += 1
        return [(iid, at) for iid, at in found if self._eligible(self.library.item(iid) or {})]

    # ---- 做 ----

    def _eligible(self, item: dict) -> bool:
        lib = self.library
        return bool(item) and int(item.get("size") or 0) > 0 and lib.full.enabled() and bool(lib.ffmpeg) \
            and lib.secret is not None and lib.port is not None and lib.remote(item) \
            and not lib.likely_direct(item, item.get("media"))

    def _key(self, item: dict, at: float) -> str:
        return f"{self.library.full.key_of_item(item)}@{int(at // BUCKET_S)}"

    def _is_done(self, item: dict, at: float) -> bool:
        key = self._key(item, at)
        with self._lock:
            return key in self._done or self.clock() - self._failed.get(key, float("-inf")) < RETRY_S

    def _busy_elsewhere(self) -> bool:
        """有人在放片库的片子、在电脑上看别的：后台的预读让开（扫描、整集缓存不算，扫描只列目录）。"""
        lib = self.library
        return lib._serving > 0 or (lib.watching is not None and lib.watching())

    def _next_job(self) -> tuple[str, float, str] | None:
        with self._lock:
            while self._urgent:
                iid, at, why = self._urgent.pop(0)
                item = self.library.item(iid)
                if item is None:
                    continue
                if why == "detail" and self.library._serving > 0:
                    continue                        # 有人在放片库的片子：详情页的不和它抢
                return iid, at, why
        if time.monotonic() - self._since < START_S or self.library.warm_blocked():
            return None
        for iid, at in self.targets():
            item = self.library.item(iid)
            if item is not None and not self._is_done(item, at):
                return iid, at, "idle"
        return None

    def step(self) -> bool:
        """做一个（有要做的时）：返回做了没有。"""
        job = self._next_job()
        if job is None:
            return False
        self.run(*job)
        return True

    def run(self, iid: str, at: float, why: str) -> str:
        """预读一个：起 ffmpeg 从 at 读 PRE_S 秒。返回 "done" / "skip"（不用读、读过了）/ "stopped" / "failed"。"""
        lib = self.library
        item = lib.item(iid)
        if item is None or not self._eligible(item):
            return "skip"
        media = lib.media_of(iid, warm=True)       # 第一次：先探测（读的多半是索引区，预热也会做）
        if media is None:
            return "failed"
        if lib.likely_direct(item, media):
            return "skip"
        duration = float(media.get("dur") or 0)
        if duration and at >= duration - 5:
            at = 0.0
        if self._is_done(item, at):
            return "skip"
        key, name = self._key(item, at), lib._full_name(item)
        start = max(0.0, at - LEAD_S)
        args = [lib.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
                "-protocol_whitelist", "http,tcp"]
        if start > 0:
            args += ["-ss", f"{start:.3f}"]
        args += ["-i", lib.raw_url(iid, pre=True), "-map", "0:V:0?", "-map", "0:a?", "-sn", "-dn",
                 "-t", str(PRE_S + (at - start)), "-c", "copy", "-f", "null", "-"]
        with self._lock:
            if self._closed:
                return "stopped"
            self._job = {"iid": iid, "why": why, "stop": False}
            self._read = 0
        began = time.monotonic()
        outcome = "failed"
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=loopback_bypass(os.environ))
        except OSError:
            proc = None
        if proc is not None:
            with self._lock:
                self._proc = proc
            while proc.poll() is None:
                with self._lock:
                    stop = self._closed or self._job["stop"] or self._read > BYTES_MAX
                if not stop and why == "idle" and self._busy_elsewhere():
                    stop = True                     # 后台的：有人开始放、开始看了
                if stop or time.monotonic() - began > RUN_MAX_S:
                    proc.kill()
                    proc.wait()
                    outcome = "stopped"
                    break
                time.sleep(0.2)
            else:
                outcome = "done" if proc.returncode == 0 else "failed"
            with self._lock:
                self._proc = None
                if self._job is not None and self._job["stop"]:
                    outcome = "stopped"
        took = time.monotonic() - began
        with self._lock:
            read, self._job = self._read, None
            if outcome == "done":
                self._done[key] = self.clock()
                while len(self._done) > DONE_MAX:
                    self._done.pop(min(self._done, key=self._done.get))
                self._last[iid] = {"at": at, "bytes": read, "secs": round(took, 1)}
            elif outcome == "failed":
                self._failed[key] = self.clock()
        where = f"从 {int(at) // 60}:{int(at) % 60:02d} 起" if at > 0 else "从头"
        label = {"idle": "后台", "detail": "打开详情页", "next": "下一集"}.get(why, why)
        if outcome == "done" and read:
            speed = f"，网盘约 {read * 8 / took / 1e6:.0f} Mbit/s" if took > 0.5 else ""
            self.log(f"片库预读（{label}）：「{name}」{where} {PRE_S} 秒，读了 {read // MB} MB，用了 {took:.0f} 秒{speed}")
        elif outcome == "stopped" and read:
            self.log(f"片库预读（{label}）：「{name}」读了 {read // MB} MB 停下了（有人开始放了）")
        elif outcome == "failed":
            self.log(f"片库预读（{label}）：「{name}」没读成，{int(RETRY_S // 60)} 分钟内不再试")
        return outcome

    def run_forever(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                did = self.step()
            except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                self.log(f"片库预读出错：{type(error).__name__}")
                did = False
            if not did:
                self._wake.wait(TICK_S)
                self._wake.clear()

    def start(self) -> None:
        with self._lock:
            if not self.background or self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self.run_forever, name="library-pre", daemon=True)
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._job is not None:
                self._job["stop"] = True
            proc = self._proc
        self._wake.set()
        if proc is not None and proc.poll() is None:
            proc.kill()
