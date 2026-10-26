"""本机控制接口：托盘（以后是 Tauri 外壳）做的每件事都经过它。

设计和约定见 docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md 第 5.13 节「实际实现的约定」。
只听 127.0.0.1 的随机端口；端口和随机令牌写进 <数据目录>/run/control.json（0600）。每个请求：

- 带 Authorization: Bearer <令牌>（比对用 hmac.compare_digest），没有或不对 401；
- Host 只能是 127.0.0.1:<端口> 或 localhost:<端口>（DNS 重绑定的网页拿不到），否则 403；
- 带 Origin 头的一律 403：浏览器里的网页没有理由用这个接口；
- 请求体是 JSON 对象，最多 64 KB。

能用这个接口的人已经能读用户自己的文件（令牌就在里面），所以它给的东西（本机的一次性管理员登录）不算新权限。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .desktop import API_VERSION, KEEP_AWAKE
from .version import VERSION

BODY_MAX = 64 * 1024
PREFIX = "/local/v1/"
LATER = {"remote/": "P5", "flash/": "P7", "update/": "P8"}


class ControlError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _not_ready() -> ControlError:
    return ControlError(503, "not_ready", "电视服务还在启动，请稍等几秒再试")


class Actions:
    """接口背后的动作；desktop 是 desktop.Desktop。"""

    def __init__(self, desktop):
        self.desktop = desktop

    def status(self, _body: dict) -> tuple[int, dict]:
        return 200, self.desktop.status()

    def console_link(self, _body: dict) -> tuple[int, dict]:
        desktop = self.desktop
        if desktop.state() != "running" or not desktop.front_up():
            raise _not_ready()
        port = desktop.lan_port
        code = desktop.setup_code()
        if code:
            return 200, {"ok": True, "kind": "setup", "url": f"http://localhost:{port}/tv/setup#{code}"}
        from ..tenancy.accounts import Accounts
        from ..tenancy.phonelinks import PhoneLinks
        accounts = Accounts(desktop.layout.db)
        try:
            admin = accounts.admin_id()
            if admin is None:
                raise _not_ready()
            made = PhoneLinks(accounts).create_local(admin)
        except ValueError:
            raise ControlError(409, "no_admin", "没有可用的管理员账号（被封了？）") from None
        finally:
            accounts.close()
        return 200, {"ok": True, "kind": "login", "url": f"http://localhost:{port}/tv/local-login?t={made['token']}",
                     "expires_in": made["ttl"]}

    def phone_qr(self, _body: dict) -> tuple[int, dict]:
        desktop = self.desktop
        if desktop.layout.setup_code.exists():
            raise ControlError(409, "setup_pending", "还没有建管理员：先在这台电脑上打开电脑版完成设置")
        if not desktop.lan_url:
            raise ControlError(503, "no_lan_address", "不知道这台电脑在家里网络中的地址（只听本机，或没有连网）")
        if desktop.state() != "running":
            raise _not_ready()
        from ..relay import qr
        from ..tenancy.accounts import Accounts
        from ..tenancy.phonelinks import PhoneLinks
        from ..tenancy.tickets import Limited
        accounts = Accounts(desktop.layout.db)
        try:
            admin = accounts.admin_id()
            if admin is None:
                raise ControlError(409, "setup_pending", "还没有建管理员")
            made = PhoneLinks(accounts).create(admin, "127.0.0.1", "小鼠电视（这台电脑）", source="pc")
        except Limited as error:
            raise ControlError(429, "limited", str(error)) from None
        finally:
            accounts.close()
        base = f"http://{desktop.lan_address}:{desktop.lan_port}"
        link = f"{base.upper()}/TV/P/{made['token']}"
        return 200, {"ok": True, "url": link, "svg": qr.svg(link), "code": made["code"], "typed": f"{base}/tv/p",
                     "expires_in": made["ttl"]}

    def power(self, body: dict) -> tuple[int, dict]:
        value = body.get("keep_awake")
        if value not in KEEP_AWAKE:
            raise ControlError(400, "bad_request", f"keep_awake 只能是 {' / '.join(KEEP_AWAKE)}")
        return 200, {"ok": True, "settings": self.desktop.settings.update(keep_awake=value)}

    def autostart(self, body: dict) -> tuple[int, dict]:
        value = body.get("enabled")
        if not isinstance(value, bool):
            raise ControlError(400, "bad_request", "enabled 要是 true 或 false")
        return 200, {"ok": True, "settings": self.desktop.settings.update(autostart=value)}

    def network_trust(self, body: dict) -> tuple[int, dict]:
        """受信任的网络（第 5.11 节）：{"id": <状态里 network.id>, "trusted": true | false}。"""
        from .network import valid_id
        network = getattr(self.desktop, "network", None)
        if network is None:
            raise ControlError(409, "not_available", "这个核心不管受信任的网络（设置里只听本机，或设置文件有错）")
        network_id, trusted = body.get("id"), body.get("trusted")
        if not valid_id(network_id):
            raise ControlError(400, "bad_request", "id 要是状态里 network.id 的值（16 位十六进制）")
        if not isinstance(trusted, bool):
            raise ControlError(400, "bad_request", "trusted 要是 true 或 false")
        return 200, {"ok": True, "network": network.trust(network_id, trusted)}

    def ffmpeg_fetch(self, body: dict) -> tuple[int, dict]:
        from tools import ffmpeg_fetch
        mirror = body.get("mirror")
        if mirror is not None and (not isinstance(mirror, str) or not ffmpeg_fetch.valid_mirror(mirror)):
            raise ControlError(400, "bad_request", "镜像要写成 https:// 开头的基础网址")
        state = self.desktop.ffmpeg.state
        if state == "ready":
            raise ControlError(409, "ready", "ffmpeg 已经准备好了")
        if not self.desktop.ffmpeg.fetch(mirror or None):
            raise ControlError(409, "busy", "正在下载 ffmpeg")
        return 202, {"ok": True}

    def diagnostics(self, body: dict) -> tuple[int, dict]:
        from . import diagnostics
        dest = body.get("dest_dir")
        if dest is not None and not isinstance(dest, str):
            raise ControlError(400, "bad_request", "dest_dir 要是一个文件夹路径")
        try:
            path = diagnostics.build(self.desktop, Path(dest) if dest else None)
        except OSError as error:
            raise ControlError(500, "write_failed", f"写不了诊断压缩包：{type(error).__name__}") from None
        return 200, {"ok": True, "path": str(path), "size": path.stat().st_size}

    def restart(self, _body: dict) -> tuple[int, dict]:
        supervisor = self.desktop.supervisor
        if supervisor is None or not supervisor.started:
            raise _not_ready()
        reload = getattr(self.desktop, "reload", None)
        if reload is None:
            supervisor.restart_requested.set()
            return 202, {"ok": True}

        def reload_and_rebuild():
            # 重读设置、重新识别地址（Windows 上起一次 PowerShell，一两秒）：不让托盘的请求等着。
            try:
                reload()
            except Exception as error:      # noqa: BLE001 —— 重读失败也照常重启
                getattr(self.desktop, "log", print)(f"重读设置失败：{type(error).__name__}")
            supervisor.rebuild_requested.set()

        threading.Thread(target=reload_and_rebuild, daemon=True, name="restart").start()
        return 202, {"ok": True}

    def stop(self, _body: dict) -> tuple[int, dict]:
        supervisor = self.desktop.supervisor
        if supervisor is not None:
            supervisor.stopping.set()
        else:
            self.desktop.stop_event.set()
        return 202, {"ok": True}


ROUTES = {
    ("GET", "status"): "status",
    ("POST", "console-link"): "console_link",
    ("POST", "phone-qr"): "phone_qr",
    ("POST", "power"): "power",
    ("POST", "autostart"): "autostart",
    ("POST", "network/trust"): "network_trust",
    ("POST", "ffmpeg/fetch"): "ffmpeg_fetch",
    ("POST", "diagnostics"): "diagnostics",
    ("POST", "restart"): "restart",
    ("POST", "stop"): "stop",
}


def _handler(server: "ControlServer"):
    class ControlHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "xiaozhi-tv-core"
        sys_version = ""

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_PUT(self):
            self._serve("PUT")

        def do_DELETE(self):
            self._serve("DELETE")

        def _answer(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, error: ControlError) -> None:
            self._answer(error.status, {"ok": False, "error": error.code, "message": error.message})

        def _serve(self, method: str) -> None:
            try:
                self._check()
                body = self._body()
                path = self.path.split("?", 1)[0]
                if not path.startswith(PREFIX):
                    raise ControlError(404, "not_found", "没有这个接口")
                name = path[len(PREFIX):]
                for prefix, phase in LATER.items():
                    if name.startswith(prefix):
                        raise ControlError(501, "not_implemented", f"这一版还没有（{phase}）")
                action = ROUTES.get((method, name))
                if action is None:
                    known = any(route == name for _, route in ROUTES)
                    raise ControlError(405 if known else 404, "method_not_allowed" if known else "not_found",
                                       "方法不对" if known else "没有这个接口")
                status, answer = getattr(server.actions, action)(body)
                self._answer(status, answer)
            except ControlError as error:
                self._error(error)
            except Exception as error:      # noqa: BLE001 —— 接口出错不能拖垮监督进程
                server.log(f"控制接口出错：{type(error).__name__}: {error}")
                self._error(ControlError(500, "internal", f"内部错误：{type(error).__name__}"))

        def _check(self) -> None:
            if self.headers.get("Origin") is not None:
                self.close_connection = True
                raise ControlError(403, "forbidden", "网页不能用这个接口")
            host = (self.headers.get("Host") or "").strip().lower()
            if host not in (f"127.0.0.1:{server.port}", f"localhost:{server.port}"):
                self.close_connection = True
                raise ControlError(403, "forbidden", "只认本机地址")
            given = self.headers.get("Authorization") or ""
            expected = f"Bearer {server.token}"
            if not hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("ascii")):
                self.close_connection = True
                raise ControlError(401, "unauthorized", "没有令牌或令牌不对（看 run/control.json）")

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > BODY_MAX or "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                raise ControlError(413, "too_large", "请求体太大")
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeError, ValueError):
                raise ControlError(400, "bad_request", "请求体要是 JSON") from None
            if not isinstance(data, dict):
                raise ControlError(400, "bad_request", "请求体要是 JSON 对象")
            return data

        def log_message(self, *_args):
            pass

    return ControlHandler


class ControlServer:
    """在 127.0.0.1 的随机端口上开接口，写 control.json；close() 关掉并删掉文件。"""

    def __init__(self, desktop, run_dir: Path, log=print, port: int = 0):
        self.desktop = desktop
        self.actions = Actions(desktop)
        self.log = log
        self.token = secrets.token_urlsafe(32)
        self.file = Path(run_dir) / "control.json"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler(self))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        desktop.control_port = self.port

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="control-api").start()
        self._write()

    def _write(self) -> None:
        desktop = self.desktop
        info = {"api": API_VERSION, "port": self.port, "token": self.token, "pid": os.getpid(), "version": VERSION,
                "started": desktop.started, "data_dir": str(desktop.layout.root), "logs_dir": str(desktop.logs_dir)}
        tmp = self.file.with_name(self.file.name + ".tmp")
        try:
            tmp.unlink()
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(info, handle)
        os.replace(tmp, self.file)

    def close(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except OSError:
            pass
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if data.get("token") == self.token:
                self.file.unlink()
        except (OSError, ValueError):
            pass


def read_control(run_dir: Path) -> dict | None:
    """读 control.json（托盘、测试、冒烟测试用）；没有或读不懂时 None。"""
    try:
        data = json.loads((Path(run_dir) / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("port"), int) else None


def call(info: dict, method: str, name: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict]:
    """调一次接口：(状态码, 回答)。给冒烟测试和测试用；托盘可以照着写。"""
    import http.client
    connection = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=timeout)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {info['token']}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, PREFIX + name, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            answer = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            answer = {"raw": raw[:200].decode("utf-8", "replace")}
        return response.status, answer
    finally:
        connection.close()


def wait_for_port(port: int, timeout: float, host: str = "127.0.0.1") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection((host, port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False
