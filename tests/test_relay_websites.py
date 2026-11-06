"""Other websites in the relay (server/relay/websites.py and its wiring in web.py).

Pure parts: share-text URLs, the local-address refusal, site keys, cookies.txt
filtering, the per-site settings files, the HLS input choice and the node's
error classes. Through the relay (a fake "web" client, real HTTP): a pasted
page plays on the PC as a browser file (vod), as HLS through the relay's own
ffmpeg, or through the node's stream when the site goes through the YouTube
proxy; DRM is said as such; guests are refused; casting to the TV records the
video with site "web" and the TV's stream opens it through the node.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server.relay import web, websites
from server.relay.bilibili import SourceError
from server.relay.state import Video
from server.relay.youtube import NodeError

import test_relay_vod as tv
import test_relay_watch as tw
from test_relay_web import FakeProc

PAGE = "https://www.dailymotion.com/video/x8abcd"
NOW = 1_800_000_000


def cookie_line(domain, name="sid", expiry=NOW + 3600):
    return "\t".join([domain, "TRUE", "/", "TRUE", str(expiry), name, "value"])


class UrlTests(unittest.TestCase):
    def test_share_text(self):
        self.assertEqual(websites.first_url("看这个 https://v.youku.com/v_show/id_X.html?a=1，好看"),
                         "https://v.youku.com/v_show/id_X.html?a=1")
        self.assertEqual(websites.first_url("no link here"), "")

    def test_which_site(self):
        self.assertTrue(websites.is_web_link(PAGE))
        for other in ("https://www.bilibili.com/video/BV1xx411c7mD", "https://b23.tv/abc",
                      "https://www.youtube.com/watch?v=jNQXAC9IVRw", "https://youtu.be/jNQXAC9IVRw",
                      "BV1xx411c7mD", "www.dailymotion.com/video/x"):
            self.assertFalse(websites.is_web_link(other), other)
        self.assertEqual(web._site_of(PAGE), "web")
        self.assertEqual(web._site_of("https://b23.tv/abc"), "bilibili")
        self.assertEqual(web._site_of("BV1xx411c7mD"), "bilibili")
        self.assertEqual(web._site_of("https://youtu.be/jNQXAC9IVRw"), "youtube")

    def test_safe_url(self):
        self.assertEqual(websites.safe_url("分享 HTTPS://WWW.Dailymotion.com/video/x8abcd#t=3"),
                         "https://www.dailymotion.com/video/x8abcd")
        self.assertEqual(websites.safe_url("https://tv.cctv.com"), "https://tv.cctv.com/")
        for bad in ("http://127.0.0.1:8080/tv", "http://localhost/x", "http://10.0.0.2/v", "http://192.168.31.1/",
                    "http://[::1]/v", "http://nas.local/v", "http://router/", "file:///etc/passwd",
                    "https://a:b@example.com/", "https://example.com/" + "x" * 2100, ""):
            self.assertIsNone(websites.safe_url(bad), bad)

    def test_site_key(self):
        self.assertEqual(websites.site_key("https://v.youku.com/x"), "youku.com")
        self.assertEqual(websites.site_key("https://news.cctv.com.cn/x"), "cctv.com.cn")
        self.assertEqual(websites.site_key("https://1.2.3.4/x"), "")
        self.assertEqual(websites.clean_site("Youku.com"), "youku.com")
        self.assertEqual(websites.clean_site("https://www.iqiyi.com/v_1.html"), "iqiyi.com")
        self.assertEqual(websites.clean_site("not a site"), "")


class CookieTests(unittest.TestCase):
    def test_keeps_only_the_site(self):
        text = "\n".join(["# Netscape HTTP Cookie File", cookie_line(".youku.com"), cookie_line("v.youku.com", "a"),
                          "#HttpOnly_" + cookie_line(".youku.com", "h"), cookie_line(".google.com", "g"),
                          cookie_line(".youku.com", "old", NOW - 10), "junk line"])
        content, count = websites.parse_cookies(text, "youku.com", now=NOW)
        self.assertEqual(count, 3)
        self.assertNotIn("google", content)
        self.assertEqual(content.count("\n"), 2 + 4)

    def test_refusals_do_not_echo_the_file(self):
        for text, site in (("", "youku.com"), ("hello\nworld", "youku.com"),
                           (cookie_line(".google.com", "secretname"), "youku.com")):
            with self.assertRaises(websites.CookieError) as caught:
                websites.parse_cookies(text, site, now=NOW)
            self.assertNotIn("secretname", str(caught.exception))

    def test_settings_files(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = websites.Settings(Path(folder) / "websites")
            self.assertEqual(settings.summary(), [])
            settings.save_cookies("youku.com", cookie_line(".youku.com"))
            path = settings.cookies_path("youku.com")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            settings.set_proxy("dailymotion.com", "youtube")
            rows = {r["site"]: r for r in settings.summary()}
            self.assertEqual(set(rows), {"youku.com", "dailymotion.com"})
            self.assertTrue(rows["youku.com"]["cookies"]["present"])
            self.assertEqual(rows["youku.com"]["proxy"], "direct")
            self.assertEqual(rows["dailymotion.com"]["proxy"], "youtube")
            settings.set_proxy("dailymotion.com", "direct")
            self.assertTrue(settings.delete_cookies("youku.com"))
            self.assertFalse(settings.delete_cookies("youku.com"))
            self.assertEqual(settings.summary(), [])


class ClientTests(unittest.TestCase):
    def test_errors_become_kinds(self):
        def fetch(path, params, timeout=None):
            raise NodeError(422, json.dumps({"error": "x", "kind": "drm"}))
        client = websites.Web(fetch_json=fetch)
        with self.assertRaises(SourceError) as caught:
            client.video(PAGE)
        self.assertTrue(caught.exception.permanent)
        self.assertEqual(websites.message_of(caught.exception), websites.MESSAGES["drm"])

        def busy(path, params, timeout=None):
            raise NodeError(503, "busy")
        with self.assertRaises(SourceError) as caught:
            websites.Web(fetch_json=busy).video(PAGE)
        self.assertFalse(caught.exception.permanent)
        self.assertEqual(caught.exception.reason, "busy")

    def test_list_is_not_a_video(self):
        client = websites.Web(fetch_json=lambda path, params, timeout=None: {"kind": "list", "entries": []})
        with self.assertRaises(SourceError) as caught:
            client.video(PAGE)
        self.assertEqual(websites.message_of(caught.exception), websites.MESSAGES["list"])

    def test_video(self):
        asked = []

        def fetch(path, params, timeout=None):
            asked.append((path, params))
            return {"kind": "video", "id": params["url"], "title": "A clip", "duration": 61}
        video = websites.Web(fetch_json=fetch).video(PAGE)
        self.assertEqual((video.bvid, video.title, video.duration, video.site), (PAGE, "A clip", 61, "web"))
        self.assertEqual(asked, [("/web/info", {"url": PAGE})])

    def test_hls_input(self):
        hls = {"format_id": "hls-480", "url": "https://cdn.example/480.m3u8", "vcodec": "avc1", "acodec": "mp4a",
               "headers": {"User-Agent": "UA", "Referer": "https://www.dailymotion.com/"}}
        self.assertEqual(websites.hls_input({"format_id": "hls-480", "formats": [hls]}),
                         ("https://cdn.example/480.m3u8", "UA", "https://www.dailymotion.com/"))
        split = {"format_id": "v+a", "formats": [dict(hls, format_id="v", acodec="none"),
                                                 dict(hls, format_id="a", vcodec="none")]}
        self.assertIsNone(websites.hls_input(split), "separate video and audio go through the node")
        self.assertEqual(websites.hls_input({"format_id": "x+y", "formats": [hls]})[0], hls["url"])


class FakeWeb:
    """The "web" client: one page whose formats are set per test."""

    def __init__(self, base):
        self.base = base
        self.kind = "mp4"
        self.fail = None
        self.opened = []
        self.infos = 0

    def video_id(self, text):
        return websites.safe_url(text)

    def video(self, url):
        self.infos += 1
        if self.fail is not None:
            raise self.fail
        return Video(bvid=url, title="A clip", duration=60, pubdate=0, site="web")

    def formats(self, url, prefetch=False, fresh=False, timeout=None):
        if self.fail is not None:
            raise self.fail
        expire = int(time.time()) + 20000
        if self.kind == "mp4":
            formats = [{"format_id": "http-360", "url": f"{self.base}/clip.mp4?expire={expire}", "protocol": "https",
                        "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "height": 360, "ext": "mp4"}]
        else:
            formats = [{"format_id": "hls-480", "url": "https://cdn.example/480.m3u8", "protocol": "m3u8_native",
                        "vcodec": "avc1.4d401e", "acodec": "mp4a.40.2", "height": 480, "ext": "mp4",
                        "headers": {"User-Agent": "WebUA", "Referer": "https://www.dailymotion.com/"}}]
        return {"id": url, "title": "A clip", "duration": 60, "format_id": formats[0]["format_id"],
                "formats": formats}

    def open(self, url, offset):
        self.opened.append((url, offset))
        return FakeProc()

    def creator_id(self, text):
        return None

    def series_id(self, text):
        return None


class RelayWebTests(unittest.TestCase):
    selfhost = True
    setUp_relay = tw.RelayWatchTests.setUp
    tearDown_relay = tw.RelayWatchTests.tearDown
    online = tw.RelayWatchTests.online
    request = tw.RelayWatchTests.request
    media = tv.RelayVodTests.media

    def setUp(self):
        self.setUp_relay()
        self.upstream = tv.Upstream()
        self.upstream.files["/clip.mp4"] = tv.CLIP
        self.data = tempfile.TemporaryDirectory()
        self.relay.selfhost_data = Path(self.data.name)
        self.relay.youtube_kind = "local"
        self.relay.youtube_proxy = lambda: "http://127.0.0.1:9"
        self.site = FakeWeb(self.upstream.base)
        self.relay.clients["web"] = self.site
        self.relay.webvod = websites.WebVod(lambda: self.relay.clients.get("web"),
                                            proxy_of=lambda url: websites.proxy_url(self.relay, url),
                                            log=lambda _m: None)
        self.relay.vod.opener = tv.no_proxy_open
        self.carol = self.accounts.create_user("Carol")
        self.cookies["carol"] = "tvs=" + self.accounts.new_session(self.carol, "pc")

    def tearDown(self):
        self.relay.vod.stop_all()
        self.tearDown_relay()
        self.upstream.close()
        self.data.cleanup()

    def watch(self, who="carol", text=PAGE, **body):
        body.setdefault("viewer", "c0ffee00")
        body.setdefault("vod", True)
        status, _, raw = self.request("POST", "/tv/api/watch", who, dict(body, url=text))
        self.assertEqual(status, 200)
        return json.loads(raw)

    def test_a_browser_file_plays_as_vod(self):
        answer = self.watch(dash=True)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["mode"], answer["title"], answer["duration"]), ("vod", "A clip", 60))
        code, _, data = self.media(answer["url"], "bytes=0-99")
        self.assertEqual((code, data), (206, tv.CLIP[:100]))
        self.assertEqual(self.site.opened, [], "no node stream")
        history = self.relay.user_state(self.carol).history_entry("web", PAGE)
        self.assertIsNotNone(history, "remembered in the cast history with site web")

    def test_hls_through_the_relays_ffmpeg(self):
        self.site.kind = "hls"
        answer = self.watch(dash=True)
        self.assertEqual(answer["mode"], "hls", answer)
        stream = next(s for s in self.relay.watch.streams() if s.sid == answer["sid"])
        self.assertEqual(stream.source.url, "https://cdn.example/480.m3u8")
        self.assertEqual((stream.source.user_agent, stream.source.referer), ("WebUA", "https://www.dailymotion.com/"))
        self.assertEqual(self.site.opened, [])

    def test_a_proxied_site_uses_the_nodes_stream(self):
        self.site.kind = "hls"
        websites.settings_of(self.relay).set_proxy("dailymotion.com", "youtube")
        answer = self.watch(dash=True)
        self.assertEqual(answer["mode"], "hls")
        stream = next(s for s in self.relay.watch.streams() if s.sid == answer["sid"])
        self.assertTrue(stream.wait_ready(5))
        self.assertEqual(self.site.opened, [(PAGE, 0.0)])

    def test_drm_and_lists_are_said(self):
        self.site.fail = SourceError("web /web/info: drm", permanent=True, reason="drm")
        answer = self.watch()
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"], websites.MESSAGES["drm"])

    def test_guests_and_no_local_mode(self):
        status, _, raw = self.request("POST", "/tv/api/watch", "guest", {"viewer": "c0ffee00", "url": PAGE})
        self.assertIn("访客", json.loads(raw).get("error", ""))
        guest = web.Caller(user=None, role="guest", device=tw.ALICE_DEVICE)
        self.assertEqual(self.relay.cast(PAGE, guest)["error"], web.WEB_GUEST)
        del self.relay.clients["web"]
        self.assertEqual(self.watch()["error"], web.WEB_OFF)

    def test_cast_to_the_tv(self):
        alice = web.Caller(user=self.alice, role="user", name="Alice")
        answer = self.relay.cast("看看这个 " + PAGE, alice)
        self.assertTrue(answer["ok"], answer)
        slot = self.relay._cast_of(self.relay._pick(alice, None))
        self.assertEqual((slot.cast.site, slot.cast.bvid), ("web", PAGE))
        self.relay._start(slot.cast, 12.0)
        self.assertEqual(self.site.opened, [(PAGE, 12.0)])
        self.assertEqual(self.relay.cast("http://192.168.1.2/video", alice)["ok"], False)

    def test_info_and_settings_endpoints(self):
        status, _, raw = self.request("GET", "/tv/api/websites/settings", "carol")
        self.assertEqual(status, 404, "only the admin")
        self.cookies["admin"] = "tvs=" + self.accounts.new_session(self.admin, "pc")
        status, _, raw = self.request("GET", "/tv/api/websites/settings", "admin")
        self.assertEqual((status, json.loads(raw)["sites"]), (200, []))
        status, _, raw = self.request("POST", "/tv/api/websites/cookies", "admin",
                                      {"url": PAGE, "text": cookie_line(".dailymotion.com", expiry=0)})
        answer = json.loads(raw)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["sites"][0]["site"], "dailymotion.com")
        self.assertNotIn("value", raw.decode())
        status, _, raw = self.request("POST", "/tv/api/websites/proxy", "admin",
                                      {"site": "dailymotion.com", "proxy": "youtube"})
        self.assertEqual(json.loads(raw)["sites"][0]["proxy"], "youtube")
        self.assertEqual(websites.proxy_url(self.relay, PAGE), "http://127.0.0.1:9")
        status, _, raw = self.request("POST", "/tv/api/websites/cookies/delete", "admin", {"site": "dailymotion.com"})
        self.assertFalse(json.loads(raw)["sites"][0]["cookies"]["present"])
        status, _, _ = self.request("GET", "/tv/api/websites/info?url=" + PAGE, "guest")
        self.assertEqual(status, 404, "guests do not see websites")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ConsoleWebTests(unittest.TestCase):
    def test_console_web_logic(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "console_web_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


class ThumbTests(unittest.TestCase):
    def test_only_seen_covers_are_proxied(self):
        thumbs = websites.Thumbs(size=2)
        self.assertEqual(thumbs.add("http://192.168.1.1/a.jpg", PAGE), "")
        self.assertEqual(thumbs.add("", PAGE), "")
        link = thumbs.add("https://img.example/a.jpg", PAGE)
        self.assertEqual(link, "api/websites/thumb?u=https%3A%2F%2Fimg.example%2Fa.jpg")
        self.assertEqual(thumbs.page_of("https://img.example/a.jpg"), PAGE)
        self.assertIsNone(thumbs.page_of("https://img.example/other.jpg"))
        thumbs.add("https://img.example/b.jpg", PAGE)
        thumbs.add("https://img.example/c.jpg", PAGE)
        self.assertIsNone(thumbs.page_of("https://img.example/a.jpg"), "the oldest is forgotten")

    def test_fetch_checks_type_and_size(self):
        class Answer:
            def __init__(self, kind, data):
                self.headers = {"Content-Type": kind}
                self.data = data

            def read(self, n):
                return self.data[:n]

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        self.assertEqual(websites.fetch_thumb("https://i/x", opener=lambda r, timeout: Answer("image/jpeg", b"jpg")),
                         (b"jpg", "image/jpeg"))
        self.assertIsNone(websites.fetch_thumb("https://i/x", opener=lambda r, timeout: Answer("text/html", b"<x>")))
        big = b"x" * (websites.THUMB_MAX + 1)
        self.assertIsNone(websites.fetch_thumb("https://i/x", opener=lambda r, timeout: Answer("image/png", big)))


if __name__ == "__main__":
    unittest.main()
