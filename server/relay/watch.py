"""在电脑上看：电脑版（/tv/console）里看片源的原画，有画面也有声音。两种看法：

- 跟随小电视：看选中的小电视正在播的台（web.py 的 watch_start），小电视换台电脑上也跟着换。
- 直接看一个台（web.py 的 watch_direct）：没有小电视、小电视没在线，或者在线但只想在电脑上看——
  全局频道表里的直播台、自己订阅的 UP 主和剧集、投屏（有设备时是那台的投屏位，没有时是自己投到电脑的）。
  电脑上看的进度和小电视的分开记（web.py 的 _pc_cursor，存进数据目录的 pc-progress.json）：第一次从小电视
  下次会播的地方起，之后从电脑上看到的地方起；电脑上看从不标小电视的「看过」、不改小电视的进度，免得在电脑上
  随便翻翻就把小电视的剧集往后推了。

小电视上的画面是 320x180 / 240x180 的调色板小图，放大了没法看；电脑上给的是**片源本身**：中转按设备
正在播的台找到片源（直播台：频道表里的地址和 User-Agent；UP 主、剧集、投屏：B 站的地址，带 Referer 和
UA，或者 YouTube 节点的流），起一个 ffmpeg 把它封装成分片 MP4 写到管道，这里切成 HLS（fMP4 段，只在
内存里留最近几段），经中转的网页端口同源给浏览器（/tv/watch/…）。浏览器不直接连片源：B 站和不少直播源
要 Referer / UA，跨域也取不到。电脑版页面用浏览器自带的 MediaSource 放这些段（console.html 的
WatchPlayer，几十行，不用第三方播放器）；没有 MediaSource 的浏览器（老 iPhone Safari）直接放 HLS。

- 编码：画面是 H.264 就只换封装（-c:v copy）；别的（HEVC、MPEG-2……）才转码（libx264 veryfast，最高 720p）。
  ffmpeg 先按「复制画面」起，stderr 打出输入流的编码后，不合适就按需要重起一次（只在要转码的源上多花一两秒）。
  声音一律转成 AAC 128k（很便宜）：直播源（MPEG-TS、HLS）里的 AAC 带 ADTS 头，只换封装的话分片 MP4 的
  初始化段里没有 AudioSpecificConfig（ffmpeg 写头时还没见到第一个包），浏览器解不了。只有声音的源（电台）
  按 2 秒切段。
- 不和小电视同步：视频从设备此刻的位置起播，直播从现在起播，之后各走各的（页面上写明）。
- 跳转（电脑版播放器的进度条）：重起一路（WatchService.open 的 restart），ffmpeg 带 -ss 从新的位置起，
  新的编号和令牌；播放器换一个新的 MediaSource，按列表里的 #EXT-X-RTV-ORIGIN 和回答里的 start 把媒体时间
  换算成视频里的位置（Window.origin）。
- 花费：只在有人看时才有 ffmpeg，每台设备（跟随）或每个浏览器页面（直接看）至多一路，每个用户至多
  MAX_PER_USER 路，全中转至多 MAX_STREAMS 路；IDLE_S 秒没人取列表或段就停
  （和 server/listen.py 一样）。ffmpeg 的输出是到中转的管道：中转没了（崩溃、被杀），ffmpeg 下一次写就失败
  退出，不会留下孤儿进程；也不往磁盘写临时文件。
- 只在自建 / 电脑客户端（中转带 --selfhost）开：云端那台小主机上行只有 3 Mbps，一路 720p 就占满，所以云端
  没有这个功能（接口 404，电脑版不显示按钮）。
- 权限：发地址的 POST /api/watch 只给这台设备的主人和成员（访客、别人的设备不行，web.py 的 watch_start）；
  地址里带短期令牌（设备 + 这一路的编号 + 到期时间的 HMAC，密钥由中转密钥派生），取列表和段只认令牌。

只用标准库；ffmpeg 由调用方给路径。
"""

from __future__ import annotations

import collections
import hashlib
import math
import os
import re
import secrets
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import listen as listen_audio
from ..frames import DEFAULT_USER_AGENT
from ..proxyenv import loopback_bypass
from .bilibili import BLOCKED_HINT as _BILIBILI_BLOCKED_HINT, blocked as _bilibili_blocked
from .stream import RECONNECT

