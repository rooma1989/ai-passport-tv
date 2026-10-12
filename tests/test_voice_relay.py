"""The relay side of voice: actions carry an explicit device, and a short catalogue for the
media server. Two devices, as on the cloud host: the phone follows the Xingzhi while the
Passport is the one speaking."""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import channel_groups, live, now_playing
from server.relay import events, voice, web
from server.relay.bilibili import BilibiliError, SourceError
from server.relay.state import MAX_CREATORS, State, Video
from server.relay.tenants import Caller
from server.tenancy import files as device_files
from server.tenancy.accounts import Accounts
from server.voice.relay_client import RelayClient
from server.voice.session import VoiceService
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
        self.assertEqual(answer["series"], [{"id": "sr01", "name": "番剧ss7", "site": "bilibili",
                                             "total": 3, "next": 1}])
        self.assertEqual(answer["creators"], [{"id": "up01", "name": "UP42", "site": "bilibili"}])
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


class SearchClient(FakeClient):
    """FakeClient plus the voice search, the way bilibili.Bilibili answers it."""

    def __init__(self):
        super().__init__()
        self.searches = []
        self.error = None
        self.creator_delay = 0.0

    def search(self, query, limit=20, timeout=4.0):
        self.searches.append(("video", query, timeout))
        if self.error:
            raise self.error
        return [{"id": f"BV1{n:09d}", "title": f"{query} {n}", "uploader": "猫咪", "duration": 60 + n}
                for n in range(8)]

    def search_creators(self, query, limit=20, timeout=4.0):
        self.searches.append(("up", query, timeout))
        if self.error:
            raise self.error
        return [{"id": "546195", "name": "老番茄", "fans": 20840107, "videos": 680, "latest": None}]

    def creator(self, mid):
        time.sleep(self.creator_delay)
        return super().creator(mid)


class FakeYouTube:
    def __init__(self):
        self.asked = []
        self.searches = []
        # reason of a SourceError to raise from the channel search ("slow" as when the node takes
        # longer than the relay waits), or None
        self.failing = None

    def search_creators(self, query, limit=12, timeout=5.0):
        self.searches.append(query)
        if self.failing:
            raise SourceError("youtube /yt/search: timed out", reason=self.failing)
        return [{"id": "UCSJ4gkVC6NrvII8umztf0Ow", "name": "Lofi Girl", "fans": 0, "videos": 0, "latest": None}]

    def creator(self, channel):
        self.asked.append(channel)
        return "Lofi Girl", [Video(bvid="n61ULEU7CO0", title="1 A.M Study Session", duration=3615, pubdate=0,
                                   site="youtube")]

    def uploads(self, channel):
        return self.creator(channel)[1]


