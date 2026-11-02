"""Which browser "打开电脑版" uses (desktop/browsers.py and the tray around it).

Nothing here looks at or starts a real browser: mdfind, `open -b`, osascript
and Info.plist reading all go through a fake `browsers.System`, on any OS.
"""

from __future__ import annotations

import collections
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop import browsers, menu, paths, power  # noqa: E402
from desktop.control import ControlClient, Status  # noqa: E402
from desktop.devcore import DevCore  # noqa: E402
from desktop.tray import App, Prefs  # noqa: E402

URL = "http://localhost:8080/tv/local-login?t=abc"


class FakeMac:
    """A Mac with some apps: `apps` maps a .app path to its Info.plist; `answers`
    are what the next osascript dialogs return (None: osascript fails)."""

    def __init__(self, root: Path, apps: dict[str, dict], elsewhere: dict[str, dict] | None = None,
                 answers=(), broken_ids=()):
        self.root = Path(root)
        self.plists: dict[str, dict] = {}
        self.elsewhere = dict(elsewhere or {})
        self.answers = list(answers)
        self.broken_ids = set(broken_ids)
        self.calls: list[list[str]] = []
        for relative, info in apps.items():
            path = self.root / relative
            (path / "Contents").mkdir(parents=True, exist_ok=True)
            self.plists[str(path / "Contents" / "Info.plist")] = info
        for path, info in self.elsewhere.items():
            self.plists[str(Path(path) / "Contents" / "Info.plist")] = info
        self.system = browsers.System(platform="darwin", run=self.run, read_plist=self.read_plist,
                                      app_dirs=(str(self.root / "Applications"), str(self.root / "Home")))

    def installed_ids(self) -> set[str]:
        return {info.get("CFBundleIdentifier") for info in self.plists.values()}

    def read_plist(self, path: Path) -> dict:
        if str(path) not in self.plists:
            raise OSError("no Info.plist")
        return self.plists[str(path)]

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "mdfind":
            self.query = argv[1]
            return subprocess.CompletedProcess(argv, 0, "\n".join(self.elsewhere) + "\n", "")
        if argv[0] == "open":
            assert argv[1] == "-b" and len(argv) == 4, argv
            ok = argv[2] in self.installed_ids() and argv[2] not in self.broken_ids
            return subprocess.CompletedProcess(argv, 0 if ok else 1, b"", b"")
        if argv[0] == "osascript":
            answer = self.answers.pop(0)
            if answer is None:
                return subprocess.CompletedProcess(argv, 1, "", "execution error")
            return subprocess.CompletedProcess(argv, 0, answer + "\n", "")
        raise AssertionError(f"unexpected command {argv}")

    def opened_with(self) -> list[tuple[str, str]]:
        return [(argv[2], argv[3]) for argv in self.calls if argv[0] == "open"]

    def dialogs(self) -> list[list[str]]:
        return [argv[3:] for argv in self.calls if argv[0] == "osascript"]


def standard_mac(root: Path, **kw) -> FakeMac:
    return FakeMac(root, {
        "Applications/Safari.app": {"CFBundleIdentifier": "com.apple.Safari", "CFBundleName": "Safari"},
        "Applications/Google Chrome.app": {"CFBundleIdentifier": "com.google.Chrome"},
        "Applications/iTerm.app": {"CFBundleIdentifier": "com.googlecode.iterm2"},     # handles http, not a browser
        "Applications/VLC.app": {"CFBundleIdentifier": "org.videolan.vlc"},
        "Applications/Broken.app": {},
        "Home/Firefox.app": {"CFBundleIdentifier": "org.mozilla.firefox"},
    }, **kw)


class MemoryPrefs:
    def __init__(self, **data):
        self.data = dict(data)
        self.writes = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.writes.append((key, value))
        self.data[key] = value


# ----------------------------------------------------------------------------- detection

class DetectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_browsers_only_in_menu_order(self):
        mac = standard_mac(self.root, elsewhere={
            "/Users/x/Tools/Brave Browser.app": {"CFBundleIdentifier": "com.brave.Browser"},
            "/Users/x/.Trash/Opera.app": {"CFBundleIdentifier": "com.operasoftware.Opera"},
            "/Volumes/Arc/Arc.app": {"CFBundleIdentifier": "company.thebrowser.Browser"},
            "/Applications/Safari.app": {"CFBundleIdentifier": "com.apple.Safari"},
        })
        found = browsers.detect(mac.system)
        self.assertEqual([b.bundle_id for b in found],
                         ["com.apple.Safari", "com.google.Chrome", "org.mozilla.firefox", "com.brave.Browser"])
        self.assertEqual(found[1].name, "Google Chrome")
        # One mdfind for all known ids, no shell.
        mdfinds = [argv for argv in mac.calls if argv[0] == "mdfind"]
        self.assertEqual(len(mdfinds), 1)
        self.assertIn("kMDItemCFBundleIdentifier == 'com.google.Chrome'", mac.query)
        self.assertIn(" || ", mac.query)

    def test_mdfind_failing_still_finds_the_applications_folder(self):
        mac = standard_mac(self.root)

        def run(argv, **kw):
            if argv[0] == "mdfind":
                raise subprocess.TimeoutExpired(argv, 10)
            return mac.run(argv, **kw)
        mac.system.run = run
        self.assertEqual([b.bundle_id for b in browsers.detect(mac.system)],
                         ["com.apple.Safari", "com.google.Chrome", "org.mozilla.firefox"])

    def test_nothing_off_macos(self):
        mac = standard_mac(self.root)
        for platform in ("win32", "linux"):
            mac.system.platform = platform
            self.assertEqual(browsers.detect(mac.system), ())
            self.assertEqual(browsers.chosen(MemoryPrefs(console_browser="com.google.Chrome"), mac.system), "")
        self.assertEqual(mac.calls, [])

    def test_known_list_has_unique_ids_and_names(self):
        ids = [bundle_id for bundle_id, _ in browsers.KNOWN]
        names = [name for _, name in browsers.KNOWN]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(names), len(set(names)), "the choose-from-list dialog maps names back to ids")
        self.assertEqual(ids[0], "com.apple.Safari")


# ----------------------------------------------------------------------------- menu

class MenuTest(unittest.TestCase):
    def submenu(self, state):
        found = [item for item in menu.build(state) if item.text == "电脑版用的浏览器"]
        return found[0] if found else None

    def test_hidden_where_not_offered(self):
        state = menu.TrayState(core="running", status=Status())
        self.assertIsNone(self.submenu(state))

    def test_default_and_installed_browsers_current_checked(self):
        listed = (("com.apple.Safari", "Safari"), ("com.google.Chrome", "Google Chrome"))
        items = menu.build(menu.TrayState(core="running", status=Status(), browsers=listed))
        texts = [item.text for item in items]
        self.assertEqual(texts.index("电脑版用的浏览器"), texts.index("打开电脑版") + 1)
        sub = self.submenu(menu.TrayState(core="running", status=Status(), browsers=listed))
        self.assertEqual([i.text for i in sub.submenu], ["跟随系统默认浏览器", "Safari", "Google Chrome"])
        self.assertEqual([i.checked for i in sub.submenu], [True, False, False])
        self.assertTrue(all(i.radio for i in sub.submenu))
        self.assertEqual([i.action for i in sub.submenu],
                         ["choose_browser:", "choose_browser:com.apple.Safari", "choose_browser:com.google.Chrome"])
        chrome = self.submenu(menu.TrayState(core="running", status=Status(), browsers=listed,
                                             browser="com.google.Chrome"))
        self.assertEqual([i.checked for i in chrome.submenu], [False, False, True])

    def test_a_missing_choice_stays_listed(self):
        sub = self.submenu(menu.TrayState(browsers=(("com.apple.Safari", "Safari"),), browser="org.mozilla.firefox"))
        self.assertEqual(sub.submenu[-1].text, "Firefox（没有找到）")
        self.assertTrue(sub.submenu[-1].checked)
        self.assertEqual(sum(1 for i in sub.submenu if i.checked), 1)

    def test_every_browser_action_has_a_handler(self):
        state = menu.TrayState(core="running", status=Status(), browsers=(("com.apple.Safari", "Safari"),))
        for action in menu.actions_in(menu.build(state)):
            self.assertIn(menu.action_name(action), App.ACTIONS)