# 多久没人取列表或段就停掉这一路（播放器按段长刷新列表，暂停时也刷新）。
IDLE_S = 20.0
# 地址里令牌的有效期：看得再久也够了；过期后页面重新要一个地址。
TOKEN_LIFETIME_S = 4 * 3600
# 全中转同时最多几路：每路一个 ffmpeg，要转码的源吃一个 CPU 核。
MAX_STREAMS = 3
# 每个用户同时最多几路（两台电脑、或者两个页面）。
MAX_PER_USER = 2
# 列表里留几段；按字节再封顶（4K 直播源复制过来一段可能十几 MB）。
WINDOW_SEGMENTS = 12
WINDOW_BYTES = 64 * 1024 * 1024
# 一段至少多长：短 GOP 的源合并成 2 秒左右一段；只有声音的源按这个时长切。
FRAGMENT_US = 2_000_000
# 取列表时最多等多久（片源要几秒才连上、出第一段）；没好就回 503，页面再来取。要比 IDLE_S 短：
# 等的时候没有新的请求，太长会被当作没人看停掉。
READY_WAIT_S = 15.0
# 等 ffmpeg 报出输入流编码的最长时间（连片源、读头）。
PROBE_WAIT_S = 30.0
# 单个 MP4 盒子的上限：防坏数据把内存撑爆。
BOX_MAX = 64 * 1024 * 1024
# ffmpeg 能用的协议：直播源和 B 站只用网络协议，绝不读本机文件（远端的 m3u8 里写 file: 也打不开）。
NETWORK_PROTOCOLS = "http,https,tls,tcp,crypto,httpproxy"
PIPE_PROTOCOLS = "pipe"
DEFAULT_CODECS = "avc1.640028,mp4a.40.2"
_SID = re.compile(r"^[0-9a-f]{16}$")
_PATH = re.compile(r"^/watch/([0-9a-f]{16})/([0-9]{1,12}\.[A-Za-z0-9_-]{16,32})/"
                   r"(master\.m3u8|index\.m3u8|init\.mp4|(\d{1,9})\.m4s)$")
_STREAM_LINE = re.compile(r"^\s*Stream #0:\d+\S*: (Video|Audio): ([A-Za-z0-9_]+)")


def derive_secret(key: str) -> bytes:
    """令牌的密钥：从中转的密钥派生（和「用手机听」的不同，一个令牌不能当另一个用）。"""
    return hashlib.sha256(b"ai-passport-tv watch v1\0" + key.strip().encode("utf-8")).digest()


# ---------------------------------------------------------------- 片源和 ffmpeg

@dataclass(frozen=True)
class Source:
    """一路要看的片源。key 认「是不是同一个」：同一台设备再要同一个片源，就接着用已经在转的那一路。

    url：地址，或者起 ffmpeg 时才取地址的函数（B 站的地址会过期、取地址要访问 B 站，放到后台线程里做）。
    opener：YouTube 节点的流（进程样的对象，stdout.read1()、kill()），喂给 ffmpeg 的标准输入；有它时不用 url。
    paced：-re，按实时速度读（视频文件不这样会一下子读完，直播源 live.py 也这样读）。
    """
    key: tuple
    url: str | Callable[[], str] = ""
    user_agent: str = ""
    referer: str = ""
    offset: float = 0.0
    paced: bool = True
    opener: Callable[[], object] | None = field(default=None, compare=False)
    label: str = ""
    # 片源在哪个网站（"bilibili"、"youtube"；直播台为空）：出错时说给人听的话按它来（explain）。
    site: str = ""
    # 视频从第几秒起（给人和进度估计看的；ffmpeg 只认 offset，YouTube 节点自己从 start 起，offset 为 0）。
    start: float = 0.0
    # 电脑版播放器的清晰度菜单：登录了 B 站、选了 1080P 时是 "1080p"（点播 vod.py 那条路最高 720p）；别的为空。
    quality: str = ""


@dataclass(frozen=True)
class Mode:
    """video："copy" 只换封装，"encode" 转码；audio："encode"（声音一律转码，见上）；None 是源里没有这一路。"""
    video: str | None = "copy"
    audio: str | None = "encode"


FIRST_MODE = Mode("copy", "encode")


def input_codecs(lines: list[str]) -> tuple[bool, str | None, str | None]:
    """ffmpeg stderr 开头的输入信息 → (读完了没有, 第一路画面的编码, 第一路声音的编码)。

    「Stream mapping:」或「Output #0」出现时输入信息就打完了。封面图（attached pic）不算画面。
    """
    video = audio = None
    for line in lines:
        if line.startswith(("Stream mapping:", "Output #0")):
            return True, video, audio
        match = _STREAM_LINE.match(line)
        if not match or "(attached pic)" in line:
            continue
        if match.group(1) == "Video" and video is None:
            video = match.group(2).lower()
        elif match.group(1) == "Audio" and audio is None:
            audio = match.group(2).lower()
    return False, video, audio


def mode_for(video: str | None, audio: str | None) -> Mode | None:
    """该怎么转：H.264 画面只换封装，别的画面转码；声音转成 AAC；什么都没有时 None。"""
    if video is None and audio is None:
        return None
    return Mode(None if video is None else "copy" if video == "h264" else "encode",
                None if audio is None else "encode")


