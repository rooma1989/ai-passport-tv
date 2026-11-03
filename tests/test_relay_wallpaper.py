"""Home page wallpapers from GitHub's dharmx/walls (server/relay/wallpaper.py).

GitHub is never contacted: the git trees answer has the shape of GET /repos/{owner}/{repo}/git/trees/{branch}
?recursive=1, and the images are a few hundred bytes of real PNG / JPEG / WebP headers followed by padding.
"""

from __future__ import annotations

import json
import os
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import wallpaper
from server.relay.wallpaper import Walls, image_size, nice_name, tree_files, usable

from test_relay_bililogin import RelayHarness

KB = 1024


def png(w, h, tag=""):
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h)
            + b"\x08\x02\x00\x00\x00" + tag.encode() + b"\0" * 64)


def jpeg(w, h, tag=""):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
    exif = b"\xff\xe1" + struct.pack(">H", 2 + 300) + b"x" * 300
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + exif + sof + tag.encode() + b"\0" * 64


def webp_vp8x(w, h):
    body = b"VP8X" + struct.pack("<I", 10) + b"\0\0\0\0" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def webp_vp8l(w, h):
    bits = (w - 1) | ((h - 1) << 14)
    body = b"VP8L" + struct.pack("<I", 13) + b"\x2f" + bits.to_bytes(4, "little") + b"\0" * 8
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def webp_vp8(w, h):
    body = b"VP8 " + struct.pack("<I", 10) + b"\0\0\0" + b"\x9d\x01\x2a" + struct.pack("<HH", w, h)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def tree(paths, truncated=False):
    rows = [{"path": p, "mode": "100644", "type": "blob", "sha": "0" * 40, "size": s} for p, s in paths]
    rows.append({"path": "nature", "mode": "040000", "type": "tree", "sha": "1" * 40})
    return {"sha": "6bf4d733" + "0" * 32, "url": "https://api.github.com/x", "tree": rows, "truncated": truncated}


