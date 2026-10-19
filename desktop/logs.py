"""tray.log: rotated at 2 MB, five files (design 5.15). No tokens, codes or full addresses."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 5

_logger = logging.getLogger("xiaozhi-tv.tray")


def setup(logs_dir: Path, echo: bool = True) -> logging.Logger:
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        handler.close()
    try:
        Path(logs_dir).mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(Path(logs_dir) / "tray.log", maxBytes=MAX_BYTES,
                                                       backupCount=BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        _logger.addHandler(handler)
    except OSError:
        pass
    # A windowed Windows build has no stdout at all; only echo when there is one.
    if echo and sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(console)
    return _logger


def log(message: str) -> None:
    _logger.info(message)
