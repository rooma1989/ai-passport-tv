"""The PC console and scan-to-sign-in (multi-user phase 2), over real sockets.

What is checked is the contract in the design
(docs/superpowers/specs/2026-10-12-multi-tenant-design.md, sections 5 and 8):
a login ticket is single-use, short-lived and rate-limited; only a signed-in
member (not a guest, not the key) can confirm it; the QR code alone cannot be
turned into a session; the PC session is HttpOnly/Secure/SameSite and listed for
revocation; the console endpoints keep every user to their own devices; and a
cross-site POST is refused.
"""

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import now_playing
from server.relay import console, events, web
from server.relay.state import State
from server.tenancy import db, files as device_files, tickets as login_tickets
from server.tenancy.accounts import Accounts

from test_relay_web import KEY, FakeClient, FakeYouTube

ALICE_DEVICE = "aaaaaaaaaaa1"
BOB_DEVICE = "bbbbbbbbbbb2"
SECRET = "s" * 43
ASSETS_DIR = Path(__file__).resolve().parents[1] / "server" / "relay" / "assets"
NODE = shutil.which("node")
# Runs the phone's scan.js (TVScan.ticketFrom) and the vendored jsQR in node, without a DOM.
# job: {"texts": [...]} -> the tickets; {"grids": [{"rows": ["0101..."], "scale": n}]} -> decoded text + ticket.
SCAN_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [scanPath, jsqrPath, jobPath] = process.argv.slice(2);
const box = {};
vm.runInNewContext(fs.readFileSync(scanPath, "utf8"), box);
vm.runInNewContext(fs.readFileSync(jsqrPath, "utf8"), box);
const job = JSON.parse(fs.readFileSync(jobPath, "utf8"));
const out = { tickets: (job.texts || []).map((t) => box.TVScan.ticketFrom(t)), decoded: [] };
for (const g of job.grids || []) {
  const n = g.rows.length, s = g.scale, w = n * s;
  const px = new Uint8ClampedArray(w * w * 4);
  for (let y = 0; y < w; y++) for (let x = 0; x < w; x++) {
    const dark = g.rows[Math.floor(y / s)][Math.floor(x / s)] === "1";
    const i = (y * w + x) * 4;      // the console's colours: #1c1915 on #efe5d0
    px.set(dark ? [0x1c, 0x19, 0x15, 255] : [0xef, 0xe5, 0xd0, 255], i);
  }
  const code = box.jsQR(px, w, w, { inversionAttempts: "dontInvert" });
  out.decoded.push(code ? [code.data, box.TVScan.ticketFrom(code.data)] : null);
}
console.log(JSON.stringify(out));
"""


def run_scan_js(job: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        harness, job_file = Path(tmp) / "harness.js", Path(tmp) / "job.json"
        harness.write_text(SCAN_HARNESS, encoding="utf-8")
        job_file.write_text(json.dumps(job), encoding="utf-8")
        done = subprocess.run([NODE, str(harness), str(ASSETS_DIR / "scan.js"), str(ASSETS_DIR / "jsqr.js"),
                               str(job_file)], capture_output=True, text=True, encoding="utf-8",
                               timeout=60, check=True)
    return json.loads(done.stdout)


def svg_grid(svg: str) -> list[str]:
    """The modules of qr.svg()'s picture (border included), as rows of '0'/'1'."""
    side = int(re.search(r'viewBox="0 0 (\d+) \1"', svg).group(1))
    rows = [[0] * side for _ in range(side)]
    for x, y, width in re.findall(r"M(\d+) (\d+)h(\d+)v1h-\d+z", svg):
        for dx in range(int(width)):
            rows[int(y)][int(x) + dx] = 1
    return ["".join(map(str, row)) for row in rows]


