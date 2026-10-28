"""Series and creator detail pages and the unified progress API (server/relay/detail.py; media-center design M4).

What is checked:
- Marks reads the TV's per-account state (positions without a time, watched ids), the PC's pc-progress.json
  records (position, duration, time, done parts) and library records (lib:<id>, with the TV's device name)
  without changing any of them; "finished" anywhere wins, the furthest position wins (TV positions carry
  no time), and a position within the last 30 s (or tenth) counts as finished.
- A series' resume episode: the last episode with any mark if unfinished, else the next one; finished when
  the last episode is; never started is episode 1. A creator channel counts what is left.
- GET /api/progress?keys= answers v:, lib: and ch: keys per caller (another user sees null), refuses
  over-long keys, and is a 404 for guests.
- GET /api/detail/series is the poster wall with merged progress and which TVs play a series; GET
  /api/detail/series/<id> lists episodes with per-episode marks; GET /api/detail/up/<id or follow key>
  lists a creator's uploads with marks, links a subscribed channel and a followed key both ways, and fetches
  the uploads of a followed-only creator.
- Home's "New episodes" and "Creator updates" use the same marks (an episode finished on the PC moves the
  series on; a video finished on the PC is no longer "new").
- The Bilibili client keeps a series' description and the episodes' publish time.
- The console's pure detail-page helpers (console_detail_test.mjs) under Node.
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import now_playing
from server.relay import detail, setup, web
from server.relay.bilibili import Bilibili
from server.tenancy import files as device_files
from server.tenancy.accounts import Accounts

from test_relay_bilibili import PAGES, SEASON, SPI, FakeFetch
from test_relay_web import KEY, FakeClient, FakeYouTube

DEVICE = "aaaaaaaaaaa1"
SECRET = "s" * 43


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = time.time()
        self.accounts = Accounts(self.root / "tv.db")
        self.accounts.create_user("管理员", "admin")         # a set-up server (else every call is "go to /tv/setup")
        self.alice = self.accounts.create_user("Alice")
        self.bob = self.accounts.create_user("Bob")
        row = self.accounts.register_device(DEVICE, "xingzhi-cube-154", SECRET)
        self.accounts.bind(row["claim_code"], self.alice)
        self.devices_dir = self.root / "devices"
        main = self.root / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://x/1.m3u8\n", encoding="utf-8")
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, youtube=FakeYouTube(), main_channels=main,
                               accounts=self.accounts, devices_dir=self.devices_dir, log=lambda _m: None,
                               clock=lambda: self.now, selfhost=setup.SelfHost())
        self.relay.sync_devices(force=True)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        self.streams.server_close()
        threading.Thread(target=self.manage.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.cookies = {"alice": "tvs=" + self.accounts.new_session(self.alice, "pc"),
                        "bob": "tvs=" + self.accounts.new_session(self.bob, "pc"),
                        "guest": "tvs=" + self.accounts.new_session(None, "guest", device_id=DEVICE)}
        self.as_alice = web.Caller(user=self.alice, role="user")
        self.relay.add_series("https://www.bilibili.com/bangumi/play/ss1", "", self.as_alice)     # sr01: ep1/1..ep3/3
        self.relay.add_creator("https://space.bilibili.com/42", self.as_alice)                    # up01: a, b, c
        self.state = self.relay.user_state(self.alice)
        self.pc = self.relay.pc_progress

    def tearDown(self):
        if self.relay.watch is not None:
            self.relay.watch.stop_all()
        self.manage.shutdown()
        self.manage.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def call(self, path, who="alice"):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=20)
        connection.request("GET", "/tv" + path, headers={"Cookie": self.cookies[who]})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, (json.loads(raw) if response.status == 200 else None)

    def marks(self, user=None):
        return detail.Marks(self.relay, web.Caller(user=user or self.alice, role="user"), self.state)

    # ---- merging ----

    def test_marks_merge_tv_pc_and_library_without_writing(self):
        self.state.save_position("up01", "a", 200.0)          # TV: 200 s, no time
        self.state.mark_watched("sr01", "ep1/1")              # TV: finished
        self.pc.put(self.alice, "up01", {"bvid": "a", "position": 120, "duration": 600})   # PC: behind the TV
        self.pc.put(self.alice, "up01x", {"bvid": "b", "position": 300, "duration": 600})  # PC only
        self.pc.put(self.alice, "v:bilibili:BV1xx411c7mD", {"bvid": "BV1xx411c7mD", "position": 590, "duration": 600})
        self.pc.put(self.alice, "lib:abc", {"bvid": "abc", "position": 61, "duration": 3000, "device": "客厅"})
        self.pc.mark_done(self.alice, "sr01", "ep2/2")         # PC: finished
        before = (json.dumps(self.state.snapshot(), sort_keys=True), (self.root / "pc-progress.json").read_text())
        marks = self.marks()
        a = marks.get("a")
        self.assertEqual((a["position"], a["device"], a["finished"]), (200, "小电视", False), "the furthest wins")
        self.assertEqual(marks.get("b")["device"], "这台电脑")
        self.assertTrue(marks.get("ep1/1")["finished"], "the TV's watched set")
        self.assertTrue(marks.get("ep2/2")["finished"], "the PC's done parts")
        self.assertTrue(marks.get("BV1xx411c7mD")["finished"], "10 s before the end counts as finished")
        lib = marks.get("lib:abc")
        self.assertEqual((lib["position"], lib["device"], lib["finished"]), (61, "客厅", False))
        self.assertIsNone(marks.get("abc"), "library ids live in their own space")
        self.assertIsNone(marks.get("c"))
        self.assertIsNone(self.marks(self.bob).get("b"), "per user")
        after = (json.dumps(self.state.snapshot(), sort_keys=True), (self.root / "pc-progress.json").read_text())
        self.assertEqual(before, after, "reading never writes")

    def test_series_resume_rules(self):
        series = self.state.get("sr01")
        fresh = self.marks().channel(series)
        self.assertEqual((fresh["started"], fresh["finished"], fresh["episode"], fresh["watched"]), (False, False, 1, 0))
        self.pc.put(self.alice, "sr01", {"bvid": "ep2/2", "position": 95, "duration": 600})
        half = self.marks().channel(series)
        self.assertEqual((half["episode"], half["position"], half["device"], half["started"]), (2, 95, "这台电脑", True))
        self.pc.mark_done(self.alice, "sr01", "ep2/2")
        nxt = self.marks().channel(series)
        self.assertEqual((nxt["episode"], nxt["position"], nxt["watched"]), (3, 0, 1), "after a finished episode: the next")
        self.state.mark_watched("sr01", "ep3/3")
        done = self.marks().channel(series)
        self.assertEqual((done["finished"], done["episode"]), (True, 1), "the last one finished: watch again from 1")
        up = self.marks().channel(self.state.get("up01"))
        self.assertEqual((up["kind"], up["left"], up["episode"]), ("up", 3, 1))
        self.state.mark_watched("up01", "a")
        self.assertEqual(self.marks().channel(self.state.get("up01"))["episode"], 2, "the newest unwatched upload")

    # ---- /api/progress ----

    def test_progress_api(self):
        self.pc.put(self.alice, "sr01", {"bvid": "ep2/2", "position": 95, "duration": 600})
        self.pc.put(self.alice, "lib:abc", {"bvid": "abc", "position": 61, "duration": 3000})
        keys = "v:bilibili:ep2/2,lib:abc,ch:sr01,v:bilibili:nope,junk"
        status, answer = self.call("/api/progress?keys=" + keys.replace("/", "%2F"))
        self.assertEqual(status, 200)
        items = answer["items"]
        self.assertEqual(items["v:bilibili:ep2/2"]["position"], 95)
        self.assertEqual(items["lib:abc"]["duration"], 3000)
        self.assertEqual((items["ch:sr01"]["episode"], items["ch:sr01"]["total"]), (2, 3))
        self.assertIsNone(items["v:bilibili:nope"])
        self.assertIsNone(items["junk"])
        bob = self.call("/api/progress?keys=ch:sr01,lib:abc", "bob")[1]
        self.assertEqual(bob["items"], {"ch:sr01": None, "lib:abc": None}, "Alice's progress is hers")
        self.assertFalse(self.call("/api/progress?keys=" + "x" * 200)[1]["ok"])
        self.assertFalse(self.call("/api/progress")[1]["ok"])
        self.assertEqual(self.call("/api/progress?keys=ch:sr01", "guest")[0], 404)

    # ---- detail pages ----

    def test_series_wall_and_detail(self):
        self.pc.put(self.alice, "sr01", {"bvid": "ep2/2", "position": 95, "duration": 600})
        self.state.mark_watched("sr01", "ep1/1")
        now_playing.write(device_files.device_files(self.devices_dir, DEVICE).now, "sr01", True, True,
                          now=self.now, since=self.now)
        wall = self.call("/api/detail/series")[1]
        self.assertEqual([s["id"] for s in wall["items"]], ["sr01"])
        poster = wall["items"][0]
        self.assertEqual((poster["progress"]["episode"], poster["progress"]["watched"]), (2, 1))
        self.assertEqual(len(poster["on_tv"]), 1, "the TV plays it")
        page = self.call("/api/detail/series/sr01")[1]
        self.assertTrue(page["ok"])
        self.assertEqual(page["total"], 3)
        marks = [(e["n"], e["key"], e["mark"] and e["mark"]["finished"], e["mark"] and e["mark"]["position"])
                 for e in page["episodes"]]
        self.assertEqual(marks, [(1, "v:bilibili:ep1/1", True, 0), (2, "v:bilibili:ep2/2", False, 95),
                                 (3, "v:bilibili:ep3/3", None, None)])
        self.assertEqual(page["tv_next"], 2, "the TV's own cursor, reported as is")
        self.assertFalse(self.call("/api/detail/series/sr09")[1]["ok"])
        self.assertFalse(self.call("/api/detail/series/up01")[1]["ok"])
        self.assertFalse(self.call("/api/detail/series/sr01", "bob")[1]["ok"], "Bob has no sr01")
        self.assertEqual(self.call("/api/detail/series", "guest")[0], 404)

    def test_creator_detail_and_follows(self):
        self.pc.mark_done(self.alice, "up01", "b")
        page = self.call("/api/detail/up/up01")[1]
        self.assertTrue(page["ok"], page)
        self.assertEqual(page["id"], "up01")
        self.assertEqual([x["title"] for x in page["videos"]], ["ta", "tb", "tc"])
        self.assertEqual(page["videos"][1]["play"], {"kind": "video", "channel": "up01", "index": 2})
        self.assertTrue(page["videos"][1]["mark"]["finished"])
        self.assertEqual(page["progress"]["left"], 2)
        self.assertFalse(self.call("/api/detail/up/up09")[1]["ok"])
        self.assertFalse(self.call("/api/detail/up/nonsense")[1]["ok"])
        follows = self.relay.account_feeds.follows
        follows.background = False
        follows.store.update(self.alice, lambda d: d["follows"].update(items={
            "bilibili:42": {"site": "bilibili", "id": "42", "name": "UP42", "sign": "签名"},
            "bilibili:77": {"site": "bilibili", "id": "77", "name": "UP77"}}))
        linked = self.call("/api/detail/up/up01")[1]
        self.assertEqual((linked["key"], linked["followed"], linked["sign"]), ("bilibili:42", True, "签名"))
        by_key = self.call("/api/detail/up/bilibili:42")[1]
        self.assertEqual((by_key["id"], len(by_key["videos"])), ("up01", 3), "a followed key on the TV is that channel")
        self.pc.put(self.alice, "v:bilibili:new", {"bvid": "new", "position": 100, "duration": 600})
        only = self.call("/api/detail/up/bilibili:77")[1]
        self.assertTrue(only["ok"], only)
        self.assertEqual((only["id"], only["can_follow_tv"], only["followed"]), ("", True, True))
        self.assertEqual([x["play"]["kind"] for x in only["videos"]], ["feed"] * 3)
        self.assertEqual(only["videos"][0]["mark"]["position"], 100, "uploads of a followed-only creator carry marks")
        self.assertFalse(self.call("/api/detail/up/bilibili:99")[1]["ok"], "not followed")

    def test_home_rows_use_the_same_marks(self):
        self.pc.mark_done(self.alice, "sr01", "ep1/1")
        self.pc.mark_done(self.alice, "up01", "a")
        home = self.call("/api/home")[1]
        series = home["series_updates"][0]
        self.assertEqual((series["series"]["episode"], series["play"]["episode"]), (2, 2))
        self.assertIn("看到第 2 集", series["subtitle"])
        self.assertNotIn("ta", [x["title"] for x in home["creator_updates"]], "finished on the PC: not new")
        creator = self.state.get("up01")
        self.assertEqual((creator.watched, self.state.get("sr01").watched), ([], []), "the TV's state is untouched")


class BilibiliDescTests(unittest.TestCase):
    def test_season_desc_and_publish_time(self):
        season = json.loads(json.dumps(SEASON))
        season["result"]["evaluate"] = "  海尔兄弟的故事  "
        season["result"]["episodes"][0]["pub_time"] = 1700000000
        client = Bilibili(fetch_json=FakeFetch({"finger/spi": SPI, "pgc/view/web/season": season, "web-interface/view": PAGES}))
        _, episodes = client.series("ss2307")
        self.assertEqual(client.desc("ss2307"), "海尔兄弟的故事")
        self.assertEqual(episodes[0].pubdate, 1700000000)
        self.assertEqual(episodes[1].pubdate, 0, "no pub_time: 0 as before")
        self.assertEqual(client.desc("BV1none"), "")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DetailJsTests(unittest.TestCase):
    """The console's detail-page helpers (routes, labels, episode window, merging marks) as pure JS."""

    def test_pure_functions(self):
        result = subprocess.run(["node", str(Path(__file__).resolve().parent / "console_detail_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
