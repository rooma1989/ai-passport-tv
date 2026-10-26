"""The PC player in the console (server/relay/web.py watch_* and watch.py): what its controls rely on.

What is checked: every watch answer says where the stream starts in the video
(start), how long the video is (duration) and whether it can seek; a seek
(start=...) starts a new ffmpeg with -ss on a new stream id and token, the old
URL stops working, and on "follow the TV" it only moves the PC (a stream of the
page's own, the TV's position untouched); the episode list ("parts") of a
multi-part Bilibili video, a collection (ugc season), a bangumi season and a
YouTube playlist, built from canned answers (the Bilibili client's view
parsing is tested on a canned view as well, the owner's router may block
Bilibili); ?p= picks the part; a finished part is marked done on the PC and
the next request moves on to the next part (auto-advance); the PC's progress
is per user, resumes with a "resume" hint, start=0 plays from the beginning,
and never touches the TV's positions or watched lists. Also: the playlist
carries the media time of the first fragment (#EXT-X-RTV-ORIGIN) so the page
can map media time to the video's timeline, and a POST answered without reading
its body closes the connection (a browser reusing it would see a 501).
"""

from __future__ import annotations

import http.client
import json
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import bilibili, watch, web
from server.relay.state import Video

import test_relay_watch as base
from test_relay_watch import ALICE_DEVICE, box, fragment, full, moov
from test_relay_web import FakeClient

MULTI = "BV1mu4411Abc"
SEASON_BV = "BV1se4411Ab2"
PARTS = [Video(bvid=f"{MULTI}/{100 + n}", title=f"P{n} 标题", duration=1000 + n, pubdate=0) for n in (1, 2, 3)]
SEASON = [Video(bvid=f"BV1se4411Ab{n}/{200 + n}", title=f"合集第{n}个", duration=300, pubdate=0) for n in (1, 2, 3)]


class PartsClient(FakeClient):
    """FakeClient plus parts(): a 3-P video and a 3-video collection (the BV in the middle)."""

    def __init__(self):
        super().__init__()
        self.views = 0

    def video(self, bvid):
        self.views += 1
        if bvid == MULTI:
            return Video(bvid=bvid, title="超清《葫芦娃》全1-3部", duration=3006, pubdate=0)
        return super().video(bvid)

    def parts(self, bvid):
        if bvid == MULTI:
            return "pages", "葫芦娃", list(PARTS)
        if bvid == SEASON_BV:
            return "season", "一个合集", list(SEASON)
        return None


