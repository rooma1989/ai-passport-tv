#!/usr/bin/env python3
"""Smoke-test a built (or installed) PC client. Prints `::error::` lines for CI.

    python3 packaging/desktop/smoke.py                       the build in build-desktop/ for this OS
    python3 packaging/desktop/smoke.py --program <dir>       an installed copy (Windows: Program Files)
    ... --tray-check                                         also show the real tray icon for a few seconds
    ... --tray-check-only                                    only that (CI runs it as its own step)

Every run uses a fresh data folder whose path contains Chinese characters and a
space (design 5.5: Chinese user names are common on Windows). What it checks:

* the bundle layout: the tray, the core beside it, NOTICE.txt; on macOS the
  Info.plist keys (LSUIElement, NSLocalNetworkUsageDescription, NSAppSleepDisabled),
  the ad-hoc signature and the DMG's contents;
* `--version`;
* `--smoke-test`: the tray starts the bundled core, reads the control API's
  status, gets a console link, a second launch hands off to the first, and the
  core is gone afterwards;
* with --tray-check: the real tray icon (pystray inside the frozen app).
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "build-desktop"
sys.path.insert(0, str(ROOT))
from desktop import __version__  # noqa: E402

errors: list[str] = []


def error(message: str) -> None:
    errors.append(message)
    print(f"::error::desktop smoke: {message}", flush=True)


def say(message: str) -> None:
    print(message, flush=True)


def run(argv: list, timeout: float = 180, **kwargs) -> subprocess.CompletedProcess:
    say("$ " + " ".join(str(a) for a in argv))
    flags = 0x08000000 if os.name == "nt" else 0
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=flags, **kwargs)


def print_log(data: Path) -> None:
    """Show tray.log: a windowed Windows build writes nothing to stdout."""
    log = data / "logs" / "tray.log"
    if log.is_file():
        say(f"--- {log.name} ---")
        say(log.read_text(encoding="utf-8", errors="replace").rstrip())
        say("---")


TRAY_CHECK_ONLY = False


def check_program(tray: Path, core: Path, notice: Path, tray_check: bool) -> None:
    for path, what in ((tray, "tray"), (core, "core"), (notice, "NOTICE.txt")):
        if not path.is_file():
            error(f"{what} missing: {path}")
    if errors:
        return
    if os.name != "nt" and not os.access(core, os.X_OK):
        error(f"core not executable: {core}")
    if os.name != "nt":
        # A windowed Windows build has no stdout to print a version to.
        result = run([tray, "--version"], timeout=60)
        if __version__ not in result.stdout:
            error(f"--version printed {result.stdout.strip()!r} (exit {result.returncode})")
    tmp = Path(tempfile.mkdtemp(prefix="xztv-smoke-"))
    try:
        if TRAY_CHECK_ONLY:
            return tray_only(tray, tmp)
        data = tmp / "小智 电视数据"
        started = time.monotonic()
        result = run([tray, "--data", data, "--smoke-test", "--timeout", "120"], timeout=240)
        say(result.stdout.rstrip())
        if result.returncode != 0 or (os.name != "nt" and "smoke test: PASS" not in result.stdout):
            print_log(data)
            error(f"--smoke-test failed (exit {result.returncode}, {time.monotonic() - started:.0f}s)")
        elif os.name == "nt":
            print_log(data)
        if (data / "run" / "control.json").exists():
            error("control.json left behind: the core did not stop cleanly")
        if tray_check:
            tray_only(tray, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def tray_only(tray: Path, tmp: Path) -> None:
    data = tmp / "托盘 检查"
    result = run([tray, "--data", data, "--tray-check", "4", "--timeout", "120"], timeout=240)
    say(result.stdout.rstrip())
    print_log(data)
    if result.returncode != 0:
        error(f"--tray-check failed (exit {result.returncode})")


def check_mac(tray_check: bool) -> None:
    app = OUT / "Xiaozhi TV.app"
    contents = app / "Contents"
    try:
        info = plistlib.loads((contents / "Info.plist").read_bytes())
    except (OSError, ValueError) as exc:
        error(f"Info.plist unreadable: {exc}")
        return
    for key, wanted in (("LSUIElement", True), ("NSAppSleepDisabled", True), ("CFBundleExecutable", "XiaozhiTV")):
        if info.get(key) != wanted:
            error(f"Info.plist {key} = {info.get(key)!r}, wanted {wanted!r}")
    if not info.get("NSLocalNetworkUsageDescription"):
        error("Info.plist has no NSLocalNetworkUsageDescription")
    result = run(["codesign", "--verify", "--deep", "--strict", app])
    if result.returncode != 0:
        error(f"codesign --verify: {result.stderr.strip()[:300]}")
    check_program(contents / "MacOS" / "XiaozhiTV", contents / "Resources" / "core" / "xiaozhi-tv-core",
                  contents / "Resources" / "NOTICE.txt", tray_check)
    dmgs = sorted(OUT.glob("XiaozhiTV-*-macos-*.dmg"))
    if len(dmgs) != 1:
        error(f"expected one DMG, found {[d.name for d in dmgs]}")
        return
    mount = Path(tempfile.mkdtemp(prefix="xztv-dmg-"))
    result = run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, dmgs[0]])
    if result.returncode != 0:
        error(f"hdiutil attach: {result.stderr.strip()[:300]}")
        return
    try:
        if not (mount / "Xiaozhi TV.app" / "Contents" / "Resources" / "core" / "xiaozhi-tv-core").is_file():
            error("DMG has no Xiaozhi TV.app with its core")
        link = mount / "Applications"
        if not link.is_symlink() or os.readlink(link) != "/Applications":
            error("DMG has no Applications link")
    finally:
        run(["hdiutil", "detach", mount])


def check_linux(tray_check: bool) -> None:
    tarballs = sorted(OUT.glob("xiaozhi-tv-*-linux-*.tar.gz"))
    if len(tarballs) != 1:
        error(f"expected one tarball, found {[t.name for t in tarballs]}")
        return
    tmp = Path(tempfile.mkdtemp(prefix="xztv-tar-"))
    try:
        with tarfile.open(tarballs[0]) as archive:
            archive.extractall(tmp, filter="data")
        folder = next(tmp.iterdir())
        for name in ("xiaozhi-tv.desktop", "xiaozhi-tv.service", "README-linux.txt", "xiaozhi-tv.png"):
            if not (folder / name).is_file():
                error(f"tarball has no {name}")
        check_program(folder / "xiaozhi-tv", folder / "core" / "xiaozhi-tv-core", folder / "NOTICE.txt",
                      tray_check)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_windows(program: Path, tray_check: bool) -> None:
    check_program(program / "XiaozhiTV.exe", program / "core" / "xiaozhi-tv-core.exe", program / "NOTICE.txt",
                  tray_check)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--program", type=Path, help="an installed or unpacked program folder (Windows)")
    parser.add_argument("--tray-check", action="store_true")
    parser.add_argument("--tray-check-only", action="store_true")
    args = parser.parse_args()
    global TRAY_CHECK_ONLY
    TRAY_CHECK_ONLY = args.tray_check_only
    if sys.platform == "darwin":
        check_mac(args.tray_check)
    elif sys.platform == "win32":
        check_windows(args.program or OUT / "stage" / "Xiaozhi TV", args.tray_check)
    else:
        check_linux(args.tray_check)
    if errors:
        say(f"desktop smoke: FAIL ({len(errors)} problem(s))")
        return 1
    say("desktop smoke: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