def ffmpeg_args(ffmpeg: str, source: Source, url: str, mode: Mode) -> list[str]:
    """ffmpeg 的参数（列表，不经 shell）。url 是取到的地址；opener 的片源从标准输入读。"""
    piped = source.opener is not None
    args = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info"]
    if not piped:
        args.append("-nostdin")
    if source.paced:
        args.append("-re")
    if piped:
        args += ["-protocol_whitelist", PIPE_PROTOCOLS]
    else:
        args += ["-protocol_whitelist", NETWORK_PROTOCOLS, *RECONNECT]
        if source.user_agent:
            args += ["-user_agent", source.user_agent]
        if source.referer:
            args += ["-headers", f"Referer: {source.referer}\r\n"]
    if source.offset >= 1:
        args += ["-ss", str(int(source.offset))]
    args += ["-i", "pipe:0" if piped else url]
    # 大写 V：不要封面图。没有这一路时不报错（?）。
    args += ["-map", "0:V:0?", "-map", "0:a:0?", "-sn", "-dn"]
    if mode.video == "copy":
        args += ["-c:v", "copy"]
    elif mode.video == "encode":
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "4M", "-bufsize", "8M",
                 "-pix_fmt", "yuv420p", "-vf", "scale=-2:'min(720,ih)'",
                 "-force_key_frames", "expr:gte(t,n_forced*2)"]
    if mode.audio == "encode":
        args += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    if mode.video is not None:
        # 每段从关键帧起（Safari 和 MediaSource 都要），至少 2 秒。
        args += ["-movflags", "frag_keyframe+empty_moov+default_base_moof",
                 "-min_frag_duration", str(FRAGMENT_US)]
    else:
        args += ["-movflags", "empty_moov+default_base_moof", "-frag_duration", str(FRAGMENT_US)]
    args += ["-flush_packets", "1", "-f", "mp4", "pipe:1"]
    return args


# ---------------------------------------------------------------- 分片 MP4

def read_box(stream) -> tuple[bytes, bytes] | None:
    """从流里读一个顶层盒子：(类型, 整个盒子的字节)；读完了为 None。"""
    header = stream.read(8)
    if not header:
        return None
    if len(header) < 8:
        raise ValueError("MP4 盒子头不完整")
    size, kind = struct.unpack(">I4s", header)
    if size == 1:
        large = stream.read(8)
        if len(large) < 8:
            raise ValueError("MP4 盒子头不完整")
        header += large
        size = struct.unpack(">Q", large)[0]
    if size < len(header) or size > BOX_MAX:
        raise ValueError(f"MP4 盒子大小不对：{size}")
    body = stream.read(size - len(header))
    if len(body) < size - len(header):
        raise ValueError("MP4 盒子不完整")
    return kind, header + body


def children(data: bytes, start: int = 8):
    """盒子里的子盒子：(类型, 内容（不含头）)。start 是内容开始的位置（完整盒子从 8 开始）。"""
    at = start
    while at + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[at:at + 8])
        head = 8
        if size == 1 and at + 16 <= len(data):
            size, head = struct.unpack(">Q", data[at + 8:at + 16])[0], 16
        elif size == 0:
            size = len(data) - at
        if size < head or at + size > len(data):
            return
        yield kind, data[at + head:at + size]
        at += size


def _child(data: bytes, *path: bytes, start: int = 0) -> bytes | None:
    for name in path:
        found = next((body for kind, body in children(data, start) if kind == name), None)
        if found is None:
            return None
        data, start = found, 0
    return data


def _descriptor(data: bytes, at: int) -> tuple[int, int, int] | None:
    """MPEG-4 描述符：(标签, 内容开始, 内容结束)。"""
    if at >= len(data):
        return None
    tag, at, size = data[at], at + 1, 0
    for _ in range(4):
        if at >= len(data):
            return None
        byte = data[at]
        at += 1
        size = (size << 7) | (byte & 0x7F)
        if not byte & 0x80:
            break
    return tag, at, min(len(data), at + size)


def _aac_object_type(esds: bytes) -> int | None:
    """esds（含版本和标志的 4 字节）里 AudioSpecificConfig 的 audioObjectType。"""
    found = _descriptor(esds, 4)
    if found is None or found[0] != 0x03:
        return None
    _, at, end = found
    flags = esds[at + 2] if at + 2 < end else 0
    at += 3 + (2 if flags & 0x80 else 0) + (2 if flags & 0x20 else 0)
    if flags & 0x40 and at < end:
        at += 1 + esds[at]
    found = _descriptor(esds, at)
    if found is None or found[0] != 0x04:
        return None
    found = _descriptor(esds, found[1] + 13)
    if found is None or found[0] != 0x05 or found[1] >= found[2]:
        return None
    kind = esds[found[1]] >> 3
    return kind or None


