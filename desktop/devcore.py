"""A stand-in core: the local control API with made-up state, nothing else.

For the tray's tests and for CI builds made before the real core
(`server/selfhost --desktop`, phase P1 on branch desktop-core) is merged. It
follows "The contract as implemented (P1c)" in design 5.13:

    <data>/run/control.json  {"api", "port", "token", "pid", "version", "started", "data_dir", "logs_dir"}
    Authorization: Bearer <token>; Host must be 127.0.0.1:<port> or localhost:<port>; no Origin
    GET  /local/v1/status
    POST /local/v1/console-link | phone-qr | power | autostart | ffmpeg/fetch | diagnostics | restart | stop
    errors: {"ok": false, "error": <code>, "message": <Chinese>}

plus, only with --network, the proposed `network` status block and
`POST /local/v1/network/trust` (trusted networks, 5.11; not in P1c).

Usage: python3 -m desktop.devcore [selfhost] [--desktop] --data <folder>
       [--setup] [--devices N] [--ffmpeg ready|fetching|missing|failed] [--network]

It binds 127.0.0.1 only, never opens LAN ports and writes only inside <data>.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.2.0-devcore"
LOCK_HELD_EXIT = 3


class DevCore:
    def __init__(self, data: Path, setup: bool = False, devices: int = 0, ffmpeg: str = "ready",
                 lan_port: int = 8080, download_s: float = 0.0, network: bool = False):
        self.data = Path(data)
        self.token = secrets.token_urlsafe(32)
        self.setup_code = "DEVC0DE123" if setup else ""
        self.lan_port = lan_port
        self.devices = [{"id": f"dev{i + 1}", "name": f"小电视 {i + 1}", "model": "xingzhi-cube-154",
                         "online": True, "channel": f"频道 {i + 1}", "since": time.time()} for i in range(devices)]
        self.ffmpeg = ffmpeg
        self.download_s = download_s
        self.started = time.time()
        self.settings = {"keep_awake": "watching", "autostart": True}
        self.network = {"id": "0123456789abcdef", "trusted": None, "profile": "", "lan_open": True} \
            if network else None
        self.calls: list[tuple[str, str, dict]] = []
        self.stopped = threading.Event()
        self._closed = False
        self._close_lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None

    @property
    def control_file(self) -> Path:
        return self.data / "run" / "control.json"

    @property
    def port(self) -> int:
        return self.server.server_address[1] if self.server else 0

    def status(self) -> dict:
        ffmpeg = {"state": self.ffmpeg, "path": "", "source": "", "done": 0, "total": 0, "error": ""}
        problems = []
        if self.download_s > 0:
            done = (time.time() - self.started) / self.download_s
            if done < 1:
                ffmpeg.update(state="fetching", done=int(done * 30_000_000), total=30_000_000)
            else:
                ffmpeg["state"] = "ready"
        if ffmpeg["state"] == "missing":
            problems.append({"code": "ffmpeg_missing", "message": "缺少视频组件 ffmpeg"})
        elif ffmpeg["state"] == "failed":
            ffmpeg["error"] = "下载超时"
            problems.append({"code": "ffmpeg_failed", "message": "视频组件下载失败：下载超时"})
        status = {
            "ok": True, "api": 1, "version": VERSION, "pid": os.getpid(), "started": self.started,
            "state": "fetching_ffmpeg" if ffmpeg["state"] == "fetching" else "running",
            "problems": problems, "data_dir": str(self.data), "logs_dir": str(self.data / "logs"),
            "bind": "127.0.0.1",
            "ports": {"lan": self.lan_port, "lan_wanted": self.lan_port, "xingzhi": 8098, "passport": 8097,
                      "control": self.port},
            "lan_url": f"http://192.0.2.10:{self.lan_port}/tv/", "local_url": f"http://localhost:{self.lan_port}/tv/",
            "setup_pending": bool(self.setup_code),
            "children": {name: {"running": True, "restarts": 0} for name in ("relay", "xingzhi", "passport", "front")},
            "devices": self.devices, "watching": sum(1 for d in self.devices if d["online"] and d["channel"]),
            "ffmpeg": ffmpeg, "voice": {"mode": "off"}, "hub": {"state": "off"}, "update": {"available": False},
            "settings": dict(self.settings),
        }
        if self.network is not None:
            status["network"] = dict(self.network)
        return status

    def handle(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        self.calls.append((method, path, body))
        base = f"http://localhost:{self.lan_port}"
        if method == "GET" and path == "/local/v1/status":
            return 200, self.status()
        if method != "POST":
            return 404, {"ok": False, "error": "not_found", "message": "没有这个接口"}
        if path.startswith(("/local/v1/remote/", "/local/v1/flash/", "/local/v1/update/")):
            return 501, {"ok": False, "error": "not_implemented", "message": "这个版本还没有这个功能"}
        if path == "/local/v1/console-link":
            if self.setup_code:
                return 200, {"ok": True, "kind": "setup", "url": f"{base}/tv/setup#{self.setup_code}"}
            return 200, {"ok": True, "kind": "login", "url": f"{base}/tv/local-login?t={secrets.token_urlsafe(8)}",
                         "expires_in": 60}
        if path == "/local/v1/phone-qr":
            if self.setup_code:
                return 409, {"ok": False, "error": "setup_pending", "message": "请先完成第一次设置"}
            link = f"http://192.0.2.10:{self.lan_port}/TV/P/{secrets.token_urlsafe(8)}"
            return 200, {"ok": True, "url": link, "expires_in": 300,
                         "svg": "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>"}
        if path == "/local/v1/power":
            if body.get("keep_awake") not in ("watching", "always", "never"):
                return 400, {"ok": False, "error": "bad_request", "message": "keep_awake 不对"}
            self.settings["keep_awake"] = body["keep_awake"]
            return 200, {"ok": True, "settings": dict(self.settings)}
        if path == "/local/v1/autostart":
            if not isinstance(body.get("enabled"), bool):
                return 400, {"ok": False, "error": "bad_request", "message": "enabled 不对"}
            self.settings["autostart"] = body["enabled"]
            return 200, {"ok": True, "settings": dict(self.settings)}
        if path == "/local/v1/ffmpeg/fetch":
            if self.ffmpeg == "ready":
                return 409, {"ok": False, "error": "ready", "message": "视频组件已经好了"}
            self.ffmpeg = "ready"
            return 202, {"ok": True}
        if path == "/local/v1/network/trust" and self.network is not None:
            if body.get("id") == self.network["id"]:
                self.network["trusted"] = bool(body.get("trusted"))
            return 200, {"ok": True}
        if path == "/local/v1/diagnostics":
            dest = Path(body["dest_dir"]) if isinstance(body.get("dest_dir"), str) else self.data / "exports"
            dest.mkdir(parents=True, exist_ok=True)
            out = dest / "diagnostics-dev.zip"
            import zipfile
            with zipfile.ZipFile(out, "w") as archive:
                archive.writestr("status.json", json.dumps(self.status(), ensure_ascii=False, indent=1))
            return 200, {"ok": True, "path": str(out), "size": out.stat().st_size}
        if path == "/local/v1/restart":
            return 202, {"ok": True}
        if path == "/local/v1/stop":
            threading.Thread(target=self.stop, daemon=True).start()
            return 202, {"ok": True}
        return 404, {"ok": False, "error": "not_found", "message": "没有这个接口"}

    def start(self) -> "DevCore":
        core = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _serve(self, method: str) -> None:
                given = self.headers.get("Authorization", "")
                given = given[7:] if given.startswith("Bearer ") else ""
                if not hmac.compare_digest(given.encode(), core.token.encode()):
                    self._reply(401, {"ok": False, "error": "unauthorized", "message": "令牌不对"})
                    return
                if self.headers.get("Origin") is not None or self.headers.get("Host") not in (
                        f"127.0.0.1:{core.port}", f"localhost:{core.port}"):
                    self._reply(403, {"ok": False, "error": "forbidden", "message": "只接受本机程序的请求"})
                    return
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 64 * 1024:
                        self._reply(413, {"ok": False, "error": "too_large", "message": "请求太大"})
                        return
                    try:
                        body = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        self._reply(400, {"ok": False, "error": "bad_json", "message": "请求格式不对"})
                        return
                code, payload = core.handle(method, self.path, body if isinstance(body, dict) else {})
                self._reply(code, payload)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name="devcore").start()
        self.control_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.control_file.with_name("control.json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"api": 1, "port": self.port, "token": self.token, "pid": os.getpid(), "version": VERSION,
                       "started": self.started, "data_dir": str(self.data), "logs_dir": str(self.data / "logs")},
                      handle)
        os.replace(tmp, self.control_file)
        return self

    def stop(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.stopped.set()
        try:
            self.control_file.unlink()
        except OSError:
            pass
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="desktop.devcore")
    parser.add_argument("command", nargs="?", default="selfhost")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--devices", type=int, default=0)
    parser.add_argument("--ffmpeg", default="ready")
    parser.add_argument("--download-seconds", type=float, default=0.0)
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args(argv)
    try:
        from tools import parentwatch
        parentwatch.start()
    except ImportError:
        pass
    # One core per data folder, like the real one (exit 3 when another holds it).
    from .instance import SingleInstance
    lock = SingleInstance(args.data / "run" / "core.lock")
    if not lock.acquire():
        print("devcore: another core holds this data folder", file=sys.stderr, flush=True)
        return LOCK_HELD_EXIT
    core = DevCore(args.data, setup=args.setup or os.environ.get("DEVCORE_SETUP") == "1", devices=args.devices,
                   ffmpeg=args.ffmpeg, download_s=args.download_seconds, network=args.network).start()
    print(f"devcore: control API on 127.0.0.1:{core.port}", flush=True)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: core.stopped.set())
    while not core.stopped.wait(0.5):
        pass
    core.stop()
    lock.release()
    print("devcore: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
