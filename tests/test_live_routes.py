"""Backup lines (routes) for live channels: server/routes.py and the places that use them.

What is checked: a playlist import keeps the other addresses of a channel as its
backup lines -- M3U rows with the same normalised name or tvg-id, and a TXT
line's #-separated addresses -- capped at ROUTES_MAX lines in all; the
channel table (channels.txt) is unchanged and the lines live in its .meta.json
sidecar next to the tvg-id and logo, which epg/logos still read as before;
saving the admin's table keeps, replaces, reorders or drops a channel's lines
and the record of which line last worked; the order lines are tried in (the
one that last worked first, a line that just failed last), a cycle that moves
on when a line fails to start or dies early and stays on a line that played a
while, and never loops without bound; ffmpeg's and HTTP's failures said in
plain Chinese without the raw text or any address; the light line check
(playlist + a few KB of one segment, no ffmpeg) against a local HTTP server
that answers 403, cuts a segment short, stalls, or works; the PC player's
stream switching lines inside one stream (notice, memory, all lines failing,
dying after it played) and the 503/410 answers that carry it; the TV media
server's channel start using the next line; and the admin check endpoint's
authorisation. Fastest line: the check stores how long a line took, real plays
(PC and TV, kept apart) record how long they took to start, and the order puts
the last good line first only while no other line is much faster (FASTER), then
the rest by speed; a failed check drops a line's start time; promoting a backup
to main on save forgets the last good line; the PC player can ask for one line
(route) and is told which line plays (answer and X-RTV-Route).
"""

from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from server import m3u, reconnect, routes
from server.relay import admin, channeltable, playlist, routecheck, watch

import test_relay_admin as admin_base     # functions only: inheriting the TestCase would run its tests twice
import test_relay_watch as watch_base     # the same
from test_relay_watch import INPUT_DUMP, LIVE_DATA, FakeProc


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------- import

class ImportTests(unittest.TestCase):
    def test_txt_hash_separated_lines_are_routes(self):
        text = "CCTV1,http://a/1.m3u8#http://b/1.m3u8$线路2#rtp://239.0.0.1:5000#http://a/1.m3u8\n"
        found = m3u.parse_playlist(text)
        self.assertEqual([(c["name"], c["url"], c["routes"]) for c in found],
                         [("CCTV1", "http://a/1.m3u8", ["http://b/1.m3u8"])])
        answer = playlist.read({"text": text})
        self.assertEqual(answer["entries"][0]["routes"], [{"url": "http://b/1.m3u8"}])

    def test_m3u_duplicates_merge_by_name_and_tvg_id(self):
        text = ("#EXTM3U\n"
                '#EXTINF:-1 tvg-id="CCTV2",CCTV-2\nhttps://a/2.m3u8\n'
                '#EXTINF:-1 http-user-agent="UA/2",cctv 2\nhttps://b/2.m3u8\n'
                '#EXTINF:-1 tvg-id="CCTV2",CCTV2 财经\nhttps://c/2.m3u8\n'
                "#EXTINF:-1,湖南卫视\nhttps://a/hn.m3u8\n"
                "#EXTINF:-1,CCTV-2\nhttps://a/2.m3u8\n")
        answer = playlist.read({"text": text})
        self.assertEqual([e["name"] for e in answer["entries"]], ["CCTV-2", "湖南卫视"])
        self.assertEqual(answer["entries"][0]["routes"],
                         [{"url": "https://b/2.m3u8", "ua": "UA/2"}, {"url": "https://c/2.m3u8"}],
                         "same name, then same tvg-id; the primary's own address is not repeated")
        self.assertEqual((answer["total"], answer["merged"]), (2, 3))

    def test_routes_are_capped(self):
        text = "#EXTM3U\n" + "".join(f"#EXTINF:-1,CCTV5\nhttps://h{n}/5.m3u8\n" for n in range(9))
        entry = playlist.read({"text": text})["entries"][0]
        self.assertEqual(len(entry["routes"]), routes.BACKUPS_MAX)
        self.assertEqual(1 + len(entry["routes"]), routes.ROUTES_MAX)

    def test_mark_names_the_matching_channel(self):
        entries = [{"name": "CCTV-1", "url": "https://other/1", "agent": ""},
                   {"name": "Two", "url": "https://x/2.m3u8", "agent": ""},
                   {"name": "新台", "url": "https://n/1", "agent": ""}]
        rows = [{"id": "ch000", "name": "CCTV1", "url": "https://x/1.m3u8", "agent": ""},
                {"id": "ch004", "name": "二台", "url": "https://x/2.m3u8", "agent": ""}]
        playlist.mark(entries, rows)
        self.assertEqual([(e["dup"], e["match"]) for e in entries], [("name", "ch000"), ("url", "ch004"), ("", "")])


# ---------------------------------------------------------------- the sidecar

class MetaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.table = Path(self.tmp.name) / "channels.txt"
        self.table.write_text("ch000 | CCTV1 | https://x/1.m3u8\nch001 | CCTV2 | https://x/2.m3u8 | UA/1\n",
                              encoding="utf-8")

    def rows(self):
        return channeltable.read(self.table)["rows"]

    def test_old_meta_and_channels_txt_still_read(self):
        channeltable.save_meta(self.table, {"ch000": {"tvg_id": "CCTV1"}})
        self.assertEqual(routes.table_routes(self.table, "ch000", "https://x/1.m3u8", "", "D"),
                         [("https://x/1.m3u8", "D")])
        routes.update_meta(self.table, lambda d: d["ch000"].update(routes=[{"url": "https://y/1.m3u8"}]))
        self.assertEqual(channeltable.meta_of(self.table), {"ch000": {"tvg_id": "CCTV1"}},
                         "epg and logos see only what they read before")
        from server import live
        channels, _, _ = live.parse_channels(self.table.read_text(encoding="utf-8"))
        self.assertEqual(list(channels), ["ch000", "ch001"], "channels.txt is the same file")

    def test_start_time_is_written_only_when_it_changes(self):
        self.assertTrue(routes.remember_start(self.table, "ch001", "https://y/2.m3u8", 4000))
        stamp = routes.meta_path(self.table).stat().st_mtime_ns
        self.assertFalse(routes.remember_start(self.table, "ch001", "https://y/2.m3u8", 4500), "within 20 %")
        self.assertEqual(routes.meta_path(self.table).stat().st_mtime_ns, stamp)
        self.assertTrue(routes.remember_start(self.table, "ch001", "https://y/2.m3u8", 6000))
        self.assertTrue(routes.remember_start(self.table, "ch001", "https://y/2.m3u8", 9000, "tv"))
        entry = routes.entry_of(self.table, "ch001")
        mark = routes.fingerprint("https://y/2.m3u8")
        self.assertEqual(entry["starts"], {"pc": {mark: 6000}, "tv": {mark: 9000}})
        self.assertFalse(routes.remember_start(self.table, "ch001", "https://y/2.m3u8", 0))
        self.assertFalse(routes.remember_start(self.table, "ch001", "https://y/2.m3u8", 100, "phone"))
        self.assertNotIn("https://", routes.meta_path(self.table).read_text(encoding="utf-8"))

    def test_promoting_a_backup_forgets_the_good_line_and_keeps_times(self):
        mark = routes.fingerprint
        routes.write_meta(self.table, {
            "ch000": {"routes": [{"url": "https://y/1.m3u8"}, {"url": "https://z/1.m3u8"}],
                      "good": mark("https://y/1.m3u8"),
                      "starts": {"pc": {mark("https://z/1.m3u8"): 3000, mark("https://gone"): 1},
                                 "xx": {mark("https://z/1.m3u8"): 1}}}})
        items = [{"id": "ch000", "url": "https://z/1.m3u8",
                  "routes": [{"url": "https://x/1.m3u8"}, {"url": "https://y/1.m3u8"}]}]
        new = channeltable.apply_edits(self.rows(), items)
        routes.update_meta(self.table, lambda d: channeltable.merge_meta(d, items, new))
        entry = routes.read_meta(self.table)["ch000"]
        self.assertNotIn("good", entry, "the admin chose a new main line: try it first")
        self.assertEqual(entry["starts"], {"pc": {mark("https://z/1.m3u8"): 3000}})
        # Moving only backups around keeps the good line.
        routes.update_meta(self.table, lambda d: d["ch000"].update(good=mark("https://y/1.m3u8")))
        items = [{"id": "ch000", "url": "https://z/1.m3u8",
                  "routes": [{"url": "https://y/1.m3u8"}, {"url": "https://x/1.m3u8"}]}]
        new = channeltable.apply_edits(self.rows(), items)
        routes.update_meta(self.table, lambda d: channeltable.merge_meta(d, items, new))
        self.assertEqual(routes.read_meta(self.table)["ch000"]["good"], mark("https://y/1.m3u8"))

    def test_check_keeps_times_and_drops_a_failed_lines_start(self):
        mark = routes.fingerprint
        routes.write_meta(self.table, {"ch000": {"routes": [{"url": "https://y/1.m3u8"}],
                                                 "starts": {"pc": {mark("https://x/1.m3u8"): 4000,
                                                                   mark("https://y/1.m3u8"): 5000}}}})
        answers = {"https://x/1.m3u8": {"ok": False, "why": "源站超时", "code": "timeout", "ms": 6000, "raw": "t"},
                   "https://y/1.m3u8": {"ok": True, "why": "", "code": "", "ms": 812}}
        with unittest.mock.patch.object(routecheck, "probe", lambda url, agent, opener=None: answers[url]):
            routecheck.check_channel(self.table, "ch000", clock=lambda: 7, log=lambda _m: None)
        entry = routes.entry_of(self.table, "ch000")
        self.assertEqual(entry["checks"][mark("https://y/1.m3u8")], {"ok": True, "why": "", "at": 7, "ms": 812})
        self.assertNotIn("ms", entry["checks"][mark("https://x/1.m3u8")])
        self.assertEqual(entry["starts"], {"pc": {mark("https://y/1.m3u8"): 5000}}, "the dead line's start goes")
        view = channeltable.routes_view(self.table, self.rows())["ch000"]
        self.assertEqual(view["checks"]["https://y/1.m3u8"]["ms"], 812)

    def test_merge_keeps_replaces_and_drops(self):
        mark = routes.fingerprint
        routes.write_meta(self.table, {
            "ch000": {"logo": "https://l/1.png", "routes": [{"url": "https://y/1.m3u8"}, {"url": "https://z/1.m3u8"}],
                      "good": mark("https://z/1.m3u8"),
                      "checks": {mark("https://y/1.m3u8"): {"ok": False, "why": "源站超时", "at": 5}}},
            "ch001": {"routes": [{"url": "https://y/2.m3u8"}]}})
        rows = self.rows()
        # ch000 unchanged (only its id), ch001 removed, a new channel with routes.
        items = ["ch000", {"name": "新台", "url": "https://n/1.m3u8", "routes": [{"url": "https://n/2.m3u8"}]}]
        new = channeltable.apply_edits(rows, items)
        routes.update_meta(self.table, lambda d: channeltable.merge_meta(d, items, new))
        data = routes.read_meta(self.table)
        self.assertEqual(set(data), {"ch000", new[1]["id"]})
        self.assertEqual(data["ch000"]["routes"], [{"url": "https://y/1.m3u8"}, {"url": "https://z/1.m3u8"}])
        self.assertEqual(data["ch000"]["good"], mark("https://z/1.m3u8"))
        self.assertEqual(data[new[1]["id"]], {"routes": [{"url": "https://n/2.m3u8"}]})
        # The page made the second backup the primary and dropped the other one.
        items = [{"id": "ch000", "url": "https://z/1.m3u8", "routes": [{"url": "https://x/1.m3u8"}]}]
        new = channeltable.apply_edits(self.rows(), items)
        routes.update_meta(self.table, lambda d: channeltable.merge_meta(d, items, new))
        data = routes.read_meta(self.table)
        self.assertEqual(data["ch000"]["routes"], [{"url": "https://x/1.m3u8"}])
        self.assertEqual(data["ch000"]["good"], mark("https://z/1.m3u8"))
        self.assertNotIn("checks", data["ch000"], "the check of a removed line goes with it")
        view = channeltable.routes_view(self.table, [dict(new[0])])
        self.assertEqual(view["ch000"]["good"], "https://z/1.m3u8")

    def test_bad_input_is_cleaned(self):
        self.assertEqual(routes.clean_routes([{"url": "ftp://x"}, {"url": "https://a b"}, {"url": "https://a|b"},
                                              "x", {"url": "https://p/1"}, {"url": "https://ok/1", "ua": "U|A"}],
                                             "https://p/1"), [{"url": "https://ok/1"}])
        self.assertEqual(routes.bad_routes([{"url": "https://ok"}, {"url": "rtsp://x"}]), [2])
        (Path(self.tmp.name) / "channels.txt.meta.json").write_text("not json", encoding="utf-8")
        self.assertEqual(routes.read_meta(self.table), {})
        self.assertEqual(routes.table_routes(self.table, "ch000", "https://x/1.m3u8"), [("https://x/1.m3u8", "")])

    def test_remember_good_writes_only_on_change(self):
        self.assertTrue(routes.remember_good(self.table, "ch001", "https://y/2.m3u8"))
        stamp = routes.meta_path(self.table).stat().st_mtime_ns
        self.assertFalse(routes.remember_good(self.table, "ch001", "https://y/2.m3u8"))
        self.assertEqual(routes.meta_path(self.table).stat().st_mtime_ns, stamp)
        self.assertEqual(routes.good_of(self.table, "ch001"), routes.fingerprint("https://y/2.m3u8"))
        self.assertNotIn("https://", routes.meta_path(self.table).read_text(encoding="utf-8"),
                         "only a fingerprint, no address")