class VoiceSearchRelayTests(unittest.TestCase):
    """GET /api/voice/search, POST /api/voice/cast and /api/voice/creator: always for the named
    device (the Passport here, while the phone follows the Xingzhi)."""

    setUp = VoiceRelayTests.setUp
    tearDown = VoiceRelayTests.tearDown
    call = VoiceRelayTests.call

    def client(self) -> SearchClient:
        client = SearchClient()
        self.relay.clients["bilibili"] = client
        return client

    def search(self, query="猫和老鼠", site="bilibili", kind="video", device="Passport", **extra):
        params = urllib.parse.urlencode(dict(device=device, site=site, kind=kind, q=query, **extra))
        return self.call("GET", f"/api/voice/search?{params}")[1]

    def test_search_videos_and_creators(self):
        client = self.client()
        answer = self.search()
        self.assertEqual((answer["ok"], answer["site"], answer["kind"]), (True, "bilibili", "video"))
        self.assertEqual(answer["results"][0], {"id": "BV1000000000", "title": "猫和老鼠 0", "uploader": "猫咪",
                                                "duration": 60})
        answer = self.search("老番茄", kind="up")
        self.assertEqual(answer["results"][0]["name"], "老番茄")
        self.assertEqual([k for k, _, _ in client.searches], ["video", "up"])

    def test_search_is_cached_briefly(self):
        client = self.client()
        self.search()
        self.search("  猫和老鼠 ")
        self.assertEqual(len(client.searches), 1)
        self.now += voice.SEARCH_CACHE_S + 1
        self.search()
        self.assertEqual(len(client.searches), 2)

    def test_search_waits_as_asked(self):
        client = self.client()
        for n, (wait, used) in enumerate((("2.5", 2.5), ("0.1", voice.WAIT_MIN_S), ("99", voice.WAIT_MAX_S),
                                          ("nan", voice.WAIT_DEFAULT_S), ("x", voice.WAIT_DEFAULT_S))):
            self.search(f"词{n}", wait=wait)
            self.assertEqual(client.searches[-1][2], used, wait)

    def test_search_failures(self):
        client = self.client()
        client.error = BilibiliError("timed out", reason="slow")
        self.assertEqual(self.search(), {"ok": False, "error": "B站搜索太慢了，稍后再试", "reason": "slow"})
        client.error = BilibiliError("code=-412", permanent=True)
        self.assertEqual(self.search("别的")["error"], "B站没回应，稍后再试")
        self.assertEqual(self.search(site="youtube")["error"], "YouTube 还没配置好")
        for bad in (dict(site="tiktok"), dict(kind="playlist"), dict(query="   ")):
            self.assertEqual(self.search(**bad)["error"], "请求格式不对", bad)
        self.assertEqual(self.search(device="Nobody")["error"], "没有这台设备")

    def test_cast_a_result_on_the_named_device(self):
        status, answer = self.call("POST", "/api/voice/cast", {"device": "Passport", "site": "bilibili",
                                                               "id": "BV1yi7rzDEWc", "title": "经典欣赏",
                                                               "duration": 102243})
        self.assertEqual((status, answer["ok"], answer["title"]), (200, True, "经典欣赏"))
        self.assertEqual(events.read_event(self.pp_events).action, "cast")
        self.assertIsNone(events.read_event(self.xz_events), "the phone's device is untouched")
        self.assertEqual((self.relay.state.cast.bvid, self.relay.state.cast.duration), ("BV1yi7rzDEWc", 102243))
        # Without a title the relay asks the site, like the phone's cast.
        answer = self.call("POST", "/api/voice/cast", {"device": "Passport", "site": "bilibili",
                                                       "id": "BV1EhAaeXEYu"})[1]
        self.assertEqual(answer["title"], "tBV1EhAaeXEYu")
        for bad in ({"site": "bilibili", "id": "BV1"}, {"site": "youtube", "id": "BV1yi7rzDEWc"},
                    {"site": "bilibili", "id": 3}):
            answer = self.call("POST", "/api/voice/cast", dict(bad, device="Passport"))[1]
            self.assertFalse(answer["ok"], bad)

    def test_follow_a_creator(self):
        self.client()
        body = {"device": "Passport", "site": "bilibili", "id": "546195", "name": "老番茄", "action": "follow"}
        answer = self.call("POST", "/api/voice/creator", body)[1]
        self.assertEqual(answer, {"ok": True, "creator": {"id": "up02", "name": "UP546195"}})
        self.assertEqual(self.relay.state.get("up02").source, "546195")
        self.assertIn("up02", (Path(self.tmp.name) / "relay.txt").read_text(encoding="utf-8"))
        self.assertEqual(self.call("POST", "/api/voice/creator", body)[1],
                         {"ok": False, "error": "已经订阅过「老番茄」了"})
        # The creator from setUp (mid 42) too.
        answer = self.call("POST", "/api/voice/creator", dict(body, id="42", name="UP42"))[1]
        self.assertEqual(answer["error"], "已经订阅过「UP42」了")

    def test_follow_respects_the_limits(self):
        self.client()
        body = {"device": "Passport", "site": "bilibili", "name": "x", "action": "follow"}
        for mid in range(100, 100 + MAX_CREATORS - 1):
            self.assertTrue(self.call("POST", "/api/voice/creator", dict(body, id=str(mid)))[1]["ok"], mid)
        answer = self.call("POST", "/api/voice/creator", dict(body, id="999"))[1]
        self.assertEqual(answer, {"ok": False, "error": f"最多只能订阅 {MAX_CREATORS} 个 UP 主"})
        self.relay.state.remove("up05")
        self.relay.room_of = lambda state, extra="": {"fits": False}
        answer = self.call("POST", "/api/voice/creator", dict(body, id="999"))[1]
        self.assertEqual(answer, {"ok": False, "error": "频道列表满了，先删掉几个台"})

    def test_follow_a_youtube_channel(self):
        yt = self.relay.clients["youtube"] = FakeYouTube()
        answer = self.call("POST", "/api/voice/creator", {"device": "Passport", "site": "youtube",
                                                          "id": "UCSJ4gkVC6NrvII8umztf0Ow", "name": "Lofi Girl",
                                                          "action": "follow"})[1]
        self.assertTrue(answer["ok"])
        self.assertEqual(yt.asked, ["https://www.youtube.com/channel/UCSJ4gkVC6NrvII8umztf0Ow"])
        self.assertEqual(self.relay.state.get(answer["creator"]["id"]).site, "youtube")
        bad = self.call("POST", "/api/voice/creator", {"device": "Passport", "site": "youtube", "id": "@LofiGirl",
                                                       "name": "x", "action": "follow"})[1]
        self.assertEqual(bad["error"], "请求格式不对")

    def test_newest_upload(self):
        self.client()
        answer = self.call("POST", "/api/voice/creator", {"device": "Passport", "site": "bilibili", "id": "77",
                                                          "name": "x", "action": "latest"})[1]
        self.assertEqual((answer["ok"], answer["title"]), (True, "tnew"))
        self.assertEqual(events.read_event(self.pp_events).action, "cast")
        self.assertIsNone(events.read_event(self.xz_events))

    def test_slow_follow_finishes_in_the_background(self):
        client = self.client()
        client.creator_delay = 1.5
        answer = self.call("POST", "/api/voice/creator", {"device": "Passport", "site": "bilibili", "id": "77",
                                                          "name": "x", "action": "follow", "wait": 1})[1]
        self.assertEqual(answer, {"ok": True, "pending": True})
        self.assertIsNone(self.relay.state.get("up02"))
        deadline = time.monotonic() + 5
        while self.relay.state.get("up02") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.relay.state.get("up02").source, "77")

    def test_media_server_client(self):
        self.client()
        client = RelayClient(self.base, KEY, "Passport")
        answer = client.search("bilibili", "猫和老鼠", 3.0, "video")
        self.assertEqual(len(answer["results"]), 8)
        self.assertTrue(client.cast_video("bilibili", "BV1000000003", "猫和老鼠 3", 63)["ok"])
        self.assertEqual(self.relay.state.cast.title, "猫和老鼠 3")
        answer = client.creator("bilibili", "546195", "老番茄", "follow", 3.0)
        self.assertEqual(answer["creator"]["name"], "UP546195")
        self.assertEqual(client.creator("bilibili", "546195", "老番茄", "follow", 3.0)["ok"], False)


