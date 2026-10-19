#!/usr/bin/env python3
"""Build the PC client for this machine's OS (design 5.2, phase P2c).

    python3 packaging/desktop/build.py --stub-core            CI before the real core is merged
    python3 packaging/desktop/build.py --core-dir <onedir>    with the core's PyInstaller onedir

Output in build-desktop/ (untracked):

    macOS    XiaozhiTV-<ver>-macos-<arm64|x86_64>.dmg   (Xiaozhi TV.app, ad-hoc signed, + Applications link)
    Windows  XiaozhiTV-<ver>-windows-x64.zip            (portable) and stage/Xiaozhi TV/ for Inno Setup
    Linux    xiaozhi-tv-<ver>-linux-x86_64.tar.gz       (onedir, .desktop, systemd --user unit, --no-tray)
    SHA256SUMS

The installer for Windows is compiled by the workflow (Inno Setup is a Windows
program): `iscc /DSourceDir=build-desktop\\stage\\Xiaozhi TV /DAppVersion=<ver>
packaging\\desktop\\xiaozhi-tv.iss`.

Builds for the machine it runs on only (PyInstaller does not cross-build).
Needs: pip install pyinstaller pystray pillow.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "build-desktop"
WORK = ROOT / "build-desktop-work"
APP = "Xiaozhi TV.app"
WINDOWS_DIR = "Xiaozhi TV"
ICON_SOURCE = ROOT / "packaging" / "site" / "assets" / "icon-512.png"

sys.path.insert(0, str(ROOT))
from desktop import __version__  # noqa: E402


def say(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    print(f"::error::build-desktop: {message}", flush=True)
    raise SystemExit(1)


def run(argv: list, **kwargs) -> None:
    """Run a build step, echo its output, and on failure repeat the last lines as
    an annotation: CI logs need a login to read, annotations do not."""
    say("$ " + " ".join(str(a) for a in argv))
    tail: collections.deque[str] = collections.deque(maxlen=15)
    process = subprocess.Popen([str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace", **kwargs)
    for line in process.stdout:
        print(line, end="", flush=True)
        if line.strip():
            tail.append(line.rstrip())
    code = process.wait()
    if code != 0:
        fail(f"{Path(str(argv[0])).name} exited with {code}%0A" + "%0A".join(tail))


def machine() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(m, m)


def icons() -> dict:
    """App icons made from the site's 512 px icon: .ico (Windows), .icns (macOS), .png (Linux)."""
    from desktop.icons import save_app_icons
    out = WORK / "icons"
    out.mkdir(parents=True, exist_ok=True)
    ico = out / "xiaozhi-tv.ico"
    iconset = out / "xiaozhi-tv.iconset"
    save_app_icons(ICON_SOURCE, ico, iconset if sys.platform == "darwin" else None)
    result = {"ico": ico, "png": ICON_SOURCE}
    if sys.platform == "darwin":
        icns = out / "xiaozhi-tv.icns"
        run(["iconutil", "-c", "icns", iconset, "-o", icns])
        result["icns"] = icns
    return result


def pyinstaller(spec: Path, dist: Path, env: dict) -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
         "--distpath", dist, "--workpath", WORK / ("pyi-" + spec.stem), spec], env=env)


def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                     b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce")


def sign_mac(app: Path) -> None:
    """Ad-hoc signatures (Apple silicon refuses unsigned code). Inside out: every
    Mach-O of the nested core first, then the bundle itself."""
    core = app / "Contents" / "Resources" / "core"
    files = sorted(p for p in core.rglob("*") if p.is_file() and not p.is_symlink() and is_macho(p))
    # Libraries before the executable that loads them.
    files.sort(key=lambda p: p.name == "xiaozhi-tv-core")
    for path in files:
        run(["codesign", "--force", "--sign", "-", "--timestamp=none", path])
    run(["codesign", "--force", "--sign", "-", "--timestamp=none", app])
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app])


def copy_tree(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True)


