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
  片库（library.py）的文件例外：浏览器说放得了 HEVC 时 HEVC 也只换封装（Source.copy_video，写成 hvc1），
  -map 按探测好的流（第一路真画面、挑好的声音，不带字幕和附件），有 AAC 的声音直接复制（Mode.audio "copy"），
  时间轴照原样（Source.exact）；不按实时读，跑在播放器前面但至多攒 VOD_AHEAD_*（WatchStream._hold）。
  ffmpeg 先按「复制画面」起，stderr 打出输入流的编码后，不合适就按需要重起一次（要转码的源多连一次片源）；
  直播台的转法记在内存里（LiveMemory），下次直接按它起。
  声音一律转成 AAC 128k（很便宜）：直播源（MPEG-TS、HLS）里的 AAC 带 ADTS 头，只换封装的话分片 MP4 的
  初始化段里没有 AudioSpecificConfig（ffmpeg 写头时还没见到第一个包），浏览器解不了。只有声音的源（电台）
  按 2 秒切段。
- 不和小电视同步：视频从设备此刻的位置起播，直播从现在起播，之后各走各的（页面上写明）。
- 换台快（电脑上直接看直播）：慢的是连片源（跳转、列表、第一段，每一步约 1 秒）。所以：直播源少探测一点
  （LIVE_PROBE）；记住跳转以后真正的列表地址（LiveMemory，RESOLVED_S）；刚换走的台再转一会儿、相邻的台先转起来
  （WatchService 的预热，WARM_S、WARM_MAX，只在开了 --pc-warm 时）。
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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .. import listen as listen_audio, routes as live_routes
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
# 直播源（MPEG-TS、HLS）读多少就开始转：默认 5 秒 / 5 MB，直播源的头里就有编码，1 秒、1 MB 够了。
# 量过（CCTV1，咪咕 6 秒一段）：慢的是连片源（跳转、列表、第一段，每一步约 1 秒），探测只占零点几秒。
LIVE_PROBE = ("-probesize", "1000000", "-analyzeduration", "1000000")
# 电脑上看直播换台时：刚换走的台再转这么久（换回来马上有画面），预热的台（相邻的台）没人看最多转这么久。
WARM_S = 30.0
# 预热（含刚换走的）最多几路，不算正在看的；只在自建 / 电脑客户端、内存够时开（中转的 --pc-warm）。
WARM_MAX = 2
# 直播片源跟不跟得上：最近 PACE_SPAN_S 秒里出的片段（媒体秒）不到 PACE_SLOW 倍就算慢（WatchStream.speed）。
# 慢的时候不预热别的台：同一个源站上的台（一个源站常常几十个台）一起拉只会更卡；页面也说一声片源慢。
# 量过（CCTV1，一个源站 63 个台）：一路只有 0.2–0.7 倍，几秒卡一次、断续时画面落在声音后面。
PACE_SPAN_S = 30.0
PACE_SLOW = 0.9
# 有备用线路的直播台放着放着卡住或变慢：主动换线路（这一路收尾、播放器重开一次，从别的线路起，WatchStream._guard），
# 不等 ffmpeg 自己超时重连（rw_timeout 15 秒再加几轮重连，比播放器的缓冲还长，画面早冻住了）。
# 卡住：STALL_MIN_S 秒、并且至少是这条线路平时两段之间最大间隔的 STALL_GAPS 倍（有的源站一次给一段 6 秒、10 秒的
# HLS，ffmpeg 一阵一阵地出段，不能当成卡住），至多 STALL_MAX_S 秒没有新的一段。
# 变慢：最近 PACE_SPAN_S 秒出片不到 SWITCH_SLOW 倍（PACE_SLOW 只让页面说「片源慢」）。
# 只在还有别的线路、别的线路最近没坏过时换（都坏过就不来回换，页面重开也有次数上限）。
STALL_MIN_S = 10.0
STALL_GAPS = 2.5
STALL_MAX_S = 30.0
SWITCH_SLOW = 0.6
GUARD_TICK_S = 1.0
# 记住多少个直播台的转法（只换封装还是转码），下次直接按它起，不用先试一次再重起。
MODE_CACHE_MAX = 256
# 直播台跳转以后真正的列表地址记多久（LiveMemory）：咪咕这类源先经两三次跳转、再取主列表，才到真正的列表，
# 每一步约 1 秒；记住了下次直接从它起，出第一段从六七秒变成三四秒。地址里带会话参数，量过 9 分钟以后还能用；
# 记的时间短一点，失效了（打不开、一段也没出）就忘掉、从频道表里的地址重来。只在内存里，从不写日志。
RESOLVED_S = 600.0
# 预先解析（WatchService.prepare）：直播页上看得见的台，先在后台把跳转和主列表走一遍（几个小请求，不起 ffmpeg），
# 真正的列表地址记进 LiveMemory；点下去 ffmpeg 直接从它起。量过（咪咕的 CCTV：跳两次、主列表、子列表，每步约 1 秒）：
# 出第一段从约 8 秒变成约 4.5 秒。同时至多 PREPARE_WORKERS 个，一次至多 PREPARE_MAX 个台；记住的地址还不到
# PREPARE_FRESH_S 秒的不再走（地址带会话参数，RESOLVED_S 内有效）。
PREPARE_WORKERS = 4
PREPARE_MAX = 40
PREPARE_FRESH_S = 300.0
PREPARE_TIMEOUT_S = 8.0
PREPARE_BYTES = 256 * 1024
# 直播台有几条线路（server/routes.py）时：一条线路起来以后这么久还没出第一段就算它不行（源站超时），换下一条。
FIRST_SEGMENT_S = 20.0
# 有备用线路时等 ffmpeg 报出输入流编码最多这么久（只有一条线路时照旧 PROBE_WAIT_S）。
ROUTE_PROBE_WAIT_S = 20.0
# 一条线路上至多起几次 ffmpeg（按编码重起、记住的地址失效重来、只换封装失败改转码）。
LEG_TRIES = 4
# 坏了的线路记多久（这么久内再开这个台时排在最后）。
BAD_ROUTE_S = 600.0
_OPENING = re.compile(r"Opening '(https?://[^']+)' for reading")
_SID = re.compile(r"^[0-9a-f]{16}$")
# 片库（Source.bounded）：ffmpeg 跑在播放器前面，至多攒这么多段、这么多字节没被取走（4K 20 Mbit/s 一段就是
# 十几 MB，按字节封顶），取走一段再接着读文件。比 WINDOW_* 小：没取走的段不会被挤出窗口。
VOD_AHEAD_SEGMENTS = 6
VOD_AHEAD_BYTES = 40 * 1024 * 1024
# 片库起播时（Source.progress）取列表最多等这么久就先回 503 和进度（「正在读取文件…已读 12 MB」），页面显示了再来取。
PROGRESS_WAIT_S = 2.0
# 片库（Source.exact）：输出的时间 = 文件里的时间 + 这么多秒（-copyts、-output_ts_offset）。不加的话分片 MP4 每条轨
# 各自从 0 起：只换封装的画面从 -ss 之前的关键帧起、转码的声音从 -ss 那一秒起，两边会差出几秒（量过：-ss 32.5、
# 关键帧在 30 秒，画面和声音都从 0 起，差 2.5 秒）。加了 frag_discont 以后 tfdt 是真的时间；再加这几秒，开头的
# B 帧（解码时间比 0 早一点）和 AAC 的预滚也不会是负数。
EXACT_TS_OFFSET = 10
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
    # 直播台的所有线路 ((地址, UA), …)，频道表的顺序（server/routes.py）；第一条和 url / user_agent 一样。
    # 只有一条（或没给）时和以前一样只用 url。
    routes: tuple = field(default=(), compare=False)
    # 片库（library.py）：只换封装的画面可以是哪些编码（浏览器说放得了 HEVC 时加上 "hevc"）。直播台、B 站只认 H.264：
    # 小电视那条路和别的客户端不一样，不跟着放宽。
    copy_video: tuple = ("h264",)
    # 明确的 -map（"0:序号"，片库：第一路真画面、挑好的那一路声音，绝不带字幕和附件）；空的照旧 0:V:0? 0:a:0?。
    maps: tuple = ()
    # 只换封装的 HEVC 写成 hvc1（MediaSource 认 hvc1，不认 hev1）。
    video_tag: str = ""
    # 片库：时间轴照原样（-copyts，-ss 带小数，EXACT_TS_OFFSET），列表的 ORIGIN 就是 offset + 文件的起始时间
    # （media_start）+ EXACT_TS_OFFSET：跳转以后进度和字幕都和文件里的时间对得上，画面和声音也对得上。
    exact: bool = False
    media_start: float = 0.0
    # 片库：不按实时读、跑在播放器前面，但至多攒 VOD_AHEAD_*（WatchStream._hold）。
    bounded: bool = False
    # 声音转码的码率（片库 192k）。
    audio_rate: str = "128k"
    # 先按哪种转法起（片库按探测的结果定好；直播台按 LiveMemory，别的先试只换封装）。
    mode: "Mode | None" = field(default=None, compare=False)
    # 网盘上的内嵌文字字幕：边转边抽成 WebVTT ((流的序号, 文件路径), …)，和画面同一个 ffmpeg、另一个输出。
    subs_out: tuple = field(default=(), compare=False)
    # 起播时给页面看的进度（「正在读取文件…已读 12 MB」）；没有时 None。
    progress: Callable[[], str] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Mode:
    """video："copy" 只换封装，"encode" 转码；audio："encode" 转成 AAC（直播源一律转码，见上），"copy"（片库里
    本来就是 AAC 的那一路）；None 是源里没有这一路。"""
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