# ----------------------------------------------------------------------------- opening

class OpenConsoleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.defaults = []

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, mac, prefs, **kw):
        return browsers.open_console(URL, prefs, self.defaults.append, mac.system, **kw)

    def test_default_uses_the_system_browser(self):
        mac = standard_mac(self.root)
        self.assertEqual(self.open(mac, MemoryPrefs()), "default")
        self.assertEqual(self.defaults, [URL])
        self.assertEqual(mac.calls, [])

    def test_chosen_browser_is_opened_with_open_b(self):
        mac = standard_mac(self.root)
        self.assertEqual(self.open(mac, MemoryPrefs(console_browser="com.google.Chrome")), "chosen")
        self.assertEqual(mac.calls, [["open", "-b", "com.google.Chrome", URL]])
        self.assertEqual(self.defaults, [])

    def test_chosen_browser_ignored_off_macos(self):
        mac = standard_mac(self.root)
        mac.system.platform = "win32"
        self.assertEqual(self.open(mac, MemoryPrefs(console_browser="com.google.Chrome")), "default")
        self.assertEqual(self.defaults, [URL])

    def test_missing_browser_falls_back_once_and_keeps_the_choice(self):
        mac = standard_mac(self.root, answers=[browsers.FALLBACK])
        prefs = MemoryPrefs(console_browser="com.brave.Browser")
        notes = []
        self.assertEqual(self.open(mac, prefs, notify=notes.append), "fallback")
        self.assertEqual(self.defaults, [URL])
        self.assertEqual(prefs.writes, [])
        self.assertEqual(prefs.get("console_browser"), "com.brave.Browser")
        self.assertIn("这次用系统默认浏览器", notes[0])
        dialog = mac.dialogs()[0]
        self.assertIn("Brave", dialog[0])
        self.assertEqual(dialog[1:4], [browsers.FALLBACK, browsers.RECHOOSE, browsers.CANCEL])

    def test_no_dialog_still_falls_back_once(self):
        mac = standard_mac(self.root, answers=[None])
        prefs = MemoryPrefs(console_browser="com.google.Chrome")
        mac.broken_ids.add("com.google.Chrome")
        notes = []
        self.assertEqual(self.open(mac, prefs, notify=notes.append), "fallback")
        self.assertEqual(self.defaults, [URL])
        self.assertEqual(prefs.writes, [])
        self.assertTrue(notes)

    def test_cancel_opens_nothing(self):
        mac = standard_mac(self.root, answers=[browsers.CANCEL])
        prefs = MemoryPrefs(console_browser="com.brave.Browser")
        self.assertEqual(self.open(mac, prefs), "cancelled")
        self.assertEqual(self.defaults, [])
        self.assertEqual(prefs.writes, [])
        mac = standard_mac(self.root, answers=[""])        # closed / gave up
        self.assertEqual(self.open(mac, prefs), "cancelled")

    def test_rechoose_saves_and_opens_the_new_one(self):
        mac = standard_mac(self.root, answers=[browsers.RECHOOSE, "Google Chrome"])
        prefs = MemoryPrefs(console_browser="com.brave.Browser")
        self.assertEqual(self.open(mac, prefs), "rechosen")
        self.assertEqual(prefs.get("console_browser"), "com.google.Chrome")
        self.assertEqual(mac.opened_with()[-1], ("com.google.Chrome", URL))
        choices = mac.dialogs()[1]
        # prompt, title, preselected, then the choices (the missing Brave is not one of them)
        self.assertEqual(choices[2], "跟随系统默认浏览器")
        self.assertEqual(choices[3:], ["跟随系统默认浏览器", "Safari", "Google Chrome", "Firefox"])

    def test_rechoose_the_system_default(self):
        mac = standard_mac(self.root, answers=[browsers.RECHOOSE, "跟随系统默认浏览器"])
        prefs = MemoryPrefs(console_browser="com.brave.Browser")
        self.assertEqual(self.open(mac, prefs), "rechosen")
        self.assertEqual(prefs.get("console_browser"), "")
        self.assertEqual(self.defaults, [URL])

    def test_rechoose_cancelled_keeps_the_choice(self):
        mac = standard_mac(self.root, answers=[browsers.RECHOOSE, ""])
        prefs = MemoryPrefs(console_browser="com.brave.Browser")
        self.assertEqual(self.open(mac, prefs), "cancelled")
        self.assertEqual(prefs.writes, [])
        self.assertEqual(self.defaults, [])

    def test_open_timeout_counts_as_opened(self):
        mac = standard_mac(self.root)

        def run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 15)
        mac.system.run = run
        self.assertTrue(browsers.open_with(mac.system, "com.google.Chrome", URL))


