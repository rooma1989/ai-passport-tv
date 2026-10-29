"""我的片库（自建 / 电脑客户端，M7 第一步）：家里自己的视频——网盘（经 OpenList 挂成 WebDAV）和这台电脑上的文件夹。

为什么这样做：主人的视频在百度网盘和夸克里，OpenList（Alist 的分支）把它们挂成 WebDAV（基本认证、PROPFIND、
带 Range 的 GET；GET 常常 302 到一个签了名的 CDN 地址）。所以来源先有两种：WebDAV（地址、用户名、密码）和本机
文件夹（Mac 上的绝对路径）。来源是一小层（LocalFolder、WebDav：list_dir / read_small / 取文件），以后加百度自己
的接口只要再写一种。

- 来源存在数据目录（数据库旁边）的 library.json：WebDAV 的密码也在里面，所以文件是 600、整份换掉写（和
  subscriptions.py 一样）。密码从不回给页面（只说 has_password），日志只写来源的名字，从不写地址。
- 索引在 library-index.json：每个视频一条（id 是来源 id + 路径的短哈希；标题、年份、电影 / 剧集 / 其他、剧名、
  第几季第几集、大小、修改时间、扩展名、能不能在电脑上放、海报的路径、简介）。重新扫描时大小和修改时间都没变的
  条目照旧用（不再读 .nfo）。每个来源至多走 WALK_MAX 个条目、往下 DEPTH_MAX 层；隐藏的文件和文件夹、群晖的
  @eaDir、回收站这些跳过。
- 本地的元数据：.nfo（标题、年份、简介）、视频旁边的海报（poster.jpg、folder.jpg、<名字>-poster.jpg、
  <名字>.jpg）、文件名（电影「名字 (2019)」「名字.2019.1080p…」；剧集 S01E02、EP03、第3集、E03，按剧名或上一层
  文件夹归成一部剧；文件名只有画质标记的用上层文件夹，「苏.里.南」连起来，英文名留着当第二个查法）。
  网盘上的文件从不截图（网盘会限速）；这一步本机的也不截，没海报的用页面自己画的图。
- 在线资料（libmeta.py）：扫描完了后台慢慢从豆瓣查电影和剧的片名、海报、评分、导演演员、简介（不登录、一次一个
  请求、被拦了歇一个小时），存在 library-meta.json，按剧 / 电影记；本地的 .nfo 片名和海报优先，管理员手动选的、
  「不匹配」的不被自动的盖掉。片库的设置里能关（关了一个请求也不往豆瓣发）。管理员填了 TMDB 的密钥时，豆瓣没把握、
  拦着或连不上的再查 TMDB（libmeta.Tmdb，备用；豆瓣永远先查），资料上标着来源。
- 网盘上小的 .nfo 扫描时可以取（每个至多 NFO_MAX 字节，一次扫描至多 NFO_PER_SCAN 个）；海报扫描时不取，只记路径，
  页面要时才取一次，存在 library-art/（总共至多 ART_CACHE_MAX 字节，超了删最旧的）。
- 扫描在后台线程里一个一个来（管理员点「扫描」，另外每 EVERY_S 秒自动一次）；为了不和给小电视推流抢，每列一个
  文件夹歇一下（PACE_LOCAL_S、PACE_DAV_S）。状态（在不在扫、走了多少、出错的大白话）给页面看。
- 放文件只认条目 id，从不收路径。本机的文件按 Range 回 206 / 200；网盘的把 Range 带给上游（基本认证），跟着
  302 走（Range 再带一次；换了主机就不带认证），上游的状态、Content-Range、Content-Length 原样回给浏览器。
  都是边读边发（CHUNK），不整个读进内存。
- 在电脑上放（第二步）：每个视频第一次放时探测一次各路流（ffprobe，电脑客户端没有 ffprobe 时用 ffmpeg 打的输入
  信息；PROBE_ARGS、PROBE_TIMEOUT_S），结果存在索引的条目里（media），大小或修改时间变了再探测。每次放按探测的结果
  和页面报的能力（MediaSource / <video> 能不能放 HEVC）定怎么放（plan）：mp4 / m4v / mov、画面浏览器放得了、
  第一路声音是 AAC / MP3 的直接按 Range 放原文件；别的经 watch.py 封装成分片 MP4（第一路真画面——封面图不算——
  能放就只换封装，HEVC 写成 hvc1，放不了才转成 H.264；声音有 AAC 就直接用，没有就把默认的那一路转成 AAC 立体声；
  字幕和附件从不放进去）。ffmpeg 读的是只给本机的 /api/library/raw/<id>?t=<令牌>（raw）：网盘的地址和密码从不在
  ffmpeg 的命令行上，本机的文件也不用 file: 协议。网盘的文件每次起 ffmpeg 都要再读的「目录」（MP4 的 moov、MKV 的头、
  附件和 Cues）存在 library-cache/（librarycache.py，_serve_cached），只给 raw 用。扫描找到新的（或变了的）网盘片子
  以后，没人在放时后台先把这几段读进缓存、探测一次（预热，warm_step），第一次放也和第二次一样快。正在看的那一集
  另外边看边整个下到本机（整集缓存，libraryfull.py，_serve_full）：跳到下好的地方从本机读，几乎马上就有画面。
- 字幕（第二步）：内嵌的文字字幕（ass、srt、mov_text、webvtt）和视频旁边的字幕文件（<名字>.srt、<名字>.zh.ass…，
  本机和网盘）按需转成 WebVTT（/api/library/subs/<id>/<n>.vtt，只给家里人，缓存在 library-subs/）。图片字幕（PGS、
  DVD）列出来但放不了。网盘上的内嵌字幕不整个再读一遍文件，而是在放的那一路边转边抽（watch_source 的 subs_out）。
- 播到小电视、接着看（第三步）：「播放到」是小电视时条目放进它的投屏位，中转起 ffmpeg 读同一个 raw 地址，缩成
  电视服务要的画面推过去（tv_args、play_tv）；进度和电脑上的记在一起（pc-progress.json 的 lib:<id>），剧集放完一集
  接着放下一集。详见「播到小电视」那一节。
- 谁能看：登录的家里人看到整个片库；访客永远看不到（404）。加 / 删来源、扫描只有管理员（admin.py）。
  云端的中转没有片库（relay.library 为 None）。
- 网盘组件（server/selfhost/openlist.py 装的 OpenList）开着时，中转（clouddrive.py）在这里维护一个「受管」的来源
  （managed = "openlist"）：名字「网盘」，地址 http://127.0.0.1:<端口>/dav，用户名、密码是组件的管理员。页面不能
  删它（只有停用组件才去掉），密码照样从不回给页面，端口换了地址跟着改（条目 id 只跟来源 id 和路径有关，不用重扫）。

只用标准库。
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import os
import re
import math
import secrets
import select
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .librarycache import IndexCache
from . import libraryfull
from .libraryfull import FullCache
from . import libmeta
from .table import CAST_ID

SOURCES_MAX = 12
NAME_MAX = 40
URL_MAX = 500
# 每个来源至多走这么多个条目（文件和文件夹都算）、往下这么多层。
WALK_MAX = 20000
DEPTH_MAX = 8
# 子文件夹列不出来的，跳过；太多了（网盘多半出了事）就当这次扫描失败。
SKIPPED_DIRS_MAX = 20
# .nfo：每个至多这么大，一次扫描至多从网盘取这么多个（本机的不限个数）。
NFO_MAX = 64 * 1024
NFO_PER_SCAN = 300
# 海报：每张至多这么大；网盘的取下来存在 library-art/，总共至多这么大。
ART_MAX = 8 * 1024 * 1024
ART_CACHE_MAX = 200 * 1024 * 1024
# 取海报失败了，这么久以内不再去网盘取同一张。
ART_RETRY_S = 600.0
# 定时扫描：成功的隔 EVERY_S（网盘的隔 DAV_EVERY_S：在手机上存进网盘的片子一小时内就出现），失败的隔 RETRY_S
# 再试；每 TICK_S 看一眼；启动后先等 START_DELAY_S。打开片库页时，上次扫描已经过了 OPEN_FRESH_S 的来源也扫一遍。
EVERY_S = 12 * 3600.0
DAV_EVERY_S = 3600.0
OPEN_FRESH_S = 600.0
RETRY_S = 3600.0
TICK_S = 600.0
START_DELAY_S = 300.0
# 低优先级：每列一个文件夹歇一下（网盘那边一次 PROPFIND 就是一次网络请求，歇久一点）。
PACE_LOCAL_S = 0.005
PACE_DAV_S = 0.3
TIMEOUT_S = 20
# 一个 PROPFIND 的回答至多这么大（一个文件夹几千个文件也够）。
LISTING_MAX = 16 * 1024 * 1024
MAX_REDIRECTS = 5
CHUNK = 256 * 1024
# 同时至多放这么多个文件（浏览器跳转时会多开几个请求）。
FILES_AT_ONCE = 12
# 页面一次至多给这么多个条目（几百部片子远远够）。
VIEW_MAX = 6000
PLOT_MAX = 600
TITLE_MAX = 120
RECENT_MAX = 12
# 在线资料（libmeta.py，豆瓣）：后台每查完一部歇 META_GAP_S（一分钟三部左右）；没事做时每 META_TICK_S 看一眼，
# 扫描完了马上看；启动后先等 META_START_S。查不成（连不上）的这么久以内不再查同一部。
META_GAP_S = 20.0
META_TICK_S = 600.0
META_START_S = 60.0
META_FAIL_S = 3600.0
# 预热（warm_step）：扫描找到新的网盘片子以后，后台一个一个（新的先）把索引区读进缓存（librarycache 的 tee）、
# 探测一次。每轮至多从网盘读 WARM_BUDGET 字节、每个文件只读索引区；读的时候至多 WARM_RATE 字节 / 秒。有人在看、
# 在放（片库的文件正在给出去、WARM_QUIET_S 秒内给过）、在扫描时不做，做到一半有人开始放就停下（下一轮接着读）。
# 网盘出错了停下、歇 WARM_BACKOFF_S（接连出错翻倍，至多 WARM_BACKOFF_MAX_S）；同一个文件失败 WARM_TRIES 次不再预热。
# 缓存里 WARM_KEEP_S 内用过的从不为了预热挤掉（不够地方就不预热）。中转启动时 WARM_NEW_S 内加进来、还没探测过的
# 也算新的。启动后先等 WARM_START_S，之后每 WARM_TICK_S 看一眼（扫描完了马上看）。
WARM_BUDGET = 300 * 1024 * 1024
WARM_RATE = 4 * 1024 * 1024
WARM_QUIET_S = 120.0
WARM_BACKOFF_S = 600.0
WARM_BACKOFF_MAX_S = 6 * 3600.0
WARM_TRIES = 2
WARM_KEEP_S = 14 * 86400.0
WARM_NEW_S = 3 * 86400.0
WARM_START_S = 120.0
WARM_TICK_S = 180.0
_META_KEY = re.compile(r"^(?:s[0-9a-f]{12}|m[0-9a-f]{16})$")
_POSTER_FILE = re.compile(r"^(?:douban-\d{1,12}|tmdb-(?:tv|movie)-\d{1,10})\.(?:jpg|png|webp)$")
# 网盘组件（OpenList）的来源：clouddrive.py 维护，页面不能删。
MANAGED = "openlist"
MANAGED_NAME = "网盘"
MANAGED_NOTE = "停用网盘组件才会去掉这个来源"

# 浏览器能直接放的容器（第一步只放这些）；别的格式第二步起经 watch.py 封装或转码后放（plan）。
PLAYABLE = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime"}
OTHER_VIDEO = {".mkv": "video/x-matroska", ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".mts": "video/mp2t",
               ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".webm": "video/webm",
               ".rmvb": "application/vnd.rn-realmedia-vbr", ".rm": "application/vnd.rn-realmedia",
               ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".vob": "video/mpeg", ".3gp": "video/3gpp",
               ".f4v": "video/mp4", ".hevc": "video/mp4"}
VIDEO_TYPES = {**PLAYABLE, **OTHER_VIDEO}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
# 跳过的文件夹和文件（不分大小写）；另外以「.」开头的都跳过（隐藏的、macOS 的 ._ 文件）。
SKIP_NAMES = {"@eadir", "#recycle", "@recycle", "$recycle.bin", "recycler", "system volume information",
              "lost+found", ".ds_store", "thumbs.db", "desktop.ini", ".trash", ".trashes", "@tmp"}
# 片花、样片不算。
_EXTRA = re.compile(r"(?i)(?:^|[ ._-])(sample|trailer|预告|花絮)$")

_ID = re.compile(r"^[0-9a-f]{16}$")
_SOURCE_ID = re.compile(r"^[0-9a-f]{8}$")
_RANGE = re.compile(r"^bytes=(\d{0,15})-(\d{0,15})$")
_CONTENT_RANGE = re.compile(r"^bytes (\d{1,15})-(\d{1,15})/(\d{1,15})$")
_LOCK = threading.RLock()
DAV = "{DAV:}"
PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
                 b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>")
UA = "RooTV-Library/1"


class LibraryError(Exception):
    """给人看的原因（大白话，不带地址）。"""


def item_id(source: str, rel: str) -> str:
    return hashlib.sha1(f"{source}\0{rel}".encode("utf-8")).hexdigest()[:16]


def _skip(name: str) -> bool:
    return not name or name.startswith(".") or name.casefold() in SKIP_NAMES


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].casefold()


# ---------------------------------------------------------------- 文件名

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_number(text: str) -> int | None:
    """「12」「十二」「二十三」「一百零五」→ 数字；认不出 None。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if not text or any(c not in _CN_DIGITS and c not in "十百" for c in text):
        return None
    total, digit = 0, None
    for c in text:
        if c == "百":
            total += (digit if digit is not None else 1) * 100
            digit = None
        elif c == "十":
            total += (digit if digit is not None else 1) * 10
            digit = None
        else:
            digit = _CN_DIGITS[c]
    return total + (digit or 0)


# 剧集：S01E02、第3集 / 第十二话、EP03 / E03、Episode 3。
_SXXEYY = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,4})(?!\d)")
_DI_JI = re.compile(r"第\s*([0-9零〇一二两三四五六七八九十百]{1,6})\s*[集话話期]")
# E 后面至多三位数：「WALL-E 2008」的年份不算集数。
_EPISODE = re.compile(r"(?i)(?<![a-z0-9])(?:episode|ep|e)[ ._-]?(\d{1,3})(?!\d)")
# 季的文件夹：Season 2、S02、第二季。
_SEASON_DIR = re.compile(r"(?i)^(?:season|s)[ ._-]?(\d{1,2})$")
_DI_JI_SEASON = re.compile(r"第\s*([0-9零〇一二两三四五六七八九十]{1,4})\s*季")
# 电影的年份：「名字 (2019)」「名字.2019.1080p」；前后不能挨着字母数字（1080p、x2019 不算）。
_YEAR = re.compile(r"(?i)(?<![0-9a-z])[(\[（【]?((?:19|20)\d{2})[)\]）】]?(?![0-9a-z])")
# 没有年份时，标题到第一个这类标记为止（分辨率、片源、编码、声音、HDR、发布的网站，还有中文的「高码版」「内封」
# 「字幕」「收藏版」「片源」这些）。中文的前后不看挨着什么（「NF高码版」「内封精修官译简中字幕」）。
_JUNK = re.compile(r"(?i)(?<![0-9a-z])((?:hd)?(?:tc|ts|cam)?(?:2160p|1080[pi]|720p|480p)|4k|8k|uhd|fhd|hq|blu-?ray|bdrip|brrip|bdremux|remux|"
                   r"web-?dl|webrip|hdtv|dvdrip|x26[45]|h\.?26[45]|hevc|avc|aac|e?ac3|ddp?(?:\d(?:\.\d)?)?|"
                   r"dd\+|dts(?:-?hd)?|truehd|atmos|flac|hdr(?:10\+?)?|sdr|dovi|10bit|8bit|60fps|120fps|nf|amzn|"
                   r"dsnp|hmax|atvp|itunes)(?![0-9a-z])|"
                   r"(高码率|高码版|高码|内封|内嵌|外挂|精修|官译|简中|繁中|简繁|中字|中英|双语|国语|粤语|字幕|收藏版|"
                   r"片源|蓝光|原盘|无删减|未删减|完整版|修复版|特效|杜比视界|杜比|全景声|60帧|高帧率|高清|超清)")
# 一段名字里的这些也算「只有标记」：声道（5.1）、单个数字、发布组前的横线。
_JUNK_REST = re.compile(r"(?i)(?<![0-9a-z])(?:\d(?:\.\d)?|ma|x|h)(?![0-9a-z])")
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# 「苏.里.南」「苏 里 南」：一个一个隔开的汉字连起来。
_SPACED_CJK = re.compile(rf"(?<![{_CJK}])[{_CJK}](?:[ ._·・][{_CJK}])+(?![{_CJK}])")
# 「10J间G敢S死D队2026」：汉字中间夹着单个字母（防搜索的写法）；夹了两处以上才去掉挨着汉字的字母。
_INTERLEAVED = re.compile(rf"[{_CJK}][A-Za-z][{_CJK}]")
_BESIDE_CJK = re.compile(rf"[A-Za-z](?=[{_CJK}])|(?<=[{_CJK}])[A-Za-z]")
# 这些文件夹不是片名（分类、网盘的分享文件夹、下载文件夹）：找片名时跳过。
GENERIC_DIRS = {"来自：分享", "来自:分享", "我的资源", "资源", "分享", "电影", "电视剧", "剧集", "美剧", "韩剧", "日剧",
                "国产剧", "动画", "动漫", "纪录片", "综艺", "视频", "下载", "我的视频", "影视", "夸克", "百度网盘", "网盘",
                "movies", "movie", "films", "film", "tv", "tv shows", "shows", "series", "videos", "video",
                "downloads", "download", "media", "quark", "baidu"}
# 解析文件名的写法改了就加一：索引里旧的条目重新解析（探测的结果照旧留着）。
PARSE_VERSION = 4
# 文件名开头的字幕组标签：[字幕组]、【xxx】。
_TAG = re.compile(r"^\s*(?:\[[^\]]*\]|【[^】]*】)\s*")


def clean_title(text: str) -> str:
    """点、下划线换成空格，去掉开头的字幕组标签、两头的横线和括号，压掉多余的空格。"""
    text = text.replace("_", " ")
    if " " not in text.strip() and text.count(".") >= 1:
        text = text.replace(".", " ")
    else:
        text = re.sub(r"\.(?=\S)", " ", text)
    while True:
        trimmed = _TAG.sub("", text)
        if trimmed == text:
            break
        text = trimmed
    text = re.sub(r"[\[(（【]\s*[\])）】]", "", text)
    # 网盘里重复转存出来的副本编号：「神探之痕迹(2)」「什么意思夫妇(2)(1)」。
    text = re.sub(r"(?:\s*[(（]\d{1,2}[)）])+\s*$", "", text)
    text = " ".join(text.split()).strip(" -–—·._[(（【")
    return text[:TITLE_MAX]


def _has_cjk(text: str) -> bool:
    return re.search(f"[{_CJK}]", text or "") is not None


def _cjk_count(text: str) -> int:
    return len(re.findall(f"[{_CJK}]", text or ""))


def unmask(name: str) -> str:
    """「苏.里.南」→「苏里南」；「10J间G敢S死D队2026」→「10间敢死队2026」。别的不动。"""
    name = _SPACED_CJK.sub(lambda m: re.sub(r"[ ._·・]", "", m.group(0)), name)
    if len(_INTERLEAVED.findall(name)) >= 2:
        name = _BESIDE_CJK.sub("", name)
    return name


def junk_only(text: str) -> bool:
    """名字里只有画质、编码、声音这些标记（「4K HQ SDR 高码率 DDP DTS」），没有片名。"""
    rest = _JUNK.sub(" ", text)
    rest = _YEAR.sub(" ", rest)
    rest = _JUNK_REST.sub(" ", rest)
    return not re.sub(r"[\s._\-–—+\[\]()（）【】]", "", rest)


def _title_folder(folder: str) -> bool:
    """这个文件夹的名字像片名：不是季的文件夹、不是分类 / 下载这类、不是只有标记。"""
    if not folder or _season_of(folder) is not None:
        return False
    if " ".join(folder.casefold().split()) in GENERIC_DIRS:
        return False
    title = _movie_title(folder)[0]
    return bool(title) and not junk_only(title)


def _movie_title(stem: str) -> tuple[str, int | None]:
    """(标题, 年份)：年份前面的就是标题；没有年份时到第一个画质标记为止。"""
    for match in _YEAR.finditer(stem):
        # 年份前面也可能夹着画质标记（「铁拳教育 4K高清完整版.育（2026）」）：到第一个标记为止。
        head = stem[:match.start()]
        junk = _JUNK.search(head)
        title = clean_title(head[:junk.start()] if junk and junk.start() > 0 else head)
        if title:
            return title, int(match.group(1))
    junk = _JUNK.search(stem)
    title = clean_title(stem[:junk.start()] if junk and junk.start() > 0 else stem)
    return title or clean_title(stem) or stem[:TITLE_MAX], None


def _season_of(folder: str) -> int | None:
    match = _SEASON_DIR.match(folder.strip())
    if match:
        return int(match.group(1))
    match = _DI_JI_SEASON.search(folder)
    return cn_number(match.group(1)) if match else None


def parse_name(rel: str) -> dict:
    """从路径（相对来源的根，用 / 分隔）猜：{kind: movie | episode | video, title, year, show, season, episode, alt}。
    文件名只有标记（「4K HQ SDR 高码率 DDP DTS.mp4」）时用最近的像片名的上层文件夹；剧集的文件名里只有英文名
    （「Narco-Saints.S01E01…」）、上层文件夹是带年份的中文名（「苏.里.南 (2022) 4K…」）时剧名用中文的，英文名留在
    alt（查豆瓣时第二个查）。剧集的 year 是剧的年份（文件夹或文件名里的）。"""
    parts = [unmask(p) for p in rel.split("/") if p]
    name = parts[-1] if parts else rel
    stem = os.path.splitext(name)[0]
    parent = parts[-2] if len(parts) >= 2 else ""
    grand = parts[-3] if len(parts) >= 3 else ""
    found = {"kind": "video", "title": "", "year": None, "show": "", "season": None, "episode": None, "alt": ""}
    episode = season = None
    cut = -1
    for pattern in (_SXXEYY, _DI_JI, _EPISODE):
        match = pattern.search(stem)
        if match is None:
            continue
        if pattern is _SXXEYY:
            season, episode = int(match.group(1)), int(match.group(2))
        else:
            episode = cn_number(match.group(1))
        if episode is not None:
            cut = match.start()
            break
    if episode is None and re.fullmatch(r"\d{1,3}", stem.strip()):
        episode, cut = int(stem.strip()), 0          # 「01.mp4」这样只有集数的，放在剧的文件夹里
    if episode is not None:
        show, year = _movie_title(stem[:cut]) if cut > 0 else ("", None)
        show = re.sub(r"(?i)[ ._-]*(?:s\d{1,2}|第[0-9一二三四五六七八九十]+季)$", "", show).strip(" -._")
        if junk_only(show):
            show = ""
        # 剧名：文件名里有就用；没有就用文件夹（季的文件夹再往上一层）。
        folder_season = _season_of(parent) if parent else None
        folder = grand if folder_season is not None and grand else parent
        if season is None:
            season = folder_season
        folder_title, folder_year = _movie_title(folder) if _title_folder(folder) else ("", None)
        alt = ""
        if not show:
            show, year = folder_title, year or folder_year
        elif folder_title and folder_year and _has_cjk(folder_title) and not _has_cjk(show) \
                and (year is None or year == folder_year):
            show, alt, year = folder_title, show, folder_year
        tail = clean_title(_JUNK.split(stem[cut:], 1)[0])
        found.update(kind="episode", show=show or "未命名的剧", season=season, episode=episode,
                     title=f"第 {episode} 集", year=year, alt=alt)
        # 集数后面跟着的集名（「S01E02 重逢」）。
        rest = re.sub(r"^(?i:s\d{1,2}[ ._-]?e\d{1,4}|第\s*\S{1,6}?\s*[集话話期]|(?:episode|ep|e)[ ._-]?\d{1,4}|\d{1,3})",
                      "", tail).strip(" -._")
        if rest and not _YEAR.fullmatch(rest):
            found["title"] = f"第 {episode} 集 · {rest}"[:TITLE_MAX]
        return found
    title, year = _movie_title(stem)
    alt = ""
    from_folder = False
    if junk_only(stem):
        # 文件名只有标记：用最近的像片名的上层文件夹（至多往上看三层）。
        folder = next((f for f in reversed(parts[-4:-1]) if _title_folder(f)), "")
        if folder:
            # 年份可能只写在文件名里（「龙/2026.WEB-DL.mp4」）：留着，查豆瓣时靠它分清同名的片子。
            stem_year = _YEAR.search(stem)
            title, year = _movie_title(folder)
            year = year or (int(stem_year.group(1)) if stem_year else None)
            from_folder = True
    elif parent and _title_folder(parent):
        folder_title, folder_year = _movie_title(parent)
        if not _has_cjk(title) and folder_year and _has_cjk(folder_title) and (year is None or year == folder_year):
            # 只有英文名、所在的文件夹是带年份的中文名，年份对得上：片名用中文的，英文名留着查。
            title, alt, year = folder_title, title, folder_year
        elif _cjk_count(title) == 1 and _cjk_count(folder_title) >= 2:
            # 文件名是乱的、只夹着一个汉字（「神探之痕迹/SHEN-碳.TC1080P.mp4」），文件夹是正经的中文片名：用文件夹的。
            title, year = folder_title, folder_year or year
            from_folder = True
    # 片名取自它自己的文件夹的（一部片一个文件夹）没有年份也算电影：查豆瓣时按电影找。
    found.update(kind="movie" if year or from_folder else "video", title=title, year=year, alt=alt)
    return found


