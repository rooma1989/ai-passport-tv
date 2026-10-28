"""yt-dlp's automatic update in local YouTube mode (server/relay/ytupdate.py) and what it needs from
the components (server/selfhost/ytcomponents.py: keep the previous version, commit, roll back, the
swap gate) and from the node (server/relay/ytnode.py: /yt/health's broken and busy).

Covers: the daily check (at most once a day, remembered across restarts); the immediate check after
a failure that looks like an extractor break, and not after ordinary failures; the install itself
(the release's SHA2-256SUMS, the previous version kept until the self-test); the self-test (passes,
fails -> rolled back and that version not installed again, inconclusive -> retried, then kept); never
swapping while the node is starting a stream; the settings (update_check, local mode only, the
mirror from the settings page); a manual update dropping a pending rollback.

No network, no real yt-dlp: GitHub is a dictionary of URLs and the node is a stand-in.
"""

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from server.relay import ytnode, ytupdate  # noqa: E402
from server.relay.bilibili import SourceError  # noqa: E402
from server.selfhost import ytcomponents  # noqa: E402

from test_ytdlp_zipapp import fake_fetch, zipapp_bytes  # noqa: E402

OLD, NEW = "2026.10.21", "2026.11.02"
OLD_PYZ, NEW_PYZ = zipapp_bytes(OLD), zipapp_bytes(NEW)
REPO = ytcomponents.YTDLP_REPO
MIRROR = "https://mirror.example/https://github.com"


def github(base="https://github.com", tag=NEW):
    sums = f"{hashlib.sha256(NEW_PYZ).hexdigest()}  yt-dlp\n{'0' * 64}  yt-dlp.exe\n".encode()
    files = {f"{base}/{REPO}/releases/download/{tag}/yt-dlp": NEW_PYZ,
             f"{base}/{REPO}/releases/download/{tag}/SHA2-256SUMS": sums}
    redirects = {f"{base}/{REPO}/releases/latest": f"https://github.com/{REPO}/releases/tag/{tag}"}
    return files, redirects


class FakeNode:
    """relay.clients["youtube"]: health() and check() like server/relay/youtube.py's client."""

    def __init__(self):
        self.state = {"state": "ok", "at": 100.0, "version": OLD, "broken": False, "busy": False}
        self.checks = ["ok"]
        self.checked = 0
        self.down = False

    def health(self, timeout=None):
        if self.down:
            raise SourceError("node down")
        return dict(self.state)

    def check(self, timeout):
        self.checked += 1
        state = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
        if state == "raise":
            raise SourceError("timed out")
        return {"state": state, "seconds": 3.0, "error": "", "version": NEW}


class FakeRelay:
    def __init__(self):
        self.youtube_kind = "local"
        self.node = FakeNode()
        self.clients = {"youtube": self.node}
        self.logs = []
        self._youtube_health = ("cached", "login")

    def log(self, message):
        self.logs.append(message)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept += seconds


