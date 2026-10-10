"""YouTube 节点：在海外服务器 cstone 上运行，替阿里云的中转取 YouTube 视频。

单文件、只用标准库、兼容 Python 3.10，部署时只拷这一个文件（/opt/yt-relay/ytnode.py）。
阿里云从不直连 YouTube：它带着 X-Key、只信任本节点的自签证书来调用这里。

    GET /yt/list?channel=<频道地址>   最新 20 个投稿
    GET /yt/info?id=<视频 id>         标题、时长、所属频道
    GET /yt/stream?id=<视频 id>&ss=<秒> 压成小码流的 MPEG-TS，实时速度输出

流用分块编码发送：正常播完发结束块，转码出错则不发直接断开，对端据此分辨。
YouTube 会拦机房 IP（要求登录证明不是机器人），所以 yt-dlp 带一个小号的 cookies 文件。
"""

from __future__ import annotations

import argparse
import dataclasses
import hmac
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIST_SIZE = 20
# 1 核的机器，同时转码最多两路。
MAX_STREAMS = 2
RUN_TIMEOUT_S = 120
# 解析一个视频在 1 核的 cstone 上要 18 秒左右，而一次起播会连两次（先采样调色板，再正式播放），
# 投屏还会先查一次信息。视频地址能用几个小时，所以解析结果留 30 分钟。
INFO_CACHE_S = 30 * 60
CHUNK_BYTES = 64 * 1024
# 先要 360p 的 H.264+AAC 合体格式（18），没有再取 360p 以内的最好的。
FORMAT = "18/b[height<=360]/bv*[height<=360]+ba/b"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL = re.compile(r"^https://www\.youtube\.com/(@[^/?#\s]+|(?:channel|c|user)/[A-Za-z0-9_-]+)$")


@dataclasses.dataclass
class Tools:
    ytdlp: str
    deno: str
    ffmpeg: str
    cookies: str


def valid_channel(channel: str) -> bool:
    return bool(_CHANNEL.match(channel or ""))


def _base(tools: Tools) -> list[str]:
    args = [tools.ytdlp, "--js-runtimes", f"deno:{tools.deno}", "--no-warnings",
            "--no-progress", "--socket-timeout", "15", "--ignore-config"]
    if tools.cookies:
        args += ["--cookies", tools.cookies]
    return args


def list_args(tools: Tools, channel: str) -> list[str]:
    return _base(tools) + ["--flat-playlist", "--playlist-end", str(LIST_SIZE), "-J",
                           "--", channel.rstrip("/") + "/videos"]


def info_args(tools: Tools, video_id: str) -> list[str]:
    return _base(tools) + ["-J", "--no-playlist", "-f", FORMAT,
                           "--", f"https://www.youtube.com/watch?v={video_id}"]


def info_summary(info: dict) -> dict:
    return {"id": info.get("id"), "title": info.get("title") or "",
            "duration": int(info.get("duration") or 0),
            "channel_url": info.get("channel_url") or ""}


def listing_summary(listing: dict) -> dict:
    videos = [{"id": e["id"], "title": e.get("title") or "", "duration": int(e.get("duration") or 0)}
              for e in listing.get("entries") or [] if _ID.match(str(e.get("id", "")))]
    return {"name": listing.get("channel") or listing.get("uploader") or listing.get("title") or "",
            "channel_url": listing.get("channel_url") or "", "videos": videos[:LIST_SIZE]}


def stream_inputs(info: dict) -> list[tuple[str, dict]]:
    """yt-dlp 选中的格式 → [(地址, HTTP 头)]：合体格式一个，音视频分开的两个。"""
    formats = info.get("requested_formats") or ([info] if info.get("url") else [])
    return [(f["url"], dict(f.get("http_headers") or {})) for f in formats if f.get("url")]


def ffmpeg_args(tools: Tools, inputs: list[tuple[str, dict]], offset: float) -> list[str]:
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


def _popen(args: list[str]):
    return subprocess.Popen(args, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


class Node:
    def __init__(self, tools: Tools, key: str, run=_run, popen=_popen, log=print):
        self.tools = tools
        self.key = key
        self.run = run
        self.popen = popen
        self.log = log
        self.streams = threading.BoundedSemaphore(MAX_STREAMS)
        self._info: dict[str, tuple[float, dict]] = {}
        self._info_lock = threading.Lock()
        self.clock = time.monotonic

    def video_info(self, video_id: str) -> tuple[int, dict | str]:
        now = self.clock()
        with self._info_lock:
            cached = self._info.get(video_id)
            if cached and now - cached[0] < INFO_CACHE_S:
                return 200, cached[1]
        code, info = self.ytdlp_json(info_args(self.tools, video_id))
        if code == 200:
            with self._info_lock:
                self._info = {k: v for k, v in self._info.items() if now - v[0] < INFO_CACHE_S}
                self._info[video_id] = (now, info)
        return code, info

    def key_ok(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.key.encode())

    def ytdlp_json(self, args: list[str]) -> tuple[int, dict | str]:
        try:
            code, out, err = self.run(args)
        except subprocess.TimeoutExpired:
            return 504, "yt-dlp timed out"
        if code != 0:
            last = (err or "").strip().splitlines()[-1:] or [""]
            self.log(f"yt-dlp 失败：{last[0][:200]}")
            return error_status(err), last[0][:200]
        try:
            return 200, json.loads(out)
        except ValueError:
            return 502, "yt-dlp output is not JSON"


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
            if parts.path == "/yt/list":
                channel = query.get("channel", "")
                if not valid_channel(channel):
                    self._json(400, "bad channel")
                    return
                code, data = node.ytdlp_json(list_args(node.tools, channel))
                self._json(code, listing_summary(data) if code == 200 else data)
            elif parts.path == "/yt/info":
                video_id = query.get("id", "")
                if not _ID.match(video_id):
                    self._json(400, "bad id")
                    return
                code, data = node.video_info(video_id)
                self._json(code, info_summary(data) if code == 200 else data)
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
                proc = node.popen(ffmpeg_args(node.tools, inputs, int(ss)))
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
    parser.add_argument("--cert", required=True)
    parser.add_argument("--tls-key", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--ytdlp", default="/opt/yt-relay/yt-dlp")
    parser.add_argument("--deno", default="/opt/yt-relay/deno")
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    parser.add_argument("--cookies", default="/var/lib/yt-relay/cookies.txt")
    args = parser.parse_args(argv)
    with open(args.key_file, encoding="utf-8") as handle:
        key = handle.read().strip()
    if len(key) < 16:
        print("密钥太短（至少 16 个字符）", file=sys.stderr)
        return 2
    cookies = args.cookies if os.path.isfile(args.cookies) else ""
    if not cookies:
        print("警告：没有 cookies 文件，YouTube 很可能要求登录", flush=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.tls_key)
    node = Node(Tools(args.ytdlp, args.deno, args.ffmpeg, cookies), key,
                log=lambda message: print(message, flush=True))
    server = make_server(node, args.bind, args.port, context)
    print(f"YouTube 节点已启动：端口 {args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