# ---------------------------------------------------------------- .nfo

_NFO_ROOTS = {"movie": "movie", "episodedetails": "episode", "tvshow": "show", "musicvideo": "video"}


def parse_nfo(data: bytes) -> dict:
    """Kodi / Jellyfin 的 .nfo（XML）：{kind?, title?, year?, plot?, show?, season?, episode?}；读不了是 {}。
    不认带 DOCTYPE / ENTITY 的（防实体展开），XML 后面跟着一行网址的（常见）先截掉。"""
    if not data or len(data) > NFO_MAX:
        return {}
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return {}
    end = re.search(rb"</(movie|episodedetails|tvshow|musicvideo)\s*>", data)
    if end is not None:
        data = data[:end.end()]
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return {}
    kind = _NFO_ROOTS.get(root.tag.casefold())
    if kind is None:
        return {}

    def text(tag: str) -> str:
        return " ".join((root.findtext(tag) or "").split())

    found: dict = {"kind": kind}
    if text("title"):
        found["title"] = text("title")[:TITLE_MAX]
    year = text("year") or text("premiered")[:4] or text("aired")[:4]
    if re.fullmatch(r"(19|20)\d{2}", year[:4] or ""):
        found["year"] = int(year[:4])
    plot = (root.findtext("plot") or root.findtext("outline") or "").strip()
    if plot:
        found["plot"] = plot[:PLOT_MAX]
    if text("showtitle"):
        found["show"] = text("showtitle")[:TITLE_MAX]
    for tag in ("season", "episode"):
        if text(tag).isdigit():
            found[tag] = int(text(tag))
    return found


# ---------------------------------------------------------------- 来源

@dataclass(frozen=True)
class Entry:
    """列出来的一项：rel 是相对来源根的路径（/ 分隔，不以 / 开头）。"""
    rel: str
    name: str
    is_dir: bool
    size: int = 0
    mtime: int = 0


def _inside(root: str, path: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class LocalFolder:
    """这台电脑上的一个文件夹。从不走出根目录：路径先 realpath 再比前缀；指到外面的符号链接跳过，
    指向文件夹的符号链接也不跟（里面的东西要么在外面，要么已经走过一遍）。"""
    kind = "local"
    pace_s = PACE_LOCAL_S

    def __init__(self, root: str):
        self.root = os.path.realpath(root)

    def check(self) -> str:
        if not os.path.isdir(self.root):
            return "这个文件夹不存在"
        try:
            with os.scandir(self.root):
                pass
        except OSError:
            return "读不了这个文件夹（看看权限）"
        return ""

    def _real(self, rel: str) -> str | None:
        parts = [p for p in rel.split("/") if p]
        if any(p in (".", "..") for p in parts):
            return None
        path = os.path.realpath(os.path.join(self.root, *parts))
        return path if _inside(self.root, path) else None

    def list_dir(self, rel: str) -> list[Entry]:
        base = self._real(rel)
        if base is None:
            raise LibraryError("文件夹不在来源里")
        found = []
        try:
            with os.scandir(base) as listing:
                for entry in listing:
                    if _skip(entry.name):
                        continue
                    child = f"{rel}/{entry.name}" if rel else entry.name
                    try:
                        if entry.is_symlink():
                            target = os.path.realpath(entry.path)
                            if not _inside(self.root, target) or os.path.isdir(target):
                                continue
                        info = entry.stat()
                    except OSError:
                        continue
                    is_dir = stat.S_ISDIR(info.st_mode)
                    if not is_dir and not stat.S_ISREG(info.st_mode):
                        continue
                    found.append(Entry(child, entry.name, is_dir, 0 if is_dir else info.st_size, int(info.st_mtime)))
        except OSError as error:
            raise LibraryError(f"读不了文件夹（{type(error).__name__}）") from None
        return found

    def path_of(self, rel: str) -> str | None:
        path = self._real(rel)
        return path if path is not None and os.path.isfile(path) else None

    def read_small(self, rel: str, cap: int) -> bytes | None:
        path = self.path_of(rel)
        if path is None:
            return None
        try:
            with open(path, "rb") as handle:
                data = handle.read(cap + 1)
        except OSError:
            return None
        return data if len(data) <= cap else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """跳转自己跟（WebDav.open）：换了主机不能再带认证，urllib 自己跟会把 Authorization 也带过去。"""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _dav_reason(error: BaseException) -> str:
    """连 WebDAV 出错 → 大白话（不带地址）。"""
    if isinstance(error, urllib.error.HTTPError):
        code = error.code
        if code == 401:
            return "用户名或密码不对"
        if code == 403:
            return "网盘不让读（看看 OpenList 里这个用户的权限）"
        if code == 404:
            return "网盘上没有这个地址（OpenList 的 WebDAV 地址一般以 /dav 结尾）"
        if code in (405, 501):
            return f"这个地址不像 WebDAV（网盘回答 {code}）"
        return f"网盘出错了（{code}）"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, (TimeoutError, OSError)) and "timed out" in str(reason):
            return "连网盘超时了"
        if isinstance(reason, ConnectionRefusedError):
            return "连不上网盘（OpenList 开着吗？）"
        return f"连不上网盘（{type(reason).__name__ if isinstance(reason, BaseException) else '地址不对'}）"
    if isinstance(error, TimeoutError):
        return "连网盘超时了"
    return f"连不上网盘（{type(error).__name__}）"


def _same_origin(a: str, b: str) -> bool:
    x, y = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    port = lambda u: u.port or (443 if u.scheme == "https" else 80)  # noqa: E731
    return x.scheme == y.scheme and (x.hostname or "").casefold() == (y.hostname or "").casefold() and port(x) == port(y)


class WebDav:
    """一个 WebDAV 地址（OpenList 挂出来的网盘）：PROPFIND Depth: 1 一层一层列，GET 带 Range 取文件。"""
    kind = "webdav"
    pace_s = PACE_DAV_S

    def __init__(self, url: str, user: str = "", password: str = "", opener=None, timeout: float = TIMEOUT_S):
        self.base = url.rstrip("/") + "/"
        self.base_path = urllib.parse.unquote(urllib.parse.urlsplit(self.base).path)
        self.auth = ("Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
                     if user or password else "")
        self.opener = opener or _OPENER.open
        self.timeout = timeout

    def url_of(self, rel: str, folder: bool = False) -> str:
        rel = rel.strip("/")
        if not rel:
            return self.base
        return self.base + urllib.parse.quote(rel, safe="/") + ("/" if folder else "")

    def _headers(self, url: str, extra: dict | None = None) -> dict:
        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if self.auth and _same_origin(url, self.base):
            headers["Authorization"] = self.auth
        headers.update(extra or {})
        return headers

    def _propfind(self, rel: str, depth: str) -> bytes:
        url = self.url_of(rel, folder=True)
        request = urllib.request.Request(url, data=PROPFIND_BODY, method="PROPFIND", headers=self._headers(
            url, {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}))
        try:
            with self.opener(request, timeout=self.timeout) as answer:
                status = getattr(answer, "status", None) or answer.getcode()
                if status != 207:
                    raise LibraryError(f"这个地址不像 WebDAV（网盘回答 {status}）")
                data = answer.read(LISTING_MAX + 1)
        except LibraryError:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (301, 302, 303, 307, 308):
                raise LibraryError("网盘让跳到别的地址（看看 WebDAV 地址最后要不要 /）") from None
            raise LibraryError(_dav_reason(error)) from None
        except (OSError, ValueError) as error:
            raise LibraryError(_dav_reason(error)) from None
        if len(data) > LISTING_MAX:
            raise LibraryError("这个文件夹里的东西太多了")
        return data

    def check(self) -> str:
        try:
            self._propfind("", "0")
        except LibraryError as error:
            return str(error)
        return ""

    def _rel_of(self, href: str) -> str | None:
        path = urllib.parse.unquote(urllib.parse.urlsplit(href).path)
        if not path.startswith(self.base_path) and path.rstrip("/") + "/" != self.base_path:
            return None
        rel = path[len(self.base_path):].strip("/") if path.startswith(self.base_path) else ""
        parts = rel.split("/") if rel else []
        if any(p in ("", ".", "..") for p in parts):
            return None
        return rel

    def list_dir(self, rel: str) -> list[Entry]:
        data = self._propfind(rel, "1")
        upper = data[:4096].upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise LibraryError("网盘的回答看不懂")
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            raise LibraryError("网盘的回答看不懂") from None
        here = rel.strip("/")
        found = []
        for response in root.iter(DAV + "response"):
            child = self._rel_of(response.findtext(DAV + "href") or "")
            if child is None or child == here or not child.startswith(here + "/" if here else ""):
                continue
            if "/" in child[len(here) + 1 if here else 0:]:
                continue                        # 不是这一层的（有的服务器不管 Depth）
            prop = None
            for propstat in response.findall(DAV + "propstat"):
                status = propstat.findtext(DAV + "status") or "200"
                if re.search(r"\b2\d\d\b", status):
                    prop = propstat.find(DAV + "prop")
                    break
            if prop is None:
                continue
            name = child.rsplit("/", 1)[-1]
            if _skip(name):
                continue
            is_dir = prop.find(f"{DAV}resourcetype/{DAV}collection") is not None
            size = (prop.findtext(DAV + "getcontentlength") or "0").strip()
            mtime = 0
            stamp = (prop.findtext(DAV + "getlastmodified") or "").strip()
            if stamp:
                try:
                    mtime = int(email.utils.parsedate_to_datetime(stamp).timestamp())
                except (TypeError, ValueError, IndexError):
                    mtime = 0
            found.append(Entry(child, name, is_dir, 0 if is_dir or not size.isdigit() else int(size), mtime))
        return found

    def open(self, rel: str, wanted_range: str | None = None, timeout: float | None = None):
        """GET 这个文件：跟着跳转走（每一跳都带 Range；到了别的主机不带认证）。返回 urllib 的回答；
        上游出错时抛 urllib.error.HTTPError（调用的人看状态码），连不上抛 OSError。"""
        url = self.url_of(rel)
        for hop in range(MAX_REDIRECTS + 1):
            extra = {"Range": wanted_range} if wanted_range else {}
            host = (urllib.parse.urlsplit(url).hostname or "").casefold()
            if host.endswith("baidupcs.com"):
                extra["User-Agent"] = "pan.baidu.com"        # 百度网盘的下载地址只认这个 UA
            request = urllib.request.Request(url, headers=self._headers(url, extra))
            try:
                return self.opener(request, timeout=timeout or self.timeout)
            except urllib.error.HTTPError as error:
                location = error.headers.get("Location") if error.code in (301, 302, 303, 307, 308) else None
                if not location or hop == MAX_REDIRECTS:
                    raise
                error.close()
                url = urllib.parse.urljoin(url, location)
                if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
                    raise LibraryError("网盘让跳到一个不认识的地址") from None
        raise LibraryError("网盘跳转太多次")

    def read_small(self, rel: str, cap: int) -> bytes | None:
        try:
            with self.open(rel) as answer:
                data = answer.read(cap + 1)
        except (urllib.error.HTTPError, LibraryError, OSError, ValueError):
            return None
        return data if len(data) <= cap else None


# ---------------------------------------------------------------- Range

def parse_range(header: str | None, size: int):
    """浏览器的 Range → (起, 止)（含止）；没有、看不懂、多段的 → None（给整个）；超出文件 → "bad"（416）。"""
    match = _RANGE.match((header or "").strip())
    if match is None or not (match.group(1) or match.group(2)):
        return None
    first, last = match.group(1), match.group(2)
    if not first:
        length = int(last)
        if length == 0 or size == 0:
            return "bad"
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        return "bad"
    return start, end


def _client_gone(sock) -> bool:
    from .vod import client_gone
    return client_gone(sock)


# 从索引缓存给完了还要接着从网盘读以前，先等对方这么久看它挂不挂断：ffmpeg 从开头读几十 KB 就跳去读 moov，
# 缓存的那一段早就塞进了连接的缓冲区，不等一下每次都白开一个网盘的请求。真要接着读的只多等这一下（网盘的
# 请求本来就要约 0.2 s 才有第一个字节）。
CONTINUE_WAIT_S = 0.1


class _Tees:
    """放片子从网盘读到的同时存进几个缓存（整集缓存、索引缓存）：write 给每一个。"""

    def __init__(self, tees: list):
        self.tees = tees

    def write(self, chunk: bytes) -> None:
        for tee in self.tees:
            tee.write(chunk)


def _client_left(sock, wait: float) -> bool:
    """至多等 wait 秒，看浏览器 / ffmpeg 那头是不是挂断了。"""
    if sock is None or not hasattr(sock, "fileno") or hasattr(sock, "getpeercert"):
        return False
    try:
        select.select([sock], [], [], wait)
    except (OSError, ValueError):
        return False
    return _client_gone(sock)


# ---------------------------------------------------------------- 媒体（第二步）：探测、怎么放、字幕

# 探测的结果存在索引的条目里（media）：格式变了（这里的写法改了）就加一，旧的重新探测。
PROBE_VERSION = 1
PROBE_TIMEOUT_S = 25
# 网盘上的：夸克不开会员限速一秒一两 MB，mkv 里的字体附件也在文件头里整个读，实测 15–23 秒。
PROBE_TIMEOUT_REMOTE_S = 60
# 探测读多少：文件头里就有各路流（mp4 的 moov 再大 ffmpeg 也会整个读，不受这个限制）。
PROBE_ARGS = ("-probesize", "5000000", "-analyzeduration", "5000000")
# ffprobe 要的各项（color_transfer、color_primaries：HDR 的片子在小电视上要色调映射，见 tonemap_for）。
PROBE_ENTRIES = ("format=format_name,duration,start_time:stream=index,codec_type,codec_name,profile,pix_fmt,"
                 "width,height,channels,color_transfer,color_primaries:stream_disposition=default,forced,attached_pic:"
                 "stream_tags=language,title")
# 探测失败了（网盘超时之类）这么久以内不再探测同一个，照扩展名猜着放。
PROBE_RETRY_S = 600.0
# 浏览器直接放（按 Range 取原文件）只认这些容器；第一路声音要是浏览器解得了的（Chrome 只放第一路）。
DIRECT_CONTAINERS = frozenset({".mp4", ".m4v", ".mov"})
DIRECT_AUDIO = frozenset({"aac", "mp3"})
# 这些容器里的 AAC 带 ADTS 头，只换封装的话初始化段里没有 AudioSpecificConfig（watch.py 的说明），要转码。
ADTS_CONTAINERS = frozenset({".ts", ".m2ts", ".mts"})
# 文字字幕（能转成 WebVTT）；别的（PGS、DVD 的图片字幕）列出来但放不了。
TEXT_SUBS = frozenset({"ass", "ssa", "subrip", "srt", "mov_text", "webvtt", "text"})
# 视频旁边的字幕文件：<名字>.srt、<名字>.zh.srt、<名字>.简体.ass……
SIDE_SUBS = {".srt": "subrip", ".ass": "ass", ".ssa": "ass", ".vtt": "webvtt"}
SIDE_SUBS_MAX = 12
SUB_FILE_MAX = 4 * 1024 * 1024
# 转好的 WebVTT 至多这么大、这么多条；缓存在 library-subs/，总共至多 SUBS_CACHE_MAX，超了删最旧的。
VTT_MAX = 8 * 1024 * 1024
CUES_MAX = 20000
SUBS_CACHE_MAX = 64 * 1024 * 1024
# 本机文件的内嵌字幕整个抽出来最多等这么久（要把文件读一遍；网盘上的不整个抽，边放边抽，见 watch_source）。
SUB_EXTRACT_S = 300
# 给中转自己的 ffmpeg 的地址（/api/library/raw/<id>?t=…）的令牌有效期：看得再久也够；过期了 ffmpeg 重连失败，
# 播放器从现在的位置重开一路（新的令牌）。
RAW_LIFETIME_S = 6 * 3600
# 经前端（server/front.py）转进来的请求一定带这些头之一：带了就不是本机的 ffmpeg。
FORWARDED_HEADERS = ("X-Real-IP", "X-Forwarded-For", "X-Forwarded-Proto", "CF-Connecting-IP", "Forwarded")
# 页面说的它能放什么（MediaSource 能不能接 HEVC、<video> 能不能直接放 HEVC），没说的当不能。
CAPS = ("hevc", "hevc10", "direct_hevc", "direct_hevc10")
_LANGS = {"chi": "中文", "zho": "中文", "zh": "中文", "chs": "简体中文", "sc": "简体中文", "zh-cn": "简体中文",
          "zh-hans": "简体中文", "gb": "简体中文", "cht": "繁体中文", "tc": "繁体中文", "zh-tw": "繁体中文",
          "zh-hk": "繁体中文", "zh-hant": "繁体中文", "big5": "繁体中文", "eng": "英文", "en": "英文", "jpn": "日文",
          "ja": "日文", "jp": "日文", "kor": "韩文", "ko": "韩文", "fre": "法文", "fra": "法文", "fr": "法文",
          "ger": "德文", "deu": "德文", "de": "德文", "spa": "西班牙文", "es": "西班牙文", "rus": "俄文", "ru": "俄文",
          "ita": "意大利文", "it": "意大利文", "por": "葡萄牙文", "pt": "葡萄牙文", "tha": "泰文", "th": "泰文",
          "und": ""}
_STREAM = re.compile(r"^\s*Stream #0:(\d+)(?:\[[^\]]*\])?(?:\(([^)]*)\))?: (Video|Audio|Subtitle|Attachment|Data): "
                     r"([A-Za-z0-9_]+)(?: \(([^)]*)\))?(.*)$")
_DURATION = re.compile(r"Duration: (?:(\d+):(\d+):(\d+(?:\.\d+)?)|N/A)(?:, start: (-?\d+(?:\.\d+)?))?")
_CHANNEL_WORDS = {"mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "quad": 4, "4.0": 4, "4.1": 5, "5.0": 5, "5.0(side)": 5,
                  "5.1": 6, "5.1(side)": 6, "6.0": 6, "6.1": 7, "7.0": 7, "7.1": 8, "7.1(wide)": 8}


def _stream_row(index: int, kind: str, codec: str, profile: str = "", pix: str = "", width: int = 0, height: int = 0,
                channels: int = 0, lang: str = "", title: str = "", default: bool = False, forced: bool = False,
                pic: bool = False, trc: str = "", prim: str = "") -> dict:
    """索引里的一路流（只留用得上的几项，空的不写）：i 序号、t 种类、c 编码、p profile、pix、w / h、ch 声道、
    lang、title、def 默认、forced 强制、pic 封面图、trc 传输特性（smpte2084 是 HDR10 / 杜比视界 8.1 的底层，
    arib-std-b67 是 HLG）、prim 原色。"""
    row: dict = {"i": int(index), "t": kind, "c": (codec or "").lower()[:32]}
    trc, prim = (x if x not in ("unknown", "unspecified", "reserved") else "" for x in (trc, prim))
    for key, value in (("p", profile), ("pix", pix), ("lang", lang), ("title", title), ("trc", trc), ("prim", prim)):
        if value:
            row[key] = str(value)[:80]
    for key, value in (("w", width), ("h", height), ("ch", channels)):
        if value:
            row[key] = int(value)
    for key, value in (("def", default), ("forced", forced), ("pic", pic)):
        if value:
            row[key] = True
    return row


def parse_probe_json(data: bytes | str) -> dict | None:
    """ffprobe -of json 的输出 → {dur, start, fmt, streams}；看不懂时 None。附件（字体）和数据流不记。"""
    try:
        found = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(found, dict) or not isinstance(found.get("streams"), list):
        return None
    fmt = found.get("format") if isinstance(found.get("format"), dict) else {}
    streams = []
    for s in found["streams"]:
        if not isinstance(s, dict) or s.get("codec_type") not in ("video", "audio", "subtitle"):
            continue
        tags = s.get("tags") if isinstance(s.get("tags"), dict) else {}
        flags = s.get("disposition") if isinstance(s.get("disposition"), dict) else {}
        try:
            streams.append(_stream_row(int(s.get("index")), s["codec_type"], str(s.get("codec_name") or ""),
                                       str(s.get("profile") or ""), str(s.get("pix_fmt") or ""),
                                       int(s.get("width") or 0), int(s.get("height") or 0), int(s.get("channels") or 0),
                                       str(tags.get("language") or ""), str(tags.get("title") or ""),
                                       bool(flags.get("default")), bool(flags.get("forced")),
                                       bool(flags.get("attached_pic")), str(s.get("color_transfer") or ""),
                                       str(s.get("color_primaries") or "")))
        except (TypeError, ValueError):
            continue
    return {"dur": _float(fmt.get("duration")), "start": _float(fmt.get("start_time")),
            "fmt": str(fmt.get("format_name") or "")[:40], "streams": streams}