def mode_for(video: str | None, audio: str | None, copyable: tuple = ("h264",)) -> Mode | None:
    """该怎么转：copyable 里的画面（一般只有 H.264）只换封装，别的画面转码；声音转成 AAC；什么都没有时 None。"""
    if video is None and audio is None:
        return None
    return Mode(None if video is None else "copy" if video in copyable else "encode",
                None if audio is None else "encode")


def is_live(source: Source) -> bool:
    """直播台（全局频道表里的）：键是 ("live", 台, 地址, UA)，有备用线路时后面再接上其余的线路。"""
    return bool(source.key) and source.key[0] == "live"


def live_key(cid: str, routes: list[tuple[str, str]]) -> tuple:
    """直播台的键：一条线路时 ("live", 台, 地址, UA)（和以前一样）；几条时后面接上其余线路，线路变了键就变。"""
    url, agent = routes[0]
    return ("live", cid, url, agent) + ((tuple(routes[1:]),) if len(routes) > 1 else ())


def legs_of(source: Source, order: list[int] | None = None) -> list[tuple[int, Source]]:
    """[(线路在频道表里的下标, 这条线路的片源)]，按 order 的顺序。一条线路的就是片源自己；几条的每条一个片源，
    键是 ("live", 台, 地址, UA)：记住的转法和跳转后的地址按线路记（LiveMemory）。"""
    if not is_live(source) or len(source.routes) < 2:
        return [(0, source)]
    order = list(range(len(source.routes))) if order is None else order
    return [(i, replace(source, key=("live", source.key[1], *source.routes[i]), url=source.routes[i][0],
                        user_agent=source.routes[i][1], routes=())) for i in order]


def fits(mode: Mode, video: str | None, audio: str | None, copyable: tuple = ("h264",)) -> bool:
    """这种转法能不能转这个片源：有没有画面、声音要对得上，只换封装的画面要在 copyable 里（一般只有 H.264；
    片库在浏览器放得了 HEVC 时还有 HEVC）。转码的画面什么都能转（H.264 也行，只是费 CPU）：只换封装失败后改成
    转码的、记住要转码的台，不再改回去。"""
    if (mode.video is None) != (video is None) or (mode.audio is None) != (audio is None):
        return False
    return mode.video != "copy" or video in copyable


def ffmpeg_args(ffmpeg: str, source: Source, url: str, mode: Mode, lenient: bool = False) -> list[str]:
    """ffmpeg 的参数（列表，不经 shell）。url 是取到的地址；opener 的片源从标准输入读。lenient：片段地址不标准的
    HLS 线路（routes.LENIENT_*），放宽扩展名检查（协议照样只许网络的）。"""
    piped = source.opener is not None
    args = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info"]
    if source.subs_out:
        args.append("-y")          # 字幕的输出是缓存里的文件：跳转重起时覆盖
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
    if source.exact and source.offset > 0:
        args += ["-ss", f"{source.offset:.3f}"]
    elif source.offset >= 1:
        args += ["-ss", str(int(source.offset))]
    if not piped and is_live(source):
        args += LIVE_PROBE
    if lenient and not piped:
        args += live_routes.LENIENT_OPTION
    args += ["-i", "pipe:0" if piped else url]
    if source.maps:
        for spec in source.maps:
            args += ["-map", spec]
    else:
        # 大写 V：不要封面图。没有这一路时不报错（?）。
        args += ["-map", "0:V:0?", "-map", "0:a:0?"]
    args += ["-sn", "-dn"]
    if source.exact:
        args += ["-copyts", "-avoid_negative_ts", "disabled", "-output_ts_offset", str(EXACT_TS_OFFSET)]
    if mode.video == "copy":
        args += ["-c:v", "copy"]
        if source.video_tag:
            args += ["-tag:v", source.video_tag]
    elif mode.video == "encode":
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "4M", "-bufsize", "8M",
                 "-pix_fmt", "yuv420p", "-vf", "scale=-2:'min(720,ih)'",
                 "-force_key_frames", "expr:gte(t,n_forced*2)"]
    if mode.audio == "encode":
        args += ["-c:a", "aac", "-b:a", source.audio_rate or "128k", "-ac", "2"]
    elif mode.audio == "copy":
        args += ["-c:a", "copy"]
    discont = "+frag_discont" if source.exact else ""       # tfdt 写真的时间（见 EXACT_TS_OFFSET）
    if mode.video is not None:
        # 每段从关键帧起（Safari 和 MediaSource 都要），至少 2 秒。
        args += ["-movflags", "frag_keyframe+empty_moov+default_base_moof" + discont,
                 "-min_frag_duration", str(FRAGMENT_US)]
    else:
        args += ["-movflags", "empty_moov+default_base_moof" + discont, "-frag_duration", str(FRAGMENT_US)]
    args += ["-flush_packets", "1", "-f", "mp4", "pipe:1"]
    for index, path in source.subs_out:
        # 另一个输出：只有这一路字幕，转成 WebVTT 写进缓存（library.py 边放边读，从不进画面那一路）。
        args += ["-map", f"0:{int(index)}", "-c:s", "webvtt", "-flush_packets", "1", "-f", "webvtt", str(path)]
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
                elif name in ("hvc1", "hev1"):
                    codec = hevc_codec(name, _child(body, b"hvcC", start=78))
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


def hevc_codec(name: str, hvcc: bytes | None) -> str:
    """hvcC → CODECS 里的那一项（ISO/IEC 14496-15 附录 E）：hvc1.<profile_space><profile_idc>.<兼容标志，按位倒过来的
    十六进制>.<L/H tier><level_idc>[.<约束标志，每字节一项，去掉后面的 0>]。例：Main 10、5.1 级是 hvc1.2.4.L153.B0。
    hvcC 太短（坏了）时给 Main、3.1 级的保守值。"""
    if not hvcc or len(hvcc) < 13:
        return f"{name}.1.6.L93.B0"
    space, tier, profile = hvcc[1] >> 6, (hvcc[1] >> 5) & 1, hvcc[1] & 0x1F
    compat = int(f"{int.from_bytes(hvcc[2:6], 'big'):032b}"[::-1], 2)
    constraints = list(hvcc[6:12])
    while constraints and constraints[-1] == 0:
        constraints.pop()
    text = f"{name}.{'' if space == 0 else 'ABC'[space - 1]}{profile}.{compat:X}.{'H' if tier else 'L'}{hvcc[12]}"
    return text + "".join(f".{byte:X}" for byte in constraints)


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
        # 直播台放了一阵断了、还有别的线路：列表收尾时加上 #EXT-X-RTV-REOPEN（自定义的标签），播放器放到这里
        # 就自己重开一次（从下一条线路起，console.js 的 routeReopen）。
        self.reopen = False
        # 一共出了多少秒（停了以后也留着）：估计电脑上看到了哪里（web.py 的 _pc_position）。
        self.total_s = 0.0
        self.origin: float | None = None
        # 片库（Source.exact）：ORIGIN 不看第一段的 tfdt，直接用这个（offset + 文件的起始时间）。
        self.fixed_origin: float | None = None
        # 播放器取过的最大的段号（片库按它攒段，WatchStream._hold）。
        self.fetched = -1

    def ahead(self) -> tuple[int, int]:
        """还没被播放器取走的：(几段, 多少字节)。"""
        waiting = [len(data) for seq, _, data in self.segments if seq > self.fetched]
        return len(waiting), sum(waiting)

    def add(self, duration: float, data: bytes, start: float | None = None) -> None:
        if self.origin is None and not self.segments and self.next_seq == 0:
            self.origin = self.fixed_origin if self.fixed_origin is not None else start if start is not None else 0.0
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
            if self.reopen:
                lines.append("#EXT-X-RTV-REOPEN")
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def master(self) -> str:
        seconds = sum(d for _, d, _ in self.segments)
        size = sum(len(data) for _, _, data in self.segments)
        bandwidth = int(size * 8 / seconds) if seconds > 0 else 3_000_000
        return ("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-INDEPENDENT-SEGMENTS\n"
                f'#EXT-X-STREAM-INF:BANDWIDTH={max(bandwidth, 64_000)},CODECS="{self.codecs}"\nindex.m3u8\n')