# ----------------------------------------------------------------------------- the tray

class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeBackend:
    def acquire(self):
        return True

    def release(self):
        pass

    def close(self):
        pass


class FakeCore:
    def __init__(self):
        self.running, self.state, self.last_exit = True, "running", None
        self.tail = collections.deque(maxlen=10)

    def start(self):
        pass

    def watch(self):
        return None

    def stop(self, client=None):
        if client is not None:
            client.stop()

    def restart(self, client=None):
        pass


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.mac = standard_mac(Path(self.tmp.name) / "mac")
        self.opened = []
        self.spawned = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, system=None, run_now=True) -> App:
        core = DevCore(self.data)
        self.devcore = core.start()
        self.addCleanup(core.stop)
        self.clock = Clock()
        spawn = (lambda fn: fn()) if run_now else self.spawned.append
        return App(self.data, ControlClient(paths.control_file(self.data), timeout=3), FakeCore(),
                   power.KeepAwake(FakeBackend(), clock=Clock()), opener=self.opened.append,
                   path_opener=self.opened.append, revealer=lambda p: None, logs_dir=self.data / "logs",
                   clock=self.clock, browser_system=system or self.mac.system, spawn=spawn)

    def test_choice_is_saved_and_survives_a_restart(self):
        app = self.make()
        app.start()
        self.assertEqual(app.state.browsers, (("com.apple.Safari", "Safari"), ("com.google.Chrome", "Google Chrome"),
                                              ("org.mozilla.firefox", "Firefox")))
        self.assertEqual(app.state.browser, "")
        app.run_action("choose_browser:org.mozilla.firefox")
        self.assertEqual(app.state.browser, "org.mozilla.firefox")
        self.assertEqual(Prefs(paths.prefs_file(self.data)).get("console_browser"), "org.mozilla.firefox")
        again = self.make()
        self.assertEqual(again.state.browser, "org.mozilla.firefox")
        sub = [i for i in menu.build(again.state) if i.text == "电脑版用的浏览器"][0]
        self.assertEqual([i.text for i in sub.submenu if i.checked], ["Firefox"])
        app.run_action("choose_browser:")
        self.assertEqual(Prefs(paths.prefs_file(self.data)).get("console_browser"), "")

    def test_open_console_uses_the_choice_and_other_items_do_not(self):
        app = self.make()
        app.start()
        app.run_action(menu.OPEN_CONSOLE)
        self.assertTrue(self.opened[-1].startswith("http://localhost:8080/tv/local-login?t="))
        self.assertEqual(self.mac.opened_with(), [])
        app.run_action("choose_browser:com.google.Chrome")
        app.run_action(menu.OPEN_CONSOLE)
        bundle_id, url = self.mac.opened_with()[-1]
        self.assertEqual(bundle_id, "com.google.Chrome")
        self.assertTrue(url.startswith("http://localhost:8080/tv/local-login?t="))
        before = len(self.opened)
        app.run_action(menu.SET_UP_TV)
        app.run_action(menu.CONNECT_PHONE)
        self.assertEqual(len(self.opened), before + 2, "only 打开电脑版 follows the choice")
        self.assertEqual(len(self.mac.opened_with()), 1)

    def test_missing_browser_fallback_does_not_overwrite_the_choice(self):
        Prefs(paths.prefs_file(self.data)).set("console_browser", "com.brave.Browser")
        self.mac.answers = [browsers.FALLBACK]
        app = self.make()
        app.start()
        app.run_action(menu.OPEN_CONSOLE)
        self.assertTrue(self.opened[-1].startswith("http://localhost:8080/tv/local-login?t="))
        self.assertEqual(Prefs(paths.prefs_file(self.data)).get("console_browser"), "com.brave.Browser")
        self.assertEqual(app.state.browser, "com.brave.Browser")
        self.assertTrue(any("这次用系统默认浏览器" in n for n in app.state.notices))
        sub = [i for i in menu.build(app.state) if i.text == "电脑版用的浏览器"][0]
        self.assertEqual(sub.submenu[-1].text, "Brave（没有找到）")

    def test_rechoosing_in_the_dialog_updates_the_menu(self):
        Prefs(paths.prefs_file(self.data)).set("console_browser", "com.brave.Browser")
        self.mac.answers = [browsers.RECHOOSE, "Safari"]
        app = self.make()
        app.start()
        app.run_action(menu.OPEN_CONSOLE)
        self.assertEqual(self.mac.opened_with()[-1][0], "com.apple.Safari")
        self.assertEqual(app.state.browser, "com.apple.Safari")
        self.assertEqual(Prefs(paths.prefs_file(self.data)).get("console_browser"), "com.apple.Safari")

    def test_lookup_is_off_the_poll_and_at_most_once_a_minute(self):
        app = self.make(run_now=False)
        self.assertEqual(app.state.browsers, ())
        app.start()
        app.tick()
        self.assertEqual(len(self.spawned), 1, "one lookup while it runs")
        self.assertFalse([argv for argv in self.mac.calls if argv[0] == "mdfind"], "not on the poll thread")
        self.spawned.pop()()
        self.assertEqual(len(app.state.browsers), 3)
        self.clock.now += 30
        app.tick()
        self.assertEqual(self.spawned, [])
        self.clock.now += 31
        app.tick()
        self.assertEqual(len(self.spawned), 1)

    def test_a_choice_written_by_another_launch_is_picked_up(self):
        app = self.make()
        app.start()
        other = Prefs(paths.prefs_file(self.data))
        other.set("console_browser", "com.google.Chrome")
        app.tick()
        self.assertEqual(app.state.browser, "com.google.Chrome")
        # ... and the tray's own next write keeps it.
        app.prefs.set("keep_awake", "always")
        self.assertEqual(Prefs(paths.prefs_file(self.data)).get("console_browser"), "com.google.Chrome")

    def test_not_offered_off_macos(self):
        self.mac.system.platform = "win32"
        Prefs(paths.prefs_file(self.data)).set("console_browser", "com.google.Chrome")
        app = self.make()
        app.start()
        self.assertIsNone(app.state.browsers)
        self.assertNotIn("电脑版用的浏览器", [i.text for i in menu.build(app.state)])
        app.run_action(menu.OPEN_CONSOLE)
        self.assertTrue(self.opened[-1].startswith("http://localhost:8080/"))
        self.assertEqual(self.mac.calls, [])


if __name__ == "__main__":
    unittest.main()
