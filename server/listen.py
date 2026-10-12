"""用手机听：把设备正在放的声音做成 HLS 音频流给手机，设备晚 H 秒照同一份时间表放。

设计见 docs/superpowers/specs/2026-10-12-phone-audio-design.md。要点：

- 手机流是主时间表。`Ledger` 每 40 ms 在流的末端追加一条：一块内容（取自频道队列，不出队）
  或一块静音（源暂时没数据）。条目 k 在 `start + k*40ms` 成为流的一部分。
- 设备晚 `lead_s`（H）播同一条目：发送端每要发一块声音时问 `Ledger.for_device(play_at)`，
  设备来早了就补「填充静音」（只发给设备，不进流），来晚了就跳过条目。设备扬声器静音，
  这两种调整都听不见，只表现为画面停一下或跳一下。
- 流里每段的 EXT-X-PROGRAM-DATE-TIME 就是设备播这段声音的墙钟时刻。手机页用
  `getStartDate() + currentTime` 读出正在放的那段的 PDT，和校准过的服务器时间比，跳到对齐的位置。
- 编码：一个常驻 ffmpeg 把 16 kHz 单声道 PCM 编成 AAC（ADTS），这里切成 1.024 秒的
  packed-audio 段（每段前加 ID3：Apple 要求的 PRIV 时间戳 + 一个 TXXX 记 PDT 毫秒）。

只依赖标准库；ffmpeg 由调用方给路径。
"""

from __future__ import annotations

import base64
import collections
import datetime
import hashlib
import hmac
import json
import re
import struct
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Callable

from . import adpcm

RATE = 16000
CHUNK_MS = 40
CHUNK_S = CHUNK_MS / 1000
CHUNK_SAMPLES = RATE * CHUNK_MS // 1000          # 640
CHUNK_PCM_BYTES = CHUNK_SAMPLES * 2              # 1280
SILENCE = bytes(CHUNK_PCM_BYTES)
AAC_FRAME_SAMPLES = 1024
# ffmpeg 自带 aac 编码器的起始填充（initial_padding）：输出第 i 帧对应输入采样 [i*1024-1024, i*1024)。
AAC_PRIMING_SAMPLES = 1024
SEGMENT_FRAMES = 16                               # 16 * 1024 / 16000 = 1.024 s
SEGMENT_S = SEGMENT_FRAMES * AAC_FRAME_SAMPLES / RATE
# 播放列表保留的段数：手机往回跳（对齐时手机领先）需要旧段；太长则每次刷新列表的流量变大。
WINDOW_SEGMENTS = 12
# 手机拿到流后比设备早多少秒（H）。须大于手机从「段出现」到「出声」的时延（段长 + 播放器
# 留在末端后的余量 + 蓝牙），否则手机只能往末端跳，容易卡。见设计文档「H 怎么定」。
DEFAULT_LEAD_S = 5.0
# 设备偏离时间表多少才调整。一块是 40 ms，所以至少要半块（20 ms），否则补一块又超过、来回震荡。
DEFAULT_TOLERANCE_S = 0.025
# 开始听时先在流里放这么多静音，手机立刻有段可放（这些条目对设备也只是静音）。
PREFILL_S = 3.0
# 多久没取播放列表就算手机不听了（锁屏时 Safari 仍按段长刷新列表）。
IDLE_S = 20.0
TOKEN_LIFETIME_S = 12 * 3600


# ---------------------------------------------------------------- 时间表

@dataclass
class Entry:
    index: int
    published: float          # 单调时钟：这一条成为流的一部分的时刻
    key: object | None        # 内容块的标识（内容时间戳）；None 为静音


