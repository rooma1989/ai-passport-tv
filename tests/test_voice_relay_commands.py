"""The relay's side of the second batch of voice commands (server/relay/voicecmds.py): each
device's channel history, sleep timer, cast history, and subscribing or removing channels,
always for the device that spoke. Run against both relay modes, as the media server calls them
(RelayClient over HTTP with the key and the device); the background tick is called by hand.

Also the media server's half of the sleep pause: a pause event with value 1 is a "sleep"
control that does not resume by itself after 30 minutes (server/cast_switch.py, tv_server.py)."""

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import now_playing
from server.cast_switch import SwitchWatcher
from server.relay import events, voicecmds, web
from server.relay.state import State, Video
from server.relay.tenants import Caller
from server.voice.relay_client import RelayClient
from test_relay_web import KEY, FakeClient
from test_voice_relay import BOB_DEVICE, TenantVoiceRelayTests, VoiceRelayTests

STATUS = {"volume": 50, "brightness": 1, "steps": 5}


class LinkClient(FakeClient):
    """FakeClient that also finds a video's uploader, as Bilibili's /view does."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def creator_id(self, text):
        self.asked.append(text)
        if "/video/BV" in text:
            return "777"
        return super().creator_id(text)


class LinkYouTube:
    def __init__(self):
        self.asked = []

    def creator_id(self, text):
        self.asked.append(text)
        return "https://www.youtube.com/channel/UC" + "y" * 22

    def creator(self, channel):
        return "Lofi Girl", [Video(bvid="n61ULEU7CO0", title="study", duration=3615, pubdate=0, site="youtube")]


class Proc:
    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True


class Commands:
    """The tests, for both relay modes. Subclasses set up: relay, now, base, device (what the
    media server sends), slot (the relay's stream slot), and the helpers below."""

    def client(self) -> RelayClient:
        return RelayClient(self.base, KEY, self.device)

    def play(self, cid, since=None, status=STATUS, online=True):
        now_playing.write(self.now_file(), cid, online, True, now=self.now,
                          since=self.now if since is None else since, device=status)

    def events(self):
        return events.read_events(self.events_file())

    def actions(self):
        return [(e.action, e.channel or e.value) for e in self.events()]

    def stream(self, cid, video, offset=0.0):
        """A stream the relay serves to the device right now (as relay.play() would record it)."""
        self.relay._active[(self.slot, cid)] = {"proc": Proc(), "video": video, "offset": offset,
                                                "began": time.monotonic(), "at": self.now, "abort": ""}

    def later(self, seconds, cid=None):
        """Time passes; the media server keeps refreshing its now-playing file."""
        self.now += seconds
        record = now_playing.read(self.now_file(), self.now - seconds)
        self.play(cid or record["channel"], since=record["since"], status=record["device"])

    def dev(self):
        return self.relay.find_device(self.device, self.voice_caller())

    # ---- channel history ----

    def test_channel_history(self):
        self.play("ch000")
        voicecmds.tick(self.relay)
        self.later(20, "ch001")
        voicecmds.tick(self.relay)
        self.assertEqual(self.client().catalog()["back"], ["ch000"])
        # Zapping through a channel for a few seconds does not make it "the last channel".
        self.later(30, "sr01")
        voicecmds.tick(self.relay)
        self.later(3, "ch000")
        voicecmds.tick(self.relay)
        self.assertEqual(self.client().catalog()["back"], ["ch001"])
        # The catalogue looks too (the media server asks it at every utterance).
        self.later(15, "ch017")
        self.assertEqual(self.client().catalog()["back"], ["ch000", "ch001"])
        # Offline: nothing recorded.
        self.play("up01", online=False)
        self.now += 60
        voicecmds.tick(self.relay)
        self.assertEqual(self.client().catalog()["back"], ["ch017", "ch000", "ch001"], "up01 never seen")

    def test_history_survives_a_relay_restart(self):
        self.play("ch001")
        voicecmds.tick(self.relay)
        self.later(30, "sr01")
        voicecmds.tick(self.relay)
        self.client().timer("set", 45)
        restarted = self.restart()
        rec = voicecmds._view(restarted, restarted.find_device(self.device, self.voice_caller()))
        self.assertEqual(voicecmds.back_channels(rec, "sr01"), ["ch001"])
        self.assertEqual(rec["timer"]["kind"], "time")

    # ---- timers ----

    def test_set_status_cancel_and_replace(self):
        self.play("ch001")
        client = self.client()
        self.assertEqual(client.timer("status"), {"ok": True, "timer": None})
        self.assertEqual(client.timer("set", 30), {"ok": True, "timer": {"kind": "time", "left": 1800}})
        self.assertEqual(client.timer("set", 10)["timer"]["left"], 600, "a second timer replaces the first")
        self.now += 125
        self.assertEqual(client.timer("status")["timer"], {"kind": "time", "left": 475})
        self.assertEqual(client.timer("cancel")["timer"]["left"], 475)
        self.assertEqual(client.timer("cancel"), {"ok": True, "timer": None})
        self.assertEqual(client.timer("set", 0)["ok"], False)
        self.assertIn("12 小时", client.timer("set", 721)["error"])
        self.assertEqual(client.timer("nap")["ok"], False)
        self.assertEqual(self.client().catalog()["timer"], None)

    def test_timer_on_a_relay_channel_pauses_like_the_phone(self):
        video = self.relay._state_of(self.dev()).get("up01").videos[0]
        self.play("up01")
        self.stream("up01", video, offset=100.0)
        self.client().timer("set", 5)
        self.later(299)
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions(), [], "not yet")
        self.later(2)
        voicecmds.tick(self.relay)
        listed = self.events()
        self.assertEqual([(e.action, e.value) for e in listed], [("brightness", 4), ("pause", 1)],
                         "dimmest backlight first, then the sleep pause")
        self.assertEqual(listed[-1].text, video.title)
        self.assertTrue(self.relay._paused[self.slot]["sleep"])
        self.assertEqual(self.relay.open_channel("up01", self.slot), 503, "no stream while asleep")
        self.assertTrue(self.client().catalog()["playing"]["paused"])
        self.assertIsNone(self.client().catalog()["timer"], "done")
        # Not resumed by itself after the phone pause's 30 minutes.
        self.later(web.PAUSE_MAX_S + 60)
        self.assertEqual(self.relay.open_channel("up01", self.slot), 503)
        # "Carry on": the backlight comes back, then the stream.
        self.assertTrue(self.client().resume()["ok"])
        self.assertEqual(self.actions()[-2:], [("brightness", 1), ("resume", None)])
        self.assertNotEqual(self.relay.open_channel("up01", self.slot), 503)

    def test_timer_on_a_live_channel(self):
        self.play("ch001")
        self.client().timer("set", 1)
        self.later(61)
        voicecmds.tick(self.relay)
        listed = self.events()
        self.assertEqual([(e.action, e.value) for e in listed], [("brightness", 4), ("pause", 1)])
        self.assertEqual(listed[-1].text, "湖南卫视")
        self.assertTrue(self.client().catalog()["playing"]["paused"])
        self.assertEqual(self.client().resume(), {"ok": True, "at": self.now})
        self.assertEqual(self.actions()[-2:], [("brightness", 1), ("resume", None)])
        self.assertEqual(self.client().resume(), {"ok": False, "error": "现在没有暂停"})

    def test_already_dim_and_old_firmware(self):
        self.play("ch001", status=dict(STATUS, brightness=4))
        self.client().timer("set", 1)
        self.later(61)
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions(), [("pause", 1)])
        self.client().resume()
        self.assertEqual(self.actions()[-1], ("resume", None), "nothing to restore")
        self.play("ch001", status=None)
        self.client().timer("set", 1)
        self.later(61)
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions()[-1], ("pause", 1))

    def test_switching_channel_wakes_and_restores_the_backlight(self):
        self.play("ch001")
        self.client().timer("set", 1)
        self.later(61)
        voicecmds.tick(self.relay)
        self.later(600, "ch000")
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions()[-1], ("brightness", 1))
        self.assertFalse(self.client().catalog()["playing"]["paused"])

    def test_a_new_session_ends_the_sleep_pause(self):
        """The device reconnected (or was switched off and on): it plays again, not stuck asleep."""
        video = self.relay._state_of(self.dev()).get("up01").videos[0]
        self.play("up01")
        self.stream("up01", video)
        self.client().timer("set", 1)
        self.later(61)
        voicecmds.tick(self.relay)
        self.assertEqual(self.relay.open_channel("up01", self.slot), 503)
        self.now += 3600
        self.play("up01", since=self.now)
        self.assertNotEqual(self.relay.open_channel("up01", self.slot), 503)
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions()[-1], ("brightness", 1))

    def test_phone_paused_before_the_timer(self):
        video = self.relay._state_of(self.dev()).get("up01").videos[0]
        self.play("up01")
        self.stream("up01", video)
        self.assertTrue(self.client().pause()["ok"])
        self.client().timer("set", 1)
        self.later(61)
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions()[-2:], [("brightness", 4), ("pause", 1)])
        self.later(web.PAUSE_MAX_S)
        self.assertEqual(self.relay.open_channel("up01", self.slot), 503, "now a sleep pause")

    def test_timer_dropped_when_offline_or_too_late(self):
        self.play("ch001")
        self.client().timer("set", 1)
        self.now += 61                      # the media server stopped writing: offline
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions(), [])
        self.assertIsNone(self.client().catalog()["timer"])
        self.play("ch001")
        self.client().timer("set", 1)
        self.later(60 + voicecmds.LATE_MAX_S + 5)   # the relay was down meanwhile
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions(), [])

    def test_end_of_episode(self):
        state = self.relay._state_of(self.dev())
        first, second = state.get("sr01").videos[:2]
        self.play("sr01")
        self.stream("sr01", first, offset=100.0)
        answer = self.client().timer("end")
        self.assertEqual(answer, {"ok": True, "timer": {"kind": "end", "what": "episode"}})
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions(), [])
        self.stream("sr01", first, offset=first.duration - 2.0)
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions(), [("brightness", 4), ("pause", 1)])

    def test_end_when_the_next_episode_started(self):
        state = self.relay._state_of(self.dev())
        first, second = state.get("sr01").videos[:2]
        self.play("sr01")
        self.stream("sr01", first)
        self.client().timer("end")
        self.stream("sr01", second)
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions()[-1], ("pause", 1))

    def test_end_of_a_cast_is_the_return(self):
        self.relay._cast_video(Video(bvid="BV1yi7rzDEWc", title="经典欣赏", duration=600, pubdate=0),
                               self.voice_caller(), self.dev())
        self.play("cast")
        self.assertEqual(self.client().timer("end")["timer"]["what"], "video")
        self.later(1, "ch001")          # the cast ended and the device went back
        voicecmds.tick(self.relay)
        self.assertEqual(self.actions()[-1], ("pause", 1))

    def test_end_dropped_by_a_manual_switch(self):
        video = self.relay._state_of(self.dev()).get("up01").videos[0]
        self.play("up01")
        self.stream("up01", video)
        self.client().timer("end")
        self.later(5, "ch001")
        voicecmds.tick(self.relay)
        self.assertNotIn(("pause", 1), self.actions())
        self.assertIsNone(self.client().catalog()["timer"])

    def test_end_refused_where_there_is_no_end(self):
        self.play("ch001")
        self.assertIn("直播台", self.client().timer("end")["error"])
        self.play("up01")
        self.relay._state_of(self.dev()).get("up01").videos[0].duration = 0
        self.stream("up01", self.relay._state_of(self.dev()).get("up01").videos[0])
        self.assertIn("多长", self.client().timer("end")["error"])
        self.play("up01", online=False)
        self.now += 60
        self.assertIn("没在线", self.client().timer("end")["error"])

    # ---- what is playing ----

    def test_catalog_says_what_is_playing(self):
        state = self.relay._state_of(self.dev())
        second = state.get("sr01").videos[1]
        self.play("sr01")
        self.stream("sr01", second, offset=100.0)
        playing = self.client().catalog()["playing"]
        self.assertEqual({k: playing[k] for k in ("channel", "episode", "title", "duration", "total")},
                         {"channel": "sr01", "episode": 2, "title": second.title, "duration": 600, "total": 3})
        self.assertAlmostEqual(playing["position"], 100, delta=1)
        self.play("ch001")
        playing = self.client().catalog()["playing"]
        self.assertNotIn("title", playing, "live: the media server knows the name")

    def test_series_rows_say_where_it_stopped(self):
        state = self.relay._state_of(self.dev())
        creator = state.get("sr01")
        state.play_from("sr01", 2)
        state.save_position("sr01", creator.videos[1].bvid, 330.0)
        row = self.client().catalog()["series"][0]
        self.assertEqual((row["next"], row["position"]), (2, 330))

    # ---- cast history ----

    def cast(self, bvid, title, duration=600):
        answer = self.client().cast_video("bilibili", bvid, title, duration)
        self.assertTrue(answer["ok"], answer)

    def test_cast_history_and_replay(self):
        self.play("ch001")
        self.cast("BV1aaaaaaaaa", "第一个")
        self.relay._cast_of(self.dev()).save_cast_position("BV1aaaaaaaaa", 120.0)
        self.cast("BV1bbbbbbbbb", "第二个")
        self.assertEqual(self.client().catalog()["casts"], ["第二个", "第一个"])
        rec = voicecmds._view(self.relay, self.dev())
        self.assertEqual([(c["id"], c["position"]) for c in rec["casts"]],
                         [("BV1bbbbbbbbb", 0.0), ("BV1aaaaaaaaa", 120.0)], "where the first one stopped")
        # "Play the previous cast", not watching a cast: the latest one.
        answer = self.client().recast(1)
        self.assertEqual((answer["ok"], answer["title"]), (True, "第二个"))
        self.assertEqual(self.actions()[-1], ("cast", None))
        # Watching it: the one before it, from the start.
        self.play("cast")
        answer = self.client().recast(1)
        self.assertEqual(answer["title"], "第一个")
        self.assertEqual(self.relay._cast_of(self.dev()).cast_position, 0.0)
        self.assertEqual(self.client().recast(5)["error"], "投屏历史里没有更早的了")
        # Replay the current one: from the start again.
        self.relay._cast_of(self.dev()).save_cast_position("BV1aaaaaaaaa", 300.0)
        self.assertTrue(self.client().recast(0)["ok"])
        self.assertEqual(self.relay._cast_of(self.dev()).cast_position, 0.0)

    def test_carry_on_with_the_last_cast(self):
        self.play("ch001")
        self.cast("BV1aaaaaaaaa", "第一个")
        self.relay._cast_of(self.dev()).save_cast_position("BV1aaaaaaaaa", 200.0)
        answer = self.client().recast(0, resume=True)
        self.assertEqual((answer["ok"], answer.get("resumed")), (True, True))
        self.assertEqual(self.actions()[-1], ("switch", "cast"), "the cast slot resumes by itself")
        self.play("cast")
        self.assertEqual(self.client().recast(0, resume=True)["note"], "正在播")
        # An older cast (replaced since) resumes where it stopped.
        self.cast("BV1bbbbbbbbb", "第二个")
        self.relay._cast_of(self.dev()).cast_ended("BV1bbbbbbbbb")
        answer = self.client().recast(0, resume=True)
        self.assertEqual(answer["title"], "第二个")
        self.assertEqual(self.relay._cast_of(self.dev()).cast_position, 0.0, "finished: from the start")

    def test_no_casts_yet(self):
        self.play("ch001")
        self.assertEqual(self.client().recast(0), {"ok": False, "error": "还没有投屏过"})

    # ---- subscribe the uploader of a cast ----

    def test_subscribe_the_uploader_of_the_cast(self):
        client = self.relay.clients["bilibili"] = LinkClient()
        self.cast("BV1yi7rzDEWc", "经典欣赏")
        self.play("cast")
        answer = self.client().subscribe_playing(3.0)
        self.assertEqual(answer["ok"], True, answer)
        self.assertEqual(client.asked[-1], "https://www.bilibili.com/video/BV1yi7rzDEWc")
        state = self.owner_state()
        self.assertEqual(state.get(answer["creator"]["id"]).source, "777")
        self.assertEqual(self.client().subscribe_playing(3.0)["error"], "已经订阅过「UP777」了")

    def test_subscribe_a_youtube_cast(self):
        youtube = self.relay.clients["youtube"] = LinkYouTube()
        self.relay._cast_video(Video(bvid="n61ULEU7CO0", title="study", duration=3615, pubdate=0, site="youtube"),
                               self.voice_caller(), self.dev())
        self.play("cast")
        answer = self.client().subscribe_playing(3.0)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(youtube.asked, ["https://www.youtube.com/watch?v=n61ULEU7CO0"])
        self.assertEqual(self.owner_state().get(answer["creator"]["id"]).site, "youtube")

    def test_subscribe_refusals(self):
        self.play("ch001")
        self.assertIn("不是投屏", self.client().subscribe_playing(3.0)["error"])
        self.play("up01")
        self.assertIn("已经在频道列表里了", self.client().subscribe_playing(3.0)["error"])
        self.play("cast")
        self.assertIn("没有在放", self.client().subscribe_playing(3.0)["error"])
        self.relay.room_of = lambda state, extra="": {"fits": False}
        self.relay.clients["bilibili"] = LinkClient()
        self.cast("BV1yi7rzDEWc", "经典欣赏")
        self.assertEqual(self.client().subscribe_playing(3.0)["error"], "频道列表满了，先删掉几个台")

    # ---- remove a creator or series ----

    def test_unsubscribe(self):
        self.play("ch000")
        voicecmds.tick(self.relay)
        self.later(30, "up01")
        answer = self.client().unsubscribe("up01")
        self.assertEqual((answer["ok"], answer["kind"]), (True, "up"))
        self.assertIsNone(self.owner_state().get("up01"))
        self.assertNotIn("up01 |", self.table_text())
        self.assertEqual(self.actions()[-1], ("switch", "ch000"), "away from the removed channel")
        self.assertEqual(self.client().unsubscribe("up01")["error"], "没有这个台了")
        self.assertEqual(self.client().unsubscribe("ch001")["error"], "这个台不能删")
        self.assertTrue(self.client().unsubscribe("sr01")["ok"])

    # ---- the media server's voice service against this relay ----

    def test_end_to_end_through_the_media_server(self):
        from test_voice_relay import EndToEnd
        self.relay_device, self.main = self.device, Path(self.tmp.name) / "channels.txt"
        say, _ = EndToEnd.voice(self)
        self.play("ch001")
        voicecmds.tick(self.relay)
        self.later(30, "ch000")
        voicecmds.tick(self.relay)
        result = say("回到刚才的台")
        self.assertEqual((result["do"], result["reply"]), ({"channel": "ch001"}, "回到湖南卫视"))
        self.assertEqual(say("30分钟后暂停")["reply"], "30 分钟后暂停")
        self.assertEqual(self.client().timer("status")["timer"], {"kind": "time", "left": 1800})
        self.assertEqual(say("还剩多久关机")["reply"], "还有 30 分钟暂停")
        self.assertEqual(say("取消定时")["reply"], "已取消定时")
        result = say("取消订阅UP42")
        self.assertEqual((result["intent"], result["reply"]), ("confirm", "确定删除「UP42」？"))
        self.assertIsNotNone(self.owner_state().get("up01"), "not before the confirmation")
        self.assertEqual(say("确定")["reply"], "已删除「UP42」")
        self.assertIsNone(self.owner_state().get("up01"))
        self.assertEqual(say("现在播的是什么")["reply"], "CCTV1（直播）")