def init_info(moov: bytes) -> dict:
    """moov（整个盒子）→ {轨道号: {"kind": "vide"/"soun", "timescale", "codec", "duration"（trex 的默认采样时长）}}。"""
    tracks: dict[int, dict] = {}
    for kind, trak in children(moov):
        if kind != b"trak":
            continue
        tkhd = _child(trak, b"tkhd")
        mdhd = _child(trak, b"mdia", b"mdhd")
        hdlr = _child(trak, b"mdia", b"hdlr")
        if tkhd is None or mdhd is None or hdlr is None or len(hdlr) < 12:
            continue
        track_id = struct.unpack(">I", tkhd[20:24] if tkhd[0] == 1 else tkhd[12:16])[0]
        timescale = struct.unpack(">I", mdhd[20:24] if mdhd[0] == 1 else mdhd[12:16])[0]
        handler = hdlr[8:12].decode("latin-1")
        codec = ""
        stsd = _child(trak, b"mdia", b"minf", b"stbl", b"stsd")
        if stsd is not None:
            for entry, body in children(stsd, 8):
                name = entry.decode("latin-1")
                if name in ("avc1", "avc3"):
                    avcc = _child(body, b"avcC", start=78)
                    codec = f"{name}.{avcc[1]:02x}{avcc[2]:02x}{avcc[3]:02x}" if avcc and len(avcc) >= 4 \
                        else "avc1.640028"
                elif name == "mp4a":
                    esds = _child(body, b"esds", start=28)
                    codec = f"mp4a.40.{(_aac_object_type(esds) if esds else None) or 2}"
                else:
                    codec = name
                break
        tracks[track_id] = {"kind": handler, "timescale": timescale or 1, "codec": codec, "duration": 0}
    mvex = _child(moov, b"mvex", start=8)
    for kind, trex in children(mvex or b"", 0):
        if kind == b"trex" and len(trex) >= 16:
            track_id, _, default_duration = struct.unpack(">III", trex[4:16])
            if track_id in tracks:
                tracks[track_id]["duration"] = default_duration
    return tracks


def codecs_of(tracks: dict) -> str:
    """HLS 的 CODECS：画面在前、声音在后。"""
    ordered = sorted(tracks.values(), key=lambda t: t["kind"] != "vide")
    names = [t["codec"] for t in ordered if t["codec"]]
    return ",".join(names) or DEFAULT_CODECS


def fragment_duration(moof: bytes, tracks: dict) -> float:
    """一个 moof 的时长（秒）：有画面按画面轨，否则按第一条轨；采样时长取自 trun，没有就用默认值。"""
    durations: dict[int, int] = {}
    for kind, traf in children(moof):
        if kind != b"traf":
            continue
        tfhd = _child(traf, b"tfhd")
        if tfhd is None or len(tfhd) < 8:
            continue
        flags = int.from_bytes(tfhd[1:4], "big")
        track_id = struct.unpack(">I", tfhd[4:8])[0]
        at = 8 + (8 if flags & 0x01 else 0) + (4 if flags & 0x02 else 0)
        default = struct.unpack(">I", tfhd[at:at + 4])[0] if flags & 0x08 else \
            tracks.get(track_id, {}).get("duration", 0)
        total = 0
        for name, trun in children(traf, 0):
            if name != b"trun" or len(trun) < 8:
                continue
            tflags = int.from_bytes(trun[1:4], "big")
            count = struct.unpack(">I", trun[4:8])[0]
            at = 8 + (4 if tflags & 0x001 else 0) + (4 if tflags & 0x004 else 0)
            if not tflags & 0x100:
                total += count * default
                continue
            step = 4 * sum(1 for bit in (0x100, 0x200, 0x400, 0x800) if tflags & bit)
            for n in range(count):
                if at + n * step + 4 > len(trun):
                    break
                total += struct.unpack(">I", trun[at + n * step:at + n * step + 4])[0]
        durations[track_id] = durations.get(track_id, 0) + total
    if not durations:
        return 0.0
    video = [tid for tid in durations if tracks.get(tid, {}).get("kind") == "vide"]
    chosen = video[0] if video else min(durations)
    return durations[chosen] / tracks.get(chosen, {}).get("timescale", 1)


def fragment_start(moof: bytes, tracks: dict) -> float | None:
    """一个 moof 从媒体时间的第几秒起（tfdt 的 baseMediaDecodeTime）：有画面按画面轨，否则按第一条轨；没有 tfdt 时 None。"""
    starts: dict[int, int] = {}
    for kind, traf in children(moof):
        if kind != b"traf":
            continue
        tfhd = _child(traf, b"tfhd")
        tfdt = _child(traf, b"tfdt")
        if tfhd is None or len(tfhd) < 8 or tfdt is None or len(tfdt) < 8:
            continue
        track_id = struct.unpack(">I", tfhd[4:8])[0]
        if tfdt[0] == 1 and len(tfdt) >= 12:
            starts[track_id] = struct.unpack(">Q", tfdt[4:12])[0]
        else:
            starts[track_id] = struct.unpack(">I", tfdt[4:8])[0]
    if not starts:
        return None
    video = [tid for tid in starts if tracks.get(tid, {}).get("kind") == "vide"]
    chosen = video[0] if video else min(starts)
    return starts[chosen] / tracks.get(chosen, {}).get("timescale", 1)