class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        (self.bin / f"yt-dlp-{OLD}.pyz").write_bytes(OLD_PYZ)
        ytcomponents.unpack_zipapp(self.bin / f"yt-dlp-{OLD}.pyz", self.bin / f"yt-dlp-{OLD}")
        (self.bin / "deno-2.9.8").write_bytes(b"deno")
        (self.bin / "components.json").write_text(json.dumps({
            "yt-dlp": {"version": OLD, "file": f"yt-dlp-{OLD}.pyz", "sha256": hashlib.sha256(OLD_PYZ).hexdigest()},
            "deno": {"version": "2.9.8", "file": "deno-2.9.8", "sha256": "1" * 64}}))
        self.relay = FakeRelay()
        self.clock = Clock()
        self.settings = {"update_check": True, "proxy": ""}
        self.serve()

    def tearDown(self):
        self.tmp.cleanup()

    def serve(self, base="https://github.com", tag=NEW):
        files, redirects = github(base, tag)
        self.fetch = fake_fetch(files, redirects)
        self.logs = []
        self.parts = ytcomponents.Components(self.bin, log=self.logs.append, fetch=self.fetch,
                                             check_run=lambda _p: "ok", system="Darwin", machine="arm64",
                                             compile_run=lambda _f: True)
        self.updater = ytupdate.Updater(self.relay, self.parts, self.bin / ytupdate.STATE_FILE, clock=self.clock,
                                        sleep=self.clock.sleep,
                                        config=lambda: (self.settings["update_check"], self.settings["proxy"]))

    def recorded(self):
        return json.loads((self.bin / "components.json").read_text())

    def files(self):
        return sorted(p.name for p in self.bin.iterdir())

    def test_daily_check_installs_and_self_tests(self):
        self.updater.tick()
        self.assertEqual(self.recorded()["yt-dlp"]["version"], NEW)
        self.assertEqual(self.recorded()["yt-dlp"]["sha256"], hashlib.sha256(NEW_PYZ).hexdigest())
        self.assertNotIn(ytcomponents.PREVIOUS, self.recorded(), "the self-test passed: no rollback kept")
        self.assertEqual(self.relay.node.checked, 1)
        self.assertEqual(self.files(), ["autoupdate.json", "components.json", "deno-2.9.8", f"yt-dlp-{NEW}",
                                        f"yt-dlp-{NEW}.pyz"])
        self.assertIn("自检通过", self.relay.logs[-1])
        self.assertIsNone(self.relay._youtube_health, "the remote's YouTube state is asked again")
        self.assertIn("自检通过", self.updater.snapshot()["last"])
        # Same day: GitHub is not asked again, also not after a restart (the time is on disk).
        asked = len(self.fetch.calls)
        self.clock.t += ytupdate.CHECK_EVERY_S - 60
        self.updater.tick()
        self.serve()
        self.updater.tick()
        self.assertEqual(len(self.fetch.calls), 0)
        self.assertGreater(asked, 0)
        self.clock.t += 120
        self.updater.tick()
        self.assertEqual(self.fetch.calls, [f"https://github.com/{REPO}/releases/latest"], "next day: asked once")

    def test_keeps_the_previous_until_the_self_test(self):
        self.parts.start("update", wait=True, keep_previous=True)
        self.assertEqual(self.recorded()[ytcomponents.PREVIOUS]["version"], OLD)
        self.assertEqual(self.parts.previous()["file"], f"yt-dlp-{OLD}.pyz")
        self.assertTrue((self.bin / f"yt-dlp-{OLD}").is_dir())
        # Another run of the components (start-up install, say) does not prune the kept one.
        self.parts.start("install", wait=True)
        self.assertTrue((self.bin / f"yt-dlp-{OLD}.pyz").is_file())
        self.assertEqual(ytnode.read_components(str(self.bin))[0], str(self.bin / f"yt-dlp-{NEW}.pyz"),
                         "the node runs the new one")

    def test_failed_self_test_rolls_back_and_skips_that_version(self):
        self.relay.node.checks = ["error"]
        self.updater.tick()
        self.assertEqual(self.recorded()["yt-dlp"]["version"], OLD)
        self.assertNotIn(ytcomponents.PREVIOUS, self.recorded())
        self.assertEqual(self.files(), ["autoupdate.json", "components.json", "deno-2.9.8", f"yt-dlp-{OLD}",
                                        f"yt-dlp-{OLD}.pyz"])
        self.assertIn(f"已换回 {OLD}", self.relay.logs[-1])
        self.assertEqual(self.updater.state["bad"], [NEW])
        self.clock.t += ytupdate.CHECK_EVERY_S
        self.fetch.calls.clear()
        self.updater.tick()
        self.assertEqual(self.fetch.calls, [f"https://github.com/{REPO}/releases/latest"],
                         "the bad version is not downloaded again")
        self.assertEqual(self.recorded()["yt-dlp"]["version"], OLD)

    def test_inconclusive_self_test_is_retried_then_kept(self):
        self.relay.node.checks = ["network", "raise", "slow"]
        self.updater.tick()
        self.assertIsNotNone(self.parts.previous(), "cannot tell yet: both kept")
        self.updater.tick()
        self.assertIsNotNone(self.parts.previous())
        self.updater.tick()
        self.assertIsNone(self.parts.previous())
        self.assertEqual(self.recorded()["yt-dlp"]["version"], NEW)
        self.assertEqual(self.relay.node.checked, ytupdate.SELFTEST_TRIES)
        self.assertIn("没法自检", self.relay.logs[-1])

    def test_login_answer_passes_the_self_test(self):
        self.relay.node.checks = ["login"]
        self.updater.tick()
        self.assertEqual(self.recorded()["yt-dlp"]["version"], NEW)
        self.assertIsNone(self.parts.previous())

    def test_extractor_break_checks_immediately_but_not_too_often(self):
        self.updater.state["checked_at"] = self.clock.t      # already checked today
        self.relay.node.state.update(state="error", at=200.0, broken=False)
        self.updater.tick()
        self.assertEqual(self.fetch.calls, [], "an ordinary failure waits for the daily check")
        self.relay.node.state.update(state="login", at=300.0, broken=True)
        self.updater.tick()
        self.assertEqual(self.recorded()["yt-dlp"]["version"], NEW)
        self.assertTrue(any("跟不上" in line for line in self.relay.logs))
        self.serve(tag="2026.11.09")
        self.clock.t += 600
        self.relay.node.state.update(state="error", at=400.0, broken=True)
        self.updater.tick()
        self.assertEqual(self.fetch.calls, [], f"at most once per {ytupdate.FAILURE_GAP_S} s")
        self.clock.t += ytupdate.FAILURE_GAP_S
        self.updater.tick()
        self.assertEqual(self.fetch.calls[0], f"https://github.com/{REPO}/releases/latest")
        self.assertEqual(self.recorded()["yt-dlp"]["version"], "2026.11.09")
        asked = len(self.fetch.calls)
        self.updater.tick()
        self.assertEqual(len(self.fetch.calls), asked, "the same failure is not acted on twice")

    def test_never_swaps_while_a_stream_is_starting(self):
        self.relay.node.state["busy"] = True
        self.updater.tick()
        self.assertEqual(self.recorded()["yt-dlp"]["version"], OLD)
        self.assertEqual(self.files(), ["autoupdate.json", "components.json", "deno-2.9.8", f"yt-dlp-{OLD}",
                                        f"yt-dlp-{OLD}.pyz"], "the download is thrown away")
        self.assertGreaterEqual(self.clock.slept, ytupdate.QUIET_WAIT_S)
        self.assertIn("正在起播", self.updater.snapshot()["last"])
        self.assertEqual(self.relay.node.checked, 0)
        # Not installed: tried again in RETRY_S, not tomorrow. Busy for a moment only: it waits, then swaps.
        self.clock.t += ytupdate.RETRY_S - 60
        self.fetch.calls.clear()
        self.updater.tick()
        self.assertEqual(self.fetch.calls, [])
        self.clock.t += 60
        answers = iter([True, True, False])
        original = self.relay.node.health

        def health(timeout=None):
            state = original(timeout)
            state["busy"] = next(answers, False)
            return state

        self.relay.node.health = health
        self.updater.tick()
        self.assertEqual(self.recorded()["yt-dlp"]["version"], NEW)

    def test_settings_are_respected(self):
        self.settings["update_check"] = False
        self.updater.tick()
        self.assertEqual(self.fetch.calls, [])
        self.assertFalse(self.updater.snapshot()["enabled"])
        self.assertIn("update_check", self.updater.snapshot()["reason"])
        self.settings["update_check"] = True
        self.relay.youtube_kind = "node"
        self.updater.tick()
        self.assertEqual(self.fetch.calls, [])
        self.relay.youtube_kind = "local"
        # The mirror and proxy from the settings are what the download uses.
        self.serve(base=MIRROR)
        self.settings["proxy"] = "http://127.0.0.1:7897"
        seen = []
        fetch = self.parts.fetch
        self.parts.fetch = lambda url, proxy="", follow=True: (seen.append(proxy), fetch(url, proxy, follow))[1]
        self.updater.remember_mirror(MIRROR)
        self.updater.tick()
        self.assertTrue(self.fetch.calls and all(c.startswith(MIRROR) for c in self.fetch.calls))
        self.assertEqual(set(seen), {"http://127.0.0.1:7897"})
        self.assertEqual(self.recorded()["yt-dlp"]["version"], NEW)
        for line in self.relay.logs + self.logs:
            self.assertNotIn("7897", line)

    def test_failures_to_ask_are_noted_not_raised(self):
        self.serve(tag="not-a-tag")
        self.updater.tick()
        self.assertIn("查新版失败", self.relay.logs[-1])
        self.relay.node.down = True
        self.clock.t += ytupdate.CHECK_EVERY_S
        self.updater.tick()      # node down: still checks daily, cannot swap, nothing raised

    def test_manual_update_drops_a_pending_rollback(self):
        self.parts.start("update", wait=True, keep_previous=True)
        self.assertIsNotNone(self.parts.previous())
        self.parts.start("update", wait=True)
        self.assertIsNone(self.parts.previous())
        self.assertFalse((self.bin / f"yt-dlp-{OLD}.pyz").exists())
        self.assertEqual(self.parts.rollback(), "", "nothing to roll back to")