def _float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def parse_ffmpeg_dump(text: str) -> dict | None:
    """没有 ffprobe 时（电脑客户端下载的 ffmpeg 只有它自己）：`ffmpeg -i 地址` 在 stderr 里打的输入信息 → 和
    parse_probe_json 一样的 {dur, start, fmt, streams}；没有「Input #0」时 None。"""
    lines = text.splitlines()
    head = next((i for i, line in enumerate(lines) if line.startswith("Input #0")), None)
    if head is None:
        return None
    fmt = re.match(r"Input #0, (.+?), from ", lines[head])
    info = {"dur": 0.0, "start": 0.0, "fmt": (fmt.group(1) if fmt else "")[:40], "streams": []}
    current = None
    for line in lines[head + 1:]:
        if line.startswith(("Output #", "Stream mapping:", "Input #1")) or not line.startswith(" "):
            break
        duration = _DURATION.search(line)
        if duration and current is None:
            if duration.group(1) is not None:
                info["dur"] = int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 + float(duration.group(3))
            info["start"] = _float(duration.group(4))
            continue
        match = _STREAM.match(line)
        if match:
            current = None
            kind = match.group(3).lower()
            if kind not in ("video", "audio", "subtitle"):
                continue
            rest = match.group(6) or ""
            extra = match.group(5) or ""
            profile = "" if " / 0x" in extra else extra       # 「(hev1 / 0x31766568)」是封装的标签，不是 profile
            pix = re.search(r",\s*((?:yuv|yuvj|gray|rgb|bgr|nv|p0)[a-z0-9]*)(?:\(([^)]*)\))?", rest)
            trc = prim = ""
            # 「yuv420p10le(tv, bt2020nc/bt2020/smpte2084, progressive)」：矩阵 / 原色 / 传输特性；三个一样时只写一个
            # （「yuv420p(tv, bt709)」）。
            for word in (w.strip() for w in ((pix.group(2) or "") if pix else "").split(",")):
                if word.count("/") == 2:
                    _, prim, trc = word.split("/")
                elif re.fullmatch(r"[a-z0-9-]+", word) and word not in ("tv", "pc", "progressive"):
                    prim = trc = word
            size = re.search(r",\s*(\d{2,5})x(\d{2,5})", rest)
            channels = 0
            if kind == "audio":
                found = re.search(r"Hz, ([^,]+)", rest)
                word = (found.group(1).strip() if found else "")
                count = re.match(r"(\d+) channels", word)
                channels = int(count.group(1)) if count else _CHANNEL_WORDS.get(word, 0)
            current = _stream_row(int(match.group(1)), kind, match.group(4),
                                  profile if kind != "subtitle" else "", pix.group(1) if pix else "",
                                  int(size.group(1)) if size else 0, int(size.group(2)) if size else 0, channels,
                                  match.group(2) or "", "", "(default)" in rest, "(forced)" in rest,
                                  "(attached pic)" in rest, trc if kind == "video" else "", prim if kind == "video" else "")
            info["streams"].append(current)
            continue
        title = re.match(r"^\s+title\s*:\s*(.+)$", line)
        if title and current is not None and "title" not in current:
            current["title"] = title.group(1).strip()[:80]
    return info


def caps_of(given) -> dict:
    """页面报的能力（POST /api/watch 的 caps）→ {hevc, hevc10, direct_hevc, direct_hevc10}，没说的为 False。"""
    given = given if isinstance(given, dict) else {}
    return {key: given.get(key) is True for key in CAPS}


def _deep(stream: dict) -> bool:
    """10 位（或 4:2:2 / 4:4:4）的画面：浏览器多半放不了 H.264 的这种（Hi10P 之类）。"""
    profile, pix = stream.get("p") or "", stream.get("pix") or ""
    return any(x in profile for x in ("10", "12", "4:2:2", "4:4:4")) or any(x in pix for x in ("10", "12", "422", "444"))


def video_ok(stream: dict, caps: dict, direct: bool) -> bool:
    """这一路画面浏览器能不能不转码就放：direct 是 <video> 直接放文件，否则是 MediaSource（分片 MP4）。
    H.264 8 位的都能；HEVC 按页面报的（Main 要 hevc，Main 10 要 hevc10；不知道 profile 的按 Main 10 算）；
    别的（MPEG-2、VC-1、RealVideo、AV1、VP9……）都转码。"""
    codec = stream.get("c")
    if codec == "h264":
        return not _deep(stream)
    if codec == "hevc":
        profile = stream.get("p") or ""
        if profile and profile not in ("Main", "Main 10"):
            return False                     # Rext、Main 4:4:4 之类
        main8 = profile == "Main" and not _deep(stream)
        return bool(caps_of(caps).get(("direct_" if direct else "") + ("hevc" if main8 else "hevc10")))
    return False


def first_video(media: dict | None) -> dict | None:
    """第一路真画面（封面图——mkv 附带的 mjpeg、mp4 的 attached pic——不算）。"""
    return next((s for s in (media or {}).get("streams") or [] if s.get("t") == "video" and not s.get("pic")), None)


def audio_choice(media: dict | None, ext: str) -> tuple[dict | None, bool]:
    """放哪一路声音、能不能直接复制：有 AAC 的挑 AAC（默认的那路优先，否则第一路 AAC），直接复制（ADTS 的容器除外）；
    没有时挑默认的那一路（没有默认的就第一路），转成 AAC 立体声（很便宜）。没有声音时 (None, False)。"""
    audios = [s for s in (media or {}).get("streams") or [] if s.get("t") == "audio"]
    if not audios:
        return None, False
    aac = [s for s in audios if s.get("c") == "aac"]
    if aac:
        return next((s for s in aac if s.get("def")), aac[0]), ext not in ADTS_CONTAINERS
    return next((s for s in audios if s.get("def")), audios[0]), False


def plan(media: dict | None, ext: str, caps=None, remote: bool = False, remux: bool = False) -> dict:
    """这一次在电脑上怎么放（按探测的结果和页面报的能力）：
    - how "direct"：浏览器按 Range 直接放原文件（不起 ffmpeg）。只在容器是 mp4 / m4v / mov、画面浏览器放得了、
      第一路声音是 AAC / MP3（或者没有声音）时；网盘上的文件有内嵌文字字幕时也不直接放（字幕要边放边抽）。
    - how "remux"：经 watch.py 封装成分片 MP4：video / audio 是要的那两路的序号，vcopy 画面只换封装（否则转成
      H.264），tag 是 "hvc1"（HEVC 只换封装时），acopy 声音直接复制（否则转成 AAC）。字幕、附件从不放进去。
    - how "none"：文件里没有能放的画面和声音。
    探测失败（media 为 None）时照扩展名猜：mp4 这类直接放，别的按通用的办法转（第一路画面、第一路声音，
    H.264 只换封装、别的转码）。remux 为真：不直接放（直接放出了错，页面要求换一种放法）。"""
    caps = caps_of(caps)
    streams = (media or {}).get("streams") or []
    if not streams:
        if ext in DIRECT_CONTAINERS and not remux:
            return {"how": "direct", "note": "没探测到，照扩展名直接放"}
        return {"how": "remux", "video": None, "audio": None, "vcodec": "", "vcopy": False, "acopy": False, "tag": "",
                "generic": True, "note": "没探测到，按通用的办法转"}
    video = first_video(media)
    audio, acopy = audio_choice(media, ext)
    if video is None and audio is None:
        return {"how": "none", "note": "这个文件里没有能放的画面和声音"}
    first_audio = next((s for s in streams if s.get("t") == "audio"), None)
    text_subs = any(s.get("t") == "subtitle" and s.get("c") in TEXT_SUBS for s in streams)
    if not remux and ext in DIRECT_CONTAINERS and (video is None or video_ok(video, caps, True)) \
            and (first_audio is None or first_audio.get("c") in DIRECT_AUDIO) and not (remote and text_subs):
        return {"how": "direct", "note": "直接放原文件"}
    vcopy = video is not None and video_ok(video, caps, False)
    note = [(f"画面 {video['c']} 只换封装" if vcopy else f"画面 {video['c']} 转成 H.264") if video else "没有画面",
            (f"声音 {audio['c']} 直接用" if acopy else f"声音 {audio['c']} 转成 AAC") if audio else "没有声音"]
    return {"how": "remux", "video": video["i"] if video else None, "audio": audio["i"] if audio else None,
            "vcodec": video["c"] if video else "", "vcopy": vcopy, "acopy": acopy,
            "tag": "hvc1" if vcopy and video["c"] == "hevc" else "", "note": "，".join(note)}


def _lang_label(code: str) -> str:
    code = (code or "").strip()
    return _LANGS.get(code.casefold(), code)


def side_lang(video_rel: str, sub_rel: str) -> str:
    """外挂字幕文件名里视频名字和扩展名中间的那一段（「电影.zh.srt」→ zh，「电影.简体.ass」→ 简体），没有为空。"""
    stem = os.path.splitext(video_rel.rsplit("/", 1)[-1])[0]
    name = os.path.splitext(sub_rel.rsplit("/", 1)[-1])[0]
    middle = name[len(stem):] if name.casefold().startswith(stem.casefold()) else ""
    return middle.strip(" ._-")[:20]


def subtitle_tracks(item: dict, media: dict | None) -> list[dict]:
    """这个视频的字幕：内嵌的（按流的顺序）在前，外挂文件在后。每条 {n, kind: embedded | file, stream / path,
    codec, lang, title, def, forced, ok（文字字幕才能放）}。n 就是 /api/library/subs/<id>/<n>.vtt 的 n。"""
    rows = []
    for s in (media or {}).get("streams") or []:
        if s.get("t") == "subtitle":
            rows.append({"kind": "embedded", "stream": s["i"], "codec": s.get("c") or "", "lang": s.get("lang") or "",
                         "title": s.get("title") or "", "def": bool(s.get("def")), "forced": bool(s.get("forced")),
                         "ok": s.get("c") in TEXT_SUBS})
    for rel in item.get("subs") or []:
        if isinstance(rel, str):
            rows.append({"kind": "file", "path": rel, "codec": SIDE_SUBS.get(_ext(rel), ""),
                         "lang": side_lang(item.get("path") or "", rel), "title": "", "def": False, "forced": False,
                         "ok": _ext(rel) in SIDE_SUBS})
    for n, row in enumerate(rows):
        row["n"] = n
    return rows


def subtitle_label(row: dict) -> str:
    """菜单里的一项：标题（没有时语言）、外挂 / 强制 / 放不了。"""
    lang = _lang_label(row.get("lang") or "")
    label = row.get("title") or lang or f"字幕 {row['n'] + 1}"
    if row.get("title") and lang and lang not in label:
        label += f" · {lang}"
    if row["kind"] == "file":
        label += "（外挂）"
    if row.get("forced"):
        label += "（强制）"
    if not row.get("ok"):
        label += "（图片字幕，放不了）"
    return label[:60]


# ---- WebVTT ----

_CUE_TIME = re.compile(r"^\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})(.*)$")


def _seconds_of(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def _stamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    return f"{millis // 3600000:02d}:{millis // 60000 % 60:02d}:{millis // 1000 % 60:02d}.{millis % 1000:03d}"


def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    """WebVTT（或 SRT：逗号的毫秒、编号行都认）→ [(起, 止, 文字)]；NOTE、STYLE、REGION 块跳过，位置设置不要。"""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.replace("﻿", "")):
        lines = block.strip("\r\n").splitlines()
        at = next((i for i, line in enumerate(lines[:3]) if "-->" in line), None)
        if at is None:
            continue
        match = _CUE_TIME.match(lines[at])
        if match is None:
            continue
        body = "\n".join(line.rstrip() for line in lines[at + 1:]).strip()
        if body:
            cues.append((_seconds_of(match.group(1)), _seconds_of(match.group(2)), body))
        if len(cues) >= CUES_MAX:
            break
    return cues


def write_vtt(cues) -> str:
    out = ["WEBVTT", ""]
    for start, end, body in cues:
        out += [f"{_stamp(start)} --> {_stamp(end)}", body, ""]
    return "\n".join(out) + "\n"


_ASS_TAG = re.compile(r"\{[^}]*\}")
# 画图模式（{\p1} 到 {\p0} 之间是矢量图形的命令，不是字）。
_ASS_DRAWING = re.compile(r"\{[^}]*\\p[1-9][^}]*\}.*?(?:\{[^}]*\\p0[^}]*\}|$)")


def ass_to_cues(text: str) -> list[tuple[float, float, str]]:
    """ASS / SSA 的 [Events] → [(起, 止, 文字)]：按 Format 行找 Start、End、Text，去掉 {\\…} 样式，\\N 换行。
    画图（\\p1）的那几行没有字，去掉样式后是空的，跳过。"""
    cues, fields = [], None
    for line in text.replace("﻿", "").splitlines():
        line = line.strip()
        if line.lower().startswith("format:") and fields is None:
            fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
            continue
        if not line.lower().startswith("dialogue:") or not fields or "text" not in fields:
            continue
        values = line.split(":", 1)[1].split(",", len(fields) - 1)
        if len(values) < len(fields):
            continue
        row = dict(zip(fields, (v.strip() for v in values)))
        body = _ASS_TAG.sub("", _ASS_DRAWING.sub("", row.get("text", ""))).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        body = "\n".join(part.strip() for part in body.splitlines() if part.strip())
        try:
            start, end = _seconds_of(row.get("start", "")), _seconds_of(row.get("end", ""))
        except ValueError:
            continue
        if body and end > start:
            cues.append((start, end, body.replace("<", "&lt;").replace(">", "&gt;")))
        if len(cues) >= CUES_MAX:
            break
    cues.sort(key=lambda c: (c[0], c[1]))
    return cues


