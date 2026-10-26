"""PyInstaller entry of the tray (RooTV / RooTV.exe / rootv): desktop/tray.py."""

import sys

from desktop.tray import main

if __name__ == "__main__":
    sys.exit(main())
