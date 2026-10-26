"""The rename to RooTV (小鼠电视), formerly Xiaozhi TV (小智电视): names, the one-time
move of the data and log folders, the old login item, stopping the old tray, icons.

Runs without pystray, Pillow or a display, on every OS (packaging/desktop/unit_tests.py
runs it in the desktop workflow on macOS, Windows and Linux). Every folder is a
temporary one: nothing here touches a real data folder.
"""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desktop  # noqa: E402
from desktop import autostart, icons, legacy, paths  # noqa: E402
from desktop.instance import SingleInstance  # noqa: E402
from desktop.tray import App, Prefs  # noqa: E402
from tools import datadir, desktop_folders as folders  # noqa: E402


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


class Home:
    """A fake home folder, laid out as on `system`."""

    def __init__(self, test: unittest.TestCase, system: str):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.system = system
        self.env = {"LOCALAPPDATA": str(self.root / "AppData" / "Local")} if system == "Windows" else {}
        self.old = folders.default_data_dir(system, self.env, self.root, legacy=True)
        self.new = folders.default_data_dir(system, self.env, self.root)
        self.old_logs = folders.default_logs_dir(system, self.env, self.root, legacy=True)
        self.new_logs = folders.default_logs_dir(system, self.env, self.root)

    def old_install(self, logs: bool = True) -> None:
        """What 0.2.5-beta leaves: data with secrets and a database, logs, stale run files."""
        (self.old / "secrets").mkdir(parents=True)
        (self.old / "secrets" / "relay-key").write_text("k", encoding="utf-8")
        (self.old / "tv.db").write_bytes(b"SQLite format 3\x00")
        (self.old / "tray.json").write_text('{"autostart_initialised": true}', encoding="utf-8")
        (self.old / "run").mkdir()
        (self.old / "run" / "tray.lock").write_text("4242\n", encoding="ascii")
        (self.old / "run" / "core.lock").write_text("", encoding="ascii")
        if logs:
            self.old_logs.mkdir(parents=True, exist_ok=True)
            (self.old_logs / "core.log").write_text("old log\n", encoding="utf-8")

    def resolve(self, **kw) -> folders.Folders:
        return folders.resolve(self.system, self.env, self.root, **kw)


