"""The media-center console's endpoints (server/relay/home.py, logos.py; docs/superpowers/specs/2026-10-24-media-center-ui-design.md).

What is checked, over real sockets like the other relay tests:
- GET /api/targets lists the caller's TVs (online, what they play) plus "this computer" only on a
  self-hosted relay; a guest sees just its device; no PC row on the cloud relay.
- POST /api/play dispatches to the existing paths: a live channel to a TV writes the switch event, an
  episode picks it, a cast link casts; an offline TV answers reason "offline"; someone else's device
  is a 404; "pc" opens a PC stream on a self-hosted relay and is a 404 on the cloud relay.
- POST /api/sleep sets and cancels the per-device sleep timer (the voice command's) and /api/state
  reports it; offline, other users' devices and bad input are refused.
- GET /api/home: per caller, cached a few seconds and dropped after a play; guests only get live
  channels; creator and series rows come from the caller's own subscriptions; a logo URL is offered
  only for channels whose M3U meta has one.
- The PC's own progress is persisted per user (pc-progress.json), shown only to that user in
  Continue watching, survives a relay restart as the resume point, and never touches the TV's state.
- The logo cache fetches once, keeps raster images up to the size cap, refuses SVG, non-http(s) and
  private addresses, remembers failures; the thumbnail proxy accepts only i.ytimg.com's fixed names.
- The console page, its stylesheet and script are served with a CSP that allows the stylesheet; the
  classic console's old addresses redirect to /tv/console.
- Bilibili covers missing after a restart (they live only in the client's memory) are fetched again in
  the background, once per channel, and /api/home says so (covers_pending) until they are back.
- The hero's thumbnail size "h" asks the image host for a big uncropped picture; GET /api/bili/space
  pages one creator's uploads (validated, cached, account only); the page's hero, shelf and creator
  filter helpers are tested as pure JS (console_shelves_test.mjs).
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import now_playing
from server.relay import bilibili, bilisearch, console, events, home, logos, setup, web
from server.tenancy import files as device_files
from server.tenancy.accounts import Accounts

from test_relay_watch import LIVE_DATA, Popen
from test_relay_web import KEY, FakeClient, FakeYouTube

ALICE_DEVICE = "aaaaaaaaaaa1"
BOB_DEVICE = "bbbbbbbbbbb2"
SECRET = "s" * 43
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
DIALS = {"volume": 50, "brightness": 1, "steps": 5, "pet": 0, "audio_only": 0}


class HomeTests(unittest.TestCase):
    selfhost = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = time.time()
        self.accounts = Accounts(self.root / "tv.db")
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.bob = self.accounts.create_user("Bob")
        self.carol = self.accounts.create_user("Carol")       # no device
        for device, user in ((ALICE_DEVICE, self.alice), (BOB_DEVICE, self.bob)):
            row = self.accounts.register_device(device, "xingzhi-cube-154", SECRET)
            self.accounts.bind(row["claim_code"], user)
        self.devices_dir = self.root / "devices"
        self.main = self.root / "channels.txt"
        self.main.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | 湖南卫视 | https://x/2.m3u8\n", encoding="utf-8")
        (self.root / "channels.txt.meta.json").write_text(json.dumps({"ch000": {"logo": "https://logo.example/cctv1.png"}}),
                                                          encoding="utf-8")
        self.start_relay()
        self.cookies = {name: "tvs=" + self.accounts.new_session(uid, "pc") for name, uid in
                        (("alice", self.alice), ("bob", self.bob), ("carol", self.carol))}
        self.cookies["guest"] = "tvs=" + self.accounts.new_session(None, "guest", device_id=ALICE_DEVICE)

    def start_relay(self):
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, youtube=FakeYouTube(), main_channels=self.main,
                               accounts=self.accounts, devices_dir=self.devices_dir, log=lambda _m: None,
                               clock=lambda: self.now, selfhost=setup.SelfHost() if self.selfhost else None)
        self.relay.sync_devices(force=True)
        self.fetched = []

        def fetcher(url):
            self.fetched.append(url)
            return PNG
        self.relay.logos = logos.LogoCache(self.root / "logos", self.main, fetcher=fetcher)
        if self.relay.watch is not None:
            self.relay.watch.popen = Popen(data=LIVE_DATA)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        self.streams.server_close()
        threading.Thread(target=self.manage.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop_relay(self):
        if self.relay.watch is not None:
            self.relay.watch.stop_all()
        self.manage.shutdown()
        self.manage.server_close()

    def tearDown(self):
        self.stop_relay()
        self.accounts.close()
        self.tmp.cleanup()

    def online(self, device, channel="ch000", dials=True):
        now_playing.write(device_files.device_files(self.devices_dir, device).now, channel, True, True,
                          now=self.now, since=self.now, device=dict(DIALS) if dials else None)

    def request(self, method, path, who=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=20)
        headers = {"Cookie": self.cookies[who]} if who else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        connection.close()
        return result

    def call(self, method, path, who="alice", body=None):
        status, _, raw = self.request(method, "/tv" + path, who, body)
        return status, (json.loads(raw) if status == 200 else None)

    def last_event(self, device=ALICE_DEVICE):
        return events.read_event(device_files.device_files(self.devices_dir, device).events)

    # ---- /api/targets ----

    def test_targets(self):
        self.online(ALICE_DEVICE, "ch001")
        status, answer = self.call("GET", "/api/targets")
        self.assertEqual(status, 200)
        rows = {t["id"]: t for t in answer["targets"]}
        self.assertEqual(set(rows), {ALICE_DEVICE, "pc"})
        tv = rows[ALICE_DEVICE]
        self.assertTrue(tv["online"])
        self.assertEqual((tv["now"]["channel"], tv["now"]["name"]), ("ch001", "湖南卫视"))
        self.assertTrue(tv["caps"]["listen"] and tv["caps"]["switch"])
        self.assertTrue(tv["mine"])
        self.assertEqual(rows["pc"]["kind"], "pc")
        bob = {t["id"]: t for t in self.call("GET", "/api/targets", "bob")[1]["targets"]}
        self.assertFalse(bob[BOB_DEVICE]["online"])
        self.assertIsNone(bob[BOB_DEVICE]["now"])
        carol = self.call("GET", "/api/targets", "carol")[1]
        self.assertEqual([t["id"] for t in carol["targets"]], ["pc"], "no TV: only this computer")
        self.assertEqual(carol["auto"], "pc")
        guest = self.call("GET", "/api/targets", "guest")[1]
        self.assertEqual([t["id"] for t in guest["targets"]], [ALICE_DEVICE], "a guest never gets the PC row")
        self.assertEqual(self.request("GET", "/tv/api/targets")[0], 404, "signed out")

    # ---- /api/play ----

    def test_play_live_channel_to_a_tv(self):
        self.online(ALICE_DEVICE, "ch000")
        status, answer = self.call("POST", "/api/play", body={"target": ALICE_DEVICE,
                                                              "item": {"kind": "live", "channel": "ch001"}})
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["target"], answer["channel"]), (ALICE_DEVICE, "ch001"))
        self.assertIn("at", answer)
        event = self.last_event()
        self.assertEqual((event.action, event.channel), ("switch", "ch001"))
        # No target: the caller's auto device.
        self.assertTrue(self.call("POST", "/api/play", body={"item": {"kind": "live", "channel": "ch000"}})[1]["ok"])

    def test_play_refusals(self):
        answer = self.call("POST", "/api/play", body={"target": ALICE_DEVICE, "item": {"kind": "live", "channel": "ch001"}})[1]
        self.assertEqual((answer["ok"], answer["reason"]), (False, "offline"))
        self.assertIsNone(self.last_event(), "nothing sent to an offline TV")
        self.online(ALICE_DEVICE)
        self.assertEqual(self.call("POST", "/api/play", "bob", {"target": ALICE_DEVICE,
                                                               "item": {"kind": "live", "channel": "ch001"}})[0], 404)
        for body in ({"target": ALICE_DEVICE}, {"target": ALICE_DEVICE, "item": {"kind": "nope"}},
                     {"target": ALICE_DEVICE, "item": {"kind": "episode", "channel": "sr01"}},
                     {"target": 5, "item": {"kind": "live", "channel": "ch001"}}):
            answer = self.call("POST", "/api/play", body=body)[1]
            self.assertFalse(answer["ok"], body)
            self.assertEqual(answer["reason"], "bad", body)
        answer = self.call("POST", "/api/play", body={"target": ALICE_DEVICE, "item": {"kind": "live", "channel": "ch404"}})[1]
        self.assertFalse(answer["ok"])
        self.assertIn("没有这个台", answer["error"])
        carol = self.call("POST", "/api/play", "carol", {"item": {"kind": "live", "channel": "ch001"}})[1]
        self.assertEqual(carol["reason"], "no_device")

    def test_play_episode_cast_and_creator_video(self):
        self.relay.add_series("https://www.bilibili.com/bangumi/play/ss1", "", web.Caller(user=self.alice, role="user"))
        self.relay.add_creator("https://space.bilibili.com/42", web.Caller(user=self.alice, role="user"))
        self.online(ALICE_DEVICE)
        answer = self.call("POST", "/api/play", body={"target": ALICE_DEVICE,
                                                      "item": {"kind": "episode", "channel": "sr01", "episode": 2}})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["episode"], answer["channel"]), (2, "sr01"))
        self.assertEqual(self.relay.user_state(self.alice).episode("sr01"), 2, "picking an episode on a TV moves it")
        answer = self.call("POST", "/api/play", body={"target": ALICE_DEVICE,
                                                      "item": {"kind": "video", "channel": "up01", "index": 3}})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertIn("下一个没看的", answer["note"], "the TV plays the creator channel from its own next video")
        answer = self.call("POST", "/api/play", body={"target": ALICE_DEVICE, "item": {
            "kind": "cast", "url": "https://www.bilibili.com/video/BV1xx411c7mD"}})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["channel"], "cast")
        answer = self.call("POST", "/api/play", body={"target": ALICE_DEVICE, "item": {
            "kind": "history", "site": "bilibili", "id": "BV1xx411c7mD"}})[1]
        self.assertTrue(answer["ok"], answer)

    def test_play_on_the_pc(self):
        status, answer = self.call("POST", "/api/play", "carol", {"target": "pc", "viewer": "c0ffee00",
                                                                  "item": {"kind": "live", "channel": "ch001"}})
        self.assertEqual(status, 200)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["target"], answer["kind"], answer["channel"]), ("pc", "live", "ch001"))
        self.assertTrue(answer["url"].startswith("watch/"))
        answer = self.call("POST", "/api/play", "guest", {"target": "pc", "item": {"kind": "live", "channel": "ch001"}})[1]
        self.assertFalse(answer["ok"])
        self.assertIn("访客", answer["error"])

    # ---- /api/sleep ----

    def test_sleep_timer(self):
        self.online(ALICE_DEVICE)
        answer = self.call("POST", "/api/sleep", body={"device": ALICE_DEVICE, "minutes": 30})[1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["timer"], {"kind": "time", "left": 1800})
        state = self.call("GET", f"/api/state?device={ALICE_DEVICE}")[1]
        self.assertEqual(state["sleep"], {"kind": "time", "left": 1800})
        self.now += 600
        self.online(ALICE_DEVICE)
        self.assertEqual(self.call("GET", f"/api/state?device={ALICE_DEVICE}")[1]["sleep"]["left"], 1200)
        # The guest of that TV may set it too, like pausing; the voice command sees the same timer.
        self.assertTrue(self.call("POST", "/api/sleep", "guest", {"minutes": 15})[1]["ok"])
        self.assertEqual(self.call("POST", "/api/voice/timer", body={"device": ALICE_DEVICE, "action": "status"})[1]["timer"],
                         {"kind": "time", "left": 900})
        cancelled = self.call("POST", "/api/sleep", body={"device": ALICE_DEVICE, "minutes": 0})[1]
        self.assertEqual((cancelled["ok"], cancelled["timer"]), (True, None))
        self.assertIsNone(self.call("GET", f"/api/state?device={ALICE_DEVICE}")[1]["sleep"])
        for minutes in (-1, 1.5, True, "30", 24 * 60):
            self.assertFalse(self.call("POST", "/api/sleep", body={"device": ALICE_DEVICE, "minutes": minutes})[1]["ok"], minutes)
        self.assertEqual(self.call("POST", "/api/sleep", "bob", {"device": ALICE_DEVICE, "minutes": 30})[0], 404)
        self.assertIn("没在线", self.call("POST", "/api/sleep", "bob", {"device": BOB_DEVICE, "minutes": 30})[1]["error"])
        self.assertIn("还没有绑定", self.call("POST", "/api/sleep", "carol", {"minutes": 30})[1]["error"])

    # ---- /api/home ----

    def test_home_rows_per_caller(self):
        as_alice = web.Caller(user=self.alice, role="user")
        self.relay.add_creator("https://space.bilibili.com/42", as_alice)
        self.relay.add_series("https://www.bilibili.com/bangumi/play/ss1", "", as_alice)
        self.online(ALICE_DEVICE, "ch001")
        status, answer = self.call("GET", "/api/home")
        self.assertEqual(status, 200)
        live = {x["channel"]["id"]: x for x in answer["live_now"]}
        self.assertEqual(set(live), {"ch000", "ch001"})
        self.assertEqual(answer["live_now"][0]["channel"]["id"], "ch001", "what the TV plays comes first")
        self.assertTrue(live["ch001"]["on_tv"])
        self.assertEqual(live["ch000"]["channel"]["logo"], "api/art/logo/ch000")
        self.assertEqual(live["ch001"]["channel"]["logo"], "", "no tvg-logo, no URL: the page draws a badge")
        self.assertEqual(live["ch001"]["play"], {"kind": "live", "channel": "ch001"})
        self.assertEqual([x["title"] for x in answer["creator_updates"]], ["ta", "tb", "tc"])
        self.assertEqual(answer["creator_updates"][0]["play"], {"kind": "video", "channel": "up01", "index": 1})
        self.assertEqual(answer["series_updates"][0]["play"], {"kind": "episode", "channel": "sr01", "episode": 1})
        self.assertTrue(answer["hero"])
        self.assertEqual(answer["library_recent"], [])
        bob = self.call("GET", "/api/home", "bob")[1]
        self.assertEqual((bob["creator_updates"], bob["series_updates"]), ([], []), "Alice's subscriptions are hers")
        guest = self.call("GET", "/api/home", "guest")[1]
        self.assertTrue(guest["guest"])
        self.assertEqual((guest["creator_updates"], guest["continue"]), ([], []))
        self.assertEqual(len(guest["live_now"]), 2)

    def test_home_is_cached_and_dropped_after_a_play(self):
        first = home.home(self.relay, web.Caller(user=self.alice, role="user"))
        self.assertIs(home.home(self.relay, web.Caller(user=self.alice, role="user")), first)
        self.assertIsNot(home.home(self.relay, web.Caller(user=self.bob, role="user")), first)
        self.online(ALICE_DEVICE)
        self.call("POST", "/api/play", body={"target": ALICE_DEVICE, "item": {"kind": "live", "channel": "ch001"}})
        self.assertIsNot(home.home(self.relay, web.Caller(user=self.alice, role="user")), first)

    def test_missing_bilibili_covers_are_fetched_again_in_the_background(self):
        # The cover and face addresses live only in the Bilibili client's memory (the lists themselves are
        # saved), so after a restart "UP 主更新" and "剧集追更" were placeholders until each channel was
        # played. Home now asks for the list once more in the background and says covers are coming.
        class CoverClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.covers, self.faces, self.asked = {}, {}, []

            def cover(self, key):
                return self.covers.get(key, "")

            def face(self, mid):
                return self.faces.get(mid, "")

            def uploads(self, mid):
                self.asked.append(("up", mid))
                found = super().creator(mid)[1]          # the same newest uploads the channel was saved with
                for v in found:
                    self.covers[v.bvid] = f"https://i0.hdslb.com/bfs/archive/{v.bvid}.jpg"
                self.faces[mid] = "https://i0.hdslb.com/bfs/face/f.jpg"
                return found

            def series(self, source):
                self.asked.append(("series", source))
                self.covers[source] = "https://i0.hdslb.com/bfs/bangumi/p.jpg"
                return super().series(source)

        as_alice = web.Caller(user=self.alice, role="user")
        self.relay.add_creator("https://space.bilibili.com/42", as_alice)
        self.relay.add_series("https://www.bilibili.com/bangumi/play/ss1", "", as_alice)
        client = self.relay.clients["bilibili"] = CoverClient()       # a restarted relay: nothing in memory
        held = []
        self.relay._cover_warm = home.CoverWarm(self.relay, start=held.append, clock=lambda: self.now,
                                                sleep=lambda _s: None)
        first = self.call("GET", "/api/home")[1]
        self.assertEqual([x["art"]["url"] for x in first["creator_updates"]], ["", "", ""])
        self.assertEqual(first["series_updates"][0]["art"]["url"], "")
        self.assertTrue(first["covers_pending"], "the page is told to ask again in a few seconds")
        self.assertEqual(len(held), 1, "one worker for the queue")
        held[0]()
        self.assertEqual(client.asked, [("up", "42"), ("series", "ss1")])
        self.relay._home_cache.clear()
        again = self.call("GET", "/api/home")[1]
        self.assertNotIn("covers_pending", again)
        for x in again["creator_updates"]:
            self.assertEqual(x["art"]["url"], home.thumb(f"https://i0.hdslb.com/bfs/archive/{x['id'].split(':')[2]}.jpg"))
            self.assertEqual(x["creator"]["face"], home.thumb("https://i0.hdslb.com/bfs/face/f.jpg", "f"))
        self.assertEqual(again["series_updates"][0]["art"]["url"], home.thumb("https://i0.hdslb.com/bfs/bangumi/p.jpg", "p"))
        # A creator whose covers still do not come back is not asked again for half an hour.
        client.covers.clear()
        self.relay._home_cache.clear()
        self.call("GET", "/api/home")
        self.assertEqual(len(client.asked), 2)

    def test_creator_uploads_from_search_need_an_account(self):
        self.relay.clients["bilibili"] = SpaceTests.Client()
        status, answer = self.call("GET", "/api/bili/space?mid=546195&page=2")
        self.assertEqual(status, 200)
        self.assertEqual((answer["page"], len(answer["items"])), (2, 30))
        self.assertEqual(self.call("GET", "/api/bili/space?mid=546195", "guest")[0], 404)
        self.assertEqual(self.request("GET", "/tv/api/bili/space?mid=546195")[0], 404, "signed out")

    # ---- the PC's own progress ----

    def test_pc_progress_is_per_user_persisted_and_leaves_the_tv_alone(self):
        as_carol = web.Caller(user=self.carol, role="user")
        self.relay.add_creator("https://space.bilibili.com/42", as_carol)
        state = self.relay.user_state(self.carol)
        state.save_position("up01", "b", 130.0)
        answer = self.call("POST", "/api/play", "carol", {"target": "pc", "viewer": "c0ffee00",
                                                          "item": {"kind": "channel", "channel": "up01"}})[1]
        self.assertTrue(answer["ok"], answer)
        stream = next(s for s in self.relay.watch.streams() if s.sid == answer["sid"])
        self.assertTrue(stream.wait_ready(5))
        deadline = time.time() + 5
        while stream.window.total_s < 4 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.call("POST", "/api/watch/stop", "carol", {"viewer": "c0ffee00"})[1]["ok"], True)
        saved = json.loads((self.root / "pc-progress.json").read_text(encoding="utf-8"))
        self.assertEqual(list(saved), [str(self.carol)], "kept per user")
        record = saved[str(self.carol)]["up01"]
        self.assertEqual((record["bvid"], record["title"], record["index"]), ("b", "tb", 2))
        position = record["position"]
        self.assertGreater(position, 110)
        carol = self.call("GET", "/api/home", "carol")[1]
        pc_rows = [x for x in carol["continue"] if x["id"].startswith("pc:")]
        self.assertEqual(len(pc_rows), 1)
        self.assertEqual(pc_rows[0]["progress"]["device"], "这台电脑")
        self.assertEqual(pc_rows[0]["play"], {"kind": "video", "channel": "up01", "index": 2})
        alice = self.call("GET", "/api/home", "alice")[1]
        self.assertFalse([x for x in alice["continue"] if x["id"].startswith("pc:")], "nobody else sees it")
        # The TV's state is untouched.
        creator = self.relay.user_state(self.carol).get("up01")
        self.assertEqual((creator.watched, creator.positions), ([], {"b": 130.0}))
        # After a restart the PC resumes from the saved spot, not from the TV's.
        self.stop_relay()
        self.start_relay()
        again = self.call("POST", "/api/play", "carol", {"target": "pc", "viewer": "c0ffee00",
                                                         "item": {"kind": "channel", "channel": "up01"}})[1]
        self.assertTrue(again["ok"], again)
        source = next(s for s in self.relay.watch.streams() if s.sid == again["sid"]).source
        self.assertAlmostEqual(source.start, position, delta=0.2)

    # ---- logos ----

    def test_logo_endpoint(self):
        status, headers, body = self.request("GET", "/tv/api/art/logo/ch000", "guest")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(body, PNG)
        self.assertEqual(self.request("GET", "/tv/api/art/logo/ch000", "alice")[2], PNG)
        self.assertEqual(self.fetched, ["https://logo.example/cctv1.png"], "fetched once, then from disk")
        self.assertEqual(len(list((self.root / "logos").glob("*.png"))), 1)
        self.assertEqual(self.request("GET", "/tv/api/art/logo/ch001", "alice")[0], 404)
        self.assertEqual(self.request("GET", "/tv/api/art/logo/ch000")[0], 404, "signed out")
        self.assertEqual(self.request("GET", "/tv/api/art/logo/..%2Fx", "alice")[0], 404)

    # ---- the pages ----

    def test_console_page_and_assets(self):
        status, headers, page = self.request("GET", "/tv/console")
        self.assertEqual(status, 200)
        policy = headers["Content-Security-Policy"]
        self.assertIn("style-src 'self' 'unsafe-inline'", policy)
        self.assertIn("img-src 'self' data:", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        text = page.decode("utf-8")
        self.assertNotIn("__CONSOLE_", text)
        self.assertRegex(text, r'href="console\.css\?v=[0-9a-f]{12}"')
        self.assertRegex(text, r'src="console\.js\?v=[0-9a-f]{12}"')
        self.assertNotIn("https://", text.replace("http://www.w3.org", ""))
        self.assertRegex(text, r'href="console-admin\.css\?v=[0-9a-f]{12}"')
        self.assertRegex(text, r'src="console-admin\.js\?v=[0-9a-f]{12}"')
        for name, kind in (("console.css", "text/css"), ("console.js", "text/javascript"),
                           ("console-admin.css", "text/css"), ("console-admin.js", "text/javascript")):
            status, headers, body = self.request("GET", "/tv/" + name)
            self.assertEqual(status, 200, name)
            self.assertTrue(headers["Content-Type"].startswith(kind), name)
            # Links the page sends to the relay (a Bilibili space URL to subscribe) are fine; loading from elsewhere is not.
            self.assertIsNone(re.search(r"""(?:src=["']?|url\(|@import\s+|fetch\(["'])https?:""", body.decode("utf-8")),
                              f"{name}: nothing loaded from elsewhere")
        # The classic console is gone; its old addresses (bookmarks) go to the new console.
        for old in ("/tv/console-classic", "/tv/console/classic", "/tv/console/"):
            status, headers, _ = self.request("GET", old, "alice")
            self.assertEqual((status, headers["Location"]), (302, "/tv/console"), old)
        self.assertNotIn("console-classic", text)
        self.assertNotIn("旧版界面", text)


class CloudHomeTests(HomeTests):
    """The owner's cloud relay (no --selfhost): the same console, no watching on the PC."""
    selfhost = False

    def test_targets(self):
        self.online(ALICE_DEVICE, "ch001")
        answer = self.call("GET", "/api/targets")[1]
        self.assertEqual([t["id"] for t in answer["targets"]], [ALICE_DEVICE], "no 这台电脑 in the cloud")
        carol = self.call("GET", "/api/targets", "carol")[1]
        self.assertEqual((carol["targets"], carol["auto"]), ([], ""))

    def test_play_on_the_pc(self):
        self.assertEqual(self.call("POST", "/api/play", "carol", {"target": "pc", "item": {"kind": "live", "channel": "ch001"}})[0],
                         404)

    def test_pc_progress_is_per_user_persisted_and_leaves_the_tv_alone(self):
        self.assertIsNone(self.relay.pc_progress)
        self.assertEqual(self.call("GET", "/api/home", "carol")[1]["continue"], [])


class LogoCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.table = self.root / "channels.txt"
        self.table.write_text("ch000 | A | https://x/1\n", encoding="utf-8")
        self.now = 0.0
        self.answers = []
        self.fetched = []

    def tearDown(self):
        self.tmp.cleanup()

    def cache(self, logo="https://logo.example/a.png"):
        (self.root / "channels.txt.meta.json").write_text(json.dumps({"ch000": {"logo": logo}}), encoding="utf-8")

        def fetcher(url):
            self.fetched.append(url)
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return logos.LogoCache(self.root / "logos", self.table, fetcher=fetcher, clock=lambda: self.now)

    def test_raster_only_and_size_cap(self):
        jpeg, gif, webp = b"\xff\xd8\xff\xe0" + b"\0" * 8, b"GIF89a" + b"\0" * 8, b"RIFF\0\0\0\0WEBPVP8 "
        for body, kind in ((jpeg, "image/jpeg"), (gif, "image/gif"), (webp, "image/webp")):
            self.assertEqual(logos.sniff(body), {"image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}[kind])
        for bad in (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", b"<html>", b""):
            self.assertIsNone(logos.sniff(bad))
        cache = self.cache()
        self.answers = [b"<svg/>"]
        self.assertIsNone(cache.get("ch000"), "SVG can carry scripts: refused")
        self.now += logos.RETRY_S + 1
        self.answers = [PNG + b"x" * logos.MAX_BYTES]
        self.assertIsNone(cache.get("ch000"), "too big")
        self.assertFalse((self.root / "logos").exists() and list((self.root / "logos").glob("*.png")))

    def test_failures_are_remembered(self):
        cache = self.cache()
        self.answers = [OSError("down")]
        self.assertIsNone(cache.get("ch000"))
        self.assertIsNone(cache.get("ch000"))
        self.assertEqual(len(self.fetched), 1, "not retried at once")
        self.now += logos.RETRY_S + 1
        self.answers = [PNG]
        self.assertEqual(cache.get("ch000"), ("image/png", PNG))
        self.assertEqual(cache.get("ch000"), ("image/png", PNG))
        self.assertEqual(len(self.fetched), 2)
        self.assertIsNone(cache.get("ch999"))
        self.assertIsNone(cache.get("../../etc"))

    def test_changed_url_is_a_new_file(self):
        self.answers = [PNG, PNG + b"2"]
        self.assertEqual(self.cache().get("ch000")[1], PNG)
        self.assertEqual(self.cache("https://logo.example/b.png").get("ch000")[1], PNG + b"2")
        self.assertEqual(len(self.fetched), 2)

    def test_address_checks(self):
        def resolver(addresses):
            return lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]
        public = resolver(["93.184.216.34"])
        self.assertTrue(logos.allowed_url("https://logo.example/a.png", resolve=public))
        self.assertTrue(logos.allowed_url("http://logo.example:8080/a.png", resolve=public))
        for url in ("ftp://logo.example/a.png", "file:///etc/passwd", "https://user:pw@logo.example/a.png",
                    "https://logo.example:22/a.png", "javascript:alert(1)", "https:///a.png"):
            self.assertFalse(logos.allowed_url(url, resolve=public), url)
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fc00::1"):
            self.assertFalse(logos.allowed_url("https://logo.example/a.png", resolve=resolver([address])), address)
        self.assertFalse(logos.allowed_url("https://logo.example/a.png", resolve=resolver(["93.184.216.34", "10.0.0.1"])),
                         "every address must be public")

        def broken(*_args, **_kwargs):
            raise OSError("no such host")
        self.assertFalse(logos.allowed_url("https://nowhere.example/a.png", resolve=broken))
        with self.assertRaises(ValueError):
            logos.fetch("https://logo.example/a.png", check=lambda _url: False)


class ThumbAllowlistTests(unittest.TestCase):
    def test_youtube_names(self):
        good = "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
        self.assertEqual(bilisearch.youtube_thumb_url(good), good)
        self.assertEqual(bilisearch.youtube_thumb_url("//i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg")
        for bad in ("https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg?x=1", "https://evil.com/vi/dQw4w9WgXcQ/mqdefault.jpg",
                    "https://i.ytimg.com.evil.com/vi/dQw4w9WgXcQ/mqdefault.jpg", "https://i.ytimg.com/vi/short/mqdefault.jpg",
                    "https://i.ytimg.com/vi/dQw4w9WgXcQ/../../x.jpg", "https://i.ytimg.com:8443/vi/dQw4w9WgXcQ/mqdefault.jpg",
                    "ftp://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg", "https://i.ytimg.com/vi/dQw4w9WgXcQ/x.png", ""):
            self.assertEqual(bilisearch.youtube_thumb_url(bad), "", bad)

    def test_youtube_goes_through_its_proxy_and_has_its_own_back_off(self):
        calls = []
        now = [0.0]

        def youtube(url, timeout, proxy):
            calls.append(("yt", url, proxy))
            if proxy == "down":
                raise OSError("unreachable")
            return "image/jpeg", b"\xff\xd8\xffjpeg"

        def bili(url, timeout):
            calls.append(("bili", url))
            return "image/webp", b"RIFFwebp"
        search = bilisearch.BiliSearch(clock=lambda: now[0], fetch=bili, fetch_youtube=youtube)
        search.youtube_proxy = lambda: "socks5://127.0.0.1:7890"
        self.assertEqual(search.thumb("https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg", "v")[0], 200)
        self.assertEqual(calls[-1], ("yt", "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg", "socks5://127.0.0.1:7890"),
                         "no Bilibili size suffix, fetched through YouTube's proxy")
        self.assertEqual(search.thumb("https://evil.com/x.jpg", "v")[0], 404)
        search.youtube_proxy = lambda: "down"
        self.assertEqual(search.thumb("https://i.ytimg.com/vi/aaaaaaaaaaa/mqdefault.jpg", "v")[0], 502)
        self.assertEqual(search.thumb("https://i.ytimg.com/vi/bbbbbbbbbbb/mqdefault.jpg", "v")[0], 502)
        self.assertEqual(sum(1 for c in calls if c[0] == "yt"), 2, "YouTube backs off after a network error")
        self.assertEqual(search.thumb("https://i0.hdslb.com/bfs/archive/a.jpg", "v")[0], 200, "Bilibili is not held back")


class HeroThumbTests(unittest.TestCase):
    def test_hero_size_asks_bilibili_for_a_big_uncropped_image(self):
        asked = []

        def bili(url, timeout):
            asked.append(url)
            return "image/webp", b"RIFFwebp"
        search = bilisearch.BiliSearch(fetch=bili)
        self.assertEqual(search.thumb("http://i0.hdslb.com/bfs/bangumi/p.jpg", "h")[0], 200)
        self.assertEqual(asked[-1], "https://i0.hdslb.com/bfs/bangumi/p.jpg@1920w_90q.webp",
                         "width only, no crop: the image host never enlarges, so the page sees the real size")
        self.assertEqual(search.thumb("https://i0.hdslb.com/bfs/bangumi/p.jpg", "x")[0], 404, "unknown sizes refused")


class SpaceTests(unittest.TestCase):
    """GET /api/bili/space: one creator's uploads, paged, for Search → UP 主 (no subscription needed)."""

    class Client:
        def __init__(self):
            self.calls = []
            self.fail = None

        def uploads_page(self, mid, page, size):
            self.calls.append((mid, page, size))
            if self.fail is not None:
                raise self.fail
            from server.relay.state import Video
            n = min(size, 65 - (page - 1) * size)
            return [Video(bvid=f"BV1{page:02d}{i:07d}", title=f"t{page}-{i}", duration=60, pubdate=1000 - i)
                    for i in range(n)], 65

        def cover(self, bvid):
            return f"https://i0.hdslb.com/bfs/archive/{bvid}.jpg"

        def face(self, mid):
            return "https://i0.hdslb.com/bfs/face/f.jpg"

    def test_pages_cache_and_errors(self):
        now = [0.0]
        search = bilisearch.BiliSearch(clock=lambda: now[0])
        client = self.Client()
        first = search.space(client, "546195", "1")
        self.assertTrue(first["ok"])
        self.assertEqual((first["page"], first["pages"], first["total"], len(first["items"])), (1, 3, 65, 30))
        self.assertEqual(first["items"][0]["cover"], "https://i0.hdslb.com/bfs/archive/BV1010000000.jpg")
        self.assertEqual(first["face"], "https://i0.hdslb.com/bfs/face/f.jpg")
        self.assertIs(search.space(client, "546195", 1), first, "cached")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(search.space(client, "546195", 3)["items"]), 5)
        for mid, page in (("abc", 1), ("1" * 21, 1), ("546195", 0), ("546195", bilisearch.SPACE_PAGES + 1), ("", 1)):
            self.assertFalse(search.space(client, mid, page)["ok"], (mid, page))
        client.fail = bilibili.BilibiliError("x", permanent=True)
        self.assertEqual(search.space(client, "7", 1)["error"], bilisearch.REFUSED_SPACE)
        client.fail = bilibili.BilibiliError("timed out")
        self.assertEqual(search.space(client, "8", 1)["error"], bilisearch.UNREACHABLE)
        now[0] = bilisearch.SPACE_CACHE_S + 1
        client.fail = None
        search.space(client, "546195", 1)
        self.assertEqual(client.calls[-1], ("546195", 1, bilisearch.SPACE_SIZE), "fetched again after the cache time")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ShelvesJsTests(unittest.TestCase):
    """The home page's hero size choice, mouse-wheel shelves and creator filter as pure JS functions."""

    def test_pure_functions(self):
        result = subprocess.run(["node", str(Path(__file__).resolve().parent / "console_shelves_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PcProgressTests(unittest.TestCase):
    def test_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pc-progress.json"
            now = [100.0]
            store = home.PcProgress(path, clock=lambda: now[0])
            store.put(1, "up01", {"bvid": "a", "position": 50})
            store.put(2, "up01", {"bvid": "z", "position": 10})
            self.assertEqual(store.get(1, "up01")["bvid"], "a")
            self.assertEqual(store.get(2, "up01")["bvid"], "z")
            self.assertIsNone(store.get(3, "up01"))
            now[0] = 200.0
            store.put(1, "up01", {"bvid": "a", "position": 52})
            self.assertEqual(store.get(1, "up01")["at"], 100.0, "small moves are not rewritten")
            store.put(1, "sr01", {"bvid": "e", "position": 70})
            self.assertEqual([k for k, _ in store.rows(1)], ["sr01", "up01"], "newest first")
            reread = home.PcProgress(path)
            self.assertEqual(reread.get(1, "sr01")["position"], 70)
            for n in range(home.PROGRESS_MAX + 5):
                now[0] += 1
                store.put(1, f"v:bilibili:BV{n}", {"bvid": f"BV{n}", "position": 40})
            self.assertEqual(len(store.rows(1)), home.PROGRESS_MAX)
            store.forget(2, "up01")
            self.assertIsNone(home.PcProgress(path).get(2, "up01"))
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(home.PcProgress(path).rows(1), [], "a broken file is an empty store")


if __name__ == "__main__":
    unittest.main()


class ContinueTitleTests(unittest.TestCase):
    """A drama episode watched on the PC whose title is only its number shows the series name instead."""

    def rows(self, rec):
        import threading
        from server.relay import home

        class Store:
            def rows(self, user):
                return [("v:bilibili:ss41282", rec)]

        class Account:
            lock = threading.Lock()
            creators = []

        relay = types.SimpleNamespace(pc_progress=Store(), clients={})
        return home._continue_rows(relay, web.Caller(user=1, role="user"), Account())

    def test_numbered_episode(self):
        row = self.rows({"title": "1", "name": "Doctor X 第七季", "part": 1, "total": 10, "position": 48, "duration": 3468})[0]
        self.assertEqual((row["title"], row["subtitle"]), ("Doctor X 第七季", "第 1 集"))

    def test_named_episode_keeps_its_title(self):
        row = self.rows({"title": "大门未知子归来", "name": "Doctor X 第七季", "part": 1, "total": 10,
                         "position": 48, "duration": 3468})[0]
        self.assertEqual((row["title"], row["subtitle"]), ("大门未知子归来", "Doctor X 第七季 · 第 1 集"))
