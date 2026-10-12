"""The relay side of voice: actions carry an explicit device, and a short catalogue for the
media server. Two devices, as on the cloud host: the phone follows the Xingzhi while the
Passport is the one speaking."""

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
from server.relay import events, web
from server.relay.state import State
from server.relay.tenants import Caller
from server.tenancy import files as device_files
from server.tenancy.accounts import Accounts
from server.voice.relay_client import RelayClient
from test_relay_web import KEY, FakeClient

ADMIN_DEVICE = "legacy-passport"
BOB_DEVICE = "bbbbbbbbbbb2"
SECRET = "s" * 43


class VoiceRelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.xz_now, self.xz_events = root / "xz-now.json", root / "xz-events.json"
        self.pp_now, self.pp_events = root / "pp-now.json", root / "pp-events.json"
        main = root / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | 湖南卫视 | https://x/2.m3u8\n",
                        encoding="utf-8")
        self.relay = web.Relay(State(root / "state.json"), FakeClient(), root / "relay.txt", "/opt/ff", KEY,
                               events_out=self.xz_events, now_playing_in=self.xz_now,
                               main_channels=main, device_name="小智",
                               extra_devices=[("Passport", self.pp_now, self.pp_events)],
                               log=lambda _m: None, clock=lambda: self.now)
        self.relay.add_series("https://www.bilibili.com/bangumi/play/ss7")
        self.relay.add_creator("https://space.bilibili.com/42")
        # Both online; the Xingzhi changed channel last, so the phone (target()) follows it.
        now_playing.write(self.pp_now, "ch000", True, True, now=self.now, since=self.now - 60)
        now_playing.write(self.xz_now, "ch001", True, True, now=self.now, since=self.now - 5)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        threading.Thread(target=self.manage.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.manage.server_address[1]}"

    def tearDown(self):
        self.manage.shutdown()
        self.manage.server_close()
        self.streams.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"X-Key": KEY, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_phone_target_is_the_xingzhi(self):
        self.assertEqual(self.relay.target()["name"], "小智")

    def test_episode_with_device_goes_to_that_device(self):
        status, answer = self.call("POST", "/api/series/sr01/play", {"episode": 2, "device": "Passport"})
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(events.read_event(self.pp_events).channel, "sr01")
        self.assertIsNone(events.read_event(self.xz_events), "the phone's device is untouched")
        self.assertEqual(self.relay.state.episode("sr01"), 2)

    def test_without_device_the_phone_rule_still_applies(self):
        self.call("POST", "/api/series/sr01/play", {"episode": 2})
        self.assertEqual(events.read_event(self.xz_events).channel, "sr01")
        self.assertIsNone(events.read_event(self.pp_events))

    def test_device_by_id(self):
        self.call("POST", "/api/switch", {"channel": "ch001", "device": "d1"})
        self.assertEqual(events.read_event(self.pp_events).channel, "ch001")

    def test_unknown_device_is_refused(self):
        status, answer = self.call("POST", "/api/pause", {"device": "Nobody"})
        self.assertEqual((status, answer["ok"]), (404, False))
        status, answer = self.call("POST", "/api/pause", {"device": 3})
        self.assertEqual(status, 404)

    def test_target_keeps_its_own_meaning(self):
        status, answer = self.call("POST", "/api/target", {"device": "d1"})
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(self.relay.target()["name"], "Passport")

    def test_nested_calls_stay_on_the_named_device(self):
        # previous() goes through play_episode(): it must keep acting on the Passport, not fall
        # back to target() (the phone's Xingzhi, on another channel, which would get a switch).
        now_playing.write(self.pp_now, "sr01", True, True, now=self.now, since=self.now - 60)
        second = self.relay.state.get("sr01").videos[1]
        self.relay._bridge[("", "sr01")] = {"video": second, "target": 10.0, "at": self.now}
        status, answer = self.call("POST", "/api/previous", {"device": "Passport"})
        self.assertEqual((status, answer["ok"], answer.get("cut")), (200, True, True))
        self.assertEqual(self.relay.state.episode("sr01"), 1)
        self.assertIsNone(events.read_event(self.xz_events), "the phone's device is untouched")

    def test_empty_device_means_the_phone_rule(self):
        self.call("POST", "/api/switch", {"channel": "ch000", "device": ""})
        self.assertEqual(events.read_event(self.xz_events).channel, "ch000")

    def test_pause_on_a_live_channel_answers_for_that_device(self):
        status, answer = self.call("POST", "/api/pause", {"device": "Passport"})
        self.assertEqual(answer, {"ok": False, "error": "现在没有在播视频"})

    def test_catalog(self):
        now_playing.write(self.pp_now, "sr01", True, True, now=self.now, since=self.now - 60)
        status, answer = self.call("GET", "/api/voice/catalog?device=Passport")
        self.assertEqual(status, 200)
        self.assertEqual(answer["device"], {"id": "d1", "name": "Passport", "online": True})
        self.assertEqual(answer["series"], [{"id": "sr01", "name": "番剧ss7", "total": 3, "next": 1}])
        self.assertEqual(answer["creators"], [{"id": "up01", "name": "UP42"}])
        self.assertEqual(answer["playing"]["channel"], "sr01")
        self.assertFalse(answer["playing"]["paused"])
        self.assertEqual(self.call("GET", "/api/voice/catalog?device=x")[1]["ok"], False)

    def test_catalog_needs_the_key(self):
        request = urllib.request.Request(self.base + "/api/voice/catalog?device=Passport")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_media_server_client(self):
        client = RelayClient(self.base, KEY, "Passport")
        self.assertEqual(client.catalog()["device"]["id"], "d1")
        self.assertTrue(client.play_episode("sr01", 3)["ok"])
        self.assertEqual(events.read_event(self.pp_events).channel, "sr01")
        self.assertEqual(client.previous()["ok"], False)
        self.assertEqual(client.next(), {"ok": False, "error": "现在没有在播视频"})
        self.assertEqual(client.seek(delta=30)["ok"], False)
        self.assertEqual(client.resume(), {"ok": False, "error": "现在没有暂停"})
        wrong = RelayClient(self.base, "wrong-key-0123456789", "Passport")
        with self.assertRaises(Exception):
            wrong.catalog()


