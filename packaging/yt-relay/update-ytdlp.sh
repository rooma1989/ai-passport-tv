#!/bin/sh
# Weekly yt-dlp update for the YouTube node (run by yt-dlp-update.timer as root).
# YouTube changes often enough that an old yt-dlp stops working within weeks.
# The new release replaces the old one only after it resolved a test video with
# the node's own deno and cookies, as the node's user; otherwise the old one stays.
set -eu

DIR=/opt/yt-relay
NEW="$DIR/yt-dlp.new"
URL=https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux
TEST_VIDEO=https://www.youtube.com/watch?v=jNQXAC9IVRw
COOKIES=/var/lib/yt-relay/cookies.txt

old=$("$DIR/yt-dlp" --version 2>/dev/null || echo none)
curl -fsSL --retry 3 -o "$NEW" "$URL"
chmod 755 "$NEW"
new=$("$NEW" --version)
if [ "$new" = "$old" ]; then
    rm -f "$NEW"
    echo "yt-dlp $old is current"
    exit 0
fi
set -- --js-runtimes "deno:$DIR/deno" --no-warnings --ignore-config -J --no-playlist
[ -f "$COOKIES" ] && set -- "$@" --cookies "$COOKIES"
if runuser -u ytrelay -- "$NEW" "$@" -- "$TEST_VIDEO" > /dev/null 2> /tmp/yt-dlp-update.err; then
    mv -f "$NEW" "$DIR/yt-dlp"
    echo "yt-dlp updated $old -> $new"
else
    echo "yt-dlp $new failed the test video, keeping $old: $(tail -1 /tmp/yt-dlp-update.err)"
    rm -f "$NEW"
    exit 1
fi
