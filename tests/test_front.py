"""The self-hosted server's web front (server/front.py): routes, the two ports, headers, /healthz.

Phase S1b of docs/superpowers/specs/2026-10-15-self-hosting-design.md, section
6.3. The routes must match packaging/nginx/tv-relay.conf and tv-listen.conf (the
relay and the listen servers are the same code as on the cloud host). The
public port is for a tunnel: admin, setup and the key link are refused there,
so "what the internet can reach" is decided by the port, not by a header. The
caller's address and scheme always come from the front, never from the client.
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import front

OPTIONS = front.Options(relay_port=8100, listen={"xingzhi": 8198, "passport": 8199})


class RouteTests(unittest.TestCase):
    def test_paths_match_the_cloud_nginx(self):
        cases = {
            "/tv/": ("proxy", 8100, "/"),
            "/tv/api/state": ("proxy", 8100, "/api/state"),
            "/tv/console": ("proxy", 8100, "/console"),
            "/TV/R/ABCDEFGHIJKLMNOPQRST": ("proxy", 8100, "/r/ABCDEFGHIJKLMNOPQRST"),
            "/TV/B/ABCDEFGHIJKLMNOPQRST": ("proxy", 8100, "/b/ABCDEFGHIJKLMNOPQRST"),
            "/TV/L/X": ("proxy", 8100, "/l/X"),
            "/TV/P/X": ("proxy", 8100, "/p/X"),
            "/TV/W": ("proxy", 8100, "/wifi"),
            "/tv/listen/xingzhi/a1b2c3d4e5f6/live.m3u8": ("proxy", 8198, "/listen/a1b2c3d4e5f6/live.m3u8"),
            "/tv/listen/passport/a1b2c3d4e5f6/3.aac": ("proxy", 8199, "/listen/a1b2c3d4e5f6/3.aac"),
            "/": ("redirect", 0, "/tv/"),
            "/tv": ("redirect", 0, "/tv/"),
            "/TV/": ("help", 0, ""),
            "/flash": ("redirect", 0, "/flash/"),
            "/flash/": ("flasher", 0, "index.html"),
            "/flash/vendor/esptool-js-0.6.1/bundle.js": ("flasher", 0, "vendor/esptool-js-0.6.1/bundle.js"),
            "/flash/firmware/manifest.json": ("firmware", 0, "manifest.json"),
            "/healthz": ("health", 0, ""),
            "/tv/listen/other/x": ("missing", 0, ""),
            "/etc/passwd": ("missing", 0, ""),
        }
        for path, expected in cases.items():
            self.assertEqual(tuple(front.route(path, "", False, OPTIONS)), expected, path)

    def test_lan_port_serves_everything(self):
        for path, query in (("/tv/admin", ""), ("/tv/api/admin/users", ""), ("/tv/setup", ""),
                            ("/tv/api/setup", ""), ("/tv/", "k=secret"), ("/tv/api/bind/register", "")):
            self.assertEqual(front.route(path, query, False, OPTIONS).kind, "proxy", path)

    def test_public_port_refuses_admin_setup_key_and_signup(self):
        for path, query in (("/tv/admin", ""), ("/tv/admin/", ""), ("/tv/api/admin/channels", ""),
                            ("/tv/%61dmin", ""), ("/tv/ADMIN", ""), ("/tv/setup", ""), ("/tv/api/setup", ""),
                            ("/tv/api/setup/info", ""), ("/tv/api/setup/wifi-qr.svg", ""),
                            ("/tv/API/SETUP/wifi-qr.svg", ""), ("/tv/", "k=secret"), ("/tv/", "x=1&k="),
                            ("/tv/api/bind/register", ""), ("/tv/x/../admin", "")):
            self.assertEqual(front.route(path, query, True, OPTIONS).kind, "refuse", (path, query))
        for path in ("/tv/", "/tv/api/state", "/TV/R/ABC", "/tv/console", "/TV/W", "/tv/listen/xingzhi/x"):
            self.assertEqual(front.route(path, "", True, OPTIONS).kind, "proxy", path)

    def test_admin_over_tunnel_and_signup_policies(self):
        opened = OPTIONS._replace(admin_over_tunnel=True, signup="anywhere")
        self.assertEqual(front.route("/tv/admin", "", True, opened).kind, "proxy")
        self.assertEqual(front.route("/tv/", "k=x", True, opened).kind, "proxy")
        self.assertEqual(front.route("/tv/api/bind/register", "", True, opened).kind, "proxy")
        self.assertEqual(front.route("/tv/setup", "", True, opened).kind, "refuse", "setup is LAN only, always")
        off = OPTIONS._replace(signup="off")
        self.assertEqual(front.route("/tv/api/bind/register", "", False, off).kind, "refuse")

    def test_caller_address(self):
        headers = {"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "198.51.100.7, 10.0.0.1"}
        self.assertEqual(front.caller_address(headers, "172.18.0.3", public=True), "203.0.113.9")
        self.assertEqual(front.caller_address({"X-Forwarded-For": "198.51.100.7, 10.0.0.1"}, "172.18.0.3", True),
                         "198.51.100.7")
        self.assertEqual(front.caller_address({"X-Forwarded-For": "not-an-ip"}, "172.18.0.3", True), "172.18.0.3")
        self.assertEqual(front.caller_address(headers, "192.168.1.30", public=False), "192.168.1.30",
                         "on the LAN port the client cannot choose its address")

    def test_forwarded_proto(self):
        self.assertEqual(front.forwarded_proto({"X-Forwarded-Proto": "https"}, public=False), "http")
        self.assertEqual(front.forwarded_proto({}, public=True), "https")
        self.assertEqual(front.forwarded_proto({"X-Forwarded-Proto": "http"}, public=True), "http")
        self.assertEqual(front.forwarded_proto({"X-Forwarded-Proto": "gopher"}, public=True), "https")


class FlasherFilesTests(unittest.TestCase):
    """/flash/ is the web flasher (packaging/flash/) and /flash/firmware/ the firmware the supervisor chose."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.page, self.firmware = root / "flash", root / "data" / "firmware"
        (self.page / "vendor").mkdir(parents=True)
        self.firmware.mkdir(parents=True)
        (self.page / "index.html").write_text("<!doctype html>刷机", encoding="utf-8")
        (self.page / "vendor" / "bundle.js").write_text("// esptool", encoding="utf-8")
        (self.firmware / "manifest.json").write_text("{}", encoding="utf-8")
        (self.firmware / "SHA256SUMS").write_text("x  a.bin\n", encoding="utf-8")
        (self.firmware / "a.bin").write_bytes(b"\xe9" * 64)
        (root / "data" / "secret.txt").write_text("no", encoding="utf-8")
        options = front.Options(relay_port=1, listen={}, flasher_dir=self.page, firmware_dir=self.firmware)
        self.servers = [front.make_server("127.0.0.1", 0, options),
                        front.make_server("127.0.0.1", 0, options, public=True)]
        for server in self.servers:
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def get(self, path, server=0):
        connection = http.client.HTTPConnection("127.0.0.1", self.servers[server].server_address[1], timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type"), body

    def test_page_scripts_and_firmware(self):
        for server in (0, 1):
            status, kind, body = self.get("/flash/", server)
            self.assertEqual((status, kind), (200, "text/html; charset=utf-8"))
            self.assertIn("刷机".encode(), body)
        self.assertEqual(self.get("/flash/vendor/bundle.js")[1], "text/javascript; charset=utf-8")
        self.assertEqual(self.get("/flash/firmware/manifest.json")[:2], (200, "application/json"))
        self.assertEqual(self.get("/flash/firmware/SHA256SUMS")[1], "text/plain; charset=utf-8")
        self.assertEqual(self.get("/flash/firmware/a.bin")[:2], (200, "application/octet-stream"))

    def test_nothing_outside_the_two_folders(self):
        for path in ("/flash/../data/secret.txt", "/flash/%2e%2e/data/secret.txt", "/flash/firmware/../secret.txt",
                     "/flash/firmware/%2e%2e%2fsecret.txt", "/flash/.hidden", "/flash/missing.js",
                     "/flash/firmware/sub/a.bin"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_without_firmware_the_page_still_loads(self):
        self.assertIsNone(front.static_file(None, "manifest.json"))
        self.assertEqual(front.content_type(Path("x.bin")), "application/octet-stream")


class _Echo(BaseHTTPRequestHandler):
    """Stands in for the relay: answers with the request it got."""
    protocol_version = "HTTP/1.1"

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        data = json.dumps({"method": self.command, "path": self.path, "headers": dict(self.headers),
                           "body": body.decode()}).encode()
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            for n in range(3):
                self.wfile.write(f"part{n}\n".encode())
            return
        self.send_response(200 if not self.path.startswith("/r/") else 302)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "tvs=abc; HttpOnly; SameSite=Lax; Path=/tv/")
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _answer

    def log_message(self, *_args):
        pass


class FrontServerTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        self.tmp = tempfile.TemporaryDirectory()
        self.status = Path(self.tmp.name) / "status.json"
        options = front.Options(relay_port=self.upstream.server_address[1], listen={},
                                status_file=self.status)
        self.lan = front.make_server("127.0.0.1", 0, options)
        self.public = front.make_server("127.0.0.1", 0, options, public=True)
        for server in (self.upstream, self.lan, self.public):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        for server in (self.upstream, self.lan, self.public):
            server.shutdown()
            server.server_close()
        self.tmp.cleanup()

    def call(self, server, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response, raw

    def test_lan_headers_are_the_fronts_own(self):
        response, raw = self.call(self.lan, "POST", "/tv/api/x?y=1",
                                  {"X-Real-IP": "6.6.6.6", "X-Forwarded-Proto": "https", "X-Key": "k",
                                   "Host": "192.168.1.20:8080", "Content-Type": "application/json"}, b'{"a":1}')
        seen = json.loads(raw)
        self.assertEqual((seen["method"], seen["path"], seen["body"]), ("POST", "/api/x?y=1", '{"a":1}'))
        self.assertEqual(seen["headers"]["X-Real-IP"], "127.0.0.1", "a client cannot pick its own address")
        self.assertEqual(seen["headers"]["X-Forwarded-Proto"], "http")
        self.assertEqual(seen["headers"]["Host"], "192.168.1.20:8080", "the host is kept for the origin check")
        self.assertEqual(seen["headers"]["X-Key"], "k")
        self.assertEqual(response.getheader("Set-Cookie"), "tvs=abc; HttpOnly; SameSite=Lax; Path=/tv/")

    def test_public_port_uses_the_tunnels_address_and_drops_the_key(self):
        _, raw = self.call(self.public, "GET", "/tv/api/state",
                           {"CF-Connecting-IP": "203.0.113.9", "X-Key": "k", "X-Forwarded-Proto": "https"})
        seen = json.loads(raw)
        self.assertEqual(seen["headers"]["X-Real-IP"], "203.0.113.9")
        self.assertEqual(seen["headers"]["X-Forwarded-Proto"], "https")
        self.assertNotIn("X-Key", seen["headers"])
        self.assertNotIn("CF-Connecting-IP", seen["headers"])

    def test_refused_on_public_served_on_lan(self):
        for path in ("/tv/admin", "/tv/setup", "/tv/?k=secret", "/tv/api/admin/users"):
            self.assertEqual(self.call(self.public, "GET", path)[0].status, 404, path)
            self.assertEqual(self.call(self.lan, "GET", path)[0].status, 200, path)

    def test_redirects_and_streams(self):
        response, _ = self.call(self.lan, "GET", "/")
        self.assertEqual((response.status, response.getheader("Location")), (302, "/tv/"))
        response, raw = self.call(self.lan, "GET", "/tv/stream")
        self.assertEqual(raw, b"part0\npart1\npart2\n", "an answer without a length is passed through to the end")

    def test_body_limits(self):
        response, _ = self.call(self.lan, "POST", "/tv/api/x", {"Content-Length": str(front.BODY_MAX + 1)}, b"")
        self.assertEqual(response.status, 413)
        # Saving the channel table may carry hundreds of new channels: it gets the relay's own larger limit.
        from server.relay import admin
        self.assertEqual(front.CHANNELS_BODY_MAX, admin.SELFHOST_CHANNELS_BODY_MAX)
        body = b"x" * (front.BODY_MAX + 1)
        response, _ = self.call(self.lan, "POST", "/tv/api/admin/channels", {"Content-Length": str(len(body))}, body)
        self.assertNotEqual(response.status, 413)
        response, _ = self.call(self.lan, "POST", "/tv/api/admin/channels",
                                {"Content-Length": str(front.CHANNELS_BODY_MAX + 1)}, b"")
        self.assertEqual(response.status, 413)

    def test_restore_upload_is_streamed_with_its_own_limit(self):
        # A settings backup (server/selfhost/backup.py) is far bigger than any JSON body: passed
        # through as it arrives, never read into memory first, with the relay's own cap.
        from server.selfhost import backup
        self.assertEqual(front.RESTORE_BODY_MAX, backup.UPLOAD_MAX)
        body = b"z" * (3 * front.CHUNK + 17)
        response, raw = self.call(self.lan, "POST", "/tv/api/admin/backup/restore/upload",
                                  {"Content-Length": str(len(body)), "Content-Type": "application/zip"}, body)
        seen = json.loads(raw)
        self.assertEqual((response.status, len(seen["body"]), seen["headers"]["Content-Length"]),
                         (200, len(body), str(len(body))))
        response, _ = self.call(self.lan, "POST", "/tv/api/admin/backup/restore/upload",
                                {"Content-Length": str(front.RESTORE_BODY_MAX + 1)}, b"")
        self.assertEqual(response.status, 413)
        self.assertEqual(self.call(self.public, "POST", "/tv/api/admin/backup/restore/upload",
                                   {"Content-Length": "4"}, b"abcd")[0].status, 404, "never over the tunnel")

    def test_health(self):
        response, raw = self.call(self.lan, "GET", "/healthz")
        self.assertEqual(response.status, 503, "no status from the supervisor yet")
        self.status.write_text(json.dumps({"at": time.time(), "children": {"relay": True, "xingzhi": True}}))
        response, raw = self.call(self.lan, "GET", "/healthz")
        self.assertEqual((response.status, json.loads(raw)["ok"]), (200, True))
        self.status.write_text(json.dumps({"at": time.time(), "children": {"relay": True, "xingzhi": False}}))
        self.assertEqual(self.call(self.lan, "GET", "/healthz")[0].status, 503)
        self.status.write_text(json.dumps({"at": time.time() - 600, "children": {"relay": True}}))
        self.assertEqual(self.call(self.lan, "GET", "/healthz")[0].status, 503, "a stale report is not health")

    def test_upstream_down_is_502(self):
        options = front.Options(relay_port=1, listen={})
        down = front.make_server("127.0.0.1", 0, options)
        threading.Thread(target=down.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(down.server_close)
        self.addCleanup(down.shutdown)
        self.assertEqual(self.call(down, "GET", "/tv/")[0].status, 502)

    def test_help_page(self):
        if not front.HELP_PAGE.is_file():
            self.skipTest("no help page in this tree")
        response, raw = self.call(self.lan, "GET", "/TV/")
        self.assertEqual(response.status, 200)
        self.assertIn("小鼠电视".encode(), raw)


if __name__ == "__main__":
    unittest.main()
