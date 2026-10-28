"""Resolving live channels ahead of a click (server/relay/watch.py prepare, POST /api/watch/prepare).

What is checked: resolve_playlist follows redirects and a master playlist with a
single variant to the playlist ffmpeg would open, returns None for several
variants, a separate audio playlist, non-HLS answers, failures and an address
that did not move, and quotes non-ASCII paths; WatchService.prepare stores what
it found in LiveMemory (so the next open starts ffmpeg there), skips channels
resolved less than PREPARE_FRESH_S ago and non-live sources, resolves only the
route that will be tried first, and caps a request at PREPARE_MAX; the endpoint
only takes live channels of the global table, refuses guests and bad bodies,
and does not exist on the cloud relay. Nothing is logged with an address.

Priorities (perf-08): prepare_order puts the hovered channel first, then the
visible ones, then the rest of the group; a round resolves at most
PREPARE_ROUND channels in that order (the rest is "later" and the page asks
again); PREPARE_SLOW_RUN consecutive channels slower than PREPARE_SLOW_S stop
the round and, for PREPARE_BACKOFF_S, rounds resolve one channel; a stream that
is starting (not ready yet) holds the round, and one still starting after
PREPARE_HOLD_S drops it. The endpoint answers which channels it took (ids).
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import watch

import test_relay_watch as base  # not the TestCase by name: discovery would run it twice
from test_relay_watch import ALICE_DEVICE, INPUT_DUMP, KEY, LIVE_DATA
from test_relay_fastswitch import TABLE_URL, VARIANT, ByInput, live, wait_for

MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\n01.m3u8?sid=abc&token=t\n"
MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\n1.ts\n"


class Answer:
    def __init__(self, final: str, text: str):
        self.final, self.text = final, text

    def geturl(self):
        return self.final

    def read(self, limit):
        return self.text.encode()[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener(final: str, text: str, asked: list | None = None):
    def urlopen(request, timeout):
        if asked is not None:
            asked.append((request.full_url, request.get_header("User-agent")))
        return Answer(final, text)
    return urlopen


class ResolveTests(unittest.TestCase):
    def test_redirect_then_one_variant(self):
        asked = []
        found = watch.resolve_playlist(TABLE_URL, "UA", urlopen=opener("https://cdn.example/live/index.m3u8", MASTER, asked))
        self.assertEqual(found, VARIANT)
        self.assertEqual(asked, [(TABLE_URL, "UA")])

    def test_redirect_to_a_media_playlist(self):
        self.assertEqual(watch.resolve_playlist(TABLE_URL, urlopen=opener(VARIANT, MEDIA)), VARIANT)

    def test_nothing_to_remember(self):
        several = MASTER + "#EXT-X-STREAM-INF:BANDWIDTH=800000\n02.m3u8\n"
        audio = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"\n' + MASTER[8:]
        for final, text in (("https://cdn.example/live/index.m3u8", several),
                            ("https://cdn.example/live/index.m3u8", audio),
                            (TABLE_URL, "not a playlist"),
                            ("https://cdn.example/live/stream.flv", MEDIA),
                            ("https://x/live.m3u8", MEDIA)):
            with self.subTest(text=text[:30], final=final):
                given = "https://x/live.m3u8" if final == "https://x/live.m3u8" else TABLE_URL
                self.assertIsNone(watch.resolve_playlist(given, urlopen=opener(final, text)))

    def test_failures_and_other_schemes(self):
        def broken(request, timeout):
            raise OSError("refused")
        self.assertIsNone(watch.resolve_playlist(TABLE_URL, urlopen=broken))
        self.assertIsNone(watch.resolve_playlist("rtmp://x/live", urlopen=opener(VARIANT, MEDIA)))

    def test_non_ascii_paths_are_quoted_and_ffmpegs_agent_is_the_default(self):
        asked = []
        watch.resolve_playlist("http://x/湖南/live.m3u8", urlopen=opener(VARIANT, MEDIA, asked))
        self.assertEqual(asked[0][0], "http://x/%E6%B9%96%E5%8D%97/live.m3u8")
        self.assertTrue(asked[0][1].startswith("Lavf/"))


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.logs: list[str] = []
        self.service = watch.WatchService("ffmpeg", watch.derive_secret(KEY), log=self.logs.append,
                                          clock=lambda: self.t, popen=ByInput(dict(lines=INPUT_DUMP, data=LIVE_DATA)),
                                          background=False)
        self.service.memory.clock = lambda: self.t
        self.asked: list[tuple[str, str]] = []
        self.service.resolver = lambda url, ua: self.asked.append((url, ua)) or VARIANT

    def tearDown(self):
        self.service.stop_all()

    def test_a_prepared_channel_starts_from_the_resolved_playlist(self):
        self.assertEqual(self.service.prepare([live()]), {"started": 1, "fresh": 0, "busy": 0, "skipped": 0, "later": 0,
                                                          "ids": ["ch000"]})
        self.assertEqual(self.service.memory.url(live().key), VARIANT)
        self.assertTrue(self.service.open(ALICE_DEVICE, live()).wait_ready(5))
        self.assertEqual(self.service.popen.inputs(), [VARIANT])
        self.assertFalse(any("token" in m or "cdn.example" in m for m in self.logs), "never logged")

    def test_fresh_channels_are_not_resolved_again(self):
        self.service.prepare([live()])
        self.assertEqual(self.service.prepare([live()])["fresh"], 1)
        self.t += watch.PREPARE_FRESH_S + 1
        self.assertEqual(self.service.prepare([live()])["started"], 1)
        self.assertEqual(len(self.asked), 2)

    def test_nothing_found_leaves_the_table_address(self):
        self.service.resolver = lambda url, ua: None
        self.service.prepare([live()])
        self.assertIsNone(self.service.memory.url(live().key))
        self.assertEqual(self.service.prepare([live()])["started"], 1, "tried again next time")

    def test_videos_and_piped_sources_are_skipped(self):
        video = watch.Source(("video", "BV1"), url="https://x/v.mp4", label="v")
        piped = watch.Source(("live", "ch009", "", ""), opener=lambda: None, label="p")
        self.assertEqual(self.service.prepare([video, piped])["skipped"], 2)
        self.assertEqual(self.asked, [])

    def test_only_the_route_tried_first(self):
        routes = (("https://a/1", "UA"), ("https://b/2", "UB"))
        source = watch.Source(watch.live_key("ch005", list(routes)), url="https://a/1", user_agent="UA",
                              paced=False, routes=routes, label="ch005")
        self.service.routes._good["ch005"] = watch.live_routes.fingerprint("https://b/2")
        self.service.prepare([source])
        self.assertEqual(self.asked, [("https://b/2", "UB")])
        self.assertEqual(self.service.memory.url(("live", "ch005", "https://b/2", "UB")), VARIANT)

    def test_a_round_takes_the_first_prepare_round_in_order(self):
        sources = [live(f"ch{i:03d}", f"https://x/{i}") for i in range(watch.PREPARE_MAX + 5)]
        answer = self.service.prepare(sources)
        self.assertEqual((answer["started"], answer["later"]),
                         (watch.PREPARE_ROUND, watch.PREPARE_MAX - watch.PREPARE_ROUND), "at most PREPARE_MAX looked at")
        self.assertEqual([url for url, _ in self.asked], [f"https://x/{i}" for i in range(watch.PREPARE_ROUND)])
        self.assertEqual(answer["ids"], [f"ch{i:03d}" for i in range(watch.PREPARE_ROUND)])
        # The next round goes on where this one stopped; the resolved ones count as fresh.
        again = self.service.prepare(sources)
        self.assertEqual((again["fresh"], again["started"]), (watch.PREPARE_ROUND, watch.PREPARE_ROUND))
        self.assertEqual(self.asked[watch.PREPARE_ROUND][0], f"https://x/{watch.PREPARE_ROUND}")

    def test_order_hover_then_visible_then_the_rest(self):
        group = ["ch000", "ch001", "ch002", "ch003", "ch004"]
        self.assertEqual(watch.prepare_order(group, ["ch003", "ch001"], "ch004"),
                         ["ch004", "ch003", "ch001", "ch000", "ch002"])
        self.assertEqual(watch.prepare_order(group, ["ch009"], "ch008"), group, "only channels of the request")
        self.assertEqual(watch.prepare_order(group, ["ch002", "ch002"], "ch002")[:2], ["ch002", "ch000"])
        self.assertEqual(watch.prepare_order([]), [])

    def test_slow_sources_stop_the_round_and_later_rounds_take_one(self):
        def slow(url, ua):
            self.asked.append((url, ua))
            self.t += watch.PREPARE_SLOW_S + 1
            return VARIANT
        self.service.resolver = slow
        sources = [live(f"ch{i:03d}", f"https://x/{i}") for i in range(watch.PREPARE_ROUND)]
        self.assertEqual(self.service.prepare(sources)["started"], watch.PREPARE_ROUND)
        self.assertEqual(len(self.asked), watch.PREPARE_SLOW_RUN, "stopped after the slow run")
        self.assertTrue(any("先不解析" in m for m in self.logs))
        self.assertFalse(self.service._preparing, "the dropped ones can be asked for again")
        # Backing off: one per round, the first in order, and the answer says so.
        self.service.resolver = lambda url, ua: self.asked.append((url, ua)) or VARIANT
        answer = self.service.prepare(sources)
        self.assertEqual((answer["started"], answer.get("slow")), (1, True))
        self.assertEqual(self.asked[-1][0], f"https://x/{watch.PREPARE_SLOW_RUN}")
        self.t += watch.PREPARE_BACKOFF_S
        answer = self.service.prepare(sources)
        self.assertEqual(answer["started"], watch.PREPARE_ROUND - watch.PREPARE_SLOW_RUN - 1)
        self.assertNotIn("slow", answer)

    def test_a_fast_source_breaks_the_slow_run(self):
        pattern = iter([True, True, False, True, True, False, True, True])

        def mixed(url, ua):
            self.asked.append((url, ua))
            if next(pattern, False):
                self.t += watch.PREPARE_SLOW_S + 1
            return VARIANT
        self.service.resolver = mixed
        sources = [live(f"ch{i:03d}", f"https://x/{i}") for i in range(8)]
        self.service.prepare(sources)
        self.assertEqual(len(self.asked), 8, "never three slow ones in a row")

    def test_a_starting_stream_holds_the_round(self):
        starting = [True, True, True]
        slept = []
        self.service.starting = lambda: bool(starting) and starting.pop()
        self.service.sleep = slept.append
        self.service.prepare([live()])
        self.assertEqual((len(slept), len(self.asked)), (3, 1), "resolved once the stream had started")
        # A stream that keeps starting: after PREPARE_HOLD_S the round is dropped.
        self.service.starting = lambda: True
        slept.clear()
        self.service.prepare([live("ch001", "https://x/1")])
        self.assertEqual(len(self.asked), 1)
        self.assertAlmostEqual(sum(1 for _ in slept) * 0.2, watch.PREPARE_HOLD_S, delta=0.5)
        self.assertFalse(self.service._preparing)

    def test_starting_means_a_viewer_stream_without_its_first_segment(self):
        class Stream:
            def __init__(self, ready, created, alive=True):
                self._ready, self.created, self.alive = ready, created, alive

            def ready(self):
                return self._ready
        self.assertFalse(self.service.starting())
        self.service._by_device["pc1-x"] = Stream(False, self.t)
        self.assertTrue(self.service.starting())
        self.service._by_device["pc1-x"] = Stream(True, self.t)
        self.assertFalse(self.service.starting(), "already playing")
        self.service._by_device["pc1-x"] = Stream(False, self.t - watch.PREPARE_START_S - 1)
        self.assertFalse(self.service.starting(), "stuck for long: not worth holding back")
        self.service._by_device["pc1-x"] = Stream(False, self.t, alive=False)
        self.assertFalse(self.service.starting())
        self.service._by_device.clear()


class PrepareEndpointTests(unittest.TestCase):
    selfhost = True
    setUp_relay = base.RelayWatchTests.setUp
    tearDown = base.RelayWatchTests.tearDown
    online = base.RelayWatchTests.online
    request = base.RelayWatchTests.request

    def setUp(self):
        self.setUp_relay()
        self.asked: list[str] = []
        self.relay.watch.resolver = lambda url, ua: self.asked.append(url) or VARIANT

    def prepare(self, who="alice", **body):
        status, _, raw = self.request("POST", "/tv/api/watch/prepare", who, body)
        return status, (json.loads(raw) if status == 200 else None)

    def test_live_channels_of_the_table(self):
        status, answer = self.prepare(channels=["ch000", "ch001", "up01", "cast", "ch404"])
        self.assertEqual(status, 200)
        self.assertEqual(answer, {"ok": True, "started": 2, "fresh": 0, "busy": 0, "skipped": 0, "later": 0,
                                  "ids": ["ch000", "ch001"]})
        self.assertTrue(wait_for(lambda: len(self.asked) == 2))
        self.assertTrue(wait_for(lambda: self.prepare(channels=["ch000"])[1]["fresh"] == 1))

    def test_the_hovered_and_visible_channels_go_first(self):
        status, answer = self.prepare(channels=["ch000", "ch001"], visible=["ch001"])
        self.assertEqual(answer["ids"], ["ch001", "ch000"])
        self.assertTrue(wait_for(lambda: len(self.asked) == 2))
        self.relay.watch.memory = watch.LiveMemory()
        self.assertEqual(self.prepare(channels=["ch000", "ch001"], visible=["ch000"], hover="ch001")[1]["ids"],
                         ["ch001", "ch000"])

    def test_bad_requests_and_guests(self):
        self.assertFalse(self.prepare(channels="ch001")[1]["ok"])
        self.assertFalse(self.prepare(channels=[f"ch{i:03d}" for i in range(watch.PREPARE_MAX + 1)])[1]["ok"])
        self.assertFalse(self.prepare(who="guest", channels=["ch001"])[1]["ok"])
        self.assertFalse(self.prepare(channels=["ch001"], visible="ch001")[1]["ok"])
        self.assertFalse(self.prepare(channels=["ch001"], hover=["ch001"])[1]["ok"])
        self.assertFalse(self.prepare(channels=["ch001"], visible=[f"ch{i:03d}" for i in range(watch.PREPARE_MAX + 1)])[1]["ok"])


class CloudPrepareTests(unittest.TestCase):
    selfhost = False
    setUp = base.RelayWatchTests.setUp
    tearDown = base.RelayWatchTests.tearDown
    online = base.RelayWatchTests.online
    request = base.RelayWatchTests.request

    def test_no_prepare_endpoint_on_the_cloud_relay(self):
        status, _, _ = self.request("POST", "/tv/api/watch/prepare", "alice", {"channels": ["ch001"]})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
