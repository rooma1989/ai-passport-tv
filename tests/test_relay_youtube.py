"""YouTube on the Aliyun side: links, the cstone client, and the stream wrapper.

YouTube itself is never contacted from Aliyun. The client talks to the node on
cstone, so these tests fake that node's JSON answers and its byte stream.
"""

import http.client
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.relay import youtube
from server.relay.bilibili import BilibiliError, SourceError
from server.relay.youtube import YouTube


class LinkTests(unittest.TestCase):
    def test_channel_links(self):
        cases = {
            "https://www.youtube.com/@LinusTechTips": "https://www.youtube.com/@LinusTechTips",
            "https://youtube.com/@LinusTechTips/videos?x=1": "https://www.youtube.com/@LinusTechTips",
            "m.youtube.com/@老高與小茉": "https://www.youtube.com/@老高與小茉",
            "https://www.youtube.com/channel/UCXuqSBlHAE6Xw-yeJA0Tunw":
                "https://www.youtube.com/channel/UCXuqSBlHAE6Xw-yeJA0Tunw",
            "看这个频道 https://www.youtube.com/c/LinusTechTips/featured":
                "https://www.youtube.com/c/LinusTechTips",
            "https://www.youtube.com/user/LinusTechTips": "https://www.youtube.com/user/LinusTechTips",
        }
        for text, expected in cases.items():
            self.assertEqual(youtube.parse_channel_link(text), expected, text)

    def test_channel_link_rejected(self):
        for text in ["https://www.youtube.com.evil.cn/@x", "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                     "https://www.youtube.com/@", "https://space.bilibili.com/1", ""]:
            self.assertIsNone(youtube.parse_channel_link(text), text)

    def test_video_links(self):
        for text in ["https://www.youtube.com/watch?v=jNQXAC9IVRw",
                     "https://m.youtube.com/watch?feature=share&v=jNQXAC9IVRw",
                     "https://youtu.be/jNQXAC9IVRw?si=abc",
                     "https://www.youtube.com/shorts/jNQXAC9IVRw",
                     "https://www.youtube.com/live/jNQXAC9IVRw",
                     "分享 https://youtu.be/jNQXAC9IVRw"]:
            self.assertEqual(youtube.parse_video_link(text), "jNQXAC9IVRw", text)
        for text in ["https://youtu.be/short", "https://www.youtube.com/watch?v=",
                     "https://evil.cn/watch?v=jNQXAC9IVRw"]:
            self.assertIsNone(youtube.parse_video_link(text), text)

    def test_is_youtube(self):
        self.assertTrue(youtube.is_youtube("https://youtu.be/jNQXAC9IVRw"))
        self.assertTrue(youtube.is_youtube("标题 https://www.youtube.com/@x"))
        self.assertFalse(youtube.is_youtube("https://www.bilibili.com/video/BV1aZpw6DEKJ"))


LIST = {"name": "Linus Tech Tips", "channel_url": "https://www.youtube.com/channel/UCX",
        "videos": [{"id": "aaaaaaaaaaa", "title": "new", "duration": 600},
                   {"id": "bbbbbbbbbbb", "title": "old", "duration": None}]}
INFO = {"id": "jNQXAC9IVRw", "title": "Me at the zoo", "duration": 19,
        "channel_url": "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A"}


class ClientTests(unittest.TestCase):
    def client(self, answers):
        self.calls = []

        def fetch(path, params):
            self.calls.append((path, params))
            answer = answers[path]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return YouTube(fetch_json=fetch)

    def test_creator_and_uploads(self):
        client = self.client({"/yt/list": LIST})
        name, videos = client.creator("https://www.youtube.com/@LinusTechTips")
        self.assertEqual(name, "Linus Tech Tips")
        self.assertEqual([(v.bvid, v.site, v.duration) for v in videos],
                         [("aaaaaaaaaaa", "youtube", 600), ("bbbbbbbbbbb", "youtube", 0)])
        self.assertEqual(self.calls, [("/yt/list", {"channel": "https://www.youtube.com/@LinusTechTips"})])

    def test_creator_id_from_video_link_uses_its_channel(self):
        client = self.client({"/yt/info": INFO})
        self.assertEqual(client.creator_id("https://youtu.be/jNQXAC9IVRw"),
                         "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A")
        self.assertEqual(client.creator_id("https://www.youtube.com/@x"), "https://www.youtube.com/@x")
        self.assertIsNone(client.creator_id("https://example.com/"))

    def test_video(self):
        client = self.client({"/yt/info": INFO})
        self.assertEqual(client.video_id("https://youtu.be/jNQXAC9IVRw"), "jNQXAC9IVRw")
        video = client.video("jNQXAC9IVRw")
        self.assertEqual((video.title, video.duration, video.site), ("Me at the zoo", 19, "youtube"))

    def test_errors(self):
        client = self.client({"/yt/list": OSError("down")})
        with self.assertRaises(BilibiliError) as caught:
            client.uploads("https://www.youtube.com/@x")
        self.assertFalse(caught.exception.permanent)
        client = self.client({"/yt/info": youtube.NodeError(404, "gone")})
        with self.assertRaises(BilibiliError) as caught:
            client.video("jNQXAC9IVRw")
        self.assertTrue(caught.exception.permanent)

    def test_playlist_links(self):
        for text in ["https://www.youtube.com/playlist?list=PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw",
                     "https://m.youtube.com/watch?v=jNQXAC9IVRw&list=PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw&index=3",
                     "看这个 https://youtube.com/playlist?list=PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw&si=x"]:
            self.assertEqual(youtube.parse_playlist_link(text), "PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw", text)
        for text in ["https://evil.cn/playlist?list=PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw",
                     "https://www.youtube.com/playlist?list=--exec", "https://youtu.be/jNQXAC9IVRw",
                     "https://www.youtube.com/watch?v=jNQXAC9IVRw"]:
            self.assertIsNone(youtube.parse_playlist_link(text), text)

    def test_playlist_series(self):
        client = self.client({"/yt/playlist": {"name": "灌籃高手", "videos": [
            {"id": "aaaaaaaaaaa", "title": "第1集", "duration": 1400},
            {"id": "bbbbbbbbbbb", "title": "第2集", "duration": 1390}, {"id": "bad"}]}})
        name, videos = client.series("PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw")
        self.assertEqual(name, "灌籃高手")
        self.assertEqual([(v.bvid, v.site) for v in videos], [("aaaaaaaaaaa", "youtube"), ("bbbbbbbbbbb", "youtube")])
        self.assertEqual(self.calls, [("/yt/playlist", {"list": "PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw"})])
        client = self.client({"/yt/playlist": {"name": "x", "videos": [{"id": "aaaaaaaaaaa"}]}})
        with self.assertRaises(BilibiliError) as caught:
            client.series("PLvWg2OZGt8Huee65hzUZ4Md3vMKZMnTYw")
        self.assertTrue(caught.exception.permanent)

    def test_health(self):
        client = self.client({"/yt/health": {"state": "login", "at": 5.0, "version": "v"}})
        self.assertEqual(client.health()["state"], "login")
        client = self.client({"/yt/health": OSError("down")})
        with self.assertRaises(BilibiliError):
            client.health()

    def test_not_configured(self):
        client = YouTube(fetch_json=None, config=None)
        with self.assertRaises(BilibiliError):
            client.video("jNQXAC9IVRw")


class FakeResponse:
    def __init__(self, chunks, cut=False):
        self.chunks = list(chunks)
        self.cut = cut
        self.closed = False

    def read1(self, _size=-1):
        if self.chunks:
            return self.chunks.pop(0)
        if self.cut:
            raise http.client.IncompleteRead(b"")
        return b""

    def close(self):
        self.closed = True


class StreamProcTests(unittest.TestCase):
    def drain(self, proc):
        out = bytearray()
        while chunk := proc.stdout.read1(65536):
            out += chunk
        return bytes(out)

    def test_clean_end(self):
        proc = youtube.StreamProc(FakeResponse([b"ab", b"cd"]))
        self.assertEqual(self.drain(proc), b"abcd")
        self.assertEqual(proc.wait(), 0)
        proc.kill()

    def test_cut_stream_is_a_failure(self):
        proc = youtube.StreamProc(FakeResponse([b"ab"], cut=True))
        self.assertEqual(self.drain(proc), b"ab")
        self.assertEqual(proc.wait(), 1)

    def test_kill_closes(self):
        response = FakeResponse([b"ab"])
        proc = youtube.StreamProc(response)
        proc.kill()
        self.assertTrue(response.closed)


class SearchTests(unittest.TestCase):
    def client(self, answer):
        self.calls = []

        def fetch(path, params, timeout=None):
            self.calls.append((path, params, timeout))
            if isinstance(answer, Exception):
                raise answer
            return answer
        return YouTube(fetch_json=fetch)

    def test_videos(self):
        client = self.client({"videos": [
            {"id": "n61ULEU7CO0", "title": " Lofi  Girl ", "duration": 3615, "uploader": "Lofi Girl"},
            {"id": "bad", "title": "x", "duration": 5}, {"id": "4xDzrJKXOOY", "title": "", "duration": 5},
            {"id": "4xDzrJKXOOZ", "title": "no duration", "duration": None}, "junk"]})
        self.assertEqual(client.search("lofi", timeout=2.5), [
            {"id": "n61ULEU7CO0", "title": "Lofi Girl", "uploader": "Lofi Girl", "duration": 3615}])
        self.assertEqual(self.calls, [("/yt/search", {"q": "lofi", "n": youtube.SEARCH_MAX}, 2.5)])

    def test_channels(self):
        client = self.client({"channels": [
            {"id": "UCSJ4gkVC6NrvII8umztf0Ow", "name": "Lofi Girl", "fans": 15200000, "videos": 0},
            {"id": "UCbad", "name": "x"}, {"id": "UCOxqgCwgOqC2lMqC5PYz_Dg", "name": "", "fans": 1}]})
        self.assertEqual(client.search_creators("lofi girl", timeout=3), [
            {"id": "UCSJ4gkVC6NrvII8umztf0Ow", "name": "Lofi Girl", "fans": 15200000, "videos": 0,
             "latest": None}])
        self.assertEqual(self.calls[0][1]["kind"], "channel")
        self.assertEqual(youtube.channel_url("UCSJ4gkVC6NrvII8umztf0Ow"),
                         "https://www.youtube.com/channel/UCSJ4gkVC6NrvII8umztf0Ow")

    def test_reasons(self):
        for error, reason in ((youtube.NodeError(503, '{"error": "busy"}'), "busy"),
                              (youtube.NodeError(504, '{"error": "yt-dlp timed out"}'), "slow"),
                              (youtube.NodeError(503, '{"error": "Sign in to confirm"}'), ""),
                              (TimeoutError("timed out"), "slow"), (OSError("refused"), "")):
            with self.subTest(error=error):
                with self.assertRaises(SourceError) as caught:
                    self.client(error).search("lofi")
                self.assertEqual(caught.exception.reason, reason)


if __name__ == "__main__":
    unittest.main()
