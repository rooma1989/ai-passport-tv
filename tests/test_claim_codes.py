"""Bind (claim) codes: 6 digits on a self-hosted server, 20 characters on the cloud relay.

- A self-hosted database (meta.selfhost = 1, written by the supervisor and by
  `relay --selfhost`) hands out 6-digit claim codes, unique among the stored ones;
  the cloud database keeps the 20-character codes.
- A device that is still unbound with a long code from before gets a 6-digit one
  the next time the media server admits it, so its screen and QR show the short code.
- Typed or scanned codes on a self-hosted relay go through a wrong-code limit
  (server/tenancy/claimguard.py): 5 per IP and 30 overall per 10 minutes, then a
  pause with a Chinese message, one audit row, and new codes for every unbound
  device once the overall limit is hit. The cloud relay is unchanged.
- The pages accept both formats and switch the field to a number pad on a
  self-hosted relay.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import setup, web
from server.tenancy import claimguard, db, gate, unbound
from server.tenancy.accounts import SELFHOST_META, Accounts
from server.tenancy.manage import AdminOps

from test_relay_web import KEY, FakeClient

SECRET = "S" * 43
DEVICE = "0123456789ab"
SHORT = re.compile(r"^[0-9]{6}$")
LONG = re.compile(r"^[A-Z2-7]{20}$")


class CodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = Accounts(Path(self.tmp.name) / "tv.db")

    def tearDown(self):
        self.accounts.close()
        self.tmp.cleanup()

    def test_cloud_database_keeps_20_characters(self):
        self.assertFalse(self.accounts.short_claims())
        row = self.accounts.register_device(DEVICE, "ai-passport", SECRET)
        self.assertRegex(row["claim_code"], LONG)
        self.assertRegex(row["remote_code"], LONG)

    def test_self_hosted_database_uses_6_digits_for_claims_only(self):
        self.accounts.set_meta(SELFHOST_META, "1")
        row = self.accounts.register_device(DEVICE, "ai-passport", SECRET)
        self.assertRegex(row["claim_code"], SHORT)
        self.assertRegex(row["remote_code"], LONG, "guest codes stay 20 characters")
        admin = self.accounts.create_user("管理员", "admin")
        self.assertRegex(self.accounts.create_invite(admin), LONG, "invites stay 20 characters")
        user = self.accounts.create_user("Alice")
        self.accounts.bind(row["claim_code"], user)
        self.accounts.unbind(DEVICE, user)
        self.assertRegex(self.accounts.device(DEVICE)["claim_code"], SHORT, "unbinding gives a short code")
        self.accounts.bind(self.accounts.device(DEVICE)["claim_code"], user)
        AdminOps(self.accounts).delete_user(user, admin)
        self.assertRegex(self.accounts.device(DEVICE)["claim_code"], SHORT, "deleting the owner too")

    def test_short_codes_retry_until_unused(self):
        self.accounts.set_meta(SELFHOST_META, "1")
        taken = self.accounts.register_device(DEVICE, "ai-passport", SECRET)["claim_code"]
        draws = iter([taken, taken, "000042"])
        self.assertEqual(db.new_short_claim(self.accounts.conn, draw=lambda: next(draws)), "000042")
        # Every draw taken (cannot happen with a household's few devices): a long code, still unique.
        self.assertRegex(db.new_short_claim(self.accounts.conn, draw=lambda: taken, tries=3), LONG)

    def test_is_short_claim(self):
        for good in ("000000", "123456"):
            self.assertTrue(db.is_short_claim(good))
        for bad in ("", None, "12345", "1234567", "12345A", "１２３４５６", "A" * 20):
            self.assertFalse(db.is_short_claim(bad), bad)

    def test_rotation_is_self_hosted_only(self):
        row = self.accounts.register_device(DEVICE, "ai-passport", SECRET)
        self.assertEqual(self.accounts.rotate_claims(), [])
        self.assertEqual(self.accounts.device(DEVICE)["claim_code"], row["claim_code"])
        self.accounts.set_meta(SELFHOST_META, "1")
        before = self.accounts.device(DEVICE)["claim_code"]
        self.assertEqual(self.accounts.rotate_claims(), [DEVICE])
        after = self.accounts.device(DEVICE)["claim_code"]
        self.assertRegex(after, SHORT)
        self.assertNotEqual(after, before)


class AdmitTests(unittest.TestCase):
    """The owner's Passport: unbound, still holding a 20-character code from before the upgrade."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.gate = gate.Gate(root / "tv.db", root / "devices", logger=lambda _m: None)
        self.accounts = self.gate.accounts
        self.accounts.set_meta("public_url", "http://192.168.1.5:8080")
        self.old = self.accounts.register_device(DEVICE, "ai-passport", SECRET)["claim_code"]
        self.assertRegex(self.old, LONG)

    def tearDown(self):
        self.accounts.close()
        self.tmp.cleanup()

    def admit(self):
        return self.gate.admit({"version": 1, "device_id": DEVICE, "device_secret": SECRET}, None, 8097,
                               "192.168.1.20")

    def test_cloud_admission_keeps_the_long_code(self):
        admission = self.admit()
        self.assertEqual(admission.claim, self.old)
        self.assertNotIn("device.claim_renewed", [r["action"] for r in self.accounts.audit_rows()])

    def test_self_hosted_admission_renews_a_long_code_once(self):
        self.accounts.set_meta(SELFHOST_META, "1")
        admission = self.admit()
        self.assertRegex(admission.claim, SHORT)
        self.assertEqual(self.accounts.device(DEVICE)["claim_code"], admission.claim)
        self.assertEqual(admission.claim_url, f"HTTP://192.168.1.5:8080/TV/B/{admission.claim}")
        self.assertEqual(unbound.picture_lines(admission.claim),
                         ["BIND THIS TV", f"{admission.claim[:3]} {admission.claim[3:]}"])
        self.assertIsNone(self.accounts.device_by_claim(self.old), "the long code no longer binds")
        again = self.admit()
        self.assertEqual(again.claim, admission.claim, "a short code is kept across reconnects")
        actions = [r["action"] for r in self.accounts.audit_rows()]
        self.assertEqual(actions.count("device.claim_renewed"), 1)

    def test_bound_devices_are_untouched(self):
        self.accounts.set_meta(SELFHOST_META, "1")
        user = self.accounts.create_user("Alice")
        self.accounts.bind(self.old, user)
        self.assertTrue(self.admit().bound)
        self.assertIsNone(self.accounts.device(DEVICE)["claim_code"])

    def test_a_rotated_code_ends_the_bind_picture(self):
        self.accounts.set_meta(SELFHOST_META, "1")
        admission = self.admit()
        self.assertFalse(self.gate.claim_changed(DEVICE, admission.claim))
        self.accounts.rotate_claims()
        self.assertTrue(self.gate.claim_changed(DEVICE, admission.claim))

    def test_picture_lines_draw_a_short_code(self):
        lines = unbound.picture_lines("042917")
        self.assertEqual(lines, ["BIND THIS TV", "042 917"])
        from server import overlay
        overlay.lines_frame(lines)      # every glyph exists


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RelayCase(unittest.TestCase):
    selfhost = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.accounts = Accounts(root / "tv.db", clock=lambda: self.now)
        if self.selfhost:
            self.accounts.set_meta(SELFHOST_META, "1")
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.alice_cookie = f"{web.COOKIE}={self.accounts.new_session(self.alice)}"
        self.claim = self.accounts.register_device(DEVICE, "ai-passport", SECRET)["claim_code"]
        main = root / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://x/1.m3u8\n", encoding="utf-8")
        self.logs = []
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, main_channels=main,
                               accounts=self.accounts, devices_dir=root / "devices", log=self.logs.append,
                               clock=lambda: self.now,
                               selfhost=setup.SelfHost() if self.selfhost else None)
        self.relay.sync_devices(force=True)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        self.streams.server_close()
        threading.Thread(target=self.manage.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        self.manage.shutdown()
        self.manage.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, cookie=None, ip="192.168.1.20"):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.manage.server_address[1]}{path}",
                                         data=data, method=method)
        request.add_header("X-Real-IP", ip)
        if cookie:
            request.add_header("Cookie", cookie)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as answer:
                status, payload = answer.status, answer.read()
        except urllib.error.HTTPError as error:
            status, payload = error.code, error.read()
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, payload

    def actions(self):
        return [r["action"] for r in self.accounts.audit_rows(500)]

    def wrong(self, ip="192.168.1.20"):
        guess = "000000" if self.claim != "000000" else "000001"
        return self.call("POST", "/api/bind", {"claim": guess}, self.alice_cookie, ip)[1]


