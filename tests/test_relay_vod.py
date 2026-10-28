"""On-demand playback on the PC (server/relay/vod.py): Bilibili videos play the original file natively.

What is checked:

- Mode selection: a single-segment H.264 MP4 from Bilibili's html5 playurl plays as
  VOD; several durl segments, FLV, HEVC, a preview, a failing lookup, YouTube, live
  channels, an old console (no `vod: true`) and a signed-in viewer who picked 1080P
  stay on the ffmpeg/HLS path (watch.py). A signed-in viewer gets a 720P/1080P
  choice in the answer.
- Resolution sharing: creator, series, cast and cast-history items pick their part and
  start position exactly as the HLS path does (same planners), only the source differs.
- The range proxy: Range passes through to the upstream with the Referer and
  User-Agent the source needs; 206, Content-Range, Content-Length, Content-Type and
  Accept-Ranges come back; 416 passes through; an upstream 403 re-resolves the
  address and retries once; a client that disconnects closes the upstream; at most
  MAX_CONNECTIONS requests per stream (the oldest is cut); VOD streams count against
  watch.py's per-user and relay-wide caps both ways; tokens are bound and expire;
  idle sessions are reaped; a page has one stream (VOD and HLS replace each other).
- Progress: reports reach the PC cursor and pc-progress.json, keep the VOD stream
  alive while paused, and the next open resumes there.

The upstream is a local HTTP server with Range support serving a generated MP4 (made
with ffmpeg when it is installed, otherwise hand-built boxes): Bilibili may be blocked
on the developer's network and is never contacted.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import bilibili, bililogin, vod, watch, web
from server.relay.bilibili import BilibiliError, Bilibili, MemberOnly

import test_relay_watch as tw
from test_relay_web import FakeClient

FFMPEG = shutil.which("ffmpeg")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def no_proxy_open(request, timeout):
    return NO_PROXY.open(request, timeout=timeout)


def generated_mp4() -> bytes:
    """A real two-second H.264 + AAC MP4 when ffmpeg is installed; otherwise hand-built boxes padded to 600 KB."""
    if FFMPEG:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            done = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                   "testsrc=size=160x90:rate=15:duration=2", "-f", "lavfi", "-i",
                                   "sine=frequency=440:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
                                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart",
                                   str(out)], capture_output=True, timeout=60)
            if done.returncode == 0 and out.stat().st_size > 1000:
                return out.read_bytes()
    data = tw.FTYP + tw.moov() + tw.fragment(2.0)
    return data + bytes(range(256)) * ((600_000 - len(data)) // 256)


CLIP = generated_mp4()
BIG = bytes(range(256)) * (8 * 1024 * 1024 // 256)


class Upstream:
    """A CDN stand-in: GET /<name> with Range support; can answer 403 a few times, go slowly, and records requests."""

    def __init__(self):
        self.files = {"/clip.mp4": CLIP, "/big.mp4": BIG}
        self.requests: list[dict] = []
        self.forbid = 0
        self.delay = 0.0
        self.aborted = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                owner.requests.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
                if owner.forbid > 0:
                    owner.forbid -= 1
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = owner.files.get(path)
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                wanted = self.headers.get("Range")
                start, end, code = 0, len(data) - 1, 200
                if wanted:
                    first, _, last = wanted.removeprefix("bytes=").partition("-")
                    if first == "":
                        start, end = max(0, len(data) - int(last)), len(data) - 1
                    else:
                        start, end = int(first), min(int(last), len(data) - 1) if last else len(data) - 1
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    code = 206
                body = data[start:end + 1]
                self.send_response(code)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                try:
                    for at in range(0, len(body), 64 * 1024):
                        self.wfile.write(body[at:at + 64 * 1024])
                        if owner.delay:
                            time.sleep(owner.delay)
                except OSError:
                    owner.aborted.set()
                    self.close_connection = True

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def playurl(url, segments=1, fmt="mp4720", codec=7, preview=False):
    """A canned /x/player/playurl answer (data) like Bilibili's html5 platform gives."""
    durl = [{"order": n + 1, "length": 2000, "size": len(CLIP), "url": url if n == 0 else url + f"&part={n}",
             "backup_url": [url + "&backup=1"]} for n in range(segments)]
    return {"quality": 64, "format": fmt, "timelength": 2000, "video_codecid": codec, "durl": durl,
            "is_preview": 1 if preview else 0}