class LegacyCommandTests(Commands, unittest.TestCase):
    device = "Passport"
    slot = ""

    def setUp(self):
        VoiceRelayTests.setUp(self)
        self.addCleanup(VoiceRelayTests.tearDown, self)
        self.relay.starter = lambda ffmpeg, url, offset: Proc()

    def now_file(self):
        return self.pp_now

    def events_file(self):
        return self.pp_events

    def voice_caller(self):
        return Caller()

    def owner_state(self):
        return self.relay.state

    def table_text(self):
        return (Path(self.tmp.name) / "relay.txt").read_text(encoding="utf-8")

    def restart(self):
        return web.Relay(State(Path(self.tmp.name) / "state.json"), FakeClient(), Path(self.tmp.name) / "relay.txt",
                         "/opt/ff", KEY, events_out=self.xz_events, now_playing_in=self.xz_now,
                         extra_devices=[("Passport", self.pp_now, self.pp_events)], log=lambda _m: None,
                         clock=lambda: self.now)

    def test_the_other_device_is_untouched(self):
        self.play("ch001")
        self.client().timer("set", 1)
        self.later(61)
        voicecmds.tick(self.relay)
        self.assertIsNone(events.read_event(self.xz_events), "the phone's Xingzhi")
        self.assertIsNone(voicecmds._view(self.relay, self.relay.find_device("d0"))["asleep"])


