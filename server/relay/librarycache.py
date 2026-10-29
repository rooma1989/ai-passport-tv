"""片库网盘文件的「索引区」缓存：只给中转自己的 ffmpeg 读的 /api/library/raw 用（library.py 的 _serve_cached）。

为什么：在电脑上放网盘里的片子时，每次起播、每次跳转都重起一个 ffmpeg（watch.py），它每次都从头打开文件，先把
「目录」读一遍才读得到画面。量过主人的片子（夸克，直连约 65 Mbit/s，每个请求约 0.2 s）：一个 21 GB 的 4K MP4
是 ftyp、21.2 GB 的 mdat，最后才是 20.9 MB 的 moov——每次打开都要把这 20.9 MB 再从网盘拉一遍，从头放 11.5 s
才出第一段，跳到 60 分钟 18 s。MKV 剧集（7–10 GB）是开头的头和附件字体（几 MB），跳转时再读文件尾的 Cues，
限速的线路上光探测就 15–23 s。这些字节每次都一样，存在本机就只用读一次。

存什么（只存 ffmpeg 每次打开都要再读的地方，从不存大段的画面）：
- 开头一段（head）：MP4 / MOV / M4V 存到 mdat（或 moof）开始再多 SLACK（ftyp、开头的 moov——faststart 的片子
  moov 在这里——和 ffmpeg 打开时先读的头几帧）；MKV / WebM 存到第一个 Cluster 再多 SLACK（EBML 头、SeekHead、
  Tracks、附件字体、Tags…）。边存边按已经存下的字节认盒子 / 元素（mp4_layout、mkv_layout），认出来之前至多存
  FILE_MAX；认不出的格式（TS、AVI…）只存开头 HEAD_OTHER（探测读 PROBE_ARGS 的 5 MB）。
- MP4 的 mdat 后面（tail）：moov 在文件尾时从 mdat 的末尾存到文件尾（就是 moov，有时后面还有一点 udta），
  只在这段不超过 FILE_MAX 时存。
- 别的容器的文件尾：最后 TAIL 字节里第一次被读的地方起存到文件尾（MKV 的 Cues、文件尾的 Tags；TS 的时长
  ffmpeg 也读文件尾）。只认第一次：ffmpeg 跳转前先读 Cues，所以第一次落进文件尾的请求是 Cues。

怎么存：放片子的时候不另外去网盘取（不和放片子抢带宽）。ffmpeg 自己读到这些地方时边放边存（tee）：一段从它的起点往后
连续地存，请求正好从一段已存到的地方起、这一段还没存满时才接着存（同一段同时只有一个在写）。下回请求的起点落在
已存的一段里：先从这里给，给完了要是还要更多，接着从网盘的那个位置读（同一个回答里接着发，ffmpeg 看不出来），
顺手把这一段存得更长（还在这一段的范围里时）。

- 按条目 id + 大小 + 修改时间认（library-cache/<id>-<大小>-<修改时间>.json 和 .<起点>.bin）：网盘上的文件变了
  （重新扫描后大小或修改时间不一样）旧的整个作废；从网盘接着读时总长对不上也整个作废。
- 每个文件至多 FILE_MAX，总共至多 CACHE_MAX，超了按最后用过的时间删最旧的（整个文件删）；索引里没有了的条目
  （删了来源、重新扫描后不见了）也删。文件都是 600、文件夹 700。
- 本机文件夹的来源不用：本来就在本机。直接在浏览器里放的（/api/library/file）也不用：只有 ffmpeg 每次重读。
- 预热（library.py 的 warm_step）：扫描找到新的网盘片子以后，没人在放时后台一个一个先读一遍这几段（todo 说从哪接着读，
  MKV 的 Cues 在哪看 cues_at），走的还是 tee，存法和上限一样；room 说不挤掉最近用过的还能存多少。

只用标准库。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

MB = 1024 * 1024
# 总共、每个文件至多存这么多（一部 2 小时 4K MP4 的 moov 约 21 MB；MKV 一集的头几 MB）。
CACHE_MAX = 512 * MB
FILE_MAX = 64 * MB
# 媒体数据开头再多存这么多：ffmpeg 每次打开都要先读头几帧（find_stream_info），量过从头放、跳转都会先读这里。
SLACK = 2 * MB
# 认不出格式的文件开头存这么多（ffprobe 的 -probesize 是 5 MB）。
HEAD_OTHER = 8 * MB
# MKV 等：最后这么多字节里第一次被读的地方起存到文件尾（Cues 一般几百 KB）。
TAIL = 8 * MB
# 认 MP4 盒子 / MKV 元素时至多走这么多个。
WALK_MAX = 256

MP4_EXTS = frozenset({".mp4", ".m4v", ".mov", ".f4v", ".3gp"})
MKV_EXTS = frozenset({".mkv", ".webm"})
UNKNOWN = (-1, None)

_KEY = re.compile(r"^([0-9a-f]{16})-(\d{1,15})-(-?\d{1,15})$")


# ---------------------------------------------------------------- 认格式（只看已经存下的开头）

def mp4_layout(read: Callable[[int, int], bytes], have: int, size: int):
    """MP4 / MOV 顶层的盒子：返回 (媒体数据从哪起, mdat 后面从哪起)。媒体数据是第一个 mdat 的内容（或第一个
    moof，分片的 MP4）；mdat 一直到文件尾时后面是 None。have 是开头已经有多少字节，不够认时返回 None；
    认不出（不像 MP4）时 UNKNOWN。"""
    off = 0
    for _ in range(WALK_MAX):
        if off + 8 > have:
            return None
        head = read(off, min(16, have - off))
        box, kind, header = int.from_bytes(head[:4], "big"), head[4:8], 8
        if not all(32 <= c < 127 for c in kind):
            return UNKNOWN
        if box == 1:                            # 64 位的大小（大的 mdat）
            if len(head) < 16:
                return None
            box, header = int.from_bytes(head[8:16], "big"), 16
        elif box == 0:                          # 一直到文件尾
            box = size - off
        if box < header or off + box > size:
            return UNKNOWN
        if kind == b"moof":
            return off, None
        if kind == b"mdat":
            return off + header, (off + box if off + box < size else None)
        off += box
        if off >= size:
            return UNKNOWN
    return UNKNOWN


def _vint(data: bytes, at: int, marker: bool):
    """EBML 的变长数：(值, 占几个字节)；字节不够时 None，不像 EBML 时 (-1, 0)。marker：ID 留着长度标记位。"""
    if at >= len(data):
        return None
    first = data[at]
    if first == 0:
        return -1, 0
    length = 9 - first.bit_length()
    if at + length > len(data):
        return None
    value = first if marker else first & (0xFF >> length)
    for byte in data[at + 1:at + length]:
        value = (value << 8) | byte
    return value, length


def mkv_layout(read: Callable[[int, int], bytes], have: int, size: int):
    """MKV / WebM：EBML 头、Segment，再一个一个走 Segment 下面的元素直到第一个 Cluster（画面从这里起）。
    返回 (第一个 Cluster 在哪, None)；不够认时 None；认不出时 UNKNOWN。"""
    off, level = 0, 0
    for _ in range(WALK_MAX):
        if off >= size:
            return UNKNOWN
        if off >= have:
            return None
        data = read(off, min(12, have - off))
        found = _vint(data, 0, True)
        if found is None:
            return None
        ident, a = found
        found = _vint(data, a, False) if a else (-1, 0)
        if found is None:
            return None
        length, b = found
        if a == 0 or b == 0 or a > 4:
            return UNKNOWN
        unknown_size = length == (1 << (7 * b)) - 1
        if level == 0:
            if ident != 0x1A45DFA3 or unknown_size:          # EBML 头
                return UNKNOWN
            off, level = off + a + b + length, 1
        elif level == 1:
            if ident != 0x18538067:                         # Segment：进去
                return UNKNOWN
            off, level = off + a + b, 2
        else:
            if ident == 0x1F43B675:                         # Cluster
                return off, None
            if unknown_size:
                return UNKNOWN
            off += a + b + length
    return UNKNOWN


def _ebml_children(body: bytes):
    """一个 EBML 元素里面的一个一个子元素：(ID, 内容)；走到认不出的地方就停。"""
    at = 0
    while at < len(body):
        ident = _vint(body, at, True)
        if ident is None or ident[1] == 0:
            return
        length = _vint(body, at + ident[1], False)
        if length is None or length[1] == 0:
            return
        start = at + ident[1] + length[1]
        yield ident[0], body[start:start + length[0]]
        at = start + length[0]


def mkv_cues(read: Callable[[int, int], bytes], have: int, size: int) -> int | None:
    """MKV / WebM 的 Cues 在文件的哪（SeekHead 里记的位置 + Segment 内容的起点）：ffmpeg 第一次跳转时从这里读。
    Cues 在第一个 Cluster 前面（已经在开头那段里）、SeekHead 里没记、开头这段不够认、不像 MKV 时 None。"""
    off, level, segment = 0, 0, 0
    for _ in range(WALK_MAX):
        if off >= min(have, size):
            return None
        data = read(off, min(12, have - off))
        found = _vint(data, 0, True)
        if found is None or found[1] == 0 or found[1] > 4:
            return None
        ident, a = found
        found = _vint(data, a, False)
        if found is None or found[1] == 0:
            return None
        length, b = found
        if level == 0:
            if ident != 0x1A45DFA3:                                 # EBML 头
                return None
            off, level = off + a + b + length, 1
        elif level == 1:
            if ident != 0x18538067:                                 # Segment：进去
                return None
            off = segment = off + a + b
            level = 2
        elif ident in (0x1F43B675, 0x1C53BB6B) or length == (1 << (7 * b)) - 1:
            return None                                             # 到了 Cluster、Cues 在前面、不知道多长
        elif ident == 0x114D9B74:                                   # SeekHead
            if off + a + b + length > have:
                return None
            for kind, seek in _ebml_children(read(off + a + b, length)):
                fields = dict(_ebml_children(seek)) if kind == 0x4DBB else {}
                if fields.get(0x53AB) == b"\x1c\x53\xbb\x6b" and fields.get(0x53AC):
                    where = segment + int.from_bytes(fields[0x53AC], "big")
                    return where if where < size else None
            return None
        else:
            off += a + b + length
    return None


# ---------------------------------------------------------------- 缓存

class _Region:
    """一段：从 start 起连续存了 length 字节，至多存到 end（不含）。head：文件开头那段。"""
    __slots__ = ("start", "end", "length", "busy")

    def __init__(self, start: int, end: int, length: int = 0):
        self.start, self.end, self.length, self.busy = start, end, length, False

    def row(self) -> dict:
        return {"start": self.start, "end": self.end, "length": self.length}


class _Entry:
    __slots__ = ("key", "iid", "size", "mtime", "ext", "used", "regions", "layout")

    def __init__(self, key: str, iid: str, size: int, mtime: int, ext: str):
        self.key, self.iid, self.size, self.mtime, self.ext = key, iid, size, mtime, ext
        self.used = 0.0
        self.regions: list[_Region] = []
        # 认出来的格式：None 还没认出来；(媒体从哪起, mdat 后面从哪起) 或 UNKNOWN。
        self.layout = None

    def stored(self) -> int:
        return sum(r.length for r in self.regions)

    def busy(self) -> bool:
        return any(r.busy for r in self.regions)


class IndexCache:
    """library-cache/：网盘文件的索引区。alive(id) 给这个条目现在的 (大小, 修改时间)，没有了给 None。"""

    def __init__(self, root: Path, alive: Callable[[str], tuple | None] | None = None,
                 log: Callable[[str], None] = print, clock=time.time, total_max: int = CACHE_MAX,
                 file_max: int = FILE_MAX):
        self.root = Path(root)
        self.alive = alive
        self.log = log
        self.clock = clock
        self.total_max = total_max
        self.file_max = file_max
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._warned = False
        self._load()

    # ---- 文件 ----

    @staticmethod
    def key_of(iid: str, size: int, mtime: int) -> str:
        return f"{iid}-{int(size)}-{int(mtime)}"

    def _data(self, entry: _Entry, region: _Region) -> Path:
        return self.root / f"{entry.key}.{region.start}.bin"

    def _meta(self, entry: _Entry) -> Path:
        return self.root / f"{entry.key}.json"

    def _load(self) -> None:
        if not self.root.is_dir():
            return
        for meta in self.root.glob("*.json"):
            match = _KEY.match(meta.stem)
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                if match is None or not isinstance(data, dict):
                    raise ValueError
                entry = _Entry(meta.stem, match.group(1), int(match.group(2)), int(match.group(3)), str(data["ext"]))
                entry.used = float(data.get("used") or 0)
                layout = data.get("layout")
                entry.layout = (int(layout[0]), None if layout[1] is None else int(layout[1])) \
                    if isinstance(layout, list) and len(layout) == 2 else None
                for row in data.get("regions") or []:
                    region = _Region(int(row["start"]), int(row["end"]), int(row["length"]))
                    on_disk = self._data(entry, region).stat().st_size if self._data(entry, region).exists() else 0
                    region.length = max(0, min(region.length, on_disk, region.end - region.start))
                    entry.regions.append(region)
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                self._unlink(meta)
                continue
            self._entries[entry.key] = entry
        known = {self._data(e, r).name for e in self._entries.values() for r in e.regions}
        for data in self.root.glob("*.bin"):
            if data.name not in known:
                self._unlink(data)
        self.prune()

    def _save(self, entry: _Entry) -> None:
        """写这个文件的说明（整份换掉写，600）。在 self._lock 里调。"""
        if entry.key not in self._entries:
            return
        row = {"ext": entry.ext, "used": round(entry.used, 3), "regions": [r.row() for r in entry.regions],
               "layout": list(entry.layout) if entry.layout is not None else None}
        path = self._meta(entry)
        tmp = path.with_name(path.name + ".tmp")
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(row, handle, separators=(",", ":"))
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
            self.log(f"片库的索引缓存写不了（{type(error).__name__}），照旧从网盘读")

    def _forget(self, entry: _Entry) -> None:
        """整个删掉一个文件的缓存。在 self._lock 里调。"""
        self._entries.pop(entry.key, None)
        for region in entry.regions:
            self._unlink(self._data(entry, region))
        self._unlink(self._meta(entry))

    # ---- 查 ----

    def _entry(self, iid: str, size: int, mtime: int, ext: str, create: bool) -> _Entry | None:
        """这个条目现在的缓存；同一个 id 大小或修改时间不一样的旧缓存作废。在 self._lock 里调。"""
        key = self.key_of(iid, size, mtime)
        for old in [e for e in self._entries.values() if e.iid == iid and e.key != key and not e.busy()]:
            self._forget(old)
        entry = self._entries.get(key)
        if entry is None and create and size > 0:
            entry = self._entries[key] = _Entry(key, iid, int(size), int(mtime), ext)
        return entry

    def open(self, iid: str, size: int, mtime: int, ext: str, start: int):
        """start 落在已经存下的一段里：(打开的数据文件，已经在 start 的位置, 从 start 起有多少字节)；没有时 None。
        顺手记下这个文件最后用过的时间。"""
        with self._lock:
            entry = self._entry(iid, size, mtime, ext, False)
            region = next((r for r in entry.regions if r.start <= start < r.start + r.length), None) \
                if entry is not None else None
            if region is None:
                return None
            try:
                handle = open(self._data(entry, region), "rb")
            except OSError:
                self._forget(entry)
                return None
            handle.seek(start - region.start)
            entry.used = self.clock()
            self._save(entry)
            return handle, region.start + region.length - start

    def tee(self, iid: str, size: int, mtime: int, ext: str, start: int) -> "_Tee | None":
        """从网盘 start 起往下读时能不能边放边存：start 正好是一段已经存到的地方、这一段还没满、没有别的在写这一段，
        或者能从这里新起一段（文件开头；MP4 以外的文件尾）。能时给 _Tee，用完一定要 close。"""
        with self._lock:
            entry = self._entry(iid, size, mtime, ext, True)
            if entry is None or start >= entry.size or entry.stored() >= self.file_max:
                self._drop_empty(entry)
                return None
            region = next((r for r in entry.regions if r.start + r.length == start and start < r.end), None)
            if region is None and not any(r.start <= start < r.start + r.length for r in entry.regions):
                region = self._new_region(entry, start)
            if region is None or region.busy:
                self._drop_empty(entry)
                return None
            region.busy = True
            entry.used = self.clock()
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self._data(entry, region), os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, region.length)             # 上次没写完（中转被关掉）多出来的不要
            os.lseek(fd, region.length, os.SEEK_SET)
        except OSError as error:
            self._warn(error)
            with self._lock:
                region.busy = False
                if region.length == 0 and region in entry.regions:
                    entry.regions.remove(region)
                self._drop_empty(entry)
            return None
        return _Tee(self, entry, region, fd)

    def _new_region(self, entry: _Entry, start: int) -> _Region | None:
        """能不能从 start 新起一段（文件开头；MP4 以外的文件尾，只起一次）。在 self._lock 里调。"""
        known = entry.ext in MP4_EXTS or entry.ext in MKV_EXTS
        if start == 0 and not any(r.start == 0 for r in entry.regions):
            region = _Region(0, min(self.file_max if known else HEAD_OTHER, entry.size))
            if not known:
                entry.layout = UNKNOWN                  # 认不出的格式不认，开头就存 HEAD_OTHER
        elif entry.ext not in MP4_EXTS and start > 0 and start >= entry.size - TAIL \
                and not any(r.start > 0 or start < r.end for r in entry.regions):   # 不和开头那段重叠（小文件）
            region = _Region(start, entry.size)
        else:
            return None
        entry.regions.append(region)
        entry.regions.sort(key=lambda r: r.start)
        return region

    def _drop_empty(self, entry: _Entry | None) -> None:
        """一点都还没存的就不留。在 self._lock 里调。"""
        if entry is not None and not entry.busy() and entry.stored() == 0 and entry.key in self._entries:
            self._forget(entry)

    def drop(self, iid: str) -> None:
        """网盘上的文件和缓存的对不上（接着读时总长不一样）：这个条目的缓存整个作废（正在写的也不再写）。"""
        with self._lock:
            for entry in [e for e in self._entries.values() if e.iid == iid]:
                self._forget(entry)

    # ---- 预热（library.py 的 warm_step）----

    def todo(self, iid: str, size: int, mtime: int, ext: str) -> list[int]:
        """预热时还要从哪些地方接着读（升序）：一点都还没存时是文件开头；每一段还没存满的、没有别的在写的，从它存到的
        地方接着读（MP4 认出 moov 在文件尾以后就多了那一段）。这个文件已经存到 FILE_MAX 时没有。"""
        with self._lock:
            entry = self._entry(iid, size, mtime, ext, False)
            if entry is None:
                return [0] if size > 0 else []
            if entry.stored() >= self.file_max:
                return []
            return [r.start + r.length for r in entry.regions if not r.busy and r.length < r.end - r.start]

    def cues_at(self, iid: str, size: int, mtime: int, ext: str) -> int | None:
        """MKV / WebM：开头那段认得出 SeekHead 里记的 Cues、在最后 TAIL 字节里、文件尾那段还没起时，Cues 从哪起
        （预热从这里读，存成文件尾那段，和 ffmpeg 第一次跳转时读的地方一样）。"""
        if ext not in MKV_EXTS:
            return None
        with self._lock:
            entry = self._entry(iid, size, mtime, ext, False)
            head = next((r for r in entry.regions if r.start == 0), None) if entry is not None else None
            if head is None or head.busy or any(r.start > 0 for r in entry.regions):
                return None
            path, have = self._data(entry, head), head.length
        try:
            with open(path, "rb") as handle:
                data = handle.read(have)
        except OSError:
            return None
        found = mkv_cues(lambda offset, count: data[offset:offset + count], len(data), size)
        return found if found is not None and found >= max(have, size - TAIL) else None

    def room(self, keep_s: float) -> int:
        """还能再存多少字节而不挤掉 keep_s 秒内用过的（超了上限按最后用过的时间删最旧的，所以先删的是更早的）。"""
        with self._lock:
            now = self.clock()
            total = sum(e.stored() for e in self._entries.values())
            old = sum(e.stored() for e in self._entries.values() if not e.busy() and now - e.used >= keep_s)
            return self.total_max - total + old

    # ---- 上限 ----

    def total(self) -> int:
        with self._lock:
            return sum(e.stored() for e in self._entries.values())

    def prune(self) -> None:
        """索引里没有了、大小或修改时间变了的删；总共超过 total_max 时按最后用过的时间删最旧的（正在写的不删）。"""
        with self._lock:
            for entry in list(self._entries.values()):
                if entry.busy():
                    continue
                now = self.alive(entry.iid) if self.alive is not None else (entry.size, entry.mtime)
                if now is None or (int(now[0] or 0), int(now[1] or 0)) != (entry.size, entry.mtime) \
                        or entry.stored() == 0:
                    self._forget(entry)
            total = sum(e.stored() for e in self._entries.values())
            for entry in sorted(self._entries.values(), key=lambda e: e.used):
                if total <= self.total_max:
                    break
                if entry.busy():
                    continue
                total -= entry.stored()
                self._forget(entry)


class _Tee:
    """边放边存一段：write 每块从网盘读到的字节（只存到这一段的 end、这个文件的 FILE_MAX 为止；写不了就不存了，
    从不打断放片子）；close 记下存到哪、删多的、按上限删旧的。"""

    def __init__(self, cache: IndexCache, entry: _Entry, region: _Region, fd: int):
        self.cache, self.entry, self.region, self.fd = cache, entry, region, fd
        self.stopped = False

    def room(self) -> int:
        """这一段还能再存多少字节（预热按它读，不多读）。"""
        cache, entry, region = self.cache, self.entry, self.region
        with cache._lock:
            if self.stopped or cache._entries.get(entry.key) is not entry:
                return 0
            return max(0, min(region.end - region.start - region.length, cache.file_max - entry.stored()))

    def write(self, chunk: bytes) -> None:
        if self.stopped or not chunk:
            return
        cache, entry, region = self.cache, self.entry, self.region
        with cache._lock:
            alive = cache._entries.get(entry.key) is entry
            room = min(region.end - region.start - region.length, cache.file_max - entry.stored())
        if not alive or room <= 0:
            self.stopped = True
            return
        part = chunk[:room]
        try:
            os.write(self.fd, part)
        except OSError as error:
            cache._warn(error)
            self.stopped = True
            return
        with cache._lock:
            region.length += len(part)
            if region.start == 0 and entry.layout is None:
                self._learn()
        if len(part) < len(chunk):
            self.stopped = True

    def _learn(self) -> None:
        """开头这段又存长了一点：认一认格式。认出来就知道开头存到哪（多存的马上截掉）、MP4 的 mdat 后面从哪起
        （那一段等 ffmpeg 读到时再存）。在 cache._lock 里调。"""
        entry, region = self.entry, self.region
        layout = mp4_layout if entry.ext in MP4_EXTS else mkv_layout
        try:
            found = layout(lambda offset, count: os.pread(self.fd, count, offset), region.length, entry.size)
        except OSError:
            found = UNKNOWN
        if found is None:
            return
        entry.layout = found
        media, after = found
        region.end = min(entry.size, media + SLACK if media >= 0 else HEAD_OTHER)
        if region.length > region.end:
            region.length = region.end
            try:
                os.ftruncate(self.fd, region.length)
                os.lseek(self.fd, region.length, os.SEEK_SET)
            except OSError:
                pass
        if after is not None and after >= region.end and 0 < entry.size - after <= self.cache.file_max \
                and not any(r.start == after for r in entry.regions):
            entry.regions.append(_Region(after, entry.size))
            entry.regions.sort(key=lambda r: r.start)

    def close(self) -> None:
        cache, entry, region = self.cache, self.entry, self.region
        with cache._lock:
            region.busy = False
            try:
                os.close(self.fd)
            except OSError:
                pass
            if cache._entries.get(entry.key) is entry:           # drop 过的不管（文件已经删了）
                if region.length == 0:
                    entry.regions.remove(region)
                    cache._unlink(cache._data(entry, region))
                if entry.stored() == 0:
                    cache._forget(entry)
                else:
                    cache._save(entry)
        cache.prune()
