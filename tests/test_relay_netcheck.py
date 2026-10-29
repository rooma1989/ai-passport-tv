"""The network check (server/relay/netcheck.py, Admin -> System -> 网络检查, /api/admin/netcheck).

Everything is fake: a fake DNS resolver (the CDN host answers a 198.18.x.x fake IP the way Clash's TUN
fake-ip mode does), a fake OpenList (storage list, /api/fs/list, /api/fs/link answering a signed CDN
URL, and the WebDAV the library reads with Range), fake HTTP servers for the live lines (playlist and
segment) and for the YouTube / Bilibili / Douban endpoints, a tiny CONNECT proxy for the YouTube proxy
path, and a Clash Verge config in a temp dir.

What is checked: Clash's rules: list is read and nothing else (secret, proxies); which rule a host
lands on and when the answer is unsure; fake-IP detection; the suggested DIRECT rules; a slow drive
behind fake-IP gets DOMAIN-SUFFIX,quark.cn,DIRECT while a slow drive without a proxy gets none and a
fast one is fine; live lines report their time and failures; any HTTP answer (even 403) counts as
reachable; the endpoint is admin-only and self-host only; the last result is kept; and no answer or
log line carries the CDN URL's signature, the admin password or the proxy's password.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import netcheck
from server.selfhost import openlist

import test_relay_clouddrive as cd_base
import test_relay_library as lib_base

CDN_HOST = "vod-cdn.quark.cn"
CDN_URL = f"https://{CDN_HOST}/f/movie.mp4?auth_key=SIGNED-SECRET-77&sp=1"
BIG = bytes(range(256)) * (8 * 1024)         # 2 MiB
DIRECT_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
CLASH_YAML = """\
mixed-port: 7897
secret: clash-secret-DO-NOT-READ
external-controller: 127.0.0.1:9097
proxies:
  - {name: "HK-01", server: hk.example.net, port: 443, password: node-SECRET}
dns:
  enhanced-mode: fake-ip
  fake-ip-range: 198.18.0.1/16
rules:
  # 国内直连
  - DOMAIN-SUFFIX,bilibili.com,DIRECT
  - 'DOMAIN,api.example.org,DIRECT'
  - "DOMAIN-KEYWORD,douban,DIRECT"  # 豆瓣
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - MATCH,PROXY
profile:
  store-selected: true