CHROME_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0 Safari/537.36")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.accounts = Accounts(root / "tv.db", clock=lambda: self.now)
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.bob = self.accounts.create_user("Bob")
        for device, user in ((ALICE_DEVICE, self.alice), (BOB_DEVICE, self.bob)):
            row = self.accounts.register_device(device, "xingzhi-cube-154", SECRET)
            self.accounts.bind(row["claim_code"], user)
        self.devices_dir = root / "devices"
        main = root / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | 湖南卫视 | https://x/2.m3u8\n", encoding="utf-8")
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, youtube=FakeYouTube(),
                               main_channels=main, public_url="https://example.com",
                               accounts=self.accounts, devices_dir=self.devices_dir,
                               log=lambda _m: None, clock=lambda: self.now)
        self.relay.login_tickets.wait_s = 0.2
        self.relay.sync_devices(force=True)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        self.streams.server_close()     # the console never streams
        threading.Thread(target=self.manage.serve_forever, daemon=True).start()
        self.cookies = {name: "tvs=" + self.accounts.new_session(uid) for name, uid in
                        (("admin", self.admin), ("alice", self.alice), ("bob", self.bob))}

    def tearDown(self):
        self.manage.shutdown()
        self.manage.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    # ---- helpers ----

    def raw(self, method, path, cookie=None, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.manage.server_address[1]}{path}",
                                         data=data, method=method)
        request.add_header("User-Agent", CHROME_MAC)
        if cookie:
            request.add_header("Cookie", cookie)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=10) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def call(self, who, method, path, body=None, headers=None):
        cookie = self.cookies.get(who, who)
        status, _, payload = self.raw(method, path, cookie, body if body is not None else
                                      ({} if method == "POST" else None), headers)
        return status, (json.loads(payload) if status == 200 else None)

    def direct_ticket(self):
        """A ticket including the id that otherwise only the QR picture carries."""
        return self.relay.login_tickets.create("203.0.113.9", CHROME_MAC)

    def poll(self, poll, cookie=None):
        status, headers, body = self.raw("POST", "/api/login/poll", cookie, {"poll": poll})
        self.assertEqual(status, 200)
        return json.loads(body), headers

    # ---- the ticket flow ----

    def test_scan_confirm_and_poll_gives_the_pc_a_session(self):
        status, _, body = self.raw("POST", "/api/login/ticket")
        answer = json.loads(body)
        self.assertTrue(answer["ok"])
        self.assertRegex(answer["code"], r"^[A-Z2-9]{3}-[A-Z2-9]{3}$")
        self.assertTrue(answer["svg"].startswith("<svg"))
        self.assertEqual(answer["ttl"], 120)
        self.assertNotIn("ticket", answer, "the QR's ticket id is not handed out separately")
        self.assertEqual(self.poll(answer["poll"])[0]["state"], "pending")

        # The phone types the code (any case, with or without the dash).
        info = self.call("alice", "GET", "/api/login/info?code=" + answer["code"].lower())[1]
        self.assertTrue(info["ok"])
        self.assertEqual((info["browser"], info["code"], info["name"]), ("Chrome · macOS", answer["code"], "Alice"))
        self.assertEqual(info["network"], "127.0.0.0/24")
        self.assertTrue(info["same_network"])
        self.assertTrue(self.call("alice", "POST", "/api/login/confirm", {"code": answer["code"]})[1]["ok"])

        result, headers = self.poll(answer["poll"])
        self.assertEqual(result["state"], "ok")
        cookie = headers["Set-Cookie"]
        for part in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/tv/", f"Max-Age={30 * 24 * 3600}"):
            self.assertIn(part, cookie)
        session = cookie.split(";")[0]
        state = self.call(session, "GET", "/api/state")[1]
        self.assertEqual(state["me"], {"name": "Alice", "role": "user"})
        self.assertEqual([d["id"] for d in state["devices"]], [ALICE_DEVICE])
        self.assertEqual(self.accounts.session(session[4:])["kind"], "pc")
        # One-time: the same poll secret never yields a second session.
        self.assertEqual(self.poll(answer["poll"])[0]["state"], "expired")
        self.assertIn("login.pc", [r["action"] for r in self.accounts.audit_rows()])

    def test_qr_ticket_confirms_but_cannot_be_polled(self):
        made = self.direct_ticket()
        link_page = self.raw("GET", f"/l/{made['ticket']}")
        self.assertEqual(link_page[0], 200)
        self.assertEqual(self.raw("GET", f"/TV/L/{made['ticket']}")[0], 200, "works without nginx too")
        # Without a session the scanner only learns the code to type into the home-screen app.
        info = json.loads(self.raw("GET", f"/api/login/info?t={made['ticket']}")[2])
        self.assertEqual((info["ok"], info["signed_in"], info["code"]), (True, False, made["code"]))
        # Someone who photographed the QR code cannot wait for the result with it.
        self.assertEqual(self.poll(made["ticket"])[0]["state"], "expired")
        self.assertTrue(self.call("alice", "POST", "/api/login/confirm", {"ticket": made["ticket"]})[1]["ok"])
        self.assertEqual(self.poll(made["ticket"])[0]["state"], "expired")
        self.assertEqual(self.poll(made["poll"])[0]["state"], "ok")
        self.assertFalse(self.call("bob", "POST", "/api/login/confirm", {"ticket": made["ticket"]})[1]["ok"],
                         "a used ticket cannot be confirmed again")

    def test_only_signed_in_members_confirm(self):
        made = self.direct_ticket()
        code = self.accounts.device(ALICE_DEVICE)["remote_code"]
        guest = self.raw("POST", "/api/scan", None, {"code": code})[1]["Set-Cookie"].split(";")[0]
        for who in (None, guest, "tvs=forged"):
            status, _, _ = self.raw("POST", "/api/login/confirm", who, {"ticket": made["ticket"]})
            self.assertEqual(status, 404, who)
            self.assertEqual(self.raw("GET", f"/api/login/info?code={made['code']}", who)[0], 404)
        status, _, _ = self.raw("POST", "/api/login/confirm", None, {"ticket": made["ticket"]}, {"X-Key": KEY})
        self.assertEqual(status, 404, "the key is for scripts, not for approving a PC")
        guest_info = json.loads(self.raw("GET", f"/api/login/info?t={made['ticket']}", guest)[2])
        self.assertTrue(guest_info["guest"])
        self.assertEqual(self.poll(made["poll"])[0]["state"], "pending")

    def test_deny_and_expiry(self):
        made = self.direct_ticket()
        self.assertTrue(self.call("alice", "POST", "/api/login/confirm",
                                  {"ticket": made["ticket"], "accept": False})[1]["ok"])
        self.assertEqual(self.poll(made["poll"])[0]["state"], "denied")
        late = self.direct_ticket()
        self.now += login_tickets.TICKET_S + 1
        self.assertFalse(json.loads(self.raw("GET", f"/api/login/info?t={late['ticket']}")[2])["ok"])
        self.assertFalse(self.call("alice", "POST", "/api/login/confirm", {"ticket": late["ticket"]})[1]["ok"])
        self.assertEqual(self.poll(late["poll"])[0]["state"], "expired")

    def test_long_poll_wakes_on_confirm(self):
        self.relay.login_tickets.wait_s = 5
        made = self.direct_ticket()
        results = []
        thread = threading.Thread(target=lambda: results.append(self.poll(made["poll"])[0]))
        began = time.monotonic()
        thread.start()
        time.sleep(0.3)
        self.assertTrue(self.call("alice", "POST", "/api/login/confirm", {"ticket": made["ticket"]})[1]["ok"])
        thread.join(5)
        self.assertEqual(results[0]["state"], "ok")
        self.assertLess(time.monotonic() - began, 3.0)

    def test_ticket_creation_is_rate_limited(self):
        limit = login_tickets.CREATE_LIMIT[0]
        for _ in range(limit):
            self.assertEqual(self.raw("POST", "/api/login/ticket")[0], 200)
        status, _, body = self.raw("POST", "/api/login/ticket")
        self.assertEqual(status, 429)
        self.assertFalse(json.loads(body)["ok"])
        self.now += login_tickets.CREATE_LIMIT[1] + 1
        self.assertEqual(self.raw("POST", "/api/login/ticket")[0], 200)

    def test_wrong_codes_are_rate_limited(self):
        made = self.direct_ticket()
        wrong = "ZZZ-ZZZ" if made["code"] != "ZZZ-ZZZ" else "YYY-YYY"
        for _ in range(login_tickets.GUESS_LIMIT[0]):
            self.assertFalse(self.call("alice", "GET", f"/api/login/info?code={wrong}")[1]["ok"])
        answer = self.call("alice", "POST", "/api/login/confirm", {"code": made["code"]})[1]
        self.assertFalse(answer["ok"])
        self.assertIn("太多次", answer["error"])
        self.assertEqual(self.poll(made["poll"])[0]["state"], "pending")

    # ---- sessions ----

    def pc_cookie(self, who):
        made = self.direct_ticket()
        self.assertTrue(self.call(who, "POST", "/api/login/confirm", {"ticket": made["ticket"]})[1]["ok"])
        return self.poll(made["poll"])[1]["Set-Cookie"].split(";")[0]

    def test_session_list_revoke_and_sign_out(self):
        pc = self.pc_cookie("alice")
        code = self.accounts.device(ALICE_DEVICE)["remote_code"]
        guest = self.raw("POST", "/api/scan", None, {"code": code})[1]["Set-Cookie"].split(";")[0]
        rows = self.call(pc, "GET", "/api/sessions")[1]["sessions"]
        self.assertEqual(sorted(r["kind"] for r in rows), ["guest", "pc", "phone"])
        self.assertEqual([r["kind"] for r in rows if r["current"]], ["pc"])
        self.assertTrue(all(len(r["id"]) == 16 and "token" not in json.dumps(r) for r in rows))
        guest_row = next(r for r in rows if r["kind"] == "guest")
        self.assertEqual(guest_row["device_id"], ALICE_DEVICE)
        # Bob cannot end Alice's sessions, and guests cannot list anything.
        self.assertFalse(self.call("bob", "POST", "/api/sessions/revoke", {"id": guest_row["id"]})[1]["ok"])
        self.assertEqual(self.call(guest, "GET", "/api/sessions")[0], 404)
        self.assertEqual(self.call(guest, "GET", "/api/state")[0], 200)
        self.assertTrue(self.call(pc, "POST", "/api/sessions/revoke", {"id": guest_row["id"]})[1]["ok"])
        self.assertEqual(self.call(guest, "GET", "/api/state")[0], 404)
        # Sign out the others: the phone goes, this PC stays.
        self.assertEqual(self.call(pc, "POST", "/api/sessions/revoke", {"others": True})[1]["count"], 1)
        self.assertEqual(self.call("alice", "GET", "/api/state")[0], 404)
        self.assertEqual(self.call(pc, "GET", "/api/state")[0], 200)
        status, headers, _ = self.raw("POST", "/api/logout", pc, {})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.call(pc, "GET", "/api/state")[0], 404)
        status = json.loads(self.raw("GET", "/api/login/status", pc)[2])
        self.assertFalse(status["signed_in"])

    @unittest.skipUnless(NODE, "node is not installed")
    def test_the_phones_scanner_reads_the_consoles_qr_code(self):
        """Scan to sign in the PC: the QR picture the console shows -> jsQR -> TVScan.ticketFrom -> confirm."""
        answer = json.loads(self.raw("POST", "/api/login/ticket")[2])
        grid = svg_grid(answer["svg"])
        result = run_scan_js({"grids": [{"rows": grid, "scale": 3}, {"rows": grid, "scale": 8}]})
        for decoded in result["decoded"]:
            self.assertIsNotNone(decoded, "jsQR found no QR code")
            text, ticket = decoded
            self.assertRegex(text, r"^HTTPS://EXAMPLE\.COM/TV/L/[A-Z2-7]{26}$")
            self.assertEqual(ticket, text.rsplit("/", 1)[1])
        ticket = result["decoded"][0][1]
        # What the phone does next: the confirm page /tv/l/<ticket>, then 确认登录.
        self.assertEqual(self.raw("GET", f"/tv/l/{ticket}")[0], 200)
        info = self.call("alice", "GET", f"/api/login/info?t={ticket}")[1]
        self.assertEqual((info["ok"], info["signed_in"], info["code"]), (True, True, answer["code"]))
        self.assertTrue(self.call("alice", "POST", "/api/login/confirm", {"ticket": ticket})[1]["ok"])
        self.assertEqual(self.poll(answer["poll"])[0]["state"], "ok")

    def test_scanner_scripts_are_served_on_demand(self):
        for path in ("/scan.js", "/tv/scan.js", "/jsqr.js", "/tv/jsqr.js?v=1"):
            status, headers, body = self.raw("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers.get_all("Cache-Control"), ["public, max-age=86400"], "cacheable")
            self.assertGreater(len(body), 1000)
        # The confirm page may load them (script-src 'self'), and nothing from anywhere else.
        csp = self.raw("GET", "/login")[1]["Content-Security-Policy"]
        self.assertIn("script-src 'self' 'unsafe-inline';", csp)
        self.assertNotRegex(csp, r"https?:|\*")

    # ---- the console's pages and endpoints ----

    def test_console_page_and_login_page(self):
        for path in ("/console", "/tv/console", "/login"):
            status, headers, body = self.raw("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertNotIn(b"https://", body.split(b"<script>")[0].replace(b"http://www.w3.org", b""),
                             "no external stylesheet or font")
        self.assertEqual(self.raw("GET", "/console/")[0], 302)
        status = json.loads(self.raw("GET", "/api/login/status", self.cookies["alice"])[2])
        self.assertEqual(status["me"], {"name": "Alice", "role": "user", "kind": "phone"})

    def test_console_endpoints_stay_within_the_users_devices(self):
        now_playing.write(device_files.device_files(self.devices_dir, BOB_DEVICE).now, "ch001", True, True,
                          now=self.now, since=self.now, device={"volume": 50, "brightness": 1, "steps": 5})
        self.assertEqual(self.call("alice", "GET", f"/api/state?device={BOB_DEVICE}")[0], 404)
        mine = self.call("alice", "GET", f"/api/state?device={ALICE_DEVICE}")[1]
        self.assertEqual([d["id"] for d in mine["devices"]], [ALICE_DEVICE])
        devices = self.call("alice", "GET", "/api/console/devices")[1]["devices"]
        self.assertEqual([d["id"] for d in devices], [ALICE_DEVICE])
        self.assertTrue(devices[0]["guest_link"].startswith("HTTPS://EXAMPLE.COM/TV/R/"))
        self.assertFalse(devices[0]["online"])
        bob = self.call("bob", "GET", "/api/console/devices")[1]["devices"][0]
        self.assertEqual((bob["online"], bob["channel"], bob["channel_name"]), (True, "ch001", "湖南卫视"))
        self.assertEqual(self.call("alice", "GET", "/api/invites")[0], 404)
        self.assertEqual(self.call("admin", "GET", "/api/invites")[1]["invites"], [])
        self.call("admin", "POST", "/api/invites", {"name": "妈妈"})
        self.assertEqual(self.call("admin", "GET", "/api/invites")[1]["invites"][0]["name"], "妈妈")
        self.assertTrue(self.call("alice", "POST", "/api/qr", {"text": "HTTPS://EXAMPLE.COM/TV/R/X"})[1]["svg"])
        code = self.accounts.device(ALICE_DEVICE)["remote_code"]
        guest = self.raw("POST", "/api/scan", None, {"code": code})[1]["Set-Cookie"].split(";")[0]
        for method, path in (("GET", "/api/console/devices"), ("POST", "/api/qr"), ("GET", "/api/invites")):
            self.assertEqual(self.call(guest, method, path, {"text": "x"} if method == "POST" else None)[0], 404)
        self.assertEqual(self.raw("GET", "/api/console/devices")[0], 404)

    def test_cross_site_posts_are_refused(self):
        now_playing.write(device_files.device_files(self.devices_dir, ALICE_DEVICE).now, "ch000", True, True,
                          now=self.now, since=self.now)
        events_file = device_files.device_files(self.devices_dir, ALICE_DEVICE).events
        port = self.manage.server_address[1]
        for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
            status, _ = self.call("alice", "POST", "/api/switch", {"channel": "ch001"}, headers)
            self.assertEqual(status, 404, headers)
            self.assertEqual(self.call("alice", "DELETE", "/api/history/bilibili/BV1", None, headers)[0], 404)
        self.assertIsNone(events.read_event(events_file))
        made = self.direct_ticket()
        status, _ = self.call("alice", "POST", "/api/login/confirm", {"ticket": made["ticket"]},
                              {"Origin": "https://evil.example"})
        self.assertEqual(status, 404, "another site cannot approve its own ticket with the victim's cookie")
        same = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
        self.assertTrue(self.call("alice", "POST", "/api/switch", {"channel": "ch001"}, same)[1]["ok"])
        self.assertEqual(events.read_event(events_file).channel, "ch001")

    def test_single_user_relay_has_no_console(self):
        legacy = web.Relay(State(Path(self.tmp.name) / "relay-state.json"), FakeClient(),
                           Path(self.tmp.name) / "relay-channels.txt", "/opt/ff", KEY, log=lambda _m: None)
        self.assertIsNone(legacy.login_tickets)
        streams, manage = web.make_servers(legacy, 0, 0)
        streams.server_close()
        threading.Thread(target=manage.serve_forever, daemon=True).start()
        try:
            for method, path in (("GET", "/console"), ("POST", "/api/login/ticket"), ("GET", "/login")):
                request = urllib.request.Request(f"http://127.0.0.1:{manage.server_address[1]}{path}",
                                                 data=b"{}" if method == "POST" else None, method=method)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 404, path)
        finally:
            manage.shutdown()
            manage.server_close()


