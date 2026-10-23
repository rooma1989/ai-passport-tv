"""YouTube 节点：在海外服务器 cstone 上运行，替阿里云的中转取 YouTube 视频。

单文件、只用标准库、兼容 Python 3.10，部署时只拷这一个文件（/opt/yt-relay/ytnode.py）。
阿里云从不直连 YouTube：它带着 X-Key、只信任本节点的自签证书来调用这里。

    GET /yt/list?channel=<频道地址>   最新 20 个投稿
    GET /yt/playlist?list=<列表 id>   播放列表的全部视频（剧集用，按列表顺序）
    GET /yt/info?id=<视频 id>         标题、时长、所属频道
    GET /yt/search?q=<关键词>&n=<个数>[&kind=channel]
                                      语音搜索：视频按 YouTube 的排序，去掉直播、预告和 Shorts；
                                      kind=channel 搜频道（名字、订阅数）
    GET /yt/stream?id=<视频 id>&ss=<秒> 压成小码流的 MPEG-TS，实时速度输出

流用分块编码发送：正常播完发结束块，转码出错则不发直接断开，对端据此分辨。
YouTube 会拦机房 IP（要求登录证明不是机器人），所以 yt-dlp 带一个小号的 cookies 文件。

本机模式（家庭服务器、电脑客户端的 [youtube] mode = "local"）：同一个文件由监督进程作为子进程启动
（--plain：只听 127.0.0.1、明文 HTTP、不要证书），另外多几样：

    --tools-dir   yt-dlp、deno 不是固定路径，每次调用都读 <目录>/components.json（下载、更新它们的是
                  server/selfhost/ytcomponents.py）；还没装好时回答 503 tools missing，装好就能用，不用重启
    --proxy       用户自己的代理（也可以放在环境变量 TV_YT_PROXY 里）：yt-dlp 用 --proxy，ffmpeg 取媒体用
                  http_proxy；socks5 代理 ffmpeg 不认，就在本机开一个只听 127.0.0.1 的 CONNECT 转接口（ConnectShim）
    --remux       选中的格式是 H.264 + AAC 时只换封装（-c copy），不转码
    --no-check    不做启动时和每 6 小时的后台检查（家里的电脑不用定时去碰 YouTube）
    GET /yt/check 不走缓存解析一次测试视频，回答用了几秒和出错的最后一行（设置页的「测试」）
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hmac
import ipaddress
import json
import os
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIST_SIZE = 20
# 播放列表（剧集）最多取这么多个，和中转的 SERIES_SIZE 一致。
PLAYLIST_SIZE = 1000
# 1 核的机器，同时转码最多两路。
MAX_STREAMS = 2
RUN_TIMEOUT_S = 120
# 解析一个视频在 1 核的 cstone 上要 18 秒左右，而一次起播会连两次（先采样调色板，再正式播放），
# 投屏还会先查一次信息。视频地址能用几个小时，所以解析结果留 30 分钟。
INFO_CACHE_S = 30 * 60
# 后台每 6 小时不走缓存解析一次这个视频，没人在看时也能发现 cookies 过期。
CHECK_VIDEO = "jNQXAC9IVRw"
CHECK_EVERY_S = 6 * 3600
CHUNK_BYTES = 64 * 1024
# 语音搜索：最多要几个；同一个词 10 分钟内直接用上次的结果（中转等不及时，节点仍把这次搜完，
# 再说一遍就快了）；yt-dlp 正在解析别的视频（每个约 18 秒）时最多等这么久，等不到回答 503 busy：
# 中转只等几秒，排长队没有意义。
SEARCH_MAX = 20
# 平铺搜索结果里会有直播、Shorts，多要几个，滤掉后仍够数。
SEARCH_EXTRA = 5
SEARCH_CACHE_S = 10 * 60
SEARCH_WAIT_S = 3
QUERY_MAX = 80
SEARCH_KINDS = ("video", "channel")
# YouTube 搜索页只看频道的筛选参数（sp=EgIQAg==）。
CHANNEL_FILTER = "EgIQAg%3D%3D"
_UCID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
# 先要 360p 的 H.264+AAC 合体格式（18），没有再取 360p 以内的最好的。
FORMAT = "18/b[height<=360]/bv*[height<=360]+ba/b"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST = re.compile(r"^(?:PL|OL|UU|FL|RD)[A-Za-z0-9_-]{8,64}$")
_CHANNEL = re.compile(r"^https://www\.youtube\.com/(@[^/?#\s]+|(?:channel|c|user)/[A-Za-z0-9_-]+)$")


@dataclasses.dataclass
class Tools:
    ytdlp: str
    deno: str
    ffmpeg: str
    cookies: str
    proxy: str = ""


# ---- 代理（本机模式）----

PROXY_SCHEMES = ("http", "socks5", "socks5h")
PROXY_ENV = "TV_YT_PROXY"
_PROXY_TEXT = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=:@\[\]/-]+$")
TUNNEL_TIMEOUT_S = 15
HEAD_MAX = 8192


@dataclasses.dataclass
class Proxy:
    scheme: str
    host: str
    port: int
    user: str = ""
    password: str = ""


def parse_proxy(url: str) -> Proxy:
    """http://主机:端口、socks5://主机:端口、socks5h://主机:端口（可带 用户:密码@）。看不懂就 ValueError，
    消息里不带原文（可能有密码）。"""
    text = (url or "").strip()
    if not text or len(text) > 300 or not _PROXY_TEXT.match(text):
        raise ValueError("代理地址里有不认识的字符")
    parts = urllib.parse.urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError("代理要以 http://、socks5:// 或 socks5h:// 开头")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("代理的端口不对") from None
    if not parts.hostname or port is None or not 0 < port < 65536:
        raise ValueError("代理要写成 协议://主机:端口，例如 http://127.0.0.1:端口")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("代理地址后面不要带路径")
    return Proxy(scheme, parts.hostname, port, urllib.parse.unquote(parts.username or ""),
                 urllib.parse.unquote(parts.password or ""))


def hide_password(text: str) -> str:
    """把网址里的「用户:密码@」换成「***@」：日志、错误信息里不出现代理的密码。"""
    return re.sub(r"(://)[^/\s:@]+:[^/\s@]*@", r"\1***@", text or "")


def _read_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(1024)
        if not chunk:
            raise OSError("proxy closed the connection")
        data += chunk
        if len(data) > HEAD_MAX:
            raise OSError("proxy answer too long")
    return data


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("proxy closed the connection")
        data += chunk
    return data


def _socks5(sock: socket.socket, proxy: Proxy, host: str, port: int) -> None:
    auth = bool(proxy.user)
    sock.sendall(b"\x05\x02\x00\x02" if auth else b"\x05\x01\x00")
    version, method = _recv_exact(sock, 2)
    if version != 5 or method == 0xFF or (method == 2 and not auth) or method not in (0, 2):
        raise OSError("socks5 proxy refused the authentication method")
    if method == 2:
        user, password = proxy.user.encode(), proxy.password.encode()
        if len(user) > 255 or len(password) > 255:
            raise OSError("socks5 user name or password too long")
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        if _recv_exact(sock, 2)[1] != 0:
            raise OSError("socks5 proxy refused the user name or password")
    if proxy.scheme == "socks5":
        # socks5：本机解析域名（socks5h 才交给代理解析）。
        address = ipaddress.ip_address(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0])
        target = (b"\x01" if address.version == 4 else b"\x04") + address.packed
    else:
        name = host.encode("idna")
        target = b"\x03" + bytes([len(name)]) + name
    sock.sendall(b"\x05\x01\x00" + target + port.to_bytes(2, "big"))
    version, reply, _reserved, kind = _recv_exact(sock, 4)
    if version != 5 or reply != 0:
        raise OSError(f"socks5 proxy could not connect (reply {reply})")
    # 代理告诉我们它这一头的地址：IPv4 4 字节、IPv6 16 字节、域名先一个长度字节；再加 2 字节端口。不用它。
    size = {1: 4, 4: 16}.get(kind)
    if size is None:
        size = _recv_exact(sock, 1)[0]
    _recv_exact(sock, size + 2)


def open_tunnel(proxy: Proxy, host: str, port: int, timeout: float = TUNNEL_TIMEOUT_S) -> socket.socket:
    """经代理连到 host:port，返回连好的套接字（之后就像直连一样用，TLS 由调用方自己套）。"""
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        if proxy.scheme == "http":
            target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
            if proxy.user:
                token = base64.b64encode(f"{proxy.user}:{proxy.password}".encode()).decode()
                head += f"Proxy-Authorization: Basic {token}\r\n"
            sock.sendall((head + "\r\n").encode())
            status = _read_head(sock).split(b"\r\n", 1)[0].split()
            if len(status) < 2 or status[1] != b"200":
                raise OSError(f"proxy answered {status[1].decode('ascii', 'replace') if len(status) > 1 else '?'}")
        else:
            _socks5(sock, proxy, host, port)
    except BaseException:
        sock.close()
        raise
    sock.settimeout(None)
    return sock


def _splice(source: socket.socket, sink: socket.socket) -> None:
    try:
        while chunk := source.recv(CHUNK_BYTES):
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class ConnectShim:
    """只听 127.0.0.1 的 HTTP CONNECT 代理，把每条连接经 socks5 代理转出去：ffmpeg 的 http_proxy 只认 http://。
    只做 CONNECT（媒体地址都是 https），别的请求回 405。"""

    def __init__(self, proxy: Proxy, log=print):
        self.proxy, self.log = proxy, log
        shim = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                client = self.request
                client.settimeout(TUNNEL_TIMEOUT_S)
                try:
                    head = _read_head(client)
                except OSError:
                    return
                line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").split()
                if len(line) != 3 or line[0] != "CONNECT":
                    client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                    return
                host, _, port = line[1].rpartition(":")
                host = host.strip("[]")
                if not host or not port.isdigit() or not 0 < int(port) < 65536:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                    return
                try:
                    upstream = open_tunnel(shim.proxy, host, int(port))
                except OSError as error:
                    shim.log(f"经代理连 {host} 失败：{hide_password(str(error))[:160]}")
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    return
                client.settimeout(None)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                other = threading.Thread(target=_splice, args=(upstream, client), daemon=True)
                other.start()
                _splice(client, upstream)
                other.join()
                upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "ConnectShim":
        threading.Thread(target=self.server.serve_forever, daemon=True, name="connect-shim").start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def ffmpeg_proxy_env(environ, proxy_url: str) -> dict:
    """ffmpeg 取媒体用的环境变量：http_proxy 指向代理（socks5 时指向本机的转接口），回环地址不走代理。"""
    env = dict(environ)
    env["http_proxy"] = proxy_url
    hosts = [h for h in (env.get("no_proxy") or env.get("NO_PROXY") or "").split(",") if h.strip()]
    for host in ("localhost", "127.0.0.1", "::1"):
        if host not in hosts:
            hosts.append(host)
    env["no_proxy"] = env["NO_PROXY"] = ",".join(hosts)
    return env


# ---- 本机模式：yt-dlp、deno 在哪 ----

COMPONENTS_FILE = "components.json"


def read_components(tools_dir: str) -> tuple[str, str] | None:
    """<目录>/components.json 里记的 yt-dlp 和 deno（只认目录里的文件名）；没装好时 None。"""
    try:
        with open(os.path.join(tools_dir, COMPONENTS_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    found = []
    for name in ("yt-dlp", "deno"):
        entry = data.get(name) if isinstance(data, dict) else None
        file = entry.get("file") if isinstance(entry, dict) else None
        if not isinstance(file, str) or not file or os.path.basename(file) != file or file.startswith("."):
            return None
        path = os.path.join(tools_dir, file)
        if not os.path.isfile(path):
            return None
        found.append(path)
    return found[0], found[1]


def valid_channel(channel: str) -> bool:
    return bool(_CHANNEL.match(channel or ""))


def clean_query(query: str) -> str | None:
    """搜索词：去掉首尾和多余空白；空的、太长的、带控制字符的不收（返回 None）。"""
    query = " ".join((query or "").split())
    if not query or len(query) > QUERY_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in query):
        return None
    return query


def _base(tools: Tools) -> list[str]:
    args = [tools.ytdlp, "--js-runtimes", f"deno:{tools.deno}", "--no-warnings",
            "--no-progress", "--socket-timeout", "15", "--ignore-config"]
    if tools.cookies:
        args += ["--cookies", tools.cookies]
    if tools.proxy:
        args += ["--proxy", tools.proxy]
    return args


def list_args(tools: Tools, channel: str) -> list[str]:
    return _base(tools) + ["--flat-playlist", "--playlist-end", str(LIST_SIZE), "-J",
                           "--", channel.rstrip("/") + "/videos"]


def playlist_args(tools: Tools, playlist: str) -> list[str]:
    return _base(tools) + ["--flat-playlist", "--playlist-end", str(PLAYLIST_SIZE), "-J",
                           "--", f"https://www.youtube.com/playlist?list={playlist}"]


def info_args(tools: Tools, video_id: str) -> list[str]:
    return _base(tools) + ["-J", "--no-playlist", "-f", FORMAT,
                           "--", f"https://www.youtube.com/watch?v={video_id}"]


def search_args(tools: Tools, query: str, n: int, kind: str = "video") -> list[str]:
    if kind == "channel":
        # 搜索页加频道筛选；只取前 n 个（不然 yt-dlp 会一页页翻下去）。
        url = (f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
               f"&sp={CHANNEL_FILTER}")
        return _base(tools) + ["--flat-playlist", "-J", "--playlist-end", str(n), "--", url]
    # 「ytsearchN:」是 yt-dlp 的搜索前缀；放在「--」之后，搜索词以「-」开头也不会被当成参数。
    return _base(tools) + ["--flat-playlist", "-J", "--", f"ytsearch{n + SEARCH_EXTRA}:{query}"]


def channel_summary(listing: dict, n: int) -> dict:
    """频道搜索的平铺结果 → {"channels": [{"id": UC…, "name", "fans", "videos"}]}。
    订阅数、视频数 yt-dlp 给不出时为 0。"""
    channels, seen = [], set()
    for e in listing.get("entries") or []:
        if not isinstance(e, dict):
            continue
        cid = str(e.get("channel_id") or e.get("id") or "")
        name = " ".join(str(e.get("channel") or e.get("title") or "").split())
        if not _UCID.match(cid) or cid in seen or not name:
            continue
        seen.add(cid)
        fans, videos = e.get("channel_follower_count"), e.get("playlist_count")
        channels.append({"id": cid, "name": name,
                         "fans": fans if isinstance(fans, int) and not isinstance(fans, bool) else 0,
                         "videos": videos if isinstance(videos, int) and not isinstance(videos, bool) else 0})
    return {"channels": channels[:n]}


def search_summary(listing: dict, n: int) -> dict:
    """平铺的搜索结果 → 能投屏的视频：去掉直播中、预告、刚播完还在处理的直播、Shorts，
    没有时长的（直播和预告都没有）。"""
    videos = []
    for e in listing.get("entries") or []:
        if not isinstance(e, dict) or not _ID.match(str(e.get("id", ""))):
            continue
        if e.get("live_status") in ("is_live", "is_upcoming", "post_live") or "/shorts/" in str(e.get("url") or ""):
            continue
        try:
            duration = int(e.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        title = e.get("title") or ""
        if duration <= 0 or not title or title in ("[Deleted video]", "[Private video]"):
            continue
        videos.append({"id": e["id"], "title": title, "duration": duration,
                       "uploader": e.get("channel") or e.get("uploader") or ""})
    return {"videos": videos[:n]}


def info_summary(info: dict) -> dict:
    return {"id": info.get("id"), "title": info.get("title") or "",
            "duration": int(info.get("duration") or 0),
            "channel_url": info.get("channel_url") or ""}


def listing_summary(listing: dict) -> dict:
    videos = [{"id": e["id"], "title": e.get("title") or "", "duration": int(e.get("duration") or 0)}
              for e in listing.get("entries") or [] if _ID.match(str(e.get("id", "")))]
    return {"name": listing.get("channel") or listing.get("uploader") or listing.get("title") or "",
            "channel_url": listing.get("channel_url") or "", "videos": videos[:LIST_SIZE]}


def playlist_summary(listing: dict) -> dict:
    # 列表里删掉或私享的视频，平铺列表里标题是 [Deleted video] / [Private video]，时长为空。
    videos = [{"id": e["id"], "title": e.get("title") or "", "duration": int(e.get("duration") or 0)}
              for e in listing.get("entries") or [] if _ID.match(str(e.get("id", "")))
              and e.get("title") not in ("[Deleted video]", "[Private video]")]
    return {"name": listing.get("title") or "", "videos": videos[:PLAYLIST_SIZE]}


def stream_inputs(info: dict) -> list[tuple[str, dict]]:
    """yt-dlp 选中的格式 → [(地址, HTTP 头)]：合体格式一个，音视频分开的两个。"""
    formats = info.get("requested_formats") or ([info] if info.get("url") else [])
    return [(f["url"], dict(f.get("http_headers") or {})) for f in formats if f.get("url")]


def copyable(info: dict) -> bool:
    """选中的格式能不能直接换封装成 MPEG-TS：视频 H.264（avc1），声音 AAC（mp4a）或没有声音。"""
    formats = info.get("requested_formats") or ([info] if info.get("url") else [])
    video = [str(f.get("vcodec") or "") for f in formats if str(f.get("vcodec") or "none") != "none"]
    audio = [str(f.get("acodec") or "") for f in formats if str(f.get("acodec") or "none") != "none"]
    return (len(video) == 1 and video[0].startswith("avc1")
            and all(a.startswith("mp4a") for a in audio) and len(audio) <= 1)


def ffmpeg_args(tools: Tools, inputs: list[tuple[str, dict]], offset: float, copy: bool = False) -> list[str]:
    args = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
    for url, headers in inputs:
        headers = dict(headers)
        agent = headers.pop("User-Agent", "")
        extra = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        if agent:
            args += ["-user_agent", agent]
        if extra:
            args += ["-headers", extra]
        args += ["-re", "-ss", str(int(offset)), "-i", url]
    if len(inputs) > 1:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += ["-map", "0:v:0", "-map", "0:a:0?"]
    if copy:
        # 本机模式：家里到电视服务是回环，不用为带宽压码率；电视服务自己会按设备的画面再编码。
        return args + ["-c", "copy", "-f", "mpegts", "pipe:1"]
    # 设备只有 240×180，跨境只送必要的码率：240 行、15 帧、约 300 kbit/s。
    args += ["-vf", "scale=-2:240,fps=15", "-c:v", "libx264", "-preset", "ultrafast",
             "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-b:v", "300k", "-maxrate", "400k",
             "-bufsize", "600k", "-g", "30", "-c:a", "aac", "-b:a", "48k", "-ac", "1",
             "-ar", "44100", "-f", "mpegts", "pipe:1"]
    return args


def error_status(stderr: str) -> int:
    text = stderr or ""
    if "not a bot" in text or "Sign in to confirm" in text:
        return 503
    for gone in ("Video unavailable", "Private video", "has been removed", "This video is not available",
                 "members-only", "This channel does not exist", "does not exist", "HTTP Error 404"):
        if gone in text:
            return 404
    return 502


def _run(args: list[str]) -> tuple[int, str, str]:
    done = subprocess.run(args, capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
                          stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout, done.stderr


def _popen(args: list[str], env: dict | None = None):
    return subprocess.Popen(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)


NETWORK_HINTS = ("Unable to download", "Failed to resolve", "Connection refused", "timed out",
                 "Network is unreachable", "getaddrinfo", "Tunnel connection failed", "ProxyError",
                 "Connection reset", "Remote end closed", "Unable to connect to proxy", "SSL",
                 "No route to host", "Temporary failure in name resolution", "nodename nor servname")


class Node:
    def __init__(self, tools: Tools, key: str, run=_run, popen=_popen, log=print,
                 tools_dir: str | None = None, remux: bool = False, ffmpeg_env: dict | None = None):
        """tools_dir：本机模式，yt-dlp、deno 每次从这里的 components.json 找（tools 里的那两项不用）。
        remux：H.264 + AAC 时只换封装。ffmpeg_env：给 ffmpeg 的环境变量（代理），None 时照监督进程的。"""
        self.tools = tools
        self.key = key
        self.run = run
        self.popen = popen
        self.log = log
        self.tools_dir = tools_dir
        self.remux = remux
        self.ffmpeg_env = ffmpeg_env
        self.last_error = ""
        self.streams = threading.BoundedSemaphore(MAX_STREAMS)
        self._info: dict[str, tuple[float, dict]] = {}
        self._info_lock = threading.Lock()
        # 搜索结果：(种类, 词, 个数) → (时刻, 回答)。
        self._searches: dict[tuple[str, str, int], tuple[float, dict]] = {}
        # 一次只跑一个 yt-dlp：每个要带一个 deno，两个并发在 400M 的内存上限里会被 OOM 杀掉。
        # 设备每次起播连两次（先采样调色板），同一视频的第二个请求等第一个解析完直接用缓存。
        self._ytdlp = threading.Lock()
        self.clock = time.monotonic
        # 最近一次 YouTube 解析的结果，给中转判断 cookies 是否过期：
        # unknown（还没解析过）、ok、login（要求登录）、error（其他失败）。
        self.health = {"state": "unknown", "at": 0.0, "version": ""}

    def _record(self, state: str, version: str = "") -> None:
        self.health = {"state": state, "at": time.time(),
                       "version": version or self.health.get("version", "")}

    def current_tools(self) -> Tools | None:
        """这次调用用的工具。本机模式现找 yt-dlp、deno（还没装好时 None），cookies 文件在才带上。"""
        if self.tools_dir is None:
            return self.tools
        found = read_components(self.tools_dir)
        if found is None:
            return None
        cookies = self.tools.cookies if self.tools.cookies and os.path.isfile(self.tools.cookies) else ""
        return dataclasses.replace(self.tools, ytdlp=found[0], deno=found[1], cookies=cookies)

    def check(self) -> None:
        """不走缓存解析一次测试视频，只为更新健康状态。"""
        tools = self.current_tools()
        if tools is not None:
            self.ytdlp_json(info_args(tools, CHECK_VIDEO))

    def check_now(self) -> dict:
        """设置页的「测试」：不走缓存解析测试视频，{"state", "seconds", "error", "version", "tools"}。"""
        tools = self.current_tools()
        if tools is None:
            return {"state": "missing", "seconds": 0, "error": "", "version": "", "tools": False}
        started = time.monotonic()
        code, data = self.ytdlp_json(info_args(tools, CHECK_VIDEO))
        state = "ok" if code == 200 else {503: "login", 504: "slow"}.get(code, "error")
        if state == "error" and any(hint in str(data) for hint in NETWORK_HINTS):
            state = "network"
        return {"state": state, "seconds": round(time.monotonic() - started, 1),
                "error": "" if code == 200 else hide_password(str(data))[:200],
                "version": self.health.get("version", ""), "tools": True}

    def _cached(self, video_id: str):
        with self._info_lock:
            cached = self._info.get(video_id)
            if cached and self.clock() - cached[0] < INFO_CACHE_S:
                return cached[1]
        return None

    def video_info(self, video_id: str) -> tuple[int, dict | str]:
        info = self._cached(video_id)
        if info is not None:
            return 200, info
        tools = self.current_tools()
        if tools is None:
            return 503, "tools missing"
        with self._ytdlp:
            # 等锁期间别的请求可能已经解析好了。
            info = self._cached(video_id)
            if info is not None:
                return 200, info
            code, info = self._ytdlp_json(info_args(tools, video_id))
        if code == 200:
            now = self.clock()
            with self._info_lock:
                self._info = {k: v for k, v in self._info.items() if now - v[0] < INFO_CACHE_S}
                self._info[video_id] = (now, info)
        return code, info

    def search(self, query: str, n: int, kind: str = "video") -> tuple[int, dict | str]:
        key = (kind, query.lower(), n)
        with self._info_lock:
            cached = self._searches.get(key)
            if cached and self.clock() - cached[0] < SEARCH_CACHE_S:
                return 200, cached[1]
        tools = self.current_tools()
        if tools is None:
            return 503, "tools missing"
        # 仍然一次只跑一个 yt-dlp；只是不像起播那样一直排队：语音那头只等几秒。
        if not self._ytdlp.acquire(timeout=SEARCH_WAIT_S):
            return 503, "busy"
        try:
            code, data = self._ytdlp_json(search_args(tools, query, n, kind))
        finally:
            self._ytdlp.release()
        if code != 200:
            return code, data
        summarise = channel_summary if kind == "channel" else search_summary
        summary = summarise(data if isinstance(data, dict) else {}, n)
        now = self.clock()
        with self._info_lock:
            self._searches = {k: v for k, v in self._searches.items() if now - v[0] < SEARCH_CACHE_S}
            self._searches[key] = (now, summary)
        return 200, summary

    def key_ok(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.key.encode())

    def ytdlp_json(self, args: list[str]) -> tuple[int, dict | str]:
        with self._ytdlp:
            return self._ytdlp_json(args)

    def _ytdlp_json(self, args: list[str]) -> tuple[int, dict | str]:
        try:
            code, out, err = self.run(args)
        except subprocess.TimeoutExpired:
            return 504, "yt-dlp timed out"
        if code != 0:
            last = (err or "").strip().splitlines()[-1:] or [""]
            last = [hide_password(last[0])]
            self.log(f"yt-dlp 失败：{last[0][:200]}")
            self.last_error = last[0][:200]
            status = error_status(err)
            # 404 是这个视频本身的问题（删了、私享）：YouTube 正常回答了，cookies 没问题。
            self._record({503: "login", 404: "ok"}.get(status, "error"))
            return status, last[0][:200]
        try:
            data = json.loads(out)
        except ValueError:
            self._record("error")
            return 502, "yt-dlp output is not JSON"
        version = (data.get("_version") or {}).get("version", "") if isinstance(data, dict) else ""
        self._record("ok", version)
        return 200, data


def _handler(node: Node):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # 一问一答，不保持连接：调用方每次都新建连接。
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data) -> None:
            if not isinstance(data, dict):
                data = {"error": str(data)}
            self._send(code, json.dumps(data, ensure_ascii=False).encode())

        def do_GET(self):
            if not node.key_ok(self.headers.get("X-Key")):
                self._send(404, b"Not Found", "text/plain")
                return
            parts = urllib.parse.urlsplit(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
            if parts.path == "/yt/health":
                health = dict(node.health)
                if node.tools_dir is not None:
                    health["tools"] = read_components(node.tools_dir) is not None
                self._json(200, health)
                return
            if parts.path == "/yt/check":
                self._json(200, node.check_now())
                return
            tools = node.current_tools()
            if parts.path in ("/yt/list", "/yt/playlist") and tools is None:
                self._json(503, "tools missing")
                return
            if parts.path == "/yt/list":
                channel = query.get("channel", "")
                if not valid_channel(channel):
                    self._json(400, "bad channel")
                    return
                code, data = node.ytdlp_json(list_args(tools, channel))
                self._json(code, listing_summary(data) if code == 200 else data)
            elif parts.path == "/yt/playlist":
                playlist = query.get("list", "")
                if not _PLAYLIST.match(playlist):
                    self._json(400, "bad playlist")
                    return
                code, data = node.ytdlp_json(playlist_args(tools, playlist))
                self._json(code, playlist_summary(data) if code == 200 else data)
            elif parts.path == "/yt/info":
                video_id = query.get("id", "")
                if not _ID.match(video_id):
                    self._json(400, "bad id")
                    return
                code, data = node.video_info(video_id)
                self._json(code, info_summary(data) if code == 200 else data)
            elif parts.path == "/yt/search":
                text, n = clean_query(query.get("q", "")), query.get("n", "10")
                kind = query.get("kind", "video")
                if text is None or not n.isdigit() or not 1 <= int(n) <= SEARCH_MAX \
                        or kind not in SEARCH_KINDS:
                    self._json(400, "bad query")
                    return
                code, data = node.search(text, int(n), kind)
                self._json(code, data)
            elif parts.path == "/yt/stream":
                self._stream(query)
            else:
                self._send(404, b"Not Found", "text/plain")

        def _stream(self, query: dict) -> None:
            video_id, ss = query.get("id", ""), query.get("ss", "0")
            if not _ID.match(video_id) or not ss.isdigit():
                self._json(400, "bad id or offset")
                return
            if not node.streams.acquire(blocking=False):
                self._json(503, "busy")
                return
            try:
                code, info = node.video_info(video_id)
                if code != 200:
                    self._json(code, info)
                    return
                inputs = stream_inputs(info)
                if not inputs:
                    self._json(404, "no playable format")
                    return
                args = ffmpeg_args(node.tools, inputs, int(ss), copy=node.remux and copyable(info))
                proc = node.popen(args) if node.ffmpeg_env is None else node.popen(args, env=node.ffmpeg_env)
                node.log(f"开始 {video_id} 从 {ss} 秒")
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                outcome = "client_gone"
                try:
                    while chunk := proc.stdout.read1(CHUNK_BYTES):
                        self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    if proc.wait() == 0:
                        self.wfile.write(b"0\r\n\r\n")
                        outcome = "ended"
                    else:
                        outcome = "failed"
                except OSError:
                    pass
                finally:
                    proc.kill()
                    proc.wait()
                    self.close_connection = True
                node.log(f"结束 {video_id} {outcome}")
            finally:
                node.streams.release()

        def log_message(self, *_args):
            pass

    return Handler


def make_server(node: Node, bind: str, port: int, context: ssl.SSLContext | None):
    server = ThreadingHTTPServer((bind, port), _handler(node))
    server.daemon_threads = True
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ytnode.py")
    parser.add_argument("--port", type=int, default=47443)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--cert")
    parser.add_argument("--tls-key")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ytdlp", default="/opt/yt-relay/yt-dlp")
    parser.add_argument("--deno", default="/opt/yt-relay/deno")
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    parser.add_argument("--cookies", default="/var/lib/yt-relay/cookies.txt")
    # 本机模式（家庭服务器、电脑客户端的子进程），见模块说明。
    parser.add_argument("--plain", action="store_true", help="明文 HTTP，只许听 127.0.0.1（本机模式）")
    parser.add_argument("--tools-dir", help="本机模式：yt-dlp、deno 按这里的 components.json 现找")
    parser.add_argument("--proxy", default=None, help="代理（不给时看环境变量 TV_YT_PROXY）")
    parser.add_argument("--remux", action="store_true", help="H.264 + AAC 时只换封装")
    parser.add_argument("--no-check", action="store_true", help="不做后台检查")
    args = parser.parse_args(argv)
    if args.plain and args.bind != "127.0.0.1":
        print("--plain 只能听 127.0.0.1", file=sys.stderr)
        return 2
    if not args.plain and not (args.cert and args.tls_key):
        print("要 --cert 和 --tls-key（或本机模式的 --plain）", file=sys.stderr)
        return 2
    with open(args.key_file, encoding="utf-8") as handle:
        key = handle.read().strip()
    if len(key) < 16:
        print("密钥太短（至少 16 个字符）", file=sys.stderr)
        return 2
    proxy_url = (args.proxy if args.proxy is not None else os.environ.get(PROXY_ENV, "")).strip()
    proxy = None
    if proxy_url:
        try:
            proxy = parse_proxy(proxy_url)
        except ValueError as error:
            print(f"代理设置不对：{error}", file=sys.stderr)
            return 2

    def log(message: str) -> None:
        print(message, flush=True)

    if args.tools_dir:
        # 本机模式：cookies 每次调用时看在不在（设置页导入、删除都不用重启）。
        cookies = args.cookies
    else:
        cookies = args.cookies if os.path.isfile(args.cookies) else ""
        if not cookies:
            print("警告：没有 cookies 文件，YouTube 很可能要求登录", flush=True)
    context = None
    if not args.plain:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.tls_key)
    ffmpeg_env = None
    if proxy is not None:
        media_proxy = proxy_url
        if proxy.scheme != "http":
            media_proxy = ConnectShim(proxy, log=log).start().url
        ffmpeg_env = ffmpeg_proxy_env(os.environ, media_proxy)
    node = Node(Tools(args.ytdlp, args.deno, args.ffmpeg, cookies, proxy_url), key, log=log,
                tools_dir=args.tools_dir, remux=args.remux, ffmpeg_env=ffmpeg_env)
    server = make_server(node, args.bind, args.port, context)

    def checks():
        while True:
            node.check()
            time.sleep(CHECK_EVERY_S)

    if not args.no_check:
        threading.Thread(target=checks, daemon=True).start()
    where = "本机模式" if args.plain else "端口"
    print(f"YouTube 节点已启动：{where} {args.bind}:{server.server_address[1]}"
          f"{'，经代理（' + proxy.scheme + '）' if proxy else ''}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
