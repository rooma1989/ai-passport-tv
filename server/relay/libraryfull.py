"""片库网盘文件的「整集缓存」：边看边把整个文件下到本机，跳转到下好的地方从本机读，几乎马上就有画面。

为什么：网盘上的片子每次起播、每次跳转都重起一个 ffmpeg（watch.py；小电视那一路也一样），它要从网盘再读 3–12 MB
（4K HEVC 的 MKV 一个 GOP 5 秒、约 10 Mbit/s）才出第一段。夸克的速度在 4–78 Mbit/s 之间变，跳一下要 2–16 秒。
索引区的缓存（librarycache.py）只存每次打开都要读的头和 Cues，画面本身还是每次从网盘读；这里把正在看的那一集
整个下到本机。

怎么下（下载线程，同一时刻只有一个，只下一个文件）：
- 放片子的请求（/api/library/raw，ffmpeg 读的；预热的探测不算）读到一个网盘文件时（touch），下载线程换到这个文件
  （上一个下了一半的留着），从放片子正在读的位置（anchor）起用一个网盘连接、一段一段（CHUNK，约 8 MB 一个 Range
  请求）往后下到文件尾，再从头下到起点：每次下的是 anchor 以后第一个还没下的地方，没有了就从文件头找。
- 放片子优先：放片子的请求要自己从网盘读时（on_drive）下载线程马上停下这一段（下好的留着），让出带宽；放片子
  读完这一阵（library.py 的 _serve_full 每次至多读 BURST）就把 anchor 设成它读到的地方，下载线程从那里接着下——
  跳到没下的地方时就从新的地方下起。下载线程赶在放片子前面以后，放片子都从本机读，不碰网盘。
- 放片子读到下载线程正在下的地方（next_at）时不自己去网盘读，等下载线程写进来（wait，边写边给）；STALL_S 内
  一点都没等到（网盘卡住、线程歇着）才自己去读。
- 放片子自己从网盘读到的也顺手写进来（tee），不会下两遍。
- 下完了停；这个文件 IDLE_S（10 分钟）没人读（没有放片子的请求开着）就歇着，再放时接着下；中转关掉时停下
  （stop）。数据文件先写、说明后写（只记已经写好的段），中途被关掉也不会把没写的当成下好了。

存在哪：library-full/<id>-<大小>-<修改时间>.bin（稀疏文件，按文件里的位置写）和 .json（下好的段 runs、名字、
最后用过的时间），都是 600、文件夹 700。网盘上的文件变了（重新扫描后大小或修改时间不一样）旧的整个作废；下载
或放片子从网盘读时总长对不上也整个作废（drop）。

上限：总共至多 limit（片库设置的 full_gb：关 / 10 / 30 / 100 GB，默认 30），超了按最后用过的时间删最旧的整个文件，
正在下、正在读的从不删；磁盘剩的空间会少于 FREE_MIN（10 GB）时不再往里存。关掉（limit 为 0）时没有下载线程、
已经存下的全删。只给网盘（WebDAV）的来源：本机文件夹本来就在本机，浏览器直接放的（/api/library/file）也不用。

日志只写片名（「片库：「名字」整集缓存好了」、删掉了哪个），从不写地址、令牌、密码、路径。

只用标准库。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

MB = 1024 * 1024
GB = 1024 * MB
# 片库设置里能选的（GB）：0 是关。
CHOICES = (0, 10, 30, 100)
DEFAULT_GB = 30
# 磁盘剩的空间不能因为整集缓存少于这么多。
FREE_MIN = 10 * GB
# 下载线程一个 Range 请求下这么多；一次至多读 PIECE 字节就看看要不要让给放片子。
CHUNK = 8 * MB
PIECE = 256 * 1024
# 放片子自己从网盘读时一阵至多读这么多（起播、跳转要 3–12 MB），之后交给下载线程。
BURST = 16 * MB
# 放片子等下载线程写进来，这么久一点都没等到就自己去网盘读。
STALL_S = 5.0
# 这么久没人读就歇着。
IDLE_S = 600.0
# 下载线程没事时隔这么久看一眼（有事时马上被叫醒）。
TICK_S = 5.0
# 网盘出错了歇这么久，接连出错翻倍，至多 BACKOFF_MAX_S。
BACKOFF_S = 15.0
BACKOFF_MAX_S = 600.0
# 磁盘剩多少空间至多隔这么久问一次系统。
DISK_CHECK_S = 2.0

_KEY = re.compile(r"^([0-9a-f]{16})-(\d{1,15})-(-?\d{1,15})$")


class Changed(Exception):
    """网盘上的文件和索引里的对不上（回答的总长不一样）：这个文件的整集缓存作废。"""


# ---------------------------------------------------------------- 下好的段（[起, 止)，排好、不重叠、不相连）

def add_run(runs: list[list[int]], start: int, end: int) -> None:
    """把 [start, end) 并进 runs。"""
    if end <= start:
        return
    merged, placed = [], False
    for a, b in runs:
        if b < start:
            merged.append([a, b])
        elif end < a:
            if not placed:
                merged.append([start, end])
                placed = True
            merged.append([a, b])
        else:
            start, end = min(a, start), max(b, end)
    if not placed:
        merged.append([start, end])
    merged.sort()
    runs[:] = merged


def run_end(runs: list[list[int]], at: int) -> int:
    """at 落在下好的一段里时这一段的止；不在时 at。"""
    for a, b in runs:
        if a <= at < b:
            return b
        if a > at:
            break
    return at


def gap_after(runs: list[list[int]], at: int, size: int):
    """at 以后（含 at）第一个还没下的地方：(起, 止)；到文件尾都下好了时 None。"""
    pos = max(0, at)
    for a, b in runs:
        if b <= pos:
            continue
        if a > pos:
            return pos, a
        pos = b
    return (pos, size) if pos < size else None


def next_gap(runs: list[list[int]], anchor: int, size: int):
    """下载线程接着下哪：anchor 以后第一个没下的地方，没有了从文件头找（绕回来）；整个下好了时 None。"""
    return gap_after(runs, anchor, size) or (gap_after(runs, 0, size) if anchor > 0 else None)


# ---------------------------------------------------------------- 缓存

class _File:
    __slots__ = ("key", "iid", "size", "mtime", "name", "used", "runs")

    def __init__(self, key: str, iid: str, size: int, mtime: int, name: str = ""):
        self.key, self.iid, self.size, self.mtime, self.name = key, iid, size, mtime, name
        self.used = 0.0
        self.runs: list[list[int]] = []

    def have(self) -> int:
        return sum(b - a for a, b in self.runs)

    def done(self) -> bool:
        return self.runs == [[0, self.size]]


class _Job:
    """下载线程手上的文件。state：空串在下；done 下完了；full / disk 地方不够；changed 网盘上变了。"""
    __slots__ = ("key", "item", "name", "anchor", "last", "state", "after", "backoff", "warned")

    def __init__(self, key: str, item: dict, name: str, anchor: int, now: float):
        self.key, self.item, self.name, self.anchor, self.last = key, item, name, anchor, now
        self.state = ""
        self.after = 0.0
        self.backoff = 0.0
        self.warned = False


class FullCache:
    """library-full/：网盘文件的整集缓存。opener(item, 起, 止) 打开网盘上这一段（含止，回答的总长对不上时抛 Changed）；
    alive(id) 给这个条目现在的 (大小, 修改时间)，没有了给 None；limit 是总共至多存多少字节（0 是关）。"""

    def __init__(self, root: Path, opener: Callable | None = None, alive: Callable[[str], tuple | None] | None = None,
                 log: Callable[[str], None] = print, clock=time.time, limit: int = DEFAULT_GB * GB,
                 disk_free: Callable[[], int] | None = None, background: bool = True):
        self.root = Path(root)
        self.opener = opener
        self.alive = alive
        self.log = log
        self.clock = clock
        self.limit = max(0, int(limit))
        self.disk_free = disk_free or self._disk_free
        self.background = background
        self.chunk, self.piece, self.burst = CHUNK, PIECE, BURST
        self.stall_s, self.idle_s, self.tick_s = STALL_S, IDLE_S, TICK_S
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._files: dict[str, _File] = {}
        self._readers: dict[str, int] = {}
        self._job: _Job | None = None
        self._drive = 0                   # 正在自己从网盘读的放片子请求
        self._at: tuple[str, int] | None = None     # 下载线程正在写的 (文件, 位置)
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._disk = (float("-inf"), 0)
        self._warned = False
        self._load()

    # ---- 文件 ----

    @staticmethod
    def key_of(iid: str, size: int, mtime: int) -> str:
        return f"{iid}-{int(size)}-{int(mtime)}"

    @classmethod
    def key_of_item(cls, item: dict) -> str:
        return cls.key_of(item["id"], int(item.get("size") or 0), int(item.get("mtime") or 0))

    def _data(self, f: _File) -> Path:
        return self.root / f"{f.key}.bin"

    def _meta(self, f: _File) -> Path:
        return self.root / f"{f.key}.json"

    def _load(self) -> None:
        if not self.root.is_dir():
            return
        for meta in self.root.glob("*.json"):
            match = _KEY.match(meta.stem)
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                if match is None or not isinstance(data, dict):
                    raise ValueError
                f = _File(meta.stem, match.group(1), int(match.group(2)), int(match.group(3)), str(data.get("name") or ""))
                f.used = float(data.get("used") or 0)
                path = self._data(f)
                on_disk = path.stat().st_size if path.exists() else 0
                for a, b in data.get("runs") or []:
                    a, b = int(a), min(int(b), on_disk, f.size)      # 说明里有、数据文件里没有的不算
                    if 0 <= a < b:
                        add_run(f.runs, a, b)
            except (OSError, ValueError, KeyError, TypeError):
                self._unlink(meta)
                continue
            self._files[f.key] = f
        known = {self._data(f).name for f in self._files.values()}
        for path in self.root.glob("*.bin"):
            if path.name not in known:
                self._unlink(path)
        for path in self.root.glob("*.tmp"):
            self._unlink(path)
        self.prune()

    def _save(self, f: _File) -> None:
        """写这个文件的说明（整份换掉写，600）。在 self._lock 里调。"""
        if self._files.get(f.key) is not f:
            return
        row = {"name": f.name, "used": round(f.used, 3), "runs": f.runs}
        path = self._meta(f)
        tmp = path.with_name(path.name + ".tmp")
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(row, handle, ensure_ascii=False, separators=(",", ":"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as error:
            self._warn(error)

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _warn(self, error: BaseException) -> None:
        if not self._warned:
            self._warned = True
            self.log(f"片库的整集缓存写不了（{type(error).__name__}），照旧从网盘读")

    def _forget(self, f: _File) -> None:
        """整个删掉一个文件的缓存。在 self._lock 里调。"""
        self._files.pop(f.key, None)
        self._unlink(self._data(f))
        self._unlink(self._meta(f))
        self._cond.notify_all()

    def _file(self, item: dict, name: str, create: bool) -> _File | None:
        """这个条目现在的缓存；同一个 id 大小或修改时间不一样的旧缓存作废（正在读的等读完）。在 self._lock 里调。"""
        key = self.key_of_item(item)
        for old in [f for f in self._files.values() if f.iid == item["id"] and f.key != key
                    and not self._readers.get(f.key)]:
            self._forget(old)
        f = self._files.get(key)
        size = int(item.get("size") or 0)
        if f is None and create and size > 0:
            f = self._files[key] = _File(key, item["id"], size, int(item.get("mtime") or 0), name)
            f.used = self.clock()
        if f is not None and name:
            f.name = name
        return f

    # ---- 设置、上限 ----

    def enabled(self) -> bool:
        return self.limit > 0

    def set_limit(self, limit: int) -> None:
        """改上限：0 是关（停下、已经存下的全删）；小了按最后用过的时间删到不超。"""
        with self._lock:
            self.limit = max(0, int(limit))
            if self.limit == 0:
                self._job = None
                for f in list(self._files.values()):
                    self._forget(f)
                if self.root.is_dir():
                    for path in list(self.root.glob("*.bin")) + list(self.root.glob("*.json")) \
                            + list(self.root.glob("*.tmp")):
                        self._unlink(path)
            elif self._job is not None and self._job.state in ("full", "disk"):
                self._job.state = ""                # 大了：接着下
            self._cond.notify_all()
        self.prune()

    def _disk_free(self) -> int:
        folder = self.root if self.root.is_dir() else self.root.parent
        return shutil.disk_usage(folder).free

    def _free(self) -> int:
        now = time.monotonic()
        if now - self._disk[0] >= DISK_CHECK_S:
            try:
                self._disk = (now, int(self.disk_free()))
            except OSError:
                self._disk = (now, 0)
        return self._disk[1]

    def _room(self, f: _File, need: int) -> str:
        """要再存 need 字节：超了上限先按最后用过的时间删别的（正在下、正在读的不删）；还不够时 "full"，
        磁盘剩的会少于 FREE_MIN 时 "disk"，够时空串。在 self._lock 里调。"""
        if not self.enabled():
            return "full"
        total = sum(x.have() for x in self._files.values())
        keep = {f.key, self._job.key if self._job is not None else ""}
        for old in sorted(self._files.values(), key=lambda x: x.used):
            if total + need <= self.limit:
                break
            if old.key in keep or self._readers.get(old.key):
                continue
            total -= old.have()
            self._forget(old)
            if old.name:
                self.log(f"片库：整集缓存满了，删掉了「{old.name}」的")
        if total + need > self.limit:
            return "full"
        if self._free() - need < FREE_MIN:
            return "disk"
        return ""

    def _block(self, job: _Job, why: str) -> None:
        """地方不够了：这个文件不再往下存（上限改大了、再放时再试）。在 self._lock 里调。"""
        if job.state != why:
            job.state = why
            self.log(f"片库：「{job.name}」没缓存完，" + ("整集缓存满了" if why == "full" else "磁盘快满了"))

    def prune(self) -> None:
        """索引里没有了、大小或修改时间变了的删；总共超过上限时按最后用过的时间删最旧的（正在下、正在读的不删）。"""
        with self._lock:
            for f in list(self._files.values()):
                if self._readers.get(f.key):
                    continue
                now = self.alive(f.iid) if self.alive is not None else (f.size, f.mtime)
                if now is None or (int(now[0] or 0), int(now[1] or 0)) != (f.size, f.mtime) or not f.runs:
                    self._forget(f)
            if self.enabled():
                total = sum(f.have() for f in self._files.values())
                keep = self._job.key if self._job is not None else ""
                for f in sorted(self._files.values(), key=lambda x: x.used):
                    if total <= self.limit:
                        break
                    if f.key == keep or self._readers.get(f.key):
                        continue
                    total -= f.have()
                    self._forget(f)
                    if f.name:
                        self.log(f"片库：整集缓存满了，删掉了「{f.name}」的")

    def drop(self, iid: str) -> None:
        """网盘上的文件和缓存的对不上：这个条目的整集缓存整个作废，下载线程也不再下它。"""
        with self._lock:
            for f in [f for f in self._files.values() if f.iid == iid]:
                self._forget(f)
            if self._job is not None and self._job.item["id"] == iid:
                self._job.state = "changed"

    # ---- 给页面 ----

    def fractions(self) -> dict[str, float]:
        """每个文件（按 key_of）下好了多少（0..1）；一点都没有的不在里面。"""
        with self._lock:
            return {f.key: min(1.0, f.have() / f.size) for f in self._files.values() if f.size > 0 and f.runs}

    def total(self) -> int:
        with self._lock:
            return sum(f.have() for f in self._files.values())

    def busy(self) -> bool:
        """下载线程手上有要下的（预热不和它抢网）。"""
        with self._lock:
            return self._ready(self._job, ignore_drive=True) and self._thread is not None and self._thread.is_alive()

    # ---- 放片子那边（library.py 的 _serve_full）----

    def touch(self, item: dict, at: int, name: str = "") -> None:
        """放片子从 at 起读这个文件了：下载线程换到它（上一个的留着），从 at 接着下。"""
        if not self.enabled() or int(item.get("size") or 0) <= 0:
            return
        key = self.key_of_item(item)
        with self._lock:
            self._file(item, name, False)          # 顺手作废旧的
            job = self._job
            if job is None or job.key != key:
                job = self._job = _Job(key, dict(item), name or key[:16], at, self.clock())
            job.anchor, job.last = at, self.clock()
            if name:
                job.name = name
            if job.state in ("full", "disk", "changed"):
                job.state = ""                      # 再放时再试一次
            self._cond.notify_all()
        self.start()

    def seen(self, item: dict, at: int) -> None:
        """放片子读到了 at（自己从网盘读完一阵）：下载线程从这里接着下。"""
        with self._lock:
            if self._job is not None and self._job.key == self.key_of_item(item):
                self._job.anchor = at
                self._job.last = self.clock()
                self._cond.notify_all()

    @contextmanager
    def reading(self, item: dict):
        """一个放片子的请求开着：这个文件不会被删，下载线程不会因为没人读而歇着。"""
        key = self.key_of_item(item)
        with self._lock:
            self._readers[key] = self._readers.get(key, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                left = self._readers.get(key, 1) - 1
                if left:
                    self._readers[key] = left
                else:
                    self._readers.pop(key, None)
                if self._job is not None and self._job.key == key:
                    self._job.last = self.clock()
                f = self._files.get(key)
                if f is not None:
                    self._save(f)
                self._cond.notify_all()

    @contextmanager
    def on_drive(self):
        """放片子自己在从网盘读：下载线程停下来让出带宽，读完了再接着下。"""
        with self._lock:
            self._drive += 1
            self._cond.notify_all()
        try:
            yield
        finally:
            with self._lock:
                self._drive -= 1
                self._cond.notify_all()

    def local(self, item: dict, at: int):
        """at 已经下好了：(数据文件, 从 at 起连续下好了多少字节)；没有时 None。"""
        key = self.key_of_item(item)
        with self._lock:
            f = self._files.get(key)
            if f is None:
                return None
            end = run_end(f.runs, at)
            if end <= at:
                return None
            f.used = self.clock()
            return self._data(f), end - at

    def can_fill(self, item: dict) -> bool:
        """下载线程在下这个文件（开着、没被挡住、没在出错后歇着）：放片子读一阵就能交给它。"""
        with self._lock:
            return self._filling(item)

    def _filling(self, item: dict) -> bool:
        job = self._job
        return job is not None and job.key == self.key_of_item(item) and self._ready(job, ignore_drive=True) \
            and self._thread is not None and self._thread.is_alive()

    def next_at(self, item: dict) -> int | None:
        """下载线程接下来写这个文件的哪（正在写的位置，或者没在写时 anchor 以后第一个没下的地方）；它不下这个文件、
        正让给放片子时 None。"""
        with self._lock:
            if not self._filling(item) or self._drive > 0:
                return None
            if self._at is not None and self._at[0] == self._job.key:
                return self._at[1]
            f = self._files.get(self._job.key)
            gap = next_gap(f.runs if f is not None else [], self._job.anchor, int(item.get("size") or 0))
            return gap[0] if gap is not None else None

    def wait(self, item: dict, at: int, timeout: float, gone: Callable[[], bool] | None = None) -> bool:
        """等下载线程把 at 写进来：写进来了 True；timeout 秒内一点都没写进来、它不下这个文件了、对方挂断了 False。"""
        key = self.key_of_item(item)
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                f = self._files.get(key)
                if f is not None and run_end(f.runs, at) > at:
                    return True
                left = deadline - time.monotonic()
                if left <= 0 or not self._filling(item):
                    return False
                self._cond.wait(min(left, 0.2))
                if gone is not None:
                    self._lock.release()
                    try:
                        if gone():
                            return False
                    finally:
                        self._lock.acquire()

    def tee(self, item: dict, at: int) -> "_FullTee | None":
        """放片子自己从网盘 at 起读时顺手存进来（这个文件是下载线程手上的、没被挡住时）。用完一定要 close。"""
        key = self.key_of_item(item)
        with self._lock:
            job = self._job
            if not self.enabled() or job is None or job.key != key or job.state:
                return None
            f = self._file(item, job.name, True)
            if f is None:
                return None
            path = self._data(f)
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
        except OSError as error:
            self._warn(error)
            return None
        return _FullTee(self, f, job, at, fd)

    def _wrote(self, f: _File, start: int, end: int, job: _Job | None, anchor: bool) -> None:
        """[start, end) 写进数据文件了：记下来、叫醒等着的；整个下好了记一行日志。在 self._lock 里调。"""
        was = f.done()
        add_run(f.runs, start, end)
        f.used = self.clock()
        if job is not None and self._job is job:
            if anchor:
                job.anchor = end
            if f.done() and not was:
                job.state = "done"
        if f.done() and not was:
            self._save(f)
            self.log(f"片库：「{f.name or f.key[:16]}」整集缓存好了")
        self._cond.notify_all()

    # ---- 下载线程 ----

    def _ready(self, job: _Job | None, ignore_drive: bool = False) -> bool:
        """下载线程现在该不该下 job。在 self._lock 里调。"""
        if job is None or self._stopped or not self.enabled() or job.state or self.opener is None \
                or time.monotonic() < job.after or (self._drive > 0 and not ignore_drive):
            return False
        return bool(self._readers.get(job.key)) or self.clock() - job.last <= self.idle_s

    def _abort(self, job: _Job, f: _File) -> bool:
        return self._stopped or not self.enabled() or self._job is not job or self._drive > 0 or job.state != "" \
            or self._files.get(f.key) is not f

    def step(self) -> bool:
        """下一段（至多 CHUNK）：返回下了没有。放片子要自己读网盘、换了文件、关了时下到一半就停（下好的留着）。"""
        with self._lock:
            job = self._job
            if not self._ready(job):
                return False
            f = self._file(job.item, job.name, True)
            if f is None:
                return False
            gap = next_gap(f.runs, job.anchor, f.size)
            if gap is None:
                job.state = "done"
                return False
            start, end = gap[0], min(gap[1], gap[0] + self.chunk)
            why = self._room(f, end - start)
            if why:
                self._block(job, why)
                return False
            self._at = (job.key, start)
            path = self._data(f)
        pos, fd = start, -1
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            upstream = self.opener(job.item, start, end - 1)
            try:
                read = getattr(upstream, "read1", None) or upstream.read
                while pos < end:
                    with self._lock:
                        if self._abort(job, f):
                            break
                    chunk = read(min(self.piece, end - pos))
                    if not chunk:
                        if pos == start:
                            raise OSError("网盘没给数据")
                        break
                    _pwrite_all(fd, chunk, pos)
                    with self._lock:
                        if self._files.get(f.key) is not f:
                            break
                        self._wrote(f, pos, pos + len(chunk), job, anchor=False)
                        pos += len(chunk)
                        self._at = (job.key, pos)
                        job.backoff = 0.0
            finally:
                upstream.close()
        except Changed:
            with self._lock:
                for old in [x for x in self._files.values() if x.iid == f.iid]:
                    self._forget(old)
                job.state = "changed"
            self.log(f"片库：「{job.name}」在网盘上变了，整集缓存不要了（重新扫描一下片库）")
        except Exception as error:  # noqa: BLE001 —— 网盘出错（超时、断了、拒绝）：歇一会儿再试
            with self._lock:
                job.backoff = min(BACKOFF_MAX_S, job.backoff * 2 or BACKOFF_S)
                job.after = time.monotonic() + job.backoff
                if not job.warned:
                    job.warned = True
                    self.log(f"片库：「{job.name}」整集缓存从网盘读不了（{type(error).__name__}），过一会儿再试")
        finally:
            if fd >= 0:
                try:
                    os.fsync(fd)                    # 先落盘，再记进说明
                except OSError:
                    pass
                os.close(fd)
            with self._lock:
                self._at = None
                self._save(f)
                self._cond.notify_all()
        return pos > start

    def run(self) -> None:
        while True:
            with self._lock:
                if self._stopped:
                    return
            try:
                did = self.step()
            except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                self.log(f"片库的整集缓存出错：{type(error).__name__}")
                did = False
            if not did:
                with self._lock:
                    if self._stopped:
                        return
                    self._cond.wait(self.tick_s)

    def start(self) -> None:
        """要下时才起下载线程（只起一个）。"""
        with self._lock:
            if not self.background or self._stopped or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self.run, name="library-full", daemon=True)
            self._thread.start()

    def stop(self, wait: float = 2.0) -> None:
        """中转关掉时：下载线程停下（正在下的这一小块写完就停，说明里只记写好了的）。"""
        with self._lock:
            self._stopped = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(wait)


def _pwrite_all(fd: int, data: bytes, at: int) -> None:
    view = memoryview(data)
    while view:
        done = os.pwrite(fd, view, at)
        view, at = view[done:], at + done


class _FullTee:
    """放片子自己从网盘读的时候顺手存：write 每块按位置写进数据文件（每写 CHUNK 字节问一次地方够不够；写不了、
    不够了就不存了，从不打断放片子）；close 记下来。"""

    def __init__(self, cache: FullCache, f: _File, job: _Job, at: int, fd: int):
        self.cache, self.f, self.job, self.at, self.fd = cache, f, job, at, fd
        self.granted = 0
        self.stopped = False

    def write(self, chunk: bytes) -> None:
        if self.stopped or not chunk:
            return
        cache, f, job = self.cache, self.f, self.job
        with cache._lock:
            if cache._files.get(f.key) is not f or cache._job is not job or job.state not in ("", "done"):
                self.stopped = True
                return
            if self.granted < len(chunk):
                why = cache._room(f, max(cache.chunk, len(chunk)))
                if why:
                    cache._block(job, why)
                    self.stopped = True
                    return
                self.granted += max(cache.chunk, len(chunk))
        try:
            _pwrite_all(self.fd, chunk, self.at)
        except OSError as error:
            cache._warn(error)
            self.stopped = True
            return
        with cache._lock:
            self.granted -= len(chunk)
            if cache._files.get(f.key) is f:
                cache._wrote(f, self.at, self.at + len(chunk), job, anchor=True)
        self.at += len(chunk)

    def close(self) -> None:
        try:
            os.fsync(self.fd)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        with self.cache._lock:
            self.cache._save(self.f)