class ScanAssetTests(unittest.TestCase):
    """The phone's 扫码登录电脑: what is loaded when, and the link it accepts."""

    def test_scripts_load_only_when_the_button_is_tapped(self):
        login = console.LOGIN_PAGE.decode("utf-8")
        for name, page in (("remote.html", web.PAGE), ("login.html", login)):
            self.assertIn("扫码登录电脑", page, name)
            self.assertIn('script.src = "/tv/scan.js?v=', page, name)
            self.assertNotRegex(page, r"<script[^>]+src", f"{name}: no script tag loads at page view")
            self.assertNotIn("jsqr.js", page.lower(), f"{name}: jsQR is scan.js's business")
        scan = (ASSETS_DIR / "scan.js").read_text(encoding="utf-8")
        self.assertIn('script.src = "/tv/jsqr.js?v=', scan)
        self.assertIn("BarcodeDetector", scan)
        for needle in ("visibilitychange", "pagehide", "playsinline", "track.stop()", "对准电脑屏幕上的二维码",
                       "这不是电脑登录码", "输入登录码"):
            self.assertIn(needle, scan)
        self.assertNotRegex(scan, r"https?://[A-Za-z0-9]", "no external host")

    def test_vendored_decoder_keeps_its_license(self):
        jsqr = (ASSETS_DIR / "jsqr.js").read_text(encoding="utf-8")
        self.assertTrue(jsqr.startswith("/*! jsQR 1.4.0"))
        self.assertIn("Apache License, Version 2.0", jsqr.split("*/", 1)[0])
        self.assertIn("Modified for ai-passport-tv", jsqr.split("*/", 1)[0])
        self.assertIn("Apache License", (ASSETS_DIR / "LICENSE.jsqr.txt").read_text(encoding="utf-8"))
        self.assertLess(len(jsqr), 100_000, "keep the on-demand decoder small")
        for needle in ("XMLHttpRequest", "fetch(", "importScripts", "eval("):
            self.assertNotIn(needle, jsqr)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_ticket_from_scanned_text(self):
        t = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:24] + "27"
        cases = {
            f"HTTPS://EXAMPLE.COM/TV/L/{t}": t,                  # what the console draws
            f"https://tv.example.com/tv/l/{t.lower()}": t,       # the other host, lower case
            f"https://example.com/l/{t}": t,                     # the relay's own path
            f"http://127.0.0.1:8100/TV/L/{t}/": t,               # local relay, trailing slash
            f"  HTTPS://EXAMPLE.COM/TV/L/{t}\n": t,
            f"HTTPS://EXAMPLE.COM/TV/R/{t}": "",                 # a device's guest link
            f"HTTPS://EXAMPLE.COM/TV/B/{t}": "",                 # a binding code
            f"HTTPS://EXAMPLE.COM/TV/L/{t}?x=1": "",
            f"HTTPS://EXAMPLE.COM/TV/L/{t[:-1]}": "",            # 25 characters
            f"HTTPS://EXAMPLE.COM/TV/L/{t[:-1]}1": "",           # not base32
            f"HTTPS://EXAMPLE.COM/x/TV/L/{t}": "",
            f"javascript://x/TV/L/{t}": "",
            f"TV/L/{t}": "",
            "K7P-4QX": "",
            "": "",
        }
        tickets = run_scan_js({"texts": list(cases)})["tickets"]
        self.assertEqual(dict(zip(cases, tickets)), cases)


