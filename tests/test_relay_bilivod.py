"""Faster Bilibili starts and seeks on the PC (server/relay/bilivod.py, bilidash.BiliDash.opener, ytvod.Fetcher spare).

Before, opening a Bilibili video on the PC took three sequential round trips (view + playurl, a 64-byte moov probe
for long videos, then the media) and every seek opened a new TLS connection upstream.

What is checked:

- bilivod.Plays: a prefetched answer is kept until the address's deadline minus EXPIRE_MARGIN_S (NO_DEADLINE_S without
  one, never past MAX_AGE_S), a play uses it (and says how old it is), a play that meets a prefetch of the same video
  waits for it, a play never stores what it fetched, forget drops it; moov sizes are remembered (not "unknown").
- ytvod.Fetcher with spare: when a host's pool runs dry a spare connection is opened in the background, so the request
  after an abandoned one (a seek) does not connect on its own path; an idle connection whose peer hung up or sent
  bytes is not reused.
- Through the relay: POST /api/bili/prefetch queues BV ids and the next play uses the prefetched address without asking
  Bilibili again (view included for a cast); guests and bad ids are refused, the cloud relay has no endpoint; the moov
  probe shares its connection with the media; a long video's second open skips the probe (and the MP4 lookup when it
  plays as DASH); an expired address is fetched again, not taken from the cache.

Bilibili is never contacted: the upstream is a local HTTP server and the client is a fake.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import bilidash, bilivod, vod, ytvod

import test_relay_bilidash as tb
import test_relay_vod as tv
import test_relay_watch as tw
from test_relay_fastswitch import wait_for
from test_relay_web import FakeClient

DEADLINE = 2_000_000_000


class PlaysTests(unittest.TestCase):
    def setUp(self):
        self.t = float(DEADLINE - 7200)
        self.plays = bilivod.Plays(clock=lambda: self.t, log=lambda _m: None)
        self.key = ("mp4", "BV1xx411c7mD", None)

    def test_kept_until_the_deadline_minus_a_margin(self):
        info = {"url": f"https://upos.example/x.mp4?deadline={DEADLINE}&os=x"}
        self.assertEqual(bilivod.deadline_of(info), DEADLINE)
        self.assertEqual(bilivod.deadline_of({"video": {"url": f"https://x/v.m4s?e=1&deadline={DEADLINE}"}}), DEADLINE)
        self.assertEqual(bilivod.keep_until(info, self.t), DEADLINE - bilivod.EXPIRE_MARGIN_S)
        self.assertEqual(bilivod.keep_until({"url": "https://x/y.mp4"}, self.t), self.t + bilivod.NO_DEADLINE_S)
        far = {"url": f"https://x/y.mp4?deadline={DEADLINE + 86400}"}
        self.assertEqual(bilivod.keep_until(far, self.t), self.t + bilivod.MAX_AGE_S)
        self.assertTrue(self.plays.prefetch(self.key, lambda: info))
        self.assertFalse(self.plays.prefetch(self.key, lambda: self.fail("not asked again")))
        self.t += 600
        self.assertEqual(self.plays.cached(self.key), (info, 600.0))
        self.t = DEADLINE - bilivod.EXPIRE_MARGIN_S
        self.assertIsNone(self.plays.cached(self.key), "too close to the deadline")

    def test_a_play_uses_the_prefetch_and_never_stores_its_own(self):
        asked = []
        self.assertEqual(self.plays.get(self.key, lambda: asked.append(1) or {"url": "own"}), ({"url": "own"}, 0.0))
        self.assertIsNone(self.plays.cached(self.key), "plays do not fill the cache")
        self.plays.prefetch(self.key, lambda: {"url": "pre"})
        self.assertEqual(self.plays.get(self.key, lambda: self.fail("cached"))[0], {"url": "pre"})
        self.assertEqual((len(asked), self.plays.hits), (1, 1))
        self.plays.forget(self.key)
        self.assertIsNone(self.plays.cached(self.key))
        self.assertFalse(self.plays.prefetch(self.key, lambda: {}), "nothing to keep")

    def test_a_play_waits_for_the_same_prefetch(self):
        gate, started = threading.Event(), threading.Event()

        def slow():
            started.set()
            gate.wait(5)
            return {"url": "pre"}
        worker = threading.Thread(target=self.plays.prefetch, args=(self.key, slow))
        worker.start()
        self.assertTrue(started.wait(5))
        threading.Timer(0.1, gate.set).start()
        self.assertEqual(self.plays.get(self.key, lambda: self.fail("waited instead"))[0], {"url": "pre"})
        worker.join(5)
        self.assertFalse(self.plays.busy())

    def test_moov_memory(self):
        self.assertIsNone(self.plays.moov("BV1"))
        self.plays.remember_moov("BV1", 0)
        self.assertIsNone(self.plays.moov("BV1"), "unknown is not remembered")
        self.plays.remember_moov("BV1", 43_782_210)
        self.assertEqual(self.plays.moov("BV1"), 43_782_210)
        self.t += bilivod.PATH_KEEP_S + 1
        self.assertIsNone(self.plays.moov("BV1"))

    def test_ids(self):
        for good in ("BV1xx411c7mD", "BV1xx411c7mD/123", "ep2/12"):
            self.assertTrue(bilivod.ID.match(good), good)
        for bad in ("BV1", "ss1", "ep2", "https://x", "BV1xx411c7mD/x", ""):
            self.assertFalse(bilivod.ID.match(bad), bad)
        self.assertIsNone(bilivod.who(None))


class SpareTests(unittest.TestCase):
    def setUp(self):
        self.upstream = tv.Upstream()
        self.fetcher = ytvod.Fetcher("", spare=True)

    def tearDown(self):
        self.fetcher.close()
        self.upstream.close()

    def open(self, wanted):
        return self.fetcher(urllib.request.Request(self.upstream.base + "/big.mp4", headers={"Range": wanted}), 10)

    def test_a_seek_takes_the_spare_connection(self):
        first = self.open("bytes=0-")
        first.read1(65536)
        self.assertTrue(wait_for(lambda: self.fetcher.spares == 1), "a spare while the first request streams")
        first.close()                               # the browser seeks: the half-read connection is dropped
        second = self.open("bytes=4000000-")
        self.assertEqual(second.read1(4)[:4], tv.BIG[4000000:4000004])
        self.assertTrue(wait_for(lambda: self.fetcher.spares == 2), "and the next spare is on its way")
        self.assertEqual(self.fetcher.connects - self.fetcher.spares, 1,
                         "the seek used the spare: only the first request connected on its own path")
        second.close()

    def test_a_closed_fetcher_keeps_no_spares(self):
        self.fetcher.close()
        answer = self.open("bytes=0-9")
        answer.read()
        answer.close()
        time.sleep(0.2)
        self.assertEqual(self.fetcher._idle, {})

    def test_dead_idle_connections_are_not_reused(self):
        ours, theirs = socket.socketpair()
        try:
            self.assertTrue(ytvod._usable(ours))
            theirs.sendall(b"x")
            self.assertFalse(ytvod._usable(ours), "unexpected bytes")
        finally:
            ours.close()
            theirs.close()
        ours, theirs = socket.socketpair()
        theirs.close()
        try:
            self.assertFalse(ytvod._usable(ours), "the other side hung up")
        finally:
            ours.close()
        self.assertFalse(ytvod._usable(None))


BV = "BV1xx411c7mD"


class RelayPrefetchTests(unittest.TestCase):
    selfhost = True
    setUp_relay = tw.RelayWatchTests.setUp
    tearDown_relay = tw.RelayWatchTests.tearDown
    online = tw.RelayWatchTests.online
    request = tw.RelayWatchTests.request
    watch = tv.RelayVodTests.watch
    media = tv.RelayVodTests.media

    def setUp(self):
        tv.RelayVodTests.setUp(self)
        self.upstream.files.update({"/long.mp4": tb.LONG_MP4, "/v.m4s": tb.VIDEO, "/a.m4s": tb.AUDIO})
        self.client = tb.DashClient(self.upstream.base)
        self.views: list[str] = []
        real_video = self.client.video
        self.client.video = lambda bvid: self.views.append(bvid) or real_video(bvid)
        self.relay.clients["bilibili"] = self.client
        self.relay.bilidash = bilidash.BiliDash(log=lambda _m: None, proxy=lambda: "")
        self.logs: list[str] = []
        self.relay.log = self.logs.append

    def tearDown(self):
        tv.RelayVodTests.tearDown(self)
        self.relay.bilidash.fetcher().close()

    def prefetch(self, who="carol", **body):
        status, _, raw = self.request("POST", "/tv/api/bili/prefetch", who, body)
        return status, (json.loads(raw) if status == 200 else None)

    def test_a_prefetched_address_starts_the_play(self):
        status, answer = self.prefetch(ids=[BV])
        self.assertEqual((status, answer), (200, {"ok": True, "queued": 1}))
        self.assertTrue(self.relay.bili_prefetcher.wait_idle(5))
        self.assertEqual(self.client.calls, [(BV, None)])
        self.assertEqual(self.views, [BV], "the video's info too")
        self.assertEqual(self.upstream.requests, [], "never a byte of media")
        answer = self.watch(url=f"https://www.bilibili.com/video/{BV}", vod=True)[1]
        self.assertEqual(answer["mode"], "vod")
        self.assertEqual(self.client.calls, [(BV, None)], "no second playurl")
        self.assertEqual(self.relay.biliplays.hits, 1)
        code, _, data = self.media(answer["url"], "bytes=0-99")
        self.assertEqual((code, data), (206, tv.CLIP[:100]))
        self.assertFalse(any("bvid=" in m or self.upstream.base in m for m in self.logs), "no addresses logged")

    def test_an_expired_address_is_fetched_again(self):
        self.prefetch(ids=[BV])
        self.assertTrue(self.relay.bili_prefetcher.wait_idle(5))
        answer = self.watch(url=f"https://www.bilibili.com/video/{BV}", vod=True)[1]
        self.upstream.forbid = 1
        code, _, _ = self.media(answer["url"], "bytes=0-99")
        self.assertEqual(code, 206)
        self.assertEqual(len(self.client.calls), 2, "resolved again after the 403")
        self.assertIsNone(self.relay.biliplays.cached(("mp4", BV, None)), "and the cached one is gone")

    def test_bad_requests_guests_and_signed_in_keys(self):
        self.assertFalse(self.prefetch(ids="BV1xx411c7mD")[1]["ok"])
        self.assertFalse(self.prefetch(ids=["https://www.bilibili.com/video/BV1xx411c7mD"])[1]["ok"])
        self.assertFalse(self.prefetch(ids=[BV] * (bilivod.PREFETCH_IDS_MAX + 1))[1]["ok"])
        self.assertFalse(self.prefetch(ids=[])[1]["ok"])
        self.assertFalse(self.prefetch(who="guest", ids=[BV])[1]["ok"])
        # A signed-in viewer's prefetch is kept under his login, and only his play uses it.
        login = tv.bililogin.Login(self.carol, {"SESSDATA": "s"}, vip=False)
        self.logins[self.carol] = login
        self.prefetch(ids=[BV])
        self.assertTrue(self.relay.bili_prefetcher.wait_idle(5))
        self.assertEqual(self.client.calls, [(BV, login)])
        self.assertIsNotNone(self.relay.biliplays.cached(("mp4", BV, (self.carol, False))))
        self.assertIsNone(self.relay.biliplays.cached(("mp4", BV, None)))

    def test_off_without_vod_media(self):
        self.relay.clients["bilibili"] = FakeClient()
        self.assertEqual(self.prefetch(ids=[BV])[1], {"ok": True, "off": True, "queued": 0})

    def test_the_probe_shares_its_connection_with_the_media(self):
        with mock.patch.object(vod, "MOOV_CHECK_S", 100):
            answer = self.watch(channel="up01", vod=True, dash=True, episode=1)[1]
        self.assertEqual(answer["mode"], "vod")
        fetcher = self.relay.bilidash.fetcher()
        self.assertTrue(wait_for(lambda: fetcher.spares >= 1))
        code, _, data = self.media(answer["url"], "bytes=0-99")
        self.assertEqual((code, data), (206, tv.CLIP[:100]))
        self.assertEqual([r["range"] for r in self.upstream.requests], ["bytes=0-63", "bytes=0-99"])
        self.assertEqual(fetcher.connects - fetcher.spares, 1, "one connect on the request path for probe + media")

    def test_the_second_open_skips_the_probe(self):
        self.client.paths.update({"a": "/long.mp4", "b": "/clip.mp4"})
        with mock.patch.object(vod, "MOOV_CHECK_S", 100):
            self.assertEqual(self.watch(channel="up01", vod=True, dash=True, episode=1)[1]["mode"], "dash")
            self.assertEqual(self.relay.biliplays.moov("a"), 43_782_210)
            self.assertEqual(self.watch(channel="up01", vod=True, dash=True, episode=2)[1]["mode"], "vod")
            probes = lambda: [r for r in self.upstream.requests if r.get("range") == "bytes=0-63"]  # noqa: E731
            self.assertEqual(len(probes()), 2)
            calls = len(self.client.calls)
            self.assertEqual(self.watch(channel="up01", vod=True, dash=True, episode=1)[1]["mode"], "dash")
            self.assertEqual(len(self.client.calls), calls, "a remembered DASH video needs no MP4 lookup")
            self.assertEqual(len(self.client.dash_calls), 2)
            self.assertEqual(self.watch(channel="up01", vod=True, dash=True, episode=2)[1]["mode"], "vod")
            self.assertEqual(len(probes()), 2, "no probe the second time")
            # DASH gone: the remembered video still plays as MP4.
            self.client.dash_down = True
            self.assertEqual(self.watch(channel="up01", vod=True, dash=True, episode=1)[1]["mode"], "vod")
            self.assertEqual(len(probes()), 2)


class CloudPrefetchTests(unittest.TestCase):
    selfhost = False
    setUp = tw.RelayWatchTests.setUp
    tearDown = tw.RelayWatchTests.tearDown
    online = tw.RelayWatchTests.online
    request = tw.RelayWatchTests.request

    def test_no_prefetch_endpoint_on_the_cloud_relay(self):
        status, _, _ = self.request("POST", "/tv/api/bili/prefetch", "alice", {"ids": [BV]})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
