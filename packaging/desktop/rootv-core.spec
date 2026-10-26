# PyInstaller configuration for the PC client's core: onedir, one executable for every process.
#
# docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md, sections 5.1 and 5.2
# (phase P1a). Build with `python tools/build_core.py`; the result is
# build-core/dist/rootv-core/ (the executable plus _internal/).
#
# Onedir, not onefile: the core starts five or more processes from the same executable, and a
# onefile bundle unpacks itself into a temporary folder at every one of those starts. Onedir
# also triggers fewer antivirus false positives and keeps LGPL components replaceable.
#
# What goes in: tools/core_entry.py and what it imports; every module of server/ named
# explicitly (the children are reached through a name at run time, which PyInstaller cannot
# follow, and modules are found by walking the folder rather than by importing them here,
# because server.live reads a channel table when imported); the files the server reads beside
# its code: the relay's pages, the help page, the web flasher, and a firmware set when
# packaging/desktop/firmware/manifest.json exists (the release workflow fills it, P2c); the standard
# library yt-dlp's zipapp imports, which the core runs on its own Python (tools/ytdlp_run.py).
# What stays out: numpy (the default picture degrade needs none, section 5.8), tkinter, tests.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.ytdlp_run import POSIX_ONLY, STDLIB_MODULES, STDLIB_PACKAGES, WINDOWS_ONLY  # noqa: E402


def modules(package: str) -> list[str]:
    names = []
    for path in sorted((ROOT / package).rglob("*.py")):
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


hiddenimports = modules("server") + [
    "tools", "tools.certs", "tools.console", "tools.datadir", "tools.desktop_folders", "tools.ffmpeg_fetch", "tools.parentwatch",
    "tools.subcommands", "tools.packaged_entry", "tools.launch", "tools.channel_config",
    "tools.firmware_manifest", "tools.netcheck", "tools.ytdlp_run",
]


def stdlib_for_ytdlp() -> list[str]:
    """yt-dlp runs from its zipapp on the core's Python (tools/ytdlp_run.py), so the standard
    library it imports has to be here although none of our code imports it."""
    names = []
    for package in STDLIB_PACKAGES:
        names += collect_submodules(package, filter=lambda name: ".test" not in name and "idle" not in name)
    # This system's own modules (fcntl, msvcrt, …); the ones built into the interpreter need nothing.
    system = WINDOWS_ONLY if sys.platform == "win32" else POSIX_ONLY
    return names + [name for name in STDLIB_MODULES + system
                    if name not in ("sys", "nt", "posix", "curses", "readline", "winsound")]


hiddenimports += stdlib_for_ytdlp()

datas = [
    (str(ROOT / "server" / "relay" / "assets"), "server/relay/assets"),
    (str(ROOT / "packaging" / "help" / "index.html"), "packaging/help"),
    (str(ROOT / "packaging" / "flash"), "packaging/flash"),
    (str(ROOT / "LICENSE"), "."),
]
firmware = ROOT / "packaging" / "desktop" / "firmware"
if (firmware / "manifest.json").is_file():
    datas.append((str(firmware), "firmware"))

excludes = ["tkinter", "numpy", "unittest", "pydoc", "doctest", "test", "lib2to3"]

a = Analysis(
    [str(ROOT / "tools" / "core_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rootv-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # No UPX: compressed executables are a common antivirus false-positive trigger, and onedir
    # is about start-up speed, not download size.
    upx=False,
    # A console program: its output is the log the supervisor reads from each child. The tray
    # starts it with CREATE_NO_WINDOW on Windows, so no window appears.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="rootv-core",
)
