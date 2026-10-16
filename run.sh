#!/usr/bin/env bash
# Start the media server and the channel page.
#
# Thin on purpose. Every check worth making -- Python version, ffmpeg, the
# address to type on the device -- is in tools/launch.py, which is one program
# that runs everywhere. Writing them again here in shell would mean a second
# copy to keep in step, and a third in run.bat, and the three would drift.
#
# Double-clicking this file works on macOS when it is marked executable. From a
# terminal: ./run.sh
#
# ./run.sh --selfhost [--data <folder>] starts the self-hosted server instead
# (accounts, phone remote, admin backend): docs/selfhost/README.md.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

# python3 first, then python: on some systems only one of the two exists, and on
# Windows-with-Git-Bash `python` is the one that is present.
candidates="python3 python"
# The self-hosted server needs Python 3.11 (tomllib). macOS's own python3 is
# 3.9, and Homebrew installs newer ones under their versioned names, so try
# those first; tools/launch.py still checks and explains if none is new enough.
for arg in "$@"; do
    if [ "$arg" = "--selfhost" ]; then
        candidates="python3.13 python3.12 python3.11 python3 python"
    fi
done
for candidate in $candidates; do
    if command -v "$candidate" >/dev/null 2>&1; then
        exec "$candidate" tools/launch.py "$@"
    fi
done

echo "找不到 Python。" >&2
echo "本项目需要 Python 3.9 或更新版本，从 https://www.python.org/downloads/ 下载安装。" >&2
exit 1