class Window:
    """内存里的 HLS：初始化段、最近几段、播放列表。

    origin：第一段的媒体时间（tfdt，秒）。播放器里的 currentTime 是媒体时间，减去它再加上这一路从视频的
    第几秒起（Source.start），就是视频里真正的位置（电脑版播放器的进度条，console.js 的 WatchPlayer）。
    写在列表的 #EXT-X-RTV-ORIGIN 里（自定义的标签，别的播放器会忽略）。"""

    def __init__(self, keep: int = WINDOW_SEGMENTS, keep_bytes: int = WINDOW_BYTES):
        self.keep = keep
        self.keep_bytes = keep_bytes
        self.init: bytes | None = None
        self.codecs = DEFAULT_CODECS
        self.segments: collections.deque[tuple[int, float, bytes]] = collections.deque()
        self.next_seq = 0
        self.target = 2
        self.ended = False
        # 一共出了多少秒（停了以后也留着）：估计电脑上看到了哪里（web.py 的 _pc_position）。
        self.total_s = 0.0
        self.origin: float | None = None

    def add(self, duration: float, data: bytes, start: float | None = None) -> None:
        if self.origin is None and not self.segments and self.next_seq == 0:
            self.origin = start if start is not None else 0.0
        self.segments.append((self.next_seq, duration, data))
        self.next_seq += 1
        self.total_s += duration
        self.target = max(self.target, math.ceil(duration))
        while len(self.segments) > self.keep or \
                (len(self.segments) > 2 and sum(len(s[2]) for s in self.segments) > self.keep_bytes):
            self.segments.popleft()

    def release(self) -> None:
        """这一路不要了：放掉段占的内存。换一个新的 deque 而不是清空，正在读旧的那个的请求不受影响。"""
        self.segments = collections.deque()
        self.init = None

    def segment(self, seq: int) -> bytes | None:
        return next((data for n, _, data in self.segments if n == seq), None)

    def playlist(self) -> str:
        first = self.segments[0][0] if self.segments else self.next_seq
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{self.target}",
                 f"#EXT-X-MEDIA-SEQUENCE:{first}", "#EXT-X-INDEPENDENT-SEGMENTS", '#EXT-X-MAP:URI="init.mp4"']
        if self.origin is not None:
            lines.insert(3, f"#EXT-X-RTV-ORIGIN:{self.origin:.3f}")
        for seq, duration, _ in self.segments:
            lines += [f"#EXTINF:{duration:.3f},", f"{seq}.m4s"]
        if self.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def master(self) -> str:
        seconds = sum(d for _, d, _ in self.segments)
        size = sum(len(data) for _, _, data in self.segments)
        bandwidth = int(size * 8 / seconds) if seconds > 0 else 3_000_000
        return ("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-INDEPENDENT-SEGMENTS\n"
                f'#EXT-X-STREAM-INF:BANDWIDTH={max(bandwidth, 64_000)},CODECS="{self.codecs}"\nindex.m3u8\n')


# ---------------------------------------------------------------- 一路