def make_dmg(app: Path, dmg: Path) -> None:
    stage = WORK / "dmg"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    copy_tree(app, stage / APP)
    (stage / "Applications").symlink_to("/Applications")
    shutil.copy2(HERE / "NOTICE.txt", stage / "NOTICE.txt")
    if dmg.exists():
        dmg.unlink()
    run(["hdiutil", "create", "-volname", "Xiaozhi TV", "-srcfolder", stage, "-fs", "HFS+", "-format", "UDZO",
         "-ov", dmg])


def make_zip(folder: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, Path(folder.name) / path.relative_to(folder))


def make_tar(folder: Path, target: Path) -> None:
    with tarfile.open(target, "w:gz") as archive:
        archive.add(folder, arcname=folder.name)


def sha256sums(files: list[Path]) -> None:
    lines = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    (OUT / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")
    say("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--core-dir", type=Path, help="the core's PyInstaller onedir (folder xiaozhi-tv-core/)")
    source.add_argument("--stub-core", action="store_true", help="build and bundle the stand-in core (CI only)")
    parser.add_argument("--release", action="store_true", help="a build meant for users: refuses the stand-in core")
    parser.add_argument("--version", default=__version__)
    args = parser.parse_args(argv)
    if args.release and args.stub_core:
        fail("--stub-core cannot be released: it is a fake core with made-up state")

    for folder in (OUT, WORK):
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
    env = dict(os.environ, XZTV_VERSION=args.version, PYTHONHASHSEED="0")
    icon = icons()
    env["XZTV_ICON"] = str(icon.get("icns") or icon["ico"])

    dist = WORK / "dist"
    pyinstaller(HERE / "tray.spec", dist, env)
    if args.stub_core:
        pyinstaller(HERE / "stub-core.spec", dist, env)
        core_dir = dist / "xiaozhi-tv-core"
    else:
        core_dir = args.core_dir.resolve()
    exe = "xiaozhi-tv-core.exe" if sys.platform == "win32" else "xiaozhi-tv-core"
    if not (core_dir / exe).is_file():
        fail(f"no {exe} in {core_dir}")

    made: list[Path] = []
    if sys.platform == "darwin":
        app = dist / APP
        resources = app / "Contents" / "Resources"
        copy_tree(core_dir, resources / "core")
        shutil.copy2(HERE / "NOTICE.txt", resources / "NOTICE.txt")
        sign_mac(app)
        copy_tree(app, OUT / APP)
        dmg = OUT / f"XiaozhiTV-{args.version}-macos-{machine()}.dmg"
        make_dmg(app, dmg)
        made.append(dmg)
    elif sys.platform == "win32":
        stage = OUT / "stage" / WINDOWS_DIR
        copy_tree(dist / "XiaozhiTV", stage)
        copy_tree(core_dir, stage / "core")
        shutil.copy2(HERE / "NOTICE.txt", stage / "NOTICE.txt")
        shutil.copy2(icon["ico"], stage / "xiaozhi-tv.ico")
        portable = OUT / f"XiaozhiTV-{args.version}-windows-x64.zip"
        make_zip(stage, portable)
        made.append(portable)
    else:
        folder = OUT / f"xiaozhi-tv-{args.version}-linux-{machine()}"
        copy_tree(dist / "xiaozhi-tv", folder)
        copy_tree(core_dir, folder / "core")
        for name in ("NOTICE.txt", "xiaozhi-tv.desktop", "xiaozhi-tv.service", "README-linux.txt"):
            shutil.copy2(HERE / name, folder / name)
        shutil.copy2(icon["png"], folder / "xiaozhi-tv.png")
        launcher = folder / "xiaozhi-tv"
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        tarball = OUT / f"{folder.name}.tar.gz"
        make_tar(folder, tarball)
        made.append(tarball)
    sha256sums(made)
    say(f"built: {', '.join(p.name for p in made)}" + ("  (STAND-IN CORE: not for users)" if args.stub_core else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