class SelfHostedRelayTests(RelayCase):
    def test_short_code_routes(self):
        self.assertRegex(self.claim, SHORT)
        status, page = self.call("GET", f"/b/{self.claim}")
        self.assertEqual(status, 200)
        self.assertIn(b"api/bind", page)
        self.assertEqual(self.call("GET", f"/api/bind/{self.claim}")[1],
                         {"ok": True, "model": "ai-passport", "tail": "89AB", "register": True})
        self.assertEqual(self.call("GET", f"/api/bind/{self.claim}", cookie=self.alice_cookie)[1],
                         {"ok": True, "model": "ai-passport", "tail": "89AB"})
        self.assertEqual(self.call("GET", "/b/12345")[0], 404)
        self.assertEqual(self.call("GET", "/r/123456")[0], 404, "guest codes are still 20 characters")

    def test_typed_code_with_spaces_binds(self):
        spaced = f"{self.claim[:3]} {self.claim[3:]}"
        answer = self.call("POST", "/api/bind", {"claim": spaced}, self.alice_cookie)[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.accounts.owner(DEVICE), self.alice)

    def test_state_says_digits(self):
        state = self.call("GET", "/api/state", cookie=self.alice_cookie)[1]
        self.assertEqual(state["claim_digits"], 6)

    def test_five_wrong_codes_per_ip_then_a_pause(self):
        for _ in range(5):
            answer = self.wrong()
            self.assertFalse(answer["ok"])
            self.assertNotIn("limited", answer)
        # Even the right code is refused now, on every claim endpoint, from this IP.
        blocked = self.call("POST", "/api/bind", {"claim": self.claim}, self.alice_cookie)[1]
        self.assertTrue(blocked.get("limited"), blocked)
        self.assertIn("10 分钟", blocked["error"])
        self.assertTrue(self.call("GET", f"/api/bind/{self.claim}")[1].get("limited"))
        self.assertTrue(self.call("POST", "/api/bind/register", {"claim": self.claim, "name": "x"})[1].get("limited"))
        self.assertTrue(self.call("GET", f"/api/bind/{self.claim}", cookie=self.alice_cookie)[1].get("limited"))
        self.assertIsNone(self.accounts.owner(DEVICE))
        locks = [r["detail"] for r in self.accounts.audit_rows(500) if r["action"] == "bind.locked"]
        self.assertEqual(locks, ["ip"], "audited once, not on every refused try")
        # Another phone on another address is not affected.
        other = self.call("GET", f"/api/bind/{self.claim}", ip="192.168.1.21")[1]
        self.assertTrue(other["ok"], other)
        # Ten minutes later the window has passed.
        self.now += claimguard.IP_LIMIT[1]
        answer = self.call("POST", "/api/bind", {"claim": self.claim}, self.alice_cookie)[1]
        self.assertTrue(answer["ok"], answer)

    def test_wrong_codes_on_every_endpoint_count(self):
        self.call("GET", "/api/bind/000000" if self.claim != "000000" else "/api/bind/000001")
        self.call("POST", "/api/bind/register", {"claim": "999999" if self.claim != "999999" else "999998"})
        self.call("GET", "/api/bind/" + "A" * 20, cookie=self.alice_cookie)
        self.wrong()
        self.wrong()
        self.assertTrue(self.call("GET", f"/api/bind/{self.claim}")[1].get("limited"))

    def test_thirty_wrong_overall_pause_everyone_and_rotate_codes(self):
        for n in range(30):
            self.wrong(ip=f"10.0.{n}.1")
        old = self.claim
        new = self.accounts.device(DEVICE)["claim_code"]
        self.assertRegex(new, SHORT)
        self.assertNotEqual(new, old, "every guessed code is void")
        locks = [r for r in self.accounts.audit_rows(500) if r["action"] == "bind.locked"]
        self.assertEqual([r["detail"] for r in locks], ["all"])
        self.assertIn("device.claim_rotated", self.actions())
        blocked = self.call("GET", f"/api/bind/{new}", ip="172.16.0.9")[1]
        self.assertTrue(blocked.get("limited"))
        self.assertIn("暂停", blocked["error"])
        self.now += claimguard.ALL_LIMIT[1]
        self.assertTrue(self.call("GET", f"/api/bind/{new}", ip="172.16.0.9")[1]["ok"])


