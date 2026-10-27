"""Long Bilibili videos on the PC play as DASH (server/relay/bilidash.py), not the html5 MP4.

The html5 MP4 keeps its whole index (moov) at the front: a 9 h 45 min part has a 43.8 MB moov the browser must
download before the first frame (about 45 s through a 1 MB/s proxy), so the player sat on "正在打开…".

What is checked:

- bilibili.dash_info picks the highest H.264 stream up to 720P and the best AAC stream, and gives nothing when there is
  no H.264 (HEVC / AV1 only), no DASH or only a preview; Bilibili.vod_dash asks playurl for DASH (fnval=16) for a part
  ("BV…/cid") and for a bangumi episode.
- vod.moov_size reads the moov size from the first bytes (moov first, or the space after mdat when moov is at the end);
  vod.client_gone notices a browser that hung up.
- Through the relay: a video at least MOOV_CHECK_S long whose MP4 has a moov over MOOV_MAX plays as "dash" with a
  manifest built from the two tracks' sidx; a short video, a big moov on an old console (no dash flag), and a failing
  DASH lookup keep the MP4 ("vod").
- bilidash.system_proxy follows the environment like urllib (and NO_PROXY).

Bilibili is never contacted: the upstream is a local HTTP server.
"""

from __future__ import annotations

import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import bilibili, bilidash, vod
from server.relay.bilibili import BilibiliError

import test_relay_vod as tv
import test_relay_watch as tw
from test_relay_ytvod import box, dash_file


def dash_item(qn, codecs, url, height=480, bandwidth=100_000, init="0-947", index="948-85287"):
    return {"id": qn, "codecs": codecs, "base_url": url, "backup_url": [url + "&b=1"], "height": height,
            "bandwidth": bandwidth, "SegmentBase": {"Initialization": init, "indexRange": index}}


def dash_play(videos, audios, duration=35105, preview=False):
    return {"quality": 32, "timelength": duration * 1000, "is_preview": 1 if preview else 0,
            "dash": {"duration": duration, "video": videos, "audio": audios}}


class DashInfoTests(unittest.TestCase):
    def test_picks_h264_up_to_720p_and_the_best_aac(self):
        play = dash_play(
            [dash_item(80, "avc1.640032", "https://x/1080", 1080), dash_item(64, "avc1.640028", "https://x/720", 720),
             dash_item(64, "hev1.1.6.L120.90", "https://x/720h", 720), dash_item(32, "avc1.64001F", "https://x/480")],
            [dash_item(30216, "mp4a.40.2", "https://x/a64", bandwidth=64_000),
             dash_item(30280, "mp4a.40.2", "https://x/a192", bandwidth=192_000)])
        info = bilibili.dash_info(play)
        self.assertEqual((info["video"]["url"], info["video"]["height"], info["audio"]["url"]),
                         ("https://x/720", 720, "https://x/a192"))
        self.assertEqual((info["video"]["init"], info["video"]["index"]), ((0, 947), (948, 85287)))
        self.assertEqual(info["duration"], 35105.0)
        self.assertEqual(info["video"]["backups"], ("https://x/720&b=1",))
        self.assertEqual(bilibili.dash_info(play, max_quality=32)["video"]["url"], "https://x/480")

    def test_nothing_the_browser_can_play(self):
        aac = [dash_item(30280, "mp4a.40.2", "https://x/a")]
        self.assertEqual(bilibili.dash_info(dash_play([dash_item(32, "hev1.1.6.L120.90", "https://x/h")], aac)), {})
        self.assertEqual(bilibili.dash_info(dash_play([dash_item(32, "avc1.64001F", "https://x/v")], [])), {})
        self.assertEqual(bilibili.dash_info(dash_play([dash_item(32, "avc1.64001F", "https://x/v")], aac,
                                                      preview=True)), {})
        self.assertEqual(bilibili.dash_info({"durl": [{"url": "https://x/mp4"}]}), {}, "no DASH in the answer")
        broken = dash_item(32, "avc1.64001F", "https://x/v", index="oops")
        self.assertEqual(bilibili.dash_info(dash_play([broken], aac)), {}, "no usable SegmentBase")

    def test_vod_dash_asks_playurl_for_dash(self):
        calls = []
        answer = dash_play([dash_item(32, "avc1.64001F", "https://x/v")], [dash_item(30280, "mp4a.40.2", "https://x/a")])

        def fetch(url, cookie, timeout=None):
            calls.append(url)
            if "finger/spi" in url:
                return {"code": 0, "data": {"b_3": "b"}}
            return {"code": 0, "data": answer}

        client = bilibili.Bilibili(fetch_json=fetch)
        self.assertEqual(client.vod_dash("BV1xx411c7mD/9")["video"]["url"], "https://x/v")
        self.assertIn("/x/player/playurl?", calls[-1])
        self.assertIn("cid=9", calls[-1])
        self.assertIn("fnval=16", calls[-1])
        self.assertNotIn("platform=html5", calls[-1])
        client.vod_dash("ep2/12")
        self.assertIn("/pgc/player/web/playurl?", calls[-1])
        self.assertIn("ep_id=2", calls[-1])