# ---------------------------------------------------------------- order, cycle, words

class OrderTests(unittest.TestCase):
    LINES = [("https://a", "U"), ("https://b", "U"), ("https://c", "U")]

    def test_good_first_bad_last(self):
        good, bad = routes.fingerprint("https://c"), {routes.fingerprint("https://a")}
        self.assertEqual(routes.preferred(self.LINES), [0, 1, 2])
        self.assertEqual(routes.preferred(self.LINES, good), [2, 0, 1])
        self.assertEqual(routes.preferred(self.LINES, "", bad), [1, 2, 0])

    def test_speed_order_and_a_sticky_good_line(self):
        mark = routes.fingerprint
        a, b, c = (mark(u) for u, _ in self.LINES)
        self.assertEqual(routes.preferred(self.LINES, speeds={b: 500, c: 300}), [2, 1, 0],
                         "fastest first, unknown after, then table order")
        # The line that worked last time stays first unless another is at least 40 % faster.
        self.assertEqual(routes.preferred(self.LINES, a, speeds={a: 1000, c: 700}), [0, 2, 1])
        self.assertEqual(routes.preferred(self.LINES, a, speeds={a: 1000, c: 600}), [2, 0, 1])
        self.assertEqual(routes.preferred(self.LINES, a, speeds={c: 100}), [0, 2, 1], "good with no time stays")
        # A fast line that just failed neither jumps ahead nor unseats the good one.
        self.assertEqual(routes.preferred(self.LINES, a, {c}, speeds={a: 1000, c: 100}), [0, 1, 2])
        self.assertEqual(routes.RouteCycle(self.LINES, a, speeds={a: 900, b: 300}).current[0], "https://b")

    def test_speeds_from_checks_and_plays(self):
        mark = routes.fingerprint
        a, b, c = (mark(u) for u, _ in self.LINES)
        entry = {"checks": {a: {"ok": True, "ms": 800}, b: {"ok": False, "why": "x", "ms": 50}, c: {"ok": True}},
                 "starts": {"pc": {a: 4200, c: "bad"}, "tv": {b: 6000}}}
        self.assertEqual(routes.speeds_of(entry, "pc"), {a: 4200})
        self.assertEqual(routes.speeds_of(entry, "tv"), {a: 800, b: 6000})
        self.assertEqual(routes.speeds_of(None), {})
        self.assertEqual(routes.blend(None, 3000), 3000)
        self.assertEqual(routes.blend(3000, 5000), 4000)

    def test_cycle_moves_on_and_stays_after_a_long_play(self):
        now = [0.0]
        cycle = routes.RouteCycle(self.LINES, routes.fingerprint("https://b"), clock=lambda: now[0], early_s=45)
        self.assertEqual(cycle.current[0], "https://b")
        self.assertTrue(cycle.failed())                 # never started: next line
        self.assertEqual(cycle.current[0], "https://a")
        cycle.started()
        now[0] = 10
        self.assertTrue(cycle.failed())                 # died after 10 s: early, next line
        self.assertEqual(cycle.current[0], "https://c")
        cycle.started()
        now[0] = 100
        self.assertFalse(cycle.failed())                # played 90 s: retry the same line first
        self.assertEqual(cycle.current[0], "https://c")
        self.assertTrue(cycle.failed())                 # and if that fails, move on
        self.assertEqual(cycle.index, 1)

    def test_cycle_is_bounded(self):
        cycle = routes.RouteCycle(self.LINES)
        for _ in range(3):
            cycle.failed()
        self.assertTrue(cycle.exhausted)
        self.assertEqual(cycle.extra_s(20.0), 40.0)
        self.assertEqual(routes.RouteCycle([("u", "")] * 20).extra_s(20.0), 120.0, "capped")
        single = routes.RouteCycle([("https://a", "")])
        self.assertFalse(single.failed())
        self.assertEqual(single.extra_s(20.0), 0.0)

    def test_hold_deadline_is_bounded_by_the_routes(self):
        # reconnect.hold asks the server for the extra time; with no server hook it is the old 60 s.
        server = types.SimpleNamespace(route_extra_s=lambda: 40.0)
        self.assertEqual(getattr(server, "route_extra_s", lambda: 0.0)() + reconnect.RECONNECT_TIMEOUT_S,
                         reconnect.RECONNECT_TIMEOUT_S + 40.0)

    def test_failures_in_plain_chinese(self):
        cases = {
            "[https @ 0x7f] HTTP error 403 Forbidden for https://x/1.m3u8?token=1": "线路被源站拒绝（403），可能已失效",
            "Server returned 404 Not Found": "源站找不到这个台（404）",
            "Server returned 5XX Server Error reply": "源站出错（5xx），可能暂时不行",
            "Error reading HTTP response: Operation timed out": "源站超时",
            "Error reading HTTP response: End of file": "源站中途断开",
            "Connection reset by peer": "源站中途断开",
            "[http @ 0x1] Stream ends prematurely at 1500, should be 54144": "源站中途断开",
            "Failed to resolve hostname x.example: nodename nor servname provided": "找不到源站（域名解析不了）",
            "Connection refused": "源站拒绝连接",
            "Invalid data found when processing input": "源站给的不是能放的直播流",
            "something odd": "直播源打不开",
        }
        for raw, said in cases.items():
            self.assertEqual(routes.friendly(raw), said, raw)
            self.assertNotIn("http", routes.friendly(raw))
        noisy = ["[h264 @ 0x1] error while decoding MB 16 0", "[http @ 0x2] Stream ends prematurely at 1500",
                 "[h264 @ 0x1] concealing 240 DC"]
        self.assertEqual(routes.friendly(routes.telling(noisy)), "源站中途断开", "the telling line, not the noise")
        self.assertEqual(routes.telling(["nothing here"]), "")
        self.assertEqual(routes.redact("Opening 'https://a/b?sid=1' for reading"), "Opening '<地址> for reading")
        self.assertEqual(routes.switching("CCTV2", 1, "HTTP error 403", 2),
                         "CCTV2 线路 1 失效（线路被源站拒绝（403），可能已失效），正在换线路 2…")
        self.assertTrue(routes.all_failed("timed out", 3).startswith(routes.ALL_FAILED))


