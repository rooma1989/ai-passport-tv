"""Getting into an account with nobody signed in: the device is the root of trust.

Three ways, all ending in an ordinary member phone session:

- self-registration: a phone that is not signed in opens an unbound device's bind
  link (/TV/B/<claim>) and creates an account that owns the device, no invite;
- device recovery: the device asks the media server (STATUS {"recover": 1}) for a
  one-time owner login link and shows it as a QR code; scanning it signs a phone in
  as the owner, and the owner's other sessions are told;
- the admin's `login-link` command, the last resort.

Checked: who can get what, single use, expiry, the rate limits, the audit trail,
and that no token reaches the database or the log.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.protocol import Kind, Packet, json_bytes, receive_packet
from server.relay import web
from server.tenancy import admin, db, phonelinks, recover, signup
from server.tenancy.accounts import Accounts
from server.tenancy.gate import Gate
from server.tv_server import AVServer

from test_relay_web import KEY, FakeClient, FakeYouTube

ALICE_DEVICE = "aaaaaaaaaaa1"
NEW_DEVICE = "cccccccccc03"
SECRET = "s" * 43
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 Version/26.0 "
          "Mobile/15E148 Safari/604.1")
CHROME_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0 Safari/537.36")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RelayCase(unittest.TestCase):
    """A multi-user relay on a real socket: Alice owns one device, a second one is unbound."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.accounts = Accounts(root / "tv.db", clock=lambda: self.now)
        self.accounts.set_meta("public_url", "https://example.com")
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        row = self.accounts.register_device(ALICE_DEVICE, "xingzhi-cube-154", SECRET)
        self.accounts.bind(row["claim_code"], self.alice)
        self.claim = self.accounts.register_device(NEW_DEVICE, "ai-passport", SECRET)["claim_code"]
        main = root / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://x/1.m3u8\n", encoding="utf-8")
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, youtube=FakeYouTube(),
                               main_channels=main, public_url="https://example.com",
                               accounts=self.accounts, devices_dir=root / "devices",
                               log=lambda _m: None, clock=lambda: self.now)
        self.relay.sync_devices(force=True)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        self.streams.server_close()
        threading.Thread(target=self.manage.serve_forever, daemon=True).start()

    def tearDown(self):
        self.manage.shutdown()
        self.manage.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def raw(self, method, path, cookie=None, body=None, agent=IPHONE, ip=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.manage.server_address[1]}{path}",
                                         data=data, method=method)
        request.add_header("User-Agent", agent)
        if cookie:
            request.add_header("Cookie", cookie)
        if ip:
            request.add_header("X-Real-IP", ip)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def json(self, method, path, cookie=None, body=None, **kwargs):
        status, headers, payload = self.raw(method, path, cookie, body, **kwargs)
        return status, headers, (json.loads(payload) if status == 200 else None)

    def actions(self):
        return [r["action"] for r in self.accounts.audit_rows(200)]