class FakeAsr:
    def __init__(self):
        self.text = ""

    def transcribe(self, pcm, rate):
        return self.text


def device_config(table_text: str, main: Path) -> dict:
    """The CONFIG channel list a media server builds from the relay's table and its own."""
    channels, labels, _ = live.parse_channels(table_text + "\n" + main.read_text(encoding="utf-8"))
    groups = list(channel_groups.GROUPS)
    return {"groups": groups, "channel_list": [
        {"id": cid, "name": labels[cid], "g": groups.index(channel_groups.classify(cid, labels[cid]))}
        for cid in channels]}


class EndToEnd:
    """The media server's voice service against the real relay over HTTP: two-site creator search
    with every row labelled, and channels matched by their bare names although the device's list
    carries the site marks. Run in both relay modes (subclasses give relay_device and table_text)."""

    def voice(self):
        from server.protocol import Kind, Packet, json_bytes
        from test_voice_session import tone_blocks
        asr = FakeAsr()
        service = VoiceService(asr, RelayClient(self.base, KEY, self.relay_device), lambda _m: None,
                               threaded=False)
        config = device_config(self.table_text(), self.main)
        uid = [0]

        def say(text):
            asr.text = text
            uid[0] += 1
            ctx = lambda: {"config": config, "current": "ch000", "device": {"volume": 50}}
            service.take(Packet(Kind.VOICE_CTL, 1, 0, 0, json_bytes({"op": "begin", "id": uid[0], "rate": 16000,
                                                                     "codec": "adpcm"})), ctx)
            service.take(Packet(Kind.VOICE, 1, 0, 0, tone_blocks(1)[0][0]), ctx)
            service.take(Packet(Kind.VOICE_CTL, 1, 0, 0, json_bytes({"op": "end", "id": uid[0]})), ctx)
            return json.loads(service.results()[0])

        return say, config

    def test_two_site_creator_search_end_to_end(self):
        self.relay.clients["bilibili"] = SearchClient()
        youtube = self.relay.clients["youtube"] = FakeYouTube()
        say, _ = self.voice()
        result = say("搜UP主老番茄")
        self.assertEqual(result["reply"], "找到 2 个UP 主：B站 1 个，油管 1 个")
        self.assertEqual([c["label"] for c in result["choices"]], ["B站 · 老番茄 · 2084万粉", "油管 · Lofi Girl"])
        # YouTube too slow for this ask: Bilibili's rows, and one to search YouTube again.
        youtube.failing = "slow"
        result = say("搜UP主李子柒")
        self.assertEqual(result["reply"], "找到 1 个UP 主：B站 1 个，油管还在搜")
        self.assertEqual([c["label"] for c in result["choices"]], ["B站 · 老番茄 · 2084万粉", "油管还在搜 · 再搜一次"])
        youtube.failing = None
        result = say("再搜一次")
        self.assertEqual([c["label"] for c in result["choices"]], ["B站 · 老番茄 · 2084万粉", "油管 · Lofi Girl"])
        self.assertEqual(youtube.searches, ["老番茄", "李子柒", "李子柒"])

    def test_bare_names_match_marked_channels(self):
        _, config = self.voice()
        names = {row["id"]: row["name"] for row in config["channel_list"]}
        self.assertEqual(names["sr01"], "番剧ss7(B站)")
        wang = next(cid for cid, name in names.items() if name.startswith("王志安"))
        self.assertEqual(names[wang], "王志安(油管)")
        self.assertEqual(names["ch001"], "湖南卫视", "live channels unchanged")
        say, _ = self.voice()
        result = say("王志安")
        self.assertEqual((result.get("do"), result.get("label")), ({"channel": wang}, "王志安"))
        self.assertEqual(say("番剧ss7第2集")["intent"], "episode")
        listed = say("有哪些YouTube频道")
        self.assertEqual([c["label"] for c in listed["choices"]], ["王志安"])
        listed = say("有哪些UP主")
        self.assertIn("王志安(油管)", [c["label"] for c in listed["choices"]])


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

    def test_device_scoped_search_and_follow_are_the_owners(self):
        self.relay.clients["bilibili"] = SearchClient()
        status, answer = self.call("GET", f"/api/voice/search?device={BOB_DEVICE}&site=bilibili&kind=up&q=x")
        self.assertEqual((status, answer["ok"]), (200, True))
        status, answer = self.call("POST", "/api/voice/creator", {"device": BOB_DEVICE, "site": "bilibili",
                                                                  "id": "546195", "name": "老番茄",
                                                                  "action": "follow"})
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(self.relay.user_state(self.bob).get(answer["creator"]["id"]).source, "546195")
        self.assertEqual([c.id for c in self.relay.user_state(self.admin).creators], [])
        status, answer = self.call("POST", "/api/voice/cast", {"device": BOB_DEVICE, "site": "bilibili",
                                                               "id": "BV1yi7rzDEWc", "title": "经典欣赏"})
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertEqual(events.read_event(self.files(BOB_DEVICE).events).action, "cast")
        self.assertIsNone(events.read_event(self.files(ADMIN_DEVICE).events))
        # Someone else's (or no) device is not reachable.
        self.assertEqual(self.call("POST", "/api/voice/creator", {"device": "cccccccccccc", "site": "bilibili",
                                                                  "id": "1", "action": "follow"})[0], 404)

    def test_media_server_client_per_device(self):
        factory = lambda device: RelayClient(self.base, KEY, device)
        client = factory(BOB_DEVICE)
        self.assertEqual(client.catalog()["device"]["id"], BOB_DEVICE)
        self.assertTrue(client.play_episode("sr01", 2)["ok"])
        self.assertEqual(events.read_event(self.files(BOB_DEVICE).events).channel, "sr01")
        self.assertEqual(self.relay.user_state(self.bob).episode("sr01"), 2)
        self.assertIsNone(events.read_event(self.files(ADMIN_DEVICE).events))
        self.assertEqual(client.resume(), {"ok": False, "error": "现在没有暂停"})


