"""The PC client's data and log folders, and the one-time move from the old name.

The product was called 小智电视 / Xiaozhi TV until 0.2.5-beta and is RooTV
(小鼠电视) from then on (docs/superpowers/specs/2026-10-24-media-center-ui-design.md,
section 15, phase R). Its per-user folders are renamed with it:

| OS      | data (old -> new)                                      | logs (old -> new)                                      |
| ------- | ------------------------------------------------------ | ------------------------------------------------------ |
| macOS   | ~/Library/Application Support/XiaozhiTV -> .../RooTV   | ~/Library/Logs/XiaozhiTV -> ~/Library/Logs/RooTV       |
| Windows | %LOCALAPPDATA%\\XiaozhiTV -> %LOCALAPPDATA%\\RooTV       | inside the data folder (moves with it)                 |
| Linux   | $XDG_DATA_HOME/xiaozhi-tv -> $XDG_DATA_HOME/rootv      | $XDG_STATE_HOME/xiaozhi-tv/logs -> .../rootv/logs      |

`resolve()` answers "which folders does this run use" for both the tray
(desktop/paths.py) and the core (server/selfhost/desktop_main.py), so the two
always agree. On the first start after the rename it moves the old folder:

- only when the new folder has no data and the old one has;
- only when no process holds the old folder's run/tray.lock or run/core.lock
  (an old tray or core still running). The tray may first ask an old tray to
  quit (`stop_old`); if the locks stay held, this run keeps using the old
  folder -- the second-instance hand-off then reaches the running copy -- and
  the move happens on a later start;
- with one rename (`os.rename`), which is atomic on the same file system (both
  names are siblings): after a crash either the old or the new folder exists,
  never half of each. A failed rename (permissions, a file held open on
  Windows) leaves everything where it was and this run uses the old folder;
- afterwards the old path is recreated holding only MOVED-TO-RooTV.txt, which
  says where the data went (best effort; nothing reads it back).

When both folders hold data the new one is used and the old one is left alone.
An explicit folder (ROOTV_DATA, the old XIAOZHI_TV_DATA, `--data`) is never
moved or renamed.
"""

from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = "RooTV"
APP_DIR_LINUX = "rootv"
LEGACY_DIR = "XiaozhiTV"
LEGACY_DIR_LINUX = "xiaozhi-tv"

# An explicit data folder. The old name is still read (one release of overlap
# at least; spec section 15), the new one wins when both are set.
DATA_ENV = "ROOTV_DATA"
LEGACY_DATA_ENV = "XIAOZHI_TV_DATA"

NOTE_NAME = "MOVED-TO-RooTV.txt"
# What a running tray and core hold (desktop/instance.py, server/selfhost/desktop.py).
LOCK_FILES = ("tray.lock", "core.lock")
# Folder litter that does not make a folder "in use".
_IGNORED = {NOTE_NAME, ".DS_Store", "desktop.ini", "Thumbs.db"}

STOP_WAIT_S = 30.0


@dataclass
class Folders:
    data: Path
    logs: Path
    explicit: bool = False
    legacy: bool = False        # this run uses the old folder (the move could not happen yet)
    migrated: bool = False      # the old folder was moved in this run
    messages: list[str] = field(default_factory=list)


def explicit_data_dir(environ=None) -> Path | None:
    environ = os.environ if environ is None else environ
    for name in (DATA_ENV, LEGACY_DATA_ENV):
        if environ.get(name):
            return Path(environ[name]).expanduser()
    return None


def default_data_dir(system: str | None = None, environ=None, home: Path | None = None,
                     legacy: bool = False) -> Path:
    system = system or platform.system()
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if system == "Windows":
        # LOCALAPPDATA, not APPDATA: a SQLite database in a roaming profile can
        # be synced half-written on domain machines.
        local = environ.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / (LEGACY_DIR if legacy else APP_DIR)
    if system == "Darwin":
        return home / "Library" / "Application Support" / (LEGACY_DIR if legacy else APP_DIR)
    xdg = environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else home / ".local" / "share") / (LEGACY_DIR_LINUX if legacy else APP_DIR_LINUX)


def default_logs_dir(system: str | None = None, environ=None, home: Path | None = None,
                     legacy: bool = False) -> Path:
    system = system or platform.system()
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if system == "Darwin":
        return home / "Library" / "Logs" / (LEGACY_DIR if legacy else APP_DIR)
    if system == "Windows":
        return default_data_dir(system, environ, home, legacy) / "logs"
    xdg = environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else home / ".local" / "state") / (LEGACY_DIR_LINUX if legacy else APP_DIR_LINUX) / "logs"


def has_data(folder: Path) -> bool:
    """A folder that exists and holds anything besides the move note and OS litter."""
    try:
        return any(entry.name not in _IGNORED for entry in Path(folder).iterdir())
    except OSError:
        return False


# ------------------------------------------------------------------ locks

def _try_lock(path: Path):
    """An open handle holding the lock, None when another process holds it, or
    True when there is no lock file (so nobody can hold it)."""
    if not path.is_file():
        return True
    try:
        handle = open(path, "a+b")
    except OSError:
        return None
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
        return None
    return handle