"""


def fake_resolver(table):
    def resolve(host):
        if host not in table:
            raise socket.gaierror("no such host")
        return list(table[host])
    return resolve


class RulesTests(unittest.TestCase):
    def test_only_the_rules_list_is_read(self):
        rules = netcheck.parse_rules(CLASH_YAML)
        self.assertEqual(rules, ["DOMAIN-SUFFIX,bilibili.com,DIRECT", "DOMAIN,api.example.org,DIRECT",
                                 "DOMAIN-KEYWORD,douban,DIRECT", "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve", "MATCH,PROXY"])
        self.assertNotIn("SECRET", json.dumps(rules))
        self.assertEqual(netcheck.parse_rules("rules: []\nfoo: 1\n"), [])
        self.assertEqual(netcheck.parse_rules("proxies:\n  - a\n"), [])

    def test_route_of(self):
        rules = netcheck.parse_rules(CLASH_YAML)
        self.assertEqual(netcheck.route_of(rules, "api.bilibili.com"),
                         {"policy": "DIRECT", "rule": "DOMAIN-SUFFIX,bilibili.com,DIRECT", "unsure": False})
        self.assertEqual(netcheck.route_of(rules, "movie.douban.com")["policy"], "DIRECT")
        self.assertEqual(netcheck.route_of(rules, "notbilibili.com")["rule"], "MATCH,PROXY")
        self.assertEqual(netcheck.route_of(rules, CDN_HOST), {"policy": "PROXY", "rule": "MATCH,PROXY", "unsure": False})
        self.assertEqual(netcheck.route_of(rules, "10.1.2.3")["policy"], "DIRECT")
        unsure = netcheck.route_of(["GEOSITE,cn,DIRECT", "MATCH,PROXY"], CDN_HOST)
        self.assertEqual((unsure["policy"], unsure["unsure"]), ("PROXY", True))
        self.assertEqual(netcheck.route_of([], CDN_HOST)["policy"], "")

    def test_load_rules_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(netcheck.load_rules(Path(tmp) / "missing.yaml"))
            self.assertIsNone(netcheck.load_rules(None))
            path = Path(tmp) / "clash-verge.yaml"
            path.write_text(CLASH_YAML, encoding="utf-8")
            self.assertEqual(len(netcheck.load_rules(path)), 5)

    def test_dns_and_suggested_rules(self):
        resolve = fake_resolver({CDN_HOST: ["198.18.0.7"], "real.example.cn": ["101.35.240.114", "::1"]})
        self.assertEqual(netcheck.dns_of(CDN_HOST, resolve), {"fake": True, "ip": False})
        self.assertEqual(netcheck.dns_of("real.example.cn", resolve), {"fake": False, "ip": False})
        self.assertEqual(netcheck.dns_of("gone.example", resolve), {"fake": None, "ip": False})
        self.assertEqual(netcheck.dns_of("198.19.255.1", resolve), {"fake": True, "ip": True})
        self.assertEqual(netcheck.dns_of("198.20.0.1", resolve), {"fake": False, "ip": True})
        self.assertEqual(netcheck.suffix_of("d.pcs.baidu.com"), "baidu.com")
        self.assertEqual(netcheck.suffix_of("a.b.example.com.cn"), "example.com.cn")
        self.assertEqual(netcheck.direct_rules([CDN_HOST, "allall01.baidupcs.com", "101.35.240.114"], ("quark.cn",)),
                         ["DOMAIN-SUFFIX,quark.cn,DIRECT", "DOMAIN-SUFFIX,baidupcs.com,DIRECT",
                          "IP-CIDR,101.35.240.114/32,DIRECT,no-resolve"])

    def test_proxy_descriptions_never_carry_passwords(self):
        env = {"https_proxy": "http://user:pw-SECRET@127.0.0.1:7897", "no_proxy": "localhost,.lan"}
        self.assertEqual(netcheck.env_proxy("https://vod.quark.cn/x", env), "127.0.0.1:7897")
        self.assertEqual(netcheck.env_proxy("https://nas.lan/x", env), "")
        self.assertEqual(netcheck.env_proxy("https://vod.quark.cn/x", {}), "")
        self.assertEqual(netcheck._hostport("socks5://u:p@10.0.0.2:1080"), "10.0.0.2:1080")


class DriveRowTests(unittest.TestCase):
    """The verdict for one drive, from a measured speed."""

    def check(self, resolve, slow=25.0, clash=None, env=lambda _u: ""):
        return netcheck.NetCheck(resolve=fake_resolver(resolve), env=env, slow_mbps=slow, clash_path=None), clash

    def test_slow_behind_fake_ip_suggests_direct_rules(self):
        nc, _ = self.check({CDN_HOST: ["198.18.3.4"]})
        row = nc._drive_row("夸克", "QuarkTV", CDN_HOST, {"bytes": 30 << 20, "first_ms": 420, "mbps": 11.5},
                            netcheck.parse_rules(CLASH_YAML))
        self.assertEqual(row["state"], "warn")
        self.assertEqual(row["rules"], ["DOMAIN-SUFFIX,quark.cn,DIRECT"])
        self.assertIn("11.5 Mbit/s", row["text"])
        self.assertTrue(any("MATCH,PROXY" in n for n in row["notes"]))
        self.assertTrue(any("假 IP" in n for n in row["notes"]))

    def test_slow_without_proxy_and_fast_with_proxy(self):
        nc, _ = self.check({CDN_HOST: ["47.1.2.3"]})
        row = nc._drive_row("夸克", "QuarkTV", CDN_HOST, {"bytes": 30 << 20, "first_ms": 90, "mbps": 8.0}, None)
        self.assertEqual((row["state"], row["rules"]), ("warn", []))
        self.assertIn("没经过代理软件", row["text"])
        nc, _ = self.check({CDN_HOST: ["198.18.3.4"]})
        row = nc._drive_row("夸克", "QuarkTV", CDN_HOST, {"bytes": 30 << 20, "first_ms": 90, "mbps": 65.0}, None)
        self.assertEqual((row["state"], row["rules"]), ("ok", []))

    def test_slow_but_already_direct(self):
        nc, _ = self.check({CDN_HOST: ["198.18.3.4"]})
        row = nc._drive_row("夸克", "QuarkTV", CDN_HOST, {"bytes": 30 << 20, "first_ms": 90, "mbps": 3.0},
                            ["DOMAIN-SUFFIX,quark.cn,DIRECT", "MATCH,PROXY"])
        self.assertEqual((row["state"], row["rules"]), ("warn", []))
        self.assertIn("已经让它直连", row["text"])

    def test_no_link_host_uses_the_drive_domain(self):
        nc, _ = self.check({"baidupcs.com": ["198.18.9.9"]})
        row = nc._drive_row("百度网盘", "BaiduNetdisk", "", {"bytes": 30 << 20, "first_ms": 90, "mbps": 2.0}, None)
        self.assertEqual(row["rules"], ["DOMAIN-SUFFIX,baidupcs.com,DIRECT"])

    def test_measure_reads_a_range_and_stops(self):
        class Answer:
            def __init__(self):
                self.left = 4 << 20

            def geturl(self):
                return "http://127.0.0.1:5244/dav/x.mp4"

            def read1(self, n):
                n = min(n, self.left)
                self.left -= n
                return b"\0" * n

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        seen = []

        class Dav:
            def open(self, path, wanted, timeout=None):
                seen.append((path, wanted))
                return Answer()

        ticks = iter(i * 0.01 for i in range(100000))
        found = netcheck.measure(Dav(), "夸克/a.mp4", 100 << 20, seconds=5.0, cap=3 << 20, monotonic=lambda: next(ticks))
        self.assertEqual(seen, [("夸克/a.mp4", f"bytes={25 << 20}-{(25 << 20) + (3 << 20) - 1}")])
        self.assertEqual(found["bytes"], 3 << 20)
        self.assertEqual(found["host"], "")
        self.assertGreater(found["mbps"], 0)


class Origin(BaseHTTPRequestHandler):
    """Live lines and the public endpoints, all on «localhost»."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _send(self, code, body=b"", kind="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.endswith(".m3u8") and "/dead/" not in path:
            self._send(200, b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg1.ts\n", "application/vnd.apple.mpegurl")
        elif path.endswith(".ts"):
            self._send(206 if self.headers.get("Range") else 200, b"\x47" * 4096, "video/mp2t")
        elif path == "/generate_204":
            self._send(204)
        elif path == "/nav":
            self._send(200, b'{"code":-101}', "application/json")
        elif path == "/favicon.ico":
            self._send(403, b"forbidden")
        else:
            self._send(404, b"no")


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


class ConnectProxy(BaseHTTPRequestHandler):
    """An HTTP CONNECT proxy that wants a password (like a YouTube proxy in the settings)."""
    seen: list = []

    def log_message(self, *_args):
        pass

    def do_CONNECT(self):
        self.seen.append((self.path, self.headers.get("Proxy-Authorization") or ""))
        host, _, port = self.path.rpartition(":")
        upstream = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
        self.send_response(200, "Connection established")
        self.end_headers()
        self.wfile.flush()
        client = self.connection

        def pump(a, b):
            try:
                while chunk := a.recv(65536):
                    b.sendall(chunk)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        other = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        other.start()
        pump(client, upstream)
        other.join(5)
        upstream.close()
        self.close_connection = True


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.origin = serve(Origin)
        self.base = f"http://localhost:{self.origin.server_address[1]}"

    def tearDown(self):
        self.origin.shutdown()
        self.origin.server_close()

    def test_any_http_answer_counts_and_errors_read_plainly(self):
        self.assertEqual(netcheck.fetch_status(self.base + "/generate_204", opener=DIRECT_OPEN), 204)
        self.assertEqual(netcheck.fetch_status(self.base + "/favicon.ico", opener=DIRECT_OPEN), 403)
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        with self.assertRaises(OSError) as caught:
            netcheck.fetch_status(f"http://127.0.0.1:{port}/", opener=DIRECT_OPEN, timeout=2)
        self.assertEqual(netcheck._web_error(caught.exception), "连接被拒绝")

    def test_through_the_youtube_proxy(self):
        handler = type("P", (ConnectProxy,), {"seen": []})
        proxy = serve(handler)
        try:
            status = netcheck.fetch_status(self.base + "/generate_204",
                                           f"http://yt:proxy-SECRET@127.0.0.1:{proxy.server_address[1]}")
        finally:
            proxy.shutdown()
            proxy.server_close()
        self.assertEqual(status, 204)
        self.assertEqual(handler.seen[0][0], f"localhost:{self.origin.server_address[1]}")
        self.assertTrue(handler.seen[0][1].startswith("Basic "))


class LinkOpenList(cd_base.FakeOpenList):
    """The fake OpenList plus /api/fs/list over its files and /api/fs/link answering a signed CDN URL."""

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in ("/api/fs/list", "/api/fs/link"):
            super().do_POST()
            return
        body = self._body()
        self.state["calls"].append(("POST", path))
        if not self._authorized():
            self._json({"code": 401, "message": "token is invalidated", "data": None})
            return
        wanted = body.get("path", "").strip("/")
        if path == "/api/fs/link":
            if wanted in self.files:
                self._json({"code": 200, "data": {"url": CDN_URL, "header": {"Cookie": ["c-SECRET"]}}})
            else:
                self._json({"code": 500, "message": "object not found"})
            return
        kids = {}
        for name, data in self.files.items():
            if name.startswith(wanted + "/"):
                head = name[len(wanted) + 1:].split("/")[0]
                is_dir = "/" in name[len(wanted) + 1:]
                kids[head] = {"name": head, "size": 0 if is_dir else len(data), "is_dir": is_dir}
        self._json({"code": 200, "data": {"content": list(kids.values()), "total": len(kids)}})


class NetcheckApiTests(lib_base.ApiTests):
    """Through the relay's real HTTP port: drive, live, web and Clash sections, cached result, no secrets."""

    def setUp(self):
        super().setUp()
        self.data = Path(self.accounts.path).parent
        self.relay.selfhost_data = self.data
        self.relay.log = self.logs.append
        self.paths = openlist.Paths(self.data)
        self.paths.bin.mkdir(parents=True)
        (self.paths.bin / "openlist-v4.2.9").write_bytes(b"x")
        (self.paths.bin / "components.json").write_text(json.dumps({"openlist": {"version": "v4.2.9",
                                                                                 "file": "openlist-v4.2.9"}}))
        self.password, _ = openlist.ensure_password(self.paths)
        state = {"storages": {}, "tokens": set(), "n": 0, "calls": [], "created": [], "scanned": False,
                 "expired": False, "refreshed": []}
        state["storages"] = {
            1: {"id": 1, "mount_path": "/夸克", "driver": "QuarkTV", "status": "work", "disabled": False,
                "order": 0, "addition": json.dumps({"refresh_token": "rt-SECRET"})},
            2: {"id": 2, "mount_path": "/百度网盘", "driver": "BaiduNetdisk", "status": "failed to refresh token: expired",
                "disabled": False, "order": 1, "addition": "{}"}}
        self.state = state
        handler = type("OL", (LinkOpenList,), {"files": {"夸克/电影/Big.Movie.2024.mp4": BIG, "夸克/readme.txt": b"hi"},
                                               "redirect": set(), "seen": [], "user": "admin",
                                               "password": self.password, "state": state})
        self.ol = serve(handler)
        self.paths.status.parent.mkdir(parents=True, exist_ok=True)
        self.paths.status.write_text(json.dumps({"state": "running", "port": self.ol.server_address[1]}))
        self.origin = serve(Origin)
        base = f"http://localhost:{self.origin.server_address[1]}"
        self.relay.main_channels.write_text(
            f"ch000 | CCTV1 | {base}/live/cctv1.m3u8 | MyUA\n"
            f"ch001 | 湖南卫视 | http://127.0.0.1:{self.origin.server_address[1]}/live/hunan.m3u8\n"
            f"ch002 | 浙江卫视 | {base}/dead/zj.m3u8\n"
            f"ch003 | 某某频道 | {base}/live/other.m3u8\n", encoding="utf-8")
        clash = Path(self.tmp.name) / "clash-verge.yaml"
        clash.write_text(CLASH_YAML, encoding="utf-8")
        resolve = fake_resolver({CDN_HOST: ["198.18.0.7"], "localhost": ["198.18.0.9"]})
        targets = (("youtube", "YouTube", base + "/generate_204", ()),
                   ("bilibili", "B 站", base + "/nav", ("bilibili.com",)),
                   ("douban", "豆瓣", base + "/favicon.ico", ("douban.com",)))
        self.relay._netcheck = netcheck.NetCheck(resolve=resolve, opener=DIRECT_OPEN, proxies=lambda _u: "",
                                                 env=lambda _u: "", clash_path=clash, targets=targets,
                                                 drive_s=0.3, drive_cap=len(BIG), slow_mbps=1e9)

    def tearDown(self):
        for server in (self.ol, self.origin):
            server.shutdown()
            server.server_close()
        super().tearDown()

    def run_check(self):
        status, started = self.call("admin", "POST", "/tv/api/admin/netcheck", {})
        self.assertEqual(status, 200)
        answers = [started]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            answer = self.call("admin", "GET", "/tv/api/admin/netcheck")[1]
            answers.append(answer)
            if not answer["running"]:
                return answer, answers
            time.sleep(0.1)
        self.fail("the check did not finish")

    def test_whole_check(self):
        self.assertEqual(self.call("admin", "GET", "/tv/api/admin/netcheck")[1],
                         {"ok": True, "running": False, "step": "", "result": None})
        answer, answers = self.run_check()
        result = answer["result"]
        sections = {s["key"]: s["rows"] for s in result["sections"]}
        self.assertEqual(list(sections), ["drives", "live", "web", "clash"])
        quark, baidu = sections["drives"]
        self.assertEqual((quark["name"], quark["state"]), ("夸克", "warn"))
        self.assertEqual(quark["rules"], ["DOMAIN-SUFFIX,quark.cn,DIRECT"])
        self.assertTrue(any(CDN_HOST in n for n in quark["notes"]))
        self.assertTrue(any("MATCH,PROXY" in n for n in quark["notes"]))
        self.assertIn("Mbit/s", quark["text"])
        self.assertEqual((baidu["name"], baidu["state"]), ("百度网盘", "warn"))
        self.assertIn("登录过期", baidu["text"])
        # The largest video was read through the library's WebDAV path, with a Range.
        reads = [s for s in self.ol.RequestHandlerClass.seen if s[0] == "GET" and s[1].startswith("/dav/")]
        self.assertEqual([(p, r) for _m, p, _a, r in reads], [("/dav/夸克/电影/Big.Movie.2024.mp4", f"bytes=0-{len(BIG) - 1}")])
        live = sections["live"]
        self.assertEqual([r["name"] for r in live], ["CCTV1", "湖南卫视", "浙江卫视"])
        self.assertEqual([r["state"] for r in live], ["ok", "ok", "warn"])
        self.assertIn("毫秒", live[0]["text"])
        self.assertTrue(any("假 IP" in n for n in live[0]["notes"]))
        self.assertTrue(any("直接是 IP" in n for n in live[1]["notes"]))
        self.assertIn("404", live[2]["text"])
        web = sections["web"]
        self.assertEqual([(r["name"], r["state"]) for r in web], [("YouTube", "ok"), ("B 站", "ok"), ("豆瓣", "ok")])
        self.assertIn("403", web[2]["text"])
        self.assertEqual(sections["clash"][0]["state"], "ok")
        self.assertIn("5 条规则", sections["clash"][0]["text"])
        self.assertEqual(result["rules"], ["DOMAIN-SUFFIX,quark.cn,DIRECT"])
        self.assertTrue(result["advice"])
        self.assertEqual(result["warn"], 3)
        # Kept: asking again gives the same result.
        self.assertEqual(self.call("admin", "GET", "/tv/api/admin/netcheck")[1]["result"], result)
        self.assertTrue(any(line.startswith("网络检查：做完了") for line in self.logs))
        seen = json.dumps(answers, ensure_ascii=False) + "\n".join(self.logs)
        for secret in ("SIGNED-SECRET", "auth_key", "c-SECRET", "rt-SECRET", self.password, "clash-secret", "node-SECRET",
                       "/f/movie.mp4"):
            self.assertNotIn(secret, seen)

    def test_fast_drive_and_no_clash(self):
        self.relay._netcheck.slow_mbps = 0.001
        self.relay._netcheck.clash_path = Path(self.tmp.name) / "none.yaml"
        result = self.run_check()[0]["result"]
        sections = {s["key"]: s["rows"] for s in result["sections"]}
        self.assertEqual((sections["drives"][0]["state"], sections["drives"][0]["rules"]), ("ok", []))
        self.assertEqual(sections["clash"][0]["state"], "skip")
        self.assertEqual(result["rules"], [])
        self.assertEqual(result["advice"], "")

    def test_admin_only_and_self_host_only(self):
        for who in ("alice", "guest"):
            self.assertEqual(self.call(who, "GET", "/tv/api/admin/netcheck")[0], 404)
            self.assertEqual(self.call(who, "POST", "/tv/api/admin/netcheck", {})[0], 404)
        self.assertFalse(self.relay._netcheck.running)
        cloud = types.SimpleNamespace(selfhost=None)
        self.assertEqual(netcheck.view(cloud), {"ok": False, "error": netcheck.NOT_HERE})
        self.assertEqual(netcheck.start(cloud), {"ok": False, "error": netcheck.NOT_HERE})

    def test_component_not_running(self):
        self.paths.status.write_text(json.dumps({"state": "stopped"}))
        result = self.run_check()[0]["result"]
        drives = next(s for s in result["sections"] if s["key"] == "drives")["rows"]
        self.assertEqual([(r["state"], r["name"]) for r in drives], [("skip", "网盘组件")])


if __name__ == "__main__":
    unittest.main()
