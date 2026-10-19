#!/usr/bin/env python3
"""Build the PC client's core as a PyInstaller onedir folder.

    python3 tools/build_core.py [--clean] [--archive]

Result: build-core/dist/xiaozhi-tv-core/ (the executable xiaozhi-tv-core[.exe] and _internal/),
from packaging/desktop/xiaozhi-tv-core.spec. --archive also writes
build-core/xiaozhi-tv-core-<system>-<machine>.zip (zip keeps the executable bit; the CI
uploads it). The tray and the installers (phase P2) put this folder inside the app; see
docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md, section 5.2.

Build on the machine you target: PyInstaller does not cross-build; the CI builds each
platform on its own runner (.github/workflows/build-desktop-core.yml).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "desktop" / "xiaozhi-tv-core.spec"
OUTPUT = ROOT / "build-core"
DIST = OUTPUT / "dist"
WORK = OUTPUT / "work"
NAME = "xiaozhi-tv-core"
PLATFORM_NAMES = {"Darwin": "macos"}

sys.path.insert(0, str(ROOT))
from tools.console import use_utf8  # noqa: E402

use_utf8()


def executable() -> Path:
    return DIST / NAME / (NAME + (".exe" if platform.system() == "Windows" else ""))


def archive_name() -> str:
    system = PLATFORM_NAMES.get(platform.system(), platform.system().lower())
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    return f"{NAME}-{system}-{machine}.zip"


def make_archive() -> Path:
    target = OUTPUT / archive_name()
    folder = DIST / NAME
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            name = str(Path(NAME) / path.relative_to(folder)).replace(os.sep, "/")
            if path.is_symlink():
                # macOS frameworks inside _internal use symlinks; keep them as links.
                info = zipfile.ZipInfo(name)
                info.external_attr = (0o120777 << 16)
                archive.writestr(info, os.readlink(path))
            elif path.is_file():
                archive.write(path, name)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the PC client's core (onedir).")
    parser.add_argument("--clean", action="store_true", help="remove previous build output first")
    parser.add_argument("--archive", action="store_true", help="also zip the folder for upload")
    args = parser.parse_args(argv)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("需要先安装 PyInstaller（只是构建需要）：python3 -m pip install pyinstaller", file=sys.stderr)
        return 1
    if sys.version_info < (3, 11):
        print("核心需要 Python 3.11 或更新的版本来构建（tomllib）", file=sys.stderr)
        return 1
    if args.clean and OUTPUT.is_dir():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(exist_ok=True)
    print(f"正在为 {platform.system()}/{platform.machine()} 构建核心…", flush=True)
    result = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(DIST),
                             "--workpath", str(WORK), str(SPEC)], cwd=ROOT)
    if result.returncode != 0:
        print("构建失败。", file=sys.stderr)
        return result.returncode
    if not executable().is_file():
        print(f"构建报告成功，但没有找到 {executable()}", file=sys.stderr)
        return 1
    print(f"已构建：{executable()}")
    if args.archive:
        print(f"已打包：{make_archive()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
