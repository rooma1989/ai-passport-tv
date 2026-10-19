"""PyInstaller entry of the stand-in core (desktop/devcore.py), built as `xiaozhi-tv-core`.

Only for CI builds made before the real core (branch desktop-core, phase P1) is
merged: it answers the control API with made-up state so the tray, the bundle
layout and the installers can be exercised end to end. A release must never
ship it -- build.py refuses `--stub-core` together with `--release`.
"""

import sys

from desktop.devcore import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