class SelfRegistrationTests(RelayCase):
    def test_a_phone_that_is_not_signed_in_creates_an_account_and_owns_the_device(self):
        status, headers, page = self.raw("GET", f"/TV/B/{self.claim}".replace("/TV/B/", "/b/"))
        self.assertEqual(status, 200)
        self.assertIn("创建账号并绑定这台设备".encode(), page)
        info = self.json("GET", f"/api/bind/{self.claim}")[2]
        self.assertEqual(info, {"ok": True, "model": "ai-passport", "tail": "CC03", "register": True})
        status, headers, answer = self.json("POST", "/api/bind/register", None,
                                            {"claim": self.claim.lower(), "name": "  小 明  "}, ip="198.51.100.7")
        self.assertTrue(answer["ok"], answer)
        self.assertRegex(answer["redirect"], r"^/tv/\?s=[\w-]{40,}&home=1$", "add-to-home hint on the remote")
        cookie = headers["Set-Cookie"]
        for part in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/tv/", f"Max-Age={180 * 24 * 3600}"):
            self.assertIn(part, cookie)
        token = cookie.split(";")[0][4:]
        session = self.accounts.session(token)
        self.assertEqual((session["kind"], session["role"], session["name"]), ("phone", "user", "小 明"))
        self.assertEqual(self.accounts.owner(NEW_DEVICE), session["user_id"])
        self.assertEqual(self.accounts.device(NEW_DEVICE)["status"], "active")
        self.assertIsNone(self.accounts.device(NEW_DEVICE)["claim_code"], "the claim is single use")
        state = self.json("GET", "/api/state", "tvs=" + token)[2]
        self.assertEqual([d["id"] for d in state["devices"]], [NEW_DEVICE])
        rows = self.accounts.session_list(session["user_id"], token)
        self.assertEqual(rows[0]["via"], "signup")
        self.assertIn("user.signup", self.actions())
        self.assertIn("device.bind", self.actions())
        # Single use: the same claim again, from another phone.
        again = self.json("POST", "/api/bind/register", None, {"claim": self.claim, "name": "x"})[2]
        self.assertFalse(again["ok"])
        self.assertFalse(self.json("GET", f"/api/bind/{self.claim}")[2]["ok"])

    def test_the_default_name_and_bad_requests(self):
        answer = self.json("POST", "/api/bind/register", None, {"claim": self.claim, "name": ""})[2]
        self.assertTrue(answer["ok"])
        self.assertEqual(self.accounts.user(self.accounts.owner(NEW_DEVICE))["name"], "我")
        for body in ({"claim": 1}, {"claim": self.claim, "name": 3}):
            self.assertEqual(self.raw("POST", "/api/bind/register", None, body)[0], 400, body)
        wrong = self.json("POST", "/api/bind/register", None, {"claim": "A" * 20})[2]
        self.assertEqual(wrong, {"ok": False, "error": "这个绑定码不对，或者设备已经绑定过了"})

    def test_signed_in_phones_bind_as_before_and_cannot_register(self):
        alice = "tvs=" + self.accounts.new_session(self.alice)
        info = self.json("GET", f"/api/bind/{self.claim}", alice)[2]
        self.assertEqual(info, {"ok": True, "model": "ai-passport", "tail": "CC03"}, "the existing confirm page")
        refused = self.json("POST", "/api/bind/register", alice, {"claim": self.claim})[2]
        self.assertFalse(refused["ok"])
        self.assertIsNone(self.accounts.owner(NEW_DEVICE))
        # A guest (scanned someone's remote code) may still create an account of their own.
        guest = "tvs=" + self.accounts.new_session(None, "guest", device_id=ALICE_DEVICE)
        self.assertTrue(self.json("GET", f"/api/bind/{self.claim}", guest)[2]["register"])
        self.assertTrue(self.json("POST", "/api/bind/register", guest, {"claim": self.claim})[2]["ok"])

    def test_account_creation_is_limited_per_network(self):
        for n in range(signup.NET_LIMIT[0]):
            claim = self.accounts.register_device(f"dddddddddd{n:02d}", "ai-passport", SECRET)["claim_code"]
            self.assertTrue(self.json("POST", "/api/bind/register", None, {"claim": claim},
                                      ip="198.51.100.7")[2]["ok"])
        claim = self.accounts.register_device("dddddddddd99", "ai-passport", SECRET)["claim_code"]
        blocked = self.json("POST", "/api/bind/register", None, {"claim": claim}, ip="198.51.100.200")[2]
        self.assertFalse(blocked["ok"])
        self.assertIn("一小时后", blocked["error"])
        self.assertIsNone(self.accounts.owner("dddddddddd99"), "the claim is not used up by a refusal")
        self.assertTrue(self.json("POST", "/api/bind/register", None, {"claim": claim},
                                  ip="203.0.113.5")[2]["ok"], "another network is not affected")
        self.now += 3601
        claim = self.accounts.register_device("dddddddddd98", "ai-passport", SECRET)["claim_code"]
        self.assertTrue(self.json("POST", "/api/bind/register", None, {"claim": claim}, ip="198.51.100.7")[2]["ok"])

    def test_wrong_claims_do_not_count(self):
        for _ in range(10):
            self.json("POST", "/api/bind/register", None, {"claim": "B" * 20}, ip="198.51.100.7")
        self.assertTrue(self.json("POST", "/api/bind/register", None, {"claim": self.claim},
                                  ip="198.51.100.7")[2]["ok"])