class Ledger:
    """手机流的时间表；设备照它晚 lead_s 播放。纯逻辑，不碰网络与时钟。"""

    def __init__(self, lead_s: float, start: float, tolerance_s: float = DEFAULT_TOLERANCE_S,
                 keep: int | None = None):
        self.lead_s = lead_s
        self.start = start
        self.tolerance_s = tolerance_s
        self.entries: collections.deque[Entry] = collections.deque()
        self.next_index = 0
        # 只保留设备还可能用到的：lead 之内的加 10 秒。
        self.keep = keep or int((lead_s + 10) / CHUNK_S)
        self.skipped = 0
        self.fillers = 0

    def due(self, now: float) -> int:
        """到 now 为止流应有而还没追加的条数。"""
        wanted = int((now - self.start) / CHUNK_S) + 1
        return max(0, wanted - self.next_index)

    def append(self, key: object | None) -> Entry:
        entry = Entry(self.next_index, self.start + self.next_index * CHUNK_S, key)
        self.next_index += 1
        self.entries.append(entry)
        while len(self.entries) > self.keep:
            self.entries.popleft()
        return entry

    def target(self, entry: Entry) -> float:
        """设备应播这一条的单调时刻。"""
        return entry.published + self.lead_s

    def for_device(self, play_at: float) -> tuple[str, Entry | None]:
        """发送端要发一块声音、设备会在 play_at 播它：返回 ("entry", 条目) 或 ("filler", None)。

        设备早了（下一条的目标时刻还远）发填充静音，让设备退后 40 ms；设备晚了跳过条目。
        """
        while self.entries:
            entry = self.entries[0]
            error = play_at - self.target(entry)
            if error > self.tolerance_s:
                self.entries.popleft()
                self.skipped += 1
                continue
            if error < -self.tolerance_s:
                self.fillers += 1
                return "filler", None
            self.entries.popleft()
            return "entry", entry
        self.fillers += 1
        return "filler", None


# ---------------------------------------------------------------- ADTS / ID3 / HLS

def split_adts(buffer: bytearray) -> list[bytes]:
    """从 buffer 头部切出完整的 ADTS 帧（原地删除），不完整的留着等下次。"""
    out = []
    while len(buffer) >= 7:
        if buffer[0] != 0xFF or (buffer[1] & 0xF0) != 0xF0:
            # 失步：丢到下一个同步字。ffmpeg 输出不该出现，出现了也不让它卡死。
            at = buffer.find(b"\xff", 1)
            del buffer[:at if at > 0 else len(buffer)]
            continue
        length = ((buffer[3] & 0x03) << 11) | (buffer[4] << 3) | (buffer[5] >> 5)
        if length < 7:
            del buffer[:1]
            continue
        if len(buffer) < length:
            break
        out.append(bytes(buffer[:length]))
        del buffer[:length]
    return out


def _syncsafe(value: int) -> bytes:
    return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


def id3_tag(pts_90k: int, pdt_ms: int) -> bytes:
    """packed audio 段头的 ID3v2.4：Apple 规定的 PRIV 传输流时间戳，另加 TXXX "pdt"=毫秒。"""
    priv = b"com.apple.streaming.transportStreamTimestamp\x00" + struct.pack(">Q", pts_90k % (1 << 33))
    txxx = b"\x03pdt\x00" + str(int(pdt_ms)).encode()
    frames = b""
    for name, body in ((b"PRIV", priv), (b"TXXX", txxx)):
        frames += name + _syncsafe(len(body)) + b"\x00\x00" + body
    return b"ID3\x04\x00\x00" + _syncsafe(len(frames)) + frames


def iso_ms(epoch_s: float) -> str:
    stamp = datetime.datetime.fromtimestamp(epoch_s, datetime.timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Segment:
    seq: int
    first_frame: int          # 流里的第几个 AAC 帧
    pdt: float                # 墙钟秒：设备播这段第一个采样的时刻
    data: bytes


class HlsWindow:
    """把 ADTS 帧攒成段，生成播放列表。帧号即流里的位置，和条目一一对应（每帧 1024 采样）。"""

    def __init__(self, wall_of_sample: Callable[[int], float], window: int = WINDOW_SEGMENTS,
                 start_offset_s: float | None = None):
        # wall_of_sample(输入采样序号) -> 设备播它的墙钟时刻
        self.wall_of_sample = wall_of_sample
        self.window = window
        self.start_offset_s = start_offset_s
        self.segments: collections.deque[Segment] = collections.deque()
        self._pending: list[bytes] = []
        self._frames = 0
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def add(self, frames: list[bytes]) -> None:
        with self.lock:
            for frame in frames:
                self._pending.append(frame)
                if len(self._pending) == SEGMENT_FRAMES:
                    first = self._frames - SEGMENT_FRAMES + 1
                    self._close(first)
                self._frames += 1
            self.changed.notify_all()

    def _close(self, first: int) -> None:
        sample = first * AAC_FRAME_SAMPLES - AAC_PRIMING_SAMPLES
        pdt = self.wall_of_sample(sample)
        pts = first * AAC_FRAME_SAMPLES * 90000 // RATE
        seq = self.segments[-1].seq + 1 if self.segments else 0
        data = id3_tag(pts, round(pdt * 1000)) + b"".join(self._pending)
        self.segments.append(Segment(seq, first, pdt, data))
        self._pending = []
        while len(self.segments) > self.window:
            self.segments.popleft()

    def segment(self, seq: int) -> bytes | None:
        with self.lock:
            for segment in self.segments:
                if segment.seq == seq:
                    return segment.data
        return None

    def playlist(self, query: str = "") -> str:
        with self.lock:
            listed = list(self.segments)
        suffix = f"?{query}" if query else ""
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:1",
                 f"#EXT-X-MEDIA-SEQUENCE:{listed[0].seq if listed else 0}"]
        if self.start_offset_s:
            lines.append(f"#EXT-X-START:TIME-OFFSET=-{self.start_offset_s:.3f},PRECISE=YES")
        for segment in listed:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{iso_ms(segment.pdt)}")
            lines.append(f"#EXTINF:{SEGMENT_S:.3f},")
            lines.append(f"{segment.seq}.aac{suffix}")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 编码

