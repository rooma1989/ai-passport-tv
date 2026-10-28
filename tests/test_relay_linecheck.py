"""Background line check and live covers (server/relay/linecheck.py).

What is checked, with a fake clock, a fake line check and a fake ffmpeg (nothing touches the network):
- Schedule: a line never checked waits for the start delay plus a jitter, then goes first; a line that
  failed is checked again after BAD_EVERY_S, a good one after OK_EVERY_S; never-checked before bad before
  the oldest good one; results land in the same meta `checks` as the admin's check.
- At most WORKERS checks run at once; nothing runs while the machine plays PAUSE_STREAMS streams or more.
- A channel whose lines all failed is reported as dead (for the Live page and the admin line), and a line
  that later plays clears it.
- A definitive failure while playing (403/404) marks the line bad at once, on the PC (watch.py, one line
  or several) and on the TV (tv_server.route_dead); the next start tries it last; the TV's RouteCycle
  puts checked-bad lines last too. URLs never reach the log.
- Covers: one ffmpeg at a time, a gap between grabs, at most once per COVER_EVERY_S per channel, none
  while the PC is playing; the store keeps only JPEGs, bounded in count and bytes, and drops covers of
  channels no longer in the table.
- GET /api/art/live/<id> needs a session (guests too, like logos), answers 404 for unknown or malformed
  ids, and /api/state and /api/home carry the cover stamps and dead list only on a self-hosted relay.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import routes
from server.relay import linecheck, setup, watch, web
from server.tenancy.accounts import Accounts

from test_live_routes import ByInput, live_source
from test_relay_watch import LIVE_DATA, Popen
from test_relay_web import KEY, FakeClient, FakeYouTube

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 400
TABLE = ("ch000 | CCTV1 | https://a/1.m3u8\n"
         "ch001 | 湖南卫视 | https://b/1.m3u8\n")


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeProbe:
    """routecheck.probe stand-in: answers by URL (ok unless listed in bad), remembers what it was asked."""

    def __init__(self, bad=()):
        self.bad = set(bad)
        self.asked: list[str] = []

    def __call__(self, url, agent=""):
        self.asked.append(url)
        if url in self.bad:
            return {"ok": False, "why": "源站找不到这个台（404）", "code": "404", "ms": 50,
                    "raw": "HTTP error 404"}
        return {"ok": True, "why": "", "code": "", "ms": 300}


class Deferred:
    """spawn stand-in that only queues the jobs; run() runs them (to see how many start at once)."""

    def __init__(self):
        self.jobs = []

    def __call__(self, fn, *args):
        self.jobs.append((fn, args))

    def run(self):
        jobs, self.jobs = self.jobs, []
        for fn, args in jobs:
            fn(*args)
        return len(jobs)


def now_spawn(fn, *args):
    fn(*args)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.table = self.root / "channels.txt"
        self.table.write_text(TABLE, encoding="utf-8")
        routes.write_meta(self.table, {"ch000": {"routes": [{"url": "https://a/2.m3u8"}]}})
        self.clock = Clock()
        self.logs: list[str] = []
        self.load = (0, 0)
        self.grabbed: list[str] = []

    def checker(self, probe=None, spawn=now_spawn, grab=None, covers=True):
        def fake_grab(_ffmpeg, url, _agent, lenient=False):
            self.grabbed.append(url)
            return JPEG
        self.covers = linecheck.CoverStore(self.root / "covers" / "live", monotonic=self.clock) if covers else None
        checker = linecheck.LineChecker(lambda: self.table, self.covers, "ffmpeg", load=lambda: self.load,
                                        probe=probe or FakeProbe(), grab=grab or fake_grab, clock=self.clock,
                                        rand=lambda: 0.5, log=self.logs.append, spawn=spawn,
                                        gate=threading.BoundedSemaphore(2), pause=lambda _s: None)
        checker.plan()          # the first tick at start-up: the new lines get their time
        return checker

    def checks(self, cid):
        return routes.entry_of(self.table, cid).get("checks", {})


class ScheduleTests(Base):
    def test_new_lines_wait_for_the_start_delay_and_jitter_then_get_checked(self):
        probe = FakeProbe()
        checker = self.checker(probe)
        self.assertEqual(checker.tick(), 0, "nothing during the start delay")
        self.clock.now += linecheck.START_DELAY_S
        self.assertEqual(checker.tick(), 0, "the jitter (rand 0.5) is not over yet")
        self.clock.now += linecheck.NEW_JITTER_S / 2 + 1
        checker.tick()
        self.assertEqual(len(probe.asked), 3, "a free worker takes the next line without waiting for a tick")
        checker.tick()
        self.assertEqual(len(probe.asked), 3, "checked lines are not due again")
        self.assertEqual(sorted(probe.asked), ["https://a/1.m3u8", "https://a/2.m3u8", "https://b/1.m3u8"])
        found = self.checks("ch000")[routes.fingerprint("https://a/1.m3u8")]
        self.assertEqual((found["ok"], found["ms"], found["at"]), (True, 300, int(self.clock.now)))
        self.assertEqual(checker.status()["checked"], 2)
        self.assertEqual(checker.status()["last"], int(self.clock.now))

    def test_bad_lines_again_after_half_an_hour_good_ones_after_hours(self):
        probe = FakeProbe(bad={"https://b/1.m3u8"})
        checker = self.checker(probe)
        self.clock.now += linecheck.START_DELAY_S + linecheck.NEW_JITTER_S
        for _ in range(3):
            checker.tick()
        probe.asked.clear()
        self.clock.now += linecheck.BAD_EVERY_S + 1
        checker.tick()
        self.assertEqual(probe.asked, ["https://b/1.m3u8"], "only the bad line is due")
        probe.asked.clear()
        self.clock.now += linecheck.OK_EVERY_S
        checker.tick()
        self.assertEqual(probe.asked[0], "https://b/1.m3u8", "bad before good when both are due")
        self.assertEqual(len(probe.asked), 3)

    def test_never_checked_first_then_bad_then_oldest(self):
        at = int(self.clock.now)
        routes.write_meta(self.table, {"ch000": {
            "routes": [{"url": "https://a/2.m3u8"}],
            "checks": {routes.fingerprint("https://a/1.m3u8"): {"ok": True, "why": "", "at": at - 9 * 3600},
                       routes.fingerprint("https://a/2.m3u8"): {"ok": False, "why": "x", "at": at - 3600}}}})
        checker = self.checker()
        self.clock.now += linecheck.START_DELAY_S + linecheck.NEW_JITTER_S
        plan = [job[5] for job in checker.plan()]
        self.assertEqual(plan, ["https://b/1.m3u8", "https://a/2.m3u8", "https://a/1.m3u8"])

    def test_at_most_two_at_once(self):
        spawn = Deferred()
        checker = self.checker(spawn=spawn)
        self.clock.now += linecheck.START_DELAY_S + linecheck.NEW_JITTER_S
        self.assertEqual(checker.tick(), 2)
        self.assertEqual(checker.tick(), 0, "both workers still busy")
        self.assertEqual(spawn.run(), 2)
        self.assertEqual(len(spawn.jobs), 1, "the third line as soon as a worker is free")
        self.assertEqual(checker.tick(), 0, "and only once")
        spawn.run()
        checker.tick()
        self.assertEqual(spawn.jobs, [], "all done")

    def test_paused_while_the_machine_is_busy(self):
        probe = FakeProbe()
        checker = self.checker(probe)
        self.clock.now += linecheck.START_DELAY_S + linecheck.NEW_JITTER_S
        self.load = (linecheck.PAUSE_STREAMS, 1)
        self.assertEqual(checker.tick(), 0)
        self.assertTrue(checker.status()["paused"])
        self.assertEqual(probe.asked, [])
        self.load = (linecheck.PAUSE_STREAMS - 1, 1)
        self.assertGreater(checker.tick(), 0)
        self.assertFalse(checker.status()["paused"])

    def test_dead_channel_reported_and_cleared_by_a_play(self):
        probe = FakeProbe(bad={"https://b/1.m3u8"})
        checker = self.checker(probe)
        self.clock.now += linecheck.START_DELAY_S + linecheck.NEW_JITTER_S
        for _ in range(3):
            checker.tick()
        checker.plan()
        self.assertEqual(checker.status()["dead"], ["ch001"])
        self.assertEqual(len([m for m in self.logs if "湖南卫视 线路 1 不通" in m]), 1)
        self.assertFalse(any("https://" in m for m in self.logs), self.logs)
        # Logged when it goes bad, not on every re-check.
        self.clock.now += linecheck.BAD_EVERY_S + 1
        checker.tick()
        self.assertEqual(len([m for m in self.logs if "不通" in m]), 1)
        # It plays after all (the TV or the PC says so): no longer dead.
        routes.mark_ok(self.table, "ch001", "https://b/1.m3u8", self.clock.now)
        checker.plan()
        self.assertEqual(checker.status()["dead"], [])

    def test_lines_removed_from_the_table_are_forgotten(self):
        checker = self.checker(spawn=Deferred())
        checker.plan()
        self.assertEqual(len(checker._first), 3)
        self.table.write_text("ch000 | CCTV1 | https://a/1.m3u8\n", encoding="utf-8")
        checker.plan()
        self.assertEqual(len(checker._first), 2)


class DefinitiveFailureTests(Base):
    def test_routes_helpers(self):
        self.assertTrue(routes.definitive("Server returned 404 Not Found"))
        self.assertTrue(routes.definitive("HTTP error 403 Forbidden"))
        self.assertFalse(routes.definitive("Operation timed out"))
        routes.write_meta(self.table, {"ch000": {"routes": [{"url": "https://a/2.m3u8"}],
                                                 "good": routes.fingerprint("https://a/1.m3u8"),
                                                 "starts": {"pc": {routes.fingerprint("https://a/1.m3u8"): 900}}}})
        routes.mark_failed(self.table, "ch000", "https://a/1.m3u8", "源站找不到这个台（404）", 5)
        entry = routes.entry_of(self.table, "ch000")
        self.assertEqual(routes.bad_in(entry), {routes.fingerprint("https://a/1.m3u8")})
        self.assertNotIn("good", entry, "the bad line is no longer the one to try first")
        self.assertNotIn("starts", entry)
        lines = [("https://a/1.m3u8", ""), ("https://a/2.m3u8", "")]
        self.assertEqual(routes.RouteCycle(lines, bad=routes.bad_in(entry)).current[0], "https://a/2.m3u8")
        self.assertTrue(routes.mark_ok(self.table, "ch000", "https://a/1.m3u8", 6))
        self.assertEqual(routes.bad_in(routes.entry_of(self.table, "ch000")), set())
        self.assertFalse(routes.mark_ok(self.table, "ch000", "https://a/1.m3u8", 7), "nothing to write")

    def service(self):
        service = watch.WatchService("ffmpeg", b"k" * 32, log=self.logs.append, background=False)
        service.routes_path = lambda: self.table
        self.addCleanup(service.stop_all)
        return service

    def test_pc_marks_a_404_line_bad_at_once_even_with_one_line(self):
        service = self.service()
        service.routes.checking = True
        service.popen = ByInput({"lines": ["[https @ 0x1] HTTP error 404 Not Found"], "hold": False, "code": 1})
        one = [("https://b/1.m3u8", "UA")]
        stream = service.open("pc1-a", live_source(one, "ch001"), user=1)
        stream._thread.join(5)
        self.assertEqual(stream.error, "这个台暂时打不开，已经在自动检测")
        self.assertEqual(routes.bad_in(routes.entry_of(self.table, "ch001")), {routes.fingerprint("https://b/1.m3u8")})
        self.assertFalse(any("https://" in m for m in self.logs), self.logs)

    def test_pc_timeouts_are_not_definitive_and_a_play_clears_the_mark(self):
        service = self.service()
        lines = [("https://a/1.m3u8", "UA"), ("https://a/2.m3u8", "UA")]
        service.popen = ByInput({"data": LIVE_DATA},
                                **{"https://a/1.m3u8": {"lines": ["Error reading HTTP response: Operation timed out"],
                                                        "hold": False, "code": 1}})
        stream = service.open("pc1-a", live_source(lines, "ch000"), user=1)
        self.assertTrue(stream.wait_ready(5) or stream.wait_ready(5))
        self.assertEqual(routes.bad_in(routes.entry_of(self.table, "ch000")), set(), "a timeout is not a verdict")
        # A line the check called bad, which plays: the mark goes.
        routes.mark_failed(self.table, "ch000", "https://a/2.m3u8", "x", 1)
        service.routes.ok("ch000", "https://a/2.m3u8", 2)
        self.assertEqual(routes.bad_in(routes.entry_of(self.table, "ch000")), set())

    def test_checked_bad_lines_go_last_on_the_pc(self):
        service = self.service()
        lines = [("https://a/1.m3u8", "UA"), ("https://a/2.m3u8", "UA")]
        routes.mark_failed(self.table, "ch000", "https://a/1.m3u8", "x", 1)
        self.assertEqual(service.routes.order("ch000", lines), [1, 0])

    def test_tv_marks_a_403_line_and_orders_checked_bad_lines_last(self):
        from server import live, tv_server
        fake = types.SimpleNamespace(channel_name="ch000", ffmpeg="ff", logger=lambda m: None,
                                     channel_factory=lambda url, ff, ua: (url, ua))
        with unittest.mock.patch.dict(live._LOADED, {"main": self.table}), \
                unittest.mock.patch.dict(tv_server.CHANNELS, {"ch000": "https://a/1.m3u8"}), \
                unittest.mock.patch.dict(tv_server.CHANNEL_AGENTS, {"ch000": ""}):
            self.assertEqual(tv_server.AVServer._start_routes(fake, "ch000")[0], "https://a/1.m3u8")
            channel = types.SimpleNamespace(timestamps=types.SimpleNamespace(
                tail=[b"[https @ 0x1] Server returned 403 Forbidden (access denied)"]))
            tv_server.AVServer.route_dead(fake, channel)
            self.assertEqual(routes.bad_in(routes.entry_of(self.table, "ch000")),
                             {routes.fingerprint("https://a/1.m3u8")})
            self.assertEqual(tv_server.AVServer._start_routes(fake, "ch000")[0], "https://a/2.m3u8",
                             "the bad line goes last next time")
            # A line that times out is not marked.
            timeout = types.SimpleNamespace(timestamps=types.SimpleNamespace(tail=[b"Operation timed out"]))
            tv_server.AVServer.route_dead(fake, timeout)
            self.assertEqual(len(routes.bad_in(routes.entry_of(self.table, "ch000"))), 1)
            # It plays: the TV clears the mark.
            tv_server.AVServer.route_ok(fake)
            self.assertEqual(routes.bad_in(routes.entry_of(self.table, "ch000")),
                             {routes.fingerprint("https://a/1.m3u8")}, "a/2 played, a/1 stays bad")


class CoverTests(Base):
    def run_checks(self, checker):
        self.clock.now += linecheck.START_DELAY_S + linecheck.NEW_JITTER_S
        for _ in range(3):
            checker.tick()

    def test_one_grab_at_a_time_with_a_gap_once_per_period(self):
        spawn = Deferred()
        checker = self.checker(spawn=spawn)
        self.clock.now += linecheck.START_DELAY_S + linecheck.NEW_JITTER_S
        checker.tick()
        spawn.run()                         # two line checks: both channels want a cover now
        checker.tick()
        jobs = [fn.__name__ for fn, _ in spawn.jobs]
        self.assertEqual(jobs.count("_grab"), 1, "one ffmpeg at a time")
        checker.tick()
        self.assertEqual([fn.__name__ for fn, _ in spawn.jobs].count("_grab"), 1, "still one while it runs")
        spawn.run()
        self.assertEqual(len(self.grabbed), 1)
        checker.tick()
        self.assertNotIn("_grab", [fn.__name__ for fn, _ in spawn.jobs], "a gap between grabs")
        self.clock.now += linecheck.COVER_GAP_S
        checker.tick()
        spawn.run()
        self.assertEqual(len(self.grabbed), 2)
        self.assertEqual(sorted(self.covers.stamps()), ["ch000", "ch001"])
        # Hours later the lines are checked again, but the covers are still fresh: no new grab.
        self.clock.now += linecheck.OK_EVERY_S - linecheck.COVER_EVERY_S
        os.utime(self.covers.path("ch000"), (self.clock.now - 60, self.clock.now - 60))
        os.utime(self.covers.path("ch001"), (self.clock.now - 60, self.clock.now - 60))
        self.covers._stamps = None
        self.clock.now += linecheck.COVER_EVERY_S - 120
        for _ in range(4):
            checker.tick()
            spawn.run()
        self.assertEqual(len(self.grabbed), 2)

    def test_no_grab_while_the_pc_plays(self):
        checker = self.checker()
        self.load = (1, 1)
        self.run_checks(checker)
        self.assertEqual(self.grabbed, [])
        self.load = (0, 0)
        checker.tick()
        self.assertEqual(len(self.grabbed), 1)

    def test_failed_grab_is_retried_later_not_at_once(self):
        checker = self.checker(grab=lambda _ff, url, _ua, lenient=False: self.grabbed.append(url))
        self.run_checks(checker)
        for _ in range(4):
            self.clock.now += linecheck.COVER_GAP_S
            checker.tick()
        self.assertEqual(len(self.grabbed), 2, "each channel tried once")
        self.clock.now += linecheck.BAD_EVERY_S
        for _ in range(4):
            self.clock.now += linecheck.COVER_GAP_S
            checker.tick()
        self.assertEqual(len(self.grabbed), 2, "not again within COVER_RETRY_S")
        self.clock.now += linecheck.OK_EVERY_S
        for _ in range(4):
            self.clock.now += linecheck.COVER_GAP_S
            checker.tick()
        self.assertEqual(len(self.grabbed), 4, "tried again at the next check")
        self.assertEqual(self.covers.stamps(), {})

    def test_store_limits(self):
        store = linecheck.CoverStore(self.root / "c")
        self.assertFalse(store.put("ch000", b"<svg/>"), "only JPEG")
        self.assertFalse(store.put("ch000", JPEG[:3] + b"\0" * linecheck.COVER_MAX_BYTES), "size cap")
        self.assertFalse(store.put("../x", JPEG))
        self.assertIsNone(store.path("a/b"))
        with unittest.mock.patch.object(linecheck, "COVERS_MAX_FILES", 3):
            for n in range(5):
                path = store.folder / f"ch00{n}.jpg"
                self.assertTrue(store.put(f"ch00{n}", JPEG))
                os.utime(path, (1000 + n, 1000 + n))
            store.prune()
        self.assertEqual(sorted(p.name for p in store.folder.iterdir()), ["ch002.jpg", "ch003.jpg", "ch004.jpg"],
                         "oldest dropped first")
        with unittest.mock.patch.object(linecheck, "COVERS_MAX_BYTES", len(JPEG) * 2):
            store.prune()
        self.assertEqual(len(list(store.folder.iterdir())), 2)
        store.put("ch004", JPEG, known={"ch004"})
        self.assertEqual([p.name for p in store.folder.iterdir()], ["ch004.jpg"], "channels gone from the table")
        self.assertEqual(store.get("ch004"), JPEG)

    def test_real_grab_command_shape(self):
        seen = {}

        def run(command, **kwargs):
            seen.update(command=command, kwargs=kwargs)
            return types.SimpleNamespace(stdout=JPEG)
        self.assertEqual(linecheck.grab_frame("ff", "https://a/1.m3u8", "UA", run=run), JPEG)
        command = seen["command"]
        self.assertEqual(command[command.index("-i") + 1], "https://a/1.m3u8")
        self.assertIn(f"scale={linecheck.COVER_WIDTH}:-2", command)
        self.assertEqual(seen["kwargs"]["timeout"], linecheck.COVER_TIMEOUT_S)
        self.assertIsNone(linecheck.grab_frame("ff", "u", run=lambda *a, **k: types.SimpleNamespace(stdout=b"<html>")))


class EndpointTests(unittest.TestCase):
    selfhost = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.accounts = Accounts(self.root / "tv.db")
        self.addCleanup(self.accounts.close)
        self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        device = "aaaaaaaaaaa1"
        row = self.accounts.register_device(device, "xingzhi-cube-154", "s" * 43)
        self.accounts.bind(row["claim_code"], self.alice)
        self.main = self.root / "channels.txt"
        self.main.write_text(TABLE, encoding="utf-8")
        routes.write_meta(self.main, {"ch000": {"routes": [{"url": "https://a/2.m3u8"}]}})
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, youtube=FakeYouTube(),
                               main_channels=self.main, accounts=self.accounts, devices_dir=self.root / "devices",
                               log=lambda _m: None, selfhost=setup.SelfHost() if self.selfhost else None)
        self.relay.sync_devices(force=True)
        if self.relay.watch is not None:
            self.relay.watch.popen = Popen(data=LIVE_DATA)
            self.addCleanup(self.relay.watch.stop_all)
        self.checker = linecheck.attach(self.relay, self.root / "covers" / "live", start=False)
        if self.checker is not None:
            self.checker.covers.put("ch001", JPEG)
            routes.mark_failed(self.main, "ch000", "https://a/1.m3u8", "x", 1)
            self.checker.plan()
        _, self.manage = web.make_servers(self.relay, 0, 0)
        self.addCleanup(self.manage.server_close)
        self.addCleanup(self.manage.shutdown)
        threading.Thread(target=self.manage.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.cookies = {"alice": "tvs=" + self.accounts.new_session(self.alice, "pc"),
                        "guest": "tvs=" + self.accounts.new_session(None, "guest", device_id=device)}

    def request(self, path, who=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=20)
        connection.request("GET", "/tv" + path, headers={"Cookie": self.cookies[who]} if who else {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_cover_endpoint_auth_and_ids(self):
        status, headers, body = self.request("/api/art/live/ch001", "alice")
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/jpeg", JPEG))
        self.assertIn("private", headers["Cache-Control"])
        self.assertEqual(self.request("/api/art/live/ch001", "guest")[0], 200, "guests see live channels too")
        self.assertEqual(self.request("/api/art/live/ch001")[0], 404, "signed out")
        self.assertEqual(self.request("/api/art/live/ch000", "alice")[0], 404, "no cover yet")
        self.assertEqual(self.request("/api/art/live/..%2Fx", "alice")[0], 404)

    def test_state_and_home_carry_covers_and_dead_channels(self):
        state = json.loads(self.request("/api/state", "alice")[2])
        self.assertEqual(list(state["live_status"]["covers"]), ["ch001"])
        self.assertEqual(state["live_status"]["dead"], [], "ch000 has a second line not checked yet")
        routes.mark_failed(self.main, "ch001", "https://b/1.m3u8", "x", 1)
        self.checker.plan()
        state = json.loads(self.request("/api/state", "alice")[2])
        self.assertEqual(state["live_status"]["dead"], ["ch001"])
        home = json.loads(self.request("/api/home", "alice")[2])
        art = {x["channel"]["id"]: x["art"]["url"] for x in home["live_now"]}
        self.assertRegex(art["ch001"], r"^api/art/live/ch001\?v=\d+$")
        self.assertEqual(art["ch000"], "")


class CloudEndpointTests(EndpointTests):
    selfhost = False

    def test_cover_endpoint_auth_and_ids(self):
        self.assertIsNone(self.checker, "no background check on the cloud relay")
        self.assertEqual(self.request("/api/art/live/ch001", "alice")[0], 404)

    def test_state_and_home_carry_covers_and_dead_channels(self):
        self.assertNotIn("live_status", json.loads(self.request("/api/state", "alice")[2]))


if __name__ == "__main__":
    unittest.main()
