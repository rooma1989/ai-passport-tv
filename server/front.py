"""自建服务器的网页前端：代替云端的 nginx，把手机和电脑的请求转给中转和「用手机听」。

设计见 docs/superpowers/specs/2026-10-15-self-hosting-design.md 第 6.3 节。只用标准库，Docker、Mac、
NAS 上一样跑；由监督进程（server/selfhost）启动。转发的路径和 packaging/nginx/tv-relay.conf、
tv-listen.conf 一样：

  /tv/…                    → 中转的网页端口（去掉 /tv）
  /tv/listen/<路线>/…      → 那个型号电视服务的「用手机听」端口（/listen/…）
  /TV/R/ /TV/B/ /TV/L/ /TV/P/ → 中转的 /r/ /b/ /l/ /p/（设备二维码）
  /TV/W                    → 中转的 /wifi（声波配网）
  /TV/                     → 使用说明（packaging/help/index.html，设备上的帮助二维码）
  /flash/                  → 网页刷机（packaging/flash/）；/flash/firmware/ 是固件（manifest.json、SHA256SUMS、
                             镜像），来自数据文件夹的 firmware/ 或镜像里带的那套（监督进程选好给 --firmware-dir）。
                             网页串口只在安全的页面上能用：在服务器那台电脑上用 http://localhost:<端口>/flash/ 打开，
                             或经隧道的 https；用局域网 IP 打开时页面会说明。两个端口都给（静态文件，不改任何东西）。
  /                        → 跳到 /tv/
  /healthz                 → 中转和各个子进程是否在跑（Docker 的 HEALTHCHECK、NAS 的状态灯）

两个端口，「互联网能到哪些页面」由端口决定，而不是看请求头：

- 局域网端口（默认 8080）：全部页面。
- 公开端口（默认关，8081）：只给隧道（Cloudflare Tunnel、Tailscale serve）用，不映射到主机上。
  拒绝总后台、第一次设置、密钥登录（?k=），以及 signup_via_device = "lan" 时扫码自己注册；
  调用者的地址取 CF-Connecting-IP 或 X-Forwarded-For 的第一个。

每个请求都换上自己的 X-Real-IP 和 X-Forwarded-Proto（客户端带来的一律丢掉）：中转只信本机转来的这两个头，
据此按网段限流、决定 cookie 带不带 Secure（server/relay/web.py 的 _https）。不记访问日志：网址里有会话和码。
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

HELP_PAGE = Path(__file__).resolve().parent.parent / "packaging" / "help" / "index.html"
# 请求体上限：普通接口很小（中转自己也只收 4–8 KB）；导入播放列表可以上传整个 M3U 文件。
BODY_MAX = 64 * 1024
IMPORT_PATH = "/api/admin/channels/import"
IMPORT_BODY_MAX = 3 * 1024 * 1024
# 电脑版登录的长轮询最多等 25 秒；留足余量。
UPSTREAM_TIMEOUT_S = 65
CHUNK = 64 * 1024
# 监督进程每几秒写一次子进程的状态（/data/run/status.json）；太久没更新就当作监督进程不在了。
STATUS_STALE_S = 30
# 设备二维码用的大写短路径 → 中转的路径前缀。
_SHORT = (("/TV/R/", "/r/"), ("/TV/B/", "/b/"), ("/TV/L/", "/l/"), ("/TV/P/", "/p/"), ("/TV/cast/", "/"))
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "proxy-connection"}
# 客户端带来的这些头不转发：由前端自己填（或根本不该从外面来）。
_DROPPED_IN = HOP_BY_HOP | {"x-real-ip", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
                            "cf-connecting-ip", "forwarded", "content-length"}
SIGNUP_POLICIES = ("lan", "anywhere", "off")


class Options(NamedTuple):
    relay_port: int = 8100
    listen: dict = {}               # 路线 → 端口，例如 {"xingzhi": 8198}
    admin_over_tunnel: bool = False
    signup: str = "lan"
    status_file: Path | None = None
    flasher_dir: Path | None = None     # 刷机页（packaging/flash/）
    firmware_dir: Path | None = None    # 固件：manifest.json、SHA256SUMS、镜像


class Route(NamedTuple):
    kind: str                       # proxy / redirect / refuse / help / health / missing
    port: int = 0
    path: str = ""


def route(path: str, query: str, public: bool, options: Options) -> Route:
    """一个请求往哪儿去。path 是原样的路径（不解码），query 是 ? 后面的部分。"""
    if path == "/healthz":
        return Route("health")
    if path in ("/", "/tv"):
        return Route("redirect", path="/tv/")
    if path in ("/TV", "/TV/"):
        return Route("help")
    if path == "/flash":
        return Route("redirect", path="/flash/")
    if path.startswith("/flash/firmware/"):
        return Route("firmware", path=path[len("/flash/firmware/"):])
    if path.startswith("/flash/"):
        return Route("flasher", path=path[len("/flash/"):] or "index.html")
    if path == "/TV/W":
        return _relay("/wifi", query, public, options)
    for prefix, target in _SHORT:
        if path.startswith(prefix):
            return _relay(target + path[len(prefix):], query, public, options)
    if path.startswith("/tv/listen/"):
        name, _, rest = path[len("/tv/listen/"):].partition("/")
        port = options.listen.get(name)
        return Route("proxy", port, "/listen/" + rest) if port else Route("missing")
    if path.startswith("/tv/"):
        return _relay(path[3:], query, public, options)
    return Route("missing")


def _relay(path: str, query: str, public: bool, options: Options) -> Route:
    """转给中转的页面端口；公开端口上先过一遍不许的路径。"""
    check = urllib.parse.unquote(path).lower()
    if "/../" in check or check.endswith("/.."):
        return Route("refuse")
    if check in ("/api/bind/register",) and (options.signup == "off" or (public and options.signup == "lan")):
        return Route("refuse")
    if public:
        if check.startswith(("/setup", "/api/setup")):
            return Route("refuse")
        if not options.admin_over_tunnel:
            keyed = "k" in urllib.parse.parse_qs(query, keep_blank_values=True)
            if keyed or check.startswith(("/admin", "/api/admin")):
                return Route("refuse")
    return Route("proxy", options.relay_port, path)


# 刷机页和固件的类型；SHA256SUMS、LICENSE 这类没有后缀的是文字。
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".json": "application/json", ".txt": "text/plain; charset=utf-8", ".bin": "application/octet-stream",
          ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}
STATIC_MAX = 16 * 1024 * 1024


def static_file(base: Path | None, relative: str, nested: bool = True) -> Path | None:
    """base 下的一个文件；跳出 base、带点开头的名字、太大的，一律当作没有。nested=False 只认 base 里直接的文件。"""
    if base is None or not relative:
        return None
    parts = urllib.parse.unquote(relative).split("/")
    if any(not part or part.startswith(".") for part in parts) or (not nested and len(parts) > 1):
        return None
    try:
        root = Path(base).resolve()
        path = root.joinpath(*parts).resolve()
        path.relative_to(root)
        return path if path.is_file() and path.stat().st_size <= STATIC_MAX else None
    except (OSError, ValueError):
        return None


def content_type(path: Path) -> str:
    return _TYPES.get(path.suffix.lower(), "text/plain; charset=utf-8" if not path.suffix else "application/octet-stream")


def caller_address(headers, peer: str, public: bool) -> str:
    """调用者的地址：局域网端口上就是对端；公开端口上看隧道给的头（拿不到合法地址时用对端）。"""
    if public:
        candidates = [headers.get("CF-Connecting-IP") or "",
                      (headers.get("X-Forwarded-For") or "").split(",")[0]]
        for candidate in candidates:
            candidate = candidate.strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                continue
    return peer


def forwarded_proto(headers, public: bool) -> str:
    """局域网端口是明文 HTTP；公开端口在隧道后面，隧道外面是 HTTPS（隧道说了算，没说就当 HTTPS）。"""
    if not public:
        return "http"
    given = (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return given if given in ("http", "https") else "https"


def health(options: Options, connect=socket.create_connection, clock=time.time) -> tuple[bool, dict]:
    """中转的页面端口连得上，监督进程报告的子进程都在跑。"""
    try:
        connect(("127.0.0.1", options.relay_port), timeout=1).close()
        relay_ok = True
    except OSError:
        relay_ok = False
    report = {"relay": relay_ok}
    healthy = relay_ok
    if options.status_file is not None:
        try:
            status = json.loads(Path(options.status_file).read_text(encoding="utf-8"))
            fresh = clock() - float(status.get("at", 0)) < STATUS_STALE_S
            children = {str(k): bool(v) for k, v in dict(status.get("children") or {}).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            fresh, children = False, {}
        report["children"] = children
        healthy = healthy and fresh and bool(children) and all(children.values())
    return healthy, report


def _handler(options: Options, public: bool):
    class FrontHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "xiaozhi-tv"
        sys_version = ""

        def do_GET(self):
            self._serve("GET")

        def do_HEAD(self):
            self._serve("HEAD")

        def do_POST(self):
            self._serve("POST")

        def do_DELETE(self):
            self._serve("DELETE")

        def _serve(self, method: str) -> None:
            parts = urllib.parse.urlsplit(self.path)
            where = route(parts.path, parts.query, public, options)
            if where.kind == "proxy":
                self._proxy(method, where.port, where.path, parts.query)
            elif where.kind == "redirect":
                self._reply(302, b"", "text/plain", {"Location": where.path})
            elif where.kind == "health":
                healthy, report = health(options)
                self._reply(200 if healthy else 503, json.dumps(dict(report, ok=healthy)).encode(),
                            "application/json")
            elif where.kind == "help" and HELP_PAGE.is_file():
                self._reply(200, HELP_PAGE.read_bytes(), "text/html; charset=utf-8",
                            {"Cache-Control": "public, max-age=3600"})
            elif where.kind in ("flasher", "firmware") and \
                    (found := static_file(options.flasher_dir if where.kind == "flasher" else options.firmware_dir,
                                          where.path, nested=where.kind == "flasher")) is not None:
                self._reply(200, found.read_bytes(), content_type(found), {"Cache-Control": "no-cache"})
            else:
                # 不许的和不存在的一样回答：外面看不出这里有没有这个页面。
                self._drain()
                self._reply(404, NOT_FOUND, "text/html; charset=utf-8")

        def _reply(self, code: int, body: bytes, kind: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", (headers or {}).pop("Cache-Control", "no-store"))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _length(self) -> int | None:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            return length if length >= 0 else None

        def _drain(self) -> None:
            """没转发的请求也要读掉请求体（太大就关连接），免得剩下的字节粘到下一个请求前面。"""
            length = self._length()
            if length is None or length > BODY_MAX:
                self.close_connection = True
            elif length:
                self.rfile.read(length)

        def _proxy(self, method: str, port: int, path: str, query: str) -> None:
            length = self._length()
            limit = IMPORT_BODY_MAX if path == IMPORT_PATH else BODY_MAX
            if length is None or length > limit:
                self.close_connection = True
                self._reply(411 if length is None else 413, b"", "text/plain")
                return
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _DROPPED_IN}
            if public:
                # 中转的密钥只在本机用（电视服务的语音命令直接连中转），从隧道来的不转。
                headers = {k: v for k, v in headers.items() if k.lower() != "x-key"}
            headers["X-Real-IP"] = caller_address(self.headers, self.client_address[0], public)
            headers["X-Forwarded-Proto"] = forwarded_proto(self.headers, public)
            if body is not None:
                headers["Content-Length"] = str(len(body))
            upstream = http.client.HTTPConnection("127.0.0.1", port, timeout=UPSTREAM_TIMEOUT_S)
            try:
                upstream.request(method, path + ("?" + query if query else ""), body=body, headers=headers)
                response = upstream.getresponse()
            except OSError:
                upstream.close()
                self._reply(502, UNAVAILABLE, "text/html; charset=utf-8")
                return
            try:
                self._relay_response(method, response)
            except OSError:
                self.close_connection = True
            finally:
                upstream.close()

        def _relay_response(self, method: str, response) -> None:
            self.send_response_only(response.status, response.reason)
            self.send_header("Date", self.date_time_string())
            sized = response.getheader("Content-Length") is not None
            for name, value in response.getheaders():
                if name.lower() in HOP_BY_HOP or name.lower() in ("date", "server"):
                    continue
                self.send_header(name, value)
            if not sized and method != "HEAD":
                # 不知道多长（流式的回答）：读到上游关闭为止，之后这条连接也关掉。
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if method == "HEAD":
                return
            while True:
                chunk = response.read1(CHUNK) if hasattr(response, "read1") else response.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()

        def log_message(self, *_args):
            pass

    return FrontHandler


NOT_FOUND = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
             "<body style='background:#1c1915;color:#e6dcc8;font:16px/1.6 serif;padding:24px'>"
             "<p>打不开。</p>").encode()
UNAVAILABLE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
               "<body style='background:#1c1915;color:#e6dcc8;font:16px/1.6 serif;padding:24px'>"
               "<p>服务还没准备好，过几秒再刷新一下。</p>").encode()


def make_server(bind: str, port: int, options: Options, public: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((bind, port), _handler(options, public))
    server.daemon_threads = True
    return server


def _pairs(values: list[str], what: str) -> dict:
    result = {}
    for value in values:
        name, _, port = value.partition("=")
        if not name or not port.isdigit():
            raise SystemExit(f"{what} {value!r}：要写成 名字=端口")
        result[name] = int(port)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m server.front", description="自建服务器的网页前端")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--lan-port", type=int, default=8080)
    parser.add_argument("--public-port", type=int, default=0, help="给隧道用的端口，0 = 不开")
    parser.add_argument("--public-bind", default="0.0.0.0", help="公开端口听哪个地址（容器里给隧道容器用）")
    parser.add_argument("--relay-port", type=int, default=8100)
    parser.add_argument("--listen", action="append", default=[], metavar="路线=端口",
                        help="「用手机听」：/tv/listen/<路线>/ 转到本机这个端口，可重复")
    parser.add_argument("--admin-over-tunnel", action="store_true", help="公开端口也给总后台和密钥登录")
    parser.add_argument("--signup", choices=SIGNUP_POLICIES, default="lan",
                        help="扫未绑定设备的码自己注册：lan 只在局域网端口，anywhere 两个端口都行，off 都不行")
    parser.add_argument("--status-file", type=Path, help="监督进程写的子进程状态（/healthz 用）")
    parser.add_argument("--flasher-dir", type=Path, help="网页刷机页的文件夹（packaging/flash/），在 /flash/ 下")
    parser.add_argument("--firmware-dir", type=Path, help="固件文件夹（manifest.json 等），在 /flash/firmware/ 下")
    args = parser.parse_args(argv)
    options = Options(relay_port=args.relay_port, listen=_pairs(args.listen, "--listen"),
                      admin_over_tunnel=args.admin_over_tunnel, signup=args.signup, status_file=args.status_file,
                      flasher_dir=args.flasher_dir, firmware_dir=args.firmware_dir)
    servers = [make_server(args.bind, args.lan_port, options)]
    if args.public_port:
        servers.append(make_server(args.public_bind, args.public_port, options, public=True))
    import threading
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"网页前端已启动：局域网 {args.bind}:{args.lan_port}"
          + (f"，公开（给隧道）{args.public_bind}:{args.public_port}" if args.public_port else ""), flush=True)
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