class MoovTests(unittest.TestCase):
    FTYP = box(b"ftyp", b"isom\0\0\0\0isomavc1")

    def test_moov_first_or_last(self):
        head = self.FTYP + (43_782_210).to_bytes(4, "big") + b"moov"
        self.assertEqual(vod.moov_size(head[:64]), 43_782_210)
        head = self.FTYP + (900_000_000).to_bytes(4, "big") + b"mdat"
        self.assertEqual(vod.moov_size(head, 950_000_000), 950_000_000 - 900_000_000 - len(self.FTYP))
        self.assertEqual(vod.moov_size(head), 0, "moov at the end of a file of unknown size")
        large = self.FTYP + (1).to_bytes(4, "big") + b"moov" + (9_000_000_000).to_bytes(8, "big")
        self.assertEqual(vod.moov_size(large), 9_000_000_000, "64-bit box size")

    def test_garbage_is_unknown(self):
        self.assertEqual(vod.moov_size(b""), 0)
        self.assertEqual(vod.moov_size(b"\0" * 64), 0)
        self.assertEqual(vod.moov_size(self.FTYP), 0, "no box after ftyp in the bytes read")

    def test_client_gone(self):
        ours, theirs = socket.socketpair()
        try:
            self.assertFalse(vod.client_gone(ours), "connected, nothing to read")
            theirs.close()
            self.assertTrue(vod.client_gone(ours), "the other side hung up")
        finally:
            ours.close()
        self.assertFalse(vod.client_gone(None))
        self.assertFalse(vod.client_gone(object()), "a stand-in without a socket")


class ProxyTests(unittest.TestCase):
    def test_follows_the_environment(self):
        self.assertEqual(bilidash.system_proxy(getproxies=lambda: {"https": "http://127.0.0.1:7897"},
                                               bypass=lambda host: False), "http://127.0.0.1:7897")
        self.assertEqual(bilidash.system_proxy(getproxies=lambda: {"https": "http://127.0.0.1:7897"},
                                               bypass=lambda host: True), "", "NO_PROXY")
        self.assertEqual(bilidash.system_proxy(getproxies=lambda: {}, bypass=lambda host: False), "")
        self.assertEqual(bilidash.system_proxy(getproxies=lambda: {"https": "ftp://x"}, bypass=lambda host: False), "",
                         "not a proxy the fetcher speaks")


VIDEO, VIDEO_INIT_END, VIDEO_SIDX, VIDEO_OFFSETS = dash_file([3000, 2500, 2800])
AUDIO, AUDIO_INIT_END, AUDIO_SIDX, AUDIO_OFFSETS = dash_file([800, 900, 700])
LONG_MP4 = MoovTests.FTYP + (43_782_210).to_bytes(4, "big") + b"moov" + b"\0" * 4096


class DashClient(tv.FakeVodClient):
    """FakeVodClient whose html5 MP4 has a huge moov, with Bilibili.vod_dash pointing at local DASH files."""

    def __init__(self, base):
        super().__init__(base)
        self.dash_calls: list = []
        self.dash_down = False

    def vod_dash(self, bvid, login=None):
        self.dash_calls.append((bvid, login))
        if self.dash_down:
            raise BilibiliError("offline")
        index_v = f"{VIDEO_SIDX}-{VIDEO_OFFSETS[0] - 1}"
        index_a = f"{AUDIO_SIDX}-{AUDIO_OFFSETS[0] - 1}"
        return bilibili.dash_info(dash_play(
            [dash_item(32, "avc1.64001F", f"{self.base}/v.m4s?bvid={bvid}", init=f"0-{VIDEO_INIT_END}", index=index_v)],
            [dash_item(30280, "mp4a.40.2", f"{self.base}/a.m4s?bvid={bvid}", init=f"0-{AUDIO_INIT_END}",
                       index=index_a)], duration=15))


