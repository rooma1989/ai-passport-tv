"""「用手机听」的本机试验：合成节目 + 模拟设备 + HLS 手机流 + 试验页。

不连设备、不连真的媒体服务。节目是每秒一声「嘀」的合成声音（像 server.media 的同步片），
模拟设备按 Ledger 晚 H 秒播同一份时间表，并把它播「嘀」的时刻发给网页：网页上的方块在那一刻
闪一下，相当于电视画面。iPhone 和电脑在同一个局域网时，用 --lan 打开，手机 Safari 访问打印出的
地址即可试听、看对齐。

    python3 tools/listen_proto/serve.py --ffmpeg /path/to/ffmpeg [--lan] [--lead 6] [--bursty]
    python3 tools/listen_proto/serve.py --ffmpeg ... --measure 40     # 无界面测量，打印数字

--bursty 让节目一阵一阵来（每 2 秒来 2 秒），模拟直播 HLS 源。
"""

from __future__ import annotations

import argparse
import collections
import functools
import json
import math
import os
import resource
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import listen  # noqa: E402

HERE = Path(__file__).resolve().parent
DEVICE = "d0"
# 模拟设备：发送端把一块声音交给设备后，设备多久播它（设备队列 + DMA）。
DEVICE_QUEUE_S = 0.4


@functools.lru_cache(maxsize=None)
def _chunk_pcm(phase_ms: int) -> bytes:
    return chunk_pcm_uncached(phase_ms)


def chunk_pcm(content_ms: int) -> bytes:
    # 波形以 5 秒为周期（220/1000/2000 Hz 在 5 秒内都是整周期），算一次就够。
    return _chunk_pcm(content_ms % 5000)