# ---------------------------------------------------------------- the light check

class FakeSource(http.server.BaseHTTPRequestHandler):
    """/ok.m3u8 → master → /v.m3u8 → /seg.ts (ok); /deny.m3u8 → 403; /cut.m3u8 → a segment cut short;
    /slow.m3u8 → stalls; /empty.m3u8 → no segments; /raw.ts → a TS stream (not a playlist)."""
    seen: list = []

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        FakeSource.seen.append((self.path, self.headers.get("User-Agent"), self.headers.get("Range")))
        if self.path == "/deny.m3u8":
            self.send_error(403)
            return
        if self.path == "/slow.m3u8":
            time.sleep(1.5)
            self.send_error(500)
            return
        if self.path == "/ok.m3u8":
            return self._send(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n")
        if self.path in ("/v.m3u8", "/cut.m3u8"):
            seg = "seg.ts" if self.path == "/v.m3u8" else "cutseg.ts"
            return self._send(f"#EXTM3U\n#EXTINF:2,\nold.ts\n#EXTINF:2,\n{seg}\n".encode())
        if self.path == "/empty.m3u8":
            return self._send(b"#EXTM3U\n#EXT-X-TARGETDURATION:2\n")
        if self.path == "/seg.ts":
            return self._send(b"G" * 30000)
        if self.path == "/raw.ts":
            return self._send(b"G" * 5000)
        if self.path == "/cutseg.ts":
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"G" * 100)
            self.wfile.flush()
            self.close_connection = True
            return
        self.send_error(404)

    def _send(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSource)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        # No proxy from the environment: the fake source is on this machine.
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def probe(self, path):
        return routecheck.probe(self.base + path, "UA/9", self.opener)

    def test_ok_follows_the_master_and_reads_a_few_kb_of_the_newest_segment(self):
        FakeSource.seen.clear()
        found = self.probe("/ok.m3u8")
        self.assertTrue(found["ok"], found)
        self.assertEqual([p for p, _, _ in FakeSource.seen], ["/ok.m3u8", "/v.m3u8", "/seg.ts"])
        self.assertEqual(FakeSource.seen[-1][1:], ("UA/9", f"bytes=0-{routecheck.SEGMENT_BYTES - 1}"))
        self.assertTrue(self.probe("/raw.ts")["ok"], "a stream that is not a playlist")

    def test_failures(self):
        self.assertEqual(self.probe("/deny.m3u8")["why"], "线路被源站拒绝（403），可能已失效")
        self.assertEqual(self.probe("/nothing.m3u8")["why"], "源站找不到这个台（404）")
        self.assertEqual(self.probe("/cut.m3u8")["why"], "源站中途断开")
        self.assertEqual(self.probe("/empty.m3u8")["why"], "源站的列表里没有能放的片段")
        with unittest.mock.patch.object(routecheck, "TIMEOUT_S", 0.5):
            self.assertEqual(self.probe("/slow.m3u8")["why"], "源站超时")
        dead = routecheck.probe("http://127.0.0.1:9/x.m3u8", "", self.opener)
        self.assertFalse(dead["ok"])
        self.assertNotIn("127.0.0.1", dead["why"] + dead["raw"])

    def test_check_channel_stores_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            table.write_text(f"ch000 | CCTV2 | {self.base}/deny.m3u8\n", encoding="utf-8")
            routes.write_meta(table, {"ch000": {"routes": [{"url": f"{self.base}/ok.m3u8"}]}})
            logs = []
            answer = routecheck.check_channel(table, "ch000", self.opener, clock=lambda: 1234, log=logs.append)
            self.assertEqual([(r["ok"], r["why"]) for r in answer["results"]],
                             [(False, "线路被源站拒绝（403），可能已失效"), (True, "")])
            view = channeltable.routes_view(table, channeltable.read(table)["rows"])["ch000"]
            found = view["checks"][f"{self.base}/ok.m3u8"]
            self.assertEqual({k: found[k] for k in ("ok", "why", "at")}, {"ok": True, "why": "", "at": 1234})
            self.assertGreater(found["ms"], 0, "how long it took to reach the first media bytes")
            self.assertNotIn("ms", view["checks"][f"{self.base}/deny.m3u8"], "no time for a line that failed")
            self.assertFalse(any(self.base in line for line in logs), "no address in the log")
            self.assertFalse(routecheck.check_channel(table, "ch404", self.opener)["ok"])


