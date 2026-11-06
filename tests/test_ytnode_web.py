"""The local yt-dlp node's routes for other websites (/web/*, server/relay/ytnode.py).

yt-dlp and ffmpeg are fakes; the HTTP side runs over a real socket.
"""

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import ytnode

KEY = "node-key-0123456789"
TOOLS = ytnode.Tools(ytdlp="/opt/yt-dlp", deno="/opt/deno", ffmpeg="/usr/bin/ffmpeg",
                     cookies="/data/youtube/cookies.txt", proxy="socks5://127.0.0.1:7890")

VIDEO_URL = "https://www.dailymotion.com/video/x8abcd"
LIST_URL = "https://www.dailymotion.com/playlist/x6xyz"
VIDEO = {"id": "x8abcd", "title": "A clip", "duration": 61, "thumbnail": "https://img.example/t.jpg",
         "uploader": "Someone", "extractor_key": "Dailymotion", "format_id": "hls-480",
         "requested_formats": None, "url": "https://cdn.example/480.m3u8",
         "http_headers": {"User-Agent": "UA", "Referer": "https://www.dailymotion.com/"},
         "formats": [
             {"format_id": "hls-480", "protocol": "m3u8_native", "url": "https://cdn.example/480.m3u8",
              "vcodec": "avc1.4d401e", "acodec": "mp4a.40.2", "height": 480, "ext": "mp4",
              "http_headers": {"User-Agent": "UA"}},
             {"format_id": "http-720", "protocol": "https", "url": "https://cdn.example/720.mp4",
              "vcodec": "avc1.64001f", "acodec": "mp4a.40.2", "height": 720, "ext": "mp4"},
             {"format_id": "drm-1080", "protocol": "https", "url": "https://cdn.example/1080.mp4",
              "vcodec": "avc1", "acodec": "mp4a", "height": 1080, "ext": "mp4", "has_drm": True},
             {"format_id": "dash", "protocol": "http_dash_segments", "url": "https://cdn.example/m.mpd",
              "vcodec": "avc1", "acodec": "none"}]}
LISTING = {"_type": "playlist", "title": "My list", "uploader": "Someone", "extractor_key": "DailymotionPlaylist",
           "entries": [{"url": "https://www.dailymotion.com/video/x1", "title": "one", "duration": 10,
                        "thumbnails": [{"url": "https://img.example/1.jpg"}]},
                       {"url": "http://192.168.1.2/video", "title": "local"},
                       {"id": "x3", "title": "no url"}]}


