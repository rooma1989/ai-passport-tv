"""What the pre-rename client (小智电视 / Xiaozhi TV, up to 0.2.5-beta) leaves behind.

On the first start of RooTV the old tray may still be running -- it started at
login from the old login item. It holds the old data folder, so the folder
cannot be moved (tools/desktop_folders.py) and a hand-off would open the old
console. `stop_old_tray` asks it to quit the way an installer would; its core
stops with it (the tray's pipe closes on POSIX, the job object ends it on
Windows). Only a frozen old tray is stopped, never a Python process from a
checkout or anything whose name does not match.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

# The old tray executables: macOS and Windows "XiaozhiTV", Linux "xiaozhi-tv".
OLD_PROGRAMS = {"XiaozhiTV", "XiaozhiTV.exe", "xiaozhi-tv"}


def _pid(old_data: Path) -> int | None:
    """The old tray writes its pid into run/tray.lock (desktop/instance.py)."""
    try:
        text = (Path(old_data) / "run" / "tray.lock").read_text(encoding="ascii", errors="replace")
        return int(text.split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _program_name(pid: int, run=subprocess.run) -> str:
    try:
        result = run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return Path(result.stdout.strip()).name if result.returncode == 0 else ""


def stop_old_tray(old_data: Path, platform_name: str | None = None, run=subprocess.run, kill=os.kill,
                  log=None) -> bool:
    """Ask a running old tray to quit. True when one was asked."""
    platform_name = platform_name or sys.platform
    if platform_name == "win32":
        # The lock byte cannot be read on Windows, so no pid: stop the old
        # program by name (what the old uninstaller does too). Only called when
        # the old folder's lock is held.
        try:
            from .core import no_window_flags
            result = run(["taskkill", "/F", "/T", "/IM", "XiaozhiTV.exe"], capture_output=True, timeout=15,
                         creationflags=no_window_flags())
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
    pid = _pid(old_data)
    if pid is None or pid == os.getpid():
        return False
    name = _program_name(pid, run)
    if name not in OLD_PROGRAMS:
        if log:
            log(f"旧数据文件夹被进程 {pid}（{name or '不知道是什么'}）占着，不是旧版小智电视，没有动它")
        return False
    try:
        kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True
