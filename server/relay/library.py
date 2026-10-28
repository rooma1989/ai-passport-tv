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
- 元数据只在本地：.nfo（标题、年份、简介）、视频旁边的海报（poster.jpg、folder.jpg、<名字>-poster.jpg、
  <名字>.jpg）、文件名（电影「名字 (2019)」「名字.2019.1080p…」；剧集 S01E02、EP03、第3集、E03，按剧名或上一层
  文件夹归成一部剧）。不上网刮削。网盘上的文件从不截图（网盘会限速）；这一步本机的也不截，没海报的用页面自己画的图。
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
  ffmpeg 的命令行上，本机的文件也不用 file: 协议。
- 字幕（第二步）：内嵌的文字字幕（ass、srt、mov_text、webvtt）和视频旁边的字幕文件（<名字>.srt、<名字>.zh.ass…，
  本机和网盘）按需转成 WebVTT（/api/library/subs/<id>/<n>.vtt，只给家里人，缓存在 library-subs/）。图片字幕（PGS、
  DVD）列出来但放不了。网盘上的内嵌字幕不整个再读一遍文件，而是在放的那一路边转边抽（watch_source 的 subs_out）。
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
import shutil
import stat
import subprocess
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
# 定时扫描：成功的隔 EVERY_S，失败的隔 RETRY_S 再试；每 TICK_S 看一眼；启动后先等 START_DELAY_S。
EVERY_S = 12 * 3600.0
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
# 没有年份时，标题到第一个这类标记为止。
_JUNK = re.compile(r"(?i)(?<![0-9a-z])(2160p|1080[pi]|720p|480p|4k|uhd|blu-?ray|bdrip|brrip|web-?dl|webrip|hdtv|"
                   r"dvdrip|x26[45]|h\.?26[45]|hevc|avc|aac|ac3|dts|hdr|10bit|60fps)(?![0-9a-z])")
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
    text = " ".join(text.split()).strip(" -–—·._[(（【")
    return text[:TITLE_MAX]


def _movie_title(stem: str) -> tuple[str, int | None]:
    """(标题, 年份)：年份前面的就是标题；没有年份时到第一个画质标记为止。"""
    for match in _YEAR.finditer(stem):
        title = clean_title(stem[:match.start()])
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
    """从路径（相对来源的根，用 / 分隔）猜：{kind: movie | episode | video, title, year, show, season, episode}。"""
    parts = [p for p in rel.split("/") if p]
    name = parts[-1] if parts else rel
    stem = os.path.splitext(name)[0]
    parent = parts[-2] if len(parts) >= 2 else ""
    grand = parts[-3] if len(parts) >= 3 else ""
    found = {"kind": "video", "title": "", "year": None, "show": "", "season": None, "episode": None}
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
        show = clean_title(stem[:cut]) if cut > 0 else ""
        show = re.sub(r"(?i)[ ._-]*(?:s\d{1,2}|第[0-9一二三四五六七八九十]+季)$", "", show).strip(" -._")
        # 剧名：文件名里有就用；没有就用文件夹（季的文件夹再往上一层）。
        folder_season = _season_of(parent) if parent else None
        folder = grand if folder_season is not None and grand else parent
        if season is None:
            season = folder_season
        if not show and folder:
            show = _movie_title(folder)[0]
        tail = clean_title(_JUNK.split(stem[cut:], 1)[0])
        found.update(kind="episode", show=show or "未命名的剧", season=season, episode=episode,
                     title=f"第 {episode} 集")
        # 集数后面跟着的集名（「S01E02 重逢」）。
        rest = re.sub(r"^(?i:s\d{1,2}[ ._-]?e\d{1,4}|第\s*\S{1,6}?\s*[集话話期]|(?:episode|ep|e)[ ._-]?\d{1,4}|\d{1,3})",
                      "", tail).strip(" -._")
        if rest and not _YEAR.fullmatch(rest):
            found["title"] = f"第 {episode} 集 · {rest}"[:TITLE_MAX]
        return found
    title, year = _movie_title(stem)
    found.update(kind="movie" if year else "video", title=title, year=year)
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


# ---------------------------------------------------------------- 媒体（第二步）：探测、怎么放、字幕

