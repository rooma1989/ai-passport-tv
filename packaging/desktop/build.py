#!/usr/bin/env python3
"""Build the PC client for this machine's OS (design 5.2, phase P2c).

    python3 packaging/desktop/build.py --stub-core            CI before the real core is merged
    python3 packaging/desktop/build.py --core-dir <onedir>    with the core's PyInstaller onedir

Output in build-desktop/ (untracked):

    macOS    RooTV-<ver>-macos-<arm64|x86_64>.dmg   (RooTV.app, ad-hoc signed, + Applications link)
    Windows  RooTV-<ver>-windows-x64.zip            (portable) and stage/RooTV/ for Inno Setup
    Linux    rootv-<ver>-linux-x86_64.tar.gz        (onedir, .desktop, systemd --user unit, --no-tray)
    SHA256SUMS

The installer for Windows is compiled by the workflow (Inno Setup is a Windows
program): `iscc /DSourceDir=build-desktop\\stage\\RooTV /DAppVersion=<ver>
packaging\\desktop\\rootv.iss`.

The icons are tracked files in packaging/desktop/icons/, made from the brand
SVGs by tools/make_icons.py (rootv.icns, rootv.ico, rootv.png).

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
APP = "RooTV.app"
WINDOWS_DIR = "RooTV"
EXE_NAME = "RooTV"           # macOS and Windows; the Linux program is LINUX_NAME
LINUX_NAME = "rootv"
CORE = "rootv-core"
ICONS = HERE / "icons"
# Where the system shows a localised name (Finder, Launchpad, Spotlight with a
# Chinese system language), the app is called this; elsewhere RooTV.
NAME_ZH = "小鼠电视"

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
    """The app icons (tools/make_icons.py): .icns (macOS), .ico (Windows), .png (Linux)."""
    result = {"icns": ICONS / "rootv.icns", "ico": ICONS / "rootv.ico", "png": ICONS / "rootv.png"}
    for path in result.values():
        if not path.is_file():
            fail(f"missing {path.relative_to(ROOT)}: run python3 tools/make_icons.py")
    return result


def localise_mac(app: Path) -> None:
    """The Chinese display name: zh-Hans.lproj/InfoPlist.strings (UTF-16, as
    Apple's tools write it) plus LSHasLocalizedDisplayName in Info.plist."""
    for language in ("zh-Hans", "zh_CN"):
        folder = app / "Contents" / "Resources" / f"{language}.lproj"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "InfoPlist.strings").write_text(
            f'CFBundleDisplayName = "{NAME_ZH}";\nCFBundleName = "{NAME_ZH}";\n', encoding="utf-16")
    english = app / "Contents" / "Resources" / "en.lproj"
    english.mkdir(parents=True, exist_ok=True)
    (english / "InfoPlist.strings").write_text('CFBundleDisplayName = "RooTV";\nCFBundleName = "RooTV";\n',
                                                encoding="utf-16")


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
    files.sort(key=lambda p: p.name == CORE)
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
    run(["hdiutil", "create", "-volname", "RooTV", "-srcfolder", stage, "-fs", "HFS+", "-format", "UDZO",
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
    source.add_argument("--core-dir", type=Path, help="the core's PyInstaller onedir (folder rootv-core/)")
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
    env["XZTV_ICON"] = str(icon["icns"] if sys.platform == "darwin" else icon["ico"])

    dist = WORK / "dist"
    pyinstaller(HERE / "tray.spec", dist, env)
    if args.stub_core:
        pyinstaller(HERE / "stub-core.spec", dist, env)
        core_dir = dist / CORE
    else:
        core_dir = args.core_dir.resolve()
    exe = CORE + ".exe" if sys.platform == "win32" else CORE
    if not (core_dir / exe).is_file():
        fail(f"no {exe} in {core_dir}")

    made: list[Path] = []
    if sys.platform == "darwin":
        app = dist / APP
        resources = app / "Contents" / "Resources"
        copy_tree(core_dir, resources / "core")
        shutil.copy2(HERE / "NOTICE.txt", resources / "NOTICE.txt")
        localise_mac(app)
        sign_mac(app)
        copy_tree(app, OUT / APP)
        dmg = OUT / f"RooTV-{args.version}-macos-{machine()}.dmg"
        make_dmg(app, dmg)
        made.append(dmg)
    elif sys.platform == "win32":
        stage = OUT / "stage" / WINDOWS_DIR
        copy_tree(dist / EXE_NAME, stage)
        copy_tree(core_dir, stage / "core")
        shutil.copy2(HERE / "NOTICE.txt", stage / "NOTICE.txt")
        shutil.copy2(icon["ico"], stage / "rootv.ico")
        portable = OUT / f"RooTV-{args.version}-windows-x64.zip"
        make_zip(stage, portable)
        made.append(portable)
    else:
        folder = OUT / f"{LINUX_NAME}-{args.version}-linux-{machine()}"
        copy_tree(dist / LINUX_NAME, folder)
        copy_tree(core_dir, folder / "core")
        for name in ("NOTICE.txt", "rootv.desktop", "rootv.service", "README-linux.txt"):
            shutil.copy2(HERE / name, folder / name)
        shutil.copy2(icon["png"], folder / "rootv.png")
        launcher = folder / LINUX_NAME
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        tarball = OUT / f"{folder.name}.tar.gz"
        make_tar(folder, tarball)
        made.append(tarball)
    sha256sums(made)
    say(f"built: {', '.join(p.name for p in made)}" + ("  (STAND-IN CORE: not for users)" if args.stub_core else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