def _release(handle) -> None:
    if handle is True or handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def lock_all(data: Path):
    """Hold every lock of a data folder: a list of handles, or None when a
    running tray or core holds one of them (nothing is kept in that case)."""
    held = []
    for name in LOCK_FILES:
        handle = _try_lock(Path(data) / "run" / name)
        if handle is None:
            for other in held:
                _release(other)
            return None
        held.append(handle)
    return held


def release_all(handles) -> None:
    for handle in handles or ():
        _release(handle)


def in_use(data: Path) -> bool:
    handles = lock_all(data)
    if handles is None:
        return True
    release_all(handles)
    return False


# ------------------------------------------------------------------ the move

def _note(old: Path, new: Path) -> str:
    return ("这个文件夹已经搬到：\n"
            f"{new}\n\n"
            "小智电视改名叫小鼠电视（RooTV）了，数据（账号、设备、频道、设置）都在新文件夹里。\n"
            "这个文件夹和这个说明可以删掉。\n\n"
            "This folder has moved to:\n"
            f"{new}\n\n"
            "Xiaozhi TV is now called RooTV; its data (accounts, devices, channels, settings) is in the new folder.\n"
            "You can delete this folder and this note.\n")


def _leave_note(old: Path, new: Path) -> None:
    try:
        old.mkdir(parents=True, exist_ok=True)
        tmp = old / (NOTE_NAME + ".tmp")
        tmp.write_text(_note(old, new), encoding="utf-8")
        os.replace(tmp, old / NOTE_NAME)
    except OSError:
        pass


def _clear_empty(folder: Path) -> bool:
    """Remove a folder that has no data (only litter), so a rename can take its name."""
    if not folder.exists():
        return True
    if has_data(folder) or not folder.is_dir() or folder.is_symlink():
        return False
    try:
        for entry in folder.iterdir():
            entry.unlink()
        folder.rmdir()
    except OSError:
        return False
    return True


def move_folder(old: Path, new: Path) -> str:
    """Rename old -> new (the caller has checked that nothing runs). "" on
    success, otherwise why not (and nothing changed)."""
    if not _clear_empty(new):
        return f"{new} 已经有东西"
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old, new)
    except OSError as error:
        return f"{type(error).__name__}: {error.strerror or error}"
    return ""


def _move_logs(old_logs: Path, new_logs: Path, messages: list[str]) -> None:
    if old_logs == new_logs or not old_logs.is_dir() or has_data(new_logs):
        return
    problem = move_folder(old_logs, new_logs)
    if problem:
        messages.append(f"旧的日志文件夹没有搬（{problem}），留在 {old_logs}")
        return
    messages.append(f"日志文件夹搬到了 {new_logs}")
    # Linux: ~/.local/state/xiaozhi-tv held only logs/.
    parent = old_logs.parent
    if parent.name == LEGACY_DIR_LINUX:
        try:
            parent.rmdir()
        except OSError:
            pass


def resolve(system: str | None = None, environ=None, home: Path | None = None, migrate: bool = True,
            stop_old=None, wait_s: float = STOP_WAIT_S, sleep=time.sleep, clock=time.monotonic) -> Folders:
    """The folders this run uses, moving the old ones first when that is safe.

    `stop_old(old_data) -> bool` (the tray passes one) asks a running old tray to
    quit; it returns whether it asked, and the locks are then waited for up to
    `wait_s` seconds. `messages` say what happened, for the caller's log.
    """
    system = system or platform.system()
    environ = os.environ if environ is None else environ
    explicit = explicit_data_dir(environ)
    if explicit is not None:
        return Folders(explicit, explicit / "logs", explicit=True)

    new = default_data_dir(system, environ, home)
    new_logs = default_logs_dir(system, environ, home)
    old = default_data_dir(system, environ, home, legacy=True)
    old_logs = default_logs_dir(system, environ, home, legacy=True)
    current = Folders(new, new_logs)
    if has_data(new):
        if has_data(old):
            current.messages.append(f"旧数据文件夹也还在：{old}（没有动它，用的是 {new}）")
        return current
    if not has_data(old):
        return current

    fallback = Folders(old, old_logs, legacy=True)
    if not migrate:
        return fallback
    handles = lock_all(old)
    if handles is None and stop_old is not None:
        if stop_old(old):
            fallback.messages.append("旧版小智电视还在运行，已经请它退出")
            deadline = clock() + wait_s
            while handles is None and clock() < deadline:
                sleep(0.5)
                handles = lock_all(old)
    if handles is None:
        fallback.messages.append(f"旧版小智电视还在用 {old}，这次先用它；退出旧版以后再打开，会搬到 {new}")
        return fallback
    try:
        if os.name == "nt":
            # Windows refuses to rename a folder with a file open inside it.
            release_all(handles)
            handles = []
        problem = move_folder(old, new)
    finally:
        release_all(handles)
    if problem:
        fallback.messages.append(f"没能把 {old} 搬到 {new}（{problem}），这次先用旧文件夹")
        return fallback
    current.migrated = True
    current.messages.append(f"小智电视改名叫小鼠电视：数据文件夹从 {old} 搬到了 {new}")
    _move_logs(old_logs, new_logs, current.messages)
    _leave_note(old, new)
    return current