def add_wang(relay, state, now):
    """A YouTube creator, the way the phone or a voice follow adds one."""
    state.add("youtube", "https://www.youtube.com/channel/UC" + "w" * 22, "王志安",
              [Video(bvid="wangzhian01", title="w", duration=600, pubdate=0, site="youtube")], now)
    state.save()
    relay._tables_changed(state)


class LegacyEndToEndTests(EndToEnd, unittest.TestCase):
    relay_device = "Passport"

    def setUp(self):
        VoiceRelayTests.setUp(self)
        self.addCleanup(VoiceRelayTests.tearDown, self)
        self.main = Path(self.tmp.name) / "channels.txt"
        add_wang(self.relay, self.relay.state, self.now)

    def table_text(self):
        return (Path(self.tmp.name) / "relay.txt").read_text(encoding="utf-8")


class TenantEndToEndTests(EndToEnd, unittest.TestCase):
    relay_device = BOB_DEVICE

    def setUp(self):
        TenantVoiceRelayTests.setUp(self)
        self.addCleanup(TenantVoiceRelayTests.tearDown, self)
        self.main = Path(self.tmp.name) / "channels.txt"
        add_wang(self.relay, self.relay.user_state(self.bob), self.now)

    def files(self, device):
        return device_files.device_files(self.devices_dir, device)

    def table_text(self):
        return self.files(BOB_DEVICE).channels.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