class NodeHealthTests(unittest.TestCase):
    def node(self):
        return ytnode.Node(ytnode.Tools("/y", "/d", "/f", ""), "k", log=lambda _m: None)

    def test_broken_flag(self):
        node = self.node()
        node._parse(1, "", "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you're not a bot")
        self.assertEqual((node.health["state"], node.health["broken"]), ("login", True))
        node._parse(1, "", "ERROR: [youtube] jNQXAC9IVRw: Unable to extract yt initial data")
        self.assertEqual((node.health["state"], node.health["broken"]), ("error", True))
        node._parse(1, "", "ERROR: unable to download webpage: <urlopen error timed out>")
        self.assertEqual((node.health["state"], node.health["broken"]), ("error", False))
        node._parse(1, "", "ERROR: [youtube] gonegonegon: Video unavailable. HTTP Error 403")
        self.assertEqual((node.health["state"], node.health["broken"]), ("ok", False), "the video itself is gone")
        node._parse(0, '{"_version": {"version": "2026.11.02"}}', "")
        self.assertEqual((node.health["state"], node.health["broken"]), ("ok", False))

    def test_busy_follows_the_lanes(self):
        node = ytnode.Node(ytnode.Tools("/y", "/d", "/f", ""), "k", log=lambda _m: None, tools_dir="/nowhere")
        self.assertFalse(node.busy())
        with node._play:
            self.assertTrue(node.busy())
        with node._ytdlp:
            self.assertTrue(node.busy())
        started, release = threading.Event(), threading.Event()

        def wait_for_lane():
            with node._gate:
                node._waiting += 1
            started.set()
            release.wait(5)
            with node._gate:
                node._waiting -= 1

        thread = threading.Thread(target=wait_for_lane)
        thread.start()
        started.wait(5)
        self.assertTrue(node.busy())
        release.set()
        thread.join(5)
        self.assertFalse(node.busy())


if __name__ == "__main__":
    unittest.main()