# ---------------------------------------------------------------- pure parts

class PlayableTests(unittest.TestCase):
    def test_single_segment_h264_mp4_plays_natively(self):
        info = bilibili.play_info(playurl("https://upos.example/a.mp4?deadline=1"))
        self.assertEqual(vod.playable(info), "")
        self.assertEqual((info["segments"], info["codec"], info["duration"], info["size"]), (1, 7, 2.0, len(CLIP)))
        self.assertEqual(info["backups"], ("https://upos.example/a.mp4?deadline=1&backup=1",))
        media = vod.media_of(info)
        self.assertEqual((media.url, media.duration), ("https://upos.example/a.mp4?deadline=1", 2.0))

    def test_what_falls_back_to_hls(self):
        cases = {"分成了 3 段": playurl("https://u/a.flv", segments=3, fmt="flv"),
                 "画面是 HEVC": playurl("https://u/a.mp4", codec=12),
                 "格式是 flv720": playurl("https://u/a.flv", fmt="flv720"),
                 "只能试看": playurl("https://u/a.mp4", preview=True),
                 "没有播放地址": {"durl": []}}
        for reason, play in cases.items():
            self.assertEqual(vod.playable(bilibili.play_info(play)), reason)
        # No format given: the path decides.
        self.assertEqual(vod.playable(bilibili.play_info({"durl": [{"url": "https://u/a.mp4?x=1"}]})), "")
        self.assertEqual(vod.playable(bilibili.play_info({"durl": [{"url": "https://u/a.flv"}]})), "不是 MP4")
        self.assertEqual(bilibili.play_info("junk")["segments"], 0)

    def test_tokens_are_not_interchangeable_with_watch_tokens(self):
        self.assertNotEqual(vod.derive_secret("k"), watch.derive_secret("k"))


class BilibiliVodMediaTests(unittest.TestCase):
    """Bilibili.vod_media: the html5 MP4 at 720p, with the viewer's cookies when signed in."""

    def client(self, answers):
        self.asked = []

        def fetch(url, cookie, timeout=None):
            self.asked.append((url, cookie))
            for part, answer in answers.items():
                if part in url:
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
            return {"code": 0, "data": {"b_3": "x"}}

        client = Bilibili(fetch_json=fetch, quality=16)
        client._cookie = "buvid3=x"
        return client

    def test_ugc_video_asks_html5_mp4_at_720p(self):
        client = self.client({"/view": {"code": 0, "data": {"cid": 7}},
                              "/x/player/playurl": {"code": 0, "data": playurl("https://upos.example/v.mp4")}})
        info = client.vod_media("BV1xx411c7mD")
        self.assertEqual(vod.playable(info), "")
        url = [u for u, _ in self.asked if "playurl" in u][0]
        for part in ("platform=html5", "qn=64", "fnval=1", "cid=7"):
            self.assertIn(part, url)
        # A part id ("BV…/cid") needs no view request; a view fetched moments ago (video()) is reused.
        self.asked.clear()
        client.vod_media("BV1xx411c7mD/9")
        self.assertEqual([u for u, _ in self.asked if "/view" in u], [])
        self.assertIn("cid=9", self.asked[0][0])
        client._keep_view("BV1yy411c7mD", {"cid": 5})
        self.asked.clear()
        client.vod_media("BV1yy411c7mD")
        self.assertEqual([u for u, _ in self.asked if "/view" in u], [])

    def test_signed_in_uses_cookies_and_falls_back_to_anonymous(self):
        from test_relay_bililogin import login
        client = self.client({"/x/player/playurl": {"code": 0, "data": playurl("https://upos.example/v.mp4")}})
        client.vod_media("BV1xx411c7mD/9", login=login())
        self.assertIn("SESSDATA", self.asked[0][1])
        client = self.client({"/x/player/playurl": BilibiliError("x")})
        with self.assertRaises(BilibiliError):
            client.vod_media("BV1xx411c7mD/9", login=login())
        self.assertEqual(len([u for u, _ in self.asked if "playurl" in u]), 2, "cookies, then anonymous")

    def test_bangumi_episode_and_member_preview(self):
        client = self.client({"/pgc/player/web/playurl": {"code": 0, "result": playurl("https://upos.example/ep.mp4")}})
        info = client.vod_media("ep2/12")
        self.assertEqual(vod.playable(info), "")
        self.assertIn("ep_id=2", self.asked[0][0])
        self.assertIn("qn=64", self.asked[0][0])
        client = self.client({"/pgc/player/web/playurl": {"code": 0, "result": playurl("https://u/p.mp4", preview=True)}})
        with self.assertRaises(MemberOnly):
            client.vod_media("ep2/12")