class TicketUnitTests(unittest.TestCase):
    def test_codes_and_agents(self):
        self.assertEqual(login_tickets.normalize_code(" k7p-4qx "), "K7P4QX")
        self.assertEqual(login_tickets.normalize_code("K7P4Q"), "")
        self.assertEqual(login_tickets.show_code("K7P4QX"), "K7P-4QX")
        self.assertEqual(login_tickets.describe_agent(CHROME_MAC), "Chrome · macOS")
        self.assertEqual(login_tickets.describe_agent(
            "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/129.0 Safari/537.36 Edg/129.0"), "Edge · Windows")
        self.assertEqual(login_tickets.describe_agent(""), "浏览器")
        self.assertEqual(console.COOKIE, web.COOKIE)

    def test_same_origin(self):
        self.assertTrue(console.same_origin({}))
        self.assertTrue(console.same_origin({"Origin": "https://example.com", "Host": "example.com"}))
        self.assertTrue(console.same_origin({"Origin": "https://example.com:8443", "Host": "example.com"}))
        self.assertFalse(console.same_origin({"Origin": "https://evil.com", "Host": "example.com"}))
        self.assertFalse(console.same_origin({"Origin": "https://example.com.evil.com", "Host": "example.com"}))
        self.assertFalse(console.same_origin({"Sec-Fetch-Site": "same-site"}))

    def test_old_database_gains_the_poll_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE login_ticket (ticket_sha256 BLOB PRIMARY KEY, short_code TEXT NOT NULL, "
                        "created_at REAL NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL, user_id INTEGER, "
                        "pc_ip_prefix TEXT NOT NULL DEFAULT '', pc_user_agent TEXT NOT NULL DEFAULT '')")
            old.commit()
            old.close()
            conn = db.connect(path)
            names = {row["name"] for row in conn.execute("PRAGMA table_info(login_ticket)")}
            self.assertIn("poll_sha256", names)
            conn.close()
            db.connect(path).close()     # a second open is a no-op


if __name__ == "__main__":
    unittest.main()