# 探测的结果存在索引的条目里（media）：格式变了（这里的写法改了）就加一，旧的重新探测。
PROBE_VERSION = 1
PROBE_TIMEOUT_S = 25
# 探测读多少：文件头里就有各路流（mp4 的 moov 再大 ffmpeg 也会整个读，不受这个限制）。
PROBE_ARGS = ("-probesize", "5000000", "-analyzeduration", "5000000")
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
                pic: bool = False) -> dict:
    """索引里的一路流（只留用得上的几项，空的不写）：i 序号、t 种类、c 编码、p profile、pix、w / h、ch 声道、
    lang、title、def 默认、forced 强制、pic 封面图。"""
    row: dict = {"i": int(index), "t": kind, "c": (codec or "").lower()[:32]}
    for key, value in (("p", profile), ("pix", pix), ("lang", lang), ("title", title)):
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
                                       bool(flags.get("attached_pic"))))
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
            pix = re.search(r",\s*((?:yuv|yuvj|gray|rgb|bgr|nv|p0)[a-z0-9]*)", rest)
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
                                  "(attached pic)" in rest)
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
        self._read: dict[str, int] = {}
        self._sub_lock = threading.Lock()
        self._partial: dict[tuple, dict] = {}

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
        self._write(self.sources_path, {"sources": sources})

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
        return {"ok": True, "max": SOURCES_MAX, "every_h": int(EVERY_S // 3600), "sources": rows, "scan": self.status()}

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
        for item in items:
            card = self.card(item, names.get(item["source"], ""))
            if item["kind"] == "episode":
                key = self.show_key(item.get("show") or "")
                show = shows.setdefault(key, {"id": key, "title": item.get("show") or "", "count": 0, "art": "",
                                              "seasons": set(), "added": 0, "episodes": []})
                show["episodes"].append(card)
                show["count"] += 1
                show["seasons"].add(item.get("season") or 1)
                show["added"] = max(show["added"], card["added"])
            elif item["kind"] == "movie":
                movies.append(card)
            else:
                videos.append(card)
        show_rows = []
        for show in shows.values():
            show["episodes"].sort(key=lambda c: (c["season"] or 0, c["episode"] or 0, c["title"]))
            show["art"] = next((c["art"] for c in show["episodes"] if c["art"]), "")
            show["seasons"] = len(show["seasons"])
            show["playable"] = any(c["playable"] for c in show["episodes"])
            show_rows.append(show)
        movies.sort(key=lambda c: c["title"].casefold())
        videos.sort(key=lambda c: c["title"].casefold())
        show_rows.sort(key=lambda s: s["title"].casefold())
        return {"ok": True, "sources": [self._public_source(s, counts.get(s["id"], 0)) for s in sources],
                "scan": self.status(), "movies": movies, "shows": show_rows, "videos": videos,
                "total": sum(counts.values()), "more": more}

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

    def walk(self, source) -> tuple[list[Entry], bool]:
        """一层一层（广度优先）走一遍：(文件, 是不是到了上限)。根列不出来抛 LibraryError。"""
        files: list[Entry] = []
        queue: deque[tuple[str, int]] = deque([("", 0)])
        walked = skipped = 0
        truncated = False
        while queue and not truncated:
            rel, depth = queue.popleft()
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
            if previous is not None and previous.get("size") == entry.size and previous.get("mtime") == entry.mtime:
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
            items.append({"id": iid, "source": sid, "path": entry.rel, "title": found["title"] or stem,
                          "year": found.get("year"), "kind": found["kind"], "show": found.get("show") or "",
                          "season": found.get("season"), "episode": found.get("episode"),
                          "plot": found.get("plot") or "", "size": entry.size, "mtime": entry.mtime, "ext": ext,
                          "playable": ext in VIDEO_TYPES, "poster": poster, "subs": subs,
                          "added": (previous or {}).get("added") or now})
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
            files, truncated = self.walk(source)
            with _LOCK:
                old = {i: dict(item) for i, item in self._items.items() if item["source"] == sid}
            items = self.build(sid, source, files, old)
        except LibraryError as error:
            reason = str(error)
            if src.get("managed") == MANAGED and reason == "网盘出错了（500）":
                # OpenList 里还没有挂任何网盘时 PROPFIND / 回答 500（storage not found）。
                reason = "网盘组件里还没有添加网盘：打开网盘管理，添加百度网盘或夸克"
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
        self.log(f"片库「{name}」扫完了：{len(items)} 个视频（新的 {fresh} 个）" + (f"，{note}" if note else ""))
        return {"ok": True, "count": len(items), "new": fresh, "note": note}

    # ---- 定时 ----

    def due(self) -> list[str]:
        now = self.clock()
        return [s["id"] for s in self._load_sources()
                if now - (s.get("last_scan") or 0) >= (RETRY_S if s.get("ok") is False else EVERY_S)]

    def run_forever(self, sleep=time.sleep) -> None:
        sleep(START_DELAY_S)
        while True:
            try:
                for sid in self.due():
                    self.scan(sid)
            except Exception as error:  # noqa: BLE001
                self.log(f"片库定时扫描出错：{type(error).__name__}")
            sleep(TICK_S)

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
    def _pump(handler, read, expected: int | None, tally: Callable[[int], None] | None = None) -> None:
        sock = getattr(handler, "connection", None)
        total = 0
        while expected is None or total < expected:
            if _client_gone(sock):
                break
            chunk = read(CHUNK if expected is None else min(CHUNK, expected - total))
            if not chunk:
                break
            handler.wfile.write(chunk)
            total += len(chunk)
            if tally is not None:
                tally(len(chunk))
        if expected is not None and total < expected:
            handler.close_connection = True

    def serve_file(self, handler, iid: str, raw: bool = False) -> None:
        """GET /api/library/file/<id>：按 Range 给文件的字节（只认 id）。raw：给中转自己的 ffmpeg 的（/api/library/raw），
        记下给了多少字节（起播时的进度）。"""
        item = self.item(iid)
        src = next((s for s in self._load_sources() if item and s["id"] == item["source"]), None)
        if item is None or src is None:
            handler._not_found()
            return
        if not self._files.acquire(blocking=False):
            handler._send(503, "同时在放的太多了，过一会儿再试".encode(), "text/plain; charset=utf-8")
            return
        try:
            kind = VIDEO_TYPES.get(item.get("ext") or "", "application/octet-stream")
            tally = self._tally(iid) if raw else None
            if src["kind"] == "local":
                self._serve_local(handler, LocalFolder(src.get("path") or ""), item, kind, tally)
            else:
                self._serve_dav(handler, self._source(src), item, kind, src.get("name") or "", tally)
        except (OSError, ValueError):
            handler.close_connection = True        # 浏览器断开（跳转、关窗口）
        finally:
            self._files.release()

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

    def _serve_dav(self, handler, dav: WebDav, item: dict, kind: str, name: str, tally=None) -> None:
        wanted = (handler.headers.get("Range") or "").strip()
        wanted = wanted if _RANGE.match(wanted) and wanted != "bytes=-" else None
        try:
            upstream = dav.open(item["path"], wanted)
        except urllib.error.HTTPError as error:
            extra = {"Content-Range": error.headers["Content-Range"]} \
                if error.code == 416 and error.headers.get("Content-Range") else {}
            error.close()
            if error.code == 416:
                handler._send(416, b"", "text/plain; charset=utf-8", extra)
                return
            reason = _dav_reason(error)
            if error.code == 403:
                reason = "网盘拒绝了（在 OpenList 里把这个存储的 WebDAV 策略改成「本地代理」再试）"
            self.log(f"片库「{name}」放不了一个文件：{reason}")
            handler._send(502, reason.encode(), "text/plain; charset=utf-8")
            return
        except (LibraryError, OSError, ValueError) as error:
            reason = str(error) if isinstance(error, LibraryError) else _dav_reason(error)
            self.log(f"片库「{name}」放不了一个文件：{reason}")
            handler._send(502, reason.encode(), "text/plain; charset=utf-8")
            return
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
            self._pump(handler, read, expected, tally)
        finally:
            upstream.close()

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

    def raw_url(self, iid: str) -> str:
        """中转自己的 ffmpeg 取这个文件的地址：只在本机（127.0.0.1）、带短期令牌。网盘的地址和密码从不给 ffmpeg，
        本机的文件也不用 file: 协议（watch.py 只许网络协议）。"""
        return f"http://127.0.0.1:{int(self.port())}/tv/api/library/raw/{iid}?t={self.raw_token(iid)}"

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

    def media_of(self, iid: str) -> dict | None:
        """这个条目探测到的各路流（缓存在索引里，大小和修改时间变了才重新探测）；探测不了时 None（不缓存，
        PROBE_RETRY_S 内不再试）。在请求的线程里做（网盘上的要几秒），同一个条目同时只探测一次。"""
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
            found = self.probe(iid)
            if found is None:
                with _LOCK:
                    self._probe_failed[iid] = self.clock()
                return None
            found.update(v=PROBE_VERSION, size=item.get("size"), mtime=item.get("mtime"))
            with _LOCK:
                if iid in self._items:
                    self._items[iid]["media"] = found
                    self._save_index()
            return found

    def probe(self, iid: str) -> dict | None:
        """探测一次（ffprobe，没有时用 ffmpeg 自己打的输入信息），读的是 raw_url。出错、超时时 None。"""
        if not self.ffmpeg or self.secret is None or self.port is None:
            return None
        from ..proxyenv import loopback_bypass
        url = self.raw_url(iid)
        probe = _ffprobe_of(self.ffmpeg)
        whitelist = ["-protocol_whitelist", "http,tcp"]
        if probe:
            args = [probe, "-v", "error", *whitelist, *PROBE_ARGS, "-show_entries",
                    "format=format_name,duration,start_time:stream=index,codec_type,codec_name,profile,pix_fmt,"
                    "width,height,channels:stream_disposition=default,forced,attached_pic:"
                    "stream_tags=language,title", "-of", "json", url]
        else:
            args = [self.ffmpeg, "-hide_banner", "-nostdin", *whitelist, *PROBE_ARGS, "-i", url]
        try:
            done = subprocess.run(args, capture_output=True, timeout=PROBE_TIMEOUT_S, env=loopback_bypass(os.environ))
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
        if decision.get("generic"):
            mode, copy = None, ("h264",)
        else:
            mode = watch_pc.Mode("copy" if decision["vcopy"] else "encode" if video is not None else None,
                                 ("copy" if decision["acopy"] else "encode") if audio is not None else None)
            copy = ("h264", decision["vcodec"]) if decision["vcopy"] else ("h264",)
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

        tag = "-".join(str(x) for x in (video, audio, decision.get("vcopy"), decision.get("acopy")))
        return watch_pc.Source(("library", iid, tag), url=lambda: self.raw_url(iid), paced=False,
                               offset=max(0.0, float(offset)), start=max(0.0, float(offset)), label=label,
                               site="library", copy_video=copy, maps=maps, video_tag=decision.get("tag") or "",
                               exact=True, media_start=float((media or {}).get("start") or 0.0), bounded=True,
                               audio_rate="192k", mode=mode, subs_out=subs_out, progress=progress)

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


# ---------------------------------------------------------------- HTTP

_FILE = re.compile(r"^/api/library/file/([0-9a-f]{16})$")
_ART = re.compile(r"^/api/library/art/([0-9a-f]{16})$")
_SUBS = re.compile(r"^/api/library/subs/([0-9a-f]{16})/(\d{1,3})\.vtt$")
_RAW = re.compile(r"^/api/library/raw/([0-9a-f]{16})$")


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
    library.serve_file(handler, match.group(1), raw=True)
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
        answer = library.view()
        answer["admin"] = caller.role == "admin"
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
        rows.append({"id": f"lib:{item['id']}", "kind": "library", "title": item.get("show") if episode else item["title"],
                     "subtitle": item["title"] if episode else (str(item["year"]) if item.get("year") else "片库"),
                     "source": "library", "playable": Library.playable(item),
                     "art": {"url": f"api/library/art/{item['id']}" if item.get("poster") else "", "ratio": "poster"},
                     "play": {"kind": "library", "id": item["id"], "playable": Library.playable(item)}})
    return rows


def attach(relay, start: bool = True) -> Library | None:
    """自建 / 电脑客户端：挂在 relay.library 上，开定时扫描。云端不建。"""
    if relay.selfhost is None or relay.accounts is None:
        return None
    library = Library(Path(relay.accounts.path).parent, log=lambda message: relay.log(message))
    relay.library = library
    wired(relay)
    if start:
        threading.Thread(target=library.run_forever, name="library", daemon=True).start()
    return library