def decode_text(data: bytes) -> str:
    """字幕文件的字：有 BOM 按 BOM，UTF-8 读得通就是 UTF-8，否则当 GB18030（GBK 的超集，中文字幕常见）。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gb18030", "replace")


def sidecar_vtt(data: bytes, ext: str) -> str:
    """外挂字幕文件（.srt、.ass、.ssa、.vtt）→ WebVTT 的字（只用标准库，不起 ffmpeg）。"""
    text = decode_text(data)
    cues = ass_to_cues(text) if ext in (".ass", ".ssa") else parse_vtt(text)
    return write_vtt(cues)


def _ffprobe_of(ffmpeg: str) -> str | None:
    """和 ffmpeg 放在一起的 ffprobe；ffmpeg 是命令名（在 PATH 里找）时也在 PATH 里找。电脑客户端下载的 ffmpeg
    （tools/ffmpeg_fetch.py）没有 ffprobe：None，改用 ffmpeg 自己打的输入信息。"""
    if not ffmpeg:
        return None
    if os.sep not in ffmpeg and "/" not in ffmpeg:
        return shutil.which("ffprobe")
    sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if ffmpeg.lower().endswith(".exe") else ""))
    return sibling if os.path.isfile(sibling) and os.access(sibling, os.X_OK) else None


def raw_secret(key: str) -> bytes:
    """/api/library/raw 的令牌密钥：从中转的密钥派生（和「在电脑上看」「用手机听」的都不同）。"""
    return hashlib.sha256(b"ai-passport-tv library raw v1\0" + key.strip().encode("utf-8")).digest()


# ---------------------------------------------------------------- 片库

class Library:
    """来源、索引、扫描、放文件和海报。data_dir 是数据目录（数据库旁边）。"""

    def __init__(self, data_dir: Path, log: Callable[[str], None] = print, clock=time.time, background: bool = True,
                 opener=None):
        self.dir = Path(data_dir)
        self.sources_path = self.dir / "library.json"
        self.index_path = self.dir / "library-index.json"
        self.art_dir = self.dir / "library-art"
        self.log = log
        self.clock = clock
        self.background = background
        self.opener = opener
        # 测试里不歇（真用时 LocalFolder / WebDav 的 pace_s）。
        self.pace = True
        self._items: dict[str, dict] = self._load_index()
        self._queue: deque[str] = deque()
        self._worker: threading.Thread | None = None
        self._status = {"running": False, "source": "", "name": "", "walked": 0, "videos": 0, "error": "",
                        "finished_at": 0}
        self._files = threading.BoundedSemaphore(FILES_AT_ONCE)
        self._art_lock = threading.Semaphore(2)
        self._art_failed: dict[str, float] = {}
        # 第二步：探测、给中转自己的 ffmpeg 的地址（wire 设好）、字幕。
        self.subs_dir = self.dir / "library-subs"
        self.ffmpeg = ""
        self.secret: bytes | None = None
        self.port: Callable[[], int] | None = None
        self._probing: dict[str, threading.Lock] = {}
        self._probe_failed: dict[str, float] = {}
        self._color_failed: dict[str, float] = {}
        self._read: dict[str, int] = {}
        # 每个文件最近一次从网盘读失败的时刻（time.monotonic）：小电视上一集「提前放完」时据此分清是断了还是放完了。
        self._drive_failed: dict[str, float] = {}
        self._sub_lock = threading.Lock()
        self._partial: dict[tuple, dict] = {}
        # 网盘组件的来源：列每个目录之前先让组件重新向网盘要一次（clouddrive.attach 设成 clouddrive.refresh_dir）。
        # 组件把目录列表缓存半小时，不刷新的话刚放进网盘的片子要过半小时才扫得到。
        self.refresh_dir: Callable[[str], None] | None = None
        # 网盘文件的索引区（MP4 的 moov、MKV 的头和 Cues）：只给 ffmpeg 读的 raw 用（librarycache.py）。
        self.cache = IndexCache(self.dir / "library-cache", alive=self._now_of, log=log, clock=clock)
        # 正在看的网盘文件边看边整个下到本机（libraryfull.py）：上限是片库设置的 full_gb，0 是关。
        self.full = FullCache(self.dir / "library-full", opener=self._full_open, alive=self._now_of, log=log,
                              clock=clock, limit=self.full_gb() * libraryfull.GB, background=background)
        # 在线资料（libmeta.py）：存在 library-meta.json；豆瓣一次一个请求，被拦了歇一个小时。
        self.meta = libmeta.MetaStore(self.dir / "library-meta.json")
        self.douban = libmeta.Douban(clock=clock, log=log)
        # 备用的 TMDB：密钥在数据目录的 secrets/tmdb-key（600）；代理由 attach 接上「设置 → YouTube」的。
        self.tmdb = libmeta.Tmdb(self.dir / "secrets", clock=clock, log=log)
        self._meta_wake = threading.Event()
        self._meta_failed: dict[str, float] = {}
        self._meta_lock = threading.Lock()
        # 预热（warm_step）：正在给出去的片库文件（不算预热自己的探测）、最后一次给完的时间；有人在看（attach 设成
        # 看 relay.watch 的）；要预热的条目、每个文件失败了几次、歇到什么时候。
        self._serving = 0
        self._served_at = 0.0
        self.watching: Callable[[], bool] | None = None
        self._warm_wanted: set[str] = set()
        self._warm_seeded = False
        self._warm_tries: dict[str, int] = {}
        self._warm_after = 0.0
        self._warm_backoff = 0.0
        self._warm_read = 0
        self._warm_wake = threading.Event()
        self._warm_lock = threading.Lock()

    # ---- 文件 ----

    def _load_sources(self) -> list[dict]:
        try:
            data = json.loads(self.sources_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found = data.get("sources") if isinstance(data, dict) else None
        return [s for s in found if isinstance(s, dict) and _SOURCE_ID.match(str(s.get("id", "")))
                and s.get("kind") in ("local", "webdav")] if isinstance(found, list) else []

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _save_sources(self, sources: list[dict]) -> None:
        keep = {k: v for k, v in self._load_config().items() if k in ("online", "full_gb")}
        self._write(self.sources_path, {**keep, "sources": sources})

    def _load_config(self) -> dict:
        try:
            data = json.loads(self.sources_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def online(self) -> bool:
        """在线资料（豆瓣）开着没有：默认开；关了以后一个请求也不往豆瓣发（存下来的照旧显示）。"""
        return self._load_config().get("online", True) is not False

    def set_online(self, on: bool) -> dict:
        with _LOCK:
            config = self._load_config()
            config["online"] = bool(on)
            config.setdefault("sources", [])
            self._write(self.sources_path, config)
        self.log(f"片库的在线资料{'开了' if on else '关了'}")
        if on:
            self._meta_wake.set()
        return {"ok": True, "online": bool(on)}

    def full_gb(self) -> int:
        """整集缓存总共至多多少 GB（libraryfull.CHOICES 之一，0 是关；默认 DEFAULT_GB）。"""
        found = self._load_config().get("full_gb")
        return found if type(found) is int and found in libraryfull.CHOICES else libraryfull.DEFAULT_GB

    def set_full_gb(self, gb) -> dict:
        """管理员改整集缓存的上限：关（0）时下载停下、已经存下的全删；改小了按最后用过的时间删到不超。"""
        if type(gb) is not int or gb not in libraryfull.CHOICES:
            return {"ok": False, "error": "只能选关、" + "、".join(f"{n} GB" for n in libraryfull.CHOICES if n)}
        with _LOCK:
            config = self._load_config()
            config["full_gb"] = gb
            config.setdefault("sources", [])
            self._write(self.sources_path, config)
        self.full.set_limit(gb * libraryfull.GB)
        self.log(f"片库的整集缓存{'改成了 ' + str(gb) + ' GB' if gb else '关了，已经缓存的删掉了'}")
        return {"ok": True, "full_gb": gb, "full_used": self.full.total()}

    def close(self) -> None:
        """中转关掉时：整集缓存的下载线程停下。"""
        self.full.stop()

    def _load_index(self) -> dict[str, dict]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data.get("items") if isinstance(data, dict) else None
        return {i["id"]: i for i in items if isinstance(i, dict) and _ID.match(str(i.get("id", "")))
                and isinstance(i.get("path"), str) and isinstance(i.get("source"), str)} \
            if isinstance(items, list) else {}

    def _save_index(self) -> None:
        self._write(self.index_path, {"items": list(self._items.values())})

    def _now_of(self, iid: str) -> tuple | None:
        """索引缓存问的：这个条目现在的 (大小, 修改时间)；不在索引里（删了、扫描后不见了）时 None。"""
        with _LOCK:
            item = self._items.get(iid)
            return (item.get("size") or 0, item.get("mtime") or 0) if item is not None else None

    def _update_source(self, sid: str, change: Callable[[dict], None]) -> dict | None:
        with _LOCK:
            sources = self._load_sources()
            found = next((s for s in sources if s["id"] == sid), None)
            if found is None:
                return None
            change(found)
            self._save_sources(sources)
            return dict(found)

    def _source(self, src: dict):
        if src["kind"] == "local":
            return LocalFolder(src.get("path") or "")
        return WebDav(src.get("url") or "", src.get("user") or "", src.get("password") or "", opener=self.opener)

    # ---- 给页面 ----

    @staticmethod
    def _public_source(src: dict, count: int) -> dict:
        return {"id": src["id"], "name": src.get("name") or "", "kind": src["kind"], "count": count,
                "last_scan": src.get("last_scan") or 0, "ok": src.get("ok"), "error": src.get("error") or "",
                "note": src.get("note") or "", "managed": src.get("managed") == MANAGED}

    def _counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with _LOCK:
            for item in self._items.values():
                counts[item["source"]] = counts.get(item["source"], 0) + 1
        return counts

    def status(self) -> dict:
        with _LOCK:
            return dict(self._status, queued=len(self._queue))

    def admin_view(self) -> dict:
        """管理员看的来源：多给路径 / 地址和用户名；密码从不给，只说 has_password。"""
        counts = self._counts()
        rows = []
        for src in self._load_sources():
            row = self._public_source(src, counts.get(src["id"], 0))
            if src["kind"] == "local":
                row["path"] = src.get("path") or ""
            else:
                row.update(url=src.get("url") or "", user=src.get("user") or "", has_password=bool(src.get("password")))
            rows.append(row)
        return {"ok": True, "max": SOURCES_MAX, "every_h": int(EVERY_S // 3600), "sources": rows, "scan": self.status(),
                "online": self.online(), "tmdb": self.tmdb.state(), "full_gb": self.full_gb(),
                "full_used": self.full.total()}

    def item(self, iid: str) -> dict | None:
        if not isinstance(iid, str) or not _ID.match(iid):
            return None
        with _LOCK:
            found = self._items.get(iid)
            return dict(found) if found is not None else None

    def items(self) -> list[dict]:
        with _LOCK:
            return [dict(i) for i in self._items.values()]

    @staticmethod
    def playable(item: dict) -> bool:
        """能不能在电脑上放：视频都能（直接放、只换封装或转码），只有探测过、里面既没有画面也没有声音的不能。"""
        media = item.get("media")
        if isinstance(media, dict) and media.get("streams") is not None:
            return plan(media, item.get("ext") or "")["how"] != "none"
        return (item.get("ext") or "") in VIDEO_TYPES

    @classmethod
    def card(cls, item: dict, source_name: str = "") -> dict:
        """页面上的一张卡片（没有路径、没有地址）。"""
        return {"id": item["id"], "title": item.get("title") or "", "year": item.get("year"), "kind": item["kind"],
                "show": item.get("show") or "", "season": item.get("season"), "episode": item.get("episode"),
                "playable": cls.playable(item), "ext": (item.get("ext") or "").lstrip("."),
                "size": item.get("size") or 0, "plot": (item.get("plot") or "")[:300],
                "art": f"api/library/art/{item['id']}" if item.get("poster") else "",
                "source": source_name, "added": item.get("added") or 0}

    @staticmethod
    def show_key(name: str) -> str:
        return hashlib.sha1(" ".join(name.casefold().split()).encode("utf-8")).hexdigest()[:12]

    def episodes_of(self, item: dict) -> list[dict]:
        """和 item 同一部剧的各集，按季、集、路径排好。"""
        if item.get("kind") != "episode":
            return []
        key = self.show_key(item.get("show") or "")
        with _LOCK:
            found = [dict(i) for i in self._items.values()
                     if i.get("kind") == "episode" and self.show_key(i.get("show") or "") == key]
        return sorted(found, key=lambda i: (i.get("season") or 0, i.get("episode") or 0, i["path"]))

    def view(self) -> dict:
        """GET /api/library：来源（没有地址）、扫描状态，电影、剧（带各集）、其他视频。"""
        sources = self._load_sources()
        names = {s["id"]: s.get("name") or "" for s in sources}
        counts = self._counts()
        items = sorted(self.items(), key=lambda i: (-(i.get("added") or 0), i["path"]))
        more = len(items) > VIEW_MAX
        items = items[:VIEW_MAX]
        movies, videos, shows = [], [], {}
        entries = self.meta.all()
        cached = self.full.fractions()
        for item in items:
            card = self.card(item, names.get(item["source"], ""))
            part = cached.get(FullCache.key_of_item(item))
            if part:
                card["cached"] = math.floor(part * 1000) / 1000     # 整集缓存下好了多少（0..1，没下完不会是 1）
            if item["kind"] == "episode":
                key = self.show_key(item.get("show") or "")
                show = shows.setdefault(key, {"id": key, "title": item.get("show") or "", "count": 0, "art": "",
                                              "seasons": set(), "added": 0, "episodes": [], "year": None,
                                              "guess": item.get("show") or "", "nfo": False})
                show["episodes"].append(card)
                show["count"] += 1
                show["seasons"].add(item.get("season") or 1)
                show["added"] = max(show["added"], card["added"])
                show["year"] = show["year"] or item.get("year")
                show["nfo"] = show["nfo"] or bool(item.get("nfo_show"))
                continue
            entry = self._dress(card, item, entries.get("m" + item["id"]))
            if item["kind"] == "movie" or (entry or {}).get("state") == "matched":
                movies.append(card)
            else:
                videos.append(card)
        show_rows = []
        for key, show in shows.items():
            show["episodes"].sort(key=lambda c: (c["season"] or 0, c["episode"] or 0, c["title"]))
            show["art"] = next((c["art"] for c in show["episodes"] if c["art"]), "")
            show["seasons"] = len(show["seasons"])
            show["playable"] = any(c["playable"] for c in show["episodes"])
            entry = entries.get("s" + key)
            show["key"] = "s" + key
            show["meta"] = libmeta.public(entry)
            if entry and entry.get("state") == "matched":
                if not show["nfo"]:
                    show["title"] = entry.get("title") or show["title"]
                show["year"] = entry.get("year") or show["year"]
                if not show["art"] and (entry.get("poster") or entry.get("img")):
                    show["art"] = self._meta_art_url("s" + key, entry)
            del show["nfo"]
            show_rows.append(show)
        # 新加进来的在前（剧按最新加进来的那一集），一样新的按名字。
        movies.sort(key=lambda c: (-(c["added"] or 0), c["title"].casefold()))
        videos.sort(key=lambda c: (-(c["added"] or 0), c["title"].casefold()))
        show_rows.sort(key=lambda s: (-(s["added"] or 0), s["title"].casefold()))
        return {"ok": True, "sources": [self._public_source(s, counts.get(s["id"], 0)) for s in sources],
                "scan": self.status(), "movies": movies, "shows": show_rows, "videos": videos,
                "total": sum(counts.values()), "more": more, "online": self.online(),
                "full_gb": self.full_gb(), "full_used": self.full.total(),
                "pending": len(self.meta_due()) if self._meta_can() else 0}

    def recent(self, limit: int = RECENT_MAX) -> list[dict]:
        """首页「我的片库」：最近加进来的（剧只放一张，按剧）。"""
        rows, seen = [], set()
        for item in sorted(self.items(), key=lambda i: (-(i.get("added") or 0), i["path"])):
            if item["kind"] == "episode":
                key = self.show_key(item.get("show") or "")
                if key in seen:
                    continue
                seen.add(key)
            rows.append(item)
            if len(rows) >= limit:
                break
        return rows

    # ---- 在线资料（libmeta.py，豆瓣；备用的 TMDB） ----

    @staticmethod
    def _meta_art_url(key: str, entry: dict) -> str:
        return f"api/library/meta-art/{key}?v={'t' if entry.get('src') == 'tmdb' else ''}{entry.get('id') or ''}"

    def _meta_can(self) -> bool:
        """后台现在能往外查：豆瓣没拦着，或者 TMDB 能用。"""
        return not self.douban.blocked() or self.tmdb.on()

    def set_tmdb_key(self, text) -> dict:
        """管理员粘贴（或清掉）TMDB 的密钥：存进 secrets/tmdb-key；回答里只有「设了没有」，从不带密钥。"""
        answer = self.tmdb.set_key(text)
        if answer.get("ok") and self.tmdb.configured():
            self._meta_failed.clear()
            self._meta_wake.set()
        return answer

    def _dress(self, card: dict, item: dict, entry: dict | None) -> dict | None:
        """电影 / 其他视频的卡片加上豆瓣的资料：片名（.nfo 里写了的不换）、年份、简介和海报（本地有的优先）。"""
        card["key"] = "m" + item["id"]
        card["guess"] = item.get("title") or ""
        card["meta"] = libmeta.public(entry)
        if not entry or entry.get("state") != "matched":
            return entry
        if not item.get("nfo"):
            card["title"] = entry.get("title") or card["title"]
            card["year"] = entry.get("year") or card["year"]
        else:
            card["year"] = card["year"] or entry.get("year")
        if not card["plot"]:
            card["plot"] = (entry.get("plot") or "")[:300]
        if not card["art"] and (entry.get("poster") or entry.get("img")):
            card["art"] = self._meta_art_url(card["key"], entry)
        return entry

    def meta_key(self, item: dict) -> str:
        """资料按什么记：剧按剧名（s + 剧名的哈希，和页面上剧的 id 一样），电影和其他视频按条目 id（m + id）。"""
        if item.get("kind") == "episode":
            return "s" + self.show_key(item.get("show") or "")
        return "m" + item["id"]

    def shown(self, item: dict) -> dict:
        """首页、播放器里用的名字（剧名或片名，对上豆瓣的用豆瓣的）和海报。"""
        episode = item.get("kind") == "episode"
        key = self.meta_key(item)
        entry = self.meta.get(key)
        name = (item.get("show") if episode else item.get("title")) or ""
        art = f"api/library/art/{item['id']}" if item.get("poster") else ""
        if entry and entry.get("state") == "matched":
            if not item.get("nfo_show" if episode else "nfo"):
                name = entry.get("title") or name
            if not art and (entry.get("poster") or entry.get("img")):
                art = self._meta_art_url(key, entry)
        return {"name": name, "art": art}

    def titles(self) -> dict[str, dict]:
        """片库里的「片子」：一部剧（各集一起）、一部电影、一个其他视频。{键: {key, kind, title, alt, year, added, stale}}"""
        found: dict[str, dict] = {}
        for item in self.items():
            episode = item["kind"] == "episode"
            key = self.meta_key(item)
            row = found.get(key)
            if row is None:
                found[key] = {"key": key, "kind": "show" if episode else item["kind"],
                              "title": (item.get("show") if episode else item.get("title")) or "",
                              "alt": item.get("alt") or "", "year": item.get("year"), "added": item.get("added") or 0,
                              "stale": False}
                row = found[key]
            else:
                row["alt"] = row["alt"] or item.get("alt") or ""
                row["year"] = row["year"] or item.get("year")
                row["added"] = max(row["added"], item.get("added") or 0)
            # 还没按新写法重新解析（下次扫描时解析）：片名可能不对，先不往外查。
            row["stale"] = row["stale"] or item.get("pv") != PARSE_VERSION
        return found

    @staticmethod
    def _query(title: dict) -> str:
        return f"{title['title']}|{title['alt']}|{title['year'] or ''}|{title['kind']}"

    def meta_due(self, titles: dict[str, dict] | None = None) -> list[str]:
        """后台要查的（新加的先查）：电影和剧（其他视频多半是自己拍的，名字不自动往外发，要管理员手动匹配）。
        没查过的、自动查时的片名变了的、没对上又过了 MISS_RETRY_S 的、详细资料没取全又过了 PARTIAL_RETRY_S 的；
        TMDB 能用时还有豆瓣没对上、TMDB 还没问过的，和当时豆瓣拦着才对上 TMDB、又过了 MISS_RETRY_S 的。
        手动选的、手动「不匹配」的不动（手动选的只补没取全的详细资料）。"""
        if not self.online():
            return []
        titles = self.titles() if titles is None else titles
        entries = self.meta.all()
        now = self.clock()
        tmdb_on = self.tmdb.on()
        due = []
        for key, title in sorted(titles.items(), key=lambda kv: (-kv[1]["added"], kv[0])):
            failed = self._meta_failed.get(key)
            if (failed is not None and now - failed < META_FAIL_S) or title.get("stale"):
                continue
            auto = title["kind"] in ("show", "movie") and bool(title["title"])
            entry = entries.get(key)
            if entry is None:
                if auto:
                    due.append(key)
                continue
            age = now - (entry.get("at") or 0)
            tmdb = entry.get("src") == "tmdb"
            if entry.get("state") == "matched" and entry.get("partial"):
                if age >= libmeta.PARTIAL_RETRY_S and (not tmdb or self.tmdb.on()):
                    due.append(key)
            elif entry.get("manual"):
                continue
            elif entry.get("q") != self._query(title):
                if auto:
                    due.append(key)
            elif entry.get("state") == "miss" and age >= libmeta.MISS_RETRY_S:
                due.append(key)
            elif entry.get("state") == "miss" and not entry.get("tmdb") and tmdb_on \
                    and (not entry.get("tmdb_failed") or now - (entry.get("tmdb_at") or 0) >= libmeta.PARTIAL_RETRY_S):
                # 豆瓣答了没有、TMDB 还没问过（刚填了密钥）或上次没问成：只问 TMDB（_auto 不再问豆瓣）。
                due.append(key)
            elif tmdb and entry.get("douban_skipped") and age >= libmeta.MISS_RETRY_S and not self.douban.blocked():
                # 当时豆瓣拦着才用了 TMDB 的：过几天再问一次豆瓣（豆瓣先）。
                due.append(key)
        return due

    def meta_step(self) -> bool:
        """后台查一部（返回 True 时还有要查的，歇一会儿再来）；关着、被拦着、没有要查的 False。"""
        if not self.online() or not self._meta_can():
            return False
        titles = self.titles()
        due = self.meta_due(titles)
        if not due:
            return False
        key = due[0]
        try:
            self._lookup(key, titles[key])
        except libmeta.Blocked:
            if not self.tmdb.on():
                return False
            # 豆瓣拦着、TMDB 也没对上：这一部过一会儿再来，别的接着问 TMDB。
            self._meta_failed[key] = self.clock()
        except libmeta.Failed as error:
            self._meta_failed[key] = self.clock()
            self.log(f"片库资料没查成：{error}")
        return True

    def _lookup(self, key: str, title: dict, force: bool = False) -> dict:
        """查一部：对上了的（手动的、片名没变的、或者 force）补 / 重新取详细资料，别的按片名自动找。"""
        with self._meta_lock:
            entry = self.meta.get(key)
            retry_douban = bool(entry and entry.get("douban_skipped") and not entry.get("manual") and not force
                                and self.clock() - (entry.get("at") or 0) >= libmeta.MISS_RETRY_S)
            if entry and entry.get("state") == "matched" and not retry_douban \
                    and (entry.get("manual") or force or entry.get("q") == self._query(title)):
                return self._complete(key, entry)
            return self._auto(key, title)

    def _auto(self, key: str, title: dict) -> dict:
        """先按片名找豆瓣（_find_douban），没把握、拦着、连不上时再找 TMDB（_find_tmdb，填了密钥才有）；都没把握
        记「没对上」。豆瓣拦着（连不上）、TMDB 也没对上时把豆瓣的错抛出去（以后再来，豆瓣先）。"""
        query = self._query(title)
        before = self.meta.get(key)
        # 豆瓣不久前刚答过「没有」（同一个片名）：这次是来问 TMDB 的，不再问豆瓣。
        douban_said_no = bool(before and before.get("state") == "miss" and before.get("q") == query
                              and self.clock() - (before.get("at") or 0) < libmeta.MISS_RETRY_S)
        problem = None
        if not douban_said_no:
            try:
                chosen, details = self._find_douban(title)
            except (libmeta.Blocked, libmeta.Failed) as error:
                chosen, details, problem = None, None, error
            if chosen is not None:
                entry = self._save_match(key, chosen, query, manual=False, details=details)
                self.log(f"片库「{title['title']}」对上了豆瓣的「{entry.get('title')}」")
                return entry
        asked = failed = False
        if self.tmdb.on():
            try:
                chosen = self._find_tmdb(title)
                asked = True
            except libmeta.Failed:
                chosen, failed = None, True
            if chosen is not None:
                entry = self._save_match(key, chosen, query, manual=False, douban_skipped=problem is not None)
                self.log(f"片库「{title['title']}」对上了 TMDB 的「{entry.get('title')}」")
                return entry
        if problem is not None:
            raise problem
        entry = {"state": "miss", "manual": False, "q": query,
                 "at": int(before["at"]) if douban_said_no else int(self.clock())}
        if asked or failed:
            entry.update(tmdb=asked, tmdb_failed=failed, tmdb_at=int(self.clock()))
        self.meta.put(key, entry)
        return entry

    def _find_tmdb(self, title: dict) -> dict | None:
        """TMDB 的候选（规矩和豆瓣一样，libmeta.pick）：中文名（zh-CN）查一次，对不上再用英文名（en-US）查一次。"""
        want = {"show": "tv", "movie": "movie"}.get(title["kind"])
        if want is None:
            return None
        names = [title["title"], title["alt"]]
        chosen = None
        if title["title"]:
            chosen = libmeta.pick(self.tmdb.search(title["title"], want, title["year"]), names, title["year"], want)
        if chosen is None and title["alt"] and libmeta.norm(title["alt"]) != libmeta.norm(title["title"]):
            chosen = libmeta.pick(self.tmdb.search(title["alt"], want, title["year"], "en-US"), names, title["year"], want)
        return chosen

    def _find_douban(self, title: dict) -> tuple[dict | None, dict | None]:
        """按片名（再按英文名）找豆瓣的条目，有把握才认（libmeta.pick）：(候选, 已经取到的详细资料或 None)。"""
        want = {"show": "tv", "movie": "movie"}.get(title["kind"])
        names = [title["title"], title["alt"]]
        chosen, details = None, None
        if title["title"]:
            suggested = self.douban.suggest(title["title"])
            chosen = libmeta.pick(suggested, names, title["year"], want)
            if chosen is None and suggested:
                # subject_suggest 只给几个最热的（「龙」给的是「欢迎来龙餐馆」）：再用手机版的搜索找一次。
                chosen = libmeta.pick(self.douban.search(title["title"]), names, title["year"], want)
        if chosen is None and title["alt"] and libmeta.norm(title["alt"]) != libmeta.norm(title["title"]):
            candidates = self.douban.suggest(title["alt"])
            chosen = libmeta.pick(candidates, names, title["year"], want)
            near = [c for c in candidates if (not want or c["type"] == want) and c["year"] == title["year"]]
            if chosen is None and title["year"] and len({c["id"] for c in near}) == 1:
                # 英文名查出来、年份和种类都对的只有一个：它的又名里有这个英文名才认。
                found = self.douban.details(near[0]["id"])
                aka = {libmeta.norm(a) for a in [found.get("title"), found.get("original"), *found.get("aka", [])]}
                if libmeta.norm(title["alt"]) in aka:
                    chosen, details = near[0], found
        return chosen, details

    def _save_match(self, key: str, chosen: dict, query: str, manual: bool, details: dict | None = None,
                    douban_skipped: bool = False) -> dict:
        """认定一个条目（豆瓣的，或 src = "tmdb" 的）：取详细资料（被拦、出错时先用搜索结果里的，标 partial，以后补）
        和海报，存下。douban_skipped：当时豆瓣拦着才用的 TMDB（过几天再问豆瓣）。"""
        if chosen.get("src") == "tmdb":
            return self._save_tmdb(key, chosen, query, manual, details, douban_skipped)
        if details is None:
            try:
                details = self.douban.details(chosen["id"])
            except (libmeta.Blocked, libmeta.Failed):
                details = None
        partial = details is None or details.get("id") != chosen["id"]
        if partial:
            details = {"id": chosen["id"], "title": chosen.get("title"), "original": chosen.get("sub_title"),
                       "year": chosen.get("year"), "type": chosen.get("type"), "episodes": chosen.get("episodes"),
                       "img": libmeta.poster_url(chosen.get("img") or "")}
        elif not details.get("img") and chosen.get("img"):
            details = dict(details, img=libmeta.poster_url(chosen["img"]))
        entry = libmeta.entry_of(details, query, manual, self.clock(), partial)
        entry["poster"] = self._poster(entry)
        self.meta.put(key, entry)
        return entry

    def _save_tmdb(self, key: str, chosen: dict, query: str, manual: bool, details: dict | None,
                   douban_skipped: bool) -> dict:
        if details is None:
            try:
                details = self.tmdb.details(chosen["type"], chosen["tid"])
            except libmeta.Failed:
                details = None
        partial = details is None or details.get("id") != chosen["tid"]
        if partial:
            details = {"id": chosen["tid"], "src": "tmdb", "title": chosen.get("title"),
                       "original": chosen.get("sub_title"), "year": chosen.get("year"), "type": chosen.get("type"),
                       "img": chosen.get("img") or ""}
        elif not details.get("img") and chosen.get("img"):
            details = dict(details, img=chosen["img"])
        entry = libmeta.entry_of(details, query, manual, self.clock(), partial)
        if douban_skipped and not manual:
            entry["douban_skipped"] = True
        entry["poster"] = self._poster(entry)
        self.meta.put(key, entry)
        return entry

    def _complete(self, key: str, entry: dict) -> dict:
        """补（或重新取）一个对上了的条目的详细资料和海报（豆瓣的找豆瓣、TMDB 的找 TMDB）；手动的、自动的标记照旧。
        被拦、出错时抛出去，存的不动。"""
        if entry.get("src") == "tmdb":
            details = self.tmdb.details(entry.get("type") or "", entry["id"])
        else:
            details = self.douban.details(entry["id"])
        fresh = libmeta.entry_of(details, entry.get("q") or "", bool(entry.get("manual")), self.clock())
        if entry.get("douban_skipped"):
            fresh["douban_skipped"] = True
        if not fresh.get("img") and entry.get("img"):
            fresh["img"] = entry["img"]
        fresh["poster"] = self._poster(fresh, entry.get("poster") or "")
        self.meta.put(key, fresh)
        return fresh

    def _poster(self, entry: dict, have: str = "") -> str:
        """海报取一次存进 library-art/（豆瓣的 douban-<id>.jpg，TMDB 的 tmdb-tv-<id>.jpg）；已经有了不再取。
        取不到 ""（以后要时再取）。"""
        if have and _POSTER_FILE.match(have) and (self.art_dir / have).is_file():
            return have
        url = entry.get("img") or ""
        tmdb = entry.get("src") == "tmdb"
        if tmdb and entry.get("type") not in ("tv", "movie"):
            return ""
        stem = f"tmdb-{entry.get('type')}-{entry.get('id')}" if tmdb else f"douban-{entry.get('id')}"
        if not url or not self.online() or not re.fullmatch(r"\d{1,10}" if tmdb else r"\d{1,12}", str(entry.get("id") or "")):
            return ""
        for ext in ("jpg", "png", "webp"):
            name = f"{stem}.{ext}"
            if (self.art_dir / name).is_file():
                return name
        try:
            kind, data = self.tmdb.image(url) if tmdb else self.douban.image(url)
        except (libmeta.Blocked, libmeta.Failed):
            return ""
        name = f"{stem}.{kind}"
        try:
            self.art_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.art_dir / (name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, self.art_dir / name)
            self._prune_art()
        except OSError:
            return ""
        return name

    def run_meta(self, sleep=time.sleep) -> None:
        """后台：扫描完了（或每 META_TICK_S）把要查的一部一部查完，每部之间歇 META_GAP_S。"""
        self._meta_wake.wait(META_START_S)
        while True:
            self._meta_wake.clear()
            try:
                while self.meta_step():
                    sleep(META_GAP_S)
            except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                self.log(f"片库资料出错：{type(error).__name__}")
            self._meta_wake.wait(META_TICK_S)

    # 管理员：搜、选、不匹配、重新取。

    def _title_of(self, key) -> dict | None:
        if not isinstance(key, str) or not _META_KEY.match(key):
            return None
        return self.titles().get(key)

    def search(self, query) -> dict:
        """GET /api/admin/library/douban?q=：豆瓣的候选，填了 TMDB 的密钥时后面跟着 TMDB 的（剧、电影各搜一次，
        交错排）；每个候选标着来源（src）。小图经中转取。一边没查成时 notes 里说一句，两边都没查成才算出错。"""
        if not self.online():
            return {"ok": False, "error": "在线资料关着（在片库上面打开）"}
        query = " ".join(str(query or "").split())[:libmeta.QUERY_MAX]
        if not query:
            return {"ok": False, "error": "写一个片名"}
        notes, rows = [], []
        try:
            found = self.douban.suggest(query)
        except libmeta.Blocked:
            found, douban_error = [], "豆瓣暂时不让查了，过一个小时再试"
        except libmeta.Failed as error:
            found, douban_error = [], str(error)
        else:
            douban_error = ""
        rows += [{"id": c["id"], "src": "douban", "title": c["title"], "sub_title": c["sub_title"], "year": c["year"],
                  "type": c["type"], "episodes": c["episodes"],
                  "thumb": f"api/admin/library/douban/thumb/{c['id']}" if c["img"] else ""} for c in found]
        tmdb_error = ""
        if self.tmdb.configured():
            lists = []
            for kind in ("tv", "movie"):
                try:
                    lists.append(self.tmdb.search(query, kind))
                except libmeta.Failed as error:
                    tmdb_error = str(error)
                    break
            mixed = [c for pair in zip(*[lst + [None] * (libmeta.CANDIDATES_MAX - len(lst)) for lst in lists])
                     for c in pair if c is not None][:libmeta.CANDIDATES_MAX] if lists else []
            rows += [{"id": c["id"], "src": "tmdb", "title": c["title"], "sub_title": c["sub_title"], "year": c["year"],
                      "type": c["type"], "episodes": None,
                      "thumb": f"api/admin/library/tmdb/thumb/{c['type']}-{c['tid']}" if c["img"] else ""}
                     for c in mixed]
        if douban_error and (tmdb_error or not self.tmdb.configured()):
            return {"ok": False, "error": douban_error + ("；" + tmdb_error if tmdb_error else "")}
        if douban_error:
            notes.append(douban_error + "，下面只有 TMDB 的")
        if tmdb_error:
            notes.append(tmdb_error + "，下面只有豆瓣的")
        return {"ok": True, "candidates": rows, "notes": notes, "tmdb": self.tmdb.configured()}

    def match(self, key, did) -> dict:
        """管理员选了一个豆瓣的条目（手动的，自动的从不盖掉它）。"""
        title = self._title_of(key)
        if title is None:
            return {"ok": False, "error": "片库里没有这部片子（刷新一下）"}
        if not self.online():
            return {"ok": False, "error": "在线资料关着（在片库上面打开）"}
        did = str(did or "")
        picked = libmeta.tmdb_pick_id(did)
        if picked is not None:
            return self._match_tmdb(key, title, did, picked)
        if not re.fullmatch(r"\d{1,12}", did):
            return {"ok": False, "error": "请求格式不对"}
        chosen = self.douban.seen(did) or {"id": did}
        with self._meta_lock:
            try:
                details = self.douban.details(did)
            except (libmeta.Blocked, libmeta.Failed) as error:
                if not chosen.get("title"):
                    return {"ok": False, "error": str(error) if isinstance(error, libmeta.Failed)
                            else "豆瓣暂时不让查了，过一个小时再试"}
                details = None
            entry = self._save_match(key, chosen, self._query(title), manual=True, details=details)
        self.log(f"片库「{title['title']}」手动对上了豆瓣的「{entry.get('title')}」")
        return {"ok": True, "meta": libmeta.public(entry)}

    def _match_tmdb(self, key: str, title: dict, did: str, picked: tuple[str, str]) -> dict:
        """管理员选了一个 TMDB 的候选（「tmdb:tv:<编号>」）。"""
        if not self.tmdb.configured():
            return {"ok": False, "error": "还没填 TMDB 的密钥"}
        kind, tid = picked
        chosen = self.tmdb.seen(did) or {"id": did, "tid": tid, "type": kind, "src": "tmdb"}
        with self._meta_lock:
            try:
                details = self.tmdb.details(kind, tid)
            except libmeta.Failed as error:
                if not chosen.get("title"):
                    return {"ok": False, "error": str(error)}
                details = None
            entry = self._save_match(key, chosen, self._query(title), manual=True, details=details)
        self.log(f"片库「{title['title']}」手动对上了 TMDB 的「{entry.get('title')}」")
        return {"ok": True, "meta": libmeta.public(entry)}

    def unmatch(self, key) -> dict:
        """「不匹配」：不用豆瓣的资料，自动的也不再查这部。"""
        title = self._title_of(key)
        if title is None:
            return {"ok": False, "error": "片库里没有这部片子（刷新一下）"}
        self.meta.put(key, {"state": "none", "manual": True, "q": self._query(title), "at": int(self.clock())})
        return {"ok": True, "meta": libmeta.public(self.meta.get(key))}

    def refresh(self, key) -> dict:
        """重新取一部：对上了的重新取详细资料和海报；没对上的（包括「不匹配」）按片名重新自动找一次。"""
        title = self._title_of(key)
        if title is None:
            return {"ok": False, "error": "片库里没有这部片子（刷新一下）"}
        if not self.online():
            return {"ok": False, "error": "在线资料关着（在片库上面打开）"}
        entry = self.meta.get(key)
        if not entry or entry.get("state") != "matched":
            self.meta.drop(key)
        self._meta_failed.pop(key, None)
        try:
            entry = self._lookup(key, title, force=True)
        except libmeta.Blocked:
            return {"ok": False, "error": "豆瓣暂时不让查了，过一个小时再试"}
        except libmeta.Failed as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "meta": libmeta.public(entry)}

    def serve_meta_art(self, handler, key: str) -> None:
        """GET /api/library/meta-art/<键>：存下来的豆瓣海报；文件没了（缓存满了删掉的）在线资料开着时再取一次。"""
        entry = self.meta.get(key) if _META_KEY.match(key or "") else None
        if not entry or entry.get("state") != "matched":
            handler._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "private, max-age=600"})
            return
        name = entry.get("poster") or ""
        if not (_POSTER_FILE.match(name) and (self.art_dir / name).is_file()):
            name = self._poster(entry)
            if not name:
                handler._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "private, max-age=600"})
                return
            self.meta.put(key, dict(entry, poster=name))
        try:
            data = (self.art_dir / name).read_bytes()
        except OSError:
            handler._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "private, max-age=600"})
            return
        handler._send(200, data, IMAGE_TYPES["." + name.rsplit(".", 1)[1]], {"Cache-Control": "private, max-age=86400"})

    def serve_thumb(self, handler, did: str) -> None:
        """GET /api/admin/library/douban/thumb/<豆瓣 id>：搜索结果里候选的小图（只认刚搜到的，内存里缓存）。"""
        found = self.douban.thumb(did) if self.online() else None
        if found is None:
            handler._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
            return
        kind, data = found
        handler._send(200, data, IMAGE_TYPES["." + kind], {"Cache-Control": "private, max-age=3600"})

    def serve_tmdb_thumb(self, handler, kind: str, tid: str) -> None:
        """GET /api/admin/library/tmdb/thumb/<tv|movie>-<编号>：TMDB 候选的小图（只认刚搜到的）。"""
        found = self.tmdb.thumb(f"tmdb:{kind}:{tid}") if self.online() else None
        if found is None:
            handler._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
            return
        image, data = found
        handler._send(200, data, IMAGE_TYPES["." + image], {"Cache-Control": "private, max-age=3600"})

    # ---- 管理员：加、删、扫描 ----

    def add(self, body: dict) -> dict:
        kind = body.get("kind")
        name = " ".join(str(body.get("name") or "").split())[:NAME_MAX]
        src: dict = {"id": secrets.token_hex(4), "kind": kind, "added_at": int(self.clock()), "last_scan": 0,
                     "ok": None, "error": "", "note": ""}
        if kind == "local":
            raw = body.get("path") if isinstance(body.get("path"), str) else ""
            path = os.path.expanduser(raw.strip())
            if not path or not os.path.isabs(path) or len(path) > URL_MAX or "\0" in path:
                return {"ok": False, "error": "填文件夹的完整路径，例如 /Users/你/Movies"}
            path = os.path.realpath(path)
            if path == os.path.realpath(os.sep):
                return {"ok": False, "error": "不能用整个磁盘，选一个放视频的文件夹"}
            if not os.path.isdir(path):
                return {"ok": False, "error": "这个文件夹不存在"}
            src.update(path=path, name=name or os.path.basename(path) or "本机文件夹")
            same = lambda s: s.get("kind") == "local" and s.get("path") == path  # noqa: E731
        elif kind == "webdav":
            url = body.get("url").strip() if isinstance(body.get("url"), str) else ""
            user = body.get("user") if isinstance(body.get("user"), str) else ""
            password = body.get("password") if isinstance(body.get("password"), str) else ""
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > URL_MAX \
                    or any(c in url for c in " \n\r\t"):
                return {"ok": False, "error": "地址要以 http:// 或 https:// 开头，例如 http://127.0.0.1:5244/dav"}
            if parsed.username or parsed.password:
                return {"ok": False, "error": "用户名和密码分开填，不要写在地址里"}
            if len(user) > 120 or len(password) > 200 or any(c in user + password for c in "\n\r\0"):
                return {"ok": False, "error": "用户名或密码太长"}
            src.update(url=url, user=user.strip(), password=password,
                       name=name or (parsed.hostname or "网盘")[:NAME_MAX])
            same = lambda s: s.get("kind") == "webdav" and s.get("url", "").rstrip("/") == url.rstrip("/") \
                and s.get("user") == src["user"]  # noqa: E731
        else:
            return {"ok": False, "error": "请求格式不对"}
        with _LOCK:
            sources = self._load_sources()
            if any(same(s) for s in sources):
                return {"ok": False, "error": "已经加过这个来源了"}
            if len(sources) >= SOURCES_MAX:
                return {"ok": False, "error": f"最多 {SOURCES_MAX} 个来源"}
        # 先试一下连得上（网盘：PROPFIND 要回 207），连不上不存。
        problem = self._source(src).check()
        if problem:
            return {"ok": False, "error": problem}
        with _LOCK:
            sources = self._load_sources()
            if any(same(s) for s in sources):
                return {"ok": False, "error": "已经加过这个来源了"}
            sources.append(src)
            self._save_sources(sources)
        self.log(f"片库加了来源「{src['name']}」（{'网盘' if kind == 'webdav' else '本机文件夹'}）")
        self.scan(src["id"])
        return {"ok": True, "source": self._public_source(src, 0)}

    def remove(self, sid: str, managed: bool = False) -> dict:
        """删一个来源（索引里它的条目、缓存的海报一起）。网盘组件的来源只有 managed=True（clouddrive.py
        停用组件时）才删得掉。"""
        with _LOCK:
            sources = self._load_sources()
            found = next((s for s in sources if s["id"] == sid), None)
            if found is None:
                return {"ok": False, "error": "没有这个来源"}
            if found.get("managed") == MANAGED and not managed:
                return {"ok": False, "error": MANAGED_NOTE}
            self._save_sources([s for s in sources if s["id"] != sid])
            gone = [i for i, item in self._items.items() if item["source"] == sid]
            for iid in gone:
                del self._items[iid]
            self._save_index()
            if sid in self._queue:
                self._queue.remove(sid)
        for cached in self.art_dir.glob(f"{sid}-*") if self.art_dir.is_dir() else ():
            try:
                cached.unlink()
            except OSError:
                pass
        self.cache.prune()
        self.full.prune()
        self.log(f"片库删了来源「{found.get('name')}」（{len(gone)} 个视频）")
        return {"ok": True, "name": found.get("name") or ""}

    def managed(self) -> dict | None:
        found = next((s for s in self._load_sources() if s.get("managed") == MANAGED), None)
        return dict(found) if found is not None else None

    def ensure_managed(self, url: str, user: str, password: str) -> str:
        """网盘组件在跑：有「网盘」来源就把地址、用户名、密码对上（端口换了、密码重设了），没有就加一个并扫描。
        不先试连（组件刚起、还没加网盘时 PROPFIND 本来就会出错）。返回 "added" / "updated" / "same"。"""
        with _LOCK:
            sources = self._load_sources()
            found = next((s for s in sources if s.get("managed") == MANAGED), None)
            if found is not None:
                if (found.get("url"), found.get("user"), found.get("password")) == (url, user, password):
                    return "same"
                found.update(url=url, user=user, password=password)
                self._save_sources(sources)
                changed = "updated"
            else:
                src = {"id": secrets.token_hex(4), "kind": "webdav", "managed": MANAGED, "name": MANAGED_NAME,
                       "url": url, "user": user, "password": password, "added_at": int(self.clock()),
                       "last_scan": 0, "ok": None, "error": "", "note": ""}
                sources.append(src)
                self._save_sources(sources)
                changed = "added"
        if changed == "added":
            self.log("片库加了网盘组件的来源「网盘」")
            self.scan(src["id"])
        else:
            self.log("片库「网盘」的地址或密码跟着网盘组件改了")
        return changed

    def drop_managed(self) -> bool:
        """网盘组件停用了（或卸掉了）：去掉「网盘」来源和它的条目。"""
        found = self.managed()
        if found is None:
            return False
        return bool(self.remove(found["id"], managed=True).get("ok"))

    def scan(self, sid: str | None = None) -> dict:
        """排上扫描（sid 为空是全部来源），后台一个一个扫。"""
        sources = self._load_sources()
        wanted = [s["id"] for s in sources if sid is None or s["id"] == sid]
        if sid is not None and not wanted:
            return {"ok": False, "error": "没有这个来源"}
        if not wanted:
            return {"ok": False, "error": "还没有片库来源"}
        with _LOCK:
            for one in wanted:
                if one not in self._queue and not (self._status["running"] and self._status["source"] == one):
                    self._queue.append(one)
            start = self.background and (self._worker is None or not self._worker.is_alive())
            if start:
                self._worker = threading.Thread(target=self._drain, name="library-scan", daemon=True)
                self._worker.start()
        if not self.background:
            self._drain()
        return {"ok": True, "scan": self.status()}

    def _drain(self) -> None:
        while True:
            with _LOCK:
                if not self._queue:
                    if self.background:
                        self._worker = None
                    return
                sid = self._queue.popleft()
            try:
                self.scan_now(sid)
            except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                self.log(f"片库扫描出错：{type(error).__name__}")
                with _LOCK:
                    self._status.update(running=False, error="扫描出错了")

    # ---- 扫描 ----

    def _sleep(self, source) -> None:
        if self.pace and source.pace_s:
            time.sleep(source.pace_s)

    def walk(self, source, refresh: Callable[[str], None] | None = None) -> tuple[list[Entry], bool]:
        """一层一层（广度优先）走一遍：(文件, 是不是到了上限)。根列不出来抛 LibraryError。refresh：列一个目录之前
        先叫它刷新这个目录（网盘组件的来源，见 refresh_dir）；刷新不了就照旧列。"""
        files: list[Entry] = []
        queue: deque[tuple[str, int]] = deque([("", 0)])
        walked = skipped = 0
        truncated = False
        while queue and not truncated:
            rel, depth = queue.popleft()
            if refresh is not None:
                try:
                    refresh(rel)
                except Exception:  # noqa: BLE001 —— 刷新不了就读组件缓存着的列表（可能旧一点）
                    pass
            try:
                listing = source.list_dir(rel)
            except LibraryError:
                if not rel:
                    raise
                skipped += 1
                if skipped > SKIPPED_DIRS_MAX:
                    raise
                continue
            for entry in sorted(listing, key=lambda e: e.name):
                walked += 1
                if walked > WALK_MAX:
                    truncated = True
                    break
                if entry.is_dir:
                    if depth + 1 < DEPTH_MAX:
                        queue.append((entry.rel, depth + 1))
                else:
                    files.append(entry)
            with _LOCK:
                self._status["walked"] = walked
            self._sleep(source)
        return files, truncated

    @staticmethod
    def _poster_of(entry: Entry, here: dict[str, Entry], above: dict[str, Entry]) -> str:
        """视频旁边的海报：<名字>-poster、<名字>、<名字>-thumb、poster、folder、cover；季的文件夹再看上一层。"""
        stem = os.path.splitext(entry.name)[0].casefold()
        for base in (f"{stem}-poster", stem, f"{stem}-thumb", "poster", "folder", "cover"):
            for ext in IMAGE_TYPES:
                found = here.get(base + ext)
                if found is not None and found.size <= ART_MAX:
                    return found.rel
        for base in ("poster", "folder", "cover"):
            for ext in IMAGE_TYPES:
                found = above.get(base + ext)
                if found is not None and found.size <= ART_MAX:
                    return found.rel
        return ""

    def build(self, sid: str, source, files: list[Entry], old: dict[str, dict]) -> list[dict]:
        """文件 → 条目。大小和修改时间都没变的照旧用（不再读 .nfo）；海报每次按目录重新找（不用取）。"""
        by_dir: dict[str, dict[str, Entry]] = {}
        for entry in files:
            by_dir.setdefault(entry.rel.rpartition("/")[0], {})[entry.name.casefold()] = entry
        videos_in: dict[str, int] = {}
        for entry in files:
            if _ext(entry.name) in VIDEO_TYPES:
                folder = entry.rel.rpartition("/")[0]
                videos_in[folder] = videos_in.get(folder, 0) + 1
        nfo_budget = NFO_PER_SCAN if source.kind == "webdav" else 1 << 30
        now = int(self.clock())
        items = []
        for entry in files:
            ext = _ext(entry.name)
            stem = os.path.splitext(entry.name)[0]
            if ext not in VIDEO_TYPES or _EXTRA.search(stem):
                continue
            folder = entry.rel.rpartition("/")[0]
            here = by_dir.get(folder, {})
            above = by_dir.get(folder.rpartition("/")[0], {}) if folder else {}
            iid = item_id(sid, entry.rel)
            poster = self._poster_of(entry, here, above)
            subs = self._sidecars(entry, here)
            previous = old.get(iid)
            same = previous is not None and previous.get("size") == entry.size and previous.get("mtime") == entry.mtime
            if same and previous.get("pv") == PARSE_VERSION:
                items.append(dict(previous, poster=poster, subs=subs, playable=ext in VIDEO_TYPES))
                continue
            found = parse_name(entry.rel)
            nfo = here.get(stem.casefold() + ".nfo")
            if nfo is None and found["kind"] != "episode" and videos_in.get(folder) == 1:
                nfo = here.get("movie.nfo")
            meta = {}
            if nfo is not None and nfo.size <= NFO_MAX and nfo_budget > 0:
                nfo_budget -= 1
                meta = parse_nfo(source.read_small(nfo.rel, NFO_MAX) or b"")
            if meta.get("kind") == "movie" and found["kind"] != "episode":
                found["kind"] = "movie"
            if meta.get("kind") == "episode" and found["kind"] != "episode":
                found.update(kind="episode", show=meta.get("show") or found["title"], episode=meta.get("episode"))
            for key in ("title", "year", "plot"):
                if meta.get(key):
                    found[key] = meta[key]
            if found["kind"] == "episode":
                found["show"] = meta.get("show") or found["show"]
                found["season"] = meta.get("season") or found["season"]
                found["episode"] = meta.get("episode") or found["episode"]
            row = {"id": iid, "source": sid, "path": entry.rel, "title": found["title"] or stem,
                   "year": found.get("year"), "kind": found["kind"], "show": found.get("show") or "",
                   "season": found.get("season"), "episode": found.get("episode"), "alt": found.get("alt") or "",
                   "plot": found.get("plot") or "", "size": entry.size, "mtime": entry.mtime, "ext": ext,
                   "playable": ext in VIDEO_TYPES, "poster": poster, "subs": subs, "pv": PARSE_VERSION,
                   "added": (previous or {}).get("added") or now}
            # .nfo 里写了片名 / 剧名的：本地的比豆瓣的优先（libmeta 的资料只补没有的）。
            if meta.get("title") and found["kind"] != "episode":
                row["nfo"] = True
            if meta.get("show"):
                row["nfo_show"] = True
            if same and isinstance(previous.get("media"), dict):
                row["media"] = previous["media"]          # 只是解析的写法改了：探测的结果照旧用
            items.append(row)
        return items

    @staticmethod
    def _sidecars(entry: Entry, here: dict[str, Entry]) -> list[str]:
        """视频旁边的字幕文件：<名字>.srt / .ass / .ssa / .vtt，或者名字后面再带一段（<名字>.zh.srt、<名字>.简体.ass）。
        只记路径，扫描时不读。"""
        stem = os.path.splitext(entry.name)[0].casefold()
        found = [e.rel for name, e in here.items() if _ext(name) in SIDE_SUBS and e.size <= SUB_FILE_MAX
                 and (os.path.splitext(name)[0] == stem or name.startswith(stem + "."))]
        return sorted(found)[:SIDE_SUBS_MAX]

    def scan_now(self, sid: str) -> dict:
        """现在扫一个来源（在调用的线程里）。走到一半出错时旧的条目照旧留着。"""
        src = next((s for s in self._load_sources() if s["id"] == sid), None)
        if src is None:
            return {"ok": False, "error": "没有这个来源"}
        name = src.get("name") or ""
        with _LOCK:
            self._status.update(running=True, source=sid, name=name, walked=0, videos=0, error="")
        source = self._source(src)
        try:
            files, truncated = self.walk(source, self.refresh_dir if src.get("managed") == MANAGED else None)
            with _LOCK:
                old = {i: dict(item) for i, item in self._items.items() if item["source"] == sid}
            items = self.build(sid, source, files, old)
        except LibraryError as error:
            reason = str(error)
            if src.get("managed") == MANAGED and reason == "网盘出错了（500）":
                # OpenList 里还没有挂任何网盘时 PROPFIND / 回答 500（storage not found）。
                reason = "还没有添加网盘：点「网盘」，再点「添加夸克」或「添加百度网盘」"
            self._update_source(sid, lambda s: s.update(last_scan=int(self.clock()), ok=False, error=reason))
            with _LOCK:
                self._status.update(running=False, error=f"「{name}」：{reason}", finished_at=int(self.clock()))
            self.log(f"片库「{name}」没扫成：{reason}")
            return {"ok": False, "error": reason}
        note = f"东西太多，只扫了前 {WALK_MAX} 个" if truncated else ""
        with _LOCK:
            if not any(s["id"] == sid for s in self._load_sources()):
                self._status.update(running=False)
                return {"ok": False, "error": "这个来源刚被删掉了"}
            for iid in [i for i, item in self._items.items() if item["source"] == sid]:
                del self._items[iid]
            for item in items:
                self._items[item["id"]] = item
            self._save_index()
            self._status.update(running=False, videos=len(items), error="", finished_at=int(self.clock()))
        self._update_source(sid, lambda s: s.update(last_scan=int(self.clock()), ok=True, error="", note=note,
                                                    count=len(items)))
        fresh = sum(1 for i in items if i["id"] not in old)
        self._meta_wake.set()                      # 新加的片子去查在线资料（后台慢慢来）
        self.full.prune()                          # 不见了、变了的片子的整集缓存不留
        if src["kind"] == "webdav":
            # 新的、变了的网盘片子排上预热（后台没人在放时先把索引区读进缓存、探测一次）。
            changed = [i["id"] for i in items if i["id"] not in old
                       or (old[i["id"]].get("size"), old[i["id"]].get("mtime")) != (i["size"], i["mtime"])]
            if changed:
                with _LOCK:
                    self._warm_wanted.update(changed)
                self._warm_wake.set()
        self.log(f"片库「{name}」扫完了：{len(items)} 个视频（新的 {fresh} 个）" + (f"，{note}" if note else ""))
        return {"ok": True, "count": len(items), "new": fresh, "note": note}

    # ---- 定时 ----

    def due(self) -> list[str]:
        now = self.clock()
        # 索引里还有按旧写法解析的条目（升级以后）的来源马上扫一遍，好让在线资料按新的片名查。
        stale = {i["source"] for i in self.items() if i.get("pv") != PARSE_VERSION}
        return [s["id"] for s in self._load_sources()
                if s["id"] in stale or now - (s.get("last_scan") or 0) >= self._every(s)]

    @staticmethod
    def _every(src: dict) -> float:
        if src.get("ok") is False:
            return RETRY_S
        return DAV_EVERY_S if src.get("kind") == "webdav" else EVERY_S

    def freshen(self) -> bool:
        """打开片库页时：上次扫描已经过了 OPEN_FRESH_S 的来源排上扫描（正在扫、已经排上的不重复排）。排了就 True。"""
        now = self.clock()
        queued = False
        for src in self._load_sources():
            if now - (src.get("last_scan") or 0) >= OPEN_FRESH_S:
                self.scan(src["id"])
                queued = True
        return queued

    def run_forever(self, sleep=time.sleep) -> None:
        sleep(START_DELAY_S)
        while True:
            try:
                for sid in self.due():
                    self.scan(sid)
            except Exception as error:  # noqa: BLE001
                self.log(f"片库定时扫描出错：{type(error).__name__}")
            sleep(TICK_S)

    # ---- 预热：新片第一次打开也快 ----

    def warm_blocked(self) -> str:
        """现在不该预热的原因（大白话）；能预热时空串。"""
        with _LOCK:
            if self._serving > 0 or self.clock() - self._served_at < WARM_QUIET_S:
                return "有人在放片库里的片子"
            if self._status["running"] or self._queue:
                return "在扫描"
        if self.full.busy():
            return "在缓存整集"
        if self.watching is not None and self.watching():
            return "有人在看"
        return ""

    def _warm_tally(self, count: int) -> None:
        with _LOCK:
            self._warm_read += count

    # 预热的探测只数从网盘读的（_serve_cached 从缓存给的那部分不数）：一轮的 WARM_BUDGET 和日志说的是网盘的字节。
    _warm_tally.drive_only = True

    @staticmethod
    def _probed(item: dict) -> bool:
        media = item.get("media")
        return isinstance(media, dict) and media.get("v") == PROBE_VERSION and media.get("size") == item.get("size") \
            and media.get("mtime") == item.get("mtime")

    def warm_due(self) -> list[dict]:
        """要预热的条目（新的先）：扫描找到的新的、变了的网盘片子；中转刚起来时另外算上 WARM_NEW_S 内加进来、
        还没探测过的。本机文件夹的、来源没了的、失败了 WARM_TRIES 次的不算。"""
        remote = {s["id"] for s in self._load_sources() if s["kind"] == "webdav"}
        now = self.clock()
        with _LOCK:
            if not self._warm_seeded:
                self._warm_seeded = True
                self._warm_wanted.update(i["id"] for i in self._items.values() if i["source"] in remote
                                         and now - (i.get("added") or 0) < WARM_NEW_S and not self._probed(i))
            for iid in [i for i in self._warm_wanted
                        if i not in self._items or self._items[i]["source"] not in remote]:
                self._warm_wanted.discard(iid)
            due = [dict(self._items[i]) for i in self._warm_wanted
                   if int(self._items[i].get("size") or 0) > 0
                   and self._warm_tries.get(self._warm_key(self._items[i]), 0) < WARM_TRIES]
        return sorted(due, key=lambda i: (i.get("added") or 0, i.get("mtime") or 0), reverse=True)

    @staticmethod
    def _warm_key(item: dict) -> str:
        return IndexCache.key_of(item["id"], int(item.get("size") or 0), int(item.get("mtime") or 0))

    def warm_step(self) -> dict:
        """预热一轮（一个文件一个文件来，新的先）：每个文件把索引区读进缓存（_warm_one），再探测一次存进索引。
        有人在放、在看时不做，做到一半有人开始了就停下；一轮至多读 WARM_BUDGET；缓存不挤掉 WARM_KEEP_S 内用过的；
        网盘出错了停下、歇一会儿。返回 {"files": 预热好了几个, "bytes": 读了多少, "stopped": 为什么停下（做完了是空串）}。
        做了点什么时记一行日志（不带地址）。"""
        result = {"files": 0, "bytes": 0, "stopped": ""}
        if not self._warm_lock.acquire(blocking=False):
            return dict(result, stopped="已经在预热")
        try:
            if self.clock() < self._warm_after:
                return dict(result, stopped="网盘刚出过错，歇一会儿")
            why = self.warm_blocked()
            if why:
                return dict(result, stopped=why)
            failed = False
            for item in self.warm_due():
                left = WARM_BUDGET - result["bytes"]
                if left <= 0:
                    result["stopped"] = "这一轮读够了"
                    break
                if self.cache.room(WARM_KEEP_S) < self.cache.file_max:
                    result["stopped"] = "索引缓存快满了"
                    break
                try:
                    read, done = self._warm_one(item, left)
                except (urllib.error.HTTPError, LibraryError, OSError, ValueError) as error:
                    if isinstance(error, urllib.error.HTTPError):
                        error.close()
                    key = self._warm_key(item)
                    with _LOCK:
                        self._warm_tries[key] = self._warm_tries.get(key, 0) + 1
                    self._warm_backoff = min(WARM_BACKOFF_MAX_S, self._warm_backoff * 2 or WARM_BACKOFF_S)
                    self._warm_after = self.clock() + self._warm_backoff
                    result["stopped"] = str(error) if isinstance(error, LibraryError) else _dav_reason(error)
                    failed = True
                    break
                result["bytes"] += read
                if not done:
                    result["stopped"] = self.warm_blocked() or "这一轮读够了"
                    break
                result["files"] += 1
                with _LOCK:
                    self._warm_wanted.discard(item["id"])
            if not failed:
                self._warm_backoff = 0.0
        finally:
            self._warm_lock.release()
        if result["files"] or result["bytes"] or failed:
            line = f"片库预热：{result['files']} 个新片子，读了 {result['bytes'] // (1024 * 1024)} MB"
            self.log(line + (f"，停下了：{result['stopped']}" if result["stopped"] else ""))
        return result

    def _warm_one(self, item: dict, left: int) -> tuple[int, bool]:
        """预热一个网盘文件：缓存还缺的索引区（IndexCache.todo、MKV 的 Cues 看 cues_at）一段一段从网盘读，经 tee
        存进去（和 ffmpeg 读到时一样的存法、一样的上限）；再探测一次（media_of，读的多半已经在缓存里）。
        返回 (读了多少字节, 做完了没有)；有人开始放了、这一轮读够了时没做完（下一轮接着读）。网盘出错时抛。"""
        src = next((s for s in self._load_sources() if s["id"] == item["source"]), None)
        if src is None or src["kind"] != "webdav":
            return 0, True
        dav = self._source(src)
        iid, size, mtime, ext = item["id"], int(item["size"]), int(item.get("mtime") or 0), item.get("ext") or ""
        read = 0
        for _ in range(8):                          # 开头、MP4 的 moov / MKV 的 Cues，没存满的再接着读
            if self.warm_blocked() or read >= left:
                return read, False
            starts = self.cache.todo(iid, size, mtime, ext)
            cues = self.cache.cues_at(iid, size, mtime, ext)
            if cues is not None:
                starts.append(cues)
            if not starts:
                break
            got = self._fill(dav, item, starts[0], left - read)
            read += got
            if got == 0:
                break
        if self.warm_blocked():
            return read, False
        if self.ffmpeg and self.secret is not None and self.port is not None:
            with _LOCK:
                before = self._warm_read
            media = self.media_of(iid, warm=True)
            with _LOCK:
                read += self._warm_read - before
            if media is None:
                raise LibraryError("探测不了")
        return read, True

    def _fill(self, dav: WebDav, item: dict, start: int, left: int) -> int:
        """从网盘 start 起读一段、经 tee 存进索引缓存（每次只读这一段还存得下的），存满了（这一段到头、到了 FILE_MAX）、读了 left、
        有人开始放了时停下。返回读了多少字节；这里存不了（别的在写）时 0。网盘的回答对不上（文件变了）时这个文件的
        缓存作废、抛 LibraryError。"""
        iid, size, mtime, ext = item["id"], int(item["size"]), int(item.get("mtime") or 0), item.get("ext") or ""
        tee = self.cache.tee(iid, size, mtime, ext, start)
        if tee is None:
            return 0
        got = 0
        try:
            upstream = dav.open(item["path"], f"bytes={start}-{min(size, start + self.cache.file_max) - 1}")
            try:
                span = self._answer_span(upstream, size)
                if span is None or span[0] != start:
                    tee.stopped = True
                    if span is None:
                        self.cache.drop(iid)
                    raise LibraryError("网盘上的文件变了（重新扫描一下片库）")
                read = getattr(upstream, "read1", None) or upstream.read
                began = time.monotonic()
                while got < left and not self.warm_blocked():
                    want = min(CHUNK, tee.room(), left - got)
                    chunk = read(want) if want > 0 else b""
                    if not chunk:
                        break
                    tee.write(chunk)
                    got += len(chunk)
                    if self.pace:                   # 低优先级：至多 WARM_RATE 字节 / 秒
                        ahead = got / WARM_RATE - (time.monotonic() - began)
                        if ahead > 0:
                            time.sleep(ahead)
            finally:
                upstream.close()
        finally:
            tee.close()
        return got

    def run_warm(self, sleep=time.sleep) -> None:
        """后台的预热：启动后先等 WARM_START_S，之后每 WARM_TICK_S（扫描找到新片子时马上）做一轮。"""
        sleep(WARM_START_S)
        while True:
            try:
                self.warm_step()
            except Exception as error:  # noqa: BLE001 —— 后台线程不能悄悄死
                self.log(f"片库预热出错：{type(error).__name__}")
            self._warm_wake.wait(WARM_TICK_S)
            self._warm_wake.clear()

    # ---- 放文件 ----

    def _send_head(self, handler, code: int, kind: str, length: int | None, extra: dict | None = None) -> None:
        handler.send_response(code)
        handler.send_header("Content-Type", kind)
        if length is not None:
            handler.send_header("Content-Length", str(length))
        else:
            handler.send_header("Connection", "close")
            handler.close_connection = True
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            handler.send_header(key, value)
        handler.end_headers()

    @staticmethod
    def _pump(handler, read, expected: int | None, tally: Callable[[int], None] | None = None,
              tee=None, close: bool = True) -> int:
        """边读边发，返回发了多少字节。tee：读到的先存进索引缓存（librarycache._Tee）再发。close：没发够
        expected 时断开连接（后面还要接着发的不断）。"""
        sock = getattr(handler, "connection", None)
        total = 0
        while expected is None or total < expected:
            if _client_gone(sock):
                break
            chunk = read(CHUNK if expected is None else min(CHUNK, expected - total))
            if not chunk:
                break
            if tee is not None:
                tee.write(chunk)
            handler.wfile.write(chunk)
            total += len(chunk)
            if tally is not None:
                tally(len(chunk))
        if close and expected is not None and total < expected:
            handler.close_connection = True
        return total

    def serve_file(self, handler, iid: str, raw: bool = False, warm: bool = False) -> None:
        """GET /api/library/file/<id>：按 Range 给文件的字节（只认 id）。raw：给中转自己的 ffmpeg 的（/api/library/raw），
        记下给了多少字节（起播时的进度）。warm：预热自己的探测（不算「有人在放」，字节记进这一轮预热）。"""
        item = self.item(iid)
        src = next((s for s in self._load_sources() if item and s["id"] == item["source"]), None)
        if item is None or src is None:
            handler._not_found()
            return
        if not self._files.acquire(blocking=False):
            handler._send(503, "同时在放的太多了，过一会儿再试".encode(), "text/plain; charset=utf-8")
            return
        with _LOCK:
            if not warm:
                self._serving += 1
        try:
            kind = VIDEO_TYPES.get(item.get("ext") or "", "application/octet-stream")
            tally = self._warm_tally if warm else self._tally(iid) if raw else None
            if src["kind"] == "local":
                self._serve_local(handler, LocalFolder(src.get("path") or ""), item, kind, tally)
            else:
                self._serve_dav(handler, self._source(src), item, kind, src.get("name") or "", tally, cached=raw,
                                playback=raw and not warm)
        except (OSError, ValueError):
            handler.close_connection = True        # 浏览器断开（跳转、关窗口）
        finally:
            self._files.release()
            with _LOCK:
                if not warm:
                    self._serving -= 1
                    self._served_at = self.clock()

    def _serve_local(self, handler, folder: LocalFolder, item: dict, kind: str, tally=None) -> None:
        path = folder.path_of(item["path"])
        if path is None:
            handler._send(404, "文件不见了（重新扫描一下片库）".encode(), "text/plain; charset=utf-8")
            return
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            wanted = parse_range(handler.headers.get("Range"), size)
            if wanted == "bad":
                handler._send(416, b"", "text/plain; charset=utf-8", {"Content-Range": f"bytes */{size}"})
                return
            if wanted is None:
                self._send_head(handler, 200, kind, size)
                self._pump(handler, handle.read, size, tally)
                return
            start, end = wanted
            handle.seek(start)
            self._send_head(handler, 206, kind, end - start + 1, {"Content-Range": f"bytes {start}-{end}/{size}"})
            self._pump(handler, handle.read, end - start + 1, tally)

    def _note_failed(self, item: dict, name: str, reason: str) -> None:
        self.log(f"片库「{name}」放不了一个文件：{reason}")
        with _LOCK:
            self._drive_failed[item["id"]] = time.monotonic()

    def drive_failed_since(self, iid: str, since: float) -> bool:
        """这个文件从 since（time.monotonic）以来从网盘读失败过。"""
        with _LOCK:
            return self._drive_failed.get(iid, float("-inf")) >= since

    def _open_dav(self, handler, dav: WebDav, item: dict, name: str, wanted: str | None):
        """GET 网盘上的这个文件；出错时已经回了浏览器（416 / 502），返回 None。"""
        try:
            return dav.open(item["path"], wanted)
        except urllib.error.HTTPError as error:
            extra = {"Content-Range": error.headers["Content-Range"]} \
                if error.code == 416 and error.headers.get("Content-Range") else {}
            error.close()
            if error.code == 416:
                handler._send(416, b"", "text/plain; charset=utf-8", extra)
                return None
            reason = _dav_reason(error)
            if error.code == 403:
                reason = "网盘拒绝了（在 OpenList 里把这个存储的 WebDAV 策略改成「本地代理」再试）"
            self._note_failed(item, name, reason)
            handler._send(502, reason.encode(), "text/plain; charset=utf-8")
            return None
        except (LibraryError, OSError, ValueError) as error:
            reason = str(error) if isinstance(error, LibraryError) else _dav_reason(error)
            self._note_failed(item, name, reason)
            handler._send(502, reason.encode(), "text/plain; charset=utf-8")
            return None

    def _relay(self, handler, upstream, kind: str, tally=None, tee=None) -> None:
        """把网盘的回答原样转给请求的人：状态（206 / 200）、Content-Range、Content-Length，边读边发。"""
        try:
            status = getattr(upstream, "status", None) or upstream.getcode()
            upstream_kind = upstream.headers.get("Content-Type") or ""
            if upstream_kind.startswith(("video/", "audio/")):
                kind = upstream_kind
            length = upstream.headers.get("Content-Length")
            extra = {}
            if status == 206 and upstream.headers.get("Content-Range"):
                extra["Content-Range"] = upstream.headers["Content-Range"]
            expected = int(length) if length and length.isdigit() else None
            self._send_head(handler, 206 if status == 206 else 200, kind, expected, extra)
            read = getattr(upstream, "read1", None) or upstream.read
            self._pump(handler, read, expected, tally, tee)
        finally:
            upstream.close()

    def _serve_dav(self, handler, dav: WebDav, item: dict, kind: str, name: str, tally=None,
                   cached: bool = False, playback: bool = False) -> None:
        """网盘的文件：Range 带给上游，上游的状态、Content-Range、Content-Length 原样回。cached：给 ffmpeg 的
        （raw），先看索引缓存（_serve_cached）。playback：放片子的 ffmpeg 读的（不是预热的探测），整集缓存开着时
        走 _serve_full。"""
        wanted = (handler.headers.get("Range") or "").strip()
        wanted = wanted if _RANGE.match(wanted) and wanted != "bytes=-" else None
        size = int(item.get("size") or 0)
        span = parse_range(wanted, size) if cached and wanted and size > 0 else None
        if playback and isinstance(span, tuple) and self.full.enabled():
            self._serve_full(handler, dav, item, kind, name, tally, *span)
            return
        if isinstance(span, tuple) and self._serve_cached(handler, dav, item, kind, name, tally, *span):
            return
        upstream = self._open_dav(handler, dav, item, name, wanted)
        if upstream is not None:
            self._relay(handler, upstream, kind, tally)

    @staticmethod
    def _answer_span(upstream, size: int):
        """上游的回答给的是文件的哪一段：(起, 止)（含止）。不是带着总长对得上的 Content-Range 的 206（200 时
        Content-Length 要等于文件大小，就是整个文件）时 None。"""
        status = getattr(upstream, "status", None) or upstream.getcode()
        if status == 200:
            length = upstream.headers.get("Content-Length") or ""
            return (0, size - 1) if length.isdigit() and int(length) == size else None
        match = _CONTENT_RANGE.match((upstream.headers.get("Content-Range") or "").strip()) if status == 206 else None
        if match is None or int(match.group(3)) != size:
            return None
        return int(match.group(1)), int(match.group(2))

    def _serve_cached(self, handler, dav: WebDav, item: dict, kind: str, name: str, tally, start: int,
                      end: int) -> bool:
        """给 ffmpeg 的 Range 请求先看索引缓存（librarycache.py）。起点在已经存下的一段里：从缓存给，给完了还要
        更多就在同一个回答里接着从网盘读（顺手把这一段存得更长）。起点正好是能存的地方（文件开头、MP4 的 mdat
        后面、别的格式的文件尾、一段已经存到的地方）：从网盘读、边放边存。都不是时返回 False（照旧直接转网盘的回答）。
        网盘回答的总长和索引里的大小对不上时这个文件的缓存整个作废（文件在网盘上变了，还没重新扫描）。"""
        iid, size, mtime, ext = item["id"], int(item["size"]), int(item.get("mtime") or 0), item.get("ext") or ""
        hit = self.cache.open(iid, size, mtime, ext, start)
        if hit is None:
            tee = self.cache.tee(iid, size, mtime, ext, start)
            if tee is None:
                return False
            try:
                upstream = self._open_dav(handler, dav, item, name, f"bytes={start}-{end}")
                if upstream is not None:
                    span = self._answer_span(upstream, size)
                    if span is None or span[0] != start:
                        tee.stopped = True
                        if span is None:
                            self.cache.drop(iid)
                    self._relay(handler, upstream, kind, tally, tee)
            finally:
                tee.close()
            return True
        handle, have = hit
        first = min(have, end - start + 1)
        with handle:
            self._send_head(handler, 206, kind, end - start + 1, {"Content-Range": f"bytes {start}-{end}/{size}"})
            cached_tally = None if getattr(tally, "drive_only", False) else tally
            if self._pump(handler, handle.read, first, cached_tally, close=False) < first:
                handler.close_connection = True
                return True
        at = start + first
        if at > end:
            return True
        # 缓存的这一段给完了，还要更多：接着从网盘读（同一个回答里接着发，ffmpeg 看不出来）。
        if _client_left(getattr(handler, "connection", None), CONTINUE_WAIT_S):
            handler.close_connection = True
            return True
        tee = self.cache.tee(iid, size, mtime, ext, at)
        try:
            upstream = dav.open(item["path"], f"bytes={at}-{end}")
        except (urllib.error.HTTPError, LibraryError, OSError, ValueError) as error:
            if tee is not None:
                tee.close()
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            reason = str(error) if isinstance(error, LibraryError) else _dav_reason(error)
            self._note_failed(item, name, reason)
            handler.close_connection = True
            return True
        try:
            span = self._answer_span(upstream, size)
            if span is None or span[0] != at:
                if span is None:
                    self.cache.drop(iid)
                    self.log(f"片库「{name}」有个文件在网盘上变了，它的索引缓存不要了（重新扫描一下片库）")
                handler.close_connection = True
                return True
            read = getattr(upstream, "read1", None) or upstream.read
            self._pump(handler, read, end - at + 1, tally, tee)
        finally:
            upstream.close()
            if tee is not None:
                tee.close()
        return True

    # ---- 整集缓存（libraryfull.py）：边看边整个下到本机 ----

    @staticmethod
    def _full_name(item: dict) -> str:
        """日志里说的名字：剧集是「剧名 第几集」，别的是片名（从不带路径）。"""
        title = item.get("title") or ""
        return f"{item['show']} {title}".strip() if item.get("kind") == "episode" and item.get("show") else title

    def _full_open(self, item: dict, start: int, last: int):
        """整集缓存的下载线程打开网盘上的 [start, last]。回答的总长对不上（文件在网盘上变了）时索引缓存也作废、
        抛 libraryfull.Changed；别的错抛 LibraryError / OSError。"""
        src = next((s for s in self._load_sources() if s["id"] == item.get("source")), None)
        if src is None or src["kind"] != "webdav":
            raise LibraryError("来源没了")
        try:
            upstream = self._source(src).open(item["path"], f"bytes={start}-{last}")
        except urllib.error.HTTPError as error:
            error.close()
            raise LibraryError(_dav_reason(error)) from None
        span = self._answer_span(upstream, int(item.get("size") or 0))
        if span is None or span[0] != start:
            upstream.close()
            if span is None:
                self.cache.drop(item["id"])
                raise libraryfull.Changed()
            raise LibraryError("网盘没按要的那段给")
        return upstream

    def _send_local(self, handler, path: Path, at: int, count: int, tally, tee=None) -> int:
        """从整集缓存的数据文件给 [at, at + count)，返回发了多少字节。"""
        try:
            handle = open(path, "rb")
        except OSError:
            return 0
        with handle:
            handle.seek(at)
            return self._pump(handler, handle.read, count, tally, tee, close=False)

    def _serve_full(self, handler, dav: WebDav, item: dict, kind: str, name: str, tally, start: int,
                    end: int) -> None:
        """整集缓存开着时给 ffmpeg 的 Range 请求（libraryfull.py）。一段一段给，回答头只发一次（206、Content-Range），
        中间换地方 ffmpeg 看不出来：索引缓存里有的从索引缓存给；已经下好的从本机给（下载线程边写边给）；都没有时，
        下载线程正要写这里就等它写进来（STALL_S 内一点都没等到才自己读），不然自己从网盘读一阵（下载线程让开；
        能交给它时至多读 BURST，读到的顺手存进两个缓存），读完了让下载线程从这里接着下。"""
        full, iid = self.full, item["id"]
        size, mtime, ext = int(item["size"]), int(item.get("mtime") or 0), item.get("ext") or ""
        sock = getattr(handler, "connection", None)
        cached_tally = None if getattr(tally, "drive_only", False) else tally
        full.touch(item, start, self._full_name(item))
        sent_head = []

        def head() -> None:
            if not sent_head:
                sent_head.append(True)
                self._send_head(handler, 206, kind, end - start + 1, {"Content-Range": f"bytes {start}-{end}/{size}"})

        at, local = start, False
        with full.reading(item):
            while at <= end:
                want = end - at + 1
                # 索引缓存先看（它按最后用过的时间删，用到了要记一下，和整集缓存关着时一样）。
                hit = self.cache.open(iid, size, mtime, ext, at)
                if hit is not None:
                    handle, have = hit
                    head()
                    count = min(have, want)
                    with handle:
                        sent = self._pump(handler, handle.read, count, cached_tally, close=False)
                    at, local = at + sent, True
                    if sent < count:
                        break
                    continue
                found = full.local(item, at)
                if found is not None:
                    head()
                    count = min(found[1], want)
                    # 索引缓存要存这里时（文件开头、一段存到的地方……）照样存：整集的以后被删了，索引区还在。
                    tee = self.cache.tee(iid, size, mtime, ext, at)
                    try:
                        sent = self._send_local(handler, found[0], at, count, cached_tally, tee)
                    finally:
                        if tee is not None:
                            tee.close()
                    at, local = at + sent, True
                    if sent < count:
                        break
                    continue
                # 本机没有：下载线程正要写这里就等它（不另开一个网盘的请求）。
                if full.next_at(item) == at and full.wait(item, at, full.stall_s, lambda: _client_gone(sock)):
                    continue
                if _client_gone(sock) or (local and _client_left(sock, CONTINUE_WAIT_S)):
                    break
                last = min(end, at + full.burst - 1) if full.can_fill(item) else end
                sent = self._full_drive(handler, dav, item, name, at, last, tally, head, bool(sent_head))
                if sent is None:
                    break
                at, local = at + sent, False
                if at <= last:
                    break
                full.seen(item, at)
        if at <= end:
            handler.close_connection = True

    def _full_drive(self, handler, dav: WebDav, item: dict, name: str, at: int, last: int, tally, head,
                    started: bool) -> int | None:
        """放片子自己从网盘读 [at, last]（这时下载线程让开），读到的顺手存进整集缓存和索引缓存。返回发了多少字节；
        网盘出错、文件变了时 None（回答头还没发时回 502 / 416）。"""
        iid, size, mtime, ext = item["id"], int(item["size"]), int(item.get("mtime") or 0), item.get("ext") or ""
        tees = [t for t in (self.full.tee(item, at), self.cache.tee(iid, size, mtime, ext, at)) if t is not None]
        try:
            with self.full.on_drive():
                if not started:
                    upstream = self._open_dav(handler, dav, item, name, f"bytes={at}-{last}")
                    if upstream is None:
                        return None
                else:
                    try:
                        upstream = dav.open(item["path"], f"bytes={at}-{last}")
                    except (urllib.error.HTTPError, LibraryError, OSError, ValueError) as error:
                        if isinstance(error, urllib.error.HTTPError):
                            error.close()
                        self._note_failed(item, name, str(error) if isinstance(error, LibraryError)
                                          else _dav_reason(error))
                        return None
                try:
                    span = self._answer_span(upstream, size)
                    if span is None or span[0] != at:
                        if span is None:
                            self.cache.drop(iid)
                            self.full.drop(iid)
                            self.log(f"片库「{name}」有个文件在网盘上变了，它的缓存不要了（重新扫描一下片库）")
                        if not started:
                            handler._send(502, "网盘上的文件变了（重新扫描一下片库）".encode(),
                                          "text/plain; charset=utf-8")
                        return None
                    head()
                    read = getattr(upstream, "read1", None) or upstream.read
                    return self._pump(handler, read, last - at + 1, tally, _Tees(tees) if tees else None, close=False)
                finally:
                    upstream.close()
        finally:
            for tee in tees:
                tee.close()

    # ---- 媒体（第二步）：探测、给中转自己的 ffmpeg 的地址、怎么放 ----

    def wire(self, ffmpeg: str, key: str, port: Callable[[], int]) -> None:
        """中转给的：ffmpeg 的路径、令牌密钥（由中转密钥派生）、管理网页的端口（ffmpeg 从 127.0.0.1 的这个端口取文件）。"""
        self.ffmpeg = ffmpeg
        self.secret = raw_secret(key)
        self.port = port

    def raw_token(self, iid: str) -> str:
        from ..listen import issue_token
        return issue_token(self.secret, f"libraw/{iid}", now=time.time(), lifetime_s=RAW_LIFETIME_S)

    def raw_ok(self, iid: str, token: str | None) -> bool:
        from ..listen import check_token
        return self.secret is not None and check_token(self.secret, f"libraw/{iid}", token, now=time.time())

    def raw_url(self, iid: str, warm: bool = False) -> str:
        """中转自己的 ffmpeg 取这个文件的地址：只在本机（127.0.0.1）、带短期令牌。网盘的地址和密码从不给 ffmpeg，
        本机的文件也不用 file: 协议（watch.py 只许网络协议）。warm：预热的探测（带 w=1，不算「有人在放」）。"""
        return f"http://127.0.0.1:{int(self.port())}/tv/api/library/raw/{iid}?t={self.raw_token(iid)}" \
            + ("&w=1" if warm else "")

    def bytes_read(self, iid: str) -> int:
        """经 /api/library/raw 给出去的这个文件一共多少字节（起播时说「已读 12 MB」）。"""
        with _LOCK:
            return self._read.get(iid, 0)

    def _tally(self, iid: str) -> Callable[[int], None]:
        def add(count: int) -> None:
            with _LOCK:
                self._read[iid] = self._read.get(iid, 0) + count
        return add

    def remote(self, item: dict) -> bool:
        src = next((s for s in self._load_sources() if s["id"] == item.get("source")), None)
        return src is not None and src["kind"] == "webdav"

    def media_of(self, iid: str, warm: bool = False) -> dict | None:
        """这个条目探测到的各路流（缓存在索引里，大小和修改时间变了才重新探测）；探测不了时 None（不缓存，
        PROBE_RETRY_S 内不再试）。在请求的线程里做（网盘上的要几秒），同一个条目同时只探测一次。warm：预热时探测
        （经 raw_url(warm=True)；失败了不记，真放的时候照样再探测）。"""
        item = self.item(iid)
        if item is None:
            return None
        cached = item.get("media")
        if isinstance(cached, dict) and cached.get("v") == PROBE_VERSION and cached.get("size") == item.get("size") \
                and cached.get("mtime") == item.get("mtime"):
            return cached
        with _LOCK:
            lock = self._probing.setdefault(iid, threading.Lock())
            failed = self._probe_failed.get(iid)
        if failed is not None and self.clock() - failed < PROBE_RETRY_S:
            return None
        with lock:
            again = self.item(iid)
            cached = (again or {}).get("media")
            if isinstance(cached, dict) and cached.get("v") == PROBE_VERSION \
                    and cached.get("size") == item.get("size") and cached.get("mtime") == item.get("mtime"):
                return cached
            found = self.probe(iid, warm=True) if warm else self.probe(iid)
            if found is None:
                if not warm:
                    with _LOCK:
                        self._probe_failed[iid] = self.clock()
                return None
            return self._keep_probe(iid, item, found)

    def _keep_probe(self, iid: str, item: dict, found: dict) -> dict:
        """探测的结果存进索引的条目（color：问过传输特性和原色了，见 with_color）。"""
        found.update(v=PROBE_VERSION, size=item.get("size"), mtime=item.get("mtime"), color=True)
        with _LOCK:
            if iid in self._items:
                self._items[iid]["media"] = found
                self._save_index()
        return found

    def with_color(self, iid: str, media: dict | None) -> dict | None:
        """要转码画面时用：media 里有没有画面的传输特性（HDR 要色调映射，is_hdr）。以前探测的（没有 color）不知道；
        只有 10 位的画面（HDR 的片子都是）才重新探测一次补上——网盘上的文件头多半在索引区的缓存里（librarycache），
        不必为这个加 PROBE_VERSION 让所有的片子都重新探测。探测不了时照旧用原来的（当 SDR），PROBE_RETRY_S 内不再试。"""
        video = first_video(media)
        if not isinstance(media, dict) or media.get("color") or video is None or not _deep(video):
            return media
        item = self.item(iid)
        if item is None:
            return media
        with _LOCK:
            lock = self._probing.setdefault(iid, threading.Lock())
            failed = self._color_failed.get(iid)
        if failed is not None and self.clock() - failed < PROBE_RETRY_S:
            return media
        with lock:
            again = (self.item(iid) or {}).get("media")
            if isinstance(again, dict) and again.get("color"):
                return again
            found = self.probe(iid)
            if found is None:
                with _LOCK:
                    self._color_failed[iid] = self.clock()
                return media
            return self._keep_probe(iid, item, found)

    def probe(self, iid: str, warm: bool = False) -> dict | None:
        """探测一次（ffprobe，没有时用 ffmpeg 自己打的输入信息），读的是 raw_url。出错、超时时 None。"""
        if not self.ffmpeg or self.secret is None or self.port is None:
            return None
        from ..proxyenv import loopback_bypass
        url = self.raw_url(iid, warm)
        probe = _ffprobe_of(self.ffmpeg)
        whitelist = ["-protocol_whitelist", "http,tcp"]
        if probe:
            args = [probe, "-v", "error", *whitelist, *PROBE_ARGS, "-show_entries",
                    PROBE_ENTRIES, "-of", "json", url]
        else:
            args = [self.ffmpeg, "-hide_banner", "-nostdin", *whitelist, *PROBE_ARGS, "-i", url]
        try:
            item = self.item(iid)
            limit = PROBE_TIMEOUT_REMOTE_S if item is not None and self.remote(item) else PROBE_TIMEOUT_S
            done = subprocess.run(args, capture_output=True, timeout=limit, env=loopback_bypass(os.environ))
        except (OSError, subprocess.SubprocessError) as error:
            self.log(f"片库探测不了一个文件（{type(error).__name__}）")
            return None
        if probe:
            found = parse_probe_json(done.stdout) if done.returncode == 0 else None
        else:
            found = parse_ffmpeg_dump(done.stderr.decode("utf-8", "replace"))
        if found is None or not found["streams"]:
            self.log("片库探测不了一个文件（读不出各路流）")
            return None
        return found

    def watch_source(self, item: dict, media: dict | None, decision: dict, offset: float, label: str):
        """要转封装时给 watch.py 的片源：ffmpeg 读 raw_url（每次起 ffmpeg 都换新令牌），按 decision 的 -map 和转法，
        时间轴照原样（exact）、不按实时读但前面至多攒几段（bounded）。网盘上的内嵌文字字幕边转边抽进缓存
        （subs_out）：网盘的文件不整个再读一遍。"""
        from . import watch as watch_pc
        iid = item["id"]
        video, audio = decision.get("video"), decision.get("audio")
        maps = tuple(f"0:{i}" for i in (video, audio) if i is not None)
        tonemap = ""
        if decision.get("generic"):
            mode, copy = None, ("h264",)
        else:
            mode = watch_pc.Mode("copy" if decision["vcopy"] else "encode" if video is not None else None,
                                 ("copy" if decision["acopy"] else "encode") if audio is not None else None)
            copy = ("h264", decision["vcodec"]) if decision["vcopy"] else ("h264",)
            if mode.video == "encode":
                # 浏览器放不了、要转成 H.264 的 HDR 片子：缩放加色调映射（和小电视那一路一样，见 tonemap_for）。
                media = self.with_color(iid, media)
                stream = next((s for s in (media or {}).get("streams") or [] if s.get("i") == video), None)
                hwaccel = tv_hwaccel(self.ffmpeg)
                tonemap = tonemap_for(self.ffmpeg, stream, hwaccel)
        subs_out = ()
        if self.remote(item):
            texts = [t for t in subtitle_tracks(item, media) if t["kind"] == "embedded" and t["ok"]]
            if texts:
                self.subs_dir.mkdir(parents=True, exist_ok=True)
                subs_out = tuple((t["stream"], str(self._live_path(iid, t["stream"]))) for t in texts[:4])
        base = self.bytes_read(iid)

        def progress() -> str:
            read = (self.bytes_read(iid) - base) // (1024 * 1024)
            return f"正在读取文件…已读 {read} MB" if read else "正在打开…"

        tag = "-".join(str(x) for x in (video, audio, decision.get("vcopy"), decision.get("acopy"), tonemap))
        return watch_pc.Source(("library", iid, tag), url=lambda: self.raw_url(iid), paced=False,
                               offset=max(0.0, float(offset)), start=max(0.0, float(offset)), label=label,
                               site="library", copy_video=copy, maps=maps, video_tag=decision.get("tag") or "",
                               exact=True, media_start=float((media or {}).get("start") or 0.0), bounded=True,
                               audio_rate="192k", mode=mode, subs_out=subs_out, progress=progress,
                               video_filter=tonemap_vf(tonemap, watch_pc.ENCODE_SCALE, PC_VT_SIZE) if tonemap else "",
                               video_input=tuple(tonemap_input(tonemap, hwaccel)) if tonemap else ())

    # ---- 字幕 ----

    def subs_view(self, item: dict, media: dict | None, live: bool) -> list[dict]:
        """给播放器的字幕菜单：{n, label, lang, ok, def, live?}。live：网盘上的内嵌字幕边放边抽，播放器隔一会儿再取。"""
        rows = []
        for t in subtitle_tracks(item, media):
            row = {"n": t["n"], "label": subtitle_label(t), "lang": t["lang"], "ok": t["ok"], "def": t["def"]}
            if live and t["kind"] == "embedded" and t["ok"]:
                row["live"] = True
            rows.append(row)
        return rows

    def _live_path(self, iid: str, stream: int) -> Path:
        return self.subs_dir / f"{iid}-s{int(stream)}.live.vtt"

    def subs_text(self, iid: str, n: int) -> tuple[str, bool] | None:
        """第 n 条字幕的 WebVTT：(字, 是不是还没抽完)。没有这条、图片字幕、抽不出来时 None。
        外挂文件第一次要时读一次（至多 SUB_FILE_MAX）转好缓存；本机文件的内嵌字幕第一次要时用 ffmpeg 整个抽一遍
        （同时只抽一个）缓存；网盘上的内嵌字幕不整个抽（要把几个 GB 再读一遍），给正在放的那一路边转边抽出来的
        （watch_source 的 subs_out），并和之前抽到的合在一起。"""
        item = self.item(iid)
        src = next((s for s in self._load_sources() if item and s["id"] == item["source"]), None)
        if item is None or src is None:
            return None
        media = item.get("media") if isinstance(item.get("media"), dict) else None
        tracks = subtitle_tracks(item, media)
        if not 0 <= n < len(tracks) or not tracks[n]["ok"]:
            return None
        track = tracks[n]
        stamp = hashlib.sha1(f"{item.get('size')}:{item.get('mtime')}:{track.get('path', '')}".encode()).hexdigest()[:8]
        cached = self.subs_dir / f"{iid}-{'f' if track['kind'] == 'file' else 's'}{track.get('stream', n)}-{stamp}.vtt"
        try:
            return cached.read_text(encoding="utf-8"), False
        except OSError:
            pass
        if track["kind"] == "file":
            data = self._source(src).read_small(track["path"], SUB_FILE_MAX)
            if data is None:
                return None
            text = sidecar_vtt(data, _ext(track["path"]))
        elif src["kind"] == "webdav":
            return self._live_subs(iid, track["stream"], float((media or {}).get("start") or 0.0)), True
        else:
            text = self._extract(iid, track["stream"])
            if text is None:
                return None
        text = text[:VTT_MAX]
        self._cache_subs(cached, text)
        return text, False

    def _extract(self, iid: str, stream: int) -> str | None:
        """本机文件里的一路内嵌字幕整个抽成 WebVTT（经 raw_url，ffmpeg 只读这一路要的包，但还是要读一遍文件）。"""
        if not self.ffmpeg or self.secret is None or self.port is None:
            return None
        from ..proxyenv import loopback_bypass
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-protocol_whitelist", "http,tcp",
                "-i", self.raw_url(iid), "-map", f"0:{int(stream)}", "-c:s", "webvtt", "-f", "webvtt", "pipe:1"]
        with self._sub_lock:
            try:
                done = subprocess.run(args, capture_output=True, timeout=SUB_EXTRACT_S, env=loopback_bypass(os.environ))
            except (OSError, subprocess.SubprocessError) as error:
                self.log(f"片库抽不出字幕（{type(error).__name__}）")
                return None
        if done.returncode != 0 or not done.stdout.startswith(b"WEBVTT"):
            self.log("片库抽不出字幕（ffmpeg 出错）")
            return None
        return write_vtt(parse_vtt(done.stdout[:VTT_MAX].decode("utf-8", "replace")))

    def _live_subs(self, iid: str, stream: int, start: float) -> str:
        """网盘上的内嵌字幕：正在放的那一路边转边抽的（时间是文件里的原样，-copyts，减去文件的起始时间），
        合进之前抽到的（跳转重起时文件会被覆盖）。"""
        key = (iid, int(stream))
        try:
            fresh = parse_vtt(self._live_path(iid, stream).read_text(encoding="utf-8", errors="replace")[:VTT_MAX])
        except OSError:
            fresh = []
        with _LOCK:
            known = self._partial.setdefault(key, {})
            for cue_start, cue_end, body in fresh:
                if len(known) >= CUES_MAX:
                    break
                known[(round(cue_start - start, 3), round(cue_end - start, 3), body)] = True
            cues = sorted(known)
        return write_vtt(cues)

    def _cache_subs(self, path: Path, text: str) -> None:
        try:
            self.subs_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            return
        files = []
        for found in self.subs_dir.glob("*.vtt"):
            if found.name.endswith(".live.vtt"):
                continue
            try:
                info = found.stat()
            except OSError:
                continue
            files.append((info.st_mtime, info.st_size, found))
        total = sum(size for _, size, _ in files)
        for _, size, found in sorted(files):
            if total <= SUBS_CACHE_MAX:
                break
            try:
                found.unlink()
                total -= size
            except OSError:
                pass

    def serve_subs(self, handler, iid: str, n: int, after: float | None) -> None:
        """GET /api/library/subs/<id>/<n>.vtt[?after=秒]：WebVTT（after 给了只给在它之后结束的）。还在边放边抽的带
        X-RTV-Partial: 1（播放器隔一会儿再取）。"""
        found = self.subs_text(iid, n)
        if found is None:
            handler._send(404, "这条字幕放不了".encode(), "text/plain; charset=utf-8")
            return
        text, partial = found
        if after is not None:
            text = write_vtt([c for c in parse_vtt(text) if c[1] > after])
        handler._send(200, text.encode("utf-8"), "text/vtt; charset=utf-8",
                      {"X-RTV-Partial": "1" if partial else "0", "Cache-Control": "no-store"})

    # ---- 海报 ----

    def serve_art(self, handler, iid: str) -> None:
        """GET /api/library/art/<id>：本机的直接读；网盘的第一次要时取一次，存进 library-art/。"""
        item = self.item(iid)
        poster = (item or {}).get("poster") or ""
        src = next((s for s in self._load_sources() if item and s["id"] == item["source"]), None)
        kind = IMAGE_TYPES.get(_ext(poster))
        if item is None or src is None or not poster or kind is None:
            handler._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "private, max-age=600"})
            return
        data = self._art_bytes(src, poster)
        if data is None:
            handler._send(404, b"", "text/plain; charset=utf-8", {"Cache-Control": "private, max-age=600"})
            return
        handler._send(200, data, kind, {"Cache-Control": "private, max-age=86400"})

    def _art_bytes(self, src: dict, poster: str) -> bytes | None:
        if src["kind"] == "local":
            return LocalFolder(src.get("path") or "").read_small(poster, ART_MAX)
        name = f"{src['id']}-{hashlib.sha1(poster.encode('utf-8')).hexdigest()[:16]}{_ext(poster)}"
        cached = self.art_dir / name
        try:
            return cached.read_bytes()
        except OSError:
            pass
        failed = self._art_failed.get(name)
        if failed is not None and self.clock() - failed < ART_RETRY_S:
            return None
        with self._art_lock:
            data = self._source(src).read_small(poster, ART_MAX)
            if data is None:
                self._art_failed[name] = self.clock()
                return None
            try:
                self.art_dir.mkdir(parents=True, exist_ok=True)
                tmp = cached.with_name(name + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, cached)
                self._prune_art()
            except OSError:
                pass
            return data

    def _prune_art(self) -> None:
        files = []
        for path in self.art_dir.iterdir():
            try:
                info = path.stat()
            except OSError:
                continue
            files.append((info.st_mtime, info.st_size, path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= ART_CACHE_MAX:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                pass

    # ---- 播到小电视（第三步） ----

    def tv_command(self, iid: str, offset: float) -> list[str] | None:
        """小电视那一路的 ffmpeg 命令（tv_args）：读 raw_url（每次起 ffmpeg 都换新令牌），按探测的结果挑画面和声音；
        条目没了（重新扫描过）时 None。"""
        item = self.item(iid)
        if item is None or not self.ffmpeg or self.secret is None or self.port is None:
            return None
        hwaccel = tv_hwaccel(self.ffmpeg)
        media = self.with_color(iid, self.media_of(iid))
        return tv_args(self.ffmpeg, self.raw_url(iid), offset, media, item.get("ext") or "", hwaccel=hwaccel,
                       tonemap=tonemap_for(self.ffmpeg, first_video(media), hwaccel))

    def tv_title(self, item: dict) -> str:
        """小电视上显示的名字：电影是片名（对上豆瓣的用豆瓣的），剧集是「剧名 · 这一集」（这一集只是「第 n 集」时
        只写剧名，第几集另由 tv_episode 给设备画）。"""
        name = self.shown(item)["name"] or item.get("title") or ""
        if item.get("kind") != "episode":
            return name[:TITLE_MAX]
        title = item.get("title") or ""
        return (name if not title or _GENERIC_EPISODE.match(title) else f"{name} · {title}")[:TITLE_MAX]

    def neighbor(self, item: dict, step: int) -> dict | None:
        """同一部剧里的下一集（step 1）/ 上一集（−1）；不是剧集、已经是头一集 / 最后一集时 None。"""
        episodes = self.episodes_of(item)
        here = next((n for n, ep in enumerate(episodes) if ep["id"] == item["id"]), None)
        if here is None or not 0 <= here + step < len(episodes):
            return None
        return episodes[here + step]


# ---------------------------------------------------------------- 播到小电视（第三步）
#
# 小电视放片库的视频走和投屏一样的路：条目放进那台设备的投屏位（Video 的 site 是 "library"、bvid 是条目 id），
# 电视服务打开 /ch/<设备>/cast 时中转起 ffmpeg（web.py 的 _start → tv_start），读的仍是只给本机的 raw_url——
# 网盘的地址和密码从不在命令行上，只认片库里登记了的条目。暂停、跳转、下一个都是投屏那一套（掐断后从存的位置
# 重起一路）；剧集的「下一个」「上一个」换成同一部剧的下一集 / 上一集，放完一集自动接下一集，最后一集放完才
# 和投屏一样切回原来的台。不进投屏历史（片库的视频不能当链接重播）。
#
# 进度和电脑上的记在一起（pc-progress.json 的 lib:<id>，记在点播放的人名下，重启后是设备主人）：首页「继续观看」
# 显示它（写着在哪台小电视上看的），电脑上、小电视上都从那里接着放。访客永远放不了。
#
# 画面：电视服务按设备的 320×180 再缩一次（server/live.py 的 source_graph），这里给它不超过 640×360、至多 30 帧
# 的 H.264（ultrafast），4K HEVC Main 10 不整个推过去；H.264 8 位、不超过 1080 行的只换封装。macOS 上有
# videotoolbox 时用它解码（4K HEVC 的 CPU 从约一个核降到四分之一个核，见 CHANGELOG），没有时 ffmpeg 自己用软解。
# HDR 的片子（HDR10、杜比视界 8.1、HLG）缩小后做色调映射成 BT.709 的 SDR，不然发灰（见下面「HDR → SDR」）。
# 声音一律转成 AAC 立体声（DTS、TrueHD、E-AC-3 在 MPEG-TS 里电视服务未必认）；没有声音的补一路静音（电视服务
# 要画面和声音两路），没有画面的补黑场。

TV_WIDTH, TV_HEIGHT = 640, 360
TV_FPS_MAX = 30
TV_COPY_HEIGHT = 1080
# 放完了（ffmpeg 正常退出）却离结尾还远：多半是网盘半路断了，当作断了从那里接着放，不算看完。
# 开头这么多秒内就「放完」的照旧算放完（时长报错的文件不能一直在结尾重开）。
TV_END_SLACK_S = 60.0
TV_SHORT_S = 15.0
# 网盘读失败的记录比这一路开始早这么多秒以内也算这一路的（ffmpeg 起来之前的那次读）。
TV_FAIL_GRACE_S = 5.0
_GENERIC_EPISODE = re.compile(r"^第 \d+ 集$")
_HWACCELS: dict[str, frozenset] = {}


def hwaccels(ffmpeg: str, run=subprocess.run) -> frozenset:
    """这个 ffmpeg 支持的硬件解码（ffmpeg -hwaccels），每个 ffmpeg 只问一次；问不了时为空。"""
    found = _HWACCELS.get(ffmpeg)
    if found is not None:
        return found
    try:
        out = run([ffmpeg, "-hide_banner", "-hwaccels"], capture_output=True, timeout=10).stdout
        lines = (out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)).splitlines()
        found = frozenset(line.strip() for line in lines[1:] if line.strip())
    except (OSError, subprocess.SubprocessError):
        found = frozenset()
    _HWACCELS[ffmpeg] = found
    return found


def tv_hwaccel(ffmpeg: str, platform: str = sys.platform) -> str:
    """小电视那一路用的硬件解码：只在 macOS 上、这个 ffmpeg 有 videotoolbox 时（没有时传了 ffmpeg 会起不来）。"""
    if platform != "darwin" or not ffmpeg:
        return ""
    return "videotoolbox" if "videotoolbox" in hwaccels(ffmpeg) else ""


# ---- HDR → SDR（色调映射）
#
# 小电视（和电脑上转成 H.264 的那一路）是 8 位 BT.709 的 SDR。HDR10（PQ，杜比视界 8.1 的底层也是）和 HLG 的片子
# 不做色调映射直接缩成 8 位，PQ 的码值被当成 BT.709 的：发灰、没颜色（4K HDR10 的样片一帧平均饱和度 25，映射后
# 90 上下）。三种做法，按顺序挑这个 ffmpeg 做得了的（每个 ffmpeg 每种只实际跑一次小样试，tonemap_works）：
# - vt：macOS 的 videotoolbox，scale_vt 带 color_transfer 等（VTPixelTransferSession 在 GPU 上缩放、映射，高光
#   柔和地压下来）。硬件解码的帧直接进去，硬件解不了、ffmpeg 退回软解的经 hwupload 传上去（同一个设备的硬件帧
#   hwupload 原样放过），所以两种情况都能放。4K HDR10 的样片按小电视的参数转 6 秒：CPU 0.5 秒（不映射的 1.6 秒，
#   缩放也在 GPU 上了）。只用于 4:2:0 的 10 位画面（取回来的是 p010）。
# - sws：ffmpeg 8 起 swscale 自己的色彩管理（scale 的 out_transfer 等，intent=perceptual 是按母版的峰值亮度
#   压高光；默认的 relative_colorimetric 是剪掉）。先缩到 640×360 再映射；起来时建 3D 查找表要约 5 个 CPU 秒
#   （半秒多），之后每秒视频多花不到 0.05 个 CPU 秒。
# - zscale：带 zimg 的 ffmpeg（老一些的静态版本）的老办法，zscale 转成线性光 + tonemap=hable。
# 都不行（ffmpeg 太老）时照旧不映射。只有探测到传输特性是 HDR 的（is_hdr）才映射：不知道的当 SDR。

HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
# 电脑上转码的那一路（watch.ENCODE_SCALE：最高 720p）用 scale_vt 时的大小。
PC_VT_SIZE = "w=-2:h='min(720,ih)'"
TONEMAP_KINDS = ("vt", "sws", "zscale")
VT_DEVICE = "rootvt"
_TO_709 = "out_transfer=bt709:out_primaries=bt709:out_color_matrix=bt709:out_range=tv"
_TONEMAPS: dict[tuple[str, str], bool] = {}
_TONEMAPS_BASE: dict[str, float] = {}
# 试用的小样：一块橙色，10 位、标成 BT.2020 / PQ。不映射时出来的饱和度约 45，映射的约 90–115。
_TONEMAP_SAMPLE = ("color=c=0xa05030:s=64x64:r=1:d=1,format=yuv420p10le,"
                   "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:range=tv")
_TONEMAP_GAIN = 1.4


def is_hdr(stream: dict | None) -> bool:
    """这一路画面是 HDR 的（PQ / HLG）：探测到的传输特性说了算，没探测到的当 SDR。"""
    return bool(stream) and stream.get("trc") in HDR_TRANSFERS


def tonemap_vf(kind: str, sw_scale: str, vt_size: str) -> str:
    """缩放加色调映射的 -vf（出来是 8 位 yuv420p 的 BT.709）。sw_scale 是软件缩放（scale=…），vt_size 是 scale_vt
    的 w=…:h=…（两个缩出来一样大）；kind 不认识时空的。都是先缩小再映射。"""
    if kind == "vt":
        # hwdownload 取回的帧标着 BT.709，后面的 scale 只转成 8 位（不写 out_color_matrix 时它会照输入那一头
        # 商量好的 bt2020nc 再转一次矩阵）。
        return (f"hwupload,scale_vt={vt_size}:color_matrix=bt709:color_primaries=bt709:color_transfer=bt709,"
                "hwdownload,format=p010le,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")
    if kind == "sws":
        return f"{sw_scale},format=yuv420p10le,scale={_TO_709}:intent=perceptual,format=yuv420p"
    if kind == "zscale":
        return (f"{sw_scale},zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    return ""


def tonemap_input(kind: str, hwaccel: str = "") -> list[str]:
    """色调映射要在 -i 前面加的参数：vt 要一个 videotoolbox 设备给 hwupload / scale_vt，有硬件解码（hwaccel）时
    解出来的帧留在 GPU 上（-hwaccel_output_format），不先取回来。"""
    if kind != "vt":
        return []
    args = ["-init_hw_device", f"videotoolbox={VT_DEVICE}", "-filter_hw_device", VT_DEVICE]
    if hwaccel == "videotoolbox":
        args += ["-hwaccel", "videotoolbox", "-hwaccel_device", VT_DEVICE, "-hwaccel_output_format", "videotoolbox_vld"]
    return args


def _saturation(ffmpeg: str, kind: str, run) -> float | None:
    """小样过 kind 的映射（kind 为空：不映射，只转成 8 位）后的平均饱和度；ffmpeg 出错、没有这个滤镜时 None。"""
    chain = tonemap_vf(kind, "scale=64:64", "w=64:h=64") if kind else "format=yuv420p"
    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *tonemap_input(kind),
            "-f", "lavfi", "-i", _TONEMAP_SAMPLE, "-frames:v", "1",
            "-vf", chain + ",signalstats,metadata=print:file=-", "-f", "null", "-"]
    try:
        done = run(args, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    out = b"".join(x for x in (done.stdout, done.stderr) if isinstance(x, bytes)).decode("utf-8", "replace")
    found = re.search(r"SATAVG=([0-9.]+)", out)
    return float(found.group(1)) if done.returncode == 0 and found else None


def tonemap_works(ffmpeg: str, kind: str, run=subprocess.run) -> bool:
    """这个 ffmpeg 做不做得了 kind 的色调映射：实际跑一次小样，出来的饱和度要比不映射的高出一截（不只是认得
    这些选项）。每个 ffmpeg、每种只试一次。"""
    key = (ffmpeg, kind)
    found = _TONEMAPS.get(key)
    if found is None:
        base = _TONEMAPS_BASE.get(ffmpeg)
        if base is None:
            base = _TONEMAPS_BASE[ffmpeg] = _saturation(ffmpeg, "", run) or 0.0
        mapped = _saturation(ffmpeg, kind, run) if base > 0 else None
        found = _TONEMAPS[key] = mapped is not None and mapped >= base * _TONEMAP_GAIN
    return found


def tonemap_for(ffmpeg: str, video: dict | None, hwaccel: str = "", run=subprocess.run) -> str:
    """HDR 的画面用哪种色调映射（"vt" / "sws" / "zscale"）；不是 HDR、这个 ffmpeg 哪种都做不了时空的。
    vt 只在有 videotoolbox 硬件解码（tv_hwaccel，只在 macOS 上）、画面是 4:2:0 时用。"""
    if not ffmpeg or not is_hdr(video):
        return ""
    pix = video.get("pix") or ""
    for kind in TONEMAP_KINDS:
        if kind == "vt" and (hwaccel != "videotoolbox" or (pix and "420" not in pix and pix != "p010le")):
            continue
        if tonemap_works(ffmpeg, kind, run=run):
            return kind
    return ""


def tv_args(ffmpeg: str, url: str, offset: float, media: dict | None, ext: str, hwaccel: str = "",
            tonemap: str = "") -> list[str]:
    """片库的视频推给电视服务的 ffmpeg 参数：按实时速度（-re）读 url（只给本机的 raw 地址），从 offset 秒起，
    写 MPEG-TS 到标准输出（和 stream.py 的一样，由 stream.pump 转给电视服务）。tonemap：HDR 的画面用哪种色调映射
    （tonemap_for；空的不映射）。"""
    video = first_video(media)
    audio, _ = audio_choice(media, ext)
    known = bool((media or {}).get("streams"))
    copy = video is not None and video.get("c") == "h264" and not _deep(video) \
        and 0 < int(video.get("h") or 0) <= TV_COPY_HEIGHT
    tonemap = tonemap if video is not None and not copy and tonemap in TONEMAP_KINDS else ""
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if tonemap == "vt":
        args += tonemap_input(tonemap, hwaccel)
    elif hwaccel and not copy and (video is not None or not known):
        args += ["-hwaccel", hwaccel]
    args += ["-re", "-protocol_whitelist", "http,tcp", "-ss", f"{max(0.0, float(offset)):.3f}", "-i", url]
    if known and video is None:
        # 只有声音的文件：补一路黑场。
        args += ["-f", "lavfi", "-i", f"color=c=black:s={TV_WIDTH}x{TV_HEIGHT}:r=25"]
    if known and audio is None:
        # 没有声音：补一路静音。
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    if known:
        args += ["-map", f"0:{video['i']}" if video is not None else "1:v",
                 "-map", f"0:{audio['i']}" if audio is not None else f"{1 if video is not None else 2}:a"]
        if video is None or audio is None:
            args += ["-shortest"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0?"]
    if copy:
        args += ["-c:v", "copy"]
    else:
        scale = f"scale={TV_WIDTH}:{TV_HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=bilinear"
        # scale_vt 没有 force_original_aspect_ratio：比 16:9 宽的按宽缩到 640，别的按高缩到 360（-2：偶数）。
        fit = f"gte(iw*{TV_HEIGHT},ih*{TV_WIDTH})"
        vt_size = f"w='if({fit},{TV_WIDTH},-2)':h='if({fit},-2,{TV_HEIGHT})'"
        args += ["-vf", tonemap_vf(tonemap, scale, vt_size) if tonemap else f"{scale},format=yuv420p",
                 "-fpsmax", str(TV_FPS_MAX), "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                 "-crf", "23", "-maxrate", "2M", "-bufsize", "4M", "-g", "60", "-threads", "2"]
    args += ["-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "128k", "-sn", "-dn",
             "-f", "mpegts", "pipe:1"]
    return args


def start_tv_process(args: list[str]) -> subprocess.Popen:
    """起小电视那一路的 ffmpeg：参数列表、不经 shell；回环地址不走代理（proxyenv）。"""
    from ..proxyenv import loopback_bypass
    return subprocess.Popen(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=loopback_bypass(os.environ))


def tv_start(relay, iid: str, offset: float):
    """web.py 的 _start：投屏位里是片库的视频时起 ffmpeg。relay.library_starter（测试换成假的）收参数列表。"""
    from .bilibili import SourceError
    library = wired(relay)
    args = library.tv_command(iid, offset) if library is not None else None
    if args is None:
        raise SourceError("片库里没有这个视频了", permanent=True)
    starter = getattr(relay, "library_starter", None) or start_tv_process
    return starter(args)


def _viewers(relay) -> dict:
    """每台设备（slot）上片库的视频是谁点的：进度记在他名下；重启后没有记录时记给设备主人。"""
    return relay.__dict__.setdefault("_library_viewers", {})


def _tv_video(library: "Library", item: dict):
    from .state import Video
    duration = float((library.media_of(item["id"]) or {}).get("dur") or 0)
    return Video(bvid=item["id"], title=library.tv_title(item), duration=int(duration), pubdate=0, site="library")


def _tv_cast(relay, dev: dict, library: "Library", item: dict, position: float = 0.0):
    """把 item 放进 dev 的投屏位（从 position 秒接着放）；返回放进去的 Video。"""
    video = _tv_video(library, item)
    cast = relay._cast_of(dev)
    cast.set_cast(video)
    if position > 0:
        cast.save_cast_position(video.bvid, position)
    cast.save()
    return video


def play_tv(relay, caller, dev: dict, iid) -> dict:
    """POST /api/play 播到小电视（home.play）：放进它的投屏位，从上次看到的地方接着放（电脑上、小电视上看的都算），
    快放完了的从头放。访客不行。"""
    library = wired(relay)
    if library is None or caller.guest or caller.user is None:
        return {"ok": False, "reason": "failed", "error": "片库只给家里人看"}
    item = library.item(iid) if isinstance(iid, str) else None
    if item is None:
        return {"ok": False, "reason": "failed", "error": "片库里没有这个视频（可能刚重新扫描过，刷新一下）"}
    if not Library.playable(item):
        return {"ok": False, "reason": "failed", "error": "这个文件放不了（里面没有画面也没有声音）"}
    key = "lib:" + item["id"]
    store = getattr(relay, "pc_progress", None)
    # 电脑上的游标（_pc_cursor）会被 pc_sync 一直写回去：换到小电视上看了，就不再要它。
    relay._pc_cursor.pop((caller.user, key), None)
    saved = store.get(caller.user, key) if store is not None else None
    position = float((saved or {}).get("position") or 0)
    video = _tv_video(library, item)
    if video.duration and position >= video.duration - 5:
        position = 0.0
    _viewers(relay)[dev["slot"]] = caller.user
    _tv_cast(relay, dev, library, item, position)
    relay.log(f"片库的「{item['title']}」播到{dev['name']}" + (f"，从 {int(position)} 秒接着放" if position else ""))
    relay._event(dev, "cast")
    answer = {"ok": True, "title": video.title, "at": relay.clock()}
    if position:
        answer["resume"] = round(position, 1)
    return answer


def tv_progress(relay, dev: dict, video, position: float, done: bool = False) -> None:
    """小电视上看到了 position 秒（done：这一集看完了）：记进 pc-progress.json 的 lib:<id>，「继续观看」里写设备名。"""
    store, library = getattr(relay, "pc_progress", None), getattr(relay, "library", None)
    user = _viewers(relay).get(dev.get("slot")) or dev.get("owner")
    if store is None or library is None or user is None:
        return
    item = library.item(video.bvid)
    if item is None:
        return
    key = "lib:" + item["id"]
    duration = float(video.duration or 0)
    if done:
        store.mark_done(user, key, item["id"])
        position = duration or position
    shown = library.shown(item)
    name = shown["name"] or item.get("title") or ""
    store.put(user, key, {"channel": "cast", "bvid": item["id"], "site": "library",
                          "title": item.get("title") if item.get("kind") == "episode" else name, "name": name,
                          "duration": duration, "position": round(max(0.0, float(position)), 1),
                          "device": dev.get("name") or "小电视"})


def tv_finished(relay, dev: dict, video, offset: float, outcome: str, elapsed: float) -> str:
    """小电视上片库的这一路停了（web.py 的 finish_cast）：记进度；放完一集、有下一集时换成下一集（"next"，电视服务
    重新打开投屏台就是它，不切回原来的台）。离结尾还远就「放完」的当作断了（"failed"）。返回 finish_cast 接着用的结果。"""
    position = offset + elapsed
    library = getattr(relay, "library", None)
    early = outcome == "ended" and video.duration and position < video.duration - TV_END_SLACK_S
    # 一开头就「放完」的照旧算放完（见 TV_SHORT_S），除非这一路里网盘明说读不了（组件还没起来、断网）：
    # 那是断了，不是放完了，不能一集一集往下跳。
    broken = early and library is not None and library.drive_failed_since(video.bvid, time.monotonic() - elapsed - TV_FAIL_GRACE_S)
    if early and (elapsed >= TV_SHORT_S or broken):
        relay.log(f"片库的一路在 {int(position)} 秒提前结束了（共 {int(video.duration)} 秒），从那里接着放")
        outcome = "failed"
    if outcome != "ended":
        tv_progress(relay, dev, video, position)
        return outcome
    tv_progress(relay, dev, video, position, done=True)
    item = library.item(video.bvid) if library is not None else None
    upcoming = library.neighbor(item, 1) if item is not None else None
    if upcoming is None:
        return outcome
    fresh = _tv_cast(relay, dev, library, upcoming)
    relay.log(f"片库：接着放下一集「{upcoming['title']}」")
    relay._loading(dev, CAST_ID, relay._state_of(dev), fresh)
    return "next"


def tv_step(relay, dev: dict, video, step: int, entry: dict | None) -> dict | None:
    """遥控的「下一个」（step 1）/「上一个」（−1）在片库的剧集上：换成同一部剧的下一集 / 上一集，掐断正在推的那一路
    （电视服务重新打开投屏台就是它）。没有那一集时 None（「下一个」照投屏的办法收场，「上一个」说已经是第 1 集）。
    往后换时这一集算看完了。"""
    library = getattr(relay, "library", None)
    item = library.item(video.bvid) if library is not None else None
    target = library.neighbor(item, step) if item is not None else None
    if target is None:
        return None
    if step > 0:
        tv_progress(relay, dev, video, video.duration, done=True)
    if entry is not None:
        # 先标上，免得旧的那一路的 tick 又把进度存回去。
        entry["abort"] = "换一集"
    fresh = _tv_cast(relay, dev, library, target)
    relay._bridge.pop((dev["slot"], CAST_ID), None)
    relay._loading(dev, CAST_ID, relay._state_of(dev), fresh)
    if relay._paused_on(dev, CAST_ID) is not None:
        # 暂停着换集：结束暂停，电视服务重新打开投屏台就是换好的那一集。
        relay._paused.pop(dev["slot"], None)
        relay._event(dev, "resume")
    elif entry is not None:
        relay._cut(CAST_ID, entry, "换到下一集" if step > 0 else "换到上一集")
    return {"ok": True, "at": relay.clock(), "title": fresh.title}


def tv_episode(relay, video) -> dict:
    """设备上「正在加载 / 正在播」要画的第几集、共几集（web.py 的 _describe）；不是剧集或只有一集时为空。"""
    library = getattr(relay, "library", None)
    item = library.item(video.bvid) if library is not None else None
    episodes = library.episodes_of(item) if item is not None else []
    if len(episodes) < 2:
        return {}
    n = next((n for n, ep in enumerate(episodes, 1) if ep["id"] == item["id"]), 0)
    return {"episode": n, "total": len(episodes)} if n else {}


# ---------------------------------------------------------------- HTTP

_FILE = re.compile(r"^/api/library/file/([0-9a-f]{16})$")
_ART = re.compile(r"^/api/library/art/([0-9a-f]{16})$")
_SUBS = re.compile(r"^/api/library/subs/([0-9a-f]{16})/(\d{1,3})\.vtt$")
_RAW = re.compile(r"^/api/library/raw/([0-9a-f]{16})$")
_META_ART = re.compile(r"^/api/library/meta-art/((?:s[0-9a-f]{12}|m[0-9a-f]{16}))$")
_THUMB = re.compile(r"^/api/admin/library/douban/thumb/(\d{1,12})$")
_TMDB_THUMB = re.compile(r"^/api/admin/library/tmdb/thumb/(tv|movie)-(\d{1,10})$")


def wired(relay) -> "Library | None":
    """relay.library，设好 ffmpeg、令牌密钥和端口（attach 已经设过；测试里直接挂上去的在这里补上）。"""
    library = getattr(relay, "library", None)
    if library is not None and library.secret is None:
        library.wire(getattr(relay, "ffmpeg", ""), getattr(relay, "key", ""),
                     lambda: getattr(relay, "manage_port", 0))
    return library


def loopback(handler) -> bool:
    """这个请求是本机直接连进来的（中转自己的 ffmpeg）：对方是 127.0.0.1 / ::1，而且不是经前端转进来的
    （server/front.py 一定加 X-Real-IP 和 X-Forwarded-Proto）。"""
    peer = (handler.client_address or ("",))[0]
    return peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1") and not any(handler.headers.get(h) for h in FORWARDED_HEADERS)


def raw(handler, relay, path: str) -> bool:
    """GET /api/library/raw/<id>?t=<令牌>：中转自己的 ffmpeg 取片库的文件（按 Range）。只认本机直接连进来的、
    令牌对得上且没过期的；别的一律 404。不看登录（ffmpeg 没有 cookie），在登录检查之前处理。"""
    if not path.startswith("/api/library/raw/"):
        return False
    library = wired(relay)
    match = _RAW.match(path)
    if library is None or match is None or not loopback(handler) \
            or not library.raw_ok(match.group(1), handler._query().get("t")):
        handler._not_found()
        return True
    library.serve_file(handler, match.group(1), raw=True, warm=handler._query().get("w") == "1")
    return True


def get(handler, relay, caller, path: str) -> bool:
    """GET /api/library、/api/library/file/<id>、/api/library/art/<id>：只给登录的家里人（访客、没登录、云端 404）。"""
    if path != "/api/library" and not path.startswith("/api/library/"):
        return False
    library = getattr(relay, "library", None)
    if library is None or caller is None or caller.guest or caller.user is None:
        handler._not_found()
        return True
    if path == "/api/library":
        fresh = handler._query().get("open") == "1" and library.freshen()
        answer = library.view()
        if fresh:
            # 后台线程可能刚把它从队里取走、还没标「正在扫」：照样告诉页面「排上了」，页面才会接着刷新。
            answer["scan"]["queued"] = max(1, answer["scan"].get("queued") or 0)
        answer["admin"] = caller.role == "admin"
        if answer["admin"]:
            # 备用的 TMDB：只说填了没有、有没有出错，从不带密钥。
            answer["tmdb"] = library.tmdb.state()
        handler._json(answer)
        return True
    match = _FILE.match(path)
    if match:
        library.serve_file(handler, match.group(1))
        return True
    match = _ART.match(path)
    if match:
        library.serve_art(handler, match.group(1))
        return True
    match = _META_ART.match(path)
    if match:
        library.serve_meta_art(handler, match.group(1))
        return True
    match = _SUBS.match(path)
    if match:
        after = handler._query().get("after")
        try:
            after = float(after) if after is not None else None
        except ValueError:
            after = None
        wired(relay).serve_subs(handler, match.group(1), int(match.group(2)),
                                after if after is not None and math.isfinite(after) else None)
        return True
    handler._not_found()
    return True


def home_rows(relay, caller) -> list[dict]:
    """首页「我的片库」那一排：最近加进来的。"""
    library = getattr(relay, "library", None)
    if library is None or caller.guest or caller.user is None:
        return []
    rows = []
    for item in library.recent():
        episode = item["kind"] == "episode"
        shown = library.shown(item)
        rows.append({"id": f"lib:{item['id']}", "kind": "library", "title": shown["name"],
                     "subtitle": item["title"] if episode else (str(item["year"]) if item.get("year") else "片库"),
                     "source": "library", "playable": Library.playable(item),
                     "art": {"url": shown["art"], "ratio": "poster"},
                     "play": {"kind": "library", "id": item["id"], "playable": Library.playable(item)}})
    return rows


def _proxy(relay) -> str:
    """TMDB 经「设置 → YouTube」里填的代理（和网盘组件的下载一样，clouddrive._proxy）；没填、读不了设置是 ""。"""
    if getattr(relay, "selfhost_data", None) is None:
        return ""
    try:
        from . import ytsettings
        config, _ = ytsettings._config(relay)
    except Exception:  # noqa: BLE001 —— 读不了设置就不经代理，TMDB 连不上也只是没有备用的资料
        return ""
    return config.youtube_proxy or ""


def attach(relay, start: bool = True) -> Library | None:
    """自建 / 电脑客户端：挂在 relay.library 上，开定时扫描。云端不建。"""
    if relay.selfhost is None or relay.accounts is None:
        return None
    library = Library(Path(relay.accounts.path).parent, log=lambda message: relay.log(message))
    library.tmdb.proxy = lambda: _proxy(relay)
    relay.library = library
    wired(relay)
    library.watching = lambda: _watching(relay)
    if start:
        threading.Thread(target=library.run_forever, name="library", daemon=True).start()
        threading.Thread(target=library.run_meta, name="library-meta", daemon=True).start()
        threading.Thread(target=library.run_warm, name="library-warm", daemon=True).start()
    return library


def _watching(relay) -> bool:
    """电脑上有人在看（或正在起播）：直播、B 站、片库，watch.py 的哪一路都算（预热不和它抢网）。"""
    watch = getattr(relay, "watch", None)
    return watch is not None and any(stream.alive for stream in watch.streams())