class PlayerTests(unittest.TestCase):
    selfhost = True
    setUp_relay = base.RelayWatchTests.setUp
    tearDown = base.RelayWatchTests.tearDown
    online = base.RelayWatchTests.online
    request = base.RelayWatchTests.request

    def setUp(self):
        self.setUp_relay()
        self.carol = self.accounts.create_user("Carol")
        self.cookies["carol"] = "tvs=" + self.accounts.new_session(self.carol, "pc")
        self.as_carol = web.Caller(user=self.carol, role="user", name="Carol")
        self.as_alice = web.Caller(user=self.alice, role="user", name="Alice")
        self.client = PartsClient()
        self.relay.clients["bilibili"] = self.client
        self.relay.add_creator("https://space.bilibili.com/42", self.as_carol)
        self.relay.add_series("https://www.bilibili.com/bangumi/play/ss1", "", self.as_carol)

    def watch(self, who="carol", **body):
        body.setdefault("viewer", "c0ffee00")
        status, _, raw = self.request("POST", "/tv/api/watch", who, body)
        self.assertEqual(status, 200)
        return json.loads(raw)

    def stream_of(self, answer):
        found = next(s for s in self.relay.watch.streams() if s.sid == answer["sid"])
        self.assertTrue(found.wait_ready(5))
        return found

    def args_of(self, answer):
        stream = self.stream_of(answer)
        proc = next(p for p in reversed(self.popen.procs) if p.args == watch.ffmpeg_args(
            "/opt/ff", stream.source, p.args[p.args.index("-i") + 1], watch.FIRST_MODE))
        return proc.args

    def report(self, answer, who="carol", **body):
        body.update(viewer="c0ffee00", sid=answer["sid"])
        return json.loads(self.request("POST", "/tv/api/watch/progress", who, body)[2])

    # ---- start, duration, seekable ----

    def test_answers_carry_start_duration_and_seekable(self):
        self.relay.user_state(self.carol).save_position("up01", "b", 130.0)
        video = self.watch(channel="up01")
        self.assertTrue(video["ok"], video)
        self.assertEqual((video["start"], video["duration"], video["seekable"]), (120.0, 600, True))
        self.assertEqual(video["resume"], 120.0, "the page offers 从头播放")
        live = self.watch(channel="ch000")
        self.assertEqual((live["start"], live["duration"], live["seekable"]), (0.0, 0, False))
        # Following the TV: the TV's video, from where the TV is.
        self.online(ALICE_DEVICE, "up01")
        dev = self.relay.find_device(ALICE_DEVICE, self.as_alice)
        tv_video = Video(bvid="BV1xx411c7mD", title="视频标题", duration=900, pubdate=0)
        self.relay._paused[dev["slot"]] = {"cid": "up01", "video": tv_video, "position": 75.0, "at": self.now}
        status, _, raw = self.request("POST", "/tv/api/watch", "alice", {"device": ALICE_DEVICE, "viewer": "a11ce000"})
        follow = json.loads(raw)
        self.assertEqual((follow["start"], follow["duration"], follow["seekable"], follow["own"]), (75.0, 900, True, False))

    # ---- seeking ----

    def test_seek_restarts_with_ss_and_a_new_token(self):
        first = self.watch(channel="sr01", episode=2)
        old_base = "/tv/" + first["url"]
        self.assertEqual(self.request("GET", old_base + "index.m3u8")[0], 200)
        seek = self.watch(channel="sr01", episode=2, start=300.4)
        self.assertTrue(seek["ok"], seek)
        self.assertNotEqual(seek["sid"], first["sid"])
        self.assertEqual((seek["start"], seek["duration"]), (300.4, 600))
        self.assertNotIn("resume", seek)
        args = self.args_of(seek)
        self.assertEqual(args[args.index("-ss") + 1], "300")
        self.assertEqual(args[args.index("-i") + 1], "https://media.example/ep2/2.mp4")
        sid, token = seek["url"].split("/")[1:3]
        self.assertEqual(self.request("GET", f"/tv/watch/{sid}/{token}/index.m3u8")[0], 200)
        self.assertEqual(self.request("GET", old_base + "index.m3u8")[0], 404, "the old stream is gone")
        self.assertEqual(self.request("GET", f"/tv/watch/{sid}/{first['url'].split('/')[2]}/index.m3u8")[0], 404,
                         "the old token does not open the new stream")
        self.assertEqual(len(self.relay.watch.streams()), 1, "the page still holds one stream")
        # Asking again without start joins the running stream; past the end is clamped, bad values refused.
        self.assertEqual(self.watch(channel="sr01", episode=2)["sid"], seek["sid"])
        self.assertEqual(self.watch(channel="sr01", episode=2, start=5000)["start"], 597.0)
        self.assertIn("格式", self.watch(channel="sr01", start=-1)["error"])
        self.assertIn("格式", self.watch(channel="sr01", start="10")["error"])
        # The TV's series state is untouched.
        creator = self.relay.user_state(self.carol).get("sr01")
        self.assertEqual((creator.watched, creator.positions), ([], {}))

    def test_seeking_while_following_the_tv_only_moves_the_pc(self):
        self.online(ALICE_DEVICE, "up01")
        dev = self.relay.find_device(ALICE_DEVICE, self.as_alice)
        tv_video = Video(bvid="BV1xx411c7mD", title="视频标题", duration=900, pubdate=0)
        self.relay._paused[dev["slot"]] = {"cid": "up01", "video": tv_video, "position": 75.0, "at": self.now}
        body = {"device": ALICE_DEVICE, "viewer": "a11ce000"}
        follow = json.loads(self.request("POST", "/tv/api/watch", "alice", body)[2])
        own = json.loads(self.request("POST", "/tv/api/watch", "alice", dict(body, start=400))[2])
        self.assertTrue(own["ok"], own)
        self.assertEqual((own["own"], own["start"], own["duration"]), (True, 400.0, 900))
        stream = self.stream_of(own)
        self.assertEqual(stream.device, f"pc{self.alice}-a11ce000", "a stream of the page's own")
        args = self.args_of(own)
        self.assertEqual(args[args.index("-ss") + 1], "400")
        sid, token = own["url"].split("/")[1:3]
        self.assertEqual(self.request("GET", f"/tv/watch/{sid}/{token}/index.m3u8")[0], 200)
        self.assertEqual(self.relay._paused[dev["slot"]]["position"], 75.0, "the TV stays where it was")
        self.assertIn(follow["sid"], [s.sid for s in self.relay.watch.streams()], "the household's stream goes on")
        # Following again (the TV moved on): the page's own stream is closed.
        again = json.loads(self.request("POST", "/tv/api/watch", "alice", body)[2])
        self.assertEqual(again["sid"], follow["sid"])
        self.assertEqual([s.device for s in self.relay.watch.streams()], [ALICE_DEVICE])
        # A live channel cannot seek; a seek needs the page id.
        self.online(ALICE_DEVICE, "ch000")
        self.assertIn("直播", json.loads(self.request("POST", "/tv/api/watch", "alice", dict(body, start=10))[2])["error"])
        self.assertFalse(json.loads(self.request("POST", "/tv/api/watch", "alice",
                                                 {"device": ALICE_DEVICE, "start": 10})[2])["ok"])

    # ---- parts ----

    def test_multi_part_video_lists_its_parts_and_follows_p(self):
        answer = self.watch(url=f"https://www.bilibili.com/video/{MULTI}?p=2")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["part"], answer["title"], answer["duration"], answer["start"]), (2, "P2 标题", 1002, 0.0))
        self.assertEqual(answer["video"], {"site": "bilibili", "id": MULTI}, "replays address the whole video")
        self.assertEqual(answer["item_title"], "超清《葫芦娃》全1-3部")
        parts = answer["parts"]
        self.assertEqual((parts["kind"], parts["name"], parts["current"]), ("pages", "葫芦娃", 2))
        self.assertEqual([(p["n"], p["title"], p["duration"], p["watched"]) for p in parts["items"]],
                         [(1, "P1 标题", 1001, False), (2, "P2 标题", 1002, False), (3, "P3 标题", 1003, False)])
        args = self.args_of(answer)
        self.assertEqual(args[args.index("-i") + 1], f"https://media.example/{MULTI}/102.mp4")
        self.assertEqual(self.relay.user_state(self.carol).history[0]["id"], MULTI, "one history entry for the video")
        # Without ?p= a fresh video starts at part 1; picking a part from the sheet uses the cached info.
        views = self.client.views
        third = self.watch(video={"site": "bilibili", "id": MULTI}, part=3)
        self.assertEqual((third["part"], third["title"]), (3, "P3 标题"))
        self.assertEqual(self.client.views, views, "switching parts does not ask Bilibili again")
        self.assertIn("没有第 9 集", self.watch(video={"site": "bilibili", "id": MULTI}, part=9)["error"])
        self.assertIn("格式", self.watch(video={"site": "bilibili", "id": MULTI}, part="2")["error"])

    def test_collection_starts_at_the_cast_video(self):
        answer = self.watch(url=f"https://www.bilibili.com/video/{SEASON_BV}")
        self.assertEqual((answer["part"], answer["title"], answer["parts"]["kind"]), (2, "合集第2个", "season"))
        args = self.args_of(answer)
        self.assertEqual(args[args.index("-i") + 1], f"https://media.example/{SEASON_BV}/202.mp4")

    def test_a_single_video_has_no_parts(self):
        answer = self.watch(url="https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("parts", answer)
        self.assertEqual((answer["duration"], answer["seekable"]), (600, True))

    def test_bangumi_link_plays_the_season(self):
        answer = self.watch(url="https://www.bilibili.com/bangumi/play/ss1")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["part"], answer["title"], answer["parts"]["kind"]), (1, "第1集", "pgc"))
        self.assertEqual(answer["video"], {"site": "bilibili", "id": "ss1"})
        self.assertEqual(len(answer["parts"]["items"]), 3)
        # Replaying a whole season on a TV is refused with a hint (the TV needs it as a series).
        refused = self.relay.replay("bilibili", "ss1", self.as_carol)
        self.assertFalse(refused["ok"])
        self.assertIn("剧集", refused["error"])

    def test_youtube_playlist(self):
        link = "https://www.youtube.com/playlist?list=PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw"
        answer = self.watch(url=link)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["parts"]["kind"], answer["parts"]["name"], answer["part"]), ("playlist", "灌籃高手", 1))
        second = self.watch(video=answer["video"], part=2, start=30)
        self.assertEqual((second["title"], second["start"]), ("第2集", 30.0))
        self.stream_of(second).source.opener()
        self.assertEqual(self.youtube.opened[-1], ("slamdunk002", 30.0))

    def test_subscriptions_list_their_videos(self):
        answer = self.watch(channel="sr01", episode=2)
        self.assertEqual((answer["parts"]["kind"], answer["parts"]["current"], len(answer["parts"]["items"])),
                         ("series", 2, 3))
        self.assertEqual(self.watch(channel="up01")["parts"]["kind"], "up")

    def test_bilibili_client_reads_parts_from_the_view(self):
        view = {"bvid": MULTI, "title": "超清《葫芦娃》全1-2部 完整版reaction", "duration": 6000, "pubdate": 1,
                "pages": [{"cid": 11, "page": 1, "part": "第一部", "duration": 2900},
                          {"cid": 12, "page": 2, "part": "", "duration": 3100}]}
        calls = []

        def fetch(url, cookie, timeout=None):
            calls.append(url)
            if "finger/spi" in url:
                return {"code": 0, "data": {"b_3": "x"}}
            return {"code": 0, "data": view}

        client = bilibili.Bilibili(fetch_json=fetch)
        self.assertEqual(client.video(MULTI).duration, 6000)
        kind, name, parts = client.parts(MULTI)
        self.assertEqual(kind, "pages")
        self.assertEqual([(p.bvid, p.title, p.duration) for p in parts],
                         [(f"{MULTI}/11", "第一部", 2900), (f"{MULTI}/12", "P2", 3100)])
        self.assertEqual(sum("view" in u for u in calls), 1, "parts() reuses the view video() fetched")
        season = {"title": "一", "pages": [{"cid": 1, "page": 1}],
                  "ugc_season": {"title": "合集名", "sections": [{"episodes": [
                      {"bvid": "BV1aa", "cid": 5, "title": "甲", "arc": {"duration": 60}},
                      {"bvid": "BV1bb", "cid": 6, "title": "乙", "arc": {"duration": 70}}]}]}}
        self.assertEqual(bilibili.view_parts("BV1aa", season)[0:2], ("season", "合集名"))
        self.assertIsNone(bilibili.view_parts("BV1aa", {"pages": [{"cid": 1}]}))
        # The series code path still works the same on top of the shared parser.
        view["pages"] = view["pages"][:1]
        client._views.clear()
        with self.assertRaises(bilibili.BilibiliError):
            client.series(MULTI)

    # ---- progress: auto-advance, resume, isolation ----

    def test_finishing_a_part_marks_it_and_moves_on(self):
        first = self.watch(url=f"https://www.bilibili.com/video/{MULTI}")
        self.assertEqual(first["part"], 1)
        self.assertEqual(self.report(first, position=500.0), {"ok": True})
        self.assertEqual(self.report(first, ended=True), {"ok": True})
        record = self.relay.pc_progress.get(self.carol, f"v:bilibili:{MULTI}")
        self.assertEqual(record["done"], [f"{MULTI}/101"])
        # The page counts down and asks for the next part; its ✓ shows in the sheet.
        second = self.watch(video={"site": "bilibili", "id": MULTI}, part=2)
        self.assertEqual((second["part"], second["start"]), (2, 0.0))
        self.assertEqual([p["watched"] for p in second["parts"]["items"]], [True, False, False])
        # Coming back to the video later without a part: the part the PC was on.
        self.report(second, position=700.0)
        self.request("POST", "/tv/api/watch/stop", "carol", {"viewer": "c0ffee00", "sid": second["sid"], "position": 710.0})
        back = self.watch(video={"site": "bilibili", "id": MULTI})
        self.assertEqual((back["part"], back["start"], back["resume"]), (2, 710.0, 710.0))
        # A finished part is not resumed at its end: the next part plays.
        self.report(back, ended=True)
        self.request("POST", "/tv/api/watch/stop", "carol", {"viewer": "c0ffee00"})
        self.assertEqual(self.watch(video={"site": "bilibili", "id": MULTI})["part"], 3)
        # Near the end counts as finished too (the stream may end a little early).
        third = self.watch(video={"site": "bilibili", "id": MULTI}, part=3)
        self.report(third, position=990.0)
        done = self.relay.pc_progress.done(self.carol, f"v:bilibili:{MULTI}")
        self.assertEqual(done, {f"{MULTI}/101", f"{MULTI}/102", f"{MULTI}/103"})

    def test_series_episode_done_on_the_pc_moves_on_but_not_the_tv(self):
        answer = self.watch(channel="sr01", episode=1)
        self.report(answer, ended=True)
        self.request("POST", "/tv/api/watch/stop", "carol", {"viewer": "c0ffee00"})
        again = self.watch(channel="sr01")
        self.assertEqual(again["episode"], 2, "the PC carries on with the next episode")
        self.assertEqual(again["parts"]["items"][0]["watched"], True)
        state = self.relay.user_state(self.carol)
        self.assertEqual((state.get("sr01").watched, state.episode("sr01")), ([], 1), "the TV still starts at 1")

    def test_progress_is_per_user_and_from_start_ignores_it(self):
        url = f"https://www.bilibili.com/video/{MULTI}?p=2"
        answer = self.watch(url=url)
        self.report(answer, position=321.0)
        self.request("POST", "/tv/api/watch/stop", "carol", {"viewer": "c0ffee00"})
        resumed = self.watch(video={"site": "bilibili", "id": MULTI})
        self.assertEqual((resumed["start"], resumed["resume"]), (321.0, 321.0))
        args = self.args_of(resumed)
        self.assertEqual(args[args.index("-ss") + 1], "321")
        fresh = self.watch(video={"site": "bilibili", "id": MULTI}, part=2, start=0)
        self.assertEqual((fresh["start"], fresh.get("resume")), (0.0, None))
        self.assertNotIn("-ss", self.args_of(fresh))
        # Alice casting the same link starts at its start, and sees nothing of Carol's.
        alice = self.watch("alice", url=url, viewer="a11ce000")
        self.assertEqual((alice["start"], alice.get("resume")), (0.0, None))
        self.assertEqual([p["watched"] for p in alice["parts"]["items"]], [False, False, False])
        # Reports for another page, a stale stream id, or from a guest change nothing.
        self.assertFalse(self.report(fresh, who="alice", position=50.0)["ok"])
        stale = json.loads(self.request("POST", "/tv/api/watch/progress", "carol",
                                        {"viewer": "c0ffee00", "sid": answer["sid"], "position": 999.0})[2])
        self.assertFalse(stale["ok"])
        self.assertFalse(json.loads(self.request("POST", "/tv/api/watch/progress", "guest",
                                                 {"viewer": "c0ffee00", "position": 1.0})[2])["ok"])
        saved = json.loads((Path(self.accounts.path).parent / "pc-progress.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[str(self.carol)][f"v:bilibili:{MULTI}"]["position"], 0.0)
        self.assertEqual(saved[str(self.alice)][f"v:bilibili:{MULTI}"]["position"], 0.0)

    def test_continue_watching_shows_the_part(self):
        answer = self.watch(url=f"https://www.bilibili.com/video/{MULTI}?p=2")
        self.report(answer, position=321.0)
        self.relay.forget_home(self.as_carol)
        rows = json.loads(self.request("GET", "/tv/api/home", "carol")[2])["continue"]
        row = next(r for r in rows if r["id"] == f"pc:v:bilibili:{MULTI}")
        self.assertEqual((row["title"], row["subtitle"]), ("P2 标题", "超清《葫芦娃》全1-3部 · P2"))
        self.assertEqual(row["play"], {"kind": "history", "site": "bilibili", "id": MULTI})
        self.assertEqual(row["progress"]["position"], 321)

    def test_cloud_relay_has_no_progress_endpoint(self):
        self.relay.watch = None
        self.assertEqual(self.request("POST", "/tv/api/watch/progress", "carol", {"viewer": "c0ffee00"})[0], 404)


class PlaylistOriginTests(unittest.TestCase):
    def test_origin_is_the_first_fragments_media_time(self):
        tracks = watch.init_info(moov())
        frag = fragment(2.0)
        moof = frag[:frag.index(b"mdat") - 4]
        self.assertIsNone(watch.fragment_start(moof, tracks), "no tfdt")
        tfdt = box(b"tfdt", b"\1\0\0\0" + struct.pack(">Q", 90000 * 12))       # version 1: 64-bit time
        traf = full(b"tfhd", struct.pack(">II", 1, 3600), flags=0x08) + tfdt + full(b"trun", struct.pack(">I", 50))
        self.assertAlmostEqual(watch.fragment_start(box(b"moof", box(b"traf", traf)), tracks), 12.0)
        tfdt = full(b"tfdt", struct.pack(">I", 90000 * 3))                           # version 0: 32-bit
        traf = full(b"tfhd", struct.pack(">II", 1, 3600), flags=0x08) + tfdt + full(b"trun", struct.pack(">I", 50))
        self.assertAlmostEqual(watch.fragment_start(box(b"moof", box(b"traf", traf)), tracks), 3.0)
        window = watch.Window()
        window.add(2.0, b"a", 12.0)
        window.add(2.0, b"b", 14.0)
        self.assertIn("#EXT-X-RTV-ORIGIN:12.000\n", window.playlist())


class KeepAliveTests(unittest.TestCase):
    setUp = base.RelayWatchTests.setUp
    tearDown = base.RelayWatchTests.tearDown
    online = base.RelayWatchTests.online
    selfhost = True

    def test_an_unread_post_body_closes_the_connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=10)
        body = json.dumps({"viewer": "c0ffee00", "channel": "ch000"}).encode()
        connection.request("POST", "/tv/api/watch", body=body, headers={"Content-Type": "application/json",
                                                                         "Cookie": "tvs=expired"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 404)
        self.assertEqual(response.getheader("Connection"), "close")
        connection.close()
        # A normal request keeps the connection open for the next one.
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=10)
        for _ in range(2):
            connection.request("POST", "/tv/api/watch/stop", body=json.dumps({"viewer": "c0ffee00"}).encode(),
                               headers={"Content-Type": "application/json", "Cookie": self.cookies["alice"]})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.getheader("Connection"))
            response.read()
        connection.close()


if __name__ == "__main__":
    unittest.main()