# ---------------------------------------------------------------- the range proxy

class FakeHandler:
    """Just enough of ManageHandler for VodService.serve, writing into a buffer."""

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.status = None
        self.sent: dict = {}
        self.body = bytearray()
        self.close_connection = False
        self.connection = None
        handler = self

        class W:
            def write(self, data):
                handler.body += data

        self.wfile = W()

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.sent[name] = value

    def end_headers(self):
        pass

    def _send(self, code, body, kind, headers=None):
        self.status, self.body = code, bytearray(body)
        self.sent = dict(headers or {}, **{"Content-Type": kind})


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.resolved = 0
        self.watch = watch.WatchService("/opt/ff", b"w" * 32, log=lambda _m: None, background=False)
        self.service = vod.VodService(b"v" * 32, watch=self.watch, log=lambda _m: None, opener=no_proxy_open,
                                      background=False)

    def tearDown(self):
        self.service.stop_all()
        self.upstream.close()

    def source(self, name="/clip.mp4", key="a"):
        def resolve():
            self.resolved += 1
            return vod.Media(url=f"{self.upstream.base}{name}?deadline={self.resolved}")

        return vod.VodSource(("video", "pc", "bilibili", key), resolve=resolve,
                             headers=(("Referer", bilibili.REFERER), ("User-Agent", bilibili.UA)),
                             label="片", site="bilibili", first=vod.Media(url=f"{self.upstream.base}{name}?deadline=0"))

    def serve(self, session, wanted=None):
        handler = FakeHandler({"Range": wanted} if wanted else {})
        self.service.serve(handler, session)
        return handler

    def test_ranges_pass_through_with_the_sources_headers(self):
        session = self.service.open("pc1-aa", self.source(), user=1)
        handler = self.serve(session, "bytes=100-1099")
        self.assertEqual(handler.status, 206)
        self.assertEqual(bytes(handler.body), CLIP[100:1100])
        self.assertEqual(handler.sent["Content-Range"], f"bytes 100-1099/{len(CLIP)}")
        self.assertEqual(handler.sent["Content-Length"], "1000")
        self.assertEqual(handler.sent["Accept-Ranges"], "bytes")
        self.assertEqual(handler.sent["Content-Type"], "video/mp4")
        seen = self.upstream.requests[-1]
        self.assertEqual((seen["range"], seen["referer"], seen["user-agent"]), ("bytes=100-1099", bilibili.REFERER, bilibili.UA))
        self.assertEqual(self.resolved, 0, "the address found when choosing VOD is used first")
        # Open-ended and suffix ranges, and no range at all (200, the whole file).
        self.assertEqual(bytes(self.serve(session, "bytes=500-").body), CLIP[500:])
        self.assertEqual(bytes(self.serve(session, "bytes=-10").body), CLIP[-10:])
        whole = self.serve(session)
        self.assertEqual((whole.status, bytes(whole.body)), (200, CLIP))
        self.assertNotIn("Content-Range", whole.sent)
        # A malformed or multi-part range is not forwarded.
        self.assertEqual(self.serve(session, "bytes=1-2,5-6").status, 200)
        self.assertNotIn("range", self.upstream.requests[-1])
        # Beyond the end: 416 with the size.
        beyond = self.serve(session, f"bytes={len(CLIP) + 10}-")
        self.assertEqual((beyond.status, beyond.sent.get("Content-Range")), (416, f"bytes */{len(CLIP)}"))
        self.assertEqual(session.error, "")

    def test_expired_address_is_resolved_again_once(self):
        session = self.service.open("pc1-aa", self.source(), user=1)
        self.upstream.forbid = 1
        handler = self.serve(session, "bytes=0-99")
        self.assertEqual((handler.status, bytes(handler.body)), (206, CLIP[:100]))
        self.assertEqual(self.resolved, 1)
        self.assertEqual([r["path"].split("=")[-1] for r in self.upstream.requests], ["0", "1"])
        # Still refused after a fresh address: 502 with a reason, no address in it.
        self.upstream.forbid = 5
        handler = self.serve(session, "bytes=0-99")
        self.assertEqual(handler.status, 502)
        self.assertIn("B 站", handler.body.decode())
        self.assertNotIn("http", handler.body.decode())
        self.assertEqual(self.resolved, 2, "one more lookup, not a loop")

    def test_backup_address_and_lookup_failures(self):
        dead = vod.Media(url="http://127.0.0.1:9/x.mp4", backups=(f"{self.upstream.base}/clip.mp4",))
        source = vod.VodSource(("video", "pc", "bilibili", "b"), resolve=lambda: dead, site="bilibili", first=dead)
        session = self.service.open("pc1-bb", source, user=1)
        self.assertEqual(bytes(self.serve(session, "bytes=0-9").body), CLIP[:10])

        def broken():
            raise BilibiliError("/x/player/playurl: URLError: EOF occurred in violation of protocol")

        source = vod.VodSource(("video", "pc", "bilibili", "c"), resolve=broken, site="bilibili")
        session = self.service.open("pc1-bb", source, user=1)
        handler = self.serve(session, "bytes=0-9")
        self.assertEqual(handler.status, 502)
        self.assertEqual(handler.body.decode(), bilibili.BLOCKED_HINT)

    def test_old_address_is_refreshed_before_use(self):
        clock = [0.0]
        self.service.clock = lambda: clock[0]
        session = self.service.open("pc1-aa", self.source(), user=1)
        clock[0] = vod.URL_REFRESH_S + 1
        self.serve(session, "bytes=0-9")
        self.assertEqual(self.resolved, 1)

    def test_connections_per_stream_are_bounded(self):
        session = self.service.open("pc1-aa", self.source(), user=1)
        conns = [session.admit(None, limit=2) for _ in range(3)]
        self.assertEqual([c.cancelled for c in conns], [True, False, False], "the oldest is cut")
        for conn in conns:
            session.release(conn)
        self.assertFalse(session.busy())
        # Closing the stream cuts what is still running.
        conn = session.admit(None)
        self.service.close("pc1-aa")
        self.assertTrue(conn.cancelled)

    def test_caps_are_shared_with_hls_streams(self):
        self.watch.max_copy = 3
        live = watch.Source(("live", "ch000", "https://x/1.m3u8", "UA"), url="https://x/1.m3u8", paced=False)
        self.watch.popen = tw.Popen(data=tw.LIVE_DATA)
        self.service.open("pc1-aa", self.source(key="a"), user=1)
        self.watch.open("pc1-bb", live, user=1)
        with self.assertRaises(watch.Busy) as caught:
            self.service.open("pc1-cc", self.source(key="c"), user=1)
        self.assertTrue(caught.exception.per_user)
        with self.assertRaises(watch.Busy):
            self.watch.open("pc1-cc", live, user=1)
        # Relay-wide: a third stream from someone else fits, a fourth does not.
        self.service.open("pc2-aa", self.source(key="d"), user=2)
        with self.assertRaises(watch.Busy) as caught:
            self.service.open("pc3-aa", self.source(key="e"), user=3)
        self.assertFalse(caught.exception.per_user)
        with self.assertRaises(watch.Busy):
            self.watch.open("pc3-aa", live, user=3)
        # The same page switching from HLS to VOD frees its HLS stream; and back.
        self.service.open("pc1-bb", self.source(key="b"), user=1)
        self.assertEqual([s.device for s in self.watch.streams()], [])
        self.assertEqual(len(self.service.sessions()), 3)

    def test_same_video_keeps_its_stream_and_tokens_are_bound(self):
        session = self.service.open("pc1-aa", self.source(), user=1)
        self.assertIs(self.service.open("pc1-aa", self.source(), user=1), session)
        token = self.service.token(session)
        self.assertIs(self.service.find(session.sid, token), session)
        self.assertIsNone(self.service.find(session.sid, "1.x" + token[3:]))
        self.assertIsNone(self.service.find("0" * 16, token))
        self.assertIsNone(self.service.find("../x", token))
        self.service.wall = lambda: time.time() + vod.TOKEN_LIFETIME_S + 60
        self.assertIsNone(self.service.find(session.sid, token), "expired")
        other = self.service.open("pc1-aa", self.source(key="z"), user=1)
        self.assertNotEqual(other.sid, session.sid)
        self.assertTrue(session.stopped)

    def test_idle_streams_are_reaped_but_not_while_sending(self):
        clock = [0.0]
        self.service.clock = lambda: clock[0]
        session = self.service.open("pc1-aa", self.source(), user=1)
        session.clock = self.service.clock
        conn = session.admit(None)
        clock[0] = vod.IDLE_S + 5
        self.service.reap()
        self.assertEqual(len(self.service.sessions()), 1, "a request is still running")
        session.release(conn)
        clock[0] += vod.IDLE_S + 5
        self.service.reap()
        self.assertEqual(self.service.sessions(), [])
        self.assertTrue(session.stopped)


