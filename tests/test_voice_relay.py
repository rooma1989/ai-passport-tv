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
from server.relay import events, voice, web
from server.relay.state import State
from server.voice.relay_client import RelayClient
from test_relay_web import KEY, FakeClient


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

    def test_forced_device_does_not_leak_to_other_threads(self):
        seen = []
        with voice.acting_for(self.relay.devices[1]):
            self.assertEqual(self.relay.target()["name"], "Passport")
            other = threading.Thread(target=lambda: seen.append(self.relay.target()["name"]))
            other.start()
            other.join()
        self.assertEqual(seen, ["小智"])
        self.assertEqual(self.relay.target()["name"], "小智")

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


if __name__ == "__main__":
    unittest.main()
