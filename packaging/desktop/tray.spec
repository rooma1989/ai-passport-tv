# PyInstaller configuration for the PC client's tray (design 5.2, phase P2c).
#
# Onedir, windowed. The core is a separate onedir (the core agent's build, or the
# stand-in core of stub-core.spec) that build.py puts beside it:
#   macOS    RooTV.app/Contents/MacOS/RooTV + Contents/Resources/core/
#   Windows  RooTV/RooTV.exe + core/rootv-core.exe
#   Linux    rootv/rootv + core/rootv-core
#
# Before the rename (0.2.5-beta) these were Xiaozhi TV.app/XiaozhiTV, XiaozhiTV.exe
# and xiaozhi-tv; desktop/legacy.py and tools/desktop_folders.py take over from them.
#
# Run through packaging/desktop/build.py, which passes the version and icons in
# the environment (XZTV_VERSION, XZTV_ICON).

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
VERSION = os.environ.get("XZTV_VERSION", "0.2.0-beta")
NUMERIC = VERSION.split("-")[0]
ICON = os.environ.get("XZTV_ICON") or None
NAME = {"darwin": "RooTV", "win32": "RooTV"}.get(sys.platform, "rootv")

# pystray picks its backend by name at run time, so the analysis cannot see it.
if sys.platform == "darwin":
    backends = ["pystray._darwin", "AppKit", "Foundation", "objc", "PyObjCTools.AppHelper",
                "PyObjCTools.MachSignals"]
elif sys.platform == "win32":
    backends = ["pystray._win32"]
else:
    backends = ["pystray._xorg", "pystray._appindicator", "pystray._gtk"]

a = Analysis(
    [str(Path(SPECPATH) / "tray_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # The tray icons (tools/make_icons.py); desktop/icons.py finds them beside itself.
    datas=[(str(ROOT / "desktop" / "assets"), "desktop/assets")],
    hiddenimports=["desktop", "desktop.tray", "desktop.icons", "desktop.diagnostics", "desktop.legacy", "tools",
                   "tools.parentwatch", "tools.netcheck", "tools.desktop_folders"] + backends,
    hookspath=[],
    runtime_hooks=[],
    # The tray never imports the server, numpy or a GUI toolkit other than the
    # tray backend; keeping them out keeps it small.
    excludes=["tkinter", "numpy", "server", "unittest", "pydoc", "doctest", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    strip=False,
    upx=False,
    # Windowed: no console window behind a tray icon. (Output goes to tray.log.)
    console=False,
    icon=ICON,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="RooTV.app",
        icon=ICON,
        # Was io.github.ai-passport-tv.XiaozhiTV; a new identity (the old login
        # item is replaced by the tray on its first run, desktop/autostart.py).
        bundle_identifier="io.github.ai-passport-tv.RooTV",
        version=NUMERIC,
        info_plist={
            # "RooTV" here, 小鼠电视 from zh-Hans.lproj/InfoPlist.strings (build.py):
            # Finder shows the localised name only when the base name matches
            # the bundle's file name.
            "CFBundleName": "RooTV",
            "CFBundleDisplayName": "RooTV",
            "LSHasLocalizedDisplayName": True,
            "CFBundleDevelopmentRegion": "en",
            "CFBundleLocalizations": ["en", "zh-Hans"],
            "CFBundleShortVersionString": NUMERIC,
            "CFBundleVersion": NUMERIC,
            "LSMinimumSystemVersion": "12.0",
            # Menu bar only: no Dock icon, no app menu (design 5.2).
            "LSUIElement": True,
            # macOS 15 asks before an app talks to LAN hosts.
            "NSLocalNetworkUsageDescription":
                "小鼠电视要连接家里网络上的小电视和片源。RooTV connects to the TV devices and video sources on your home network.",
            # The core's pacing must not be throttled by App Nap.
            "NSAppSleepDisabled": True,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.entertainment",
            "NSHumanReadableCopyright": "MIT licence. See NOTICE.txt.",
        },
    )