def chunk_pcm_uncached(content_ms: int) -> bytes:
    """40 ms：很轻的 220 Hz 底音；每个整秒开头 50 ms 的 1 kHz「嘀」，每 5 秒一声高音。"""
    out = []
    for n in range(listen.CHUNK_SAMPLES):
        t_ms = content_ms + n * 1000 / listen.RATE
        within = t_ms % 1000
        value = 0.03 * math.sin(2 * math.pi * 220 * t_ms / 1000)
        if within < 50:
            freq = 2000 if (t_ms // 1000) % 5 == 0 else 1000
            value = 0.3 * math.sin(2 * math.pi * freq * t_ms / 1000)
        out.append(int(value * 32767))
    return struct.pack(f"<{listen.CHUNK_SAMPLES}h", *out)


class FakeChannel:
    """LiveChannel 的替身：同名的 lock / audio / audio_content 队列。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.audio: collections.deque = collections.deque(maxlen=600)
        self.audio_content: collections.deque = collections.deque(maxlen=600)

    def pop_until(self, key):
        """设备的发送端：出队直到拿到 key 那块（更旧的丢掉）。"""
        with self.lock:
            while self.audio_content and self.audio_content[0] < key:
                self.audio.popleft()
                self.audio_content.popleft()
            if self.audio_content and self.audio_content[0] == key:
                self.audio_content.popleft()
                return self.audio.popleft()
        return None


def produce(channel: FakeChannel, stop: threading.Event, bursty: bool) -> None:
    started = time.monotonic()
    made = 0
    while not stop.is_set():
        elapsed = time.monotonic() - started
        if bursty:
            # 每 2 秒一次来 2 秒的量。
            due = int(elapsed // 2 + 1) * 50
        else:
            due = int(elapsed / listen.CHUNK_S) + 1
        while made < due:
            content = made * listen.CHUNK_MS
            with channel.lock:
                channel.audio.append(chunk_pcm(content))
                channel.audio_content.append(content)
            made += 1
        time.sleep(0.01)


class FakeDevice:
    """模拟设备的发送端：每 40 ms 发一块，设备 DEVICE_QUEUE_S 后播它。"""

    def __init__(self, stream: listen.ListenStream, channel: FakeChannel):
        self.stream, self.channel = stream, channel
        self.beeps: collections.deque = collections.deque(maxlen=40)   # (墙钟毫秒, 高音?)
        self.errors: collections.deque = collections.deque(maxlen=2000)
        self.lock = threading.Lock()

    def run(self, stop: threading.Event) -> None:
        origin = time.monotonic() + 0.3
        sent = 0
        while not stop.is_set():
            send_at = origin + sent * listen.CHUNK_S
            delay = send_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            play_at = send_at + DEVICE_QUEUE_S
            kind, entry = self.stream.ledger.for_device(play_at)
            if kind == "entry":
                self.errors.append(play_at - self.stream.ledger.target(entry))
                if entry.key is not None:
                    self.channel.pop_until(entry.key)
                    if entry.key % 1000 == 0:
                        with self.lock:
                            self.beeps.append((round((self.stream.wall_offset + play_at) * 1000),
                                               (entry.key // 1000) % 5 == 0))
            sent += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG", "ffmpeg"))
    parser.add_argument("--port", type=int, default=8199)
    parser.add_argument("--lan", action="store_true", help="listen on all interfaces (phone on the same Wi-Fi)")
    parser.add_argument("--lead", type=float, default=listen.DEFAULT_LEAD_S)
    parser.add_argument("--cushion", type=float, default=1.0)
    parser.add_argument("--bursty", action="store_true")
    parser.add_argument("--measure", type=float, default=0, help="run headless for N seconds and print numbers")
    args = parser.parse_args()

    stop = threading.Event()
    channel = FakeChannel()
    seen: dict[int, float] = {}            # 段号 -> 本机第一次见到它的时刻（测量用）

    stream = listen.ListenStream(args.ffmpeg, lead_s=args.lead)
    original_add = stream.window.add

    def add_and_note(frames):
        before = stream.window.segments[-1].seq if stream.window.segments else -1
        original_add(frames)
        now = time.monotonic()
        for segment in list(stream.window.segments):
            if segment.seq > before:
                seen[segment.seq] = now

    stream.encoder.on_frames = add_and_note
    tap = listen.ChannelTap(channel, cushion_s=args.cushion)
    device = FakeDevice(stream, channel)

    def pump():
        while not stop.is_set():
            stream.pump(tap)
            time.sleep(0.02)

    threads = [threading.Thread(target=produce, args=(channel, stop, args.bursty), daemon=True),
               threading.Thread(target=pump, daemon=True),
               threading.Thread(target=device.run, args=(stop,), daemon=True)]
    for thread in threads:
        thread.start()

    page = (HERE / "index.html").read_bytes()

    def extra(handler, path):
        if path in ("/", "/index.html"):
            handler._send(200, page, "text/html; charset=utf-8")
            return True
        if path == "/device.json":
            with device.lock:
                beeps = list(device.beeps)
            body = {"beeps": beeps, "lead_s": args.lead, "skipped": stream.ledger.skipped,
                    "fillers": stream.ledger.fillers, "taken": stream.taken, "silent": stream.silent}
            handler._send(200, json.dumps(body).encode(), "application/json")
            return True
        return False

    handler = listen.make_handler(lambda d, start: stream if d == DEVICE else None, None, extra=extra)
    server = ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = "127.0.0.1"
    if args.lan:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            host = probe.getsockname()[0]
        except OSError:
            pass
        finally:
            probe.close()
    print(f"open http://{host}:{args.port}/   (lead H={args.lead}s, bursty={args.bursty})", flush=True)

    if not args.measure:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        stop.set()
        stream.close()
        return 0

    # ---- 无界面测量 ----
    base = f"http://127.0.0.1:{args.port}/listen/{DEVICE}/"
    fetch_ms, playlist_bytes, segment_bytes = [], [], []
    got: set[int] = set()
    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    t0 = time.monotonic()
    while time.monotonic() - t0 < args.measure:
        started = time.monotonic()
        text = urllib.request.urlopen(base + "live.m3u8").read()
        fetch_ms.append((time.monotonic() - started) * 1000)
        playlist_bytes.append(len(text))
        for line in text.decode().splitlines():
            if line.endswith(".aac"):
                seq = int(line.split(".")[0])
                if seq not in got:
                    got.add(seq)
                    segment_bytes.append(len(urllib.request.urlopen(base + line).read()))
        time.sleep(listen.SEGMENT_S)
    elapsed = time.monotonic() - t0
    cpu1 = resource.getrusage(resource.RUSAGE_SELF)
    py_cpu = (cpu1.ru_utime + cpu1.ru_stime) - (cpu0.ru_utime + cpu0.ru_stime)
    pid = stream.encoder.process.pid
    ff_cpu = os.popen(f"ps -o time= -p {pid}").read().strip()
    # 段出现时刻 - 段里最后一个采样成为流的一部分的时刻 = 编码 + 攒段之外的额外延迟
    lags = []
    ledger = stream.ledger
    for segment in list(stream.window.segments):
        if segment.seq in seen:
            last_sample = (segment.first_frame + listen.SEGMENT_FRAMES) * listen.AAC_FRAME_SAMPLES \
                - listen.AAC_PRIMING_SAMPLES
            published = ledger.start + last_sample / listen.RATE
            lags.append((seen[segment.seq] - published) * 1000)
    errors = sorted(abs(e) * 1000 for e in device.errors)
    stop.set()
    stream.close()
    server.shutdown()

    def pct(values, q):
        return values[min(len(values) - 1, int(q * len(values)))] if values else float("nan")

    print(json.dumps({
        "seconds": round(elapsed, 1),
        "segments_fetched": len(segment_bytes),
        "segment_kB_per_s": round(sum(segment_bytes) / elapsed / 1000, 2),
        "playlist_bytes_avg": round(sum(playlist_bytes) / len(playlist_bytes)),
        "playlist_fetch_ms_p50": round(pct(sorted(fetch_ms), 0.5), 2),
        "encode_extra_lag_ms_p50": round(pct(sorted(lags), 0.5), 1),
        "encode_extra_lag_ms_max": round(max(lags), 1) if lags else None,
        "device_vs_schedule_abs_ms_p50": round(pct(errors, 0.5), 1),
        "device_vs_schedule_abs_ms_p99": round(pct(errors, 0.99), 1),
        "ledger_skipped": ledger.skipped, "ledger_fillers": ledger.fillers,
        "entries_content": stream.taken, "entries_silence": stream.silent,
        "python_cpu_pct": round(py_cpu / elapsed * 100, 1),
        "ffmpeg_cpu_time": ff_cpu,
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
