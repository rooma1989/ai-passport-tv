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

INFO = {"id": "jNQXAC9IVRw", "title": "Me at the zoo", "duration": 19, "_version": {"version": "2026.08.19"},
        "channel_url": "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A",
        "requested_formats": [
            {"url": "https://v.example/video", "http_headers": {"User-Agent": "UA", "Accept": "*/*"}},
            {"url": "https://v.example/audio", "http_headers": {"User-Agent": "UA"}}]}
SINGLE = {"id": "jNQXAC9IVRw", "url": "https://v.example/both", "http_headers": {"User-Agent": "UA2"}}
LISTING = {"channel": "Linus Tech Tips", "channel_url": "https://www.youtube.com/channel/UCX",
           "entries": [{"id": "aaaaaaaaaaa", "title": "new", "duration": 600},
                       {"id": "bbbbbbbbbbb", "title": "old"}, {"id": "bad", "title": "x"}]}


# Voice search: what `yt-dlp --flat-playlist -J ytsearchN:…` prints, trimmed: a live stream, an
# upcoming premiere, a Short, a deleted entry and one without a duration among the videos.
SEARCH_LISTING = {"id": "lofi", "title": "lofi", "_type": "playlist", "_version": {"version": "2026.08.19"},
                  "entries": [
    {"_type": "url", "ie_key": "Youtube", "id": "jfKfPfyJRdk", "url": "https://www.youtube.com/watch?v=jfKfPfyJRdk",
     "title": "lofi hip hop radio 📚 beats to relax/study to", "duration": None, "channel": "Lofi Girl",
     "live_status": "is_live"},
    {"_type": "url", "ie_key": "Youtube", "id": "n61ULEU7CO0", "url": "https://www.youtube.com/watch?v=n61ULEU7CO0",
     "title": "Lofi Girl - 1 A.M Study Session", "duration": 3615.0, "channel": "Lofi Girl", "live_status": None},
    {"_type": "url", "ie_key": "Youtube", "id": "upcomingAAA", "url": "https://www.youtube.com/watch?v=upcomingAAA",
     "title": "Premiere", "duration": None, "live_status": "is_upcoming"},
    {"_type": "url", "ie_key": "Youtube", "id": "shortsAAAAA", "url": "https://www.youtube.com/shorts/shortsAAAAA",
     "title": "a Short", "duration": 30},
    {"_type": "url", "ie_key": "Youtube", "id": "postliveAAA", "title": "just ended", "duration": 7200,
     "live_status": "post_live"},
    {"_type": "url", "ie_key": "Youtube", "id": "deletedAAAA", "title": "[Deleted video]", "duration": 60},
    {"_type": "url", "ie_key": "Youtube", "id": "bad", "title": "bad id", "duration": 60},
    {"_type": "url", "ie_key": "Youtube", "id": "4xDzrJKXOOY", "title": "synthwave radio", "duration": 1801,
     "uploader": "Lofi Girl"},
]}
# Channel search (the results page with the channel filter), trimmed.
CHANNEL_LISTING = {"_type": "playlist", "entries": [
    {"_type": "url", "ie_key": "YoutubeTab", "id": "UCSJ4gkVC6NrvII8umztf0Ow",
     "url": "https://www.youtube.com/channel/UCSJ4gkVC6NrvII8umztf0Ow", "title": "Lofi Girl", "channel": "Lofi Girl",
     "channel_id": "UCSJ4gkVC6NrvII8umztf0Ow", "channel_follower_count": 15200000, "playlist_count": None,
     "uploader_id": "@LofiGirl"},
    {"_type": "url", "ie_key": "YoutubeTab", "id": "UCSJ4gkVC6NrvII8umztf0Ow", "title": "Lofi Girl (again)"},
    {"_type": "url", "ie_key": "YoutubeTab", "id": "UCOxqgCwgOqC2lMqC5PYz_Dg", "title": "lofi geek",
     "channel_follower_count": None, "playlist_count": 120},
    {"_type": "url", "ie_key": "Youtube", "id": "n61ULEU7CO0", "title": "a video, not a channel"},
]}


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

    def test_search_args_and_summaries(self):
        args = ytnode.search_args(TOOLS, "-lofi", 5)
        self.assertEqual(args[-2:], ["--", "ytsearch10:-lofi"], "the words come after --")
        self.assertIn("--flat-playlist", args)
        args = ytnode.search_args(TOOLS, "lofi girl", 5, "channel")
        self.assertEqual(args[-2], "--")
        self.assertEqual(args[-1], "https://www.youtube.com/results?search_query=lofi+girl&sp=EgIQAg%3D%3D")
        self.assertEqual(args[args.index("--playlist-end") + 1], "5")
        self.assertEqual(ytnode.search_summary(SEARCH_LISTING, 10), {"videos": [
            {"id": "n61ULEU7CO0", "title": "Lofi Girl - 1 A.M Study Session", "duration": 3615, "uploader": "Lofi Girl"},
            {"id": "4xDzrJKXOOY", "title": "synthwave radio", "duration": 1801, "uploader": "Lofi Girl"}]})
        self.assertEqual(len(ytnode.search_summary(SEARCH_LISTING, 1)["videos"]), 1)
        self.assertEqual(ytnode.channel_summary(CHANNEL_LISTING, 10), {"channels": [
            {"id": "UCSJ4gkVC6NrvII8umztf0Ow", "name": "Lofi Girl", "fans": 15200000, "videos": 0},
            {"id": "UCOxqgCwgOqC2lMqC5PYz_Dg", "name": "lofi geek", "fans": 0, "videos": 120}]})

    def test_clean_query(self):
        self.assertEqual(ytnode.clean_query("  lofi   girl "), "lofi girl")
        for bad in ("", "   ", "x" * 81, "a\x01b", "a\x7fb"):
            self.assertIsNone(ytnode.clean_query(bad), repr(bad))

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

        self.bot = False

        def run(args):
            self.runs.append(args)
            if self.bot:
                return 1, "", "ERROR: [youtube] x: Sign in to confirm you’re not a bot."
            if "--flat-playlist" in args:
                return 0, json.dumps(LISTING), ""
            if args[-1].endswith("v=gonegonegon"):
                return 1, "", "ERROR: [youtube] gonegonegon: Video unavailable"
            if args[-1].endswith("v=brokenbroke"):
                return 1, "", "ERROR: something nobody expected"
            return 0, json.dumps(INFO), ""

        def popen(args):
            self.runs.append(args)
            return FakeProc(b"\x47" * 1000, self.stream_code)

        node = ytnode.Node(TOOLS, KEY, run=run, popen=popen, log=lambda _m: None)
        self.node = node
        self.server = ytnode.make_server(node, "127.0.0.1", 0, context=None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, key=KEY):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("GET", path, headers={"X-Key": key} if key else {})
        return connection.getresponse()

    def search_runs(self):
        def run(args):
            self.runs.append(args)
            if args[-1].startswith("ytsearch"):
                return 0, json.dumps(SEARCH_LISTING), ""
            if "/results?" in args[-1]:
                return 0, json.dumps(CHANNEL_LISTING), ""
            raise AssertionError(args)
        self.node.run = run

    def test_search(self):
        self.search_runs()
        answer = self.get("/yt/search?q=lofi&n=5")
        self.assertEqual(answer.status, 200)
        self.assertEqual([v["id"] for v in json.loads(answer.read())["videos"]], ["n61ULEU7CO0", "4xDzrJKXOOY"])
        self.assertEqual(self.runs[-1][-1], "ytsearch10:lofi")
        answer = self.get("/yt/search?q=lofi%20girl&n=5&kind=channel")
        self.assertEqual([c["name"] for c in json.loads(answer.read())["channels"]], ["Lofi Girl", "lofi geek"])
        self.assertEqual(self.node.health["state"], "ok")

    def test_search_is_cached(self):
        self.search_runs()
        for _ in range(3):
            self.get("/yt/search?q=Lofi&n=5").read()
        self.get("/yt/search?q=lofi&n=5").read()
        self.assertEqual(len(self.runs), 1, "the same words within ten minutes: no new yt-dlp")
        self.get("/yt/search?q=lofi&n=5&kind=channel").read()
        self.assertEqual(len(self.runs), 2)
        self.node.clock = lambda: 1e12
        self.get("/yt/search?q=lofi&n=5").read()
        self.assertEqual(len(self.runs), 3, "an old answer is asked again")

    def test_search_does_not_queue_behind_a_resolve(self):
        self.search_runs()
        saved, ytnode.SEARCH_WAIT_S = ytnode.SEARCH_WAIT_S, 0.1
        try:
            with self.node._ytdlp:
                answer = self.get("/yt/search?q=lofi&n=5")
                self.assertEqual((answer.status, json.loads(answer.read())), (503, {"error": "busy"}))
        finally:
            ytnode.SEARCH_WAIT_S = saved
        self.assertEqual(self.runs, [])

    def test_bad_search_never_reaches_yt_dlp(self):
        for query in ("q=&n=5", "q=lofi&n=0", "q=lofi&n=21", "q=lofi&n=x", "q=lofi&n=5&kind=playlist",
                      "q=" + "x" * 81 + "&n=5", "n=5"):
            self.assertEqual(self.get(f"/yt/search?{query}").status, 400, query)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.get("/yt/search?q=lofi&n=5", key="wrong").status, 404)

    def test_search_failure_passes_on(self):
        self.bot = True
        answer = self.get("/yt/search?q=lofi&n=5")
        self.assertEqual(answer.status, 503)

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

    def test_video_info_is_cached(self):
        """Resolving takes about 18 s on cstone; a channel start connects twice."""
        self.get("/yt/info?id=jNQXAC9IVRw").read()
        self.get("/yt/stream?id=jNQXAC9IVRw&ss=0").read()
        self.get("/yt/stream?id=jNQXAC9IVRw&ss=10").read()
        resolves = [args for args in self.runs if args[0] == TOOLS.ytdlp]
        self.assertEqual(len(resolves), 1)

    def test_concurrent_starts_resolve_once_and_never_overlap(self):
        """Two yt-dlp (each with a deno) at once overran the 400 MB unit and were OOM-killed."""
        running, overlap = [0], [False]
        gate = threading.Lock()
        plain = self.node.run

        def slow_run(args):
            with gate:
                running[0] += 1
                overlap[0] = overlap[0] or running[0] > 1
            threading.Event().wait(0.2)
            try:
                return plain(args)
            finally:
                with gate:
                    running[0] -= 1

        self.node.run = slow_run
        answers = []
        threads = [threading.Thread(target=lambda path=path: answers.append(self.get(path).status))
                   for path in ("/yt/info?id=jNQXAC9IVRw", "/yt/info?id=jNQXAC9IVRw",
                                "/yt/list?channel=https%3A%2F%2Fwww.youtube.com%2F%40LinusTechTips")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(answers, [200, 200, 200])
        self.assertFalse(overlap[0])
        resolves = [args for args in self.runs if args[0] == TOOLS.ytdlp and "--flat-playlist" not in args]
        self.assertEqual(len(resolves), 1)

    def test_playlist(self):
        answer = self.get("/yt/playlist?list=PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw")
        self.assertEqual(answer.status, 200)
        data = json.loads(answer.read())
        self.assertEqual([v["id"] for v in data["videos"]], ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertTrue(self.runs[-1][-1].endswith("playlist?list=PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw"))
        self.assertIn(str(ytnode.PLAYLIST_SIZE), self.runs[-1])
        for bad in ("--exec", "PLx", "PL%26x%3D1aaaaaaaaaa"):
            self.assertEqual(self.get(f"/yt/playlist?list={bad}").status, 400, bad)

    def test_playlist_drops_deleted_entries(self):
        listing = {"title": "灌籃高手", "entries": [
            {"id": "aaaaaaaaaaa", "title": "第1集", "duration": 1400},
            {"id": "bbbbbbbbbbb", "title": "[Deleted video]", "duration": None},
            {"id": "ccccccccccc", "title": "[Private video]"}]}
        self.assertEqual(ytnode.playlist_summary(listing),
                         {"name": "灌籃高手", "videos": [{"id": "aaaaaaaaaaa", "title": "第1集", "duration": 1400}]})

    def health(self):
        answer = self.get("/yt/health")
        self.assertEqual(answer.status, 200)
        return json.loads(answer.read())

    def test_health_follows_the_last_resolve(self):
        self.assertEqual(self.health()["state"], "unknown")
        self.assertEqual(self.get("/yt/health", key=None).status, 404)
        self.get("/yt/info?id=jNQXAC9IVRw").read()
        health = self.health()
        self.assertEqual((health["state"], health["version"]), ("ok", "2026.08.19"))
        self.bot = True
        self.get("/yt/info?id=bbbbbbbbbbb").read()
        self.assertEqual(self.health()["state"], "login")
        self.bot = False
        self.get("/yt/info?id=brokenbroke").read()
        self.assertEqual(self.health()["state"], "error")
        # A missing video is YouTube answering normally: the cookies are fine.
        self.get("/yt/info?id=gonegonegon").read()
        self.assertEqual(self.health()["state"], "ok")

    def test_background_check_skips_the_cache(self):
        self.get("/yt/info?id=jNQXAC9IVRw").read()
        self.bot = True
        self.node.check()
        self.assertEqual(self.health()["state"], "login")

    def test_stream_failure_is_cut(self):
        self.stream_code = 1
        answer = self.get("/yt/stream?id=jNQXAC9IVRw&ss=0")
        with self.assertRaises(http.client.IncompleteRead):
            answer.read()


if __name__ == "__main__":
    unittest.main()