class PhoneLinkOriginTests(unittest.TestCase):
    """phonelinks.py's new creation paths, without HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.accounts = Accounts(Path(self.tmp.name) / "tv.db", clock=lambda: self.now)
        self.alice = self.accounts.create_user("Alice")
        row = self.accounts.register_device(ALICE_DEVICE, "xingzhi-cube-154", SECRET)
        self.accounts.bind(row["claim_code"], self.alice)
        self.accounts.register_device(NEW_DEVICE, "ai-passport", SECRET)
        self.links = phonelinks.PhoneLinks(self.accounts)

    def tearDown(self):
        self.accounts.close()
        self.tmp.cleanup()

    def test_only_a_bound_devices_owner(self):
        made = self.links.create_for_device(ALICE_DEVICE, "203.0.113.5")
        self.assertEqual((made["user_id"], made["ttl"]), (self.alice, 300))
        self.assertRegex(made["token"], r"^[A-Z2-7]{26}$")
        self.assertEqual(self.links.info(token=made["token"])["name"], "Alice")
        self.assertEqual(self.links.origin_of(token=made["token"]),
                         {"origin": "device", "device_id": ALICE_DEVICE, "device_name": "小智 AAA1"})
        for device in (NEW_DEVICE, "000000000000", ""):
            with self.assertRaises(phonelinks.NotBound):
                self.links.create_for_device(device)
        self.accounts.set_user_status(self.alice, "banned")
        with self.assertRaises(phonelinks.NotBound):
            self.links.create_for_device(ALICE_DEVICE)
        self.assertIsNone(self.links.consume(token=made["token"]), "a banned owner's link is dead")
        rows = [r for r in self.accounts.audit_rows() if r["action"] == "phone_link.device"]
        self.assertEqual([(r["actor_user"], r["target_id"], r["actor_ip_prefix"]) for r in rows],
                         [(self.alice, ALICE_DEVICE, "203.0.113.0/24")], "refusals are not links")

    def test_three_an_hour_per_device_and_the_newest_wins(self):
        tokens = [self.links.create_for_device(ALICE_DEVICE)["token"] for _ in range(3)]
        self.assertEqual([self.links.info(token=t) is not None for t in tokens], [False, False, True])
        with self.assertRaises(phonelinks.Limited):
            phonelinks.PhoneLinks(self.accounts).create_for_device(ALICE_DEVICE)   # kept in the database
        self.now += 3599
        with self.assertRaises(phonelinks.Limited):
            self.links.create_for_device(ALICE_DEVICE)
        self.now += 2
        self.assertTrue(self.links.create_for_device(ALICE_DEVICE)["token"])
        # Single use and five minutes.
        self.now += 3601
        token = self.links.create_for_device(ALICE_DEVICE)["token"]
        self.now += 299
        self.assertEqual(self.links.consume(token=token, ip="198.51.100.9", user_agent=IPHONE), self.alice)
        self.assertIsNone(self.links.consume(token=token))
        token = self.links.create_for_device(ALICE_DEVICE)["token"]
        self.now += 301
        self.assertIsNone(self.links.consume(token=token))

    def test_no_typed_code_and_only_hashes(self):
        made = self.links.create_for_device(ALICE_DEVICE)
        stored = b"".join(v if isinstance(v, bytes) else str(v).encode()
                          for row in self.accounts.conn.execute("SELECT * FROM phone_link") for v in row)
        self.assertNotIn(made["token"].encode(), stored)
        for _ in range(3):
            self.assertIsNone(self.links.info(code="ABCD2345"))

    def test_admin_link(self):
        made = self.links.create_for_admin(self.alice, None)
        self.assertEqual(self.links.origin_of(token=made["token"])["origin"], "admin")
        with self.assertRaises(ValueError):
            self.links.create_for_admin(999)
        self.assertEqual(self.links.consume(token=made["token"]), self.alice)

    def test_a_pc_link_is_still_origin_pc(self):
        made = self.links.create(self.alice, "203.0.113.9", CHROME_MAC)
        self.assertEqual(self.links.origin_of(token=made["token"])["origin"], "pc")
        self.assertEqual(self.links.origin_of(token="nonsense")["origin"], "pc")


class MediaServerRecoverTests(unittest.TestCase):
    """The media server's side: CONFIG offers it, STATUS asks, SWITCH answers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logs = []
        self.gate = Gate(root / "tv.db", root / "devices", logger=self.logs.append)
        self.accounts = self.gate.accounts
        self.accounts.set_meta("public_url", "https://tv.example.com")
        self.alice = self.accounts.create_user("Alice")
        row = self.accounts.register_device(ALICE_DEVICE, "xingzhi-cube-154", SECRET)
        self.accounts.bind(row["claim_code"], self.alice)
        self.server = types.SimpleNamespace(device_recover=True, tenancy=self.gate, device_id=ALICE_DEVICE,
                                            device_peer="203.0.113.5", logger=self.logs.append, phase="",
                                            device_status=None, _recover_out=[])
        for name in ("_recover_ask", "recover_flush", "_recover_config"):
            setattr(self.server, name, types.MethodType(getattr(AVServer, name), self.server))

    def tearDown(self):
        self.accounts.close()
        self.tmp.cleanup()

    def status(self, report):
        AVServer._take_status(self.server, Packet(Kind.STATUS, 1, 0, 0, json_bytes(report)))

    def sent(self, session=7, seq=3):
        a, b = socket.socketpair()
        try:
            after = self.server.recover_flush(a, session, seq)
            payloads = []
            for _ in range(after - seq):
                packet = receive_packet(b, 1.0, expected_session=session)
                self.assertEqual(packet.kind, Kind.SWITCH)
                payloads.append(json.loads(packet.payload))
            return payloads
        finally:
            a.close()
            b.close()

    def test_config_offers_it_only_when_it_can_work(self):
        self.assertEqual(self.server._recover_config(), {"recover": 1})
        self.server.device_recover = False
        self.assertEqual(self.server._recover_config(), {}, "firmware that did not announce it")
        self.server.device_recover = True
        self.accounts.set_meta("public_url", "")
        self.assertEqual(self.server._recover_config(), {}, "no site for the QR code")
        self.server.tenancy = None
        self.assertEqual(self.server._recover_config(), {})

    def test_status_asks_and_switch_answers_with_an_upper_case_link(self):
        self.status({"recover": 1})
        (answer,) = self.sent()
        url = answer["recover"]["url"]
        self.assertRegex(url, r"^HTTPS://TV\.EXAMPLE\.COM/TV/P/[A-Z2-7]{26}$")
        self.assertLessEqual(len(url), recover.RECOVER_URL_MAX)
        self.assertEqual(answer["recover"]["ttl"], 300)
        token = url.rsplit("/", 1)[1]
        self.assertEqual(phonelinks.PhoneLinks(self.accounts).info(token=token)["name"], "Alice")
        self.assertFalse(any(token in line for line in self.logs), "the token never reaches the log")
        rows = [r for r in self.accounts.audit_rows() if r["action"] == "phone_link.device"]
        self.assertEqual((rows[0]["actor_user"], rows[0]["target_id"], rows[0]["actor_ip_prefix"]),
                         (self.alice, ALICE_DEVICE, "203.0.113.0/24"))
        self.assertEqual(self.sent(), [], "sent once")

    def test_refusals(self):
        for _ in range(2):
            self.status({"recover": 1})
        self.status({"recover": 1})
        self.status({"recover": 1})
        answers = self.sent()
        self.assertEqual([("url" in a["recover"], a["recover"].get("err")) for a in answers],
                         [(True, None)] * 3 + [(False, "limited")])
        self.accounts.unbind(ALICE_DEVICE, None)
        self.status({"recover": 1})
        self.assertEqual(self.sent(), [{"recover": {"err": "unbound"}}])
        self.accounts.set_meta("public_url", "")
        self.status({"recover": 1})
        self.assertEqual(self.sent(), [{"recover": {"err": "off"}}])
        self.server.device_recover = False
        self.status({"recover": 1})
        self.assertEqual(self.sent(), [], "not announced in HELLO: nothing sent")

    def test_the_firmware_takes_the_longest_address(self):
        import re
        header = (Path(__file__).resolve().parents[1] / "main" / "av_protocol.h").read_text()
        limit = int(re.search(r"#define AV_RECOVER_URL_MAX (\d+)u", header).group(1))
        self.assertEqual(limit, recover.RECOVER_URL_MAX)
        self.assertIn('"recover"', (Path(__file__).resolve().parents[1] / "main" / "av_player.c").read_text())

    def test_a_long_site_does_not_produce_a_broken_code(self):
        self.assertEqual(recover.link_url("https://" + "a" * 80 + ".example.com", "A" * 26), "")
        self.assertEqual(recover.link_url("https://example.com/", "A" * 26), "HTTPS://EXAMPLE.COM/TV/P/" + "A" * 26)
        self.assertEqual(recover.link_url("https://ex ample.com", "A" * 26), "")


