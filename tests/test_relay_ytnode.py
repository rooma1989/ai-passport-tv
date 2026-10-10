"""The YouTube node that runs on cstone: commands it builds and what it answers.

yt-dlp and ffmpeg are replaced by fakes; the HTTP side runs over a real socket
(without TLS, which the node adds only when it is given a certificate).
"""

import http.client
import io
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import ytnode

KEY = "node-key-0123456789"
TOOLS = ytnode.Tools(ytdlp="/opt/yt-relay/yt-dlp", deno="/opt/yt-relay/deno",
                     ffmpeg="/usr/bin/ffmpeg", cookies="/var/lib/yt-relay/cookies.txt")

INFO = {"id": "jNQXAC9IVRw", "title": "Me at the zoo", "duration": 19,
        "channel_url": "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A",
        "requested_formats": [
            {"url": "https://v.example/video", "http_headers": {"User-Agent": "UA", "Accept": "*/*"}},
            {"url": "https://v.example/audio", "http_headers": {"User-Agent": "UA"}}]}
SINGLE = {"id": "jNQXAC9IVRw", "url": "https://v.example/both", "http_headers": {"User-Agent": "UA2"}}
LISTING = {"channel": "Linus Tech Tips", "channel_url": "https://www.youtube.com/channel/UCX",
           "entries": [{"id": "aaaaaaaaaaa", "title": "new", "duration": 600},
                       {"id": "bbbbbbbbbbb", "title": "old"}, {"id": "bad", "title": "x"}]}


class CommandTests(unittest.TestCase):
    def test_channel_validation(self):
        for ok in ["https://www.youtube.com/@LinusTechTips", "https://www.youtube.com/@老高與小茉",
                   "https://www.youtube.com/channel/UCX-y_z", "https://www.youtube.com/c/Name"]:
            self.assertTrue(ytnode.valid_channel(ok), ok)
        for bad in ["https://evil.cn/@x", "https://www.youtube.com/@x/videos", "--exec=rm",
                    "https://www.youtube.com/watch?v=jNQXAC9IVRw", "https://www.youtube.com/@a b"]:
            self.assertFalse(ytnode.valid_channel(bad), bad)

    def test_list_args(self):
        args = ytnode.list_args(TOOLS, "https://www.youtube.com/@LinusTechTips")
        self.assertEqual(args[0], "/opt/yt-relay/yt-dlp")
        self.assertIn("deno:/opt/yt-relay/deno", args)
        self.assertEqual(args[args.index("--cookies") + 1], "/var/lib/yt-relay/cookies.txt")
        self.assertEqual(args[args.index("--playlist-end") + 1], "20")
        self.assertEqual(args[-2:], ["--", "https://www.youtube.com/@LinusTechTips/videos"])

    def test_no_cookies_flag_without_a_file(self):
        tools = ytnode.Tools(ytdlp="y", deno="d", ffmpeg="f", cookies="")
        self.assertNotIn("--cookies", ytnode.info_args(tools, "jNQXAC9IVRw"))

    def test_info_and_listing_parsed(self):
        self.assertEqual(ytnode.info_summary(INFO), {
            "id": "jNQXAC9IVRw", "title": "Me at the zoo", "duration": 19,
            "channel_url": "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A"})
        self.assertEqual(ytnode.listing_summary(LISTING), {
            "name": "Linus Tech Tips", "channel_url": "https://www.youtube.com/channel/UCX",
            "videos": [{"id": "aaaaaaaaaaa", "title": "new", "duration": 600},
                       {"id": "bbbbbbbbbbb", "title": "old", "duration": 0}]})

    def test_inputs_from_formats(self):
        self.assertEqual(ytnode.stream_inputs(INFO),
                         [("https://v.example/video", {"User-Agent": "UA", "Accept": "*/*"}),
                          ("https://v.example/audio", {"User-Agent": "UA"})])
        self.assertEqual(ytnode.stream_inputs(SINGLE), [("https://v.example/both", {"User-Agent": "UA2"})])
        self.assertEqual(ytnode.stream_inputs({"id": "x"}), [])

    def test_ffmpeg_args(self):
        two = ytnode.ffmpeg_args(TOOLS, ytnode.stream_inputs(INFO), 65.9)
        self.assertEqual(two.count("-i"), 2)
        self.assertEqual(two.count("-ss"), 2)
        self.assertEqual(two[two.index("-ss") + 1], "65")
        self.assertIn("1:a:0", two)
        self.assertEqual(two[two.index("-user_agent") + 1], "UA")
        self.assertIn("Accept: */*\r\n", two)
        self.assertEqual(two[-3:], ["-f", "mpegts", "pipe:1"])
        one = ytnode.ffmpeg_args(TOOLS, ytnode.stream_inputs(SINGLE), 0)
        self.assertEqual(one.count("-i"), 1)
        self.assertIn("0:a:0?", one)

    def test_error_classes(self):
        self.assertEqual(ytnode.error_status("ERROR: [youtube] x: Video unavailable"), 404)
        self.assertEqual(ytnode.error_status("ERROR: [youtube] x: Private video"), 404)
        self.assertEqual(ytnode.error_status("ERROR: Sign in to confirm you’re not a bot"), 503)
        self.assertEqual(ytnode.error_status("something else"), 502)


