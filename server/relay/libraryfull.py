"""片库网盘文件的「边看边缓存」：把正在看的地方前后一段下到本机（按 64 MB 分片存），跳转到下好的地方从本机读，
几乎马上就有画面；网盘比片子慢时暂停一会儿就能攒出一段来。

为什么：网盘上的片子每次起播、每次跳转都重起一个 ffmpeg（watch.py；小电视那一路也一样），它要从网盘再读 3–12 MB
（4K HEVC 的 MKV 一个 GOP 5 秒、约 10 Mbit/s）才出第一段。夸克的速度在 4–78 Mbit/s 之间变，跳一下要 2–16 秒；
晚上有时只有 9–19 Mbit/s，供不上 23 Mbit/s 的蓝光片子。索引区的缓存（librarycache.py）只存每次打开都要读的头和
Cues，画面本身还是每次从网盘读；这里存正在看的那一段。以前是整集都下（一部蓝光 24 GB，缓存设 10 GB 就装不下，
满了按片子整个删，看了一半的也删），2026-11-04 起改成只留一段（窗口），删的时候按分片删。

怎么下（下载线程，同一时刻只有一个，只下一个文件）：
- 放片子的请求（/api/library/raw，ffmpeg 读的；预热的探测不算）读到一个网盘文件时（touch），下载线程换到这个文件
  （上一个下的留着），从放片子正在读的位置（anchor）起用一个网盘连接、一段一段（CHUNK，约 8 MB 一个 Range
  请求）往后下，至多下到 anchor 往后 AHEAD（4 GB，23 Mbit/s 的片子约 23 分钟）。放片子往后读（本机给的也算，
  seen），anchor 跟着走，窗口跟着往后挪；不再绕回文件头。
- 放片子优先：放片子的请求要自己从网盘读时（on_drive）下载线程马上停下这一段（下好的留着），让出带宽；放片子
  读完这一阵（library.py 的 _serve_full 每次至多读 BURST）就把 anchor 设成它读到的地方，下载线程从那里接着下——
  跳到没下的地方时就从新的地方下起。下载线程赶在放片子前面以后，放片子都从本机读，不碰网盘。
- 放片子读到下载线程正在下的地方（next_at）时不自己去网盘读，等下载线程写进来（wait，边写边给）；STALL_S 内
  一点都没等到（网盘卡住、线程歇着）才自己去读。
- 放片子自己从网盘读到的也顺手写进来（tee），不会下两遍。
- 窗口下满了就歇着；这个文件 IDLE_S（30 分钟）没人读就不下了，再放时接着下（暂停着时放片子的请求一般还开着，
  一直下到窗口满）；中转关掉时停下（stop）。数据先写、说明后写（只记已经写好的段），中途被关掉也不会把没写的
  当成下好了。

存在哪：library-full/<id>-<大小>-<修改时间>.<第几片>.bin（第 n 片存文件里 [n×SLICE, (n+1)×SLICE) 那一截，按位置写）
和 <id>-<大小>-<修改时间>.json（下好的段 runs、名字、最后用过的时间、最后读到哪），都是 600、文件夹 700。网盘上
的文件变了（重新扫描后大小或修改时间不一样）旧的整个作废；下载或放片子从网盘读时总长对不上也整个作废（drop）。
旧版整个文件一个 .bin 的缓存启动时删掉（说一句）。

上限：总共至多 limit（片库设置的 full_gb：关 / 5 / 10 / 30 GB，默认 5）。要地方时按分片删：先删正在下的这个文件
窗口外的（身后 BEHIND 1 GB 以外、往前 AHEAD 以外的，离得最远的先删），再删别的文件的（最久没看的先删，每个文件
离它最后读到的地方最远的先删）；窗口里的、正在读的那一片、正在读的文件从不删。预读开头（pre_tee）只能挤掉别的
预读（至多 PRE_EVICT_MAX），不动看过一段的片子。磁盘剩的空间会少于 FREE_MIN（10 GB）时不再往里存。关掉
（limit 为 0）时没有下载线程、已经存下的全删。只给网盘（WebDAV）的来源：本机文件夹本来就在本机，浏览器直接放的
（/api/library/file）也不用。

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
CHOICES = (0, 5, 10, 30)
DEFAULT_GB = 5
# 一片多大（按片存、按片删）；窗口：anchor 往前至多下多少、身后留多少（要地方时窗口外的先删）。
SLICE = 64 * MB
AHEAD = 4 * GB
BEHIND = 1 * GB
# 磁盘剩的空间不能因为整集缓存少于这么多。
FREE_MIN = 10 * GB
# 下载线程一个 Range 请求下这么多；一次至多读 PIECE 字节就看看要不要让给放片子。
CHUNK = 8 * MB
PIECE = 256 * 1024
# 预读开头（pre_tee）要地方时只能删这么小的（别的预读的开头）；看过一段的大片子留着。
PRE_EVICT_MAX = 256 * MB
# 放片子自己从网盘读时一阵至多读这么多（起播、跳转要 3–12 MB），之后交给下载线程。
BURST = 16 * MB
# 放片子等下载线程写进来，这么久一点都没等到就自己去网盘读。
STALL_S = 5.0
# 这么久没人读就歇着（暂停着攒缓冲时请求一般还开着；关了的话至多再下这么久）。
IDLE_S = 1800.0
# 下载线程没事时隔这么久看一眼（有事时马上被叫醒）。
TICK_S = 5.0
# 网盘出错了歇这么久，接连出错翻倍，至多 BACKOFF_MAX_S。
BACKOFF_S = 15.0
BACKOFF_MAX_S = 600.0
# 「往后缓存不下了」同一个文件至多隔这么久说一次；地方不够时下载线程隔这么久再试。
BLOCK_LOG_S = 3600.0
BLOCK_RETRY_S = 30.0
# 磁盘剩多少空间至多隔这么久问一次系统。
DISK_CHECK_S = 2.0

_KEY = re.compile(r"^([0-9a-f]{16})-(\d{1,15})-(-?\d{1,15})$")
_PIECE = re.compile(r"^(.+)\.(\d{1,9})\.bin$")


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


def cut_run(runs: list[list[int]], start: int, end: int) -> None:
    """从 runs 里去掉 [start, end)。"""
    if end <= start:
        return
    kept = []
    for a, b in runs:
        if b <= start or a >= end:
            kept.append([a, b])
            continue
        if a < start:
            kept.append([a, start])
        if b > end:
            kept.append([end, b])
    runs[:] = kept


def overlap(runs: list[list[int]], start: int, end: int) -> int:
    """runs 落在 [start, end) 里的字节数。"""
    return sum(max(0, min(b, end) - max(a, start)) for a, b in runs)


def window_gap(runs: list[list[int]], anchor: int, size: int, ahead: int):
    """下载线程接着下哪：anchor 以后、anchor + ahead 以内第一个没下的地方 (起, 止)；窗口里都下好了时 None
    （不绕回文件头）。"""
    gap = gap_after(runs, anchor, size)
    limit = min(size, max(0, anchor) + ahead)
    if gap is None or gap[0] >= limit:
        return None
    return gap[0], min(gap[1], limit)


# ---------------------------------------------------------------- 缓存

class _File:
    __slots__ = ("key", "iid", "size", "mtime", "name", "used", "runs", "at")

    def __init__(self, key: str, iid: str, size: int, mtime: int, name: str = ""):
        self.key, self.iid, self.size, self.mtime, self.name = key, iid, size, mtime, name
        self.used = 0.0
        self.runs: list[list[int]] = []
        self.at = 0                       # 最后读到 / 写到哪（要地方时离这儿远的先删）

    def have(self) -> int:
        return sum(b - a for a, b in self.runs)

    def done(self) -> bool:
        return self.runs == [[0, self.size]]


class _Job:
    """下载线程手上的文件。state：空串在下（窗口满了就歇着）；changed 网盘上变了。after：出错、地方不够时
    歇到这时候（time.monotonic()）。"""
    __slots__ = ("key", "item", "name", "anchor", "last", "state", "after", "backoff", "warned")

    def __init__(self, key: str, item: dict, name: str, anchor: int, now: float):
        self.key, self.item, self.name, self.anchor, self.last = key, item, name, anchor, now
        self.state = ""
        self.after = 0.0
        self.backoff = 0.0
        self.warned = False


class FullCache:
    """library-full/：网盘文件的边看边缓存（按片存）。opener(item, 起, 止) 打开网盘上这一段（含止，回答的总长对不上
    时抛 Changed）；alive(id) 给这个条目现在的 (大小, 修改时间)，没有了给 None；limit 是总共至多存多少字节（0 是关）。
    slice / ahead / behind：一片多大、窗口往前多大、身后留多少（测试里改小）。"""

    def __init__(self, root: Path, opener: Callable | None = None, alive: Callable[[str], tuple | None] | None = None,
                 log: Callable[[str], None] = print, clock=time.time, limit: int = DEFAULT_GB * GB,
                 disk_free: Callable[[], int] | None = None, background: bool = True,
                 slice: int = SLICE, ahead: int = AHEAD, behind: int = BEHIND):
        self.root = Path(root)
        self.opener = opener
        self.alive = alive
        self.log = log
        self.clock = clock
        self.limit = max(0, int(limit))
        self.disk_free = disk_free or self._disk_free
        self.background = background
        self.slice, self.ahead, self.behind = int(slice), int(ahead), int(behind)
        self.chunk, self.piece, self.burst = CHUNK, PIECE, BURST
        self.stall_s, self.idle_s, self.tick_s = STALL_S, IDLE_S, TICK_S
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._files: dict[str, _File] = {}
        self._readers: dict[str, int] = {}
        self._job: _Job | None = None
        self._drive = 0                   # 正在自己从网盘读的放片子请求
        self._at: tuple[str, int] | None = None     # 下载线程正在写的 (文件, 位置)
        self._writing: dict[tuple[str, int], int] = {}   # 正在往里写的片（不删）
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._disk = (float("-inf"), 0)
        self._blocked_said: dict[tuple[str, str], float] = {}
        self._warned = False
        self._load()

    # ---- 文件 ----

    @staticmethod
    def key_of(iid: str, size: int, mtime: int) -> str:
        return f"{iid}-{int(size)}-{int(mtime)}"

    @classmethod
    def key_of_item(cls, item: dict) -> str:
        return cls.key_of(item["id"], int(item.get("size") or 0), int(item.get("mtime") or 0))

    def _data(self, f: _File, index: int) -> Path:
        return self.root / f"{f.key}.{index}.bin"

    def _meta(self, f: _File) -> Path:
        return self.root / f"{f.key}.json"

    def _indexes(self, f: _File) -> list[int]:
        """有数据的片（第几片）。"""
        found = set()
        for a, b in f.runs:
            found.update(range(a // self.slice, (b - 1) // self.slice + 1))
        return sorted(found)

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
                f.at = int(data.get("at") or 0)
                for a, b in data.get("runs") or []:
                    pos, b = max(0, int(a)), min(int(b), f.size)
                    while pos < b:                   # 说明里有、片里没有的不算
                        index = pos // self.slice
                        base = index * self.slice
                        stop = min(b, base + self.slice)
                        try:
                            on_disk = self._data(f, index).stat().st_size
                        except OSError:
                            on_disk = 0
                        if pos < min(stop, base + on_disk):
                            add_run(f.runs, pos, min(stop, base + on_disk))
                        pos = stop
            except (OSError, ValueError, KeyError, TypeError):
                self._unlink(meta)
                continue
            self._files[f.key] = f
        old = False
        for path in self.root.glob("*.bin"):
            match = _PIECE.match(path.name)
            f = self._files.get(match.group(1)) if match else None
            if f is None or int(match.group(2)) not in self._indexes(f):
                old = old or bool(_KEY.match(path.stem))
                self._unlink(path)
        for path in self.root.glob("*.tmp"):
            self._unlink(path)
        if old:
            self.log("片库：缓存改成只存正在看的那一段（按片存），旧的整集缓存删掉了")
        self.prune()

    def _save(self, f: _File) -> None:
        """写这个文件的说明（整份换掉写，600）。在 self._lock 里调。"""
        if self._files.get(f.key) is not f:
            return
        row = {"name": f.name, "used": round(f.used, 3), "at": f.at, "runs": f.runs}
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
        if self.root.is_dir():
            for path in self.root.glob(f"{f.key}.*.bin"):
                self._unlink(path)
        self._unlink(self._meta(f))
        self._cond.notify_all()

    def _drop_piece(self, f: _File, index: int) -> int:
        """删掉一片，返回腾出多少字节。在 self._lock 里调。"""
        base = index * self.slice
        freed = overlap(f.runs, base, base + self.slice)
        cut_run(f.runs, base, base + self.slice)
        self._unlink(self._data(f, index))
        if not f.runs:
            self._forget(f)
        return freed

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
        """改上限：0 是关（停下、已经存下的全删）；小了按片删到不超。"""
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
            elif self._job is not None:
                self._job.after = 0.0               # 大了：马上接着下
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

    def _victims(self, f: _File | None, at: int, pre: bool) -> list[tuple[_File, int]]:
        """要地方时能删的片，先删的在前。在 self._lock 里调。
        1. 下载线程手上那个文件窗口外的（身后 behind 以外、往前 ahead 以外），离 anchor 最远的先删；
        2. 别的文件的（正在读的、f 自己除外），最久没看的先删，每个文件离它最后读到的地方最远的先删；
        3. 下载线程手上那个文件身后留着的（整片在 anchor 那一片之前的），最远的先删。
        正在写的片、f 里 at 那一片从不删。pre（预读开头要地方）：只删别的、至多 PRE_EVICT_MAX 的文件的。"""
        job = self._job
        out: list[tuple[_File, int]] = []

        def busy(x: _File, index: int) -> bool:
            return (x.key, index) in self._writing or (x is f and index == at // self.slice)

        current = self._files.get(job.key) if job is not None else None
        if current is not None and not pre:
            lo, hi = job.anchor - self.behind, job.anchor + self.ahead
            far = []
            for index in self._indexes(current):
                start, end = index * self.slice, (index + 1) * self.slice
                if busy(current, index) or (end > lo and start < hi):
                    continue
                far.append((lo - end if end <= lo else start - hi, index))
            out += [(current, index) for _, index in sorted(far, reverse=True)]
        for x in sorted(self._files.values(), key=lambda y: y.used):
            if x is current or x is f or self._readers.get(x.key) or (pre and x.have() > PRE_EVICT_MAX):
                continue
            pieces = sorted(self._indexes(x), key=lambda index: -abs(index * self.slice - x.at))
            out += [(x, index) for index in pieces if not busy(x, index)]
        if current is not None and not pre:
            # 3. 还不够：身后留着的（整片在 anchor 那一片之前的），最远的先删——往前的比往回拖的要紧。
            listed = {index for x, index in out if x is current}
            out += [(current, index) for index in sorted(self._indexes(current), reverse=False)
                    if index < job.anchor // self.slice and index not in listed and not busy(current, index)]
        return out

    def _make_room(self, f: _File | None, need: int, at: int = 0, pre: bool = False) -> int:
        """按片删到总共 + need 不超上限（删得到的话）；返回删完以后总共多少。整个文件删光了记一行日志。
        在 self._lock 里调。"""
        total = sum(x.have() for x in self._files.values())
        if total + need <= self.limit:
            return total
        for x, index in self._victims(f, at, pre):
            if total + need <= self.limit:
                break
            if self._files.get(x.key) is not x:
                continue
            total -= self._drop_piece(x, index)
            if self._files.get(x.key) is not x and x.name and (self._job is None or x.key != self._job.key):
                self.log(f"片库：缓存满了，删掉了「{x.name}」的")
        return total

    def _room(self, f: _File, need: int, at: int = 0, pre: bool = False) -> str:
        """要往 f 的 at 处再存 need 字节：超了上限先按片删（_victims）；还不够时 "full"，磁盘剩的会少于 FREE_MIN 时
        "disk"，够时空串。在 self._lock 里调。"""
        if not self.enabled():
            return "full"
        if self._make_room(f, need, at, pre) + need > self.limit:
            return "full"
        if self._free() - need < FREE_MIN:
            return "disk"
        return ""

    def _block(self, job: _Job, why: str) -> None:
        """地方不够了：下载线程歇 BLOCK_RETRY_S 再试（放片子往后读了，身后的就能删了）。在 self._lock 里调。
        日志同一个文件 BLOCK_LOG_S 内只说一次。"""
        job.after = max(job.after, time.monotonic() + BLOCK_RETRY_S)
        now = self.clock()
        if now - self._blocked_said.get((job.key, why), float("-inf")) >= BLOCK_LOG_S:
            self._blocked_said[(job.key, why)] = now
            self.log(f"片库：「{job.name}」往后缓存不下了，" + ("缓存满了" if why == "full" else "磁盘快满了"))

    def prune(self) -> None:
        """索引里没有了、大小或修改时间变了的删；总共超过上限时按片删（_victims）。"""
        with self._lock:
            for f in list(self._files.values()):
                if self._readers.get(f.key):
                    continue
                now = self.alive(f.iid) if self.alive is not None else (f.size, f.mtime)
                if now is None or (int(now[0] or 0), int(now[1] or 0)) != (f.size, f.mtime) or not f.runs:
                    self._forget(f)
            if self.enabled():
                self._make_room(None, 0)
                for f in self._files.values():
                    self._save(f)

    def drop(self, iid: str) -> None:
        """网盘上的文件和缓存的对不上：这个条目的缓存整个作废，下载线程也不再下它。"""
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
        """下载线程手上有要下的（窗口没满；预热、截封面不和它抢网）。"""
        with self._lock:
            return self._ready(self._job, ignore_drive=True) and self._gap(self._job) is not None \
                and self._thread is not None and self._thread.is_alive()

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
            job.state = ""                          # 再放时再试一次
            job.after = 0.0 if not job.backoff else job.after
            self._cond.notify_all()
        self.start()

    def seen(self, item: dict, at: int) -> None:
        """放片子读到了 at（自己从网盘读完一阵，或者从本机给到这儿）：窗口跟着往后挪，下载线程从这里接着下。"""
        with self._lock:
            f = self._files.get(self.key_of_item(item))
            if f is not None:
                f.at = at
            if self._job is not None and self._job.key == self.key_of_item(item):
                self._job.anchor = at
                self._job.last = self.clock()
                self._cond.notify_all()

    @contextmanager
    def reading(self, item: dict):
        """一个放片子的请求开着：这个文件不会被别的文件挤掉，下载线程不会因为没人读而歇着。"""
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
        """at 已经下好了：(那一片的数据文件, at 在片里的位置, 从 at 起这一片里连续下好了多少字节)；没有时 None。"""
        key = self.key_of_item(item)
        with self._lock:
            f = self._files.get(key)
            if f is None:
                return None
            end = run_end(f.runs, at)
            if end <= at:
                return None
            index = at // self.slice
            base = index * self.slice
            f.used = self.clock()
            f.at = at
            return self._data(f, index), at - base, min(end, base + self.slice) - at

    def can_fill(self, item: dict) -> bool:
        """下载线程在下这个文件（开着、没被挡住、没在出错后歇着）：放片子读一阵就能交给它。"""
        with self._lock:
            return self._filling(item)

    def _filling(self, item: dict) -> bool:
        job = self._job
        return job is not None and job.key == self.key_of_item(item) and self._ready(job, ignore_drive=True) \
            and self._thread is not None and self._thread.is_alive()

    def _gap(self, job: _Job | None):
        """下载线程手上的文件窗口里第一个没下的地方；没有时 None。在 self._lock 里调。"""
        if job is None:
            return None
        f = self._files.get(job.key)
        return window_gap(f.runs if f is not None else [], job.anchor, int(job.item.get("size") or 0), self.ahead)

    def next_at(self, item: dict) -> int | None:
        """下载线程接下来写这个文件的哪（正在写的位置，或者没在写时窗口里第一个没下的地方）；它不下这个文件、
        正让给放片子时 None。"""
        with self._lock:
            if not self._filling(item) or self._drive > 0:
                return None
            if self._at is not None and self._at[0] == self._job.key:
                return self._at[1]
            gap = self._gap(self._job)
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
        """放片子自己从网盘 at 起读时顺手存进来（这个文件是下载线程手上的、没作废时）。用完一定要 close。"""
        key = self.key_of_item(item)
        with self._lock:
            job = self._job
            if not self.enabled() or job is None or job.key != key or job.state:
                return None
            f = self._file(item, job.name, True)
            if f is None:
                return None
        return _FullTee(self, f, job, at)

    def pre_tee(self, item: dict, at: int, name: str = "") -> "_FullTee | None":
        """预读开头（librarypre.py）从网盘 at 起读时存进来：不经下载线程、不换它手上的文件，地方不够了就不存。
        用完一定要 close。"""
        with self._lock:
            if not self.enabled():
                return None
            f = self._file(item, name, True)
            if f is None:
                return None
            f.at = at
        return _FullTee(self, f, None, at)

    def covered(self, item: dict, start: int, end: int) -> bool:
        """[start, end) 都已经下好了。"""
        with self._lock:
            f = self._files.get(self.key_of_item(item))
            return f is not None and run_end(f.runs, start) >= min(end, f.size)

    def _wrote(self, f: _File, start: int, end: int, job: _Job | None, anchor: bool) -> None:
        """[start, end) 写进那一片了：记下来、叫醒等着的；整个下好了记一行日志。在 self._lock 里调。"""
        was = f.done()
        add_run(f.runs, start, end)
        f.used = self.clock()
        if job is not None and self._job is job and anchor:
            job.anchor = end
            f.at = end
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
        """下一段（至多 CHUNK，不跨片）：返回下了没有。放片子要自己读网盘、换了文件、关了时下到一半就停（下好的
        留着）；窗口里都下好了就歇着。"""
        with self._lock:
            job = self._job
            if not self._ready(job):
                return False
            f = self._file(job.item, job.name, True)
            if f is None:
                return False
            gap = self._gap(job)
            if gap is None:
                return False
            start = gap[0]
            end = min(gap[1], start + self.chunk, (start // self.slice + 1) * self.slice)
            why = self._room(f, end - start, start)
            if why:
                self._block(job, why)
                return False
            self._at = (job.key, start)
            writer = _Writer(self, f)
        pos = start
        try:
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
                    writer.write(pos, chunk)
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
            writer.close()                          # 先落盘，再记进说明
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


def _pwrite_all(fd: int, data, at: int) -> None:
    view = memoryview(data)
    while view:
        done = os.pwrite(fd, view, at)
        view, at = view[done:], at + done


class _Writer:
    """往一个文件的片里写（跨片时分开写）：每片开一次、close 时全部落盘关掉。写着的片记在 cache._writing 里，
    要地方时不删。写不了时 OSError。"""

    def __init__(self, cache: FullCache, f: _File):
        self.cache, self.f = cache, f
        self.fds: dict[int, int] = {}

    def write(self, at: int, data) -> None:
        cache, view = self.cache, memoryview(data)
        while view:
            index = at // cache.slice
            base = index * cache.slice
            count = min(len(view), base + cache.slice - at)
            fd = self.fds.get(index)
            if fd is None:
                with cache._lock:
                    cache._writing[(self.f.key, index)] = cache._writing.get((self.f.key, index), 0) + 1
                try:
                    cache.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd = os.open(cache._data(self.f, index), os.O_RDWR | os.O_CREAT, 0o600)
                    os.fchmod(fd, 0o600)
                except OSError:
                    self._release(index)
                    raise
                self.fds[index] = fd
            _pwrite_all(fd, view[:count], at - base)
            view, at = view[count:], at + count

    def _release(self, index: int) -> None:
        cache, key = self.cache, (self.f.key, index)
        with cache._lock:
            left = cache._writing.get(key, 1) - 1
            if left:
                cache._writing[key] = left
            else:
                cache._writing.pop(key, None)

    def close(self) -> None:
        for index, fd in self.fds.items():
            try:
                os.fsync(fd)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            self._release(index)
        self.fds = {}


class _FullTee:
    """放片子自己从网盘读的时候顺手存：write 每块按位置写进那一片（每写 CHUNK 字节问一次地方够不够；写不了、
    不够了就不存了，从不打断放片子）；close 记下来。job 为 None：预读开头的（pre_tee），不跟着下载线程。"""

    def __init__(self, cache: FullCache, f: _File, job: _Job | None, at: int):
        self.cache, self.f, self.job, self.at = cache, f, job, at
        self.writer = _Writer(cache, f)
        self.granted = 0
        self.stopped = False

    def write(self, chunk: bytes) -> None:
        if self.stopped or not chunk:
            return
        cache, f, job = self.cache, self.f, self.job
        with cache._lock:
            if cache._files.get(f.key) is not f or (job is not None and (cache._job is not job or job.state)):
                self.stopped = True
                return
            if self.granted < len(chunk):
                why = cache._room(f, max(cache.chunk, len(chunk)), self.at, pre=job is None)
                if why:
                    if job is not None:
                        cache._block(job, why)
                    self.stopped = True
                    return
                self.granted += max(cache.chunk, len(chunk))
        try:
            self.writer.write(self.at, chunk)
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
        self.writer.close()
        with self.cache._lock:
            self.cache._save(self.f)
