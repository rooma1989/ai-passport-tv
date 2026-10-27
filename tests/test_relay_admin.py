"""The admin backend (multi-user phase 3), over real sockets, plus its pieces.

The contract is section 6 of docs/superpowers/specs/2026-10-12-multi-tenant-design.md
and section 8.2's "admin endpoints check the role on the server": every admin
API answers 404 to a member, a guest and nobody, and works for the admin (and
the key, the break-glass); every change is in the audit log with the admin and
their network; a ban ends the user's sessions and refuses their devices; a
delete exports first; the global channel table is validated by the media
server's own parser, keeps channel ids, and is saved with a dated backup; the
host sampler degrades to "not available" without /proc.
"""

import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import channel_groups, now_playing
from server.cast_switch import SwitchWatcher
from server.protocol import ProtocolError
from server.relay import admin, channeltable, events, hoststats, web
from server.tenancy import files as device_files, gate
from server.tenancy.accounts import Accounts

from test_relay_web import KEY, FakeClient, FakeYouTube

ASSETS = Path(__file__).resolve().parents[1] / "server" / "relay" / "assets"
ADMIN_JS = (ASSETS / "console-admin.js").read_text(encoding="utf-8")
ALICE_DEVICE = "aaaaaaaaaaa1"
BOB_DEVICE = "bbbbbbbbbbb2"
SECRET = "s" * 43
CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129.0 Safari/537.36"
MAIN = ("# 手写的\n"
        "ch000 | CCTV1 | https://x/1.m3u8\n"
        "ch001 | 湖南卫视 | https://x/2.m3u8 | Agent/1\n"
        "ch007 | 凤凰中文 | https://x/7.m3u8\n")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.now = 1_760_000_000.0
        self.accounts = Accounts(root / "tv.db", clock=lambda: self.now)
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.bob = self.accounts.create_user("Bob")
        for device, user in ((ALICE_DEVICE, self.alice), (BOB_DEVICE, self.bob)):
            row = self.accounts.register_device(device, "xingzhi-cube-154", SECRET)
            self.accounts.bind(row["claim_code"], user)
        self.devices_dir = root / "devices"
        self.main = root / "channels.txt"
        self.main.write_text(MAIN, encoding="utf-8")
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, youtube=FakeYouTube(),
                               main_channels=self.main, public_url="https://example.com",
                               accounts=self.accounts, devices_dir=self.devices_dir,
                               log=lambda _m: None, clock=lambda: self.now)
        self.relay.sync_devices(force=True)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        self.streams.server_close()
        threading.Thread(target=self.manage.serve_forever, daemon=True).start()
        guest = self.accounts.new_session(None, "guest", device_id=ALICE_DEVICE)
        self.cookies = {name: "tvs=" + self.accounts.new_session(uid) for name, uid in
                        (("admin", self.admin), ("alice", self.alice), ("bob", self.bob))}
        self.cookies["guest"] = "tvs=" + guest
        self.cookies["nobody"] = ""

    def tearDown(self):
        self.manage.shutdown()
        self.manage.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    # ---- helpers ----

    def raw(self, method, path, cookie=None, body=None, headers=None):
        # UTF-8 like a browser's fetch (the 8 kB body limit counts these bytes).
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.manage.server_address[1]}{path}",
                                         data=data, method=method)
        request.add_header("User-Agent", CHROME)
        request.add_header("X-Real-IP", "203.0.113.77")
        if cookie:
            request.add_header("Cookie", cookie)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=20) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def call(self, who, method, path, body=None, headers=None):
        status, _, payload = self.raw(method, path, self.cookies.get(who, who),
                                      body if body is not None else ({} if method == "POST" else None), headers)
        return status, (json.loads(payload) if status == 200 else None)

    def audits(self, action):
        return [r for r in self.accounts.audit_rows(500) if r["action"] == action]

    def events_of(self, device):
        return [e.action for e in events.read_events(device_files.device_files(self.devices_dir, device).events)]

    # ---- who may use it ----

    GETS = ("/api/admin/overview", "/api/admin/overview?history=1", "/api/admin/users", "/api/admin/devices",
            "/api/admin/audit", "/api/admin/system", "/api/admin/channels", "/api/admin/users/2/sessions",
            "/api/admin/users/2/export")
    POSTS = ("/api/admin/users/2/rename", "/api/admin/users/2/ban", "/api/admin/users/2/unban",
             "/api/admin/users/2/reviewed", "/api/admin/users/2/login-link", "/api/admin/users/2/delete",
             "/api/admin/users/2/sessions/revoke", "/api/admin/invites", "/api/admin/channels",
             "/api/admin/system/legacy-token", f"/api/admin/devices/{ALICE_DEVICE}/unbind",
             f"/api/admin/devices/{ALICE_DEVICE}/ban", f"/api/admin/devices/{ALICE_DEVICE}/unban",
             f"/api/admin/devices/{ALICE_DEVICE}/reconnect", f"/api/admin/devices/{ALICE_DEVICE}/repair",
             f"/api/admin/devices/{ALICE_DEVICE}/rename")

    def test_every_admin_api_is_404_to_members_guests_and_nobody(self):
        before = len(self.accounts.audit_rows(1000))
        for who in ("alice", "guest", "nobody"):
            for path in self.GETS:
                self.assertEqual(self.call(who, "GET", path)[0], 404, (who, path))
            for path in self.POSTS:
                body = {"name": "x", "confirm": "Alice", "all": True, "on": False, "version": "", "rows": ["ch000"]}
                self.assertEqual(self.call(who, "POST", path, body)[0], 404, (who, path))
        # Nothing happened: no audit entry, Alice still active with her device, the table untouched.
        self.assertEqual(len(self.accounts.audit_rows(1000)), before)
        self.assertEqual(self.accounts.user(self.alice)["status"], "active")
        self.assertEqual(self.accounts.device(ALICE_DEVICE)["status"], "active")
        self.assertEqual(self.main.read_text(encoding="utf-8"), MAIN)

    def test_admin_and_key_can_read_every_page(self):
        for path in self.GETS:
            self.assertEqual(self.call("admin", "GET", path)[0], 200, path)
        status, answer = self.call("nobody", "GET", "/api/admin/users", headers={"X-Key": KEY})
        self.assertEqual(status, 200)
        self.assertEqual([u["name"] for u in answer["users"]], ["管理员", "Alice", "Bob"])

    def test_old_page_address_goes_to_the_console_admin_section(self):
        # The admin backend is the console's 管理 section now (assets/console-admin.js); the old
        # address is a bookmark that keeps working. The console shows the section to admins only.
        for who in ("admin", "alice", "guest", "nobody"):
            for path in ("/tv/admin", "/tv/admin/"):
                status, headers, _ = self.raw("GET", path, self.cookies[who])
                self.assertEqual((status, headers["Location"]), (302, "/tv/console#/admin"), (who, path))
        status, headers, body = self.raw("GET", "/tv/console", self.cookies["admin"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        page = body.decode("utf-8")
        self.assertRegex(page, r'src="console-admin\.js\?v=[0-9a-f]{12}"')
        self.assertRegex(page, r'href="console-admin\.css\?v=[0-9a-f]{12}"')
        self.assertIn('data-route="admin"', page)
        self.assertNotIn('href="admin"', page)
        for name in ("console-admin.js", "console-admin.css"):
            self.assertEqual(self.raw("GET", "/tv/" + name, "")[0], 200, name)
        script = ADMIN_JS
        # Every section of the old page is in the console, against the same endpoints.
        for path in ("api/admin/overview", "api/admin/users", "api/admin/devices", "api/admin/channels",
                     "api/admin/audit?", "api/admin/system", "api/admin/epg", "api/admin/invites",
                     "api/admin/channels/import", "api/admin/channels/probe", "api/admin/channels/check", "当备用", "检测全部线路", "/login-link", "/sessions/revoke",
                     "/reconnect", "/repair", "/unbind", "api/admin/system/legacy-token"):
            self.assertIn(path, script, path)
        console_js = (ASSETS / "console.js").read_text(encoding="utf-8")
        self.assertIn('admin: "管理"', console_js)
        self.assertIn('if (name === "admin" && !isAdmin()) name = "home";', console_js)

    def test_system_reports_versions_and_pc_watch(self):
        from server.selfhost.version import VERSION
        answer = self.call("admin", "GET", "/api/admin/system")[1]
        self.assertEqual(answer["version"], VERSION)
        self.assertEqual(answer["firmware_version"], "")
        self.assertEqual((answer["pc_watch"], answer["pc_warm"]), (self.relay.watch is not None, 0))

    def test_cross_site_post_is_refused(self):
        status, _ = self.call("admin", "POST", f"/api/admin/users/{self.alice}/rename", {"name": "Eve"},
                              headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 404)
        status, _ = self.call("admin", "POST", f"/api/admin/users/{self.alice}/ban", {},
                              headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 404)
        self.assertEqual(self.accounts.user(self.alice)["name"], "Alice")
        self.assertEqual(self.accounts.user(self.alice)["status"], "active")

    # ---- users ----

    def test_rename_is_audited_with_admin_and_network(self):
        status, answer = self.call("admin", "POST", f"/api/admin/users/{self.alice}/rename", {"name": "  阿丽  丝 "})
        self.assertEqual((status, answer["ok"], answer["name"]), (200, True, "阿丽 丝"))
        self.assertEqual(self.accounts.user(self.alice)["name"], "阿丽 丝")
        entry = self.audits("user.rename")[0]
        self.assertEqual((entry["actor_user"], entry["actor_ip_prefix"], entry["target_id"]),
                         (self.admin, "203.0.113.0/24", str(self.alice)))
        self.assertFalse(self.call("admin", "POST", f"/api/admin/users/{self.alice}/rename", {"name": " "})[1]["ok"])

    def test_ban_ends_sessions_and_refuses_devices(self):
        self.assertEqual(self.call("alice", "GET", "/api/state")[0], 200)
        status, answer = self.call("admin", "POST", f"/api/admin/users/{self.alice}/ban")
        self.assertTrue(answer["ok"])
        self.assertEqual(self.accounts.user(self.alice)["status"], "banned")
        self.assertEqual(self.call("alice", "GET", "/api/state")[0], 404)
        self.assertEqual(self.events_of(ALICE_DEVICE)[-1], "reconnect")
        self.assertEqual(self.audits("user.ban")[0]["actor_ip_prefix"], "203.0.113.0/24")
        # The media server refuses her device on its next HELLO, and serves Bob's.
        tv = gate.Gate(self.root / "tv.db", self.devices_dir, logger=lambda _m: None)
        try:
            with self.assertRaises(ProtocolError):
                tv.admit({"version": 1, "device_id": ALICE_DEVICE, "device_secret": SECRET}, None, 8098, "198.51.100.1")
            self.assertTrue(tv.admit({"version": 1, "device_id": BOB_DEVICE, "device_secret": SECRET},
                                     None, 8098, "198.51.100.2").bound)
            self.call("admin", "POST", f"/api/admin/users/{self.alice}/unban")
            self.assertTrue(tv.admit({"version": 1, "device_id": ALICE_DEVICE, "device_secret": SECRET},
                                     None, 8098, "198.51.100.3").bound)
        finally:
            tv.accounts.close()

    def test_admin_cannot_ban_or_delete_themselves(self):
        self.assertFalse(self.call("admin", "POST", f"/api/admin/users/{self.admin}/ban")[1]["ok"])
        self.assertFalse(self.call("admin", "POST", f"/api/admin/users/{self.admin}/delete",
                                   {"confirm": "管理员"})[1]["ok"])
        self.assertEqual(self.accounts.user(self.admin)["status"], "active")
        self.assertFalse((self.root / "exports").exists())     # refused before anything was exported

    def test_login_link_is_the_cli_path_and_signs_a_phone_in(self):
        status, answer = self.call("admin", "POST", f"/api/admin/users/{self.alice}/login-link")
        self.assertTrue(answer["ok"])
        self.assertRegex(answer["link"], r"^HTTPS://EXAMPLE\.COM/TV/P/[A-Z2-7]{26}$")
        self.assertEqual(answer["ttl"], 300)
        self.assertTrue(answer["svg"].startswith("<svg"))
        row = self.accounts._one("SELECT origin, user_id FROM phone_link ORDER BY created_at DESC LIMIT 1")
        self.assertEqual((row["origin"], row["user_id"]), ("admin", self.alice))
        self.assertEqual(self.audits("phone_link.admin")[0]["actor_user"], self.admin)
        token = answer["link"].rsplit("/", 1)[1]
        status, headers, body = self.raw("POST", "/api/phone-link/accept", None, {"token": token})
        self.assertTrue(json.loads(body)["ok"])
        phone = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.accounts.session(phone.split("=", 1)[1])["user_id"], self.alice)
        # Single use.
        self.assertFalse(json.loads(self.raw("POST", "/api/phone-link/accept", None, {"token": token})[2])["ok"])

    def test_sessions_list_and_revoke(self):
        status, answer = self.call("admin", "GET", f"/api/admin/users/{self.alice}/sessions")
        kinds = sorted(s["kind"] for s in answer["sessions"])
        self.assertEqual(kinds, ["guest", "phone"])
        self.assertTrue(all("token" not in json.dumps(s) and len(s["id"]) == 16 for s in answer["sessions"]))
        guest = next(s for s in answer["sessions"] if s["kind"] == "guest")
        self.assertTrue(self.call("admin", "POST", f"/api/admin/users/{self.alice}/sessions/revoke",
                                  {"id": guest["id"]})[1]["ok"])
        self.assertEqual(self.call("guest", "GET", "/api/state")[0], 404)
        self.assertTrue(self.call("admin", "POST", f"/api/admin/users/{self.alice}/sessions/revoke", {"all": True})[1]["ok"])
        self.assertEqual(self.call("alice", "GET", "/api/state")[0], 404)
        self.assertEqual(self.call("bob", "GET", "/api/state")[0], 200)
        self.assertEqual(len(self.audits("admin.session.revoke")), 1)
        self.assertEqual(len(self.audits("admin.session.revoke_all")), 1)

    def test_delete_exports_first_then_unbinds_and_removes(self):
        self.accounts.save_user_state(self.alice, {"creators": [], "history": [{"title": "x"}]})
        invite = self.accounts.create_invite(self.alice, "user", "朋友")
        self.assertFalse(self.call("admin", "POST", f"/api/admin/users/{self.alice}/delete", {"confirm": "alice"})[1]["ok"])
        self.assertIsNotNone(self.accounts.user(self.alice))
        status, answer = self.call("admin", "POST", f"/api/admin/users/{self.alice}/delete", {"confirm": "Alice"})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["unbound"], [ALICE_DEVICE])
        saved = self.root / answer["file"]
        self.assertTrue(saved.is_file())
        exported = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(exported["user"]["name"], "Alice")
        self.assertEqual(exported["state"]["history"], [{"title": "x"}])
        self.assertEqual([d["id"] for d in exported["devices"]], [ALICE_DEVICE])
        self.assertNotIn("token", json.dumps(exported["sessions"]))
        self.assertIsNone(self.accounts.user(self.alice))
        self.assertEqual(self.accounts.device(ALICE_DEVICE)["status"], "unclaimed")
        self.assertIsNotNone(self.accounts.device(ALICE_DEVICE)["claim_code"])
        self.assertIsNone(self.accounts.load_user_state(self.alice))
        self.assertEqual(self.call("alice", "GET", "/api/state")[0], 404)
        self.assertEqual(self.call("guest", "GET", "/api/state")[0], 404)
        self.assertIsNotNone(self.accounts.invite(invite))       # her unused invite still works for the friend
        self.assertEqual(self.events_of(ALICE_DEVICE)[-1], "reconnect")
        self.assertEqual(self.audits("user.delete")[0]["detail"], "Alice")
        self.assertEqual(self.call("bob", "GET", "/api/state")[0], 200)

    def test_owner_of_a_legacy_device_cannot_be_deleted(self):
        self.accounts.add_legacy_device("legacy-xingzhi", 8098, "xingzhi-cube-154", "小智", self.bob)
        answer = self.call("admin", "POST", f"/api/admin/users/{self.bob}/delete", {"confirm": "Bob"})[1]
        self.assertFalse(answer["ok"])
        self.assertIn("旧固件", answer["error"])
        self.assertIsNotNone(self.accounts.user(self.bob))
        self.assertEqual(self.accounts.device(BOB_DEVICE)["status"], "active")

    def test_users_list_flags_self_registered_until_reviewed(self):
        device = self.accounts.register_device("ccccccccccc3", "ai-passport", SECRET)
        uid, _token, _row = self.accounts.register_with_claim(device["claim_code"], "新人")
        invite = self.accounts.create_invite(self.admin, "user", "")
        invited, _ = self.accounts.accept_invite(invite, "受邀")
        listed = {u["id"]: u for u in self.call("admin", "GET", "/api/admin/users")[1]["users"]}
        self.assertEqual((listed[uid]["origin"], listed[uid]["reviewed_at"], listed[uid]["devices"]), ("signup", None, 1))
        self.assertEqual(listed[invited]["origin"], "invite")
        self.assertEqual(listed[self.alice]["origin"], "initial")
        self.assertEqual(self.call("admin", "GET", "/api/admin/overview")[1]["counts"]["review"], 1)
        self.call("admin", "POST", f"/api/admin/users/{uid}/reviewed")
        listed = {u["id"]: u for u in self.call("admin", "GET", "/api/admin/users")[1]["users"]}
        self.assertEqual(listed[uid]["reviewed_at"], self.now)
        self.assertEqual(self.call("admin", "GET", "/api/admin/overview")[1]["counts"]["review"], 0)

    def test_invite_and_legacy_token_toggle_are_audited(self):
        answer = self.call("admin", "POST", "/api/admin/invites", {"name": "表妹"})[1]
        self.assertRegex(answer["link"], r"^https://example\.com/tv/i/[A-Z2-7]{20}$")
        self.assertEqual(self.accounts.invite(answer["link"].rsplit("/", 1)[1])["name"], "表妹")
        self.assertEqual(self.audits("invite.create")[0]["actor_ip_prefix"], "203.0.113.0/24")
        self.assertTrue(self.call("admin", "POST", "/api/admin/system/legacy-token", {"on": False})[1]["ok"])
        self.assertEqual(self.accounts.meta(gate.LEGACY_TOKEN_KEY), "0")
        self.assertFalse(self.call("admin", "GET", "/api/admin/system")[1]["legacy_token"])
        self.assertEqual(len(self.audits("legacy_token.off")), 1)

    # ---- devices ----

    def test_device_ban_unban_unbind_reconnect(self):
        self.assertTrue(self.call("admin", "POST", f"/api/admin/devices/{ALICE_DEVICE}/reconnect")[1]["ok"])
        self.assertEqual(self.events_of(ALICE_DEVICE), ["reconnect"])
        self.assertTrue(self.call("admin", "POST", f"/api/admin/devices/{ALICE_DEVICE}/ban")[1]["ok"])
        self.assertEqual(self.accounts.device(ALICE_DEVICE)["status"], "banned")
        self.assertEqual(self.call("guest", "GET", "/api/state")[0], 404)
        self.assertEqual(self.events_of(ALICE_DEVICE), ["reconnect", "reconnect"])
        self.assertEqual(self.call("admin", "POST", f"/api/admin/devices/{ALICE_DEVICE}/unban")[1]["status"], "active")
        self.assertTrue(self.call("admin", "POST", f"/api/admin/devices/{ALICE_DEVICE}/unbind")[1]["ok"])
        self.assertEqual(self.accounts.device(ALICE_DEVICE)["status"], "unclaimed")
        self.assertIsNone(self.accounts.owner(ALICE_DEVICE))
        actions = [r["action"] for r in reversed(self.accounts.audit_rows(50))
                   if r["target_id"] == ALICE_DEVICE and r["actor_user"] == self.admin]
        self.assertEqual(actions, ["device.reconnect", "device.banned", "device.active", "device.unbind"])
        self.assertFalse(self.call("admin", "POST", f"/api/admin/devices/{ALICE_DEVICE}/reconnect")[1]["ok"])
        self.assertEqual(self.call("admin", "POST", "/api/admin/devices/zzzzzzzzzzzz/ban")[0], 404)

    def test_a_device_refused_for_its_secret_is_flagged_until_it_connects(self):
        """The admin sees which device needs "re-pair" (gate.py audits device.secret_mismatch)."""
        def flagged():
            rows = self.call("admin", "GET", "/api/admin/devices")[1]["devices"]
            overview = self.call("admin", "GET", "/api/admin/overview")[1]["devices"]
            mine = {d["id"]: d["key_mismatch"] for d in rows}
            self.assertEqual(mine, {d["id"]: d["key_mismatch"] for d in overview})
            return mine
        self.accounts.touch_device(ALICE_DEVICE)
        self.assertEqual(flagged(), {ALICE_DEVICE: False, BOB_DEVICE: False})
        self.now += 60
        self.accounts.audit("device.secret_mismatch", None, "203.0.113.5", "device", ALICE_DEVICE)
        self.assertEqual(flagged(), {ALICE_DEVICE: True, BOB_DEVICE: False})
        # While a re-pair window is open the badge gives way to "waiting for the new key".
        self.assertTrue(self.call("admin", "POST", f"/api/admin/devices/{ALICE_DEVICE}/repair")[1]["ok"])
        self.assertFalse(flagged()[ALICE_DEVICE])
        self.now += 11 * 60
        self.assertTrue(flagged()[ALICE_DEVICE])
        # Connected again (with the pinned or the old secret): gone.
        self.now += 1
        self.accounts.touch_device(ALICE_DEVICE)
        self.assertFalse(flagged()[ALICE_DEVICE])
        self.assertIn("密钥不匹配：点「重新配对」", ADMIN_JS)
        self.assertIn("d.key_mismatch", ADMIN_JS)

    def test_devices_carry_the_firmware_version_and_the_release_s(self):
        self.accounts.touch_device(ALICE_DEVICE, fw="v0.1.0")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"schema": 1, "version": "v0.2.0", "builds": [
            {"board": "xingzhi-cube-154", "version": "v0.2.0"}, {"board": "ai-passport", "version": "v0.2.0"}]}))
        env = {admin.FIRMWARE_MANIFEST_ENV: str(manifest), admin.FLASHER_URL_ENV: "/flash/"}
        with unittest.mock.patch.dict(admin.os.environ, env):
            answer = self.call("admin", "GET", "/api/admin/devices")[1]
        row = next(d for d in answer["devices"] if d["id"] == ALICE_DEVICE)
        self.assertEqual((row["model"], row["fw"]), ("xingzhi-cube-154", "v0.1.0"))
        self.assertEqual(answer["firmware"], {"latest": {"xingzhi-cube-154": "v0.2.0", "ai-passport": "v0.2.0"},
                                              "version": "v0.2.0", "flasher": "/flash/"})
        # The owner's cloud host has neither: versions only, and the page says
        # where the flasher is in words.
        with unittest.mock.patch.dict(admin.os.environ, {}, clear=True):
            self.assertEqual(self.call("admin", "GET", "/api/admin/devices")[1]["firmware"],
                             {"latest": {}, "version": "", "flasher": ""})
        for bad in ("javascript:alert(1)", "//evil.example/", '/flash/"><b>'):
            self.assertEqual(admin.firmware_info({admin.FLASHER_URL_ENV: bad})["flasher"], "", bad)
        self.assertEqual(admin.firmware_info({admin.FIRMWARE_MANIFEST_ENV: str(self.root / "missing.json")})["latest"], {})
        self.assertIn('panel.id = "devUpdate"', ADMIN_JS)
        self.assertIn("可更新到", ADMIN_JS)

    def test_devices_and_overview_show_metrics(self):
        files = device_files.device_files(self.devices_dir, ALICE_DEVICE)
        now_playing.write(files.now, "ch001", True, True, now=self.now, since=self.now - 125,
                          metrics={"bytes_10s": 1_800_000, "fps": 14.8, "target_rate": 200000})
        overview = self.call("admin", "GET", "/api/admin/overview")[1]
        row = next(d for d in overview["devices"] if d["id"] == ALICE_DEVICE)
        self.assertEqual((row["online"], row["owner_name"], row["channel_name"], row["session_s"]),
                         (True, "Alice", "湖南卫视", 125))
        self.assertEqual((row["bytes_10s"], row["fps"], row["target_rate"]), (1_800_000, 14.8, 200000))
        other = next(d for d in overview["devices"] if d["id"] == BOB_DEVICE)
        self.assertFalse(other["online"])
        self.assertEqual(overview["youtube"]["max_streams"], 2)
        self.assertEqual(overview["counts"]["online"], 1)
        listed = self.call("admin", "GET", "/api/admin/devices")[1]["devices"]
        self.assertEqual({d["id"]: d["guests"] for d in listed}, {ALICE_DEVICE: 1, BOB_DEVICE: 0})

    # ---- audit ----

    def test_audit_filters_and_pages(self):
        for n in range(60):
            self.accounts.audit("device.rename", self.admin, "", "device", BOB_DEVICE, str(n))
        self.accounts.audit("user.rename", self.alice, "", "user", str(self.alice))
        page = self.call("admin", "GET", f"/api/admin/audit?device={BOB_DEVICE}")[1]
        self.assertEqual(len(page["rows"]), 50)
        self.assertTrue(all(r["target_id"] == BOB_DEVICE for r in page["rows"]))
        rest = self.call("admin", "GET", f"/api/admin/audit?device={BOB_DEVICE}&before={page['next']}")[1]
        self.assertEqual(len([r for r in rest["rows"] if r["action"] == "device.rename"]), 10)
        mine = self.call("admin", "GET", f"/api/admin/audit?user={self.alice}&action=user.")[1]["rows"]
        self.assertEqual([r["action"] for r in mine], ["user.rename"])
        self.assertEqual(mine[0]["actor_name"], "Alice")
        self.assertIn("device.rename", self.call("admin", "GET", "/api/admin/audit")[1]["actions"])
        # A filter is data, never SQL.
        weird = self.call("admin", "GET", "/api/admin/audit?action=%25'%20OR%201=1%20--&device=x")[1]
        self.assertEqual(weird["rows"], [])

    # ---- channel table ----

    def channels(self):
        return self.call("admin", "GET", "/api/admin/channels")[1]

    def test_channel_table_save_keeps_ids_backs_up_and_sets_groups(self):
        table = self.channels()
        self.assertEqual([r["id"] for r in table["rows"]], ["ch000", "ch001", "ch007"])
        self.assertEqual(table["rows"][1]["auto"], "卫视")
        rows = ["ch007", {"id": "ch000", "name": "CCTV-1 综合", "group": "其他"},
                {"name": "新台", "url": "https://x/new.m3u8", "agent": ""}]
        dry = self.call("admin", "POST", "/api/admin/channels", {"version": table["version"], "rows": rows, "dry": True})[1]
        self.assertTrue(dry["ok"], dry)
        self.assertEqual(self.main.read_text(encoding="utf-8"), MAIN)        # a check writes nothing
        self.assertEqual((dry["summary"]["added"], dry["summary"]["removed"], dry["summary"]["changed"]), (1, 1, 1))
        saved = self.call("admin", "POST", "/api/admin/channels", {"version": table["version"], "rows": rows})[1]
        self.assertTrue(saved["ok"], saved)
        backup = self.main.parent / saved["backup"]
        self.assertTrue(saved["backup"].startswith("channels.txt.bak-"))
        self.assertEqual(backup.read_text(encoding="utf-8"), MAIN)
        lines = [l for l in self.main.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines, ["ch007 | 凤凰中文 | https://x/7.m3u8", "ch000 | CCTV-1 综合 | https://x/1.m3u8",
                                 "ch008 | 新台 | https://x/new.m3u8"])
        # The global group is under each user's own, and reaches the device's groups file.
        self.assertEqual(admin.global_groups(self.relay), {"ch000": "其他"})
        groups = json.loads(device_files.device_files(self.devices_dir, ALICE_DEVICE).groups.read_text(encoding="utf-8"))
        self.assertEqual(groups, {"ch000": "其他"})
        audit = self.audits("channels.save")[0]
        self.assertIn("备份 channels.txt.bak-", audit["detail"])
        self.assertEqual(audit["actor_ip_prefix"], "203.0.113.0/24")
        # The old version is stale now.
        stale = self.call("admin", "POST", "/api/admin/channels", {"version": table["version"], "rows": ["ch007"]})[1]
        self.assertTrue(stale["stale"])

    def test_user_group_sits_on_top_of_the_global_one(self):
        table = self.channels()
        self.call("admin", "POST", "/api/admin/channels",
                  {"version": table["version"], "rows": [{"id": "ch000", "group": "国际"}, "ch001", "ch007"]})
        status, answer = self.call("alice", "POST", "/api/groups", {"channel": "ch000", "group": "音乐"})
        self.assertTrue(answer["ok"])
        groups = json.loads(device_files.device_files(self.devices_dir, ALICE_DEVICE).groups.read_text(encoding="utf-8"))
        self.assertEqual(groups["ch000"], "音乐")
        # Choosing the default (the global group) again just removes her own entry.
        self.call("alice", "POST", "/api/groups", {"channel": "ch000", "group": "国际"})
        self.assertEqual(self.relay.user_state(self.alice).group_overrides, {})
        state = self.call("bob", "GET", "/api/state")[1]
        self.assertIn("CCTV1", [c["name"] for g in state["groups"] if g["name"] == "国际" for c in g["channels"]])

    def test_channel_table_refuses_bad_rows(self):
        version = self.channels()["version"]
        for rows, needle in (([{"name": "坏", "url": "ftp://x"}], "http"),
                             ([{"name": "a|b", "url": "https://x/a"}], "竖线"),
                             ([{"name": "", "url": "https://x/a"}], "名字"),
                             (["ch000", "ch000"], "两次"),
                             (["ch404"], "没有"),
                             ([], "空"),
                             ([{"id": "ch000", "group": "火星"}], "分组")):
            answer = self.call("admin", "POST", "/api/admin/channels", {"version": version, "rows": rows})[1]
            self.assertFalse(answer["ok"], rows)
            self.assertIn(needle, answer["error"], rows)
        self.assertEqual(self.main.read_text(encoding="utf-8"), MAIN)

    def test_channel_table_longer_than_a_device_is_saved_and_says_what_the_tv_shows(self):
        table = self.channels()
        self.assertEqual(table["limits"], {"channels": channel_groups.CHANNEL_MAX, "bytes": channel_groups.CONFIG_MAX,
                                           "table": channel_groups.TABLE_MAX})
        self.assertTrue(all(f["fits"] and f["shown"] == f["channels"] for f in table["fit"]))
        name = ("很长的频道名字" * 5)[:39]
        rows = ["ch000"] + [{"name": name, "url": f"https://x/{n}.m3u8"} for n in range(30)]
        # What the media server will send each device: the user's own channels, then the table, cut the same way.
        own = [("cast", "我的投屏")]
        expected = channel_groups.device_list(own + [("ch000", "CCTV1")] + [(f"ch{n:03d}", name) for n in range(30)])
        self.assertLess(len(expected), 32)
        for dry in (True, False):
            status, answer = self.call("admin", "POST", "/api/admin/channels",
                                       {"version": table["version"], "rows": rows, "dry": dry})
            self.assertEqual(status, 200)
            self.assertTrue(answer["ok"], answer)
            fit = {f["user"]: f for f in answer["fit"]}
            self.assertEqual(set(fit), {"Alice", "Bob"})
            for user, item in fit.items():
                self.assertFalse(item["fits"])
                self.assertEqual(item["channels"], 32)
                self.assertEqual(item["shown"], len(expected))
                self.assertEqual(item["table_shown"], len(expected) - 1)
                self.assertTrue(any(w.startswith(f"「{user}」的小电视上只显示前 {len(expected) - 1} 个台"
                                                 "（按表里的顺序，常看的往前挪）") for w in answer["warnings"]),
                                answer["warnings"])
            if dry:
                self.assertEqual(self.main.read_text(encoding="utf-8"), MAIN)
        lines = [l for l in self.main.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
        self.assertEqual(len(lines), 31)
        # The cloud's 8 kB body limit still applies before anything is read.
        version = self.channels()["version"]
        rows = [{"name": name, "url": f"https://x/{n}.m3u8"} for n in range(60)]
        self.assertEqual(self.call("admin", "POST", "/api/admin/channels", {"version": version, "rows": rows})[0], 400)

    def test_channel_table_has_a_ceiling_of_its_own(self):
        rows = [{"id": f"ch{n:04d}", "name": "台", "url": "https://x/a.m3u8", "agent": ""}
                for n in range(channel_groups.TABLE_MAX)]
        self.assertEqual(channeltable.problems(rows), [])
        found = channeltable.problems(rows + [{"id": "ch9999", "name": "台", "url": "https://x/b.m3u8", "agent": ""}])
        self.assertEqual(found, [f"一共 {channel_groups.TABLE_MAX + 1} 个台，频道表最多 {channel_groups.TABLE_MAX} 个"])
        # The media server's parser takes a whole table of that size too (the PC plays all of it).
        checked = channeltable.media_check(channeltable.render(rows))
        self.assertTrue(checked["ok"], checked)
        self.assertEqual(checked["warnings"], [])

    def test_a_long_table_does_not_block_adding_a_series(self):
        # Before, the global table counted against the device's list and a long one refused every new channel.
        self.main.write_text("".join(f"ch{n:03d} | 很长很长很长的频道名字{n} | https://x/{n}.m3u8\n"
                                     for n in range(300)), encoding="utf-8")
        room = self.relay.room_of(self.relay.user_state(self.alice), "海尔兄弟(B站)")
        self.assertTrue(room["fits"])
        self.assertEqual(room["channels"], channel_groups.CHANNEL_MAX - 2)


class ChannelTableTests(unittest.TestCase):
    def test_new_ids_follow_the_highest(self):
        self.assertEqual(channeltable.next_id({"ch000", "ch093", "cctv"}), "ch094")
        self.assertEqual(channeltable.next_id(set()), "ch000")

    def test_media_server_parser_runs_and_reports_table_rows(self):
        good = channeltable.media_check(channeltable.render([{"id": "ch000", "name": "A", "url": "https://x", "agent": ""}]))
        self.assertTrue(good["ok"], good)
        bad = channeltable.media_check(channeltable.HEADER + "a | x | https://x\nb |  | https://y\n")
        self.assertFalse(bad["ok"])
        self.assertIn("第 2 行", bad["error"])

    def test_save_backs_up_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "channels.txt"
            self.assertEqual(channeltable.save(path, "one\n", clock=lambda: 1_760_000_000), "")
            names = [channeltable.save(path, f"v{n}\n", clock=lambda: 1_760_000_000) for n in range(3)]
            self.assertEqual(len(set(names)), 3)
            self.assertEqual(path.read_text(encoding="utf-8"), "v2\n")
            self.assertEqual((Path(tmp) / names[0]).read_text(encoding="utf-8"), "one\n")
            for n in range(channeltable.BACKUP_KEEP + 5):
                channeltable.save(path, f"w{n}\n", clock=lambda: 1_760_000_000 + n)
            self.assertEqual(len(list(Path(tmp).glob("channels.txt.bak-*"))), channeltable.BACKUP_KEEP)


class HostStatsTests(unittest.TestCase):
    def write_proc(self, root: Path, busy: int, idle: int, used_kb: int, tx: int):
        (root / "net").mkdir(parents=True, exist_ok=True)
        (root / "stat").write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 2 3 4\n", encoding="ascii")
        (root / "meminfo").write_text(f"MemTotal:  2000000 kB\nMemFree: 100 kB\nMemAvailable: {2000000 - used_kb} kB\n",
                                      encoding="ascii")
        (root / "net" / "dev").write_text(
            "Inter-|   Receive |  Transmit\n face |bytes packets|bytes\n"
            f"    lo: 999999 1 0 0 0 0 0 0 999999 1 0 0 0 0 0 0\n"
            f"  eth0: 5000 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n", encoding="ascii")

    def test_missing_proc_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = hoststats.HostStats(Path(tmp) / "no-proc")
            self.assertFalse(stats.available)
            self.assertFalse(stats.start())
            self.assertIsNone(stats.sample())
            self.assertEqual((stats.latest(), stats.history(), stats.processes()), (None, [], []))

    def test_rates_and_minute_folding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = [1_000_020.0]
            stats = hoststats.HostStats(root, clock=lambda: clock[0])
            self.write_proc(root, 100, 900, 500_000, 0)
            self.assertIsNone(stats.sample())          # the first reading only remembers
            for n in range(1, 14):
                clock[0] += 5
                self.write_proc(root, 100 + 25 * n, 900 + 75 * n, 512_000, 50_000 * n)
                point = stats.sample()
            self.assertEqual(point[1:], (25.0, 500, 1953, 0, 10000))
            history = stats.history()
            self.assertEqual(len(stats.coarse), 1)
            self.assertEqual(history[0][0] % 60, 0)
            self.assertEqual(history[0][1], 25.0)


class MetricsTests(unittest.TestCase):
    def test_meter_rates_and_session_reset(self):
        meter = now_playing.Meter()
        self.assertEqual(meter.sample(0.0, 0, 0), {"bytes_10s": 0, "fps": 0.0})
        self.assertEqual(meter.sample(2.0, 50_000, 30), {"bytes_10s": 0, "fps": 0.0})   # too soon
        self.assertEqual(meter.sample(10.0, 2_000_000, 150), {"bytes_10s": 2_000_000, "fps": 15.0})
        # New session: the byte counter started again from 0.
        self.assertEqual(meter.sample(15.0, 100_000, 225), {"bytes_10s": 200_000, "fps": 15.0})

    def test_metrics_are_written_but_read_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "now-playing.json"
            now_playing.write(path, "ch001", True, True, now=100.0, since=90.0,
                              metrics={"bytes_10s": 1500, "fps": 12.5, "target_rate": 110000, "junk": 1})
            self.assertEqual(now_playing.read(path, 105.0),
                             {"channel": "ch001", "online": True, "switch": True, "since": 90.0, "device": None,
                              "listen": None})
            self.assertEqual(now_playing.read_metrics(path, 105.0),
                             {"bytes_10s": 1500, "fps": 12.5, "target_rate": 110000})
            self.assertIsNone(now_playing.read_metrics(path, 200.0))
            now_playing.write(path, "ch001", True, True, now=100.0)
            self.assertEqual(now_playing.read_metrics(path, 105.0), {"bytes_10s": None, "fps": None, "target_rate": None})

    def test_media_server_reports_its_session(self):
        from unittest import mock
        from server import tv_server
        server = tv_server.AVServer.__new__(tv_server.AVServer)
        server.wire_bytes, server.frames_sent = 0, 0
        server._last_controller = type("Rate", (), {"rate": 180000})()
        with mock.patch.object(tv_server.time, "monotonic", return_value=100.0):
            self.assertEqual(server._session_metrics(), {"bytes_10s": 0, "fps": 0.0, "target_rate": 180000})
        server.wire_bytes, server.frames_sent = 1_000_000, 75
        with mock.patch.object(tv_server.time, "monotonic", return_value=110.0):
            self.assertEqual(server._session_metrics(), {"bytes_10s": 1_000_000, "fps": 7.5, "target_rate": 180000})

    def test_reconnect_event_ends_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay-events.json"
            watcher = SwitchWatcher(path)
            self.assertIsNone(watcher.poll("ch001", 1.0))
            events.write_event(path, "reconnect")
            self.assertEqual(watcher.poll("ch001", 2.0), ("restart", None))


if __name__ == "__main__":
    unittest.main()