class FakeGitHub:
    """fetch(url, headers, cap, timeout, proxy) for the tree API, raw.githubusercontent.com and jsDelivr."""

    def __init__(self, files):
        self.files = dict(files)          # path → bytes
        self.calls = []
        self.raw_down = False
        self.down = False
        self.tree_status = 200

    def __call__(self, url, headers, cap, timeout, proxy):
        self.calls.append((url, dict(headers), cap, timeout, proxy))
        if self.down:
            raise OSError("connection refused")
        if url == wallpaper.TREE_URL:
            body = json.dumps(tree([(p, max(200 * KB, len(b))) for p, b in self.files.items()])).encode()
            return self.tree_status, body if self.tree_status == 200 else b""
        for prefix in ("https://raw.githubusercontent.com/dharmx/walls/main/",
                       "https://cdn.jsdelivr.net/gh/dharmx/walls@main/"):
            if url.startswith(prefix):
                if self.raw_down and "raw.githubusercontent" in url:
                    raise OSError("timed out")
                from urllib.parse import unquote
                found = self.files.get(unquote(url[len(prefix):]))
                return (200, found[:cap + 1]) if found is not None else (404, b"")
        raise AssertionError("unexpected url " + url)

    def count(self, part):
        return sum(part in c[0] for c in self.calls)


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class PureTests(unittest.TestCase):
    def test_tree_keeps_reasonable_images_only(self):
        answer = tree([("nature/a_misty_mountain.jpg", 900 * KB), ("nature/README.md", 2 * KB),
                       ("animated/box.gif", 900 * KB), ("animated/still.jpg", 900 * KB),
                       ("logo/arch.png", 300 * KB), ("Tile/pattern.png", 300 * KB),
                       ("nord/huge.png", 9 * 1024 * KB), ("nord/tiny.png", 20 * KB),
                       ("nord/ok.PNG", 2000 * KB), ("unsorted/x.webp", 500 * KB), ("top.jpg", 900 * KB),
                       ("a/b/c.jpg", 900 * KB), ("../etc/passwd.jpg", 900 * KB), ("m-26.jp/one.jpeg", 400 * KB),
                       ("nature/we ird.jpg", 900 * KB)])
        self.assertEqual([p for p, _ in tree_files(answer)],
                         ["nature/a_misty_mountain.jpg", "nord/ok.PNG", "unsorted/x.webp", "m-26.jp/one.jpeg"])
        self.assertEqual(tree_files({"message": "API rate limit exceeded"}), [])
        self.assertEqual(tree_files(None), [])
        self.assertEqual(tree_files({"tree": "nope"}), [])

    def test_image_sizes_from_headers(self):
        self.assertEqual(image_size(png(1920, 1080)), ("image/png", 1920, 1080))
        self.assertEqual(image_size(jpeg(2560, 1440)), ("image/jpeg", 2560, 1440))
        self.assertEqual(image_size(webp_vp8x(3840, 2160)), ("image/webp", 3840, 2160))
        self.assertEqual(image_size(webp_vp8l(1600, 900)), ("image/webp", 1600, 900))
        self.assertEqual(image_size(webp_vp8(1366, 768)), ("image/webp", 1366, 768))
        for junk in (b"", b"<html>rate limited</html>", b"\xff\xd8\xff", b"GIF89a" + b"\0" * 40, b"RIFF0000WEBPVP8Z"):
            self.assertIsNone(image_size(junk), junk)

    def test_landscape_and_large_enough(self):
        self.assertTrue(usable(1920, 1080))
        self.assertFalse(usable(1080, 1920), "portrait")
        self.assertFalse(usable(1000, 600), "too small")
        self.assertFalse(usable(2000, 1800), "nearly square")

    def test_nice_names(self):
        self.assertEqual(nice_name("nature/a_misty-mountain.at.dawn.jpg"), ("nature", "a misty mountain at dawn"))
        self.assertTrue(len(nice_name("x/" + "a_" * 80 + ".png")[1]) <= 60)


class WallsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wallpapers"
        self.clock = Clock()
        self.logs = []
        self.files = {f"nature/wide_{i:02d}.png": png(1920, 1080, f"n{i}") for i in range(20)}
        self.files["anime/tall_one.jpg"] = jpeg(1080, 1920, "tall")
        self.gh = FakeGitHub(self.files)
        self.busy = False
        self.walls = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self):
        import random
        return Walls(self.root, fetch=self.gh, clock=self.clock, log=self.logs.append, proxy=lambda: "socks5://p:1080",
                     busy=lambda: self.busy, background=False, rand=random.Random(7))

    def fill(self, steps=40):
        for _ in range(steps):
            self.walls.step()

    def test_never_fetches_on_the_request_path_and_idles_until_asked(self):
        self.assertEqual(self.walls.step(), 3600, "nobody asked: GitHub is not contacted at all")
        self.assertEqual(self.gh.calls, [])
        answer = self.walls.answer()
        self.assertEqual((answer["wallpaper"], answer["count"], answer["filling"]), (None, 0, True))
        self.assertEqual(self.gh.calls, [], "the page's question never waits on GitHub")
        self.walls.step()
        self.assertEqual(self.gh.count("api.github.com"), 1)
        self.assertEqual(self.gh.count("raw.githubusercontent.com"), 1, "one image per step")

    def test_fills_one_at_a_time_through_the_proxy_without_user_data(self):
        self.walls.pick()
        self.fill()
        answer = self.walls.answer()
        self.assertEqual(answer["count"], wallpaper.POOL_MAX)
        self.assertFalse(answer["filling"])
        w = answer["wallpaper"]
        self.assertEqual(w["credit"], "壁纸：dharmx/walls")
        self.assertEqual(w["theme"], "nature")
        self.assertTrue(w["name"].startswith("wide "))
        self.assertEqual(w["url"], "api/home/wallpaper/" + w["id"])
        self.assertEqual((w["w"], w["h"]), (1920, 1080))
        mime, body = self.walls.image(w["id"])
        self.assertEqual(mime, "image/png")
        self.assertIn(body, self.files.values())
        self.assertEqual(self.gh.count("api.github.com"), 1, "the list is fetched once")
        for url, headers, cap, timeout, proxy in self.gh.calls:
            self.assertEqual(proxy, "socks5://p:1080")
            self.assertEqual(set(headers), {"User-Agent", "Accept"}, "no cookie, no user name")
            self.assertLessEqual(timeout, 30)
        # files are private
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        for path in self.root.iterdir():
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)
        self.assertEqual(len([p for p in self.root.iterdir() if p.suffix == ".png"]), wallpaper.POOL_MAX)

    def test_portrait_is_rejected_and_never_tried_again(self):
        self.gh.files = {"anime/tall_one.jpg": jpeg(1080, 1920), "nature/wide.png": png(1920, 1080)}
        self.walls.pick()
        self.fill(6)
        self.assertEqual([p["path"] for p in self.walls._pool], ["nature/wide.png"])
        self.assertIn("anime/tall_one.jpg", json.loads((self.root / "list.json").read_text())["rejected"])
        self.assertEqual(self.gh.count("tall_one"), 1)

    def test_falls_back_to_jsdelivr(self):
        self.gh.raw_down = True
        self.walls.pick()
        self.walls.step()
        self.assertEqual(self.gh.count("cdn.jsdelivr.net/gh/dharmx/walls@main/nature/"), 1)
        self.assertEqual(len(self.walls._pool), 1)

    def test_offline_backs_off_and_keeps_the_pool(self):
        self.walls.pick()
        self.fill(3)
        have = len(self.walls._pool)
        self.gh.down = True
        delay = self.walls.step()
        self.assertGreaterEqual(delay, wallpaper.FAIL_GAP_S)
        before = len(self.gh.calls)
        self.clock.t += 10
        self.walls.step()
        self.assertEqual(len(self.gh.calls), before, "resting after a failure")
        self.assertEqual(len(self.walls._pool), have)
        self.assertIsNotNone(self.walls.pick())

    def test_no_list_no_errors(self):
        self.gh.tree_status = 403          # rate limited
        self.walls.pick()
        delay = self.walls.step()
        self.assertGreaterEqual(delay, 60)
        self.assertEqual(self.gh.count("raw.githubusercontent"), 0)
        self.walls.step()
        self.assertEqual(self.gh.count("api.github.com"), 1, "not asked again before LIST_RETRY_S")
        self.assertIsNone(self.walls.answer()["wallpaper"])

    def test_list_is_weekly_and_survives_a_restart(self):
        self.walls.pick()
        self.fill(3)
        self.clock.t += 3600
        self.walls = self.make()
        self.walls.pick()
        self.walls.step()
        self.assertEqual(self.gh.count("api.github.com"), 1, "list.json is reused")
        self.assertEqual(len(self.walls._pool), 4, "pool.json is reused")
        self.clock.t += wallpaper.LIST_TTL_S
        self.walls.pick()
        self.walls.step()
        self.assertEqual(self.gh.count("api.github.com"), 2)

    def test_full_pool_rotates_slowly_and_respects_the_caps(self):
        self.walls.pick()
        self.fill()
        fetched = len(self.gh.calls)
        self.walls.step()
        self.assertEqual(len(self.gh.calls), fetched, "full: wait ROTATE_S")
        oldest = min(self.walls._pool, key=lambda p: p["at"])["id"]
        self.clock.t += wallpaper.ROTATE_S
        self.walls.pick()
        self.walls.step()
        self.assertEqual(len(self.walls._pool), wallpaper.POOL_MAX)
        self.assertNotIn(oldest, [p["id"] for p in self.walls._pool])
        self.assertFalse(any(p.name.startswith(oldest) for p in self.root.iterdir()), "dropped file deleted")
        # bytes cap
        big = {f"nord/big_{i}.png": png(1920, 1080, str(i)) + b"\0" * (3 * 1024 * KB) for i in range(30)}
        self.gh.files = big
        self.clock.t += wallpaper.LIST_TTL_S
        for _ in range(30):
            self.clock.t += wallpaper.ROTATE_S
            self.walls.pick()
            self.walls.step()
        self.assertLessEqual(sum(p["bytes"] for p in self.walls._pool), wallpaper.POOL_BYTES)

    def test_idle_and_busy_do_not_fetch(self):
        self.walls.pick()
        self.busy = True
        self.assertEqual(self.walls.step(), wallpaper.BUSY_S)
        self.assertEqual(self.gh.calls, [])
        self.busy = False
        self.clock.t += wallpaper.IDLE_S + 1
        self.walls.step()
        self.assertEqual(self.gh.calls, [], "nobody looked at the home page for days")

    def test_pick_avoids_the_current_one(self):
        self.walls.pick()
        self.fill(4)
        current = self.walls.pick()["id"]
        for _ in range(20):
            self.assertNotEqual(self.walls.pick(current)["id"], current)

    def test_stray_files_are_tidied(self):
        self.root.mkdir(parents=True)
        (self.root / ("0123456789abcdef.png")).write_bytes(b"x")
        (self.root / "keep.txt").write_text("not ours")
        self.make()
        self.assertFalse((self.root / "0123456789abcdef.png").exists())
        self.assertTrue((self.root / "keep.txt").exists())