class FakeProc:
    def __init__(self, data, code):
        self.stdout = io.BytesIO(data)
        self.code = code
        self.returncode = None

    def wait(self, timeout=None):
        self.returncode = self.code
        return self.code

    def kill(self):
        pass


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.runs = []
        self.stream_code = 0

        def run(args):
            self.runs.append(args)
            if "--flat-playlist" in args:
                return 0, json.dumps(LISTING), ""
            if args[-1].endswith("v=gonegonegon"):
                return 1, "", "ERROR: [youtube] gonegonegon: Video unavailable"
            return 0, json.dumps(INFO), ""

        def popen(args):
            self.runs.append(args)
            return FakeProc(b"\x47" * 1000, self.stream_code)

        node = ytnode.Node(TOOLS, KEY, run=run, popen=popen, log=lambda _m: None)
        self.server = ytnode.make_server(node, "127.0.0.1", 0, context=None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, key=KEY):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", path, headers={"X-Key": key} if key else {})
        return connection.getresponse()

    def test_key_required(self):
        self.assertEqual(self.get("/yt/info?id=jNQXAC9IVRw", key=None).status, 404)
        self.assertEqual(self.get("/yt/info?id=jNQXAC9IVRw", key="wrong").status, 404)
        self.assertEqual(self.runs, [])

    def test_info_and_list(self):
        answer = self.get("/yt/info?id=jNQXAC9IVRw")
        self.assertEqual(answer.status, 200)
        self.assertEqual(json.loads(answer.read())["title"], "Me at the zoo")
        answer = self.get("/yt/list?channel=https%3A%2F%2Fwww.youtube.com%2F%40LinusTechTips")
        self.assertEqual(len(json.loads(answer.read())["videos"]), 2)

    def test_bad_parameters_never_reach_yt_dlp(self):
        self.assertEqual(self.get("/yt/info?id=--exec").status, 400)
        self.assertEqual(self.get("/yt/list?channel=--exec%3Dx").status, 400)
        self.assertEqual(self.get("/yt/stream?id=jNQXAC9IVRw&ss=abc").status, 400)
        self.assertEqual(self.runs, [])

    def test_unavailable_is_404(self):
        self.assertEqual(self.get("/yt/info?id=gonegonegon").status, 404)

    def test_stream_clean_end(self):
        answer = self.get("/yt/stream?id=jNQXAC9IVRw&ss=30")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.read(), b"\x47" * 1000)
        self.assertIn("30", self.runs[-1])

    def test_stream_failure_is_cut(self):
        self.stream_code = 1
        answer = self.get("/yt/stream?id=jNQXAC9IVRw&ss=0")
        with self.assertRaises(http.client.IncompleteRead):
            answer.read()


if __name__ == "__main__":
    unittest.main()