class NamesTest(unittest.TestCase):
    def test_product_names(self):
        self.assertEqual((desktop.APP_NAME, desktop.APP_NAME_EN, desktop.APP_ID), ("小鼠电视", "RooTV", "RooTV"))
        self.assertEqual(desktop.BUNDLE_ID, "io.github.ai-passport-tv.RooTV")
        self.assertEqual(desktop.LEGACY_BUNDLE_ID, "io.github.ai-passport-tv.XiaozhiTV")
        self.assertEqual(paths.CORE_NAME, "rootv-core")

    def test_folders_per_os(self):
        home = Path("/home/u")
        self.assertEqual(folders.default_data_dir("Darwin", {}, home), home / "Library/Application Support/RooTV")
        self.assertEqual(folders.default_data_dir("Darwin", {}, home, legacy=True),
                         home / "Library/Application Support/XiaozhiTV")
        self.assertEqual(folders.default_logs_dir("Darwin", {}, home, legacy=True), home / "Library/Logs/XiaozhiTV")
        win = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
        self.assertEqual(folders.default_data_dir("Windows", win, home), Path(win["LOCALAPPDATA"]) / "RooTV")
        self.assertEqual(folders.default_logs_dir("Windows", win, home, legacy=True),
                         Path(win["LOCALAPPDATA"]) / "XiaozhiTV" / "logs")
        self.assertEqual(folders.default_data_dir("Linux", {}, home, legacy=True), home / ".local/share/xiaozhi-tv")
        self.assertEqual(folders.default_logs_dir("Linux", {"XDG_STATE_HOME": "/s"}, home), Path("/s/rootv/logs"))
        # The tray and the core compute the same folder.
        for system in ("Darwin", "Windows", "Linux"):
            self.assertEqual(paths.data_dir(dict(win), home, system), datadir.desktop_data_dir(system, dict(win), home))

    def test_data_env_new_and_old(self):
        self.assertEqual(folders.explicit_data_dir({"XIAOZHI_TV_DATA": "/old"}), Path("/old"))
        self.assertEqual(folders.explicit_data_dir({"XIAOZHI_TV_DATA": "/old", "ROOTV_DATA": "/new"}), Path("/new"))
        self.assertIsNone(folders.explicit_data_dir({"ROOTV_DATA": ""}))
        data = Path("/d")
        for env in ({"ROOTV_DATA": "/d"}, {"XIAOZHI_TV_DATA": "/d"}):
            self.assertEqual(paths.core_command(data, env=env)[-2:], ["--data", str(data)])
            self.assertEqual(paths.logs_dir(data, env, Path("/h"), "Darwin"), data / "logs")
        self.assertEqual(paths.core_command(data, env={"XIAOZHI_TV_CORE": "old-core"})[:1], ["old-core"])
        self.assertEqual(paths.core_command(data, env={"ROOTV_CORE": "new", "XIAOZHI_TV_CORE": "old"})[:1], ["new"])

    def test_release_and_packaging_names(self):
        sys.path.insert(0, str(ROOT / "packaging" / "desktop"))
        try:
            import release
        finally:
            sys.path.remove(str(ROOT / "packaging" / "desktop"))
        self.assertEqual(release.expected_files("9.9.9"), [
            "RooTV-9.9.9-windows-x64-setup.exe", "RooTV-9.9.9-windows-x64.zip", "RooTV-9.9.9-macos-arm64.dmg",
            "RooTV-9.9.9-macos-x86_64.dmg", "rootv-9.9.9-linux-x86_64.tar.gz"])
        spec = (ROOT / "packaging" / "desktop" / "tray.spec").read_text(encoding="utf-8")
        for needle in ('name="RooTV.app"', '"io.github.ai-passport-tv.RooTV"', '"LSHasLocalizedDisplayName": True',
                       '"desktop/assets"', '"tools.desktop_folders"', '"desktop.legacy"'):
            self.assertIn(needle, spec)
        build = (ROOT / "packaging" / "desktop" / "build.py").read_text(encoding="utf-8")
        self.assertIn('NAME_ZH = "小鼠电视"', build)
        self.assertIn("zh-Hans", build)

    def test_installer_upgrades_the_old_installation(self):
        iss = (ROOT / "packaging" / "desktop" / "rootv.iss").read_text(encoding="utf-8-sig")
        # The AppId of Xiaozhi TV: one entry in Programs and Features, upgraded in place.
        self.assertIn("AppId={{6F1C2B7E-3D44-4A8E-9B1F-7A5C2E9D4B10}", iss)
        for needle in ("AppName=小鼠电视 RooTV", "DefaultDirName={autopf}\\RooTV", "UsePreviousAppDir=no",
                       "OutputBaseFilename=RooTV-{#AppVersion}-windows-x64-setup", "SetupIconFile={#SourceDir}\\rootv.ico",
                       '#define CoreExe "core\\rootv-core.exe"', '#define FirewallRule "RooTV"',
                       '#define LegacyFirewallRule "Xiaozhi TV"', "function PrepareToInstall",
                       "'XiaozhiTV');", "'RooTV');", '{group}\\小鼠电视'):
            self.assertIn(needle, iss)

    def test_old_names_only_where_they_belong(self):
        """The old product name survives only as history or to find the old install."""
        allowed = {"desktop/__init__.py", "desktop/autostart.py", "desktop/legacy.py", "desktop/tray.py",
                   "desktop/paths.py", "tools/desktop_folders.py"}
        for path in sorted((ROOT / "desktop").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"小智电视|Xiaozhi ?TV|xiaozhi-tv", text):
                self.assertIn(path.relative_to(ROOT).as_posix(), allowed)