class RelayLongVideoTests(unittest.TestCase):
    selfhost = True
    setUp_relay = tw.RelayWatchTests.setUp
    tearDown_relay = tw.RelayWatchTests.tearDown
    online = tw.RelayWatchTests.online
    request = tw.RelayWatchTests.request
    watch = tv.RelayVodTests.watch
    media = tv.RelayVodTests.media

    def setUp(self):
        tv.RelayVodTests.setUp(self)
        self.upstream.files.update({"/long.mp4": LONG_MP4, "/v.m4s": VIDEO, "/a.m4s": AUDIO})
        self.client = DashClient(self.upstream.base)
        self.client.paths.update({"a": "/long.mp4", "b": "/long.mp4"})
        self.relay.clients["bilibili"] = self.client
        self.relay.bilidash = bilidash.BiliDash(log=lambda _m: None, proxy=lambda: "")
        patcher = mock.patch.object(vod, "MOOV_CHECK_S", 100)     # the creator's videos are 600 s long
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        tv.RelayVodTests.tearDown(self)
        self.relay.bilidash.fetcher().close()

    def test_a_huge_moov_plays_as_dash(self):
        status, answer = self.watch(channel="up01", vod=True, dash=True, episode=1)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["mode"], answer["kind"], answer["seekable"]), ("dash", "video", True))
        self.assertRegex(answer["url"], r"^vod/[0-9a-f]{16}/\d+\.[A-Za-z0-9_-]+/$")
        self.assertEqual(self.client.dash_calls, [("a", None)])
        probes = [r for r in self.upstream.requests if r["path"].startswith("/long.mp4")]
        self.assertEqual([r["range"] for r in probes], ["bytes=0-63"], "one tiny look at the MP4's head")
        code, _, raw = self.media(answer["url"] + "manifest")
        self.assertEqual(code, 200, raw)
        manifest = json.loads(raw)
        self.assertEqual(manifest["tracks"]["v"]["o"], VIDEO_OFFSETS)
        self.assertEqual(manifest["tracks"]["a"]["o"], AUDIO_OFFSETS)
        self.assertEqual(manifest["tracks"]["v"]["mime"], 'video/mp4; codecs="avc1.64001F"')
        self.assertEqual(manifest["duration"], 15.0)
        self.assertIn("ib", manifest["tracks"]["v"])
        code, headers, data = self.media(answer["url"] + "v", f"bytes={VIDEO_OFFSETS[1]}-{VIDEO_OFFSETS[2] - 1}")
        self.assertEqual((code, data), (206, VIDEO[VIDEO_OFFSETS[1]:VIDEO_OFFSETS[2]]))
        self.assertEqual(self.upstream.requests[-1]["referer"], bilibili.REFERER)
        self.assertEqual(self.media(answer["url"] + "media")[0], 404, "no MP4 in a DASH stream")
        # Asking for the same part again keeps the stream; falling back (no dash) is another stream with the MP4.
        again = self.watch(channel="up01", vod=True, dash=True, episode=1, start=20)[1]
        self.assertEqual((again["sid"], again["mode"]), (answer["sid"], "dash"))
        plain = self.watch(channel="up01", vod=True, episode=1, start=20)[1]
        self.assertEqual(plain["mode"], "vod")
        self.assertNotEqual(plain["sid"], answer["sid"])

    def test_what_keeps_the_mp4(self):
        # An old console that cannot play DASH: the MP4, and no probe.
        self.assertEqual(self.watch(channel="up01", vod=True, episode=1)[1]["mode"], "vod")
        self.assertFalse([r for r in self.upstream.requests if r.get("range") == "bytes=0-63"])
        # A small moov.
        self.client.paths["a"] = "/clip.mp4"
        self.assertEqual(self.watch(channel="up01", vod=True, dash=True, episode=1)[1]["mode"], "vod")
        self.assertEqual(self.client.dash_calls, [])
        # A short video is not even probed.
        self.client.paths["a"] = "/long.mp4"
        with mock.patch.object(vod, "MOOV_CHECK_S", 10_000):
            self.assertEqual(self.watch(channel="up01", vod=True, dash=True, episode=1)[1]["mode"], "vod")
        self.assertEqual(self.client.dash_calls, [])
        # DASH lookup fails: the MP4 still plays.
        self.client.dash_down = True
        self.assertEqual(self.watch(channel="up01", vod=True, dash=True, episode=2)[1]["mode"], "vod")
        self.assertEqual(len(self.client.dash_calls), 1)


if __name__ == "__main__":
    unittest.main()