class TenantCommandTests(Commands, unittest.TestCase):
    device = BOB_DEVICE
    slot = BOB_DEVICE

    def setUp(self):
        TenantVoiceRelayTests.setUp(self)
        self.addCleanup(TenantVoiceRelayTests.tearDown, self)
        self.relay.add_creator("https://space.bilibili.com/42", Caller(user=self.bob, role="user"))
        self.relay.sync_devices(force=True)
        self.relay.starter = lambda ffmpeg, url, offset: Proc()

    def files(self, device):
        from server.tenancy import files as device_files
        return device_files.device_files(self.devices_dir, device)

    def now_file(self):
        return self.files(BOB_DEVICE).now

    def events_file(self):
        return self.files(BOB_DEVICE).events

    def voice_caller(self):
        return Caller(role="guest", device=BOB_DEVICE)

    def owner_state(self):
        return self.relay.user_state(self.bob)

    def table_text(self):
        return self.files(BOB_DEVICE).channels.read_text(encoding="utf-8")

    def restart(self):
        from server.tenancy.accounts import Accounts
        accounts = Accounts(Path(self.tmp.name) / "tv.db", clock=lambda: self.now)
        self.addCleanup(accounts.close)
        return web.Relay(None, FakeClient(), None, "/opt/ff", KEY, accounts=accounts,
                         devices_dir=self.devices_dir, log=lambda _m: None, clock=lambda: self.now)

    def test_memory_is_the_devices_and_goes_with_unbinding(self):
        self.play("ch001")
        voicecmds.tick(self.relay)
        self.client().timer("set", 30)
        stored = self.accounts.load_device_state(BOB_DEVICE)
        self.assertEqual(stored["voice"]["timer"]["kind"], "time")
        self.assertIn("channels", stored["voice"])
        self.relay.unbind_device(BOB_DEVICE, Caller(user=self.bob, role="user"))
        self.assertIsNone(self.accounts.load_device_state(BOB_DEVICE))

    def test_a_phone_guest_cannot_remove_the_owners_channels(self):
        guest = Caller(role="guest", device=BOB_DEVICE)
        answer = voicecmds.unsubscribe(self.relay, BOB_DEVICE, guest, {"id": "up01"}, keyed=False)
        self.assertEqual(answer, {"ok": False, "error": "只有设备主人能删台"})
        admin = Caller(user=self.admin, role="admin")
        self.assertIsNone(self.relay.find_device(BOB_DEVICE, admin), "the admin does not even see it")
        bob = Caller(user=self.bob, role="user")
        self.assertTrue(voicecmds.unsubscribe(self.relay, BOB_DEVICE, bob, {"id": "up01"})["ok"])

    def test_admins_own_device_is_separate(self):
        self.play("ch001")
        self.client().timer("set", 1)
        self.later(61)
        voicecmds.tick(self.relay)
        from test_voice_relay import ADMIN_DEVICE
        self.assertIsNone(events.read_event(self.files(ADMIN_DEVICE).events))
        self.assertEqual([c.id for c in self.relay.user_state(self.admin).creators], [])


