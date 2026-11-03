"""片库剧集的分集封面：剧的详情页上每一集一张自己的图，不再都是同一张画出来的图。

从哪来（按顺序）：
1. 视频旁边的图片（Kodi 的叫法）：<名字>-thumb.jpg / .png / .webp、<名字>.jpg / .png。扫描时只记路径（library.py 的
   _thumb_of，条目的 thumb），页面要时才取一次（和海报一样经 Library._art_bytes，网盘的存在 library-art/）。
   文件夹里的 poster.jpg、folder.jpg 是这部剧的海报（条目的 poster，剧的卡片用），从不当某一集的封面。
2. 没有的：后台从这一集里截一帧（Thumbs），存在 library-thumb/<id>-<大小>-<修改时间>.jpg（600，文件夹 700），
   一直留着；大小或修改时间变了、条目不见了（扫描后 prune）、有了旁边的图片时删掉。网盘上的视频从不改。
豆瓣 / TMDB 的海报是整部剧、整季的，从不当分集封面。都没有时页面照旧画那张图。

怎么截（一集一个 ffmpeg、只解一帧）：
- 先探测（Library.media_of(warm=True)，多半已经探测过或在索引缓存里）拿时长和画面那一路。位置避开片头片尾和黑场：
  先试约 10% 处（夹在 90–600 秒之间），再试 25%（180–1200 秒）、50%（spots_of）；短片子按比例。
- ffmpeg 从那里往前最近的关键帧起只读一点（-ss 在 -i 前面、-noaccurate_seek、-t），只解关键帧（-skip_frame nokey）、
  单线程，缩到 THUMB_W 宽，存 JPEG（-q:v THUMB_Q）。同一遍里 signalstats 量亮度：九成的像素都很暗（YHIGH）或者
  几乎一样亮（YHIGH − YLOW，片头片尾的黑底白字、纯色）算黑场 / 空镜，换下一个位置；量不出时按 JPEG 太小算。
- HDR（PQ / HLG）的照小电视那一路做色调映射（library.tonemap_for：macOS 上 videotoolbox 的 scale_vt 在 GPU 上，
  不然 swscale 自己的、带 zimg 的 zscale；都不行就不映射，发灰也比没有强）。
- ffmpeg 读的是中转自己的 raw 地址的截图模式（Library.raw_url(thumb=True)，带 g=1，library.py 的 _serve_pre）：
  索引缓存、整集缓存里有的从本机给，没有的从网盘一小段一小段（THUMB_PIECE）读，读到的索引区顺手存进索引缓存；
  不存进整集缓存、不挪整集缓存的下载线程、不算「有人在放」。一次读的多半是索引区加一个 GOP（4K 的 3–12 MB）。

什么时候截（和预热、预读开头一样只在闲着时）：
- Library.warm_blocked 为空（没人在放、在看，没在扫描、没在缓存整集、没在预读）、预热也没在做时才开始一集；做着时
  有人开始放、在看了，或预读、整集缓存动起来了，马上停（yield_to_playback、_busy_elsewhere），下一轮接着来。
- 一个一个来，一轮至多读 BUDGET 字节、一集至多 FILE_MAX、RUN_MAX_S 秒；网盘出错了整个歇一会儿（越歇越久）；
  一集没截成（读不出、全是黑场）这一集歇 RETRY_S 再试，越歇越久，至多 RETRY_MAX_S。
- 先截新加进来的剧；打开一部剧的详情页时（web.py 的 library_preread → bump）这部剧的排到最前面。
- 管理员能让一集重新截一张（POST /api/admin/library/thumb {id}，refresh）：删掉现在的，从下一个位置截（不再是同一帧）。

日志一轮至多一行，只说截了几集、读了多少，从不写地址、令牌、路径。

只用标准库。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from ..proxyenv import loopback_bypass

MB = 1024 * 1024
# 封面多宽（高按比例）、JPEG 的质量（mjpeg 的 -q:v，2 最好、31 最差）。
THUMB_W = 480
THUMB_Q = 4
# 截图模式从网盘一次读多大一段（ffmpeg 读够了就断开，多读的至多这么多）。
THUMB_PIECE = 2 * MB
# 一轮至多读网盘这么多；一集至多这么多、这么久（4K 一个 5 秒的 GOP 约 10 MB）。
BUDGET = 100 * MB
FILE_MAX = 40 * MB
RUN_MAX_S = 90.0
# 从关键帧往后至多读这么久的数据（只要第一帧；-t 是输入的选项）。
READ_S = 0.5
# 截哪儿：(时长的比例, 至少, 至多) 秒，一个不行试下一个。短于「至少」的片子按比例。
SPOTS = ((0.10, 90.0, 600.0), (0.25, 180.0, 1200.0), (0.50, 0.0, 1e9))
# 不知道时长时试这几处（过了结尾 ffmpeg 给最后一个关键帧）。
SPOTS_UNKNOWN = (90.0, 10.0)
# 黑场 / 空镜：九成的像素都比 DARK_HIGH 暗，或者一成到九成的像素亮度差不到 FLAT_SPREAD（0–255）。
DARK_HIGH = 40.0
FLAT_SPREAD = 12.0
# 量不出亮度时：比这小的 JPEG 当黑场；比这大的不要（不是正常的一帧）。
SMALL_JPEG = 5 * 1024
BIG_JPEG = 1024 * 1024
# 一集没截成：歇这么久再试，每失败一次翻倍，至多这么久。
RETRY_S = 1800.0
RETRY_MAX_S = 2 * 86400.0
# 网盘出错：整个歇这么久，连着出错翻倍，至多这么久。
BACKOFF_S = 600.0
BACKOFF_MAX_S = 6 * 3600.0
# 中转起来后先等这么久（网盘组件还在起、预热先做）；之后每 TICK_S 一轮（扫描找到新的剧集、打开详情页时马上）。
START_S = 240.0
TICK_S = 300.0
# 打开详情页时一部剧至多排这么多集到最前面。
BUMP_MAX = 60

_NAME = re.compile(r"^([0-9a-f]{16})-(\d{1,15})-(-?\d{1,15})\.jpg$")
_STAT = re.compile(r"lavfi\.signalstats\.(YLOW|YHIGH|YAVG)=([0-9.]+)")


def spots_of(duration: float, shift: int = 0) -> list[float]:
    """截哪几处（秒，先试的在前）：约 10%（90–600 秒）、25%（180–1200 秒）、50%；片子短到夹出来的位置超过时长的
    八成时按比例。shift：管理员要过几次「重新截」，从后面的位置开始（不再是同一帧）。"""
    duration = float(duration or 0)
    if duration <= 0:
        found = list(SPOTS_UNKNOWN)
    else:
        found = []
        for share, low, high in SPOTS:
            at = min(max(duration * share, low), high)
            if at > duration * 0.8:
                at = duration * share
            at = round(at, 1)
            if at not in found:
                found.append(at)
    n = shift % len(found)
    return found[n:] + found[:n]


def thumb_args(ffmpeg: str, url: str, at: float, stream: int | None, out: str, tonemap: str = "") -> list[str]:
    """截一帧的 ffmpeg 参数：从 at 往前最近的关键帧起只读 READ_S 秒、只解关键帧、单线程；缩到 THUMB_W 宽（HDR 的按
    tonemap 映射成 BT.709），signalstats 的亮度打到标准输出，JPEG 写到 out。stream：画面那一路的序号（不知道时第一路
    真画面，封面图不算）。关键帧在 at 之前，时间戳是负的，图片的封装会把它丢掉：先 setpts 从 0 起。"""
    from .library import tonemap_input, tonemap_vf, TONEMAP_KINDS
    tonemap = tonemap if tonemap in TONEMAP_KINDS else ""
    scale = f"scale={THUMB_W}:-2:flags=bilinear"
    chain = tonemap_vf(tonemap, scale, f"w={THUMB_W}:h=-2") if tonemap else f"{scale},format=yuv420p"
    args = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", *tonemap_input(tonemap),
            "-protocol_whitelist", "http,tcp", "-threads", "1", "-skip_frame", "nokey", "-noaccurate_seek",
            "-ss", f"{max(0.0, float(at)):.3f}", "-t", str(READ_S), "-i", url,
            "-map", f"0:{stream}" if stream is not None else "0:V:0", "-an", "-sn", "-dn", "-frames:v", "1",
            "-vf", "setpts=PTS-STARTPTS," + chain + ",format=yuvj420p,signalstats,metadata=print:file=-",
            "-c:v", "mjpeg", "-q:v", str(THUMB_Q), "-f", "image2", "-y", out]
    return args


def parse_stats(text: str) -> dict[str, float]:
    """signalstats 打出来的第一帧的 YLOW / YHIGH / YAVG（0–255）；没有时空的。"""
    found: dict[str, float] = {}
    for key, value in _STAT.findall(text or ""):
        if key in found:
            break                                   # 第二帧了
        found[key] = float(value)
    return found


def looks_blank(stats: dict[str, float], size: int) -> bool:
    """这一帧是黑场、空镜（片头片尾的黑底白字、纯色）：量出了亮度按亮度，没量出按 JPEG 的大小。"""
    if "YHIGH" in stats and "YLOW" in stats:
        return stats["YHIGH"] < DARK_HIGH or stats["YHIGH"] - stats["YLOW"] < FLAT_SPREAD
    return size < SMALL_JPEG


class Thumbs:
    """library 是 Library（item、items、remote、media_of、with_color、raw_url、ffmpeg、secret、port、warm_blocked、
    _warm_lock、_warm_read、_serving、watching、pre、full、_load_sources、_art_bytes、_now_of）。root 是 library-thumb/。"""

    def __init__(self, library, root: Path, log: Callable[[str], None] = print, clock=time.time,
                 background: bool = True):
        self.library = library
        self.root = Path(root)
        self.log = log
        self.clock = clock
        self.background = background
        self._lock = threading.Lock()
        self._step_lock = threading.Lock()
        self._wake = threading.Event()
        self._have: dict[str, int] | None = None   # 存下的：<id>-<大小>-<修改时间> → 文件的修改时间（页面的 ?v=）
        self._front: list[str] = []                 # 先截的条目 id（管理员要重新截的、打开了详情页的那部剧）
        self._failed: dict[str, tuple[int, float]] = {}   # 键 → (失败了几次, 歇到什么时候)
        self._shift: dict[str, int] = {}            # 条目 id → 管理员要过几次「重新截」
        self._job: dict | None = None               # 正在截的 {iid, stop, drive}
        self._proc: subprocess.Popen | None = None
        self._read = 0
        self._after = 0.0
        self._backoff = 0.0
        self._closed = False
        self._thread: threading.Thread | None = None

    # ---- 存下的 ----

    @staticmethod
    def key_of(item: dict) -> str:
        return f"{item['id']}-{int(item.get('size') or 0)}-{int(item.get('mtime') or 0)}"

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.jpg"

    def _known(self) -> dict[str, int]:
        """存下的封面（第一次用时列一遍文件夹，之后记在内存里）。调用时拿着 _lock。"""
        if self._have is None:
            self._have = {}
            try:
                for path in self.root.iterdir():
                    if _NAME.match(path.name):
                        try:
                            self._have[path.name[:-4]] = int(path.stat().st_mtime)
                        except OSError:
                            pass
            except OSError:
                pass
        return self._have

    def has(self, item: dict) -> bool:
        with self._lock:
            return self.key_of(item) in self._known()

    def url(self, item: dict) -> str:
        """这一集的封面地址（页面的卡片用）：旁边有图片的用图片，截好了的用截的（?v= 换了图就换），都没有时空的。"""
        if item.get("kind") != "episode":
            return ""
        if item.get("thumb"):
            return f"api/library/thumb/{item['id']}"
        with self._lock:
            stamp = self._known().get(self.key_of(item))
        return f"api/library/thumb/{item['id']}?v={stamp}" if stamp is not None else ""

    def serve(self, handler, iid: str) -> None:
        """GET /api/library/thumb/<id>：旁边的图片（网盘的第一次要时取一次，和海报一样）或截好的封面。"""
        from .library import IMAGE_TYPES, _ext
        item = self.library.item(iid)
        miss = {"Cache-Control": "private, max-age=600"}
        if item is None or item.get("kind") != "episode":
            handler._send(404, b"", "text/plain; charset=utf-8", miss)
            return
        side = item.get("thumb") or ""
        if side:
            src = next((s for s in self.library._load_sources() if s["id"] == item["source"]), None)
            kind = IMAGE_TYPES.get(_ext(side))
            data = self.library._art_bytes(src, side) if src is not None and kind else None
        else:
            kind = "image/jpeg"
            try:
                data = self._path(self.key_of(item)).read_bytes() if self.has(item) else None
            except OSError:
                data = None
        if not data:
            handler._send(404, b"", "text/plain; charset=utf-8", miss)
            return
        handler._send(200, data, kind, {"Cache-Control": "private, max-age=86400"})

    def _drop(self, key: str) -> None:
        with self._lock:
            self._known().pop(key, None)
        try:
            self._path(key).unlink()
        except OSError:
            pass

    def prune(self) -> None:
        """扫描以后：条目不见了、大小或修改时间变了、有了旁边的图片的，截的封面删掉；没用的失败记录也忘掉。"""
        with self._lock:
            keys = list(self._known())
        for key in keys:
            iid, size, mtime = _NAME.match(key + ".jpg").groups()
            now = self.library._now_of(iid)
            item = self.library.item(iid) if now is not None else None
            if now is None or (int(now[0] or 0), int(now[1] or 0)) != (int(size), int(mtime)) \
                    or item is None or item.get("thumb") or item.get("kind") != "episode":
                self._drop(key)
        try:
            for path in self.root.glob(".*.tmp.jpg"):
                path.unlink()
        except OSError:
            pass
        with self._lock:
            for key in [k for k in self._failed if self.library._now_of(k[:16]) is None]:
                del self._failed[key]

    # ---- 截哪些 ----

    def due(self) -> list[dict]:
        """要截的剧集（先截的在前）：排到最前面的，再是新加进来的。旁边有图片的、截好了的、没有画面的、
        歇着的（没截成、还没到再试的时候）、来源没了的不算。"""
        sources = {s["id"] for s in self.library._load_sources()}
        now = self.clock()
        with self._lock:
            have = set(self._known())
            front = list(self._front)
            failed = dict(self._failed)
        found = {}
        for item in self.library.items():
            if item.get("kind") != "episode" or item.get("thumb") or int(item.get("size") or 0) <= 0 \
                    or item["source"] not in sources:
                continue
            key = self.key_of(item)
            if key in have or failed.get(key, (0, 0.0))[1] > now:
                continue
            media = item.get("media")
            if isinstance(media, dict) and media.get("streams") is not None \
                    and not any(st.get("t") == "video" and not st.get("pic") for st in media["streams"]):
                continue                            # 探测过、没有画面
            found[item["id"]] = item
        first = [found.pop(iid) for iid in front if iid in found]
        rest = sorted(found.values(), key=lambda i: (-(i.get("added") or 0), i.get("show") or "",
                                                       i.get("season") or 0, i.get("episode") or 0, i["path"]))
        return first + rest

    def bump(self, show: str) -> int:
        """打开了一部剧的详情页（show 是 Library.show_key）：这部剧还没有封面的各集排到最前面（按集数）。返回排了几集。"""
        lib = self.library
        episodes = [i for i in lib.items() if i.get("kind") == "episode" and lib.show_key(i.get("show") or "") == show]
        if not episodes:
            return 0
        wanted = [i["id"] for i in lib.episodes_of(episodes[0]) if not i.get("thumb") and not self.has(i)][:BUMP_MAX]
        if not wanted:
            return 0
        with self._lock:
            self._front = wanted + [iid for iid in self._front if iid not in wanted]
        self._wake.set()
        return len(wanted)

    def refresh(self, iid) -> dict:
        """管理员要这一集重新截一张：删掉现在的，从下一个位置截（排到最前面，没人在看时截）。"""
        item = self.library.item(iid) if isinstance(iid, str) else None
        if item is None or item.get("kind") != "episode":
            return {"ok": False, "error": "没有这一集（页面可能是旧的，请刷新）"}
        if item.get("thumb"):
            return {"ok": False, "error": "这一集用的是视频旁边的图片，要换就换掉那张图片"}
        key = self.key_of(item)
        had = self.has(item)
        self._drop(key)
        with self._lock:
            if had:
                self._shift[iid] = self._shift.get(iid, 0) + 1
            self._failed.pop(key, None)
            self._front = [iid] + [i for i in self._front if i != iid]
        self._wake.set()
        return {"ok": True, "queued": True}

    # ---- 让开 ----

    def busy(self) -> bool:
        """正在截（预热、预读的后台不和它抢网）。"""
        with self._lock:
            return self._job is not None

    def stopping(self) -> bool:
        """给 Library._serve_pre：截图停下了（有人开始放了、关了）。"""
        with self._lock:
            return self._closed or self._job is None or self._job["stop"]

    def drive_error(self) -> None:
        """给 Library._serve_pre：从网盘读出错了（这一轮停下、歇一会儿，不算这一集没截成）。"""
        with self._lock:
            if self._job is not None:
                self._job["drive"] = True

    def tally(self, count: int) -> None:
        with self._lock:
            self._read += count

    # 只数从网盘读的（_serve_pre 从本机给的不数）。
    tally.drive_only = True

    def yield_to_playback(self) -> None:
        """有人开始真放片库的片子：正在截的马上停（下一轮接着来）。"""
        with self._lock:
            if self._job is not None:
                self._job["stop"] = True

    def _busy_elsewhere(self) -> bool:
        lib = self.library
        return lib._serving > 0 or (lib.watching is not None and lib.watching()) or lib.pre.busy() or lib.full.busy()

    # ---- 截 ----

    def step(self) -> dict:
        """截一轮（一集一集来，先截的在前）：返回 {"files": 截好了几集, "failed": 几集没截成, "bytes": 读了网盘多少,
        "stopped": 为什么停下（做完了是空串）}。做了点什么时记一行日志（不带地址、路径）。"""
        result = {"files": 0, "failed": 0, "bytes": 0, "stopped": ""}
        lib = self.library
        if not lib.ffmpeg or lib.secret is None or lib.port is None:
            return dict(result, stopped="没有 ffmpeg")
        if not self._step_lock.acquire(blocking=False):
            return dict(result, stopped="已经在截")
        try:
            if self.clock() < self._after:
                return dict(result, stopped="网盘刚出过错，歇一会儿")
            drive_failed = False
            for item in self.due():
                if result["bytes"] >= BUDGET:
                    result["stopped"] = "这一轮读够了"
                    break
                why = lib.warm_blocked() or ("在预热" if lib._warm_lock.locked() else "")
                if why:
                    result["stopped"] = why
                    break
                with self._lock:
                    if self._closed:
                        result["stopped"] = "关了"
                        break
                    self._front = [i for i in self._front if i != item["id"]]
                outcome, read = self.grab(item)
                result["bytes"] += read
                if outcome == "done":
                    result["files"] += 1
                elif outcome == "drive":
                    self._backoff = min(BACKOFF_MAX_S, self._backoff * 2 or BACKOFF_S)
                    self._after = self.clock() + self._backoff
                    result["stopped"] = "网盘出错了"
                    drive_failed = True
                    break
                elif outcome == "stopped":
                    with self._lock:
                        self._front.insert(0, item["id"])
                    result["stopped"] = lib.warm_blocked() or "有人开始放了"
                    break
                else:
                    result["failed"] += 1
                    key = self.key_of(item)
                    with self._lock:
                        tries = self._failed.get(key, (0, 0.0))[0] + 1
                        self._failed[key] = (tries, self.clock() + min(RETRY_MAX_S, RETRY_S * 2 ** (tries - 1)))
            if not drive_failed:
                self._backoff = 0.0
        finally:
            self._step_lock.release()
        if result["files"] or result["failed"] or result["bytes"]:
            line = f"片库分集封面：截好了 {result['files']} 集，读了网盘 {result['bytes'] // MB} MB"
            if result["failed"]:
                line += f"，{result['failed']} 集没截成（过一会儿再试）"
            self.log(line + (f"，停下了：{result['stopped']}" if result["stopped"] else ""))
        return result

    def grab(self, item: dict) -> tuple[str, int]:
        """截一集：(结果, 读了网盘多少字节)。结果是 "done"、"failed"（读不出、全是黑场）、"stopped"（有人开始放了）、
        "drive"（网盘出错了）。"""
        from .library import _LOCK, first_video, is_hdr, tonemap_for, tv_hwaccel
        lib = self.library
        iid = item["id"]
        with _LOCK:                                 # 探测从网盘读的数进 _warm_read（和预热一样）
            before = lib._warm_read
        media = lib.media_of(iid, warm=True)          # 多半探测过了；没有时读的多半是索引区
        if media is not None:
            media = lib.with_color(iid, media, warm=True)
        with _LOCK:
            read = max(0, lib._warm_read - before)
        if media is None:
            return "failed", read
        video = first_video(media)
        if video is None:
            return "failed", read
        tonemap = tonemap_for(lib.ffmpeg, video, tv_hwaccel(lib.ffmpeg)) if is_hdr(video) else ""
        with self._lock:
            shift = self._shift.get(iid, 0)
        key = self.key_of(item)
        for at in spots_of(float(media.get("dur") or 0), shift):
            outcome, got, size, stats = self._run(item, video, at, tonemap)
            read += got
            if outcome != "done":
                return outcome, read
            if size and not looks_blank(stats, size):
                tmp = self.root / f".{key}.tmp.jpg"
                try:
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, self._path(key))
                    stamp = int(self._path(key).stat().st_mtime)
                except OSError:
                    return "failed", read
                with self._lock:
                    self._known()[key] = stamp
                    self._failed.pop(key, None)
                return "done", read
        try:
            (self.root / f".{key}.tmp.jpg").unlink()
        except OSError:
            pass
        return "failed", read

    def _run(self, item: dict, video: dict, at: float, tonemap: str) -> tuple[str, int, int, dict]:
        """起一个 ffmpeg 截 at 那一帧：(结果, 读了网盘多少, JPEG 多大, 亮度)。"""
        lib = self.library
        key = self.key_of(item)
        tmp = self.root / f".{key}.tmp.jpg"
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            tmp.unlink(missing_ok=True)
        except OSError:
            return "failed", 0, 0, {}
        args = thumb_args(lib.ffmpeg, lib.raw_url(item["id"], thumb=True), at, video.get("i"), str(tmp), tonemap)
        with self._lock:
            if self._closed:
                return "stopped", 0, 0, {}
            self._job = {"iid": item["id"], "stop": False, "drive": False}
            self._read = 0
        began = time.monotonic()
        outcome, out = "failed", b""
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=loopback_bypass(os.environ))
        except OSError:
            proc = None
        if proc is not None:
            with self._lock:
                self._proc = proc
            while proc.poll() is None:
                with self._lock:
                    stop = self._closed or self._job["stop"]
                    too_much = self._read > FILE_MAX
                if stop or self._busy_elsewhere():
                    outcome = "stopped"
                if outcome == "stopped" or too_much or time.monotonic() - began > RUN_MAX_S:
                    proc.kill()
                    break
                time.sleep(0.1)
            else:
                outcome = "done" if proc.returncode == 0 else "failed"
            try:
                out = proc.communicate(timeout=10)[0] or b""
            except (OSError, subprocess.SubprocessError, ValueError):
                proc.kill()
            with self._lock:
                self._proc = None
        with self._lock:
            read, job, self._job = self._read, self._job, None
        if job is not None and job["stop"] and outcome != "done":
            outcome = "stopped"
        elif job is not None and job["drive"]:
            outcome = "drive"
        size = 0
        if outcome == "done":
            try:
                size = tmp.stat().st_size
            except OSError:
                size = 0
            if not 0 < size <= BIG_JPEG:
                outcome = "failed"
        if outcome != "done":
            try:
                tmp.unlink()
            except OSError:
                pass
            return outcome, read, 0, {}
        return outcome, read, size, parse_stats(out.decode("utf-8", "replace"))

    # ---- 后台 ----

    def run_forever(self) -> None:
        self._wake.wait(START_S)
        self._wake.clear()
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                self.step()
            except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                self.log(f"片库分集封面出错：{type(error).__name__}")
            self._wake.wait(TICK_S)
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        with self._lock:
            if not self.background or self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self.run_forever, name="library-thumb", daemon=True)
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

