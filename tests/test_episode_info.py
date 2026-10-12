"""What the device is told about the episode playing or loading (server/episode_info.py)."""

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import episode_info


class EpisodeInfoTests(unittest.TestCase):
    def test_info_url_only_for_relay_streams(self):
        self.assertEqual(episode_info.info_url("http://127.0.0.1:8099/ch/sr01"),
                         "http://127.0.0.1:8099/info/sr01")
        self.assertEqual(episode_info.info_url("http://127.0.0.1:8099/ch/0123456789ab/up02"),
                         "http://127.0.0.1:8099/info/0123456789ab/up02")
        self.assertEqual(episode_info.info_url("http://127.0.0.1:8099/ch/legacy-passport/cast"),
                         "http://127.0.0.1:8099/info/legacy-passport/cast")
        for url in ("https://example.com/live/ch/sr01", "http://10.0.0.2:8099/ch/sr01",
                    "http://127.0.0.1:8099/ch/ch001", "http://127.0.0.1:8099/ch/sr01/x", "", None):
            self.assertIsNone(episode_info.info_url(url), url)

    def test_title_is_cut_on_a_character(self):
        clipped = episode_info.clip_utf8("犬" * 30)
        self.assertEqual(clipped, "犬" * 20)
        self.assertEqual(episode_info.clip_utf8("ab" * 40), "ab" * 30)

    def test_payload(self):
        self.assertEqual(episode_info.payload("第3集  战国\n", 3, 26),
                         {"title": "第3集 战国", "episode": 3, "total": 26})
        self.assertEqual(episode_info.payload("A video"), {"title": "A video"})
        self.assertEqual(episode_info.payload("x", 5, 3), {"title": "x", "episode": 5}, "a total below it is wrong")
        self.assertEqual(episode_info.payload("x", True, -1), {"title": "x"})
        self.assertIsNone(episode_info.payload("", 0, 0))
        self.assertEqual(episode_info.payload("", 2, 0), {"title": "", "episode": 2})

    def test_fetch(self):
        def opener(url, timeout):
            self.assertEqual(timeout, episode_info.TIMEOUT_S)
            return io.BytesIO(json.dumps({"channel": "sr01", "title": "t", "episode": 1, "total": 2}).encode())
        self.assertEqual(episode_info.fetch("http://127.0.0.1:1/info/sr01", opener=opener),
                         {"title": "t", "episode": 1, "total": 2})

        def broken(url, timeout):
            raise OSError("refused")
        self.assertIsNone(episode_info.fetch("http://127.0.0.1:1/info/sr01", opener=broken))
        self.assertIsNone(episode_info.fetch("http://127.0.0.1:1/info/sr01",
                                             opener=lambda u, timeout: io.BytesIO(b"not json")))
        self.assertIsNone(episode_info.fetch(None))


if __name__ == "__main__":
    unittest.main()