class SleepPauseMediaServerTests(unittest.TestCase):
    """The media server's half: a pause event with value 1 does not resume after 30 minutes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "relay-events.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_pause_with_value_one_is_a_sleep_control(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("ch001", 1.0)
        events.write_event(self.path, "brightness", value=4)
        events.write_event(self.path, "pause", value=1, text="湖南卫视")
        events.write_event(self.path, "pause", text="手机的暂停")
        watcher.poll("ch001", 2.0)
        self.assertEqual(watcher.take_controls(), [("brightness", 4), ("sleep", "湖南卫视"), ("pause", "手机的暂停")])

    def test_sleep_pause_does_not_expire_after_thirty_minutes(self):
        from server import tv_server
        watcher = SwitchWatcher(self.path)
        server = types.SimpleNamespace(switch_watcher=watcher, channel_name="ch001", logger=lambda _m: None)
        verdict = lambda at: tv_server.AVServer.pause_verdict(server, at)
        long_ago = time.monotonic() - tv_server.PAUSE_MAX_S - 5
        self.assertEqual(verdict(long_ago), "resume", "the phone's pause, as before")
        server.pause_limit_s = tv_server.SLEEP_PAUSE_MAX_S
        self.assertEqual(verdict(long_ago), "paused")
        self.assertEqual(verdict(time.monotonic() - tv_server.SLEEP_PAUSE_MAX_S - 5), "resume")
        # Paused by the phone, then the timer fired: it becomes a sleep pause.
        server.pause_limit_s = tv_server.PAUSE_MAX_S
        events.write_event(self.path, "pause", value=1, text="x")
        watcher._next_poll = 0
        self.assertEqual(verdict(long_ago), "paused")
        self.assertEqual(server.pause_limit_s, tv_server.SLEEP_PAUSE_MAX_S)
        events.write_event(self.path, "resume")
        watcher._next_poll = 0
        self.assertEqual(verdict(long_ago), "resume")

    def test_relay_and_media_server_agree(self):
        from server import tv_server
        self.assertEqual(tv_server.SLEEP_PAUSE_MAX_S, voicecmds.SLEEP_PAUSE_MAX_S)
        self.assertEqual(tv_server.PAUSE_MAX_S, web.PAUSE_MAX_S)


if __name__ == "__main__":
    unittest.main()
