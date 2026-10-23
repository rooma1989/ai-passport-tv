"""监督进程：启动中转、每个型号一个电视服务、网页前端，掉了就重启；每晚备份；写状态给 /healthz。

和 tools/launch.py 一样的思路（启动、看着、重启、用中文报告），但这里是长期运行的服务：子进程退出
不结束整个服务器，等一会儿再起；收到 SIGTERM（docker stop、Ctrl+C）才把子进程都停掉再退出。
每个子进程的输出一行一行加上前缀（relay: 、xingzhi: ……）转到自己的标准输出，Docker 的日志轮转管大小。

电脑客户端（--desktop，docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md 第 5.1 节）多几样：
- 打包版里没有 `python -m`：子进程用同一个可执行文件加子命令启动（child_command，tools/packaged_entry.py 分派）。
- 子进程随监督进程一起结束，哪怕监督进程是被强行杀掉的：POSIX 上每个子进程在自己的进程组里，标准输入是一根
  监督进程从不写的管道（tools/parentwatch.py）；Windows 上每个子进程在自己的作业对象里，停一个子进程就是结束
  它的整个作业（连同 ffmpeg 孙进程），监督进程死了作业句柄一关，系统把它们全结束。
- 停止不靠信号：本机控制接口（control.py）设 stopping；托盘关掉给监督进程的标准输入管道也一样（watch_stdin）。
- 换了网络（受信任的网络，network.py）：rebuild_requested —— 停掉子进程，用 pending 按新的地址重新造一遍再起。
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
# tools/parentwatch.py 的 WATCH_ENV：子进程看到它（和标准输入上的管道）就在监督进程不在时自己退出。
WATCH_ENV = "TV_WATCH_PARENT"
_CREATE_NO_WINDOW = 0x08000000


class Child:
    """watch：电脑客户端的子进程（见模块说明）——给它一根监视用的管道、自己的进程组或作业对象。
    cwd：子进程的工作目录（None：和监督进程一样）。"""

    def __init__(self, name: str, argv: list[str], env: dict, log=print, cwd: Path | None = None,
                 watch: bool = False):
        self.name, self.argv, self.env, self.log = name, argv, env, log
        self.cwd, self.watch_parent = cwd, watch
        self.process: subprocess.Popen | None = None
        self.job = None
        self.started = 0.0
        self.delay = RESTART_MIN_S
        self.next_start = 0.0
        self.restarts = 0

    def start(self) -> None:
        env = dict(self.env)
        kwargs: dict = {}
        if self.watch_parent:
            env[WATCH_ENV] = "1"
            stdin = subprocess.PIPE
            if os.name == "posix":
                kwargs["start_new_session"] = True
        else:
            env.pop(WATCH_ENV, None)
            stdin = subprocess.DEVNULL
        if os.name == "nt":
            # 托盘不带控制台启动核心；子进程是控制台程序，不加这一项每个都会弹出一个黑窗口。
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        if self.cwd is not None:
            kwargs["cwd"] = str(self.cwd)
        self.process = subprocess.Popen(self.argv, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, **kwargs)
        if self.watch_parent and os.name == "nt":
            from tools import parentwatch
            self.job = parentwatch.new_job()
            parentwatch.assign(self.job, self.process)
        self.started = time.monotonic()
        self._pumping = threading.Thread(target=self._pump, args=(self.process,), daemon=True)
        self._pumping.start()

    def _pump(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stdout:
                self.log(f"{self.name}: {line.rstrip()}")
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.running else None

    def watch(self, now: float) -> None:
        """退出了就安排重启；到时间了就起。"""
        if self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        if self.next_start == 0.0:
            self._release()
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
        if not self.running:
            return
        if self.watch_parent and os.name == "nt" and self.job is not None:
            from tools import parentwatch
            if parentwatch.terminate_job(self.job):
                return
        if self.watch_parent and os.name == "posix":
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                return
            except OSError:
                pass
        self.process.terminate()

    def _kill(self) -> None:
        if self.watch_parent and os.name == "posix":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
                return
            except OSError:
                pass
        self.process.kill()

    def _release(self) -> None:
        """子进程已经退出：关掉它的作业对象（里面还剩的孙进程跟着结束）和监视管道。"""
        if self.job is not None:
            from tools import parentwatch
            parentwatch.close_job(self.job)
            self.job = None
        if self.process is not None and self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.watch_parent and os.name == "posix" and self.process is not None:
            # 子进程自己退出了，它的进程组里可能还剩 ffmpeg：一起结束。
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                pass

    def wait_or_kill(self, deadline: float) -> None:
        if self.process is None:
            return
        try:
            self.process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self._kill()
            self.process.wait()
        self._release()


class Supervisor:
    """children：一开始就能起的子进程。pending：还不能起时（电脑客户端还在下载 ffmpeg）每半秒问一次的
    函数，返回子进程列表（可以起了）或 None；rebuild_requested 时也用它重新造子进程（参数变了）。
    tick：每半秒调一次（电脑客户端更新状态）。"""

    def __init__(self, layout: bootstrap.Layout, children: list[Child], keep_days: int, log=print,
                 pending=None, tick=None):
        self.layout, self.children, self.keep_days, self.log = layout, children, keep_days, log
        self.pending, self.tick = pending, tick
        self.stopping = threading.Event()
        self.restart_requested = threading.Event()
        self.rebuild_requested = threading.Event()
        self.started = False
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

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        names = ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK")   # SIGBREAK：Windows 控制台的 Ctrl+Break
        for name in names:
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: self.stopping.set())

    def _stop_children(self) -> None:
        for child in reversed(self.children):
            child.stop()
        deadline = time.monotonic() + STOP_WAIT_S
        for child in reversed(self.children):
            child.wait_or_kill(deadline)

    def run(self) -> int:
        self._install_signals()
        next_status = next_backup = 0.0
        while not self.stopping.is_set():
            now = time.monotonic()
            if not self.started:
                if self.pending is not None and not self.children:
                    ready = self.pending()
                    if ready is not None:
                        self.children = list(ready)
                if self.children:
                    for child in self.children:
                        child.start()
                    self.started = True
            else:
                for child in self.children:
                    child.watch(now)
            if self.rebuild_requested.is_set():
                self.rebuild_requested.clear()
                # 还没起的时候不用管：pending 第一次造子进程时就用新的参数。
                if self.started and self.pending is not None:
                    self.log("按新的地址和设置重启所有子进程…")
                    self._stop_children()
                    ready = self.pending()
                    if ready is not None:
                        self.children = list(ready)
                    for child in self.children:
                        child.delay, child.next_start = RESTART_MIN_S, 0.0
                        child.start()
            if self.restart_requested.is_set() and self.started:
                self.restart_requested.clear()
                self.log("正在重启所有子进程…")
                self._stop_children()
                for child in self.children:
                    child.delay, child.next_start = RESTART_MIN_S, 0.0
                    child.start()
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
            if self.tick is not None:
                try:
                    self.tick()
                except Exception as error:      # noqa: BLE001 —— 状态更新出错不能停掉服务器
                    self.log(f"状态更新出错：{type(error).__name__}: {error}")
            self.stopping.wait(0.5)
        self.log("正在停止…")
        self._stop_children()
        try:
            self.layout.status.unlink()
        except OSError:
            pass
        self.log("已停止。")
        return 0


def watch_stdin(event: threading.Event, stream=None) -> bool:
    """托盘启动核心时给的管道（TV_WATCH_PARENT=1）：读到文件尾（托盘退出或被杀）就设 event，正常停止。
    返回有没有开始看。和 tools/parentwatch.start() 一样读文件描述符（不碰缓冲对象，见那里的说明）。"""
    stream = sys.stdin if stream is None else stream
    if not os.environ.get(WATCH_ENV) or stream is None:
        return False
    try:
        descriptor = stream.fileno()
    except (OSError, ValueError):
        return False

    def watch() -> None:
        try:
            while os.read(descriptor, 1):
                pass
        except OSError:
            pass
        event.set()

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
    return True


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def python_command() -> list[str]:
    return [sys.executable, "-u"]


def child_command(module: str, desktop: bool = False) -> list[str]:
    """起一个子进程（module 是 server.relay 这样的模块名）的命令开头，后面接它自己的参数。

    - 打包版：同一个可执行文件加子命令（tools/subcommands.py 的 CHILD_COMMANDS），没有 `python -m`。
    - 电脑客户端从源代码跑：经 tools/packaged_entry.py 走同一个分派，和打包版一条路（测试不用先打包）。
    - 其余（Docker、./run.sh --selfhost、云端）：和以前一样 `python -u -m 模块`。
    """
    if is_frozen() or desktop:
        # Docker 镜像里没有 tools/：只在这两种情况下用到。
        from tools.subcommands import CHILD_COMMANDS
    if is_frozen():
        return [sys.executable, CHILD_COMMANDS[module]]
    if desktop:
        entry = Path(__file__).resolve().parents[2] / "tools" / "packaged_entry.py"
        return python_command() + [str(entry), CHILD_COMMANDS[module]]
    return python_command() + ["-m", module]
