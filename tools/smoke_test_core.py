#!/usr/bin/env python3
"""Smoke test of the PC client's core: the frozen onedir build, or the same code from a checkout.

    python3 tools/smoke_test_core.py --core dist/xiaozhi-tv-core/xiaozhi-tv-core [--ffmpeg fetch]
    python3 tools/smoke_test_core.py --source [--ffmpeg /opt/homebrew/bin/ffmpeg]

What it checks, in order (docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md, 5.2 and 5.13):

1. Every sub-command the supervisor starts its children by answers `--help` from the build
   (`__relay`, `__front`, `__tv`, `__selfhost`, `__tenancy-admin`) -- PyInstaller cannot see
   imports reached through a name at run time, and this is where a missing one shows.
2. `selfhost --desktop` on 127.0.0.1 with a fresh data folder whose path has Chinese
   characters and a space: control.json appears, the status reaches "running" (after the
   ffmpeg download with --ffmpeg fetch), /healthz answers 200.
3. The control API: refuses no token, a wrong token and an Origin header; console-link gives
   the setup URL, the setup page creates the admin, console-link then gives a one-time login
   URL that signs a browser in as admin exactly once; phone-qr says there is no LAN address
   (loopback only); power and autostart are stored; diagnostics makes a zip with no secrets.
4. restart, then stop through the API: exit code 0, control.json removed, no process left.
5. A second start stops when its standard-input pipe closes (what the tray does); a second
   core on the same data folder exits with 3.
6. A third start is killed outright: every child must be gone within seconds.

Every failure is printed as a GitHub Actions `::error::` line, so the reason shows in the run's
annotations without the log. Uses only the standard library. Ports 8080, 8097-8100, 8198, 8199
must be free.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.console import use_utf8  # noqa: E402

use_utf8()

PORTS = (8080, 8097, 8098, 8099, 8100, 8198, 8199)
SUBCOMMANDS = ("__relay", "__front", "__tv", "__selfhost", "__tenancy-admin")


class Failure(Exception):
    pass


def fail(message: str) -> None:
    raise Failure(message)


def note(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


def request(method: str, port: int, path: str, body=None, headers=None, timeout=15.0):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        all_headers = dict(headers or {})
        if data is not None:
            all_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=data, headers=all_headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), raw
    finally:
        connection.close()


def api(info: dict, method: str, name: str, body=None, extra=None):
    headers = {"Authorization": f"Bearer {info['token']}"}
    headers.update(extra or {})
    status, _headers, raw = request(method, info["port"], "/local/v1/" + name, body, headers)
    try:
        answer = json.loads(raw.decode()) if raw else {}
    except ValueError:
        answer = {"raw": raw[:200].decode("utf-8", "replace")}
    return status, answer


def port_open(port: int) -> bool:
    import socket
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def leftovers(marker: str) -> list[str]:
    """Processes whose command line mentions the data folder (all children get --db <data>/tv.db)."""
    if os.name == "nt":
        script = ("Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stdout
    else:
        out = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout
    me = str(os.getpid())
    return [line.strip() for line in out.splitlines()
            if marker in line and "smoke_test_core" not in line and not line.strip().startswith(me + " ")]


class Core:
    def __init__(self, command: list[str], data: Path, ffmpeg: str | None, log: Path):
        self.command, self.data, self.ffmpeg, self.log_path = command, data, ffmpeg, log
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        argv = self.command + ["selfhost", "--desktop", "--data", str(self.data), "--bind", "127.0.0.1"]
        if self.ffmpeg:
            argv += ["--ffmpeg", self.ffmpeg]
        env = dict(os.environ, TV_WATCH_PARENT="1", PYTHONIOENCODING="utf-8")
        self.log_file = open(self.log_path, "ab")
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=self.log_file,
                                        stderr=subprocess.STDOUT, env=env, **kwargs)
        note(f"started core pid {self.process.pid}")

    def tail(self, lines: int = 40) -> str:
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def control(self, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        path = self.data / "run" / "control.json"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                fail(f"the core exited with {self.process.returncode} before writing control.json")
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                if info.get("pid") and port_open(info["port"]):
                    return info
            except (OSError, ValueError):
                pass
            time.sleep(0.3)
        fail("control.json did not appear")

    def wait_running(self, info: dict, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                fail(f"the core exited with {self.process.returncode} while starting")
            status, answer = api(info, "GET", "status")
            if status != 200:
                fail(f"status answered {status}: {answer}")
            state = answer.get("state")
            ffmpeg = answer.get("ffmpeg", {})
            line = f"state={state} ffmpeg={ffmpeg.get('state')} {ffmpeg.get('done')}/{ffmpeg.get('total')}"
            if line != last:
                note(line)
                last = line
            if state == "running" and all(c.get("running") for c in answer.get("children", {}).values()) \
                    and answer.get("children"):
                return answer
            if state == "error":
                fail(f"the core reports an error: {answer.get('problems')}")
            time.sleep(1)
        fail(f"the core did not reach 'running' in {timeout:.0f} s (last: {last})")

    def wait_exit(self, timeout: float) -> int:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            fail(f"the core did not exit within {timeout:.0f} s")
        finally:
            self.log_file.close()


def wait_health(port: int, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            status, _h, raw = request("GET", port, "/healthz", timeout=5)
            last = (status, raw[:200])
            if status == 200:
                return
        except OSError as error:
            last = error
        time.sleep(1)
    fail(f"/healthz did not answer 200 in {timeout:.0f} s: {last}")


def wait_gone(marker: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        left = leftovers(marker)
        busy = [p for p in PORTS if port_open(p)]
        if not left and not busy:
            return
        time.sleep(0.5)
    fail(f"left behind after {timeout:.0f} s: processes {left}, ports {busy}")


def check_subcommands(command: list[str]) -> None:
    for sub in SUBCOMMANDS:
        done = subprocess.run(command + [sub, "--help"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, stdin=subprocess.DEVNULL)
        if done.returncode != 0 or "usage" not in (done.stdout + done.stderr).lower():
            fail(f"`{sub} --help` exited {done.returncode}: {(done.stdout + done.stderr)[-600:]}")
        note(f"{sub} --help: ok")


def check_device(port: int) -> None:
    """An unbound device: HELLO, then CONFIG with the bind channel, the palette and a picture.
    Exercises the frozen media server end to end (protocol, tenancy gate, frames, ffmpeg)."""
    import socket
    from server.protocol import Kind, Packet, json_bytes, json_object, receive_packet, send_packet
    hello = {"version": 1, "device_id": "5a0ce0000001", "device_secret": "S" * 43,
             "model": "xingzhi-cube-154", "fw": "smoke"}
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        connection.setblocking(False)
        send_packet(connection, Packet(Kind.HELLO, 0, 0, 0, json_bytes(hello)))
        config = receive_packet(connection, 15)
        if config.kind != Kind.CONFIG:
            fail(f"the media server answered HELLO with {config.kind.name}")
        payload = json_object(config.payload)
        if (payload.get("bound"), payload.get("channel")) != (0, "bind"):
            fail(f"an unbound device got {payload}")
        kinds = set()
        deadline = time.monotonic() + 30
        while Kind.JPEG not in kinds and time.monotonic() < deadline:
            kinds.add(receive_packet(connection, 30, config.session).kind)
        if Kind.JPEG not in kinds:
            fail(f"no picture for the unbound device, only {sorted(k.name for k in kinds)}")
    finally:
        connection.close()


def check_channel_check(command: list[str]) -> None:
    """The relay checks a channel table in a child of its own (server/relay/channeltable.py)."""
    done = subprocess.run(command + ["__channel-check", "--check"], input="a | A | http://127.0.0.1/a.m3u8\n",
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    try:
        answer = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        answer = None
    if done.returncode != 0 or not isinstance(answer, dict) or answer.get("ok") is not True:
        fail(f"`__channel-check --check` exited {done.returncode}: {(done.stdout + done.stderr)[-600:]}")
    note("__channel-check --check: ok")


def secrets_of(data: Path) -> list[str]:
    found = []
    for path in (data / "secrets").glob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if len(text) >= 8:
                found.append(text)
    return found


def check_api(core: Core, info: dict, scratch: Path) -> None:
    port = info["port"]
    status, _h, _raw = request("GET", port, "/local/v1/status")
    if status != 401:
        fail(f"status without a token answered {status}, expected 401")
    status, _a = api(dict(info, token="wrong"), "GET", "status")
    if status != 401:
        fail(f"status with a wrong token answered {status}, expected 401")
    status, _a = api(info, "GET", "status", extra={"Origin": "http://evil.example"})
    if status != 403:
        fail(f"status with an Origin header answered {status}, expected 403")
    status, answer = api(info, "POST", "remote/enable")
    if status != 501:
        fail(f"remote/enable answered {status}, expected 501")

    answer = core.status = api(info, "GET", "status")[1]
    lan = answer["ports"]["lan"]
    wait_health(lan)
    note(f"/healthz on :{lan}: 200")

    status, answer = api(info, "POST", "console-link")
    if status != 200 or answer.get("kind") != "setup" or "#" not in answer.get("url", ""):
        fail(f"console-link before setup: {status} {answer}")
    code = answer["url"].split("#", 1)[1]
    status, _h, raw = request("GET", lan, "/tv/setup")
    if status != 200:
        fail(f"/tv/setup answered {status}")
    status, headers, raw = request("POST", lan, "/tv/api/setup", {"code": code, "name": "冒烟"},
                                {"Host": f"localhost:{lan}"})
    if status != 200 or not json.loads(raw).get("ok"):
        fail(f"setup failed: {status} {raw[:200]!r}")
    note("setup through the setup code: ok")

    status, answer = api(info, "POST", "console-link")
    if status != 200 or answer.get("kind") != "login":
        fail(f"console-link after setup: {status} {answer}")
    path = answer["url"].split(f"localhost:{lan}", 1)[1]
    status, headers, _raw = request("GET", lan, path, headers={"Host": f"localhost:{lan}"})
    cookie = (headers.get("Set-Cookie") or "").split(";", 1)[0]
    if status != 302 or not cookie.startswith("tvs=") or headers.get("Location") != "/tv/console":
        fail(f"local-login: {status} {headers}")
    status, _h, raw = request("GET", lan, "/tv/api/login/status", headers={"Cookie": cookie})
    me = json.loads(raw).get("me") or {}
    if status != 200 or me.get("role") != "admin":
        fail(f"signed in as {me} after the console link")
    status, headers, _raw = request("GET", lan, path, headers={"Host": f"localhost:{lan}"})
    if "Set-Cookie" in headers:
        fail("the console link worked twice")
    note("console-link signs in as admin, once: ok")

    check_device(core.status["ports"]["xingzhi"])
    listed = [d["id"] for d in api(info, "GET", "status")[1]["devices"]]
    if "5a0ce0000001" not in listed:
        fail(f"the simulated device is not in the status: {listed}")
    note("a simulated Xingzhi gets CONFIG and the bind picture, and is listed: ok")

    status, answer = api(info, "POST", "phone-qr")
    if status != 503 or answer.get("error") != "no_lan_address":
        fail(f"phone-qr on loopback: {status} {answer}")
    status, answer = api(info, "POST", "power", {"keep_awake": "always"})
    if status != 200 or answer["settings"]["keep_awake"] != "always":
        fail(f"power: {status} {answer}")
    status, answer = api(info, "POST", "autostart", {"enabled": False})
    if status != 200 or answer["settings"]["autostart"] is not False:
        fail(f"autostart: {status} {answer}")
    stored = json.loads((core.data / "desktop.json").read_text(encoding="utf-8"))
    if stored != {"keep_awake": "always", "autostart": False}:
        fail(f"desktop.json is {stored}")

    status, answer = api(info, "POST", "diagnostics", {"dest_dir": str(scratch)})
    if status != 200 or not Path(answer.get("path", "")).is_file():
        fail(f"diagnostics: {status} {answer}")
    with zipfile.ZipFile(answer["path"]) as archive:
        names = archive.namelist()
        blob = b"".join(archive.read(name) for name in names).decode("utf-8", "replace")
    if "logs/core.log" not in names or "status.json" not in names:
        fail(f"diagnostics zip has {names}")
    leaked = [s[:6] + "…" for s in secrets_of(core.data) + [info["token"], cookie[4:], code] if s and s in blob]
    if leaked:
        fail(f"diagnostics zip contains secrets: {leaked}")
    note(f"diagnostics: {len(names)} files, no secrets")


def run(command: list[str], ffmpeg: str | None, keep: bool, fetch_timeout: float) -> None:
    busy = [p for p in PORTS if port_open(p)]
    if busy:
        fail(f"ports already in use before the test: {busy}")
    scratch = Path(tempfile.mkdtemp(prefix="xztv-smoke-"))
    marker = scratch.name
    data = scratch / "数据 目录"
    note(f"data folder: {data}")
    try:
        core = Core(command, data, ffmpeg, scratch / "core-1.out")
        core.start()
        info = core.control()
        core.wait_running(info, fetch_timeout)
        check_api(core, info, scratch)

        status, _a = api(info, "POST", "restart")
        if status != 202:
            fail(f"restart answered {status}")
        time.sleep(2)
        wait_health(core.status["ports"]["lan"])
        note("restart: ok")
        status, _a = api(info, "POST", "stop")
        if status != 202:
            fail(f"stop answered {status}")
        code = core.wait_exit(40)
        if code != 0:
            fail(f"stop: the core exited with {code}\n{core.tail()}")
        if (data / "run" / "control.json").exists():
            fail("control.json is still there after a clean stop")
        wait_gone(marker, 15)
        note("stop through the API: exit 0, nothing left")

        core = Core(command, data, ffmpeg, scratch / "core-2.out")
        core.start()
        info = core.control()
        core.wait_running(info, 120)
        second = subprocess.run(command + ["selfhost", "--desktop", "--data", str(data), "--bind", "127.0.0.1"],
                                capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        if second.returncode != 3:
            fail(f"a second core on the same data folder exited {second.returncode}, expected 3")
        note("second core on the same data folder: exit 3")
        core.process.stdin.close()
        code = core.wait_exit(40)
        if code != 0:
            fail(f"closing the pipe: the core exited with {code}\n{core.tail()}")
        wait_gone(marker, 15)
        note("closing the tray's pipe: exit 0, nothing left")

        core = Core(command, data, ffmpeg, scratch / "core-3.out")
        core.start()
        info = core.control()
        core.wait_running(info, 120)
        wait_health(api(info, "GET", "status")[1]["ports"]["lan"])
        core.process.kill()
        core.process.wait()
        core.log_file.close()
        wait_gone(marker, 20)
        note("hard kill of the core: every child gone")
    except Failure:
        for out in sorted(scratch.glob("core-*.out")):
            print(f"----- {out.name} (last lines) -----")
            print("\n".join(out.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]))
        raise
    finally:
        for left in leftovers(marker):
            pid = int(left.split()[0])
            try:
                os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
            except OSError:
                pass
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--core", type=Path, help="the built xiaozhi-tv-core executable")
    where.add_argument("--source", action="store_true", help="run the same code from this checkout")
    parser.add_argument("--ffmpeg", help="passed to the core: a path, or 'fetch' for the pinned download")
    parser.add_argument("--fetch-timeout", type=float, default=600.0)
    parser.add_argument("--keep", action="store_true", help="keep the temporary data folder")
    args = parser.parse_args(argv)
    if args.core:
        command = [str(args.core.resolve())]
    else:
        command = [sys.executable, "-u", str(ROOT / "tools" / "packaged_entry.py")]
    try:
        check_subcommands(command)
        check_channel_check(command)
        run(command, args.ffmpeg, args.keep, args.fetch_timeout)
    except Failure as error:
        for line in str(error).splitlines()[:1]:
            print(f"::error::core smoke test: {line}", flush=True)
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:      # noqa: BLE001 —— a crash of the test itself is a failure too
        print(f"::error::core smoke test crashed: {type(error).__name__}: {error}", flush=True)
        raise
    print("[smoke] all checks passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