class RecoveryLandingTests(RelayCase):
    """The link the device showed, opened on a phone; the notice on the owner's other sessions."""

    def device_link(self):
        answer = recover.answer(self.accounts, ALICE_DEVICE, "203.0.113.5", logger=lambda _m: None)
        return answer["recover"]["url"].rsplit("/", 1)[1]

    def test_scanning_the_device_code_signs_the_phone_in_as_the_owner(self):
        pc = "tvs=" + self.accounts.new_session(self.alice, "pc", user_agent=CHROME_MAC)
        old_phone = "tvs=" + self.accounts.new_session(self.alice, "phone", user_agent=IPHONE)
        self.assertNotIn("notice", self.json("GET", "/api/login/status", pc)[2])
        self.now += 60
        token = self.device_link()
        status, _, page = self.raw("GET", f"/TV/P/{token}")
        self.assertEqual(status, 200)
        info = self.json("GET", f"/api/phone-link/info?t={token}")[2]
        self.assertEqual((info["ok"], info["name"], info["origin"], info["device_name"]),
                         (True, "Alice", "device", "小智 AAA1"))
        status, headers, answer = self.json("POST", "/api/phone-link/accept", None, {"token": token},
                                            ip="198.51.100.9")
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["redirect"].endswith("&home=1"))
        new_phone = headers["Set-Cookie"].split(";")[0]
        session = self.accounts.session(new_phone[4:])
        self.assertEqual((session["user_id"], session["kind"], session["role"]), (self.alice, "phone", "user"))
        self.assertIn("login.phone.recover", self.actions())
        self.assertFalse(self.json("POST", "/api/phone-link/accept", None, {"token": token})[2]["ok"], "single use")
        # The owner's other sessions are told, and the list marks it; the new phone itself is not told.
        notice = self.json("GET", "/api/login/status", pc)[2]["notice"]
        self.assertEqual((notice["text"], notice["browser"], notice["network"]),
                         ("有新手机通过设备恢复登录", "Safari · iOS", "198.51.100.0/24"))
        self.assertEqual(self.json("GET", "/api/state", old_phone)[2]["notice"]["id"], notice["id"])
        self.assertNotIn("notice", self.json("GET", "/api/state", new_phone)[2])
        self.assertNotIn("notice", self.json("GET", "/api/login/status", new_phone)[2])
        rows = self.json("GET", "/api/sessions", pc)[2]["sessions"]
        marked = [r for r in rows if r["via"] == "recover"]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["id"], notice["id"])
        # Revoking it from the PC ends it, and the notice goes with it.
        self.assertTrue(self.json("POST", "/api/sessions/revoke", pc, {"id": notice["id"]})[2]["ok"])
        self.assertIsNone(self.accounts.session(new_phone[4:]))
        self.assertNotIn("notice", self.json("GET", "/api/login/status", pc)[2])
        # A session that starts after the recovery is not told about it.
        self.assertIsNone(self.accounts.recovery_notice(self.alice, None))

    def test_the_notice_fades_after_a_week(self):
        pc = "tvs=" + self.accounts.new_session(self.alice, "pc", user_agent=CHROME_MAC)
        self.now += 1
        self.json("POST", "/api/phone-link/accept", None, {"token": self.device_link()})
        self.assertIn("notice", self.json("GET", "/api/login/status", pc)[2])
        self.now += 7 * 24 * 3600 + 2
        self.assertNotIn("notice", self.json("GET", "/api/login/status", pc)[2])

    def test_a_pc_link_is_not_a_recovery(self):
        pc = "tvs=" + self.accounts.new_session(self.alice, "pc", user_agent=CHROME_MAC)
        self.now += 1
        link = self.json("POST", "/api/phone-link", pc, {}, agent=CHROME_MAC)[2]
        token = link["link"].rsplit("/", 1)[1]
        self.assertEqual(self.json("GET", f"/api/phone-link/info?t={token}")[2]["origin"], "pc")
        self.assertTrue(self.json("POST", "/api/phone-link/accept", None, {"token": token})[2]["ok"])
        self.assertIn("login.phone.link", self.actions())
        self.assertNotIn("notice", self.json("GET", "/api/login/status", pc)[2])