# ---------------------------------------------------------------- the PC player

class ByInput:
    def __init__(self, default: dict, **by_url):
        self.default = default
        self.by_url = by_url
        self.procs: list[FakeProc] = []

    def __call__(self, args, **_kwargs):
        given = args[args.index("-i") + 1]
        proc = FakeProc(args, **self.by_url.get(given, self.default))
        self.procs.append(proc)
        return proc

    def inputs(self) -> list[str]:
        return [p.args[p.args.index("-i") + 1] for p in self.procs]


DENY = {"lines": ["[https @ 0x1] HTTP error 403 Forbidden", "Conversion failed!"], "hold": False, "code": 1}
TIMEOUT = {"lines": ["[https @ 0x1] Error reading HTTP response: Operation timed out"], "hold": False, "code": 1}
GOOD = {"data": LIVE_DATA}
LINES = [("https://a/2.m3u8", "UA"), ("https://b/2.m3u8", "UA"), ("https://c/2.m3u8", "UA")]


def live_source(lines=LINES, cid="ch002"):
    return watch.Source(watch.live_key(cid, lines), url=lines[0][0], user_agent=lines[0][1], label="CCTV2",
                        paced=False, routes=tuple(lines))


class PlayerFailoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.table = Path(self.tmp.name) / "channels.txt"
        self.table.write_text("ch002 | CCTV2 | https://a/2.m3u8 | UA\n", encoding="utf-8")
        self.logs = []
        self.service = watch.WatchService("ffmpeg", b"k" * 32, log=self.logs.append, background=False)
        self.service.routes_path = lambda: self.table
        self.addCleanup(self.service.stop_all)

    def test_single_line_key_is_unchanged(self):
        self.assertEqual(watch.live_key("ch000", [("u", "a")]), ("live", "ch000", "u", "a"))
        self.assertEqual(watch.legs_of(watch.Source(("live", "c", "u", "a"), url="u"))[0][1].key, ("live", "c", "u", "a"))

    def test_next_line_after_a_dead_one_and_remembered(self):
        self.service.popen = ByInput(GOOD, **{"https://a/2.m3u8": DENY, "https://b/2.m3u8": TIMEOUT})
        stream = self.service.open("pc1-a", live_source(), user=1)
        self.assertTrue(stream.wait_ready(5) or stream.wait_ready(5) or stream.wait_ready(5))
        self.assertEqual(self.service.popen.inputs(), [u for u, _ in LINES])
        self.assertEqual(stream.route, 2)
        self.assertEqual(stream.notice, "", "cleared once the line plays")
        self.assertEqual(routes.good_of(self.table, "ch002"), routes.fingerprint("https://c/2.m3u8"),
                         "persisted in the sidecar")
        self.assertFalse(any("https://" in line for line in self.logs), self.logs)
        self.assertTrue(any("线路 1 不行" in line for line in self.logs), self.logs)
        # Next time the line that worked is tried first.
        self.service.close("pc1-a")
        self.service.popen = ByInput(GOOD)
        again = self.service.open("pc1-a", live_source(), user=1)
        self.assertTrue(again.wait_ready(5))
        self.assertEqual(self.service.popen.inputs(), ["https://c/2.m3u8"])

    def test_notice_while_switching(self):
        gate = threading.Event()
        slow_good = dict(GOOD)

        class Gated(ByInput):
            def __call__(self, args, **kwargs):
                if args[args.index("-i") + 1] == "https://b/2.m3u8":
                    gate.wait(5)
                return super().__call__(args, **kwargs)

        self.service.popen = Gated(slow_good, **{"https://a/2.m3u8": DENY})
        stream = self.service.open("pc1-a", live_source(), user=1)
        self.assertTrue(wait_for(lambda: stream.notice))
        self.assertEqual(stream.notice, "CCTV2 线路 1 失效（线路被源站拒绝（403），可能已失效），正在换线路 2…")
        handler = FakeHandler()
        with unittest.mock.patch.object(watch, "READY_WAIT_S", 0.2):
            watch.get(handler, types.SimpleNamespace(watch=self.service),
                      f"/watch/{stream.sid}/{self.service.token(stream)}/index.m3u8")
        self.assertEqual(handler.sent[0], 503)
        self.assertEqual(handler.sent[1].decode(), stream.notice)
        self.assertEqual(handler.sent[3], {"X-RTV-Notice": "1"})
        gate.set()
        self.assertTrue(stream.wait_ready(5))

    def test_all_lines_failing_is_bounded_and_said_plainly(self):
        self.service.popen = ByInput(DENY)
        stream = self.service.open("pc1-a", live_source(), user=1)
        stream._thread.join(5)
        self.assertFalse(stream.alive)
        self.assertEqual(len(self.service.popen.procs), 3, "one ffmpeg per line, no loop")
        self.assertTrue(stream.error.startswith(routes.ALL_FAILED), stream.error)
        self.assertNotIn("HTTP error", stream.error)
        self.assertFalse(stream.reopen)
        # The failed lines go last next time (in memory).
        self.assertEqual(self.service.routes.order("ch002", LINES), [0, 1, 2])

    def test_copy_failure_retry_is_capped_per_line(self):
        silent = {"lines": INPUT_DUMP, "hold": False, "code": 1}
        self.service.popen = ByInput(silent)
        stream = self.service.open("pc1-a", live_source(LINES[:2]), user=1)
        stream._thread.join(5)
        self.assertLessEqual(len(self.service.popen.procs), 2 * watch.LEG_TRIES)
        self.assertFalse(stream.alive)

    def test_dying_after_it_played_asks_the_page_to_reopen(self):
        self.service.popen = ByInput({"data": LIVE_DATA, "hold": False, "code": 1,
                                      "lines": INPUT_DUMP + ["Error reading HTTP response: End of file"]})
        stream = self.service.open("pc1-a", live_source(), user=1)
        stream._thread.join(5)
        self.assertTrue(stream.reopen)
        self.assertEqual(stream.error, "源站中途断开，正在换线路…")
        # The segments it made are still served; the playlist ends with the reopen tag.
        text = stream.window.playlist()
        self.assertIn("#EXT-X-RTV-REOPEN\n#EXT-X-ENDLIST", text)
        # Once the segments are gone a poll gets 410 with the same request.
        handler = FakeHandler()
        stream.window.release()
        watch.get(handler, types.SimpleNamespace(watch=self.service),
                  f"/watch/{stream.sid}/{self.service.token(stream)}/index.m3u8")
        self.assertEqual((handler.sent[0], handler.sent[3]), (410, {"X-RTV-Reopen": "1"}))
        self.assertEqual(self.service.routes.order("ch002", LINES), [1, 2, 0], "the line that died goes last")


    def test_first_segment_records_how_long_the_line_took(self):
        self.service.popen = ByInput(GOOD)
        with unittest.mock.patch.object(self.service.routes, "ok", wraps=self.service.routes.ok) as ok:
            stream = self.service.open("pc1-a", live_source(), user=1)
            self.assertTrue(stream.wait_ready(5))
        cid, url, count, ms = ok.call_args.args
        self.assertEqual((cid, url, count), ("ch002", "https://a/2.m3u8", 3))
        self.assertGreaterEqual(ms, 0)
        # In memory and in the sidecar (blended with what was there, written when it moved enough).
        memory = watch.RouteMemory(lambda: self.table)
        memory.ok("ch002", "https://b/2.m3u8", 3, 4000)
        memory.ok("ch002", "https://b/2.m3u8", 3, 2000)
        mark = routes.fingerprint("https://b/2.m3u8")
        self.assertEqual(memory._starts[("ch002", mark)], 3000)
        self.assertEqual(routes.starts_in(routes.entry_of(self.table, "ch002"))[mark], 3000)
        fresh = watch.RouteMemory(lambda: self.table)
        fresh.ok("ch002", "https://b/2.m3u8", 3, 1000)
        self.assertEqual(fresh._starts[("ch002", mark)], 2000, "carries on from the sidecar after a restart")
        # Single-line channels keep no times.
        fresh.ok("ch009", "https://z/9.m3u8", 1, 1000)
        self.assertNotIn(("ch009", routes.fingerprint("https://z/9.m3u8")), fresh._starts)

    def test_a_much_faster_line_goes_first(self):
        mark = routes.fingerprint
        routes.write_meta(self.table, {"ch002": {"good": mark("https://a/2.m3u8"),
                                                 "starts": {"pc": {mark("https://a/2.m3u8"): 9000,
                                                                   mark("https://c/2.m3u8"): 3000}}}})
        self.assertEqual(self.service.routes.order("ch002", LINES), [2, 0, 1])
        self.assertEqual(self.service.routes.order("ch002", LINES, first=1), [1, 2, 0], "the page's pick first")
        # A promoted main line (good cleared by the admin save) is not overridden by a stale in-memory good.
        self.service.routes.ok("ch002", "https://a/2.m3u8", 3)
        routes.update_meta(self.table, lambda d: d["ch002"].pop("good"))
        self.assertEqual(self.service.routes.order("ch002", LINES)[0], 2)

    def test_the_page_picks_a_line(self):
        self.service.popen = ByInput(GOOD)
        stream = self.service.open("pc1-a", live_source(), user=1, keep_old=True, route=2)
        self.assertEqual(stream.route, 2, "known before the thread runs")
        self.assertTrue(stream.wait_ready(5))
        self.assertEqual(self.service.popen.inputs(), ["https://c/2.m3u8"])
        self.assertIs(self.service.open("pc1-a", live_source(), user=1, keep_old=True, route=2), stream,
                      "already on that line: keep it")
        self.assertIs(self.service.open("pc1-a", live_source(), user=1, keep_old=True), stream)
        handler = FakeHandler()
        watch.get(handler, types.SimpleNamespace(watch=self.service),
                  f"/watch/{stream.sid}/{self.service.token(stream)}/index.m3u8")
        self.assertEqual((handler.sent[0], handler.sent[3]), (200, {"X-RTV-Route": "3"}))
        other = self.service.open("pc1-a", live_source(), user=1, keep_old=True, route=0)
        self.assertIsNot(other, stream)
        self.assertFalse(stream.alive, "the old line stops (not parked as a warm stream)")
        self.assertTrue(other.wait_ready(5))
        self.assertEqual(self.service.popen.inputs()[-1], "https://a/2.m3u8")
        self.assertEqual(routes.good_of(self.table, "ch002"), routes.fingerprint("https://a/2.m3u8"))
        # Out of range: ignored.
        self.assertIs(self.service.open("pc1-a", live_source(), user=1, route=7), other)

    def test_a_picked_line_that_fails_falls_back(self):
        self.service.popen = ByInput(GOOD, **{"https://b/2.m3u8": DENY})
        stream = self.service.open("pc1-a", live_source(), user=1, route=1)
        self.assertTrue(stream.wait_ready(5) or stream.wait_ready(5))
        self.assertEqual(self.service.popen.inputs(), ["https://b/2.m3u8", "https://a/2.m3u8"])
        self.assertEqual(stream.route, 0)
        lines = self.service.routes.lines("ch002", LINES)
        self.assertEqual(lines[1], {"bad": True})
        self.assertEqual(set(lines[0]) - {"ms"}, set(), "the line playing has its time, if any")
        self.assertEqual(lines[2], {})
        self.assertNotIn("https://", json.dumps(lines))


