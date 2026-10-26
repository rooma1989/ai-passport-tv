"""Where the client keeps its data and logs, and where the core program is.

Section 5.5 of the PC client design: one per-user folder with the S1 layout,
local rather than roaming on Windows, plus `logs/`. The names, and the one-time
move from the pre-rename XiaozhiTV folder, are in `tools/desktop_folders.py`,
which the core (`server/selfhost/desktop_main.py`) uses as well; the tray computes
the same folder so it can find `run/control.json` before the core has said
anything. Both must agree -- `--data` on the tray is passed to the core, so the
tray's answer is the one the core uses.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from tools import desktop_folders

# An explicit data folder, for tests, portable use and support. Also what
# `--data` sets for the core it starts. The pre-rename name is still honoured.
DATA_ENV = desktop_folders.DATA_ENV
LEGACY_DATA_ENV = desktop_folders.LEGACY_DATA_ENV
# A different core program (path to an executable, or a command line split on
# whitespace). Used by CI before the real core is merged, and by developers.
CORE_ENV = "ROOTV_CORE"
LEGACY_CORE_ENV = "XIAOZHI_TV_CORE"

LINUX_NAME = desktop_folders.APP_DIR_LINUX
CORE_NAME = "rootv-core"


def system() -> str:
    return platform.system()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir(env: dict | None = None, home: Path | None = None, os_name: str | None = None) -> Path:
    """The per-user data folder under the current name (not created or moved here;
    `resolve` is what a run uses)."""
    env = os.environ if env is None else env
    explicit = desktop_folders.explicit_data_dir(env)
    if explicit is not None:
        return explicit
    return desktop_folders.default_data_dir(os_name or system(), env, home)


def logs_dir(data: Path, env: dict | None = None, home: Path | None = None, os_name: str | None = None) -> Path:
    """Where tray.log (and the core's core.log) go.

    With an explicit data folder everything stays inside it, so a test or a
    portable copy leaves nothing behind elsewhere.
    """
    env = os.environ if env is None else env
    os_name = os_name or system()
    if desktop_folders.explicit_data_dir(env) is not None or os_name == "Windows":
        return Path(data) / "logs"
    return desktop_folders.default_logs_dir(os_name, env, home)


def resolve(stop_old=None, env: dict | None = None, home: Path | None = None,
            os_name: str | None = None) -> "desktop_folders.Folders":
    """The data and logs folders of this run: the RooTV ones, after moving the
    pre-rename XiaozhiTV ones there when nothing uses them any more
    (tools/desktop_folders.py). The core answers the same question the same way."""
    return desktop_folders.resolve(os_name or system(), env, home, stop_old=stop_old)


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

    macOS: `RooTV.app/Contents/MacOS/RooTV` is the tray and the core's
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
    ROOTV_DATA, or the old XIAOZHI_TV_DATA): the core then keeps its logs inside it, as the tray does.
    Otherwise both use the per-OS folders of design 5.5 on their own.
    """
    env = os.environ if env is None else env
    data_args = ["--data", str(data)] if desktop_folders.explicit_data_dir(env) is not None else []
    override = (env.get(CORE_ENV) or env.get(LEGACY_CORE_ENV) or "").strip()
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