class AacEncoder:
    """常驻 ffmpeg：stdin 收 PCM，stdout 出 ADTS，读线程把帧交给 on_frames。"""

    def __init__(self, ffmpeg: str, on_frames: Callable[[list[bytes]], None], bitrate: str = "32k"):
        self.process = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
             "-c:a", "aac", "-b:a", bitrate, "-f", "adts", "-flush_packets", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.on_frames = on_frames
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        buffer = bytearray()
        stdout = self.process.stdout
        while True:
            chunk = stdout.read(4096)
            if not chunk:
                return
            buffer.extend(chunk)
            frames = split_adts(buffer)
            if frames:
                self.on_frames(frames)

    def write(self, pcm: bytes) -> None:
        try:
            self.process.stdin.write(pcm)
        except (BrokenPipeError, OSError):
            pass

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


# ---------------------------------------------------------------- 取声音

class ChannelTap:
    """从 LiveChannel 的声音队列里往前看（不出队）取 PCM，给手机流用。

    设备的发送端之后照常 pop_audio；手机比它早 H，所以这里读的是队列中段。cushion_s 是
    开始取之前要求攒下的余量（直播 HLS 源一阵一阵来，要几秒；中转源按实时速度来，一秒就够）。
    """

    def __init__(self, channel, cushion_s: float = 3.0):
        self.channel = channel
        self.cushion = int(cushion_s / CHUNK_S)
        self.last_key: float | None = None
        self.primed = False
        self.starved_since: float | None = None

    def _index_after(self) -> int:
        contents = self.channel.audio_content
        if self.last_key is None:
            return 0
        # 内容时间单调递增；队列不长（≤400），线性找即可。
        for at, key in enumerate(contents):
            if key > self.last_key:
                return at
        return len(contents)

    def take(self, now: float) -> tuple[object, bytes] | None:
        with self.channel.lock:
            at = self._index_after()
            available = len(self.channel.audio) - at
            if not self.primed:
                if available < self.cushion:
                    return None
                self.primed = True
            if available <= 0:
                # 断一会儿是常态；断太久说明余量用光了，重新攒。
                if self.starved_since is None:
                    self.starved_since = now
                elif now - self.starved_since > 0.5:
                    self.primed = False
                return None
            self.starved_since = None
            key, block = self.channel.audio_content[at], self.channel.audio[at]
        self.last_key = key
        return key, pcm_of(block)


def pcm_of(block: bytes) -> bytes:
    """队列里存的是线上格式（ADPCM 块，或 1280 字节 PCM）；手机流要 PCM。"""
    if len(block) == CHUNK_PCM_BYTES:
        return block
    return struct.pack(f"<{CHUNK_SAMPLES}h", *adpcm.decode(block, CHUNK_SAMPLES))


# ---------------------------------------------------------------- 一台设备的手机流

