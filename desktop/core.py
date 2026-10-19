"""Starting, watching and stopping the core program (design 5.1).

The tray is the core's parent. The core is put in the tray's job object on
Windows (`tools/parentwatch.attach`, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) and
given a never-written stdin pipe on POSIX, so quitting or killing the tray stops
the core and, through the core, everything it started.

If the core exits on its own it is started again after a growing delay, the
same policy as the supervisor uses for its children.
"""

from __future__ import annotations

import collections
import os
import re
import subprocess
import threading
import time

RESTART_MIN_S = 2.0
RESTART_MAX_S = 60.0
STABLE_S = 120.0
STOP_WAIT_S = 15.0
TAIL_LINES = 200
# The core's exit code when run/core.lock is held by another core (P1c contract).
ANOTHER_CORE_EXIT = 3

# Windows: no console window for the core (the tray is a windowed program) and
# its own process group, so a Ctrl+Break meant for it reaches nothing else.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

_REDACT = (
    re.compile(r"(setup#)[A-Za-z0-9-]+"),
    re.compile(r"(设置码[:：]\s*)\S+"),
    re.compile(r"((?:token|tvs|key)=)[A-Za-z0-9._~-]+", re.IGNORECASE),
    re.compile(r"(/TV/[PR]/)[A-Za-z0-9._~-]+"),
)


def redact(line: str) -> str:
    """Remove one-time codes and tokens from a line before it reaches tray.log."""
    for pattern in _REDACT:
        line = pattern.sub(r"\1…", line)
    return line


def no_window_flags() -> int:
    return CREATE_NO_WINDOW if os.name == "nt" else 0


class CoreProcess:
    def __init__(self, command, env: dict | None = None, log=print, popen=subprocess.Popen,
                 attach=None, clock=time.monotonic):
        """command: a list, or a callable returning one (re-evaluated at each start)."""
        self._command = command
        self.env = env
        self.log = log
        self._popen = popen
        self._attach = attach
        self._clock = clock
        self.process = None
        self.started = 0.0
        self.delay = RESTART_MIN_S
        self.next_start = 0.0
        self.restarts = 0
        self.last_exit: int | None = None
        self.wanted = False
        self.tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def state(self) -> str:
        """"running" | "restarting" | "stopped"."""
        if self.running:
            return "running"
        return "restarting" if self.wanted else "stopped"

    def start(self) -> None:
        with self._lock:
            self.wanted = True
            if self.running:
                return
            argv = self._command() if callable(self._command) else list(self._command)
            env = dict(os.environ if self.env is None else self.env)
            # The core may watch this pipe (tools/parentwatch.start) and leave
            # when the tray is gone, however the tray ended.
            env["TV_WATCH_PARENT"] = "1"
            env.setdefault("PYTHONIOENCODING", "utf-8")
            kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)
            if os.name == "nt":
                kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            self.process = self._popen(argv, **kwargs)
            self.started = self._clock()
            self.next_start = 0.0
            attach = self._attach
            if attach is None:
                try:
                    from tools.parentwatch import attach
                except ImportError:     # pragma: no cover - tools/ always ships with the tray
                    attach = None
            if attach is not None:
                try:
                    attach(self.process)
                except Exception:       # noqa: BLE001 - a backstop, never a reason not to run
                    pass
            stdout = getattr(self.process, "stdout", None)
            if stdout is not None:
                threading.Thread(target=self._pump, args=(stdout,), daemon=True, name="core-output").start()
        self.log(f"已启动电视服务（进程 {getattr(self.process, 'pid', '?')}）")

    def _pump(self, stream) -> None:
        try:
            for line in stream:
                self.tail.append(redact(line.rstrip()))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _close_stdin(process) -> None:
        # The never-written pipe the core may watch; closed once the core is gone.
        stdin = getattr(process, "stdin", None)
        if stdin is not None and hasattr(stdin, "close"):
            try:
                stdin.close()
            except (OSError, ValueError):
                pass

    def watch(self) -> str | None:
        """Call regularly. Returns "exited" once when the core died, "restarted" when started again."""
        if not self.wanted or self.process is None:
            return None
        code = self.process.poll()
        if code is None:
            return None
        now = self._clock()
        if self.next_start == 0.0:
            if now - self.started >= STABLE_S:
                self.delay = RESTART_MIN_S
            self.last_exit = code
            self._close_stdin(self.process)
            if code == ANOTHER_CORE_EXIT:
                # Another core already serves this data folder (its run/core.lock
                # is held): use it through control.json, and only try again later.
                self.log("这个数据文件夹上已经有一个电视服务在运行，改用它")
                self.next_start = now + RESTART_MAX_S
                return "exited"
            self.log(f"电视服务退出了（代码 {code}），{self.delay:.0f} 秒后重启")
            for line in list(self.tail)[-30:]:
                self.log(f"  core: {line}")
            self.next_start = now + self.delay
            self.delay = min(RESTART_MAX_S, self.delay * 2)
            return "exited"
        if now >= self.next_start:
            self.restarts += 1
            self.start()
            return "restarted"
        return None

    def stop(self, client=None, wait_s: float = STOP_WAIT_S) -> None:
        """Ask the core to stop through the control API, then terminate, then kill."""
        with self._lock:
            self.wanted = False
            process = self.process
        if process is None or process.poll() is not None:
            if process is not None:
                self._close_stdin(process)
            return
        asked = False
        if client is not None:
            try:
                client.stop()
                asked = True
            except Exception:       # noqa: BLE001 - fall through to terminate
                pass
        try:
            process.wait(timeout=wait_s if asked else 0.1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=wait_s)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self._close_stdin(process)
        self.log("电视服务已停止")

    def restart(self, client=None) -> None:
        self.stop(client)
        self.delay = RESTART_MIN_S
        self.start()
