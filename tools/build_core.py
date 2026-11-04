#!/usr/bin/env python3
"""Build the PC client's core as a PyInstaller onedir folder.

    python3 tools/build_core.py [--clean] [--archive]

Result: build-core/dist/rootv-core/ (the executable rootv-core[.exe] and _internal/),
from packaging/desktop/rootv-core.spec. --archive also writes
build-core/rootv-core-<system>-<machine>.zip (zip keeps the executable bit; the CI
uploads it). The tray and the installers (phase P2) put this folder inside the app; see
docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md, section 5.2.

Build on the machine you target: PyInstaller does not cross-build; the CI builds each
platform on its own runner (.github/workflows/build-desktop-core.yml).
"""

from __future__ import annotations

import argparse
import ast
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "desktop" / "rootv-core.spec"
OUTPUT = ROOT / "build-core"
DIST = OUTPUT / "dist"
WORK = OUTPUT / "work"
NAME = "rootv-core"
PLATFORM_NAMES = {"Darwin": "macos"}

sys.path.insert(0, str(ROOT))
from tools.console import use_utf8  # noqa: E402

use_utf8()


EDITION_MODULE = ROOT / "server" / "_edition_build.py"


def check_edition(edition: str, channels_url: str) -> None:
    if edition not in ("full", "lite"):
        raise SystemExit(f"--edition 只能是 full 或 lite，收到 {edition!r}")
    if channels_url:
        bad = any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in channels_url)
        try:
            parts = urlsplit(channels_url)
            ok = parts.scheme == "https" and bool(parts.hostname)
        except ValueError:
            ok = False
        if bad or not ok:
            raise SystemExit("--channels-url 要么留空，要么是带主机名、没有空白和控制字符的 https:// 地址")


def stamp_edition(edition: str, channels_url: str, target: Path = EDITION_MODULE) -> None:
    """把版本写死进这次打的包（server/edition.py 读它）；打完 clear_edition() 删掉，从不进仓库。"""
    check_edition(edition, channels_url)
    Path(target).write_text(f"EDITION = {edition!r}\nCHANNELS_URL = {channels_url!r}\n", encoding="utf-8")


def clear_edition(target: Path = EDITION_MODULE) -> None:
    try:
        Path(target).unlink()
    except FileNotFoundError:
        pass


def stamped_edition(target: Path = EDITION_MODULE) -> str:
    """这次构建写死的版本（rootv-core.spec 用它决定带不带精简版的频道包）；没有生成文件就是 full。

    只解析不执行：读 EDITION = '...' 这一行的字面值。"""
    try:
        tree = ast.parse(Path(target).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "full"
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "EDITION" for t in node.targets):
            value = ast.literal_eval(node.value)
            if value in ("full", "lite"):
                return value
    raise SystemExit(f"{target} 里没有 full 或 lite 的 EDITION")


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
    parser.add_argument("--edition", default="full", help="full (RooTV) or lite (the minimal edition)")
    parser.add_argument("--channels-url", default="", help="lite: where the signed channel pack is fetched (https)")
    args = parser.parse_args(argv)
    check_edition(args.edition, args.channels_url)
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
    stamp_edition(args.edition, args.channels_url)
    try:
        result = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(DIST),
                                 "--workpath", str(WORK), str(SPEC)], cwd=ROOT)
    finally:
        clear_edition()
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