class AdminLoginLinkTests(unittest.TestCase):
    def test_login_link_prints_a_one_time_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.db"
            accounts = Accounts(path)
            accounts.create_user("管理员", "admin")
            alice = accounts.create_user("Alice")
            accounts.set_meta("public_url", "https://example.com")
            accounts.close()

            def run(*argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    code = admin.main(["--db", str(path), *argv])
                return code, out.getvalue()

            code, out = run("login-link", str(alice))
            self.assertEqual(code, 0, out)
            first = out.splitlines()[0]
            self.assertRegex(first, r"^HTTPS://EXAMPLE\.COM/TV/P/[A-Z2-7]{26}$")
            self.assertIn("5 分钟", out)
            self.assertEqual(run("login-link", "99")[0], 1)
            accounts = Accounts(path)
            links = phonelinks.PhoneLinks(accounts)
            token = first.rsplit("/", 1)[1]
            self.assertEqual(links.info(token=token)["name"], "Alice")
            self.assertEqual(links.consume(token=token), alice)
            self.assertIsNone(links.consume(token=token))
            row = [r for r in accounts.audit_rows() if r["action"] == "phone_link.admin"][0]
            self.assertEqual(row["target_id"], str(alice))
            accounts.close()


class SchemaTests(unittest.TestCase):
    def test_old_database_gains_the_origin_and_via_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE phone_link (token_sha256 BLOB PRIMARY KEY, code_sha256 BLOB NOT NULL, "
                        "poll_sha256 BLOB NOT NULL, user_id INTEGER NOT NULL, created_at REAL NOT NULL, "
                        "expires_at REAL NOT NULL, used_at REAL, pc_ip_prefix TEXT NOT NULL DEFAULT '', "
                        "pc_user_agent TEXT NOT NULL DEFAULT '', phone_ip_prefix TEXT NOT NULL DEFAULT '', "
                        "phone_user_agent TEXT NOT NULL DEFAULT '')")
            old.execute("INSERT INTO phone_link VALUES (x'00', x'01', x'02', 1, 1, 2, NULL, '', '', '', '')")
            old.execute("CREATE TABLE session (token_sha256 BLOB PRIMARY KEY, user_id INTEGER, kind TEXT NOT NULL, "
                        "device_id TEXT, created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL, "
                        "user_agent TEXT NOT NULL DEFAULT '', ip_prefix TEXT NOT NULL DEFAULT '')")
            old.commit()
            old.close()
            conn = db.connect(path)
            self.assertEqual(conn.execute("SELECT origin, device_id FROM phone_link").fetchone()[:], ("pc", None))
            self.assertIn("via", {row["name"] for row in conn.execute("PRAGMA table_info(session)")})
            conn.close()
            db.connect(path).close()


if __name__ == "__main__":
    unittest.main()