class FakeHandler:
    def __init__(self):
        self.sent = None

    def _send(self, code, body, kind, headers=None):
        self.sent = (code, body, kind, headers)

    def _not_found(self):
        self.sent = (404, b"", "", None)


# ---------------------------------------------------------------- the TV media server

class TvServerTests(unittest.TestCase):
    def test_make_channel_uses_the_current_line(self):
        from server import live, tv_server
        made = []
        fake = types.SimpleNamespace(channel_name="ch002", ffmpeg="ff", logger=lambda m: made.append(("log", m)),
                                     channel_factory=lambda url, ff, ua: made.append((url, ua)) or (url, ua))
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            table.write_text("ch002 | CCTV2 | https://a/2.m3u8\n", encoding="utf-8")
            routes.write_meta(table, {"ch002": {"routes": [{"url": "https://b/2.m3u8", "ua": "UB"}],
                                                "good": routes.fingerprint("https://b/2.m3u8")}})
            with unittest.mock.patch.dict(live._LOADED, {"main": table}), \
                    unittest.mock.patch.dict(tv_server.CHANNELS, {"ch002": "https://a/2.m3u8"}), \
                    unittest.mock.patch.dict(tv_server.CHANNEL_AGENTS, {"ch002": "UA"}):
                first = tv_server.AVServer._start_routes(fake, "ch002")
                self.assertEqual(first, ("https://b/2.m3u8", "UB"), "the line that worked last time first")
                tv_server.AVServer.route_failed(fake, "start_failed")
                self.assertEqual(tv_server.AVServer.make_channel(fake), ("https://a/2.m3u8", "UA"))
                self.assertTrue(any("route 2/2 failed" in str(m) and "trying route 1" in str(m) for m in made))
                self.assertFalse(any("https://" in m[1] for m in made if m[0] == "log"))
                tv_server.AVServer.route_ok(fake)
                self.assertEqual(routes.good_of(table, "ch002"), routes.fingerprint("https://a/2.m3u8"))
                self.assertNotIn("starts", routes.entry_of(table, "ch002"), "a start after a switch is not timed")
                # A clean start is timed (kept apart from the PC's), and a much faster line goes first next time.
                tv_server.AVServer._start_routes(fake, "ch002")
                fake._route_began -= 9.0
                tv_server.AVServer.route_ok(fake)
                played = routes.starts_in(routes.entry_of(table, "ch002"), "tv")
                self.assertGreaterEqual(played[routes.fingerprint("https://a/2.m3u8")], 9000)
                self.assertEqual(routes.starts_in(routes.entry_of(table, "ch002"), "pc"), {})
                routes.update_meta(table, lambda d: d["ch002"]["starts"]["tv"].update(
                    {routes.fingerprint("https://b/2.m3u8"): 2000}))
                self.assertEqual(tv_server.AVServer._start_routes(fake, "ch002"), ("https://b/2.m3u8", "UB"))
                self.assertEqual(tv_server.AVServer.route_extra_s(fake), reconnect.ATTEMPT_TIMEOUT_S)
                fake.channel_name = "ch003"     # another channel: no lines of its own
                with unittest.mock.patch.dict(tv_server.CHANNELS, {"ch003": "https://z/3.m3u8"}):
                    self.assertEqual(tv_server.AVServer.make_channel(fake)[0], "https://z/3.m3u8")

    def test_hold_moves_to_the_next_line_and_gives_up(self):
        """reconnect.hold with a source that never starts: every failed restart asks the server to move on,
        and the session still ends at the (extended) deadline."""
        from server.live import LiveError

        class Never:
            palette = bytes(768)
            shown = None

            def build_palette(self):
                return self.palette

            def start(self):
                raise LiveError("no source")

            def failure(self):
                return None

            def close(self):
                pass

        calls = []
        clock = [1000.0]

        server = types.SimpleNamespace(
            stop=threading.Event(), phase="", logger=lambda m: None, frame_deadline_s=1.0, video_sent=0,
            make_channel=Never, route_failed=lambda reason: calls.append(reason),
            route_ok=lambda: calls.append("ok"), route_extra_s=lambda: 4.0,
            _wait_until=lambda *_a: True)
        with unittest.mock.patch.object(reconnect, "RECONNECT_TIMEOUT_S", 2.0), \
                unittest.mock.patch.object(reconnect, "RETRY_DELAY_S", 0.0), \
                unittest.mock.patch.object(reconnect, "_send_notice", lambda *a, **k: None), \
                unittest.mock.patch.object(reconnect, "_send_silence", lambda *a, **k: None), \
                unittest.mock.patch.object(reconnect, "_progress_stopped", lambda *a, **k: None), \
                unittest.mock.patch.object(reconnect, "_tell_device", lambda *a, **k: None), \
                unittest.mock.patch.object(reconnect.time, "monotonic", lambda: clock[0]):
            def tick(*_a):
                clock[0] += 0.05
                return True
            server._wait_until = tick
            wire = reconnect.WireState(seq=2, audio_pts=0)
            with self.assertRaises(LiveError):
                reconnect.hold(server, None, 1, Never(), wire, clock[0], clock[0], restart_reason="start_failed")
        self.assertGreater(len(calls), 3, "each failed restart reported")
        self.assertNotIn("ok", calls)
        self.assertLessEqual(clock[0] - 1000.0, 2.0 + 4.0 + 0.2, "ends at the extended deadline")


