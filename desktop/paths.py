"""Where the client keeps its data and logs, and where the core program is.

Section 5.5 of the PC client design: one per-user folder with the S1 layout,
local rather than roaming on Windows, plus `logs/`. The core agent owns the
core's side of this (`tools/datadir.py`, `server/selfhost`); the tray computes
the same folder so it can find `run/control.json` before the core has said
anything. Both must agree -- `--data` on the tray is passed to the core, so the
tray's answer is the one the core uses.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from . import APP_ID

# An explicit data folder, for tests, portable use and support. Also what
# `--data` sets for the core it starts.
DATA_ENV = "XIAOZHI_TV_DATA"
# A different core program (path to an executable, or a command line split on
# whitespace). Used by CI before the real core is merged, and by developers.
CORE_ENV = "XIAOZHI_TV_CORE"

LINUX_NAME = "xiaozhi-tv"
CORE_NAME = "xiaozhi-tv-core"


def system() -> str:
    return platform.system()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir(env: dict | None = None, home: Path | None = None, os_name: str | None = None) -> Path:
    """The per-user data folder (not created here)."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    os_name = os_name or system()
    if env.get(DATA_ENV):
        return Path(env[DATA_ENV]).expanduser()
    if os_name == "Darwin":
        return home / "Library" / "Application Support" / APP_ID
    if os_name == "Windows":
        # LOCALAPPDATA, not APPDATA: a SQLite database in a roaming profile can
        # be synced half-written on domain machines (design 5.5).
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / APP_ID
    base = env.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / LINUX_NAME


def logs_dir(data: Path, env: dict | None = None, home: Path | None = None, os_name: str | None = None) -> Path:
    """Where tray.log (and the core's core.log) go.

    With an explicit data folder everything stays inside it, so a test or a
    portable copy leaves nothing behind elsewhere.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    os_name = os_name or system()
    if env.get(DATA_ENV) or os_name == "Windows":
        return Path(data) / "logs"
    if os_name == "Darwin":
        return home / "Library" / "Logs" / APP_ID
    base = env.get("XDG_STATE_HOME")
    return (Path(base) if base else home / ".local" / "state") / LINUX_NAME / "logs"


def run_dir(data: Path) -> Path:
    return Path(data) / "run"


def control_file(data: Path) -> Path:
    """Written by the core: {"port", "token", ...} for the local control API (5.13)."""
    return run_dir(data) / "control.json"


def lock_file(data: Path) -> Path:
    return run_dir(data) / "tray.lock"


def prefs_file(data: Path) -> Path:
    """The tray's own few settings, used only while the core does not report them."""
    return Path(data) / "tray.json"


def program_path() -> Path:
    """The executable autostart should start: the tray itself."""
    return Path(sys.executable).resolve()


def bundled_core(executable: Path | None = None, os_name: str | None = None) -> Path:
    """The core inside an installed client.

    macOS: `Xiaozhi TV.app/Contents/MacOS/XiaozhiTV` is the tray and the core's
    onedir sits in `Contents/Resources/core/` (code signing wants everything in
    `Contents/MacOS` to be code; a onedir also carries data files). Windows and
    Linux: `core/` beside the tray.
    """
    executable = Path(executable or sys.executable).resolve()
    os_name = os_name or system()
    if os_name == "Darwin" and executable.parent.name == "MacOS":
        return executable.parent.parent / "Resources" / "core" / CORE_NAME
    suffix = ".exe" if os_name == "Windows" else ""
    return executable.parent / "core" / (CORE_NAME + suffix)


def core_command(data: Path, env: dict | None = None) -> list[str]:
    """The command line that starts the core (design 5.13, "Starting the core").

    `--data` only when the folder was chosen explicitly (`--data` on the tray,
    XIAOZHI_TV_DATA): the core then keeps its logs inside it, as the tray does.
    Otherwise both use the per-OS folders of design 5.5 on their own.
    """
    env = os.environ if env is None else env
    data_args = ["--data", str(data)] if env.get(DATA_ENV) else []
    override = env.get(CORE_ENV, "").strip()
    if override:
        base = [override] if Path(override).exists() else override.split()
    elif is_frozen():
        base = [str(bundled_core())]
    else:
        # From a checkout: the core is the selfhost supervisor in desktop mode.
        return [sys.executable, "-u", "-m", "server.selfhost", "--desktop"] + data_args
    return base + ["selfhost", "--desktop"] + data_args


def translocated(executable: Path | None = None) -> bool:
    """macOS runs an unsigned app from a quarantined DMG under a random read-only
    path (App Translocation); data paths and autostart then point nowhere.
    The tray asks the user to drag the app to Applications first (design 5.12)."""
    path = Path(executable or sys.executable).as_posix()
    return "/AppTranslocation/" in path or path.startswith("/Volumes/")