class ListenStream:
    """一台设备一个：时间表 + 编码器 + 段窗口。pump() 按实时节奏推进流的末端。"""

    def __init__(self, ffmpeg: str | None, lead_s: float = DEFAULT_LEAD_S,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 encoder_factory=None, prefill_s: float = PREFILL_S, start_offset_s: float | None = None):
        self.clock = clock
        now = clock()
        self.wall_offset = wall() - now
        self.ledger = Ledger(lead_s, start=now - prefill_s)
        start = self.ledger.start

        def wall_of_sample(sample: int) -> float:
            return self.wall_offset + start + sample / RATE + lead_s

        if start_offset_s is None:
            # 让播放器一开始就大致落在对齐位置：末端往回 (H - 段长 - 0.5) 秒。
            start_offset_s = max(SEGMENT_S * 2, lead_s - SEGMENT_S - 0.5)
        self.window = HlsWindow(wall_of_sample, start_offset_s=start_offset_s)
        factory = encoder_factory or (lambda on_frames: AacEncoder(ffmpeg, on_frames))
        self.encoder = factory(self.window.add)
        self.last_fetch = now
        self.taken = self.silent = 0

    def pump(self, source: ChannelTap | None) -> int:
        """把到期的条目追加进流；返回追加了几条。在发送循环里或单独线程里经常调用。"""
        now = self.clock()
        added = 0
        for _ in range(self.ledger.due(now)):
            got = source.take(now) if source is not None else None
            if got is None:
                self.ledger.append(None)
                self.encoder.write(SILENCE)
                self.silent += 1
            else:
                key, pcm = got
                self.ledger.append(key)
                self.encoder.write(pcm)
                self.taken += 1
            added += 1
        return added

    def touch(self) -> None:
        self.last_fetch = self.clock()

    def idle(self) -> bool:
        return self.clock() - self.last_fetch > IDLE_S

    def close(self) -> None:
        self.encoder.close()


# ---------------------------------------------------------------- 访问令牌

def issue_token(secret: bytes, device: str, now: float | None = None,
                lifetime_s: float = TOKEN_LIFETIME_S) -> str:
    """中转发给已登录的手机页；媒体服务用同一个密钥校验。放在网址里，因为播放器取段时
    带不带 cookie（尤其加到主屏幕的网页应用）没有保证。"""
    expires = int((time.time() if now is None else now) + lifetime_s)
    mac = hmac.new(secret, f"{device}.{expires}".encode(), hashlib.sha256).digest()
    return f"{expires}.{base64.urlsafe_b64encode(mac[:16]).decode().rstrip('=')}"


def check_token(secret: bytes | None, device: str, token: str | None, now: float | None = None) -> bool:
    if secret is None:
        return True       # 本机试验：不设密钥就不校验
    if not token or "." not in token:
        return False
    expires, _, given = token.partition(".")
    if not expires.isdigit() or int(expires) < (time.time() if now is None else now):
        return False
    mac = hmac.new(secret, f"{device}.{expires}".encode(), hashlib.sha256).digest()
    return hmac.compare_digest(given, base64.urlsafe_b64encode(mac[:16]).decode().rstrip("="))


# ---------------------------------------------------------------- HTTP

_PATH = re.compile(r"^/listen/(d\d{1,2})/(live\.m3u8|(\d{1,9})\.aac|bye)$")


def make_handler(streams: Callable[[str, bool], ListenStream | None], secret: bytes | None,
                 wall: Callable[[], float] = time.time, extra=None):
    """streams(device, start) 返回该设备的流；start 为真时没有就开一个（手机开始听）。

    extra(handler, path) 可处理其余路径（试验页用），返回 True 表示已处理。
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _route(self):
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path
            # nginx 去掉 /tv 前缀转进来；直接访问时也接受带前缀的。
            if path.startswith("/tv/"):
                path = path[3:]
            return path, urllib.parse.parse_qs(parts.query), parts.query

        def do_GET(self):
            path, query, raw = self._route()
            if path == "/listen/time":
                self._send(200, json.dumps({"t": wall() * 1000}).encode(), "application/json")
                return
            match = _PATH.match(path)
            if not match:
                if extra is None or not extra(self, path):
                    self._send(404, b"not found", "text/plain")
                return
            device, name, seq = match.group(1), match.group(2), match.group(3)
            token = (query.get("t") or [None])[0]
            if not check_token(secret, device, token):
                self._send(404, b"not found", "text/plain")
                return
            if name == "live.m3u8":
                stream = streams(device, True)
                if stream is None:
                    self._send(404, b"not found", "text/plain")
                    return
                stream.touch()
                tail = urllib.parse.urlencode({"t": token}) if token else ""
                self._send(200, stream.window.playlist(tail).encode(), "application/vnd.apple.mpegurl")
                return
            stream = streams(device, False)
            data = stream.window.segment(int(seq)) if (stream and seq is not None) else None
            if data is None:
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, data, "audio/aac")

        def do_POST(self):
            path, query, _ = self._route()
            match = _PATH.match(path)
            if match and match.group(2) == "bye" and check_token(secret, match.group(1),
                                                                 (query.get("t") or [None])[0]):
                stream = streams(match.group(1), False)
                if stream is not None:
                    stream.last_fetch = -1e9      # 下一次检查就当作不听了
                self._send(200, b"{}", "application/json")
                return
            self._send(404, b"not found", "text/plain")

        def log_message(self, *_args):
            pass

    return Handler