class TenantVoiceRelayTests(unittest.TestCase):
    """Multi-user relay (--db): the media server holds the relay key, which is the admin, but a
    device it serves may belong to anyone. A keyed request that names a device acts as that
    device's guest: that one device, its owner's series, no account data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.accounts = Accounts(root / "tv.db", clock=lambda: self.now)
        self.admin = self.accounts.create_user("管理员", "admin")
        self.bob = self.accounts.create_user("Bob")
        row = self.accounts.register_device(BOB_DEVICE, "xingzhi-cube-154", SECRET)
        self.accounts.bind(row["claim_code"], self.bob)
        self.accounts.add_legacy_device(ADMIN_DEVICE, 8097, "ai-passport", "AI Passport", self.admin)
        self.devices_dir = root / "devices"
        main = root / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | 湖南卫视 | https://x/2.m3u8\n",
                        encoding="utf-8")
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, main_channels=main,
                               accounts=self.accounts, devices_dir=self.devices_dir,
                               log=lambda _m: None, clock=lambda: self.now)
        self.relay.add_series("https://www.bilibili.com/bangumi/play/ss7", caller=Caller(user=self.bob, role="user"))
        self.relay.sync_devices(force=True)
        for device in (ADMIN_DEVICE, BOB_DEVICE):
            now_playing.write(self.files(device).now, "ch000", True, True, now=self.now, since=self.now - 60)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        threading.Thread(target=self.manage.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.manage.server_address[1]}"

    def tearDown(self):
        self.manage.shutdown()
        self.manage.server_close()
        self.streams.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def files(self, device):
        return device_files.device_files(self.devices_dir, device)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers=dict({"X-Key": KEY, "Content-Type": "application/json"},
                                                      **(headers or {})))
        try:
            with urllib.request.urlopen(request, timeout=5) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as error:
            return error.code, None

    def test_keyed_switch_reaches_the_named_device_whoever_owns_it(self):
        status, answer = self.call("POST", "/api/switch", {"channel": "ch001", "device": BOB_DEVICE})
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(events.read_event(self.files(BOB_DEVICE).events).channel, "ch001")
        self.assertIsNone(events.read_event(self.files(ADMIN_DEVICE).events))

    def test_keyed_without_device_is_still_the_admin(self):
        self.call("POST", "/api/switch", {"channel": "ch001"})
        self.assertEqual(events.read_event(self.files(ADMIN_DEVICE).events).channel, "ch001")
        self.assertIsNone(events.read_event(self.files(BOB_DEVICE).events))

    def test_names_are_not_ids_and_unknown_devices_are_404(self):
        self.assertEqual(self.call("POST", "/api/pause", {"device": "Bob"})[0], 404)
        self.assertEqual(self.call("POST", "/api/pause", {"device": "cccccccccccc"})[0], 404)
        self.assertEqual(self.call("GET", "/api/voice/catalog?device=Bob")[1]["ok"], False)

    def test_device_scope_reaches_no_account_data(self):
        # Not a single-device endpoint: the key stays the admin, who cannot see Bob's device.
        status, _ = self.call("POST", "/api/creators", {"url": "https://space.bilibili.com/42",
                                                         "device": BOB_DEVICE})
        self.assertEqual(status, 404)
        self.assertEqual(self.relay.user_state(self.bob).get("up01"), None)

    def test_a_phone_cannot_borrow_the_device_scope(self):
        bob = "tvs=" + self.accounts.new_session(self.bob)
        admin = "tvs=" + self.accounts.new_session(self.admin)
        request = urllib.request.Request(self.base + "/api/switch", method="POST",
                                         data=json.dumps({"channel": "ch001", "device": BOB_DEVICE}).encode(),
                                         headers={"Cookie": admin, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 404, "the admin's phone does not control Bob's TV")
        self.assertIsNone(events.read_event(self.files(BOB_DEVICE).events))
        # Bob's own phone may name his device, as before; the key is what a phone never has.
        request = urllib.request.Request(self.base + "/api/switch", method="POST",
                                         data=json.dumps({"channel": "ch001", "device": BOB_DEVICE}).encode(),
                                         headers={"Cookie": bob, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as answer:
            self.assertTrue(json.loads(answer.read())["ok"])

    def test_catalog_is_the_owners(self):
        status, answer = self.call("GET", f"/api/voice/catalog?device={BOB_DEVICE}")
        self.assertEqual(status, 200)
        self.assertEqual((answer["device"]["id"], answer["device"]["online"]), (BOB_DEVICE, True))
        self.assertEqual([s["id"] for s in answer["series"]], ["sr01"])
        status, answer = self.call("GET", f"/api/voice/catalog?device={ADMIN_DEVICE}")
        self.assertEqual(answer["series"], [], "the admin has no series")

    def test_media_server_client_per_device(self):
        factory = lambda device: RelayClient(self.base, KEY, device)
        client = factory(BOB_DEVICE)
        self.assertEqual(client.catalog()["device"]["id"], BOB_DEVICE)
        self.assertTrue(client.play_episode("sr01", 2)["ok"])
        self.assertEqual(events.read_event(self.files(BOB_DEVICE).events).channel, "sr01")
        self.assertEqual(self.relay.user_state(self.bob).episode("sr01"), 2)
        self.assertIsNone(events.read_event(self.files(ADMIN_DEVICE).events))
        self.assertEqual(client.resume(), {"ok": False, "error": "现在没有暂停"})


if __name__ == "__main__":
    unittest.main()
