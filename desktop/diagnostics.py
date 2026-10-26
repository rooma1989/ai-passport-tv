"""The tray's own "Export diagnostics" (design 5.15), used when the core cannot make one.

The core's `POST /local/v1/diagnostics` is the full archive (config without
secrets, ffmpeg version, port check...). When the core is not running -- which
is exactly when someone needs diagnostics -- the tray still collects what it
has: the log files, its version, the OS, and the last status it saw. Nothing
from `secrets/`, nothing uploaded; the zip lands on the Desktop for the user to
send if they want.
"""

from __future__ import annotations

import json
import platform
import sys
import time
import zipfile
from pathlib import Path

from . import __version__
from .core import redact


def desktop_folder() -> Path:
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def export(logs_dir: Path, last_status: dict | None, core_tail: list[str], out_dir: Path | None = None,
           clock=time.time) -> Path:
    out_dir = Path(out_dir or desktop_folder())
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    target = out_dir / f"rootv-diagnostics-{stamp}.zip"
    info = {
        "tray_version": __version__,
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "made_by": "tray (core unavailable)",
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("versions.json", json.dumps(info, ensure_ascii=False, indent=1))
        if last_status is not None:
            archive.writestr("status.json", json.dumps(last_status, ensure_ascii=False, indent=1))
        if core_tail:
            archive.writestr("core-output-tail.txt", "\n".join(redact(line) for line in core_tail) + "\n")
        logs = Path(logs_dir)
        if logs.is_dir():
            for path in sorted(logs.glob("*.log*")):
                if path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
                    archive.write(path, f"logs/{path.name}")
    return target