class EndpointTests(RelayHarness):
    def setUp(self):
        super().setUp()
        walls = self.relay.wallpapers
        if walls is None:
            return
        walls.background = False
        walls.fetch = self.gh = FakeGitHub({f"nature/wide_{i}.png": png(1920, 1080, str(i)) for i in range(3)})
        walls.proxy = lambda: ""

    def test_answer_and_image(self):
        self.assertEqual(self.request("GET", "/tv/api/home/wallpaper")[0], 404, "signed out")
        self.assertTrue(self.request("GET", "/tv/api/state", user=self.kid)[1]["wallpaper"])
        status, answer = self.request("GET", "/tv/api/home/wallpaper", user=self.kid)
        self.assertEqual((status, answer["wallpaper"], answer["count"]), (200, None, 0))
        for _ in range(3):
            self.relay.wallpapers.step()
        answer = self.request("GET", "/tv/api/home/wallpaper", user=self.kid)[1]
        w = answer["wallpaper"]
        self.assertEqual(answer["count"], 3)
        self.assertEqual(self.request("GET", "/tv/api/home/wallpaper?not=" + w["id"], user=self.kid)[1]["count"], 3)
        self.assertNotEqual(self.request("GET", "/tv/api/home/wallpaper?not=" + w["id"], user=self.kid)[1]["wallpaper"]["id"],
                            w["id"])
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=5)
        connection.request("GET", "/tv/" + w["url"], headers={"Cookie": "tvs=" + self.tokens[self.kid]})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertIn("immutable", response.getheader("Cache-Control"))
        self.assertEqual(image_size(body), ("image/png", 1920, 1080))
        for bad in ("/tv/api/home/wallpaper/../../tv.db", "/tv/api/home/wallpaper/zzzz", "/tv/api/home/wallpaper/" + "0" * 16):
            self.assertEqual(self.request("GET", bad, user=self.kid)[0], 404, bad)


class CloudEndpointTests(RelayHarness):
    selfhost = False

    def test_cloud_has_no_wallpapers(self):
        self.assertIsNone(self.relay.wallpapers)
        self.assertEqual(self.request("GET", "/tv/api/home/wallpaper", user=self.admin)[0], 404)
        self.assertNotIn("wallpaper", self.request("GET", "/tv/api/state", user=self.admin)[1])


if __name__ == "__main__":
    unittest.main()