class HelperTests(unittest.TestCase):
    def test_url_rules(self):
        for good in (VIDEO_URL, "http://tv.cctv.com/2024/01/01/VIDE123.shtml", "https://8.8.8.8/v"):
            self.assertTrue(ytnode.web_url_ok(good), good)
        for bad in ("file:///etc/passwd", "ftp://x.com/a", "http://127.0.0.1:8080/x", "http://localhost/x",
                    "http://192.168.1.10/v", "http://[::1]/v", "http://169.254.1.1/", "http://nas.local/v",
                    "http://router/", "https://user:pw@example.com/v", "https://a.com/" + "x" * 2100,
                    "https://a.com/x y", ""):
            self.assertFalse(ytnode.web_url_ok(bad), bad)

    def test_site(self):
        self.assertEqual(ytnode.web_site("https://v.youku.com/v_show/id_X.html"), "youku.com")
        self.assertEqual(ytnode.web_site("https://tv.cctv.com.cn/a"), "cctv.com.cn")
        self.assertEqual(ytnode.web_site("https://www.bbc.co.uk/iplayer"), "bbc.co.uk")
        self.assertEqual(ytnode.web_site("https://8.8.8.8/"), "")

    def test_args(self):
        args = ytnode.web_args(TOOLS, VIDEO_URL, cookies="/w/cookies/dailymotion.com.txt")
        self.assertIn("default,-generic", args)
        self.assertEqual(args[args.index("--proxy") + 1], "", "direct sites override the environment's proxy")
        self.assertEqual(args[args.index("--cookies") + 1], "/w/cookies/dailymotion.com.txt")
        self.assertNotIn(TOOLS.cookies, args, "the YouTube cookies never go to another site")
        self.assertEqual(args[-2:], ["--", VIDEO_URL])
        proxied = ytnode.web_args(TOOLS, VIDEO_URL, proxy=TOOLS.proxy)
        self.assertEqual(proxied[proxied.index("--proxy") + 1], TOOLS.proxy)
        self.assertNotIn("--cookies", proxied)

    def test_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(ytnode.web_settings(folder, "youku.com"), ("", False))
            os.makedirs(os.path.join(folder, "cookies"))
            Path(folder, "cookies", "youku.com.txt").write_text("x")
            Path(folder, "sites.json").write_text(json.dumps({"youku.com": {"proxy": "youtube"}}))
            self.assertEqual(ytnode.web_settings(folder, "youku.com"),
                             (os.path.join(folder, "cookies", "youku.com.txt"), True))
            self.assertEqual(ytnode.web_settings(folder, "qq.com"), ("", False))
            self.assertEqual(ytnode.web_settings(None, "youku.com"), ("", False))

    def test_errors(self):
        self.assertEqual(ytnode.web_error("ERROR: [Foo] x: This video is DRM protected"), (422, "drm"))
        self.assertEqual(ytnode.web_error("ERROR: Unsupported URL: https://x"), (400, "unsupported"))
        self.assertEqual(ytnode.web_error("ERROR: [Youku] x: This video is only available for VIP"), (401, "login"))
        self.assertEqual(ytnode.web_error("ERROR: The uploader has not made this video available in your country"),
                         (451, "geo"))
        self.assertEqual(ytnode.web_error("ERROR: HTTP Error 404: Not Found"), (404, "gone"))
        self.assertEqual(ytnode.web_error("ERROR: [dailymotion] x2iuewm: Not found."), (404, "gone"))
        self.assertEqual(ytnode.web_error("ERROR: something odd"), (502, "error"))
        self.assertEqual(ytnode.hide_urls("ERROR: bad https://a.com/x?k=1 here"), "ERROR: bad <地址> here")

    def test_summaries(self):
        video = ytnode.web_summary(VIDEO, VIDEO_URL)
        self.assertEqual(video, {"kind": "video", "id": VIDEO_URL, "title": "A clip", "duration": 61,
                                 "thumb": "https://img.example/t.jpg", "uploader": "Someone", "site": "Dailymotion"})
        listing = ytnode.web_summary(LISTING, LIST_URL)
        self.assertEqual(listing["kind"], "list")
        self.assertEqual([e["url"] for e in listing["entries"]], ["https://www.dailymotion.com/video/x1"],
                         "local and url-less entries are dropped")
        self.assertEqual(listing["entries"][0]["thumb"], "https://img.example/1.jpg")

    def test_slim_keeps_hls_and_drops_drm(self):
        slim = ytnode.web_slim(VIDEO)
        self.assertEqual([f["format_id"] for f in slim["formats"]], ["hls-480", "http-720"])
        self.assertEqual(slim["formats"][0]["headers"], {"User-Agent": "UA"})
        answer = ytnode.web_formats_summary(slim)
        self.assertEqual(answer["format_id"], "hls-480")
        self.assertEqual(len(answer["formats"]), 2)

    def test_direct_env(self):
        env = ytnode.direct_env({"PATH": "/bin", "http_proxy": "x", "HTTPS_PROXY": "y", "ALL_PROXY": "z"})
        self.assertEqual(env, {"PATH": "/bin"})


class FakeProc:
    def __init__(self, data, code=0):
        self.stdout = io.BytesIO(data)
        self.code = code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        pass


