"""My library (server/relay/library.py): network drives over WebDAV (OpenList) and folders on this computer.

What is checked: file names are parsed into movies (「名字 (2019)」, 「名字.2019.1080p…」),
episodes (S01E02, EP03, E03, 第3集 / 第十二集, a bare 「01」 in the show's folder, the show
taken from a season folder's parent) and other videos, without mistaking a year or a
resolution for an episode; .nfo files give title, year and plot, a trailing URL line is
tolerated and DOCTYPE/ENTITY files are refused; a local walk skips hidden files, @eaDir,
.DS_Store and recycle bins, never follows a symlink out of the root (nor into a folder),
stops at the entry and depth caps and says so; posters next to the video (or in the show
folder above a season folder) are found without being read; a rescan keeps unchanged
items (the .nfo is not read again), re-reads changed ones and drops deleted ones. Over a
tiny in-process WebDAV server (PROPFIND Depth: 1, basic auth, GET with Range, one 302 to
a different host): the walk percent-decodes names, fetches small .nfo files, a wrong
password is refused in plain Chinese, and posters are fetched only when asked and then
cached. Serving, through the relay's real HTTP port: by item id only, 200 without Range and
206 with it (local and WebDAV, including the redirect, where Range is re-sent and the
password is not), 416 past the end; guests and signed-out callers get 404; passwords are
never in an API answer, in the logs or readable by others (library.json is mode 600); only
admins add, delete and scan sources (and those are audited); the cloud relay has no library.
On the PC a library item plays as a "vod" answer at api/library/file/<id> (an mkv as an "hls"
answer through watch.py), its progress is kept under lib:<id> and resumes, a show's episodes
are the episode list, and a TV target is refused for now. How step 2 decides, probes, remuxes
and serves subtitles is in test_relay_library_play.py.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import home, library, web

import test_relay_watch as watch_base

PASSWORD = "s3cret-Library-pw"
MP4 = bytes(range(256)) * 40          # 10240 字节的「视频」
NFO = (b'<?xml version="1.0" encoding="UTF-8"?>\n<movie><title>\xe7\x9b\x97\xe6\xa2\xa6\xe7\xa9\xba\xe9\x97\xb4</title>'
       b"<year>2010</year><plot>A thief who steals secrets.</plot></movie>\nhttps://www.themoviedb.org/movie/27205\n")


# ---------------------------------------------------------------- a tiny WebDAV server

class DavHandler(BaseHTTPRequestHandler):
    """/dav/... is the drive (basic auth); a file in REDIRECT answers 302 to /cdn/... on «localhost»
    (another host than 127.0.0.1), which wants no password."""
    protocol_version = "HTTP/1.1"
    files: dict[str, bytes] = {}
    redirect: set[str] = set()
    seen: list[tuple[str, str, str, str]] = []
    user = "family"

    def log_message(self, *_args):
        pass

    def _ok(self) -> bool:
        good = "Basic " + base64.b64encode(f"{self.user}:{PASSWORD}".encode()).decode()
        return self.headers.get("Authorization") == good

    def _record(self):
        self.seen.append((self.command, urllib.parse.unquote(urllib.parse.urlsplit(self.path).path),
                          self.headers.get("Authorization") or "", self.headers.get("Range") or ""))

    def _plain(self, code, body=b"", headers=None):
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PROPFIND(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._record()
        if not self._ok():
            self._plain(401, b"no", {"WWW-Authenticate": 'Basic realm="dav"'})
            return
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if not path.startswith("/dav"):
            self._plain(404)
            return
        rel = path[4:].strip("/")
        dirs = {""}
        for name in self.files:
            parts = name.split("/")
            dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        if rel not in dirs:
            self._plain(404)
            return
        rows = [(rel, True, 0)]
        if self.headers.get("Depth") != "0":
            prefix = rel + "/" if rel else ""
            kids = {}
            for name, data in self.files.items():
                if name.startswith(prefix):
                    head = name[len(prefix):].split("/")[0]
                    kids[prefix + head] = (prefix + head) in dirs, len(data)
            for d in dirs:
                if d.startswith(prefix) and d and "/" not in d[len(prefix):]:
                    kids[d] = (True, 0)
            rows += [(k, v[0], v[1]) for k, v in sorted(kids.items())]
        out = ['<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">']
        for name, is_dir, size in rows:
            href = urllib.parse.quote("/dav/" + name) + ("/" if is_dir and name else "")
            kind = "<D:collection/>" if is_dir else ""
            out.append(f"<D:response><D:href>{href}</D:href><D:propstat><D:prop><D:resourcetype>{kind}</D:resourcetype>"
                       f"<D:getcontentlength>{size}</D:getcontentlength>"
                       "<D:getlastmodified>Mon, 26 Oct 2026 08:00:00 GMT</D:getlastmodified></D:prop>"
                       "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")
        out.append("</D:multistatus>")
        self._plain(207, "".join(out).encode(), {"Content-Type": "application/xml; charset=utf-8"})

    def do_GET(self):
        self._record()
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path.startswith("/cdn/"):
            rel = path[5:]
        else:
            if not self._ok():
                self._plain(401)
                return
            rel = path[4:].strip("/")
            if rel in self.redirect:
                port = self.server.server_address[1]
                self._plain(302, b"", {"Location": f"http://localhost:{port}/cdn/{urllib.parse.quote(rel)}?sign=x"})
                return
        data = self.files.get(rel)
        if data is None:
            self._plain(404)
            return
        wanted = library.parse_range(self.headers.get("Range"), len(data))
        if wanted == "bad":
            self._plain(416, b"", {"Content-Range": f"bytes */{len(data)}"})
        elif wanted is None:
            self._plain(200, data, {"Content-Type": "application/octet-stream"})
        else:
            a, b = wanted
            self._plain(206, data[a:b + 1], {"Content-Type": "video/mp4", "Content-Range": f"bytes {a}-{b}/{len(data)}"})


def dav_server(files, redirect=()):
    handler = type("Dav", (DavHandler,), {"files": dict(files), "redirect": set(redirect), "seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, handler


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def quiet(root, logs=None) -> library.Library:
    lib = library.Library(root, log=(logs.append if logs is not None else lambda _m: None), background=False)
    lib.pace = False
    return lib


# ---------------------------------------------------------------- names and .nfo

class NameTests(unittest.TestCase):
    def check(self, rel, **want):
        found = library.parse_name(rel)
        self.assertEqual({k: found[k] for k in want}, want, (rel, found))

    def test_movies_and_other_videos(self):
        self.check("Inception.2010.1080p.BluRay.x264.mp4", kind="movie", title="Inception", year=2010)
        self.check("电影/流浪地球 (2019).mp4", kind="movie", title="流浪地球", year=2019)
        self.check("流浪地球2.2023.WEB-DL.4K.mkv", kind="movie", title="流浪地球2", year=2023)
        self.check("WALL-E (2008).mp4", kind="movie", title="WALL-E", year=2008, episode=None)
        self.check("家庭/2024 春节 聚会.mp4", kind="video", year=None)
        self.check("Big.Buck.Bunny.1080p.mp4", kind="video", title="Big Buck Bunny", year=None)

    def test_episodes(self):
        self.check("Friends.S01E02.1080p.mkv", kind="episode", show="Friends", season=1, episode=2)
        self.check("动画/进击的巨人/第3集.mp4", kind="episode", show="进击的巨人", episode=3)
        self.check("[字幕组] 名侦探柯南 第十二集 [1080p].mp4", kind="episode", show="名侦探柯南", episode=12)
        self.check("Show Name - EP03.mp4", kind="episode", show="Show Name", episode=3)
        self.check("Breaking Bad/Season 2/E03.mp4", kind="episode", show="Breaking Bad", season=2, episode=3)
        self.check("葫芦娃 (1986)/01.mp4", kind="episode", show="葫芦娃", episode=1)
        self.check("我的剧/第二季/第二十三集.mp4", kind="episode", show="我的剧", season=2, episode=23)
        self.assertEqual(library.parse_name("Friends.S01E02.Reunion.mkv")["title"], "第 2 集 · Reunion")

    def test_chinese_numbers(self):
        self.assertEqual([library.cn_number(t) for t in ("12", "十", "十二", "二十", "二十三", "一百零五", "两")],
                         [12, 10, 12, 20, 23, 105, 2])
        self.assertIsNone(library.cn_number("abc"))


class NfoTests(unittest.TestCase):
    def test_movie_with_a_trailing_url(self):
        self.assertEqual(library.parse_nfo(NFO), {"kind": "movie", "title": "盗梦空间", "year": 2010,
                                                  "plot": "A thief who steals secrets."})

    def test_episode_and_refusals(self):
        found = library.parse_nfo(b"<episodedetails><title>Pilot</title><showtitle>Lost</showtitle>"
                                  b"<season>1</season><episode>1</episode><aired>2004-09-22</aired></episodedetails>")
        self.assertEqual(found, {"kind": "episode", "title": "Pilot", "year": 2004, "show": "Lost", "season": 1,
                                 "episode": 1})
        bomb = b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaa">]><movie><title>&a;</title></movie>'
        self.assertEqual(library.parse_nfo(bomb), {})
        self.assertEqual(library.parse_nfo(b"https://www.imdb.com/title/tt1375666/"), {})
        self.assertEqual(library.parse_nfo(b"<html><title>x</title></html>"), {})
        self.assertEqual(library.parse_nfo(b"<movie>" + b"x" * library.NFO_MAX + b"</movie>"), {})


# ---------------------------------------------------------------- local folders

class LocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "data"
        self.root = base / "videos"
        self.outside = touch(base / "secret" / "private.mp4")
        r = self.root
        touch(r / "Movies" / "Inception.2010.mp4", MP4)
        touch(r / "Movies" / "Inception.2010.nfo", NFO)
        touch(r / "Movies" / "Inception.2010-poster.jpg", b"\xff\xd8poster")
        touch(r / "Shows" / "Friends" / "poster.jpg", b"\xff\xd8show")
        touch(r / "Shows" / "Friends" / "Season 1" / "Friends.S01E01.mp4")
        touch(r / "Shows" / "Friends" / "Season 1" / "Friends.S01E02.mkv")
        touch(r / "Family" / "生日.mov")
        touch(r / "Family" / "生日-sample.mov")
        touch(r / ".hidden" / "x.mp4")
        touch(r / "Family" / ".secret.mp4")
        touch(r / "@eaDir" / "Inception.2010.mp4" / "SYNOVIDEO.mp4")
        touch(r / "$RECYCLE.BIN" / "old.mp4")
        touch(r / ".DS_Store")
        os.symlink(self.outside, r / "Family" / "escape.mp4")
        os.symlink(self.outside.parent, r / "linked-dir")
        self.lib = quiet(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self):
        answer = self.lib.add({"kind": "local", "name": "电脑", "path": str(self.root)})
        self.assertTrue(answer["ok"], answer)
        return answer["source"]["id"]

    def test_walk_skips_hidden_and_never_leaves_the_root(self):
        self.add()
        paths = sorted(i["path"] for i in self.lib.items())
        self.assertEqual(paths, ["Family/生日.mov", "Movies/Inception.2010.mp4",
                                 "Shows/Friends/Season 1/Friends.S01E01.mp4",
                                 "Shows/Friends/Season 1/Friends.S01E02.mkv"])
        movie = next(i for i in self.lib.items() if i["kind"] == "movie")
        self.assertEqual((movie["title"], movie["year"], movie["plot"], movie["poster"]),
                         ("盗梦空间", 2010, "A thief who steals secrets.", "Movies/Inception.2010-poster.jpg"))
        episodes = [i for i in self.lib.items() if i["kind"] == "episode"]
        self.assertEqual({(e["show"], e["season"], e["episode"], e["playable"]) for e in episodes},
                         {("Friends", 1, 1, True), ("Friends", 1, 2, True)}, "an mkv plays too (step 2)")
        self.assertEqual({e["poster"] for e in episodes}, {"Shows/Friends/poster.jpg"}, "the show folder's poster")
        folder = library.LocalFolder(str(self.root))
        self.assertIsNone(folder.path_of("../secret/private.mp4"))
        self.assertIsNone(folder.path_of("Family/escape.mp4"))

    def test_bad_local_paths_are_refused(self):
        self.assertIn("完整路径", self.lib.add({"kind": "local", "path": "videos"})["error"])
        self.assertIn("不存在", self.lib.add({"kind": "local", "path": str(self.root / "nope")})["error"])
        self.assertIn("整个磁盘", self.lib.add({"kind": "local", "path": "/"})["error"])
        self.add()
        self.assertIn("已经加过", self.lib.add({"kind": "local", "path": str(self.root) + "/"})["error"])

    def test_caps_on_entries_and_depth(self):
        deep = self.root / "deep"
        for n in range(10):
            deep = deep / f"d{n}"
        touch(deep / "bottom.mp4")
        sid = self.add()
        self.assertNotIn("bottom.mp4", {Path(i["path"]).name for i in self.lib.items()})
        with mock.patch.object(library, "WALK_MAX", 5):
            self.assertTrue(self.lib.scan_now(sid)["ok"])
        source = self.lib.admin_view()["sources"][0]
        self.assertIn("只扫了前 5 个", source["note"])
        self.assertLessEqual(source["count"], 5)

    def test_rescan_is_incremental(self):
        sid = self.add()
        movie = next(i for i in self.lib.items() if i["kind"] == "movie")
        # The .nfo changes but the video does not: the item is kept as it was (the .nfo is not read again).
        touch(self.root / "Movies" / "Inception.2010.nfo", NFO.replace(b"2010", b"1999"))
        with mock.patch.object(library.LocalFolder, "read_small", side_effect=AssertionError("re-read")):
            self.assertTrue(self.lib.scan_now(sid)["ok"])
        self.assertEqual(self.lib.item(movie["id"])["year"], 2010)
        # The video changes: read again; a deleted file goes away; 「added」 is kept.
        touch(self.root / "Movies" / "Inception.2010.mp4", MP4 + b"more")
        os.remove(self.root / "Family" / "生日.mov")
        self.assertTrue(self.lib.scan_now(sid)["ok"])
        again = self.lib.item(movie["id"])
        self.assertEqual((again["year"], again["added"]), (1999, movie["added"]))
        self.assertEqual(len(self.lib.items()), 3)
        # The index survives a restart.
        self.assertEqual(len(quiet(self.data).items()), 3)

    def test_view_groups_movies_shows_and_videos(self):
        self.add()
        view = self.lib.view()
        self.assertEqual([m["title"] for m in view["movies"]], ["盗梦空间"])
        self.assertEqual([(s["title"], s["count"]) for s in view["shows"]], [("Friends", 2)])
        self.assertEqual([e["episode"] for e in view["shows"][0]["episodes"]], [1, 2])
        self.assertEqual([v["title"] for v in view["videos"]], ["生日"])
        self.assertNotIn(str(self.root), json.dumps(view, ensure_ascii=False), "no paths for the page")
        self.assertEqual(view["sources"][0]["count"], 4)


# ---------------------------------------------------------------- WebDAV

DAV_FILES = {
    "电影/盗梦空间 (2010)/盗梦空间 (2010).mp4": MP4,
    "电影/盗梦空间 (2010)/盗梦空间 (2010).nfo": NFO,
    "电影/盗梦空间 (2010)/poster.jpg": b"\xff\xd8dav-poster",
    "剧集/老友记/S01/老友记 S01E01.mp4": MP4[:2000],
    "剧集/老友记/S01/老友记 S01E02.mp4": MP4[:3000],
    "@eaDir/thumb.mp4": b"x",
    ".hidden.mp4": b"x",
}


class ScheduleTests(unittest.TestCase):
    """Newest first, the year kept from a file name that is only a year and tags, cloud drives scanned hourly and
    when the library page is opened."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "videos"
        self.now = 2_000_000_000.0
        self.lib = quiet(Path(self.tmp.name) / "data")
        self.lib.clock = lambda: self.now

    def test_year_from_a_tags_only_file_name(self):
        found = library.parse_name("夸克/来自：分享/龙/2026.WEB-DL.mp4")
        self.assertEqual((found["kind"], found["title"], found["year"]), ("movie", "龙", 2026))

    def test_newest_first(self):
        touch(self.root / "Alpha (2001)" / "Alpha.2001.mp4", MP4)
        self.assertTrue(self.lib.add({"kind": "local", "path": str(self.root)})["ok"])
        self.now += 100
        touch(self.root / "Zulu (2002)" / "Zulu.2002.mp4", MP4)
        self.lib.scan()
        self.assertEqual([m["title"] for m in self.lib.view()["movies"]], ["Zulu", "Alpha"])

    def test_cloud_hourly_and_on_opening_the_page(self):
        self.lib._save_sources([{"id": "aaaaaaaa", "kind": "webdav", "name": "网盘", "url": "http://127.0.0.1:1/dav",
                                 "last_scan": self.now, "ok": True},
                                {"id": "bbbbbbbb", "kind": "local", "name": "本机", "path": str(self.root),
                                 "last_scan": self.now, "ok": True}])
        self.assertEqual(self.lib.due(), [])
        self.now += library.DAV_EVERY_S
        self.assertEqual(self.lib.due(), ["aaaaaaaa"], "a cloud drive hourly, a local folder twice a day")
        queued = []
        self.lib.scan = lambda sid=None: queued.append(sid)
        self.now = 2_000_000_000.0 + library.OPEN_FRESH_S - 1
        self.lib.freshen()
        self.assertEqual(queued, [], "scanned a moment ago")
        self.now += 1
        self.assertTrue(self.lib.freshen())
        self.assertEqual(queued, ["aaaaaaaa", "bbbbbbbb"])


class DavTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server, self.handler = dav_server(DAV_FILES, redirect={"剧集/老友记/S01/老友记 S01E02.mp4"})
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dav"
        self.logs = []
        self.lib = quiet(Path(self.tmp.name), self.logs)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_wrong_password_and_not_webdav(self):
        answer = self.lib.add({"kind": "webdav", "url": self.url, "user": "family", "password": "wrong"})
        self.assertEqual(answer, {"ok": False, "error": "用户名或密码不对"})
        answer = self.lib.add({"kind": "webdav", "url": self.url.replace("/dav", "/nope"), "user": "family",
                               "password": PASSWORD})
        self.assertIn("/dav", answer["error"])
        self.assertIn("分开填", self.lib.add({"kind": "webdav", "url": "http://u:p@127.0.0.1/dav"})["error"])
        self.assertIn("http://", self.lib.add({"kind": "webdav", "url": "ftp://x/dav"})["error"])
        self.assertFalse(self.lib._load_sources(), "nothing stored")

    def test_walk_decodes_names_reads_nfo_not_posters(self):
        answer = self.lib.add({"kind": "webdav", "name": "网盘", "url": self.url, "user": "family",
                               "password": PASSWORD})
        self.assertTrue(answer["ok"], answer)
        items = {i["path"]: i for i in self.lib.items()}
        self.assertEqual(sorted(items), sorted(k for k in DAV_FILES if k.endswith(".mp4") and "@" not in k
                                               and not k.startswith(".")))
        movie = items["电影/盗梦空间 (2010)/盗梦空间 (2010).mp4"]
        self.assertEqual((movie["title"], movie["year"], movie["poster"]), ("盗梦空间", 2010,
                                                                            "电影/盗梦空间 (2010)/poster.jpg"))
        gets = [path for method, path, _, _ in self.handler.seen if method == "GET"]
        self.assertEqual(gets, ["/dav/电影/盗梦空间 (2010)/盗梦空间 (2010).nfo"], "only the .nfo was fetched")
        show = {(i["show"], i["season"], i["episode"]) for i in items.values() if i["kind"] == "episode"}
        self.assertEqual(show, {("老友记", 1, 1), ("老友记", 1, 2)})
        stored = (Path(self.tmp.name) / "library.json")
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        self.assertIn(PASSWORD, stored.read_text(encoding="utf-8"))
        everything = json.dumps([self.lib.view(), self.lib.admin_view()], ensure_ascii=False) + "\n".join(self.logs)
        self.assertNotIn(PASSWORD, everything)
        self.assertNotIn(self.url, "\n".join(self.logs), "logs name the source, never its address")
        self.assertTrue(self.lib.admin_view()["sources"][0]["has_password"])

    def test_poster_is_fetched_lazily_and_cached(self):
        self.lib.add({"kind": "webdav", "url": self.url, "user": "family", "password": PASSWORD})
        movie = next(i for i in self.lib.items() if i["kind"] == "movie")
        src = self.lib._load_sources()[0]
        self.assertEqual(self.lib._art_bytes(src, movie["poster"]), b"\xff\xd8dav-poster")
        self.assertEqual(self.lib._art_bytes(src, movie["poster"]), b"\xff\xd8dav-poster")
        fetched = [p for m, p, _, _ in self.handler.seen if m == "GET" and p.endswith("poster.jpg")]
        self.assertEqual(len(fetched), 1, "the second time comes from library-art/")
        self.assertEqual(len(list((Path(self.tmp.name) / "library-art").iterdir())), 1)
        self.lib.remove(src["id"])
        self.assertEqual(list((Path(self.tmp.name) / "library-art").iterdir()), [], "a removed source's art goes")
        self.assertEqual(self.lib.items(), [])


