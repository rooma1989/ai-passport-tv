"""监督进程：启动中转、每个型号一个电视服务、网页前端，掉了就重启；每晚备份；写状态给 /healthz。

和 tools/launch.py 一样的思路（启动、看着、重启、用中文报告），但这里是长期运行的服务：子进程退出
不结束整个服务器，等一会儿再起；收到 SIGTERM（docker stop、Ctrl+C）才把子进程都停掉再退出。
每个子进程的输出一行一行加上前缀（relay: 、xingzhi: ……）转到自己的标准输出，Docker 的日志轮转管大小。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import bootstrap

# 子进程退出后等多久再起：越来越长，最多 RESTART_MAX_S；跑够 STABLE_S 秒才算好了，下次从头算。
RESTART_MIN_S = 2.0
RESTART_MAX_S = 30.0
STABLE_S = 60.0
STOP_WAIT_S = 10.0
STATUS_EVERY_S = 5.0
BACKUP_CHECK_S = 3600.0


class Child:
    def __init__(self, name: str, argv: list[str], env: dict, log=print):
        self.name, self.argv, self.env, self.log = name, argv, env, log
        self.process: subprocess.Popen | None = None
        self.started = 0.0
        self.delay = RESTART_MIN_S
        self.next_start = 0.0
        self.restarts = 0

    def start(self) -> None:
        self.process = subprocess.Popen(self.argv, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                        bufsize=1)
        self.started = time.monotonic()
        threading.Thread(target=self._pump, args=(self.process,), daemon=True).start()

    def _pump(self, process: subprocess.Popen) -> None:
        for line in process.stdout:
            self.log(f"{self.name}: {line.rstrip()}")

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def watch(self, now: float) -> None:
        """退出了就安排重启；到时间了就起。"""
        if self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        if self.next_start == 0.0:
            if now - self.started >= STABLE_S:
                self.delay = RESTART_MIN_S
            self.log(f"{self.name} 退出了（代码 {code}），{self.delay:.0f} 秒后重启")
            self.next_start = now + self.delay
            self.delay = min(RESTART_MAX_S, self.delay * 2)
        elif now >= self.next_start:
            self.next_start = 0.0
            self.restarts += 1
            self.start()

    def stop(self) -> None:
        if self.running:
            self.process.terminate()

    def wait_or_kill(self, deadline: float) -> None:
        if self.process is None:
            return
        try:
            self.process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class Supervisor:
    def __init__(self, layout: bootstrap.Layout, children: list[Child], keep_days: int, log=print):
        self.layout, self.children, self.keep_days, self.log = layout, children, keep_days, log
        self.stopping = threading.Event()
        self._setup_seen = layout.setup_code.exists()

    def _write_status(self) -> None:
        status = {"at": time.time(), "children": {c.name: c.running for c in self.children},
                  "restarts": {c.name: c.restarts for c in self.children}}
        tmp = self.layout.status.with_name("status.json.tmp")
        tmp.write_text(json.dumps(status), encoding="utf-8")
        os.replace(tmp, self.layout.status)

    def _maybe_backup(self) -> None:
        if not bootstrap.nightly_due(self.layout):
            return
        try:
            made = bootstrap.backup(self.layout)
            removed = bootstrap.prune_backups(self.layout, self.keep_days)
            self.log(f"已备份数据库：backups/{made.name}" + (f"，删掉了 {len(removed)} 份旧备份" if removed else ""))
        except Exception as error:      # noqa: BLE001 —— 备份失败不能让服务器停下
            self.log(f"备份数据库失败：{type(error).__name__}: {error}")

    def run(self) -> int:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: self.stopping.set())
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, lambda *_: self.stopping.set())
        for child in self.children:
            child.start()
        next_status = next_backup = 0.0
        while not self.stopping.is_set():
            now = time.monotonic()
            for child in self.children:
                child.watch(now)
            if now >= next_status:
                next_status = now + STATUS_EVERY_S
                try:
                    self._write_status()
                except OSError:
                    pass
                if self._setup_seen and not self.layout.setup_code.exists():
                    self._setup_seen = False
                    self.log("管理员已经建好，设置码作废了。")
            if now >= next_backup:
                next_backup = now + BACKUP_CHECK_S
                self._maybe_backup()
            self.stopping.wait(0.5)
        self.log("正在停止…")
        for child in reversed(self.children):
            child.stop()
        deadline = time.monotonic() + STOP_WAIT_S
        for child in reversed(self.children):
            child.wait_or_kill(deadline)
        try:
            self.layout.status.unlink()
        except OSError:
            pass
        self.log("已停止。")
        return 0


def python_command() -> list[str]:
    return [sys.executable, "-u"]