class RelayLineTests(unittest.TestCase):
    """POST /api/watch {channel, viewer, route}: the answer says how many lines, which one plays, and each line's
    time or trouble (no addresses)."""
    selfhost = True
    setUp = watch_base.RelayWatchTests.setUp
    tearDown = watch_base.RelayWatchTests.tearDown
    online = watch_base.RelayWatchTests.online
    request = watch_base.RelayWatchTests.request

    def watch(self, **extra):
        status, _, raw = self.request("POST", "/tv/api/watch", "alice", {"channel": "ch000", "viewer": "c0ffee00",
                                                                         **extra})
        self.assertEqual(status, 200)
        return json.loads(raw)

    def test_answer_and_picking_a_line(self):
        mark = routes.fingerprint
        routes.write_meta(self.relay.main_channels, {"ch000": {
            "routes": [{"url": "https://y/1.m3u8"}],
            "checks": {mark("https://y/1.m3u8"): {"ok": True, "why": "", "at": 1, "ms": 900}}}})
        answer = self.watch()
        self.assertEqual((answer["routes"], answer["route"]), (2, 1), "the measured line first")
        self.assertEqual(answer["lines"], [{}, {"ms": 900}])
        self.assertNotIn("https://", json.dumps(answer))
        picked = self.watch(route=0)
        self.assertEqual(picked["route"], 0)
        self.assertNotEqual(picked["sid"], answer["sid"], "a new stream on the picked line")
        self.assertEqual(self.watch(route=0)["sid"], picked["sid"], "same line again: same stream")
        for bad in (-1, "1", True, 1.5):
            self.assertFalse(self.watch(route=bad)["ok"], bad)
        # One-line channels have no menu.
        single = json.loads(self.request("POST", "/tv/api/watch", "alice",
                                         {"channel": "ch001", "viewer": "c0ffee00", "route": 1})[2])
        self.assertEqual(single["routes"], 1)
        self.assertNotIn("lines", single)