class RelayVodTests(unittest.TestCase):
    """End to end through the relay's web port: POST /tv/api/watch with vod: true, then GET /tv/vod/…/media."""
    selfhost = True
    setUp_relay = tw.RelayWatchTests.setUp
    tearDown_relay = tw.RelayWatchTests.tearDown
    online = tw.RelayWatchTests.online
    request = tw.RelayWatchTests.request

    def setUp(self):
        self.setUp_relay()
        self.upstream = Upstream()
        self.client = FakeVodClient(self.upstream.base)
        self.relay.clients["bilibili"] = self.client
        self.carol = self.accounts.create_user("Carol")
        self.cookies["carol"] = "tvs=" + self.accounts.new_session(self.carol, "pc")
        self.as_carol = web.Caller(user=self.carol, role="user", name="Carol")
        if self.relay.vod is not None:
            self.relay.vod.opener = no_proxy_open
            self.relay.add_creator("https://space.bilibili.com/42", self.as_carol)
            self.relay.add_series("https://www.bilibili.com/bangumi/play/ss1", "", self.as_carol)
        self.logins: dict = {}
        self.relay.bili_login = lambda user: self.logins.get(user)

    def tearDown(self):
        if self.relay.vod is not None:
            self.relay.vod.stop_all()
        self.tearDown_relay()
        self.upstream.close()

    def watch(self, who="carol", **body):
        body.setdefault("viewer", "c0ffee00")
        status, _, raw = self.request("POST", "/tv/api/watch", who, body)
        return status, (json.loads(raw) if status == 200 else None)

    def media(self, url, wanted=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=20)
        connection.request("GET", "/tv/" + url, headers={"Range": wanted} if wanted else {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def test_creator_video_plays_as_vod_through_the_range_proxy(self):
        self.relay.user_state(self.carol).save_position("up01", "b", 130.0)
        status, answer = self.watch(channel="up01", vod=True)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["mode"], answer["kind"], answer["title"], answer["index"]), ("vod", "video", "tb", 2))
        self.assertRegex(answer["url"], r"^vod/[0-9a-f]{16}/\d+\.[A-Za-z0-9_-]+/media$")
        # The same start the HLS path would use (TV position − RESUME_BACK_S), sought by the browser.
        self.assertEqual((answer["start"], answer["duration"], answer["seekable"]), (120.0, 600, True))
        self.assertNotIn("quality", answer, "not signed in: 720P is all there is")
        self.assertEqual(self.popen.procs, [], "no ffmpeg")
        code, headers, data = self.media(answer["url"], "bytes=0-1023")
        self.assertEqual((code, headers["Content-Range"], headers["Accept-Ranges"]), (206, f"bytes 0-1023/{len(CLIP)}", "bytes"))
        self.assertEqual(data, CLIP[:1024])
        self.assertEqual(self.upstream.requests[-1]["referer"], bilibili.REFERER)
        self.assertEqual(self.client.calls, [("b", None)])
        # The token is required; a cookie is no substitute; a watch-style path is not a vod path.
        sid, token = answer["url"].split("/")[1:3]
        self.assertEqual(self.media(f"vod/{sid}/1.{token.split('.')[1]}/media")[0], 404)
        self.assertEqual(self.request("GET", f"/tv/vod/{sid}/x.y/media", "carol")[0], 404)
        self.assertEqual(self.request("GET", f"/tv/watch/{sid}/{token}/index.m3u8")[0], 404)
        # Asking again for the same video keeps the stream (the player seeks by itself).
        again = self.watch(channel="up01", vod=True, start=300)[1]
        self.assertEqual((again["sid"], again["start"]), (answer["sid"], 300.0))

    def test_what_stays_on_hls(self):
        # An old console (no vod flag) gets HLS as before.
        self.assertEqual(self.watch(channel="up01")[1]["mode"], "hls")
        # Several durl segments, FLV, HEVC: HLS.
        for play in (playurl("x", segments=2, fmt="flv"), playurl("x", codec=12)):
            self.client.plays["a"] = play
            self.assertEqual(self.watch(channel="up01", vod=True, episode=1)[1]["mode"], "hls")
        # A lookup that fails: HLS, which reports the error the usual way.
        self.client.down = True
        self.assertEqual(self.watch(channel="up01", vod=True, episode=2)[1]["mode"], "hls")
        self.client.down = False
        # Live channels always.
        answer = self.watch(channel="ch000", vod=True)[1]
        self.assertEqual((answer["mode"], answer["kind"]), ("hls", "live"))
        self.assertEqual(self.relay.vod.sessions(), [])

    def test_signed_in_viewer_chooses_720p_vod_or_1080p_hls(self):
        self.logins[self.carol] = bililogin.Login(self.carol, {"SESSDATA": "s"}, vip=False)
        answer = self.watch(channel="up01", vod=True)[1]
        self.assertEqual(answer["mode"], "vod")
        self.assertEqual(answer["quality"], {"current": "720p", "choices": ["720p", "1080p"]})
        self.assertIs(self.client.calls[-1][1], self.logins[self.carol], "her cookies (member episodes)")
        hd = self.watch(channel="up01", vod=True, quality="1080p", start=42)[1]
        self.assertEqual((hd["mode"], hd["quality"]["current"], hd["start"]), ("hls", "1080p", 42.0))
        self.assertEqual(self.relay.vod.sessions(), [], "the page's VOD stream gave way")
        back = self.watch(channel="up01", vod=True, quality="720p", start=50)[1]
        self.assertEqual((back["mode"], back["start"]), ("vod", 50.0))
        self.assertEqual([s.device for s in self.relay.watch.streams() if s.alive], [], "and the HLS one too")

    def test_series_cast_and_history_share_the_resolution(self):
        answer = self.watch(channel="sr01", vod=True, episode=2)[1]
        self.assertEqual((answer["mode"], answer["episode"], answer["title"]), ("vod", 2, "第2集"))
        self.assertEqual(self.client.calls[-1][0], "ep2/2")
        answer = self.watch(url="https://www.bilibili.com/video/BV1xx411c7mD", vod=True)[1]
        self.assertEqual((answer["mode"], answer["channel"], answer["video"]["id"]), ("vod", "cast", "BV1xx411c7mD"))
        answer = self.watch(video={"site": "bilibili", "id": "BV1xx411c7mD"}, vod=True)[1]
        self.assertEqual(answer["mode"], "vod")
        # YouTube stays on the node's stream through ffmpeg.
        self.relay.user_state(self.carol).remember(tw.Video(bvid="ytcast00001", title="yt", duration=60, pubdate=0,
                                                            site="youtube"), at=self.now)
        answer = self.watch(video={"site": "youtube", "id": "ytcast00001"}, vod=True)[1]
        self.assertEqual(answer["mode"], "hls")

    def test_play_to_this_pc_passes_the_flag(self):
        status, _, raw = self.request("POST", "/tv/api/play", "carol",
                                      {"target": "pc", "viewer": "c0ffee00", "vod": True,
                                       "item": {"kind": "channel", "channel": "up01"}})
        answer = json.loads(raw)
        self.assertEqual((answer["mode"], answer["target"]), ("vod", "pc"))

    def test_progress_resume_and_stop(self):
        answer = self.watch(channel="up01", vod=True)[1]
        session = self.relay.vod.sessions()[0]
        session.seen -= 100
        status, _, raw = self.request("POST", "/tv/api/watch/progress", "carol",
                                      {"viewer": "c0ffee00", "sid": answer["sid"], "position": 321.5})
        self.assertTrue(json.loads(raw)["ok"])
        self.assertLess(time.monotonic() - session.seen, 5, "a report keeps a paused VOD stream alive")
        saved = self.relay.pc_progress.get(self.carol, "up01")
        self.assertEqual((saved["bvid"], saved["position"]), ("a", 321.5))
        # A late report from another stream id does not count.
        self.request("POST", "/tv/api/watch/progress", "carol", {"viewer": "c0ffee00", "sid": "f" * 16, "position": 9})
        self.assertEqual(self.relay.pc_progress.get(self.carol, "up01")["position"], 321.5)
        stopped = json.loads(self.request("POST", "/tv/api/watch/stop", "carol",
                                          {"viewer": "c0ffee00", "sid": answer["sid"], "position": 400})[2])
        self.assertEqual(stopped, {"ok": True, "stopped": True})
        self.assertEqual(self.relay.vod.sessions(), [])
        self.assertEqual(self.media(answer["url"], "bytes=0-9")[0], 404, "closed")
        again = self.watch(channel="up01", vod=True)[1]
        self.assertEqual((again["title"], again["start"], again.get("resume")), ("ta", 400.0, 400.0))
        # The TV's state is untouched.
        self.assertEqual(self.relay.user_state(self.carol).get("up01").watched, [])

    def test_following_the_tv_plays_the_tvs_video_as_vod(self):
        self.online(tw.ALICE_DEVICE, "up01")
        video = tw.Video(bvid="BV1xx411c7mD", title="视频标题", duration=600, pubdate=0)
        dev = self.relay.find_device(tw.ALICE_DEVICE, web.Caller(user=self.alice, role="user"))
        self.relay._paused[dev["slot"]] = {"cid": "up01", "video": video, "position": 75.4, "at": self.now}
        status, _, raw = self.request("POST", "/tv/api/watch", "alice",
                                      {"device": tw.ALICE_DEVICE, "viewer": "a11ce000", "vod": True})
        answer = json.loads(raw)
        self.assertEqual((answer["mode"], answer["own"], answer["start"]), ("vod", True, 75.4))
        self.assertEqual([s.device for s in self.relay.vod.sessions()], [f"pc{self.alice}-a11ce000"])
        # Without a page id (old console), the family's HLS stream.
        status, _, raw = self.request("POST", "/tv/api/watch", "alice", {"device": tw.ALICE_DEVICE, "vod": True})
        self.assertEqual(json.loads(raw)["mode"], "hls")