class WatchStream:
    """一路：后台线程起 ffmpeg（必要时按编码重起一次）、把输出切成段放进 Window。

    device 是这一路占的位置（slot）：跟随小电视时是设备 id，直接看一个台时是「pc<用户>-<页面>」；
    令牌绑的是它和 sid。user 是开这一路的用户（每个用户的上限按它数）。"""

    def __init__(self, sid: str, device: str, source: Source, ffmpeg: str, log=print,
                 clock=time.monotonic, popen=subprocess.Popen, user: int | None = None):
        self.sid = sid
        self.device = device
        self.user = user
        self.source = source
        self.ffmpeg = ffmpeg
        self.log = log
        self.clock = clock
        self.popen = popen
        self.window = Window()
        self.created = self.seen = clock()
        self.error = ""
        self.stopped = False
        self.mode: Mode | None = None
        self._proc = None
        self._upstream = None
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self.stderr: collections.deque[str] = collections.deque(maxlen=12)

    # ---- 状态 ----

    @property
    def alive(self) -> bool:
        """还在转（或者还在起）：没停、没出错、没播完。"""
        return not self.stopped and not self.error and not self.window.ended

    def touch(self) -> None:
        self.seen = self.clock()

    def ready(self) -> bool:
        return self.window.init is not None and bool(self.window.segments)

    def wait_ready(self, timeout: float) -> bool:
        """等到有初始化段和至少一段、或者出错 / 结束 / 停了；返回是否有东西可放。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self.ready() and self.alive:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(min(left, 1.0))
            return self.ready()

    def _notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    # ---- 起停 ----

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"watch-{self.sid}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopped = True
        self._kill()
        self._notify()

    def _kill(self) -> None:
        for thing in (self._upstream, self._proc):
            if thing is None:
                continue
            try:
                thing.kill()
            except OSError:
                pass

    def _run(self) -> None:
        mode, retried = FIRST_MODE, False
        try:
            while not self.stopped:
                outcome, detail = self._attempt(mode)
                if outcome == "restart":
                    self.log(f"在电脑上看 {self.device}：片源是 {detail[1]}，"
                             f"{'画面转码' if detail[0].video == 'encode' else '没有画面' if detail[0].video is None else '画面只换封装'}")
                    mode = detail[0]
                    continue
                if outcome == "copy_failed" and not retried:
                    # 复制画面失败、一段也没出（少见的 H.264 变体）：转码再试一次。
                    retried = True
                    mode = Mode("encode", mode.audio)
                    self.log(f"在电脑上看 {self.device}：只换封装不行，改成转码")
                    continue
                if outcome == "ended":
                    self.window.ended = True
                elif not self.stopped:
                    self.error = explain(self.source.site, detail or "片源打不开")
                    # 已经有段的：列表收尾，播放器放完已有的就停。
                    self.window.ended = bool(self.window.segments)
                    self.log(f"在电脑上看 {self.device} 停了：{self.error}")
                return
        except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死：记下来，页面上看到出错
            self.error = f"{type(error).__name__}: {error}"
            self.log(f"在电脑上看 {self.device} 出错：{self.error}")
        finally:
            self._kill()
            self._notify()

    def _attempt(self, mode: Mode) -> tuple[str, object]:
        """起一次 ffmpeg。返回 ("restart", (新的 Mode, 编码说明))、("ended", None)、("copy_failed", 原因)、("failed", 原因)。"""
        source = self.source
        url = ""
        if source.opener is None:
            try:
                url = source.url() if callable(source.url) else source.url
            except Exception as error:  # noqa: BLE001 —— 取地址失败（B 站接口不通、视频下架……）
                if getattr(error, "permanent", False):
                    return "failed", f"放不了这个视频：{error}"
                return "failed", f"取不到片源地址：{error}"
            if not str(url).startswith(("http://", "https://")):
                return "failed", "片源地址不是 http(s)"
        args = ffmpeg_args(self.ffmpeg, source, url, mode)
        proc = self.popen(args, stdin=subprocess.PIPE if source.opener else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=loopback_bypass(os.environ))
        self._proc = proc
        try:
            return self._follow(proc, mode)
        finally:
            for pipe in (proc.stdout, proc.stderr, proc.stdin):
                try:
                    if pipe is not None:
                        pipe.close()
                except (OSError, ValueError):
                    pass

    def _follow(self, proc, mode: Mode) -> tuple[str, object]:
        source = self.source
        if self.stopped:
            self._kill()
            return "failed", ""
        lines: list[str] = []
        probed = threading.Event()
        threading.Thread(target=self._read_stderr, args=(proc, lines, probed), daemon=True).start()
        if source.opener is not None:
            threading.Thread(target=self._feed, args=(proc,), daemon=True).start()
        probed.wait(PROBE_WAIT_S)
        done, video, audio = input_codecs(lines)
        wanted = mode_for(video, audio)
        # 输入信息打出来了、但这种转法 ffmpeg 起不来（例如只有声音的源用了按关键帧切段）：照编码换一种转法。
        if not done and (wanted is None or wanted == mode):
            self._kill()
            proc.wait()
            return "failed", self._reason(lines) or "片源连不上"
        if wanted is None:
            self._kill()
            proc.wait()
            return "failed", "片源里没有画面也没有声音"
        if wanted != mode:
            self._kill()
            proc.wait()
            return "restart", (wanted, f"{video or '无画面'} + {audio or '无声音'}")
        self.mode = mode
        produced = self._segment(proc.stdout)
        code = proc.wait()
        if self.stopped:
            return "failed", ""
        if code == 0:
            return "ended", None
        reason = self._reason(lines) or f"ffmpeg 退出码 {code}"
        if not produced and mode.video == "copy":
            return "copy_failed", reason
        return "failed", reason

    def _segment(self, stdout) -> int:
        """读 ffmpeg 的分片 MP4：ftyp+moov 是初始化段，每个 moof+mdat 是一段。返回出了几段。"""
        head = b""
        tracks: dict = {}
        pending = b""
        produced = 0
        while True:
            try:
                box = read_box(stdout)
            except (ValueError, OSError):
                break
            if box is None:
                break
            kind, data = box
            if kind in (b"ftyp", b"styp") and self.window.init is None:
                head += data
            elif kind == b"moov":
                tracks = init_info(data)
                if self.window.init is None:
                    self.window.codecs = codecs_of(tracks)
                    self.window.init = head + data
            elif kind == b"moof":
                pending = data
            elif kind == b"mdat" and pending:
                self.window.add(fragment_duration(pending, tracks), pending + data,
                                fragment_start(pending, tracks))
                pending = b""
                produced += 1
                self._notify()
        return produced

    def _read_stderr(self, proc, lines: list[str], probed: threading.Event) -> None:
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if not probed.is_set():
                    lines.append(line)
                    if input_codecs(lines)[0]:
                        probed.set()
                self.stderr.append(line)
        except (OSError, ValueError):
            pass
        finally:
            probed.set()

    def _feed(self, proc) -> None:
        """YouTube：节点的流 → ffmpeg 的标准输入。"""
        try:
            upstream = self.source.opener()
            self._upstream = upstream
            if self.stopped:
                upstream.kill()
                return
            while not self.stopped:
                chunk = upstream.stdout.read1(64 * 1024)
                if not chunk:
                    break
                proc.stdin.write(chunk)
        except Exception as error:  # noqa: BLE001 —— 节点打不开、ffmpeg 已经退出：都是这一路结束
            self.stderr.append(f"片源：{error}")
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def _reason(self, lines: list[str]) -> str:
        """给人看的失败原因：ffmpeg 最后几行里像错误的那一行（去掉地址，地址里可能有令牌）。"""
        recent = list(self.stderr) or lines
        for line in recent:
            if any(word in line for word in _GENERIC):
                continue
            if any(word in line for word in ("rror", "failed", "Invalid", "denied", "403", "404", "refused",
                                             "片源")):
                line = re.sub(r"^\[[^\]]* @ 0x[0-9a-f]+\]\s*", "", line.strip())
                return re.sub(r"https?://\S+", "<地址>", line)[:200]
        return ""


# ffmpeg 收尾时的套话，不是原因。
_GENERIC = ("Conversion failed", "Terminating thread", "Task finished", "Last message repeated")
# 像是网络不通（而不是片源拒绝）的说法：B 站、YouTube 连不上多半是路由器或代理的事。
_NETWORK = ("取不到片源地址", "Connection", "connection", "timed out", "Timeout", "timeout", "resolve",
            "Name or service", "nodename", "unreachable", "refused", "Network", "SSL", "TLS", "URLError",
            "Errno", "片源：")
# 片源明确拒绝（放不了，不是网络的事）。
_REFUSED = ("放不了这个视频", "403", "404", "410", "denied", "Forbidden")


def explain(site: str, reason: str) -> str:
    """给人看的出错原因：B 站、YouTube 连不上时说该去查哪里；原因本身（已去掉地址）放在括号里。"""
    reason = re.sub(r"https?://\S+", "<地址>", (reason or "片源打不开").strip())[:200]
    refused = any(word in reason for word in _REFUSED)
    network = not refused and any(word in reason for word in _NETWORK)
    if site == "bilibili":
        if network and _bilibili_blocked(reason):
            return _BILIBILI_BLOCKED_HINT
        if network:
            return f"B 站连不上：检查路由器或代理（{reason}）"
        if refused:
            return f"B 站说这个放不了（{reason}）"
    elif site == "youtube":
        if network:
            return f"YouTube 连不上：检查「设置 → YouTube」里的代理（{reason}）"
        if refused:
            return f"YouTube 说这个放不了（{reason}）"
    elif network:
        return f"直播源连不上（{reason}）"
    return reason


# ---------------------------------------------------------------- 管所有路

class Busy(Exception):
    """满了：per_user 为真是这个用户自己的路数到了上限（MAX_PER_USER），否则是全中转的（MAX_STREAMS）。"""

    def __init__(self, per_user: bool = False):
        super().__init__("per user" if per_user else "relay")
        self.per_user = per_user


class WatchService:
    """所有设备的「在电脑上看」：每台设备至多一路，没人看就停（reap，后台每秒一次）。"""

    def __init__(self, ffmpeg: str, secret: bytes, log=print, clock=time.monotonic, wall=time.time,
                 popen=subprocess.Popen, idle_s: float = IDLE_S, max_streams: int = MAX_STREAMS,
                 background: bool = True, max_per_user: int = MAX_PER_USER):
        self.ffmpeg = ffmpeg
        self.secret = secret
        self.log = log
        self.clock = clock
        self.wall = wall
        self.popen = popen
        self.idle_s = idle_s
        self.max_streams = max_streams
        self.max_per_user = max_per_user
        self.background = background
        self._by_device: dict[str, WatchStream] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        # 别处占着的名额：点播（vod.py 的 VodService.load）的 [(位置, 用户)]，和这里的一起数。
        self.also: Callable[[], list] = lambda: []

    def open(self, device: str, source: Source, user: int | None = None, restart: bool = False) -> WatchStream:
        """device 这个位置（设备 id，或直接看时的「pc<用户>-<页面>」）要看 source：同一个片源还在转就
        接着用，否则停掉旧的、起新的。满了（全中转 max_streams 路，或这个用户 max_per_user 路）时 Busy。
        restart：跳转（播放器上拖进度条）——同一个片源也重起一个 ffmpeg，从 source.offset / start 起；
        新的一路有新的编号，地址（令牌）跟着换，旧地址马上失效。"""
        with self._lock:
            current = self._by_device.get(device)
            if current is not None and current.alive and current.source.key == source.key and not restart:
                current.touch()
                return current
            others = [s.user for d, s in self._by_device.items() if d != device and s.alive]
            others += [u for d, u in self.also() if d != device]
            if len(others) >= self.max_streams:
                raise Busy()
            if user is not None and others.count(user) >= self.max_per_user:
                raise Busy(per_user=True)
            if current is not None:
                current.stop()
                current.window.release()
                del self._by_device[device]
            stream = WatchStream(secrets.token_hex(8), device, source, self.ffmpeg, log=self.log,
                                 clock=self.clock, popen=self.popen, user=user)
            self._by_device[device] = stream
            self._ensure_reaper()
        self.log(f"在电脑上看 {device}：开始 {source.label or source.key[1]}")
        stream.start()
        return stream

    def token(self, stream: WatchStream) -> str:
        return listen_audio.issue_token(self.secret, f"{stream.device}/{stream.sid}", now=self.wall(),
                                        lifetime_s=TOKEN_LIFETIME_S)

    def find(self, sid: str, token: str | None) -> WatchStream | None:
        """令牌对得上的那一路；不认识的编号、错的或过期的令牌都是 None。"""
        if not _SID.match(sid or ""):
            return None
        with self._lock:
            stream = next((s for s in self._by_device.values() if s.sid == sid), None)
        if stream is None or not listen_audio.check_token(self.secret, f"{stream.device}/{sid}", token,
                                                         now=self.wall()):
            return None
        return stream

    def close(self, device: str) -> bool:
        """关掉这个位置上的一路（页面上点了「关闭」）：不用等 IDLE_S，马上让出名额。"""
        with self._lock:
            stream = self._by_device.pop(device, None)
        if stream is None:
            return False
        stream.stop()
        stream.window.release()
        self.log(f"在电脑上看 {device}：关了")
        return True

    def streams(self) -> list[WatchStream]:
        with self._lock:
            return list(self._by_device.values())

    def reap(self) -> None:
        """停掉 idle_s 秒没人取的。"""
        now = self.clock()
        with self._lock:
            idle = [(d, s) for d, s in self._by_device.items() if now - s.seen > self.idle_s]
            for device, _ in idle:
                del self._by_device[device]
        for device, stream in idle:
            if stream.alive:
                self.log(f"在电脑上看 {device}：{int(self.idle_s)} 秒没人看，停了")
            stream.stop()
            stream.window.release()

    def stop_all(self) -> None:
        with self._lock:
            streams = list(self._by_device.values())
            self._by_device.clear()
        for stream in streams:
            stream.stop()

    def _ensure_reaper(self) -> None:
        if not self.background or (self._reaper is not None and self._reaper.is_alive()):
            return

        def run() -> None:
            while True:
                time.sleep(1.0)
                try:
                    self.reap()
                except Exception as error:  # noqa: BLE001 —— 后台线程不能死
                    self.log(f"在电脑上看：清理出错 {type(error).__name__}: {error}")
                with self._lock:
                    if not self._by_device:
                        self._reaper = None
                        return

        self._reaper = threading.Thread(target=run, name="watch-reaper", daemon=True)
        self._reaper.start()


# ---------------------------------------------------------------- 直播台的地址

def live_source(path: Path | None, channel: str) -> tuple[str, str] | None:
    """频道表（channels.txt 的格式：id | 名字 | 地址 [| User-Agent]）里这个台的 (地址, User-Agent)。
    只认 http(s) 地址；没写 User-Agent 的用和电视服务一样的默认值（frames.DEFAULT_USER_AGENT）。"""
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) in (3, 4) and parts[0] == channel and parts[2].startswith(("http://", "https://")):
            return parts[2], (parts[3] if len(parts) == 4 and parts[3] else DEFAULT_USER_AGENT)
    return None


# ---------------------------------------------------------------- HTTP（中转的网页端口）

PLAYLIST_TYPE = "application/vnd.apple.mpegurl"


def get(handler, relay, path: str) -> bool:
    """GET /watch/<编号>/<令牌>/<文件>（前端去掉了 /tv）：只认令牌，不看登录。处理了返回 True。"""
    if not path.startswith("/watch/"):
        return False
    service = getattr(relay, "watch", None)
    match = _PATH.match(path)
    stream = service.find(match.group(1), match.group(2)) if service is not None and match else None
    if stream is None:
        handler._not_found()
        return True
    stream.touch()
    name, seq = match.group(3), match.group(4)
    if name in ("master.m3u8", "index.m3u8"):
        if not stream.wait_ready(READY_WAIT_S):
            if stream.alive:
                handler._send(503, "还在连片源，稍后再试".encode(), "text/plain; charset=utf-8")
            else:
                handler._send(410, (stream.error or "这一路已经停了").encode(), "text/plain; charset=utf-8")
            return True
        stream.touch()
        text = stream.window.master() if name == "master.m3u8" else stream.window.playlist()
        handler._send(200, text.encode(), PLAYLIST_TYPE)
        return True
    data = stream.window.init if name == "init.mp4" else stream.window.segment(int(seq))
    if data is None:
        handler._not_found()
        return True
    handler._send(200, data, "video/mp4")
    return True