# ---------------------------------------------------------------- admin

class AdminRouteTests(unittest.TestCase):
    setUp = admin_base.AdminApiTests.setUp
    tearDown = admin_base.AdminApiTests.tearDown
    raw = admin_base.AdminApiTests.raw
    call = admin_base.AdminApiTests.call

    def test_check_is_admin_only(self):
        probed = []
        fake = lambda url, agent, opener=None: probed.append(url) or {"ok": True, "why": "", "code": "", "ms": 3}  # noqa: E731
        with unittest.mock.patch.object(routecheck, "probe", fake):
            for who in ("alice", "guest", "nobody"):
                status, _ = self.call(who, "POST", "/api/admin/channels/check", {"id": "ch000"})
                self.assertEqual(status, 404, who)
            self.assertEqual(probed, [])
            status, answer = self.call("admin", "POST", "/api/admin/channels/check", {"id": "ch000"})
        self.assertEqual(status, 200)
        self.assertEqual([r["url"] for r in answer["results"]], ["https://x/1.m3u8"])
        status, answer = self.call("admin", "POST", "/api/admin/channels/check", {"id": ["x"]})
        self.assertEqual(status, 400)

    def test_save_and_show_routes(self):
        version = self.call("admin", "GET", "/api/admin/channels")[1]["version"]
        rows = ["ch000", {"id": "ch001", "routes": [{"url": "https://y/2.m3u8", "ua": "U2"}]}, "ch007"]
        status, answer = self.call("admin", "POST", "/api/admin/channels", {"version": version, "rows": rows})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["summary"]["routes"], 1)
        table = self.call("admin", "GET", "/api/admin/channels")[1]
        row = next(r for r in table["rows"] if r["id"] == "ch001")
        self.assertEqual(row["routes"], [{"url": "https://y/2.m3u8", "ua": "U2"}])
        self.assertEqual(table["routes_max"], routes.ROUTES_MAX)
        self.assertEqual(self.main.read_text(encoding="utf-8").count("https://y/2.m3u8"), 0, "not in channels.txt")
        bad = ["ch000", {"id": "ch001", "routes": [{"url": "rtsp://y"}]}, "ch007"]
        answer = self.call("admin", "POST", "/api/admin/channels", {"version": table["version"], "rows": bad})[1]
        self.assertFalse(answer["ok"])
        self.assertIn("备用线路 1 的地址不对", answer["error"])
        many = ["ch000", {"id": "ch001", "routes": [{"url": f"https://y/{n}"} for n in range(6)]}, "ch007"]
        answer = self.call("admin", "POST", "/api/admin/channels", {"version": table["version"], "rows": many})[1]
        self.assertIn("备用线路最多", answer["error"])


if __name__ == "__main__":
    unittest.main()
