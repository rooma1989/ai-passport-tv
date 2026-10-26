# PyInstaller configuration for the STAND-IN core (desktop/devcore.py), named
# rootv-core like the real one. For CI before branch desktop-core (P1) is
# merged; see stub_core_entry.py. Onedir, console (the tray starts it with
# CREATE_NO_WINDOW on Windows, as it will the real core).

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]

a = Analysis(
    [str(Path(SPECPATH) / "stub_core_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["desktop", "desktop.devcore", "desktop.instance", "tools", "tools.parentwatch"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "numpy", "server", "PIL", "pystray", "unittest", "pydoc", "doctest", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="rootv-core", debug=False, strip=False,
          upx=False, console=True, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="rootv-core")
