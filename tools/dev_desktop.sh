#!/usr/bin/env bash
# Run the PC client's core straight from this checkout on a Mac, against the real
# data folder of the installed RooTV.app, so a change can be tried in seconds
# instead of waiting for a packaged build.
#
#   tools/dev_desktop.sh start     quit RooTV.app, back up the database, start the core from source
#   tools/dev_desktop.sh restart   stop and start again (after pulling or merging new code)
#   tools/dev_desktop.sh stop      stop the core started here
#   tools/dev_desktop.sh status    is it running, and does /healthz answer
#   tools/dev_desktop.sh log       follow the log
#
# Back to the packaged app: `tools/dev_desktop.sh stop`, then open RooTV.app.
# Environment: ROOTV_DEV_DATA (data folder), ROOTV_DEV_PYTHON (Python 3.11+),
# ROOTV_DEV_FFMPEG (ffmpeg path).
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
data="${ROOTV_DEV_DATA:-$HOME/Library/Application Support/RooTV}"
logs="$HOME/Library/Logs/RooTV"
log="$logs/core-dev.log"
pidfile="$data/run/dev-core.pid"
python="${ROOTV_DEV_PYTHON:-$(command -v python3)}"
ffmpeg="${ROOTV_DEV_FFMPEG:-$(command -v ffmpeg || echo fetch)}"
app_id="io.github.ai-passport-tv.RooTV"

running() {
    [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

healthz() {
    # Loopback must not go through a proxy app's environment (see server/proxyenv.py).
    curl --noproxy '*' -s --max-time 3 http://127.0.0.1:8080/healthz || true
}

quit_app() {
    if pgrep -f "/Applications/RooTV.app/Contents/MacOS/RooTV" >/dev/null; then
        echo "退出 RooTV.app…"
        osascript -e "quit app id \"$app_id\"" >/dev/null 2>&1 || true
        for _ in $(seq 1 20); do
            pgrep -f "rootv-core selfhost" >/dev/null || break
            sleep 1
        done
    fi
    if pgrep -f "rootv-core selfhost" >/dev/null; then
        echo "RooTV.app 的核心还在运行，先手动退出它再试。" >&2
        exit 1
    fi
}

backup() {
    local db="$data/tv.db"
    [ -f "$db" ] || return 0
    mkdir -p "$data/backups"
    local copy
    copy="$data/backups/dev-$(date +%Y%m%d-%H%M%S).db"
    "$python" -c 'import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$db" "$copy"
    # Keep the five newest dev backups.
    ls -1t "$data"/backups/dev-*.db 2>/dev/null | tail -n +6 | while read -r old; do rm -f "$old"; done
    echo "数据库已备份：$copy"
}

start() {
    if running; then
        echo "已经在运行（进程 $(cat "$pidfile")）。"
        return 0
    fi
    "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
        echo "需要 Python 3.11 或更新的版本：$python" >&2
        exit 1
    }
    quit_app
    backup
    mkdir -p "$logs" "$data/run"
    echo "从源码启动核心（$root）…"
    (
        cd "$root"
        env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
            -u NO_PROXY -u no_proxy PYTHONPATH="$root" PYTHONUNBUFFERED=1 \
            nohup "$python" -m server.selfhost --desktop --data "$data" --ffmpeg "$ffmpeg" \
            >>"$log" 2>&1 </dev/null &
        echo $! >"$pidfile"
    )
    for _ in $(seq 1 30); do
        if healthz | grep -q '"ok": true'; then
            echo "已启动：http://localhost:8080/tv/console （日志：$log）"
            return 0
        fi
        running || break
        sleep 1
    done
    echo "启动后 30 秒还没就绪，看日志：$log" >&2
    tail -20 "$log" >&2
    exit 1
}

stop() {
    if ! running; then
        rm -f "$pidfile"
        echo "没有在运行。"
        return 0
    fi
    local pid
    pid="$(cat "$pidfile")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    rm -f "$pidfile"
    echo "已停止。"
}

case "${1:-status}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status)
        if running; then echo "运行中（进程 $(cat "$pidfile")）：$(healthz)"; else echo "没有在运行。"; fi ;;
    log) tail -f "$log" ;;
    *) echo "用法：$0 start|stop|restart|status|log" >&2; exit 2 ;;
esac