class MoveTest(unittest.TestCase):
    """tools/desktop_folders.resolve on each OS's layout (the OS name is a parameter,
    the file system is this machine's)."""

    def test_fresh_install(self):
        for system in ("Darwin", "Windows", "Linux"):
            home = Home(self, system)
            got = home.resolve()
            self.assertEqual((got.data, got.logs, got.legacy, got.migrated), (home.new, home.new_logs, False, False))
            self.assertFalse(home.new.exists(), "nothing created by resolving")

    def test_moves_the_old_folder_and_logs(self):
        for system in ("Darwin", "Windows", "Linux"):
            with self.subTest(system=system):
                home = Home(self, system)
                home.old_install()
                got = home.resolve()
                self.assertTrue(got.migrated and not got.legacy)
                self.assertEqual((got.data, got.logs), (home.new, home.new_logs))
                self.assertEqual((home.new / "secrets" / "relay-key").read_text(encoding="utf-8"), "k")
                self.assertEqual((home.new / "tv.db").read_bytes()[:6], b"SQLite")
                self.assertEqual((home.new_logs / "core.log").read_text(encoding="utf-8"), "old log\n")
                # The old path holds only the note now.
                self.assertEqual([p.name for p in home.old.iterdir()], [folders.NOTE_NAME])
                note = (home.old / folders.NOTE_NAME).read_text(encoding="utf-8")
                self.assertIn(str(home.new), note)
                self.assertIn("小鼠电视", note)
                self.assertTrue(any("搬到了" in m for m in got.messages))
                if system != "Windows":
                    self.assertFalse(home.old_logs.exists())
                if system == "Linux":
                    self.assertFalse(home.old_logs.parent.exists(), "~/.local/state/xiaozhi-tv removed when empty")
                # The next start: already moved, nothing to do, nothing said.
                again = home.resolve()
                self.assertEqual((again.data, again.migrated, again.legacy, again.messages),
                                 (home.new, False, False, []))

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_permissions_survive(self):
        home = Home(self, "Darwin")
        home.old_install()
        (home.old / "secrets").chmod(0o700)
        (home.old / "secrets" / "relay-key").chmod(0o600)
        home.resolve()
        self.assertEqual(stat.S_IMODE((home.new / "secrets").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((home.new / "secrets" / "relay-key").stat().st_mode), 0o600)

    def test_both_present_keeps_both(self):
        home = Home(self, "Darwin")
        home.old_install()
        home.new.mkdir(parents=True)
        (home.new / "tv.db").write_bytes(b"new")
        got = home.resolve()
        self.assertEqual((got.data, got.migrated, got.legacy), (home.new, False, False))
        self.assertEqual((home.new / "tv.db").read_bytes(), b"new")
        self.assertEqual((home.old / "secrets" / "relay-key").read_text(encoding="utf-8"), "k", "old left alone")
        self.assertFalse((home.old / folders.NOTE_NAME).exists())
        self.assertTrue(any("旧数据文件夹也还在" in m for m in got.messages))

    def test_empty_new_folder_does_not_block_the_move(self):
        home = Home(self, "Linux")
        home.old_install()
        home.new.mkdir(parents=True)
        (home.new / ".DS_Store").write_bytes(b"")
        got = home.resolve()
        self.assertTrue(got.migrated)
        self.assertEqual((home.new / "tv.db").read_bytes()[:6], b"SQLite")

    def test_new_path_taken_by_a_file(self):
        home = Home(self, "Linux")
        home.old_install()
        home.new.parent.mkdir(parents=True, exist_ok=True)
        home.new.write_text("not a folder", encoding="utf-8")
        got = home.resolve()
        self.assertTrue(got.legacy)
        self.assertEqual(got.data, home.old)
        self.assertTrue((home.old / "tv.db").exists())

    def test_note_only_is_not_an_install(self):
        home = Home(self, "Darwin")
        home.old.mkdir(parents=True)
        (home.old / folders.NOTE_NAME).write_text("moved", encoding="utf-8")
        got = home.resolve()
        self.assertEqual((got.data, got.migrated, got.legacy), (home.new, False, False))

    def test_crash_after_rename_before_note(self):
        """The rename is the commit point: with the new folder in place and no old
        one (a crash right after os.rename), the next start just uses the new one."""
        home = Home(self, "Darwin")
        home.old_install(logs=False)
        os.rename(home.old, home.new)
        got = home.resolve()
        self.assertEqual((got.data, got.migrated, got.legacy, got.messages), (home.new, False, False, []))

    def test_failed_rename_changes_nothing(self):
        home = Home(self, "Darwin")
        home.old_install()
        with mock.patch.object(folders.os, "rename", side_effect=PermissionError(13, "Permission denied")):
            got = home.resolve()
        self.assertTrue(got.legacy)
        self.assertEqual((got.data, got.logs), (home.old, home.old_logs))
        self.assertFalse(home.new.exists())
        self.assertTrue((home.old / "tv.db").exists())
        self.assertTrue(any("没能把" in m for m in got.messages))
        # A later start, when the rename works, moves it.
        self.assertTrue(home.resolve().migrated)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "POSIX permissions (root ignores them)")
    def test_unwritable_parent(self):
        home = Home(self, "Darwin")
        home.old_install(logs=False)
        parent = home.old.parent
        parent.chmod(0o555)
        self.addCleanup(parent.chmod, 0o755)
        got = home.resolve()
        self.assertTrue(got.legacy)
        self.assertTrue((home.old / "tv.db").exists())

    def test_explicit_folder_is_never_moved(self):
        home = Home(self, "Darwin")
        home.old_install()
        env = dict(home.env, XIAOZHI_TV_DATA=str(home.old))
        got = folders.resolve("Darwin", env, home.root)
        self.assertTrue(got.explicit)
        self.assertEqual((got.data, got.logs), (home.old, home.old / "logs"))
        self.assertFalse(home.new.exists())

    def test_no_migrate_reads_the_old_folder(self):
        home = Home(self, "Darwin")
        home.old_install()
        got = home.resolve(migrate=False)
        self.assertEqual((got.data, got.legacy), (home.old, True))
        self.assertFalse(home.new.exists())


