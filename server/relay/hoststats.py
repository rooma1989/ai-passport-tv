"""总后台的主机负载：每 5 秒读一次 /proc（CPU、内存、网卡流量），在内存里留 24 小时。

只用标准库，一个后台线程，每次只读三个小文件。留多少：最近 15 分钟每 5 秒一个点，
更早的按分钟合成一个点（平均），24 小时 1440 个点，总共几百 KB 以内。
中转重启就从头记（不落盘）。没有 /proc 的系统（macOS 上开发、跑测试）不采样，页面说「不支持」。

每个点：(时刻, CPU 占用 %, 已用内存 MB, 总内存 MB, 收 字节/秒, 发 字节/秒)。
网卡只算真实的（不算 lo）；「发」就是公网出站流量（这台主机只有一块对外的网卡）。
"""

from __future__ import annotations

import collections
import os
import threading
import time
from pathlib import Path

INTERVAL_S = 5.0
FINE_S = 15 * 60
COARSE_S = 60
KEEP_S = 24 * 3600
# 进程表（电视服务、中转、ffmpeg）只在有人看总后台时读：扫 /proc/<pid> 比三个文件贵。
PROCESS_NAMES = (("server.tv_server", "电视服务"), ("server.relay", "中转"), ("ffmpeg", "ffmpeg"),
                 ("muse-asr", "语音识别"), ("nginx", "nginx"))


def read_cpu(root: Path) -> tuple[int, int] | None:
    """(总时间, 空闲时间)，单位是时钟滴答；读不到为 None。"""
    try:
        first = (root / "stat").read_text(encoding="ascii").splitlines()[0].split()
    except (OSError, IndexError, UnicodeError):
        return None
    if not first or first[0] != "cpu":
        return None
    try:
        values = [int(v) for v in first[1:]]
    except ValueError:
        return None
    # user nice system idle iowait irq softirq steal（guest 已经算在 user 里）。
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values[:8]), idle


def read_memory(root: Path) -> tuple[float, float] | None:
    """(已用 MB, 总 MB)。已用 = 总量 - MemAvailable（缓存可以随时让出来，不算用掉）。"""
    fields = {}
    try:
        for line in (root / "meminfo").read_text(encoding="ascii").splitlines():
            name, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                fields[name] = int(parts[0])
    except (OSError, ValueError, UnicodeError):
        return None
    total, available = fields.get("MemTotal"), fields.get("MemAvailable", fields.get("MemFree"))
    if not total or available is None:
        return None
    return (total - available) / 1024, total / 1024


def read_network(root: Path) -> tuple[int, int] | None:
    """所有非回环网卡累计的 (收, 发) 字节。"""
    received = sent = 0
    try:
        lines = (root / "net" / "dev").read_text(encoding="ascii").splitlines()[2:]
    except (OSError, UnicodeError):
        return None
    for line in lines:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if name.strip() == "lo" or len(parts) < 9:
            continue
        try:
            received += int(parts[0])
            sent += int(parts[8])
        except ValueError:
            continue
    return received, sent


class HostStats:
    def __init__(self, root: Path | str = "/proc", clock=time.time, interval_s: float = INTERVAL_S):
        self.root = Path(root)
        self.clock = clock
        self.interval_s = interval_s
        self.lock = threading.Lock()
        self.fine: collections.deque = collections.deque(maxlen=int(FINE_S / interval_s) + 1)
        self.coarse: collections.deque = collections.deque(maxlen=int(KEEP_S / COARSE_S) + 1)
        self._bucket: list[tuple] = []
        self._previous: tuple | None = None
        self._processes: dict[int, tuple[float, int]] = {}
        self.thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return (self.root / "stat").is_file()

    def sample(self) -> tuple | None:
        """读一次，和上一次比出速率，存一个点；第一次（没有上一次）只记下读数。"""
        now = self.clock()
        cpu, memory, network = read_cpu(self.root), read_memory(self.root), read_network(self.root)
        if cpu is None or memory is None:
            return None
        previous, self._previous = self._previous, (now, cpu, network)
        if previous is None:
            return None
        elapsed = now - previous[0]
        total, idle = cpu[0] - previous[1][0], cpu[1] - previous[1][1]
        if elapsed <= 0 or total <= 0:
            return None
        busy = max(0.0, min(100.0, 100.0 * (total - idle) / total))
        rx = tx = 0.0
        if network is not None and previous[2] is not None:
            rx = max(0, network[0] - previous[2][0]) / elapsed
            tx = max(0, network[1] - previous[2][1]) / elapsed
        point = (now, round(busy, 1), round(memory[0]), round(memory[1]), round(rx), round(tx))
        with self.lock:
            self.fine.append(point)
            self._fold(point)
        return point

    def _fold(self, point: tuple) -> None:
        """攒满一分钟就合成一个点（各项取平均，时刻取这一分钟的开头）。"""
        if self._bucket and int(point[0] // COARSE_S) != int(self._bucket[0][0] // COARSE_S):
            n = len(self._bucket)
            start = int(self._bucket[0][0] // COARSE_S) * COARSE_S
            merged = tuple(round(sum(p[i] for p in self._bucket) / n, 1) for i in range(1, 6))
            self.coarse.append((start,) + merged)
            self._bucket = []
        self._bucket.append(point)

    def latest(self) -> tuple | None:
        with self.lock:
            return self.fine[-1] if self.fine else None

    def history(self) -> list[tuple]:
        """24 小时的分钟点，加上还没合成的最近几个 5 秒点（页面画曲线用）。"""
        with self.lock:
            return list(self.coarse) + list(self._bucket)

    def recent(self) -> list[tuple]:
        with self.lock:
            return list(self.fine)

    def processes(self) -> list[dict]:
        """几个关心的进程：名字、pid、常驻内存 MB、CPU %（和上次看比；第一次为 None）。"""
        if not self.available:
            return []
        try:
            ticks = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError, AttributeError):
            ticks = 100
        page_mb = (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096) / (1024 * 1024)
        now = time.monotonic()
        rows, seen = [], {}
        for entry in self.root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                label = next((name for pattern, name in PROCESS_NAMES if pattern in command), None)
                if label is None:
                    continue
                stat = (entry / "stat").read_text(encoding="ascii", errors="replace")
                fields = stat[stat.rindex(")") + 2:].split()
                cpu_ticks = int(fields[11]) + int(fields[12])
                rss_mb = int(fields[21]) * page_mb
            except (OSError, ValueError, IndexError):
                continue
            pid = int(entry.name)
            seen[pid] = (now, cpu_ticks)
            before = self._processes.get(pid)
            percent = None
            if before is not None and now > before[0]:
                percent = round(100.0 * (cpu_ticks - before[1]) / ticks / (now - before[0]), 1)
            rows.append({"name": label, "pid": pid, "rss_mb": round(rss_mb), "cpu": percent})
        self._processes = seen
        return sorted(rows, key=lambda r: (r["name"], r["pid"]))

    def start(self) -> bool:
        """后台线程开始采样；没有 /proc 时不开，返回 False。"""
        if not self.available or self.thread is not None:
            return False

        def loop() -> None:
            while True:
                try:
                    self.sample()
                except Exception:   # noqa: BLE001 —— 负载曲线断一个点不要紧，线程不能死
                    pass
                time.sleep(self.interval_s)

        self.thread = threading.Thread(target=loop, name="hoststats", daemon=True)
        self.thread.start()
        return True