class CloudRelayTests(RelayCase):
    selfhost = False

    def test_cloud_codes_routes_and_no_limit(self):
        self.assertRegex(self.claim, LONG)
        self.assertEqual(self.call("GET", "/b/123456")[0], 404)
        self.assertEqual(self.call("GET", "/api/bind/123456")[0], 404)
        self.assertNotIn("claim_digits", self.call("GET", "/api/state", cookie=self.alice_cookie)[1])
        for _ in range(40):
            self.assertFalse(self.call("POST", "/api/bind", {"claim": "B" * 20}, self.alice_cookie)[1]["ok"])
        answer = self.call("POST", "/api/bind", {"claim": self.claim}, self.alice_cookie)[1]
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("bind.locked", self.actions())


class PageTests(unittest.TestCase):
    ASSETS = ROOT / "server" / "relay" / "assets"

    def test_typed_code_pages_accept_both_formats(self):
        # The console's script is console.js now; the classic console page keeps its own copy for this beta.
        for name in ("remote.html", "console.js", "console-classic.html"):
            page = (self.ASSETS / name).read_text(encoding="utf-8")
            match = re.search(r"const CLAIM_CODE = /(.+?)/;", page)
            self.assertIsNotNone(match, name)
            pattern = re.compile(match.group(1))
            for good in ("042917", "ABCDEFGHIJKLMNOPQRS7"):
                self.assertTrue(pattern.match(good), (name, good))
            for bad in ("04291", "0429170", "ABCDEFGHIJKLMNOPQRS8", "ABC"):
                self.assertFalse(pattern.match(bad), (name, bad))
            self.assertIn('input.inputMode = "numeric"', page, name)
            self.assertIn("data.claim_digits", page, name)
            self.assertIn("claimField();", page, name)

    def test_join_page_groups_a_short_code_like_the_screen(self):
        page = (self.ASSETS / "join.html").read_text(encoding="utf-8")
        self.assertIn('/^[0-9]{6}$/.test(text) ? text.slice(0, 3) + " " + text.slice(3)', page)


if __name__ == "__main__":
    unittest.main()