class LockedTest(unittest.TestCase):
    """An old tray or core still running: their lock files are held."""

    def hold(self, path: Path):
        handle = folders._try_lock(path)
        self.assertNotIn(handle, (None, True))
        self.addCleanup(folders._release, handle)
        return handle

    def test_old_tray_running_keeps_the_old_folder(self):
        home = Home(self, "Darwin")
        home.old_install()
        tray = SingleInstance(home.old / "run" / "tray.lock")
        self.assertTrue(tray.acquire())
        self.addCleanup(tray.release)
        got = home.resolve()
        self.assertTrue(got.legacy)
        self.assertEqual((got.data, got.logs), (home.old, home.old_logs))
        self.assertFalse(home.new.exists())
        self.assertTrue(any("旧版小智电视还在用" in m for m in got.messages))

    def test_old_core_running_keeps_the_old_folder(self):
        home = Home(self, "Linux")
        home.old_install()
        self.hold(home.old / "run" / "core.lock")
        self.assertTrue(folders.in_use(home.old))
        self.assertTrue(home.resolve().legacy)

    def test_stopping_the_old_tray_then_moving(self):
        home = Home(self, "Darwin")
        home.old_install()
        tray = SingleInstance(home.old / "run" / "tray.lock")
        self.assertTrue(tray.acquire())
        self.addCleanup(tray.release)
        asked = []

        def stop_old(old):
            asked.append(old)
            return True

        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 2:
                tray.release()      # the old tray quits two seconds later

        got = home.resolve(stop_old=stop_old, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(asked, [home.old])
        self.assertTrue(got.migrated)
        self.assertTrue((home.new / "tv.db").exists())

    def test_old_tray_does_not_quit_in_time(self):
        home = Home(self, "Darwin")
        home.old_install()
        tray = SingleInstance(home.old / "run" / "tray.lock")
        self.assertTrue(tray.acquire())
        self.addCleanup(tray.release)
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        got = home.resolve(stop_old=lambda old: True, wait_s=5, sleep=sleep, clock=lambda: now[0])
        self.assertTrue(got.legacy)
        self.assertGreaterEqual(now[0], 5)
        got = home.resolve(stop_old=lambda old: False, sleep=sleep, clock=lambda: now[0])
        self.assertTrue(got.legacy)

    def test_lock_check_leaves_no_lock_behind(self):
        home = Home(self, "Darwin")
        home.old_install()
        self.assertFalse(folders.in_use(home.old))
        tray = SingleInstance(home.old / "run" / "tray.lock")
        self.assertTrue(tray.acquire(), "the check released what it took")
        tray.release()


class StopOldTrayTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.old = Path(tmp.name)
        (self.old / "run").mkdir()
        (self.old / "run" / "tray.lock").write_text("4242\n", encoding="ascii")
        self.killed = []

    def ps(self, comm):
        def run(argv, **kw):
            self.assertEqual(argv[:4], ["ps", "-o", "comm=", "-p"])
            return subprocess.CompletedProcess(argv, 0, comm + "\n", "")
        return run

    def test_stops_the_frozen_old_tray(self):
        for comm in ("/Applications/Xiaozhi TV.app/Contents/MacOS/XiaozhiTV", "/home/u/xiaozhi-tv/xiaozhi-tv"):
            self.killed.clear()
            asked = legacy.stop_old_tray(self.old, "darwin", run=self.ps(comm),
                                         kill=lambda pid, sig: self.killed.append((pid, sig)))
            self.assertTrue(asked)
            self.assertEqual(self.killed, [(4242, signal.SIGTERM)])

    def test_leaves_anything_else_alone(self):
        logs = []
        for comm in ("Python", "/usr/bin/python3", "RooTV", ""):
            self.assertFalse(legacy.stop_old_tray(self.old, "linux", run=self.ps(comm),
                                                  kill=lambda pid, sig: self.killed.append(pid), log=logs.append))
        self.assertEqual(self.killed, [])
        (self.old / "run" / "tray.lock").write_text("", encoding="ascii")
        self.assertFalse(legacy.stop_old_tray(self.old, "darwin", run=self.ps("XiaozhiTV"),
                                              kill=lambda pid, sig: self.killed.append(pid)))
        self.assertEqual(self.killed, [])

    def test_windows_by_image_name(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.assertTrue(legacy.stop_old_tray(self.old, "win32", run=run))
        self.assertEqual(calls, [["taskkill", "/F", "/T", "/IM", "XiaozhiTV.exe"]])


class FakeEntry:
    def __init__(self, present=False, disabled=False):
        self.present, self.disabled, self.enabled = present, disabled, present and not disabled

    def exists(self):
        return self.present

    def disabled_by_user(self):
        return self.disabled

    def is_enabled(self):
        return self.enabled

    def enable(self):
        self.present = self.enabled = True

    def disable(self):
        self.present = self.enabled = False


class LoginItemMigrationTest(unittest.TestCase):
    def app(self, prefs: dict, item, old):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        saved = Prefs(paths.prefs_file(data))
        for key, value in prefs.items():
            saved.set(key, value)
        return App(data, None, None, None, login_item=item, legacy_login_item=old, logs_dir=data / "logs")

    def test_old_entry_replaced_by_the_new_one(self):
        item, old = FakeEntry(), FakeEntry(present=True)
        app = self.app({"autostart_initialised": True}, item, old)     # tray.json moved from XiaozhiTV
        app._initialise_login_item()
        self.assertTrue(item.enabled)
        self.assertFalse(old.present)
        self.assertEqual(app.prefs.get("login_item_name"), "RooTV")
        # Once only: the user's later choice is respected.
        item.disable()
        app._initialise_login_item()
        self.assertFalse(item.enabled)

    def test_off_stays_off(self):
        for prefs, old in (({"autostart_initialised": True, "autostart": False}, FakeEntry()),
                           ({"autostart_initialised": True}, FakeEntry(present=True, disabled=True))):
            item = FakeEntry()
            app = self.app(prefs, item, old)
            app._initialise_login_item()
            self.assertFalse(item.enabled)
            self.assertFalse(old.present)

    def test_uninstaller_removed_the_old_entry(self):
        """The old Windows uninstaller deletes its Run value before the new tray
        starts: on by default still means on."""
        item = FakeEntry()
        self.app({"autostart_initialised": True}, item, FakeEntry())._initialise_login_item()
        self.assertTrue(item.enabled)

    def test_legacy_entries_per_os(self):
        with tempfile.TemporaryDirectory() as home:
            mac = autostart.legacy_for_this_os(Path("/x"), "darwin", home=Path(home))
            self.assertEqual(mac.path.name, "io.github.ai-passport-tv.XiaozhiTV.plist")
            self.assertFalse(mac.exists())
            mac.path.parent.mkdir(parents=True)
            mac.path.write_bytes(b"<plist/>")
            self.assertTrue(mac.exists())
            mac.disable()
            self.assertFalse(mac.path.exists())
            new = autostart.for_this_os(Path("/Applications/RooTV.app/Contents/MacOS/RooTV"), "darwin")
            self.assertEqual(new.path.name, "io.github.ai-passport-tv.RooTV.plist")
            xdg = autostart.legacy_for_this_os(Path("/x"), "linux", config_home=Path(home))
            self.assertEqual(xdg.path, Path(home) / "autostart" / "xiaozhi-tv.desktop")
            current = autostart.XdgAutostart(Path("/opt/rootv/rootv"), config_home=Path(home))
            self.assertEqual(current.path.name, "rootv.desktop")
            self.assertIn("Name=小鼠电视", current.entry())
        from tests.test_desktop_tray import FakeWinreg
        reg = FakeWinreg()
        old = autostart.legacy_for_this_os(Path(r"C:\Program Files\RooTV\RooTV.exe"), "win32", winreg=reg)
        self.assertFalse(old.exists())
        reg.keys.setdefault(autostart.RUN_KEY, {})["XiaozhiTV"] = r'"C:\Program Files\Xiaozhi TV\XiaozhiTV.exe"'
        self.assertTrue(old.exists())
        self.assertFalse(old.disabled_by_user())
        reg.keys.setdefault(autostart.APPROVED_KEY, {})["XiaozhiTV"] = bytes([3] + [0] * 11)
        self.assertTrue(old.disabled_by_user())
        old.disable()
        self.assertFalse(old.exists())


class IconFilesTest(unittest.TestCase):
    """The generated icons (tools/make_icons.py) are tracked and have the right sizes."""

    def test_tray_icons(self):
        for kind in icons.KINDS:
            self.assertEqual(png_size(icons.file_for(kind, True, retina=False)), (18, 18))
            self.assertEqual(png_size(icons.file_for(kind, True)), (36, 36))
            self.assertEqual(png_size(icons.file_for(kind, False, size=16)), (16, 16))
            self.assertEqual(png_size(icons.file_for(kind, False)), (32, 32))
        self.assertEqual(icons.file_for("nonsense", False), icons.file_for("idle", False))

    def test_tray_template_is_black_and_alpha(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        for kind in icons.KINDS:
            image = Image.open(icons.file_for(kind, True)).convert("RGBA")
            self.assertEqual(image.getextrema()[:3], ((0, 0), (0, 0), (0, 0)), kind)
            self.assertEqual(icons.draw(kind, True).size, (36, 36))
            self.assertEqual(icons.draw(kind, False).size, (32, 32))
            self.assertEqual(icons._drawn(kind, True).size, (64, 64))

    def test_app_icons(self):
        app = ROOT / "packaging" / "desktop" / "icons"
        self.assertEqual(png_size(app / "rootv.png"), (256, 256))
        ico = (app / "rootv.ico").read_bytes()
        self.assertEqual(ico[:4], b"\x00\x00\x01\x00")
        count = int.from_bytes(ico[4:6], "little")
        sizes = {ico[6 + 16 * i] or 256 for i in range(count)}
        self.assertTrue({16, 24, 32, 48, 256} <= sizes, sizes)
        icns = (app / "rootv.icns").read_bytes()
        self.assertEqual(icns[:4], b"icns")
        for kind in (b"ic10", b"ic09", b"ic08", b"ic07"):    # 1024, 512, 256, 128
            self.assertIn(kind, icns)
        for path in app.iterdir():
            self.assertLess(path.stat().st_size, 1024 * 1024, path.name)

    def test_web_icons(self):
        relay = ROOT / "server" / "relay" / "assets"
        site = ROOT / "packaging" / "site" / "assets"
        for name, size in (("apple-touch-icon.png", 180), ("icon-180.png", 180), ("icon-512.png", 512)):
            self.assertEqual(png_size(relay / name), (size, size))
            self.assertEqual((relay / name).read_bytes(), (site / name).read_bytes(), name)
        for name, size in (("icon-192.png", 192), ("icon-maskable-192.png", 192), ("icon-maskable-512.png", 512)):
            self.assertEqual(png_size(relay / name), (size, size))
        self.assertIn(b"<svg", (relay / "favicon.svg").read_bytes())
        self.assertEqual((relay / "favicon.ico").read_bytes()[:4], b"\x00\x00\x01\x00")


if __name__ == "__main__":
    unittest.main()