# ---------------------------------------------------------------- 记住的直播台

class LiveMemory:
    """每个直播台（Source.key）记住的两样，让下次起得快：
    - 转法（Mode）：要转码的台直接按转码起，不用先按只换封装起、看到编码再重起（又一次连片源）。
    - 跳转以后真正的列表地址（RESOLVED_S 秒内有效）：ffmpeg 不用再走一遍跳转和主列表。"""

    def __init__(self, clock=time.monotonic, resolved_s: float = RESOLVED_S):
        self.clock = clock
        self.resolved_s = resolved_s
        self._modes: collections.OrderedDict[tuple, Mode] = collections.OrderedDict()
        self._urls: dict[tuple, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def mode(self, key: tuple) -> Mode | None:
        with self._lock:
            return self._modes.get(key)

    def remember_mode(self, key: tuple, mode: Mode) -> None:
        with self._lock:
            self._modes[key] = mode
            self._modes.move_to_end(key)
            while len(self._modes) > MODE_CACHE_MAX:
                self._modes.popitem(last=False)

    def url(self, key: tuple) -> str | None:
        with self._lock:
            found = self._urls.get(key)
            if found is None:
                return None
            if self.clock() - found[1] > self.resolved_s:
                del self._urls[key]
                return None
            return found[0]

    def remember_url(self, key: tuple, url: str) -> None:
        with self._lock:
            self._urls[key] = (url, self.clock())
            while len(self._urls) > MODE_CACHE_MAX:
                del self._urls[min(self._urls, key=lambda k: self._urls[k][1])]

    def forget_url(self, key: tuple) -> None:
        with self._lock:
            self._urls.pop(key, None)

    def age(self, key: tuple) -> float | None:
        """记住的列表地址记了多少秒；没有（或已过期）时 None。"""
        with self._lock:
            found = self._urls.get(key)
        if found is None:
            return None
        age = self.clock() - found[1]
        return age if age <= self.resolved_s else None


class RouteMemory:
    """每个直播台上次放得起来的线路（先试它）、每条线路真放起来用了多久（快很多的排到前面，routes.preferred）和
    最近坏了的线路（BAD_ROUTE_S 秒内排在最后）。好的线路和用时也记进频道表旁边的 meta（routes.remember_good、
    remember_start，变了才写），下次启动接着用；path() 给频道表的路径。有 meta 时上次好的那条以 meta 为准（总后台
    把备用线路设为主线路时会清掉它），没有频道表时只在内存里。"""

    def __init__(self, path: Callable[[], Path | None] = lambda: None, clock=time.monotonic,
                 bad_s: float = BAD_ROUTE_S, log=print):
        self.path = path
        self.clock = clock
        self.bad_s = bad_s
        self.log = log
        self._good: dict[str, str] = {}
        self._bad: dict[tuple[str, str], float] = {}
        # (台, 指纹) → 起播用时（毫秒，和以前的 blend 过）：只在内存里也算，写 meta 的变了才写。
        self._starts: dict[tuple[str, str], int] = {}
        # (台, 指纹)：这次运行里发现要放宽检查的线路（也写进 meta；没有频道表时只在这里）。
        self._lenient: set[tuple[str, str]] = set()
        # 读过的 meta（文件的修改时间, 内容）：直播页一次预解析几十个台，不必每个台都重读、重解析一遍。
        self._meta: tuple[tuple, dict] | None = None
        self._lock = threading.Lock()
        # 后台在自动检测线路（server/relay/linecheck.py 开着）：都打不开时只说「已经在自动检测」。
        self.checking = False
        self.wall = time.time

    def _entry(self, cid: str) -> dict:
        """meta 里这个台的那一项（没有频道表、没有 meta 时 {}）；文件没变就用读过的。"""
        path = self.path()
        if path is None:
            return {}
        try:
            info = live_routes.meta_path(path).stat()
            stamp = (str(path), info.st_mtime_ns, info.st_size)
        except OSError:
            return {}
        cached = self._meta
        if cached is None or cached[0] != stamp:
            cached = (stamp, live_routes.read_meta(path))
            self._meta = cached
        entry = cached[1].get(cid, {})
        return entry if isinstance(entry, dict) else {}

    def _known(self, cid: str) -> tuple[str, set[str], dict[str, int], set[str]]:
        """(上次好的指纹, 最近坏了的和画面可疑的指纹, 每条的用时, 实测跟不上实时的指纹)。"""
        path = self.path()
        entry = self._entry(cid)
        speeds = live_routes.speeds_of(entry, "pc")
        now = self.clock()
        with self._lock:
            good = live_routes.good_in(entry) if path is not None else self._good.get(cid, "")
            bad = {mark for (c, mark), at in self._bad.items() if c == cid and now - at < self.bad_s}
            # 上次检测（总后台点的、后台自检的）连不上的也排最后；刚在这里放起来的不算。
            bad |= live_routes.bad_in(entry) - {self._good.get(cid, "")}
            speeds.update({mark: ms for (c, mark), ms in self._starts.items() if c == cid})
        bad |= live_routes.suspect_in(entry)
        return good, bad, speeds, live_routes.weak_in(entry, self.wall())

    def order(self, cid: str, routes: list[tuple[str, str]], first: int | None = None) -> list[int]:
        """试线路的顺序；first（页面上选的第几条，从 0 数）给了就先试它，别的照常。"""
        good, bad, speeds, weak = self._known(cid)
        order = live_routes.preferred(routes, good, bad, speeds, weak)
        if first is not None and 0 <= first < len(routes):
            order = [first] + [i for i in order if i != first]
        return order

    def lines(self, cid: str, routes: list[tuple[str, str]]) -> list[dict]:
        """给页面的线路菜单：频道表的顺序，每条 {ms?（起播或检测的用时）, bad?（最近坏过、或上次检测连不上）,
        suspect?（画面可疑：隔了很久一模一样、或和同台其他线路对不上）, pace?（量过跟不上实时：几倍）}。不带地址。"""
        good, bad, speeds, weak = self._known(cid)
        entry = self._entry(cid)
        suspect = live_routes.suspect_in(entry)
        quality = live_routes.quality_in(entry)
        checks = entry.get("checks")
        checks = checks if isinstance(checks, dict) else {}
        found = []
        for url, _ in routes:
            mark = live_routes.fingerprint(url)
            check = checks.get(mark) if isinstance(checks.get(mark), dict) else {}
            item: dict = {}
            if mark in suspect:
                item["suspect"] = True
            elif mark in bad or (check.get("ok") is False and mark not in speeds):
                item["bad"] = True
            elif speeds.get(mark):
                item["ms"] = speeds[mark]
            if mark in weak:
                item["pace"] = quality[mark]["pace"]
            found.append(item)
        return found

    def ok(self, cid: str, url: str, count: int, ms: float | None = None) -> None:
        """这条线路出了第一段（ms：从起这条线路到这时用了多久）。"""
        mark = live_routes.fingerprint(url)
        timed = ms is not None and ms > 0 and count > 1
        # 内存里还没有时（刚启动）接着 meta 里存的（只算真放起来的，不算检测的用时）。
        stored = live_routes.starts_in(self._entry(cid), "pc").get(mark) if timed else None
        with self._lock:
            self._good[cid] = mark
            self._bad.pop((cid, mark), None)
            value = None
            if timed:
                before = self._starts.get((cid, mark), stored)
                value = live_routes.blend(before, int(ms))
                self._starts[(cid, mark)] = value
                while len(self._starts) > MODE_CACHE_MAX:
                    self._starts.pop(next(iter(self._starts)))
        try:
            # 检测说它连不上、其实放起来了：改过来（不然直播页上这个台还标着「暂时打不开」）。
            live_routes.mark_ok(self.path(), cid, url, self.wall())
            if count > 1:
                live_routes.remember_good(self.path(), cid, url)
                if value is not None:
                    live_routes.remember_start(self.path(), cid, url, value, "pc")
        except OSError as error:
            self.log(f"在电脑上看：记不下好用的线路（{type(error).__name__}）")

    def dead(self, cid: str, url: str, reason: str) -> None:
        """这条线路明确坏了（403、404……，routes.definitive）：马上记进 meta 的检测结果，下次起播（电脑和电视）
        排在最后，后台的自检过一阵再测它。"""
        self.failed(cid, url)
        try:
            live_routes.mark_failed(self.path(), cid, url, live_routes.friendly(reason), self.wall())
        except OSError as error:
            self.log(f"在电脑上看：记不下坏了的线路（{type(error).__name__}）")

    def paced(self, cid: str, url: str, pace: float) -> None:
        """电脑上真放了一阵量到的速度（几倍实时）记进 meta（routes.remember_pace），排线路顺序用。"""
        try:
            live_routes.remember_pace(self.path(), cid, url, pace, self.wall())
        except OSError as error:
            self.log(f"在电脑上看：记不下线路的速度（{type(error).__name__}）")

    def lenient(self, cid: str, url: str) -> bool:
        """这条线路要放宽 ffmpeg 的扩展名检查（meta 的 lenient，或者这次运行里刚发现的）。"""
        mark = live_routes.fingerprint(url)
        with self._lock:
            if (cid, mark) in self._lenient:
                return True
        return mark in live_routes.lenient_in(self._entry(cid))

    def remember_lenient(self, cid: str, url: str) -> None:
        with self._lock:
            self._lenient.add((cid, live_routes.fingerprint(url)))
        try:
            live_routes.remember_lenient(self.path(), cid, url)
        except OSError as error:
            self.log(f"在电脑上看：记不下要放宽检查的线路（{type(error).__name__}）")

    def failed(self, cid: str, url: str) -> None:
        mark = live_routes.fingerprint(url)
        with self._lock:
            self._bad[(cid, mark)] = self.clock()
            if self._good.get(cid) == mark:
                del self._good[cid]
            while len(self._bad) > MODE_CACHE_MAX:
                del self._bad[min(self._bad, key=self._bad.get)]


def resolved_playlist(line: str, given: str) -> str | None:
    """ffmpeg 的一行输出里打开的列表地址（跳转和主列表之后真正的那个 .m3u8）；不是、或者就是频道表里的地址时 None。"""
    match = _OPENING.search(line)
    if match is None:
        return None
    url = match.group(1)
    path = url.split("?", 1)[0].split("#", 1)[0]
    return url if path.endswith(".m3u8") and url != given else None


def resolve_playlist(url: str, user_agent: str = "", timeout: float = PREPARE_TIMEOUT_S,
                     urlopen=urllib.request.urlopen) -> str | None:
    """像 ffmpeg 那样打开一个直播地址：跟着跳转，主列表里只有一路（没有单独的声音列表）时进到那一路。返回 ffmpeg
    会记下的那个列表地址（和 resolved_playlist 一样：.m3u8 结尾、和给的不同）；多路清晰度、不是 HLS、打不开时 None。
    地址里带会话参数：不写日志。"""
    if not url.startswith(("http://", "https://")):
        return None
    # 频道表里偶尔有中文路径：ffmpeg 原样发，urllib 要先转义。
    asked = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    # 没写 User-Agent 的台：和 ffmpeg 一样报 Lavf（ffmpeg_args 只在台写了时才加 -user_agent）。
    request = urllib.request.Request(asked, headers={"User-Agent": user_agent or "Lavf/61"})
    try:
        with urlopen(request, timeout=timeout) as response:
            final = response.geturl()
            text = response.read(PREPARE_BYTES).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 —— 解析不了就算了，点下去时 ffmpeg 照常从频道表里的地址起
        return None
    if "#EXTM3U" not in text:
        return None
    if "#EXT-X-STREAM-INF" in text:
        if "#EXT-X-MEDIA:" in text and "URI=" in text:
            return None        # 单独的声音（字幕）列表：ffmpeg 会打开几个列表，LiveMemory 也不记
        lines = text.splitlines()
        variants = [lines[i + 1].strip() for i, line in enumerate(lines[:-1])
                    if line.startswith("#EXT-X-STREAM-INF") and lines[i + 1].strip() and not lines[i + 1].startswith("#")]
        if len(variants) != 1:
            return None
        found = urllib.parse.urljoin(final, variants[0])
    elif "#EXTINF" in text:
        found = final
    else:
        return None
    path = found.split("?", 1)[0].split("#", 1)[0]
    return found if path.endswith(".m3u8") and found not in (url, asked) else None


# ---------------------------------------------------------------- 一路

class WatchStream:
    """一路：后台线程起 ffmpeg（必要时按编码重起一次）、把输出切成段放进 Window。

    device 是这一路占的位置（slot）：跟随小电视时是设备 id，直接看一个台时是「pc<用户>-<页面>」；
    令牌绑的是它和 sid。user 是开这一路的用户（每个用户的上限按它数）。"""

    def __init__(self, sid: str, device: str, source: Source, ffmpeg: str, log=print,
                 clock=time.monotonic, popen=subprocess.Popen, user: int | None = None,
                 mode: Mode = FIRST_MODE, memory: LiveMemory | None = None, cheap: bool = False,
                 route_memory: RouteMemory | None = None, first_route: int | None = None):
        self.sid = sid
        self.device = device
        self.user = user
        self.source = source
        self.ffmpeg = ffmpeg
        self.log = log
        self.clock = clock
        self.popen = popen
        self.window = Window()
        if source.exact:
            self.window.fixed_origin = max(0.0, source.offset) + source.media_start + EXACT_TS_OFFSET
        self.created = self.seen = clock()
        self.error = ""
        self.stopped = False
        self.mode: Mode | None = None
        # 先按哪种转法起：记住了这个台的转法（WatchService 的缓存）就按它，否则先试只换封装。
        self.first_mode = mode
        # 直播台记住的转法和地址（WatchService 的 LiveMemory）；只给直播台。
        self.memory = memory if is_live(source) else None
        # 这一次是不是从记住的地址起的（失效了就忘掉、从频道表里的地址重来一次）。
        self._via_memory = False
        self._given = ""
        # 预热的（没人在看）：要转码（一路吃一个核）就不转了，只记下转法。
        self.cheap = cheap
        # 预热、刚换走的台：转到这个时刻（WatchService 的单调时钟）为止，没人接上就停。
        self.warm_until = 0.0
        self._proc = None
        self._upstream = None
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self.stderr: collections.deque[str] = collections.deque(maxlen=12)
        # 直播台：ffmpeg 说的能认出原因的几行（routes.classify 认得的：403、超时、中途断开……）。片段坏了时 ffmpeg
        # 会刷一大堆解码错误，原因那一行早就挤出了上面那 12 行。
        self.telling: collections.deque[str] = collections.deque(maxlen=4)
        # 有备用线路的直播台：现在在试的线路（片源和它在频道表里的下标）、换线路时给播放器看的话（notice），
        # 放了一阵又断了、还有别的线路可换时 reopen 为真（播放器自己重开一次，见 get() 的 410）。
        self.route_memory = route_memory if is_live(source) else None
        self.leg = source
        self.route = 0
        # 页面上选的线路（第几条，从 0 数）：先试它，起不来照常换别的。
        self.first_route = first_route
        self._legs: list[tuple[int, Source]] = []
        # (时刻, 一共出了多少媒体秒)：每出一段记一个，留最近两个 PACE_SPAN_S（speed）。
        self._pace: collections.deque[tuple[float, float]] = collections.deque()
        self._pace_lock = threading.Lock()
        self.notice = ""
        self.reopen = False
        self._leg_segments = 0
        self._leg_since: float | None = None
        # 这条线路从什么时候开始试（出第一段时算起播用了多久，RouteMemory.ok）。
        self._leg_began: float | None = None
        self._starved = False
        # ffmpeg 因为片段地址不标准拒绝打开（routes.LENIENT_ERROR）：放宽检查再试这条线路一次。
        self._picky = False
        # 没有 RouteMemory 时（测试、云端以外的少见情形）这一路自己记着放宽了哪条线路。
        self._lenient_here: set[tuple[str, str]] = set()
        # 这条线路上一段到的时刻、平时两段之间最大的间隔（卡住的时限按它放宽）、主动换线路的原因（_guard）。
        self._last_segment_at: float | None = None
        self._usual_gap = 0.0
        self._leg_count = 0
        self._switch = ""

    # ---- 状态 ----

    @property
    def alive(self) -> bool:
        """还在转（或者还在起）：没停、没出错、没播完。"""
        return not self.stopped and not self.error and not self.window.ended

    def _paced(self) -> None:
        now = self.clock()
        with self._pace_lock:
            self._pace.append((now, self.window.total_s))
            while len(self._pace) > 2 and self._pace[1][0] < now - 2 * PACE_SPAN_S:
                self._pace.popleft()

    def speed(self) -> float | None:
        """最近 PACE_SPAN_S 秒里片源出片的速度（1 是刚好实时）；出片还不到这么久时 None。ffmpeg 不按实时读
        （直播源不加 -re），起播时会一下子读进列表里已有的几段，过了这一阵才看得出片源本身的速度；
        一直没来新的一段时，等着的这段也算进去。"""
        now = self.clock()
        since = now - PACE_SPAN_S
        with self._pace_lock:
            before = [total for at, total in self._pace if at <= since]
            last = self._pace[-1][1] if self._pace else 0.0
        if not before:
            return None
        return (last - before[-1]) / PACE_SPAN_S

    def slow(self) -> bool:
        speed = self.speed()
        return speed is not None and speed < PACE_SLOW

    # ---- 卡住、变慢时换线路 ----

    def _new_leg(self) -> None:
        """换到一条新线路：出片的速度、段的间隔从这条线路重新算。"""
        with self._pace_lock:
            self._pace.clear()
        self._last_segment_at, self._usual_gap, self._leg_count, self._switch = None, 0.0, 0, ""

    def _arrived(self) -> None:
        now = self.clock()
        last, self._last_segment_at = self._last_segment_at, now
        self._leg_count += 1
        # 头几段是 ffmpeg 一下子读进列表里已有的那几段，间隔不算。
        if last is not None and self._leg_count > 3:
            self._usual_gap = max(self._usual_gap, now - last)

    def stall_limit(self) -> float:
        return min(STALL_MAX_S, max(STALL_MIN_S, STALL_GAPS * self._usual_gap))

    def _alternative(self) -> bool:
        """还有别的线路可换（别的线路最近没坏过）。"""
        if not is_live(self.source) or len(self.source.routes) < 2:
            return False
        line = self._leg_line()
        if self.route_memory is None or line is None:
            return True
        _good, bad, _speeds, _weak = self.route_memory._known(line[0])
        here = live_routes.fingerprint(line[1])
        return any(live_routes.fingerprint(url) not in bad | {here} for url, _ua in self.source.routes)

    def _record_pace(self) -> None:
        """这条线路放了 PACE_SPAN_S 秒以上：把量到的速度记下（预热的也算，它也是真拉片源）。"""
        line = self._leg_line()
        speed = self.speed()
        if line is not None and speed is not None and self.route_memory is not None and len(self.source.routes) > 1:
            self.route_memory.paced(line[0], line[1], speed)

    def _guard_check(self, proc) -> bool:
        """（_guard 每秒一次）这条线路卡住了、或者太慢：记下原因，停掉 ffmpeg（_follow 当成这条线路不行，_run 让
        播放器重开、从别的线路起）。预热的（没人在看）不管。返回换不换。"""
        if self.stopped or self.cheap or self._switch or self._last_segment_at is None:
            return False
        now = self.clock()
        quiet = now - self._last_segment_at
        if quiet > self.stall_limit():
            reason = f"{int(quiet)} 秒没有新画面"
        else:
            speed = self.speed()
            if speed is None or speed >= SWITCH_SLOW:
                return False
            reason = f"片源太慢（约 {speed:.1f} 倍速）"
        if not self._alternative():
            return False
        self._switch = reason
        self.log(f"在电脑上看 {self.device}：{self.source.label or self.source.key[1]} 线路 {self.route + 1} {reason}，换线路")
        try:
            proc.kill()
        except OSError:
            pass
        return True

    def _guard(self, proc, done: threading.Event) -> None:
        while not done.wait(GUARD_TICK_S):
            if self._guard_check(proc):
                return

    def touch(self) -> None:
        self.seen = self.clock()

    def ready(self) -> bool:
        return self.window.init is not None and bool(self.window.segments)

    def wait_ready(self, timeout: float) -> bool:
        """等到有初始化段和至少一段、或者出错 / 结束 / 停了；返回是否有东西可放。"""
        deadline = time.monotonic() + timeout
        notice = self.notice
        with self._cond:
            # 换线路了（notice 变了）也先回去：页面马上能说「正在换线路 2…」。
            while not self.ready() and self.alive and self.notice == notice:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(min(left, 1.0))
            return self.ready()

    def _notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def fetched(self, seq: int) -> None:
        """播放器取了第 seq 段（watch.get）：片库攒着的那一路可以接着读了。"""
        with self._cond:
            if seq > self.window.fetched:
                self.window.fetched = seq
                self._cond.notify_all()

    def _hold(self) -> None:
        """片库（Source.bounded）：没被取走的攒够了 VOD_AHEAD_SEGMENTS 段或 VOD_AHEAD_BYTES 就先不读 ffmpeg 的输出——
        管道满了 ffmpeg 就停下，也就不再读文件（网盘不白白多拉）；播放器取走一段再接着读。暂停着时播放器照样
        取列表（不会被当成没人看停掉），只是不取段。"""
        with self._cond:
            while not self.stopped:
                count, size = self.window.ahead()
                if count < VOD_AHEAD_SEGMENTS and size < VOD_AHEAD_BYTES:
                    return
                self._cond.wait(1.0)

    # ---- 起停 ----

    def start(self) -> None:
        self._legs = self._plan()
        self.route = self._legs[0][0]
        self._thread = threading.Thread(target=self._run, name=f"watch-{self.sid}", daemon=True)
        self._thread.start()

    def _plan(self) -> list[tuple[int, Source]]:
        """按什么顺序试线路（RouteMemory.order；页面选了线路时先试它）。在 start() 里先排好：回答页面时就知道先放哪条。"""
        order = None
        if self.route_memory is not None and len(self.source.routes) > 1:
            order = self.route_memory.order(self.source.key[1], list(self.source.routes), self.first_route)
        return legs_of(self.source, order)

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
        try:
            source = self.source
            legs = self._legs or self._plan()
            last = ""
            for n, (index, leg) in enumerate(legs):
                self.leg, self.route = leg, index
                self._leg_segments, self._leg_since, self._leg_began = 0, None, self.clock()
                self._new_leg()
                outcome, detail = self._run_leg()
                self._record_pace()
                if self.stopped or outcome == "stopped":
                    return
                if outcome == "ended":
                    self.window.ended = True
                    return
                last = detail or "片源打不开"
                if len(legs) > 1:
                    self.log(f"在电脑上看 {self.device}：{source.label or source.key[1]} 线路 {index + 1} 不行："
                             f"{live_routes.redact(last)}")
                    early = self._leg_since is None or self.clock() - self._leg_since < live_routes.EARLY_S
                    # 卡住、太慢换的（_guard）：放了多久都记成刚坏过，播放器重开时排到最后。
                    if (early or self._switch) and self.route_memory is not None:
                        self.route_memory.failed(source.key[1], leg.url)
                if self.route_memory is not None and isinstance(leg.url, str) and live_routes.definitive(last):
                    # 源站明说 403 / 404：一条线路的台也记下，直播页标「暂时打不开」，后台自检过一阵再测。
                    self.route_memory.dead(source.key[1], leg.url, last)
                if self._leg_segments:
                    # 放了一阵断了：这一路的段已经给了播放器（换线路得换初始化段），不在这一路里接着换；
                    # 还有别的线路时让播放器重开一次（RouteMemory 把这条排到了最后）。
                    self.reopen = self.window.reopen = len(legs) > 1
                    break
                if n + 1 < len(legs):
                    self.notice = live_routes.switching(source.label or source.key[1], index + 1, last,
                                                        legs[n + 1][0] + 1)
                    self._notify()
            if self.stopped:
                return
            if is_live(source):
                if self._leg_segments and self._switch and self.reopen:
                    self.error = f"{self._switch}，正在换线路…"
                elif self._leg_segments:
                    self.error = (f"{live_routes.friendly(last)}，正在换线路…" if self.reopen
                                  else f"直播断了（{live_routes.friendly(last)}）")
                else:
                    self.error = live_routes.all_failed(last, len(legs), self.route_memory is not None
                                                        and self.route_memory.checking)
            else:
                self.error = explain(source.site, last)
            # 已经有段的：列表收尾，播放器放完已有的就停。
            self.window.ended = bool(self.window.segments)
            self.log(f"在电脑上看 {self.device} 停了：{self.error}"
                     + (f"（{live_routes.redact(last)}）" if is_live(source) else ""))
        except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死：记下来，页面上看到出错
            self.error = f"{type(error).__name__}: {error}"
            self.log(f"在电脑上看 {self.device} 出错：{self.error}")
        finally:
            self._kill()
            self._notify()

    def _run_leg(self) -> tuple[str, str]:
        """在一条线路（self.leg）上起 ffmpeg，必要时按编码重起、改转码、忘掉失效的地址重来（至多 LEG_TRIES 次）。
        返回 ("ended", "")、("stopped", "") 或 ("failed", 原因的原话)。"""
        leg = self.leg
        if leg is self.source:
            mode = self.first_mode
        else:
            mode = (self.memory.mode(leg.key) if self.memory is not None else None) or FIRST_MODE
        retried, detail = False, ""
        for _ in range(LEG_TRIES):
            if self.stopped:
                return "stopped", ""
            outcome, detail = self._attempt(mode)
            if outcome == "restart":
                self.log(f"在电脑上看 {self.device}：片源是 {detail[1]}，"
                         f"{'画面转码' if detail[0].video == 'encode' else '没有画面' if detail[0].video is None else '画面只换封装'}")
                mode = detail[0]
                if self.cheap and mode.video == "encode":
                    # 预热的台要转码：记下转法（真要看时直接按它起），这一路不转了。
                    self._decided(mode)
                    self.stopped = True
                    self.log(f"在电脑上看 {self.device}：要转码，不预热")
                    return "stopped", ""
                detail = ""
                continue
            if outcome in ("failed", "copy_failed") and self._picky and not self._leg_segments \
                    and is_live(self.leg) and not self._lenient_leg() and not self.stopped:
                # 片段地址不标准（/api/…、.php）、ffmpeg 不肯打开：记下这条线路，放宽检查再试（电视服务、取封面也照着记的放宽）。
                self._remember_lenient()
                self.log(f"在电脑上看 {self.device}：片段地址不标准，放宽检查再试")
                continue
            if outcome in ("failed", "copy_failed") and self._via_memory and not self._leg_segments \
                    and not self.stopped:
                # 记住的地址失效了（会话过期之类）：忘掉，从频道表里的地址重来。
                self.memory.forget_url(leg.key)
                self.log(f"在电脑上看 {self.device}：记住的地址打不开了，重新连")
                continue
            if outcome == "copy_failed" and not retried and not self._starved and \
                    (not is_live(self.source) or live_routes.classify(str(detail)) in ("other", "invalid")):
                # （直播台的原因是网络上的——403、超时、中途断开——时不转码再试：换封装没错，是线路坏了。）
                # 复制画面失败、一段也没出（少见的 H.264 变体）：转码再试一次。
                retried = True
                mode = Mode("encode", mode.audio)
                self.log(f"在电脑上看 {self.device}：只换封装不行，改成转码")
                continue
            if outcome == "ended":
                return "ended", ""
            return ("stopped", "") if self.stopped else ("failed", str(detail or ""))
        return "failed", str(detail or "片源打不开")

    def _leg_line(self) -> tuple[str, str] | None:
        """(台, 频道表里这条线路的地址)：直播台才有。"""
        leg = self.leg
        if not is_live(leg) or len(leg.key) < 2 or not isinstance(leg.url, str):
            return None
        return leg.key[1], leg.url

    def _lenient_leg(self) -> bool:
        line = self._leg_line()
        if line is None:
            return False
        if self.route_memory is not None:
            return self.route_memory.lenient(*line)
        return line in self._lenient_here

    def _remember_lenient(self) -> None:
        line = self._leg_line()
        if line is None:
            return
        if self.route_memory is not None:
            self.route_memory.remember_lenient(*line)
        else:
            self._lenient_here.add(line)

    def _attempt(self, mode: Mode) -> tuple[str, object]:
        """起一次 ffmpeg。返回 ("restart", (新的 Mode, 编码说明))、("ended", None)、("copy_failed", 原因)、("failed", 原因)。"""
        source = self.leg
        url = ""
        self.stderr.clear()
        self.telling.clear()
        self._starved = False
        if source.opener is None:
            try:
                url = source.url() if callable(source.url) else source.url
            except Exception as error:  # noqa: BLE001 —— 取地址失败（B 站接口不通、视频下架……）
                if getattr(error, "permanent", False):
                    return "failed", f"放不了这个视频：{error}"
                return "failed", f"取不到片源地址：{error}"
            if not str(url).startswith(("http://", "https://")):
                return "failed", "片源地址不是 http(s)"
        remembered = self.memory.url(source.key) if self.memory is not None and source.opener is None else None
        self._via_memory = remembered is not None
        # 从频道表里的地址起的直播台：ffmpeg 打开的真正的列表地址记下来（_read_stderr）。
        self._given = url if self.memory is not None and not self._via_memory else ""
        self._picky = False
        args = ffmpeg_args(self.ffmpeg, source, remembered or url, mode, lenient=self._lenient_leg())
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
        source = self.leg
        if self.stopped:
            self._kill()
            return "failed", ""
        lines: list[str] = []
        probed = threading.Event()
        threading.Thread(target=self._read_stderr, args=(proc, lines, probed, self._given), daemon=True).start()
        if source.opener is not None:
            threading.Thread(target=self._feed, args=(proc,), daemon=True).start()
        probed.wait(ROUTE_PROBE_WAIT_S if len(self.source.routes) > 1 else PROBE_WAIT_S)
        done, video, audio = input_codecs(lines)
        copyable = source.copy_video
        wanted = mode_for(video, audio, copyable)
        # 输入信息打出来了、但这种转法 ffmpeg 起不来（例如只有声音的源用了按关键帧切段）：照编码换一种转法。
        if not done and (wanted is None or fits(mode, video, audio, copyable)):
            self._kill()
            proc.wait()
            return "failed", self._reason(lines) or "片源连不上"
        if wanted is None:
            self._kill()
            proc.wait()
            return "failed", "片源里没有画面也没有声音"
        if not fits(mode, video, audio, copyable):
            self._kill()
            proc.wait()
            return "restart", (wanted, f"{video or '无画面'} + {audio or '无声音'}")
        self.mode = mode
        self._decided(mode)
        watchdog = None
        if is_live(self.source):
            # 直播：编码报出来了、却迟迟不出第一段（列表在、片段取不到）：FIRST_SEGMENT_S 秒后算源站超时。
            watchdog = threading.Timer(FIRST_SEGMENT_S, self._no_segments, args=(proc, self._leg_segments))
            watchdog.daemon = True
            watchdog.start()
        guarded = threading.Event()
        if is_live(self.source) and len(self.source.routes) > 1:
            threading.Thread(target=self._guard, args=(proc, guarded), daemon=True).start()
        try:
            produced = self._segment(proc.stdout)
        finally:
            guarded.set()
            if watchdog is not None:
                watchdog.cancel()
        code = proc.wait()
        if self.stopped:
            return "failed", ""
        if self._switch:
            return "failed", self._switch
        if code == 0 and not self._starved:
            return "ended", None
        reason = ("没有出片段（timed out）" if self._starved else "") or self._reason(lines) or f"ffmpeg 退出码 {code}"
        if not produced and mode.video == "copy":
            return "copy_failed", reason
        return "failed", reason

    def _decided(self, mode: Mode) -> None:
        if self.memory is not None:
            self.memory.remember_mode(self.leg.key, mode)

    def _no_segments(self, proc, before: int) -> None:
        if self.stopped or self._leg_segments != before:
            return
        self._starved = True
        try:
            proc.kill()
        except OSError:
            pass

    def _first_segment(self) -> None:
        """这条线路出了第一段：记住它好用（RouteMemory），换线路的话收起来。"""
        self._leg_since = self.clock()
        if self.notice:
            self.notice = ""
        if self.route_memory is not None and self.source.routes:
            # 起播用时只算真有人在看的（预热的同时开着别的台，慢一点不说明线路慢）。
            began = None if self.cheap else self._leg_began
            ms = (self._leg_since - began) * 1000 if began is not None else None
            self.route_memory.ok(self.source.key[1], self.leg.url, len(self.source.routes), ms)

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
                self._arrived()
                self._paced()
                self._leg_segments += 1
                if self._leg_segments == 1:
                    self._first_segment()
                self._notify()
                if self.leg.bounded:
                    self._hold()
        return produced

    def _read_stderr(self, proc, lines: list[str], probed: threading.Event, given: str = "") -> None:
        """given：这次给 ffmpeg 的频道表里的地址（直播台、不是从记住的地址起时）。读完输入信息之前 ffmpeg 只打开了
        一个列表（跳转、主列表以后真正的那个）就记下它；打开了几个（多路清晰度、单独的声音列表）不记：只给其中一个
        会少了别的。"""
        opened: list[str] = []
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if not probed.is_set():
                    found = resolved_playlist(line, given) if given else None
                    if found is not None and found not in opened:
                        opened.append(found)
                    lines.append(line)
                    if input_codecs(lines)[0]:
                        probed.set()
                        if len(opened) == 1:
                            self.memory.remember_url(self.leg.key, opened[0])
                self.stderr.append(line)
                if live_routes.LENIENT_ERROR in line:
                    self._picky = True
                if self.route_memory is not None or is_live(self.source):
                    if routes_known(line):
                        self.telling.append(line)
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
        if is_live(self.source):
            found = live_routes.telling(list(self.telling) + recent)
            if found:
                return re.sub(r"^\[[^\]]* @ 0x[0-9a-f]+\]\s*", "", found.strip())[:200]
        for line in recent:
            if any(word in line for word in _GENERIC):
                continue
            if any(word in line for word in ("rror", "failed", "Invalid", "denied", "403", "404", "refused",
                                             "片源")):
                line = re.sub(r"^\[[^\]]* @ 0x[0-9a-f]+\]\s*", "", line.strip())
                return re.sub(r"https?://\S+", "<地址>", line)[:200]
        return ""


def routes_known(line: str) -> bool:
    return live_routes.classify(line) != "other"


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
    """所有设备的「在电脑上看」：每台设备至多一路，没人看就停（reap，后台每秒一次）。

    换台快一点（warm_max > 0，只在自建 / 电脑客户端、内存够时开，中转的 --pc-warm）：
    - 刚换走的直播台再转 warm_s 秒（open 的 keep_old）：换回来时接上它，列表里已经有十几秒，马上有画面。
      只换封装的才留（转码的一路吃三四个核）。
    - 预热（warm）：电脑上看直播时，页面让中转先把相邻的台转起来（停在上一台 / 下一台的按钮上、看了几秒以后），
      换过去时同样接上。预热的台要转码时不预热（一路吃一个核），只记下转法。
    预热的和刚换走的合起来至多 warm_max 路，和正在看的一起算在 max_streams 里：真要看的路数不够时先停预热的，
    预热从不挡人看。转了 warm_s 秒没人接上就停。
    每个直播台的转法和跳转以后真正的列表地址记在内存里（LiveMemory）：下次直接按它起，要转码的台不用先起一次
    再重起，经跳转的源少走两三步。"""

    def __init__(self, ffmpeg: str, secret: bytes, log=print, clock=time.monotonic, wall=time.time,
                 popen=subprocess.Popen, idle_s: float = IDLE_S, max_streams: int = MAX_STREAMS,
                 background: bool = True, max_per_user: int = MAX_PER_USER, warm_max: int = 0,
                 warm_s: float = WARM_S):
        self.ffmpeg = ffmpeg
        self.secret = secret
        self.log = log
        self.clock = clock
        self.wall = wall
        self.popen = popen
        self.idle_s = idle_s
        self.max_streams = max_streams
        self.max_per_user = max_per_user
        self.warm_max = max(0, int(warm_max))
        self.warm_s = warm_s
        self.background = background
        self._by_device: dict[str, WatchStream] = {}
        # 预热的和刚换走的：片源的键 → 那一路（stream.user 是谁的）。
        self._warm: dict[tuple, WatchStream] = {}
        self.memory = LiveMemory(clock=clock)
        # 直播台的线路：上次好用的先试，坏了的排最后（RouteMemory）；routes_path 由中转设成全局频道表的路径。
        self.routes_path: Callable[[], Path | None] = lambda: None
        self.routes = RouteMemory(lambda: self.routes_path(), clock=clock, log=log)
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        # 别处占着的名额：点播（vod.py 的 VodService.load）的 [(位置, 用户)]，和这里的一起数。
        self.also: Callable[[], list] = lambda: []
        # 预先解析（prepare）：解析用的函数（测试换掉）、正在解析的键、线程池（第一次用时才建）。
        self.resolver: Callable[[str, str], str | None] = resolve_playlist
        self._preparing: set[tuple] = set()
        self._prepare_pool: ThreadPoolExecutor | None = None

    def open(self, device: str, source: Source, user: int | None = None, restart: bool = False,
             keep_old: bool = False, route: int | None = None) -> WatchStream:
        """device 这个位置（设备 id，或直接看时的「pc<用户>-<页面>」）要看 source：同一个片源还在转就
        接着用，否则停掉旧的、起新的。满了（全中转 max_streams 路，或这个用户 max_per_user 路）时 Busy。
        restart：跳转（播放器上拖进度条）——同一个片源也重起一个 ffmpeg，从 source.offset / start 起；
        新的一路有新的编号，地址（令牌）跟着换，旧地址马上失效。
        keep_old：电脑上直接看时换台——旧的是直播台就不马上停，再转 warm_s 秒（开了预热时）；
        新的这个台已经在预热（或刚换走）就接上那一路。
        route：直播台在页面上选了第几条线路（从 0 数）——正在这条线路上放的照旧接着用，否则重起一路、先试它
        （不接预热的、换走的这一路不留）。"""
        evicted: list[WatchStream] = []
        if route is not None and not (is_live(source) and len(source.routes) > 1 and 0 <= route < len(source.routes)):
            route = None
        with self._lock:
            current = self._by_device.get(device)
            if route is not None:
                if current is not None and current.alive and current.source.key == source.key \
                        and current.route == route and current._legs:
                    current.touch()
                    return current
                restart, keep_old = True, False
            if current is not None and current.alive and current.source.key == source.key and not restart:
                current.touch()
                return current
            others = [s.user for d, s in self._by_device.items() if d != device and s.alive]
            others += [u for d, u in self.also() if d != device]
            if len(others) >= self.max_streams:
                raise Busy()
            if user is not None and others.count(user) >= self.max_per_user:
                raise Busy(per_user=True)
            adopted = None
            if not restart and source.key in self._warm:
                warm = self._warm[source.key]
                if warm.user == user and warm.alive:
                    del self._warm[source.key]
                    adopted = warm
            if current is not None:
                del self._by_device[device]
                if keep_old and self._lingers(current):
                    evicted += self._park(current)
                else:
                    evicted.append(current)
            if adopted is not None:
                adopted.device = device
                adopted.cheap = False
                adopted.warm_until = 0.0
                adopted.touch()
                stream = adopted
            else:
                stream = self._new(device, source, user, route=route)
            self._by_device[device] = stream
            evicted += self._fit()
            self._ensure_reaper()
        self._stop(evicted)
        if adopted is not None:
            self.log(f"在电脑上看 {device}：接上预热的 {source.label or source.key[1]}")
            return stream
        self.log(f"在电脑上看 {device}：开始 {source.label or source.key[1]}")
        stream.start()
        return stream

    def prepare(self, sources: list[Source]) -> dict[str, int]:
        """直播页上看得见的这些台：先把跳转和主列表走一遍，真正的列表地址记进 LiveMemory（resolve_playlist）。
        有几条线路的只解析会先试的那条。返回各有几个：started 开始解析、fresh 记住的还新、busy 正在解析、skipped 不用。"""
        counts = {"started": 0, "fresh": 0, "busy": 0, "skipped": 0}
        jobs = []
        for source in sources[:PREPARE_MAX]:
            if not is_live(source) or source.opener is not None or not isinstance(source.url, str):
                counts["skipped"] += 1
                continue
            order = self.routes.order(source.key[1], list(source.routes)) if len(source.routes) > 1 else None
            leg = legs_of(source, order)[0][1]
            age = self.memory.age(leg.key)
            with self._lock:
                if age is not None and age < PREPARE_FRESH_S:
                    counts["fresh"] += 1
                    continue
                if leg.key in self._preparing:
                    counts["busy"] += 1
                    continue
                self._preparing.add(leg.key)
            counts["started"] += 1
            jobs.append(leg)
        for leg in jobs:
            if self.background:
                if self._prepare_pool is None:
                    self._prepare_pool = ThreadPoolExecutor(PREPARE_WORKERS, thread_name_prefix="watch-prepare")
                self._prepare_pool.submit(self._prepare_one, leg)
            else:
                self._prepare_one(leg)
        return counts

    def _prepare_one(self, leg: Source) -> None:
        try:
            found = self.resolver(leg.url, leg.user_agent)
            if found is not None:
                self.memory.remember_url(leg.key, found)
        finally:
            with self._lock:
                self._preparing.discard(leg.key)

    def warm(self, user: int | None, source: Source) -> str:
        """先把这个直播台转起来（没人看），换过去时 open 接上。返回发生了什么（给页面和测试看）：
        "off" 没开预热、"live" 不是直播台、"playing" 这个用户正在看它、"slow" 这个用户正在看的直播片源跟不上
        （不再多拉一路和它抢）、"warm" 已经在转（续上 warm_s 秒）、
        "encode" 要转码不预热、"full" 中转的路数满了、"started" 起了一路。"""
        if not self.warm_max:
            return "off"
        if not is_live(source):
            return "live"
        evicted: list[WatchStream] = []
        with self._lock:
            mine = [s for s in self._by_device.values() if s.alive and s.user == user]
            if any(s.source.key == source.key for s in mine):
                return "playing"
            if any(is_live(s.source) and s.slow() for s in mine):
                return "slow"
            found = self._warm.get(source.key)
            if found is not None and found.alive and found.user == user:
                found.warm_until = self.clock() + self.warm_s
                return "warm"
            if found is not None:
                del self._warm[source.key]
                evicted.append(found)
            if self.mode_of(source).video == "encode":
                result = "encode"
            else:
                while len(self._warm) >= self.warm_max:
                    evicted.append(self._pop_oldest_warm())
                slots = sum(1 for s in self._by_device.values() if s.alive)
                if slots + len(self._warm) >= self.max_streams:
                    result = "full"
                else:
                    stream = self._new(f"warm{user}-{source.key[1]}", source, user, cheap=True)
                    stream.warm_until = self.clock() + self.warm_s
                    self._warm[source.key] = stream
                    self._ensure_reaper()
                    result = "started"
        self._stop(evicted)
        if result == "started":
            self.log(f"在电脑上看：预热 {source.label or source.key[1]}")
            stream.start()
        return result

    def _new(self, device: str, source: Source, user: int | None, cheap: bool = False,
             route: int | None = None) -> WatchStream:
        return WatchStream(secrets.token_hex(8), device, source, self.ffmpeg, log=self.log, clock=self.clock,
                           popen=self.popen, user=user, mode=self.mode_of(source), memory=self.memory,
                           cheap=cheap, route_memory=self.routes, first_route=route)

    def mode_of(self, source: Source) -> Mode:
        """这个片源先按哪种转法起：片库的按探测好的（Source.mode），记住了的直播台按记住的，别的先试只换封装。"""
        if source.mode is not None:
            return source.mode
        known = self.memory.mode(source.key) if is_live(source) else None
        return known or FIRST_MODE

    def _lingers(self, stream: WatchStream) -> bool:
        """换走的这一路要不要再转一会儿：开了预热、是直播台、还在转、只换封装。转码的（HEVC 之类）不留：
        量过一路 1080p HEVC 转 720p 要三四个核、几百 MB 内存；换回来时按记住的转法直接起，不用再重起一次。"""
        return bool(self.warm_max) and stream.alive and is_live(stream.source) and \
            stream.mode is not None and stream.mode.video != "encode"

    def _park(self, stream: WatchStream) -> list[WatchStream]:
        """（持锁）换走的直播台放进预热里转 warm_s 秒；返回要停的（同一个台旧的预热、超出 warm_max 的）。"""
        evicted = []
        old = self._warm.pop(stream.source.key, None)
        if old is not None:
            evicted.append(old)
        while len(self._warm) >= self.warm_max:
            evicted.append(self._pop_oldest_warm())
        stream.warm_until = self.clock() + self.warm_s
        self._warm[stream.source.key] = stream
        return evicted

    def _pop_oldest_warm(self) -> WatchStream:
        key = min(self._warm, key=lambda k: self._warm[k].warm_until)
        return self._warm.pop(key)

    def _fit(self) -> list[WatchStream]:
        """（持锁）正在看的加预热的超过 max_streams 时，先停最早到期的预热的。"""
        evicted = []
        slots = sum(1 for s in self._by_device.values() if s.alive)
        while self._warm and slots + len(self._warm) > self.max_streams:
            evicted.append(self._pop_oldest_warm())
        return evicted

    @staticmethod
    def _stop(streams: list[WatchStream]) -> None:
        for stream in streams:
            stream.stop()
            stream.window.release()

    def warm_streams(self) -> list[WatchStream]:
        with self._lock:
            return list(self._warm.values())

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
        """停掉 idle_s 秒没人取的；预热的到了 warm_until 还没人接上的、自己停了的。"""
        now = self.clock()
        with self._lock:
            idle = [(d, s) for d, s in self._by_device.items() if now - s.seen > self.idle_s]
            for device, _ in idle:
                del self._by_device[device]
            cold = [(k, s) for k, s in self._warm.items() if now > s.warm_until or not s.alive]
            for key, _ in cold:
                del self._warm[key]
        for device, stream in idle:
            if stream.alive:
                self.log(f"在电脑上看 {device}：{int(self.idle_s)} 秒没人看，停了")
            stream.stop()
            stream.window.release()
        for _, stream in cold:
            if stream.alive:
                self.log(f"在电脑上看：预热的 {stream.source.label or stream.source.key[1]} 没人接上，停了")
        self._stop([s for _, s in cold])

    def stop_all(self) -> None:
        with self._lock:
            streams = list(self._by_device.values()) + list(self._warm.values())
            self._by_device.clear()
            self._warm.clear()
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
                    if not self._by_device and not self._warm:
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


def live_lines(path: Path | None, channel: str) -> list[tuple[str, str]]:
    """这个台的所有线路 [(地址, UA)]：频道表里的（主线路）在前，再是频道表旁边 meta 里的备用线路
    （server/routes.py），频道表的顺序。不在表里时为空。"""
    found = live_source(path, channel)
    if found is None:
        return []
    url, agent = found
    return live_routes.table_routes(path, channel, url, agent, DEFAULT_USER_AGENT)


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
        progress = stream.source.progress
        if not stream.wait_ready(READY_WAIT_S if progress is None else PROGRESS_WAIT_S):
            if stream.alive and progress is not None and not stream.notice:
                # 片库起播：说说读到哪了（网盘上的大文件第一段要好几秒），页面显示出来再来取。
                handler._send(503, (progress() or "正在打开…").encode(), "text/plain; charset=utf-8",
                              {"X-RTV-Notice": "1"})
            elif stream.alive:
                # 换线路时正文是「CCTV2 线路 1 失效（…），正在换线路 2…」，带 X-RTV-Notice：播放器显示出来、
                # 接着等（不算进它「503 至多等几次」的次数，线路有几条就可能要等几轮）。
                notice = stream.notice
                handler._send(503, (notice or "还在连片源，稍后再试").encode(), "text/plain; charset=utf-8",
                              {"X-RTV-Notice": "1"} if notice else None)
            else:
                # 放了一阵断了、还有别的线路：X-RTV-Reopen，播放器自己重开一次（会从下一条线路起）。
                handler._send(410, (stream.error or "这一路已经停了").encode(), "text/plain; charset=utf-8",
                              {"X-RTV-Reopen": "1"} if stream.reopen else None)
            return True
        stream.touch()
        text = stream.window.master() if name == "master.m3u8" else stream.window.playlist()
        # 有几条线路的直播台：X-RTV-Route 是现在在放第几条（从 1 数）。自动换了线路以后播放器头上的「线路 2/3」
        # 跟着变（页面每秒取一次列表，不用再问 /api/state）。
        # 直播片源跟不上（WatchStream.slow）：X-RTV-Slow 是最近的速度（0.6 就是六成），播放器说一声「片源慢」。
        headers = {}
        if is_live(stream.source) and len(stream.source.routes) > 1:
            headers["X-RTV-Route"] = str(stream.route + 1)
        if is_live(stream.source) and stream.slow():
            headers["X-RTV-Slow"] = f"{stream.speed():.2f}"
        handler._send(200, text.encode(), PLAYLIST_TYPE, headers or None)
        return True
    data = stream.window.init if name == "init.mp4" else stream.window.segment(int(seq))
    if seq is not None:
        stream.fetched(int(seq))
    if data is None:
        handler._not_found()
        return True
    handler._send(200, data, "video/mp4")
    return True