# ---------------------------------------------------------------- over the relay's HTTP port

class ApiTests(unittest.TestCase):
    selfhost = True
    setUp_relay = watch_base.RelayWatchTests.setUp
    tearDown_relay = watch_base.RelayWatchTests.tearDown
    online = watch_base.RelayWatchTests.online

    def setUp(self):
        self.setUp_relay()
        self.cookies["admin"] = "tvs=" + self.accounts.new_session(self.admin, "pc")
        self.root = Path(self.tmp.name) / "videos"
        touch(self.root / "Inception.2010.mp4", MP4)
        touch(self.root / "Inception.2010.jpg", b"\xff\xd8local")
        touch(self.root / "Clip.mkv", b"mkv")
        touch(self.root / "家庭剧" / "S01E01.mp4", MP4[:1000])
        touch(self.root / "家庭剧" / "S01E02.mp4", MP4[:1500])
        self.server, self.handler = dav_server(DAV_FILES, redirect={"剧集/老友记/S01/老友记 S01E02.mp4"})
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dav"
        self.logs = []
        self.relay.library = quiet(Path(self.accounts.path).parent, self.logs)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tearDown_relay()

    def request(self, method, path, who=None, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=20)
        sent = dict(headers or {})
        if who:
            sent["Cookie"] = self.cookies[who]
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            sent["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, response, raw

    def call(self, who, method, path, body=None):
        status, _, raw = self.request(method, path, who, body if body is not None or method != "POST" else {})
        return status, (json.loads(raw) if status == 200 else None)

    def add_both(self):
        local = self.call("admin", "POST", "/tv/api/admin/library", {"kind": "local", "path": str(self.root)})[1]
        dav = self.call("admin", "POST", "/tv/api/admin/library", {"kind": "webdav", "name": "网盘", "url": self.url,
                                                                    "user": "family", "password": PASSWORD})[1]
        self.assertTrue(local["ok"] and dav["ok"], (local, dav))
        return {i["path"]: i["id"] for i in self.relay.library.items()}

    # ---- who ----

    def test_admin_only_management_and_family_only_reading(self):
        for who in ("alice", "guest"):
            self.assertEqual(self.call(who, "GET", "/tv/api/admin/library")[0], 404)
            self.assertEqual(self.call(who, "POST", "/tv/api/admin/library",
                                       {"kind": "local", "path": str(self.root)})[0], 404)
            self.assertEqual(self.call(who, "POST", "/tv/api/admin/library/scan")[0], 404)
        self.assertEqual(self.relay.library.items(), [])
        ids = self.add_both()
        any_id = ids["Inception.2010.mp4"]
        for path in ("/tv/api/library", f"/tv/api/library/file/{any_id}", f"/tv/api/library/art/{any_id}"):
            self.assertEqual(self.request("GET", path, "guest")[0], 404, path)
            self.assertEqual(self.request("GET", path)[0], 404, path)
        status, view = self.call("alice", "GET", "/tv/api/library")
        self.assertEqual(status, 200)
        self.assertFalse(view["admin"])
        self.assertEqual(len(view["sources"]), 2)
        admin_view = self.call("admin", "GET", "/tv/api/admin/library")[1]
        dav = next(s for s in admin_view["sources"] if s["kind"] == "webdav")
        self.assertEqual((dav["user"], dav["has_password"]), ("family", True))
        for answer in (view, admin_view, self.call("alice", "GET", "/tv/api/home")[1]):
            self.assertNotIn(PASSWORD, json.dumps(answer, ensure_ascii=False))
        self.assertNotIn(self.url, json.dumps(view, ensure_ascii=False), "family see no addresses")
        self.assertNotIn(PASSWORD, "\n".join(self.logs))
        self.assertTrue(self.call("admin", "POST", "/tv/api/admin/library/scan", {"id": dav["id"]})[1]["ok"])
        self.assertTrue(self.call("admin", "POST", "/tv/api/admin/library/delete", {"id": dav["id"]})[1]["ok"])
        actions = [r["action"] for r in self.accounts.audit_rows(100) if r["action"].startswith("library.")]
        self.assertEqual(sorted(actions), ["library.add", "library.add", "library.delete", "library.scan"])
        self.assertEqual(len(self.call("alice", "GET", "/tv/api/library")[1]["sources"]), 1)

    def test_cloud_relay_has_no_library(self):
        cloud = web.Relay(None, watch_base.FakeClient(), None, "/opt/ff", watch_base.KEY, accounts=self.accounts,
                          devices_dir=self.devices_dir, log=lambda _m: None)
        self.assertIsNone(library.attach(cloud, start=False))
        self.assertIsNone(cloud.library)
        self.relay.library = None
        self.assertEqual(self.request("GET", "/tv/api/library", "alice")[0], 404)
        self.assertFalse(self.call("admin", "GET", "/tv/api/admin/library")[1]["ok"])

    # ---- serving ----

    def test_local_file_with_and_without_range(self):
        ids = self.add_both()
        path = f"/tv/api/library/file/{ids['Inception.2010.mp4']}"
        status, response, raw = self.request("GET", path, "alice")
        self.assertEqual((status, raw, response.getheader("Content-Type"), response.getheader("Accept-Ranges")),
                         (200, MP4, "video/mp4", "bytes"))
        status, response, raw = self.request("GET", path, "alice", headers={"Range": "bytes=100-199"})
        self.assertEqual((status, raw, response.getheader("Content-Range")), (206, MP4[100:200], "bytes 100-199/10240"))
        status, response, raw = self.request("GET", path, "alice", headers={"Range": "bytes=-10"})
        self.assertEqual((status, raw), (206, MP4[-10:]))
        status, response, _ = self.request("GET", path, "alice", headers={"Range": "bytes=99999-"})
        self.assertEqual((status, response.getheader("Content-Range")), (416, "bytes */10240"))
        status, _, raw = self.request("GET", f"/tv/api/library/art/{ids['Inception.2010.mp4']}", "alice")
        self.assertEqual((status, raw), (200, b"\xff\xd8local"))

    def test_id_only(self):
        self.add_both()
        for path in ("/tv/api/library/file/../../etc/passwd", "/tv/api/library/file/" + "0" * 16,
                     "/tv/api/library/file/Inception.2010.mp4", "/tv/api/library/file/%2e%2e%2fsecret"):
            self.assertEqual(self.request("GET", path, "alice")[0], 404, path)

    def test_webdav_file_with_range_and_redirect(self):
        ids = self.add_both()
        movie = ids["电影/盗梦空间 (2010)/盗梦空间 (2010).mp4"]
        status, response, raw = self.request("GET", f"/tv/api/library/file/{movie}", "alice")
        self.assertEqual((status, raw, response.getheader("Content-Type")), (200, MP4, "video/mp4"))
        status, response, raw = self.request("GET", f"/tv/api/library/file/{movie}", "alice",
                                             headers={"Range": "bytes=10-19"})
        self.assertEqual((status, raw, response.getheader("Content-Range")), (206, MP4[10:20], "bytes 10-19/10240"))
        self.handler.seen.clear()
        moved = ids["剧集/老友记/S01/老友记 S01E02.mp4"]
        status, response, raw = self.request("GET", f"/tv/api/library/file/{moved}", "alice",
                                             headers={"Range": "bytes=0-99"})
        self.assertEqual((status, raw), (206, MP4[:100]))
        hops = [(p, auth != "", r) for m, p, auth, r in self.handler.seen if m == "GET"]
        self.assertEqual(hops, [("/dav/剧集/老友记/S01/老友记 S01E02.mp4", True, "bytes=0-99"),
                                ("/cdn/剧集/老友记/S01/老友记 S01E02.mp4", False, "bytes=0-99")],
                         "Range sent again after the redirect, the password not sent to the other host")
        self.assertFalse(any("sign=" in line for line in self.logs))
        # The poster is fetched only now, once.
        status, _, raw = self.request("GET", f"/tv/api/library/art/{movie}", "alice")
        self.assertEqual((status, raw), (200, b"\xff\xd8dav-poster"))

    # ---- playing on the PC ----

    def watch(self, **body):
        body.setdefault("viewer", "c0ffee00")
        return self.call("alice", "POST", "/tv/api/watch", body)[1]

    def test_plays_on_the_pc_with_progress_and_episodes(self):
        ids = self.add_both()
        movie = ids["Inception.2010.mp4"]
        answer = self.watch(library=movie)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["mode"], answer["url"], answer["kind"], answer["seekable"], answer["start"]),
                         ("vod", f"api/library/file/{movie}", "video", True, 0.0))
        self.assertNotIn("resume", answer)
        report = self.call("alice", "POST", "/tv/api/watch/progress",
                           {"viewer": "c0ffee00", "sid": answer["sid"], "position": 300.0, "duration": 5400.0})[1]
        self.assertTrue(report["ok"], report)
        saved = self.relay.pc_progress.get(self.alice, "lib:" + movie)
        self.assertEqual((saved["position"], saved["duration"], saved["site"]), (300.0, 5400.0, "library"))
        again = self.watch(library=movie)
        self.assertEqual((again["start"], again["resume"], again["duration"]), (300.0, 300.0, 5400))
        self.relay.forget_home(web.Caller(user=self.alice, role="user", name="Alice"))
        home_answer = self.call("alice", "GET", "/tv/api/home")[1]
        row = next(r for r in home_answer["continue"] if r["id"] == f"pc:lib:{movie}")
        self.assertEqual((row["play"], row["progress"]["position"]), ({"kind": "library", "id": movie}, 300))
        self.assertTrue(home_answer["library"]["enabled"])
        self.assertTrue(home_answer["library_recent"])
        self.assertEqual(self.watch(library=movie, start=0)["start"], 0.0)
        # A show: the episode list, each with its id.
        first = ids["家庭剧/S01E01.mp4"]
        show = self.watch(library=first)
        self.assertEqual([p["id"] for p in show["parts"]["items"]], [first, ids["家庭剧/S01E02.mp4"]])
        self.assertEqual((show["name"], show["parts"]["current"]), ("家庭剧", 1))
        # An mkv (not probed here: the relay's ffmpeg is fake) goes through watch.py (on a TV: test_relay_library_tv).
        clip = self.watch(library=ids["Clip.mkv"])
        self.assertEqual((clip["ok"], clip["mode"], clip["kind"], clip["seekable"]), (True, "hls", "video", True), clip)
        self.assertIn("片库里没有", self.watch(library="0" * 16)["error"])
        pc = self.call("alice", "POST", "/tv/api/play", {"target": "pc", "viewer": "c0ffee00",
                                                          "item": {"kind": "library", "id": movie}})[1]
        self.assertEqual((pc["ok"], pc["mode"]), (True, "vod"))

    def test_guest_cannot_watch_a_library_item(self):
        ids = self.add_both()
        status, _, raw = self.request("POST", "/tv/api/watch", "guest",
                                      {"viewer": "c0ffee00", "library": ids["Inception.2010.mp4"]})
        self.assertTrue(status == 404 or not json.loads(raw)["ok"])
        self.assertEqual(home._pc_body({"kind": "library", "id": "abc"}), {"library": "abc"})


if __name__ == "__main__":
    unittest.main()