class WebRouteTests(unittest.TestCase):
    def setUp(self):
        self.runs, self.popens = [], []
        self.folder = tempfile.TemporaryDirectory()
        self.answers = {VIDEO_URL: (0, json.dumps(VIDEO), ""), LIST_URL: (0, json.dumps(LISTING), "")}

        def run(args):
            self.runs.append(args)
            return self.answers.get(args[-1], (1, "", "ERROR: Unsupported URL: " + args[-1]))

        def popen(args, env=None):
            self.popens.append((args, env))
            return FakeProc(b"\x47" * 500)

        self.logs = []
        self.node = ytnode.Node(TOOLS, KEY, run=run, popen=popen, log=self.logs.append, web_dir=self.folder.name)
        self.node.ffmpeg_env = {"http_proxy": "http://127.0.0.1:7890"}
        self.server = ytnode.make_server(self.node, "127.0.0.1", 0, context=None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def get(self, path, **params):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", path + "?" + urllib.parse.urlencode(params), headers={"X-Key": KEY})
        answer = connection.getresponse()
        return answer.status, answer.read()

    def test_info_video_then_cached_formats(self):
        status, body = self.get("/web/info", url=VIDEO_URL)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["title"], "A clip")
        status, body = self.get("/web/formats", url=VIDEO_URL)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["format_id"], "hls-480")
        self.assertEqual(len(self.runs), 1, "formats reuse the cached resolve")
        self.get("/web/formats", url=VIDEO_URL, fresh=1)
        self.assertEqual(len(self.runs), 2)

    def test_info_list(self):
        status, body = self.get("/web/info", url=LIST_URL)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["kind"], "list")
        status, body = self.get("/web/formats", url=LIST_URL)
        self.assertEqual(status, 400)

    def test_refusals(self):
        status, body = self.get("/web/info", url="http://192.168.1.5/video")
        self.assertEqual((status, json.loads(body)["kind"]), (400, "unsupported"))
        self.assertEqual(self.runs, [], "a local address never reaches yt-dlp")
        status, body = self.get("/web/info", url="https://example.com/page")
        self.assertEqual((status, json.loads(body)["kind"]), (400, "unsupported"))
        self.assertNotIn("example.com/page", " ".join(self.logs), "the log carries the site, never the address")
        self.assertIn("example.com", " ".join(self.logs))
        self.assertEqual(self.node.health["state"], "unknown", "websites do not change YouTube's health")

    def test_drm(self):
        self.answers[VIDEO_URL] = (1, "", "ERROR: [Foo] x8abcd: This video is DRM protected")
        status, body = self.get("/web/info", url=VIDEO_URL)
        self.assertEqual((status, json.loads(body)["kind"]), (422, "drm"))

    def test_no_web_dir(self):
        self.node.web_dir = None
        status, _ = self.get("/web/info", url=VIDEO_URL)
        self.assertEqual(status, 404)

    def test_stream_direct_and_proxied(self):
        status, body = self.get("/web/stream", url=VIDEO_URL, ss=30)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 500)
        args, env = self.popens[-1]
        self.assertIn("https://cdn.example/480.m3u8", args)
        self.assertNotIn("http_proxy", env, "a direct site's ffmpeg does not use the proxy")
        Path(self.folder.name, "sites.json").write_text(json.dumps({"dailymotion.com": {"proxy": "youtube"}}))
        self.get("/web/stream", url=VIDEO_URL, ss=0)
        self.assertEqual(self.popens[-1][1], {"http_proxy": "http://127.0.0.1:7890"})
        self.get("/web/formats", url=VIDEO_URL, fresh=1)
        self.assertEqual(self.runs[-1][self.runs[-1].index("--proxy") + 1], TOOLS.proxy)

    def test_cookies_per_site(self):
        os.makedirs(os.path.join(self.folder.name, "cookies"))
        Path(self.folder.name, "cookies", "dailymotion.com.txt").write_text("x")
        self.get("/web/info", url=VIDEO_URL)
        args = self.runs[-1]
        self.assertEqual(args[args.index("--cookies") + 1],
                         os.path.join(self.folder.name, "cookies", "dailymotion.com.txt"))


if __name__ == "__main__":
    unittest.main()
