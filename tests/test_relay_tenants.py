"""The relay with several users (server/tenancy): who sees and controls what.

Driven over real sockets like test_relay_web.py, with the same fakes for
Bilibili, YouTube and ffmpeg. What is checked is the tenancy contract from the
design (docs/superpowers/specs/2026-10-12-multi-tenant-design.md, section 8.2):
another user's device is 404, never 403; account data is per user; the cast
slot and the channel table are per device; guests control one device and edit
nothing.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import now_playing
from server.relay import events, state as relay_state, web
from server.tenancy import files as device_files
from server.tenancy.accounts import Accounts

from test_relay_web import KEY, FakeClient, FakeProc, FakeYouTube

ALICE_DEVICE = "aaaaaaaaaaa1"
BOB_DEVICE = "bbbbbbbbbbb2"
SECRET = "s" * 43


class CountingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.upload_calls = 0

    def uploads(self, mid):
        self.upload_calls += 1
        return super().uploads(mid)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TenantWebTests(unittest.TestCase):
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
        self.main = root / "channels.txt"
        self.main.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | 湖南卫视 | https://x/2.m3u8\n",
                             encoding="utf-8")
        self.client = CountingClient()
        self.started = []
        self.relay = web.Relay(None, self.client, None, "/opt/ff", KEY, youtube=FakeYouTube(),
                               main_channels=self.main, public_url="https://example.com",
                               accounts=self.accounts, devices_dir=self.devices_dir,
                               log=lambda _m: None, clock=lambda: self.now,
                               starter=self.fake_start, pumper=self.fake_pump)
        self.relay.sync_devices(force=True)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        for server in (self.streams, self.manage):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.cookies = {name: "tvs=" + self.accounts.new_session(uid) for name, uid in
                        (("admin", self.admin), ("alice", self.alice), ("bob", self.bob))}

    def tearDown(self):
        for server in (self.streams, self.manage):
            server.shutdown()
            server.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def fake_start(self, ffmpeg, url, offset):
        self.started.append((url, offset))
        return FakeProc()

    def fake_pump(self, proc, write, on_tick):
        write(b"\x47" * 188)
        on_tick(30.0)
        return "ended"

    # ---- helpers ----

    def files(self, device):
        return device_files.device_files(self.devices_dir, device)

    def online(self, device, channel="ch000"):
        now_playing.write(self.files(device).now, channel, True, True, now=self.now, since=self.now,
                          device={"volume": 50, "brightness": 1, "steps": 5})

    def raw(self, method, path, cookie=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.manage.server_address[1]}{path}",
                                         data=data, method=method)
        if cookie:
            request.add_header("Cookie", cookie)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=5) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def call(self, who, method, path, body=None):
        cookie = self.cookies.get(who, who)
        status, _, payload = self.raw(method, path, cookie, body if body is not None else
                                      ({} if method == "POST" else None))
        return status, (json.loads(payload) if status == 200 else None)

    def stream(self, path):
        url = f"http://127.0.0.1:{self.streams.server_address[1]}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as answer:
                answer.read()   # to the end: the relay saves the outcome before it closes
                return answer.status
        except urllib.error.HTTPError as error:
            return error.code

    # ---- isolation ----

    def test_each_user_sees_only_their_own_devices(self):
        status, state = self.call("alice", "GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual([d["id"] for d in state["devices"]], [ALICE_DEVICE])
        self.assertEqual(state["me"], {"name": "Alice", "role": "user"})
        self.assertTrue(state["devices"][0]["mine"])
        status, devices = self.call("bob", "GET", "/api/devices")
        self.assertEqual([d["id"] for d in devices["devices"]], [BOB_DEVICE])

    def test_another_users_device_is_404_everywhere(self):
        self.online(BOB_DEVICE)
        for method, path, body in (
                ("POST", "/api/switch", {"channel": "ch001", "device": BOB_DEVICE}),
                ("POST", "/api/pause", {"device": BOB_DEVICE}),
                ("POST", "/api/device", {"volume": 30, "device": BOB_DEVICE}),
                ("POST", "/api/cast", {"url": "https://www.bilibili.com/video/BV1aZpw6DEKJ", "device": BOB_DEVICE}),
                ("POST", "/api/target", {"device": BOB_DEVICE}),
                ("POST", f"/api/devices/{BOB_DEVICE}", {"name": "mine now"}),
                ("POST", f"/api/devices/{BOB_DEVICE}/unbind", {}),
                ("POST", f"/api/devices/{BOB_DEVICE}/repair", {}),
                ("GET", f"/api/series/sr01/episodes?device={BOB_DEVICE}", None)):
            status, answer = self.call("alice", method, path, body)
            if path == "/api/target":
                # set_target answers in-band like before, but never selects the device.
                self.assertFalse(answer["ok"])
                continue
            self.assertEqual(status, 404, path)
        self.assertIsNone(events.read_event(self.files(BOB_DEVICE).events), "nothing reached Bob's TV")
        self.assertEqual(self.accounts.device(BOB_DEVICE)["status"], "active")

    def test_no_session_no_key_is_404(self):
        self.assertEqual(self.raw("GET", "/api/state")[0], 404)
        self.assertEqual(self.raw("GET", "/api/state", "tvs=forged")[0], 404)

    # ---- per-user data, per-device files ----

    def test_creators_are_per_user_and_tables_per_device(self):
        status, answer = self.call("alice", "POST", "/api/creators", {"url": "https://space.bilibili.com/42"})
        self.assertTrue(answer["ok"])
        alice_table = self.files(ALICE_DEVICE).channels.read_text(encoding="utf-8")
        self.assertIn(f"up01 | UP42(B站) | http://127.0.0.1:8099/ch/{ALICE_DEVICE}/up01", alice_table)
        self.assertNotIn("up01", self.files(BOB_DEVICE).channels.read_text(encoding="utf-8"))
        self.assertEqual(self.call("bob", "GET", "/api/state")[1]["creators"], [])
        self.assertEqual(self.stream(f"/ch/{ALICE_DEVICE}/up01"), 200)
        self.assertEqual(self.relay.user_state(self.alice).get("up01").watched, ["a"])
        self.assertEqual(self.stream(f"/ch/{BOB_DEVICE}/up01"), 404, "Bob has no up01")
        self.assertEqual(self.stream("/ch/up01"), 404, "the single-user address is gone")
        # What a stream plays, per device, for the TV server's loading screen.
        url = f"http://127.0.0.1:{self.streams.server_address[1]}/info/{ALICE_DEVICE}/up01"
        with urllib.request.urlopen(url, timeout=5) as answer:
            self.assertEqual(json.loads(answer.read())["channel"], "up01")
        self.assertEqual(self.stream(f"/info/{BOB_DEVICE}/up01"), 404, "Bob has no up01")
        self.assertEqual(self.stream("/info/up01"), 404)
        # Stored in the database, not only in memory.
        stored = self.accounts.load_user_state(self.alice)
        self.assertEqual(stored["creators"][0]["watched"], ["a"])

    def test_cast_slot_is_per_device_and_history_per_user(self):
        self.online(ALICE_DEVICE)
        status, answer = self.call("alice", "POST", "/api/cast",
                                   {"url": "https://www.bilibili.com/video/BV1aZpw6DEKJ"})
        self.assertTrue(answer["ok"])
        self.assertEqual(events.read_event(self.files(ALICE_DEVICE).events).action, "cast")
        self.assertEqual(self.stream(f"/ch/{ALICE_DEVICE}/cast"), 200)
        self.assertEqual(self.stream(f"/ch/{BOB_DEVICE}/cast"), 404)
        self.assertEqual(self.accounts.load_device_state(ALICE_DEVICE)["cast"]["video"]["bvid"], "BV1aZpw6DEKJ")
        self.assertEqual(len(self.call("alice", "GET", "/api/state")[1]["history"]), 1)
        self.assertEqual(self.call("bob", "GET", "/api/state")[1]["history"], [])

    def test_groups_are_per_user(self):
        status, answer = self.call("alice", "POST", "/api/groups", {"channel": "ch001", "group": "音乐"})
        self.assertTrue(answer["ok"])
        self.assertEqual(json.loads(self.files(ALICE_DEVICE).groups.read_text(encoding="utf-8")), {"ch001": "音乐"})
        self.assertEqual(json.loads(self.files(BOB_DEVICE).groups.read_text(encoding="utf-8")), {})
        groups = {g["name"] for g in self.call("bob", "GET", "/api/state")[1]["groups"]}
        self.assertNotIn("音乐", groups)

    def test_shared_source_cache(self):
        for who in ("alice", "bob"):
            self.assertTrue(self.call(who, "POST", "/api/creators", {"url": "https://space.bilibili.com/42"})[1]["ok"])
        self.now += relay_state.LIST_MAX_AGE_S + 1
        self.relay.refresh_if_stale("up01", self.relay.user_state(self.alice))
        self.assertEqual(self.client.upload_calls, 1)
        self.relay.refresh_if_stale("up01", self.relay.user_state(self.bob))
        self.assertEqual(self.client.upload_calls, 1, "Bob's list came from the shared cache")
        self.assertEqual(self.relay.user_state(self.bob).get("up01").videos[0].bvid, "new")

    # ---- guests ----

    def guest_cookie(self, device):
        code = self.accounts.device(device)["remote_code"]
        status, headers, body = self.raw("POST", "/api/scan", None, {"code": code})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        return headers["Set-Cookie"].split(";")[0]

    def test_guest_controls_one_device_and_edits_nothing(self):
        self.online(ALICE_DEVICE)
        guest = self.guest_cookie(ALICE_DEVICE)
        status, state = self.call(guest, "GET", "/api/state")
        self.assertEqual(state["me"]["role"], "guest")
        self.assertEqual([d["id"] for d in state["devices"]], [ALICE_DEVICE])
        self.assertTrue(self.call(guest, "POST", "/api/switch", {"channel": "ch001"})[1]["ok"])
        self.assertEqual(events.read_event(self.files(ALICE_DEVICE).events).channel, "ch001")
        for method, path, body in (("POST", "/api/creators", {"url": "https://space.bilibili.com/42"}),
                                   ("POST", "/api/groups", {"channel": "ch001", "group": "音乐"}),
                                   ("POST", "/api/code", {}),
                                   ("GET", "/api/devices", None),
                                   ("POST", "/api/switch", {"channel": "ch001", "device": BOB_DEVICE})):
            self.assertEqual(self.call(guest, method, path, body)[0], 404, path)

    def test_owner_scanning_keeps_their_session(self):
        code = self.accounts.device(ALICE_DEVICE)["remote_code"]
        status, headers, body = self.raw("POST", "/api/scan", self.cookies["alice"], {"code": code})
        answer = json.loads(body)
        self.assertTrue(answer["ok"])
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertIn(self.cookies["alice"][4:], answer["redirect"])
        # The remote's 扫一扫 switches to the scanned device and names it.
        self.assertEqual(answer["name"], self.accounts.device(ALICE_DEVICE)["name"])
        state = self.call("alice", "GET", "/api/state")[1]
        self.assertFalse(state["auto"])
        self.assertEqual([d["id"] for d in state["devices"] if d["target"]], [ALICE_DEVICE])

    def test_another_user_scanning_is_told_not_downgraded(self):
        code = self.accounts.device(ALICE_DEVICE)["remote_code"]
        status, headers, body = self.raw("POST", "/api/scan", self.cookies["bob"], {"code": code})
        answer = json.loads(body)
        self.assertFalse(answer["ok"])
        self.assertTrue(answer["not_mine"], "the remote explains it is someone else's guest code")
        self.assertNotIn("name", answer, "and does not say whose")
        self.assertIsNone(headers.get("Set-Cookie"))
        stale = json.loads(self.raw("POST", "/api/scan", self.cookies["bob"], {"code": "A" * 20})[2])
        self.assertFalse(stale["ok"])
        self.assertNotIn("not_mine", stale, "an unknown code is not someone else's")

    def test_new_code_rotates_one_device_and_ends_only_its_guests(self):
        guest = self.guest_cookie(ALICE_DEVICE)
        bob_url = self.files(BOB_DEVICE).remote.read_text()
        old_url = self.files(ALICE_DEVICE).remote.read_text()
        self.assertTrue(self.call("alice", "POST", "/api/code")[1]["ok"])
        self.assertEqual(self.call(guest, "GET", "/api/state")[0], 404)
        self.assertEqual(self.call("alice", "GET", "/api/state")[0], 200)
        self.assertNotEqual(self.files(ALICE_DEVICE).remote.read_text(), old_url)
        self.assertEqual(self.files(BOB_DEVICE).remote.read_text(), bob_url)

    # ---- sign-in, invites, binding ----

    def test_key_link_signs_in_as_admin(self):
        status, headers, _ = self.raw("GET", f"/tv/?k={KEY}")
        self.assertEqual(status, 302)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.call(cookie, "GET", "/api/state")[1]["me"]["role"], "admin")

    def test_invite_once(self):
        status, answer = self.call("admin", "POST", "/api/invites", {"name": "妈妈"})
        self.assertTrue(answer["link"].startswith("https://example.com/tv/i/"))
        code = answer["code"]
        self.assertEqual(self.call("alice", "POST", "/api/invites", {})[0], 404, "users cannot invite")
        self.assertEqual(self.raw("GET", f"/i/{code}")[0], 200, "the landing page needs no sign-in")
        self.assertEqual(json.loads(self.raw("GET", f"/api/invite/{code}")[2])["name"], "妈妈")
        status, headers, body = self.raw("POST", "/api/invite/accept", None, {"code": code, "name": "妈妈"})
        self.assertTrue(json.loads(body)["ok"])
        cookie = headers["Set-Cookie"].split(";")[0]
        state = self.call(cookie, "GET", "/api/state")[1]
        self.assertEqual((state["me"]["name"], state["devices"]), ("妈妈", []))
        again = json.loads(self.raw("POST", "/api/invite/accept", None, {"code": code, "name": "x"})[2])
        self.assertFalse(again["ok"])

    def test_bind_a_new_device(self):
        row = self.accounts.register_device("ccccccccccc3", "ai-passport", SECRET)
        claim = row["claim_code"]
        self.assertEqual(self.raw("GET", f"/b/{claim}")[0], 200)
        self.assertEqual(self.call("alice", "GET", f"/api/bind/{claim}")[1], {"ok": True, "model": "ai-passport",
                                                                              "tail": "CCC3"})
        spaced = f"{claim[:5]} {claim[5:10]} {claim[10:15]}-{claim[15:]}"
        answer = self.call("alice", "POST", "/api/bind", {"claim": spaced.lower()})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.accounts.owner("ccccccccccc3"), self.alice)
        self.assertTrue(self.files("ccccccccccc3").channels.is_file(), "its table is written at once")
        self.assertIn("/TV/R/", self.files("ccccccccccc3").remote.read_text())
        self.assertFalse(self.call("bob", "POST", "/api/bind", {"claim": claim})[1]["ok"])
        ids = [d["id"] for d in self.call("alice", "GET", "/api/state")[1]["devices"]]
        self.assertEqual(ids, [ALICE_DEVICE, "ccccccccccc3"])

    def test_unbind_and_repair(self):
        self.assertTrue(self.call("alice", "POST", f"/api/devices/{ALICE_DEVICE}/repair")[1]["ok"])
        self.assertIsNotNone(self.accounts.device(ALICE_DEVICE)["repair_until"])
        self.assertEqual(self.call("alice", "POST", f"/api/devices/{ALICE_DEVICE}", {"name": "客厅"})[1]["name"], "客厅")
        self.assertTrue(self.call("alice", "POST", f"/api/devices/{ALICE_DEVICE}/unbind")[1]["ok"])
        row = self.accounts.device(ALICE_DEVICE)
        self.assertEqual(row["status"], "unclaimed")
        self.assertTrue(row["claim_code"])
        self.assertEqual(self.call("alice", "GET", "/api/state")[1]["devices"], [])
        self.assertEqual(self.stream(f"/ch/{ALICE_DEVICE}/cast"), 404)


if __name__ == "__main__":
    unittest.main()
