# PyInstaller configuration for the PC client's tray (design 5.2, phase P2c).
#
# Onedir, windowed. The core is a separate onedir (the core agent's build, or the
# stand-in core of stub-core.spec) that build.py puts beside it:
#   macOS    Xiaozhi TV.app/Contents/MacOS/XiaozhiTV + Contents/Resources/core/
#   Windows  Xiaozhi TV/XiaozhiTV.exe + core/xiaozhi-tv-core.exe
#   Linux    xiaozhi-tv/xiaozhi-tv + core/xiaozhi-tv-core
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
NAME = {"darwin": "XiaozhiTV", "win32": "XiaozhiTV"}.get(sys.platform, "xiaozhi-tv")

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
    datas=[],
    hiddenimports=["desktop", "desktop.tray", "desktop.icons", "desktop.diagnostics", "tools",
                   "tools.parentwatch", "tools.netcheck"] + backends,
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
        name="Xiaozhi TV.app",
        icon=ICON,
        bundle_identifier="io.github.ai-passport-tv.XiaozhiTV",
        version=NUMERIC,
        info_plist={
            "CFBundleName": "Xiaozhi TV",
            "CFBundleDisplayName": "小智电视",
            "CFBundleShortVersionString": NUMERIC,
            "CFBundleVersion": NUMERIC,
            "LSMinimumSystemVersion": "12.0",
            # Menu bar only: no Dock icon, no app menu (design 5.2).
            "LSUIElement": True,
            # macOS 15 asks before an app talks to LAN hosts.
            "NSLocalNetworkUsageDescription":
                "小智电视要连接家里网络上的小电视和片源。Xiaozhi TV connects to the TV devices and video sources on your home network.",
            # The core's pacing must not be throttled by App Nap.
            "NSAppSleepDisabled": True,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.entertainment",
            "NSHumanReadableCopyright": "MIT licence. See NOTICE.txt.",
        },
    )
