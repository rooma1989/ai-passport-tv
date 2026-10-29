"""Playing the library by voice, the relay's side (server/relay/voicelib.py): the catalogue's
library section and POST /api/voice/library, called the way the media server calls them
(RelayClient over HTTP with the key, naming the device, which the relay serves as that
device's guest).

Checked: the section lists the owner's titles (shows with their episode count, movies, other
videos; local, English and Douban names) only for the TV service or a family member, never for
a guest's phone; a movie resumes from the owner's saved position; a show without an episode
carries on (the unfinished latest episode, else the next one, else episode 1); an episode by
its number and season; "jie zhe kan" plays the latest of Continue watching; what My cast shows
is reported for "di 3 ji"; everything lands in the cast slot through library.play_tv like
"Play to". A banned owner or a guest gets nothing. No ffmpeg, no device, no sound: the stream
is never opened here."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import events, library, voicelib
from server.relay.tenants import Caller
from server.tenancy import files as device_files
from server.voice.relay_client import RelayClient
from server.voice.session import VoiceService

import test_relay_library_play as play_base
from test_relay_watch import ALICE_DEVICE, BOB_DEVICE
from test_relay_web import KEY
from test_voice_session import FakeAsr, audio, context, ctl, tone_blocks

EPISODE_1, EPISODE_2 = "家庭剧/S01E01.mp4", "家庭剧/S01E02.mp4"
MOVIE = "电影/盗梦空间 (2010)/盗梦空间 (2010).mp4"
FRIENDS_1 = "剧集/老友记/S01/老友记 S01E01.mp4"


class VoiceLibraryTests(play_base._Relay):
    def setUp(self):
        super().setUp()
        self.ids = self.add_both()
        self.lib = library.wired(self.relay)
        for path in self.ids:
            self.media(path, 1200 if "S01" in path else 5400)
        self.relay.library_starter = lambda _args: self.fail("no stream is opened here")
        base = f"http://127.0.0.1:{self.manage.server_address[1]}"
        self.tv = RelayClient(base, KEY, ALICE_DEVICE)
        self.bob_tv = RelayClient(base, KEY, BOB_DEVICE)

    def media(self, path, duration):
        iid = self.ids[path]
        with library._LOCK:
            item = self.lib._items[iid]
            item["media"] = dict(play_base.OWNER_MKV, dur=float(duration), v=library.PROBE_VERSION,
                                 size=item.get("size"), mtime=item.get("mtime"))
        return iid

    def key(self, path):
        return self.lib.meta_key(self.lib.item(self.ids[path]))

    def cast(self):
        dev = self.relay.find_device(ALICE_DEVICE, Caller(user=self.alice, role="user"))
        return self.relay._cast_of(dev).cast

    def watched(self, path, position, duration=1200.0, done=False):
        iid = self.ids[path]
        self.now += 10          # a later record; the TV is still on (its now-playing file is fresh)
        self.online(ALICE_DEVICE, "ch000")
        self.relay.pc_progress.put(self.alice, "lib:" + iid, {"channel": "cast", "bvid": iid, "site": "library",
                                                               "position": position, "duration": duration})
        if done:
            self.relay.pc_progress.mark_done(self.alice, "lib:" + iid, iid)

    def last_event(self):
        return events.read_events(device_files.device_files(self.devices_dir, ALICE_DEVICE).events)[-1]

    # ---- the catalogue ----

    def test_catalogue_lists_the_owners_titles_for_the_tv_service(self):
        section = self.tv.catalog()["library"]
        titles = {row["name"]: row for row in section["titles"]}
        self.assertEqual((titles["家庭剧"]["kind"], titles["家庭剧"]["total"], titles["家庭剧"]["id"]),
                         ("show", 2, self.key(EPISODE_1)))
        self.assertEqual((titles["老友记"]["kind"], titles["老友记"]["total"]), ("show", 2))
        self.assertEqual(titles["盗梦空间"]["kind"], "movie")
        self.assertIn("Inception", titles)
        self.assertEqual(titles["Clip"]["kind"], "video")
        self.assertEqual((section["continue"], section["playing"]), (None, None))
        text = json.dumps(section, ensure_ascii=False)
        self.assertNotIn("/", text.replace("\\/", ""), "no paths or addresses")
        # Douban's original title and aka are other names for the same title.
        key = self.key(MOVIE)
        self.lib.meta.put(key, {"state": "matched", "id": "3541415", "title": "盗梦空间", "original": "Inception",
                                "aka": ["潜行凶间", "全面启动"], "year": 2010})
        row = next(r for r in self.tv.catalog()["library"]["titles"] if r["id"] == key)
        self.assertEqual(row["aka"][:3], ["Inception", "潜行凶间", "全面启动"])
        # Watched titles come first.
        self.watched(FRIENDS_1, 300.0)
        self.assertEqual(self.tv.catalog()["library"]["titles"][0]["id"], self.key(FRIENDS_1))

    def test_guests_and_banned_owners_get_no_library(self):
        status, answer = self.call("guest", "GET", f"/tv/api/voice/catalog?device={ALICE_DEVICE}")
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"])
        self.assertNotIn("library", answer, "a guest's phone never sees the library")
        status, answer = self.call("alice", "GET", f"/tv/api/voice/catalog?device={ALICE_DEVICE}")
        self.assertIn("library", answer, "the family does")
        status, answer = self.call("guest", "POST", "/tv/api/voice/library",
                                   {"device": ALICE_DEVICE, "id": self.key(MOVIE)})
        self.assertTrue(status == 404 or not answer["ok"], answer)
        self.assertIsNone(self.cast())
        self.assertIn("library", self.bob_tv.catalog())
        self.accounts.set_user_status(self.bob, "banned")
        self.assertNotIn("library", self.bob_tv.catalog())
        self.assertEqual(self.bob_tv.library(self.key(MOVIE)), {"ok": False, "error": voicelib.REPLY_NO_ACCESS})
        # Directly: a guest caller without the key is refused even for its own device.
        guest = Caller(role="guest", device=ALICE_DEVICE)
        dev = self.relay.find_device(ALICE_DEVICE, guest)
        self.assertEqual(voicelib.catalog(self.relay, dev, guest), {})
        self.assertFalse(voicelib.play(self.relay, ALICE_DEVICE, guest, {"id": self.key(MOVIE)})["ok"])
        self.assertTrue(voicelib.catalog(self.relay, dev, guest, keyed=True))
        # No library on the relay (the cloud): nothing either.
        self.relay.library = None
        self.assertNotIn("library", self.tv.catalog())

    # ---- playing ----

    def test_a_movie_resumes_where_the_owner_stopped(self):
        self.watched(MOVIE, 900.0, 5400.0)
        answer = self.tv.library(self.key(MOVIE))
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["name"], answer["kind"], answer["resume"], answer["episode"]),
                         ("盗梦空间", "movie", 900.0, 0))
        video = self.cast()
        self.assertEqual((video.site, video.bvid), ("library", self.ids[MOVIE]))
        self.assertEqual(self.last_event().action, "cast")
        self.assertEqual(library._viewers(self.relay)[ALICE_DEVICE], self.alice, "progress goes to the owner")
        # Nearly finished: from the start.
        self.watched(MOVIE, 5398.0, 5400.0)
        self.assertNotIn("resume", self.tv.library(self.key(MOVIE)))

    def test_a_show_carries_on(self):
        show = self.key(EPISODE_1)
        answer = self.tv.library(show)
        self.assertEqual((answer["episode"], answer["total"], self.cast().bvid), (1, 2, self.ids[EPISODE_1]),
                         "never watched: episode 1")
        self.watched(EPISODE_1, 400.0)
        answer = self.tv.library(show)
        self.assertEqual((answer["episode"], answer["resume"]), (1, 400.0), "unfinished: carry on in it")
        self.watched(EPISODE_1, 1200.0, done=True)
        answer = self.tv.library(show)
        self.assertEqual((answer["episode"], self.cast().bvid), (2, self.ids[EPISODE_2]), "finished: the next one")
        self.assertNotIn("resume", answer)
        self.watched(EPISODE_2, 1195.0)
        answer = self.tv.library(show)
        self.assertEqual((answer["episode"], answer["note"]), (1, "已经看完了，从第 1 集重看"))

    def test_an_episode_by_number(self):
        show = self.key(EPISODE_1)
        self.assertEqual(self.tv.library(show, episode=2)["episode"], 2)
        self.assertEqual(self.cast().bvid, self.ids[EPISODE_2])
        self.assertEqual(self.tv.library(show, episode=1, season=1)["episode"], 1)
        self.assertEqual(self.tv.library(show, episode=3), {"ok": False, "error": "《家庭剧》没有第 3 集"})
        self.assertEqual(self.tv.library(show, episode=1, season=2), {"ok": False, "error": "《家庭剧》没有第 2 季第 1 集"})
        # A movie ignores an episode number.
        self.assertEqual(self.tv.library(self.key(MOVIE), episode=3)["kind"], "movie")
        for bad in ("", "s123", "../x", "m" + "0" * 16):
            self.assertFalse(self.tv.library(bad)["ok"], bad)

    def test_carry_on_with_the_latest_of_continue_watching(self):
        self.assertEqual(self.tv.library(resume=True), {"ok": False, "error": voicelib.REPLY_NO_CONTINUE})
        self.watched(FRIENDS_1, 300.0)
        self.watched(MOVIE, 900.0, 5400.0)
        self.watched(EPISODE_2, 1199.0)          # finished: not in Continue watching
        section = self.tv.catalog()["library"]
        self.assertEqual((section["continue"]["id"], section["continue"]["name"]), (self.key(MOVIE), "盗梦空间"))
        answer = self.tv.library(resume=True)
        self.assertEqual((answer["name"], answer["resume"], self.cast().bvid), ("盗梦空间", 900.0, self.ids[MOVIE]))
        # The same rule as the home page's row.
        self.relay._home_cache.clear()
        rows = [r for r in self.call("alice", "GET", "/tv/api/home")[1]["continue"] if r["source"] == "library"]
        self.assertEqual(rows[0]["play"]["id"], self.ids[MOVIE])

    def test_what_my_cast_shows(self):
        self.tv.library(self.key(EPISODE_1), episode=2)
        self.assertIsNone(self.tv.catalog()["library"]["playing"], "the TV is not on My cast yet")
        self.online(ALICE_DEVICE, "cast")
        playing = self.tv.catalog()["library"]["playing"]
        self.assertEqual(playing, {"id": self.key(EPISODE_1), "kind": "show", "episode": 2, "total": 2,
                                   "name": "家庭剧"})

    def test_from_words_to_the_cast_slot(self):
        """The media server's session against this relay: the words, the catalogue, the call, the reply."""
        asr = FakeAsr()
        voice = VoiceService(asr, self.tv, lambda _m: None, threaded=False)

        def say(text, uid):
            asr.text = text
            ctx = context()
            voice.take(ctl("begin", uid, rate=16000, codec="adpcm"), ctx)
            voice.take(audio(tone_blocks(1)[0][0], 0), ctx)
            voice.take(ctl("end", uid), ctx)
            return json.loads(voice.results()[-1])

        result = say("我要看家庭剧第二集", 1)
        self.assertEqual((result["intent"], result["reply"]), ("library", "播放《家庭剧》第 2 集"))
        self.assertEqual(self.cast().bvid, self.ids[EPISODE_2])
        self.watched(MOVIE, 900.0, 5400.0)
        result = say("放道梦空间", 2)
        self.assertEqual(result["reply"], "接着放《盗梦空间》，从 15:00 起")
        self.assertEqual(self.cast().bvid, self.ids[MOVIE])
        self.assertEqual(say("湖南卫视", 3)["do"], {"channel": "ch017"}, "channels first")
        # A banned owner's TV: no titles, so the words are just not found.
        self.accounts.set_user_status(self.alice, "banned")
        self.assertEqual(say("我要看老友记", 4)["intent"], "none")

    def test_offline_device(self):
        answer = self.bob_tv.library(self.key(MOVIE))
        self.assertFalse(answer["ok"])
        self.assertIn("没在线", answer["error"])

    def test_catalogue_stays_small(self):
        with library._LOCK:
            for n in range(3000):
                iid = f"{n:016x}"
                self.lib._items[iid] = {"id": iid, "source": "x", "path": f"m/{n}.mp4", "kind": "movie",
                                        "title": "很长的片名" * 8 + str(n), "ext": ".mp4", "added": n}
        section = voicelib.catalog(self.relay, self.relay.find_device(ALICE_DEVICE, Caller(
            role="guest", device=ALICE_DEVICE)), keyed=True)["library"]
        size = len(json.dumps(section["titles"], ensure_ascii=False).encode())
        self.assertLessEqual(size, voicelib.CATALOG_BYTES + 1024)
        self.assertGreater(len(section["titles"]), 100)
        # The whole catalogue still fits what the media server reads (256 KB).
        self.assertLessEqual(len(json.dumps(self.tv.catalog(), ensure_ascii=False).encode()), 128 * 1024)


if __name__ == "__main__":
    unittest.main()