class CloudRelayVodTests(unittest.TestCase):
    def test_cloud_relay_has_no_vod(self):
        relay = web.Relay(None, FakeClient(), None, "/opt/ff", "k" * 32, log=lambda _m: None)
        self.assertIsNone(relay.vod)
        handler = FakeHandler()
        handler._not_found = lambda: handler._send(404, b"", "text/html")
        self.assertTrue(vod.get(handler, relay, "/vod/" + "0" * 16 + "/1.aaaaaaaaaaaaaaaa/media"))
        self.assertEqual(handler.status, 404)
        self.assertFalse(vod.get(handler, relay, "/watch/x"))


class RealSocketTests(unittest.TestCase):
    """A real browser-like client against the relay port: disconnecting mid-body closes the upstream."""
    selfhost = True
    setUp = RelayVodTests.setUp
    tearDown = RelayVodTests.tearDown
    setUp_relay = tw.RelayWatchTests.setUp
    tearDown_relay = tw.RelayWatchTests.tearDown
    online = tw.RelayWatchTests.online
    request = tw.RelayWatchTests.request
    watch = RelayVodTests.watch

    def test_disconnect_closes_the_upstream(self):
        self.client.paths["a"] = "/big.mp4"
        self.upstream.delay = 0.01
        answer = self.watch(channel="up01", vod=True, episode=1)[1]
        self.assertEqual(answer["mode"], "vod")
        sock = socket.create_connection(("127.0.0.1", self.manage.server_address[1]), timeout=10)
        sock.sendall(f"GET /tv/{answer['url']} HTTP/1.1\r\nHost: x\r\nRange: bytes=0-\r\n\r\n".encode())
        got = b""
        while len(got) < 200_000:
            got += sock.recv(65536)
        self.assertIn(b"206", got.split(b"\r\n", 1)[0])
        session = self.relay.vod.sessions()[0]
        self.assertTrue(session.busy())
        sock.close()
        self.assertTrue(self.upstream.aborted.wait(10), "the upstream connection was closed")
        deadline = time.monotonic() + 10
        while session.busy() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(session.busy())
        self.assertLess(session.sent, len(BIG))


class FakeVodClient(FakeClient):
    """FakeClient with Bilibili.vod_media: answers from canned html5 playurl data pointing at the local upstream."""

    def __init__(self, base):
        super().__init__()
        self.base = base
        self.calls: list[tuple] = []
        self.plays: dict = {}
        self.paths: dict = {}

    def vod_media(self, bvid, login=None):
        self.calls.append((bvid, login))
        if self.down:
            raise BilibiliError("offline")
        play = self.plays.get(bvid) or playurl(f"{self.base}{self.paths.get(bvid, '/clip.mp4')}?bvid={bvid}")
        return bilibili.play_info(play)

    def media_url(self, bvid, login=None, quality=None):
        return super().media_url(bvid)

    def series(self, source, login=None):
        return super().series(source)

    def parts(self, bvid):
        return None


if __name__ == "__main__":
    unittest.main()
