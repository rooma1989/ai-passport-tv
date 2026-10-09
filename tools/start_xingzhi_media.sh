#!/usr/bin/env bash
# Start a media server for the Xingzhi Cube 1.54 board beside the Passport's.
#
# Same tv-server program, run as its media part only: 240x180 picture (what the
# Xingzhi firmware is built for), port 8098, no channel page of its own -- the
# Passport instance's page on 8097 edits the channel list both use.
#
#   tools/start_xingzhi_media.sh [path/to/tv-server] [channel-id]
#
# Untested on Windows; macOS only.
set -euo pipefail

bin="${1:-${TV_SERVER_BIN:-$HOME/Downloads/tv-server-macos-arm64}}"
channel="${2:-ch000}"
port="${TV_XINGZHI_PORT:-8098}"

if [[ ! -x "${bin}" ]]; then
    echo "tv-server not found at ${bin}; pass its path as the first argument" >&2
    exit 1
fi

# tv-server downloads ffmpeg-<version> next to itself on first run.
ffmpeg="$(ls "$(dirname "${bin}")"/ffmpeg-* 2>/dev/null | head -n 1 || true)"
if [[ -z "${ffmpeg}" ]]; then
    ffmpeg="$(command -v ffmpeg || true)"
fi
if [[ -z "${ffmpeg}" ]]; then
    echo "no ffmpeg beside ${bin} or on PATH; run tv-server once so it fetches one" >&2
    exit 1
fi

host="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
echo "Xingzhi media server: 240x180, port ${port}, ffmpeg ${ffmpeg}"
echo "Server address to enter on the device: ${host}.local:${port}"
exec env TV_GEOMETRY=240x180 "${bin}" __media live \
    --channel "${channel}" --port "${port}" --ffmpeg "${ffmpeg}"
