"""Only one tray per data folder (design 5.1, "Single instance").

The first tray holds an OS file lock on `<data>/run/tray.lock` for as long as it
runs. The lock, not the file, is the signal: the OS drops it when the process
ends however it ends, so a crash never leaves a stale "already running".

A second launch (double-clicking the app again, the Start-menu entry while it
runs) cannot get the lock; it then asks the running core, through the control
API, for a console sign-in link, opens it, and exits -- which is what a person
who double-clicks an app that is already running expects to see.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class SingleInstance:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        """True if this process is now the only tray; False if another holds it."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        # The pid is for people reading the folder; nothing parses it. Written
        # after the lock byte on Windows, where the locked byte cannot be
        # rewritten through another handle but can through this one.
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def hand_off(client, open_url, wait_s: float = 30.0, sleep=time.sleep, clock=time.monotonic, log=print) -> bool:
    """Second launch: open the console of the tray that is already running.

    Waits a little because the first tray may have been started a moment ago
    and its core may not have written control.json yet. Returns whether a page
    was opened.
    """
    from .control import ControlError, ControlUnavailable
    deadline = clock() + wait_s
    while True:
        try:
            url = client.console_link()
            open_url(url)
            log("另一个小鼠电视已经在运行，已经替你打开电脑版。")
            return True
        except ControlUnavailable:
            if clock() >= deadline:
                log("另一个小鼠电视正在运行，但电视服务还没准备好；请稍后从托盘图标打开。")
                return False
            sleep(1.0)
        except ControlError as error:
            log(f"另一个小鼠电视正在运行，打开电脑版失败（{error}）。")
            return False
