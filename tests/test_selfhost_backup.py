"""Export / restore of all settings (server/selfhost/backup.py, server/relay/settingsbackup.py).

What a backup holds: the settings and the household's data, never the caches
that can be rebuilt; without login information it carries no secrets/, no
OpenList database, no cookies, no library passwords and no proxy password.
Every archive has a manifest with sha256 per file, and a restore accepts only
the paths a backup can contain: no absolute paths, no "..", no symlinks, no
unlisted or tampered files, size caps. A restore first moves the current files
into backups/pre-restore-*/ and puts everything back if any step fails. The
daily backup skips unchanged days and keeps 7 days. The endpoints are 404 to
everyone but the admin, and exports and restores are in the audit log.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import setup, web
from server.selfhost import backup, bootstrap
from server.selfhost.supervisor import Supervisor
from server.tenancy.accounts import Accounts

from test_relay_web import KEY, FakeClient, FakeYouTube

CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/129.0 Safari/537.36"
CONFIG = ('[server]\nname = "我家"\n\n[youtube]\nmode = "local"\n'
          'proxy = "http://alice:hunter2@127.0.0.1:7890"\ncookies = "youtube/cookies.txt"\n')
LIBRARY = {"online": True, "sources": [
    {"id": "aaaa1111", "kind": "webdav", "name": "NAS", "url": "http://nas:5005/dav", "user": "me", "password": "pw-nas"},
    {"id": "bbbb2222", "kind": "local", "name": "电影", "path": "/Movies"}]}


def seed(root: Path, users=("管理员",)) -> None:
    """A data folder like the owner's: settings, data, logins, and caches that must stay out."""
    root.mkdir(parents=True, exist_ok=True)
    accounts = Accounts(root / "tv.db")
    for i, name in enumerate(users):
        accounts.create_user(name, "admin" if i == 0 else "user")
    accounts.close()
    files = {
        "config.toml": CONFIG,
        "channels.txt": "ch000 | CCTV1 | https://x/1.m3u8\n",
        "channels.txt.meta.json": '{"ch000": {"routes": []}}',
        "channels.txt.subs.json": '{"subs": []}',
        "library.json": json.dumps(LIBRARY),
        "library-index.json": '{"items": {}}',
        "library-meta.json": '{"m": 1}',
        "library-art/douban-1.jpg": "jpeg",
        "pc-progress.json": '{"1": {}}',
        "live-plays.json": '{"1": {}}',
        "feeds/1.json": '{"writeback": true}',
        "devices/aaaaaaaaaaa1/channel-groups.json": '{"ch000": "央视"}',
        "devices/aaaaaaaaaaa1/now-playing.json": "{}",
        "openlist/settings.json": '{"disabled": false}',
        # logins
        "secrets/relay-key": "relay-secret",
        "secrets/wifi-key": "wifi-secret",
        "secrets/openlist-admin": "drive-admin-pw",
        "secrets/bilibili/1.json": '{"cookies": "SESSDATA=x"}',
        "secrets/setup-code": "K7P4QX2M9A",
        "openlist/data/config.json": '{"jwt_secret": "j"}',
        "openlist/data/log/log.log": "log",
        "openlist/data/temp/x": "tmp",
        "youtube/cookies.txt": "# Netscape\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n",
        # never in a backup
        "library-cache/a.bin": "cache", "library-subs/1.vtt": "WEBVTT", "covers/live/1.jpg": "c",
        "logos/ch000.png": "l", "epg/abc.xml": "<tv/>", "epg/abc.json": "{}", "firmware/manifest.json": "{}",
        "bin/ffmpeg": "bin", "youtube/bin/yt-dlp": "bin", "openlist/bin/openlist": "bin",
        "run/status.json": "{}", "backups/tv-20261020.db": "old", "exports/user-3.json": "{}", "logs/core.log": "log",
        "channels.txt.bak-20261027-082149": "old", "config.toml.bak-20261022": "old",
        "desktop.json": '{"trusted_networks": []}', "tray.json": "{}",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    db = sqlite3.connect(str(root / "openlist" / "data" / "data.db"))
    db.execute("CREATE TABLE storages (id INTEGER, addition TEXT)")
    db.execute("INSERT INTO storages VALUES (1, 'refresh_token=abc')")
    db.commit()
    db.close()


def names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def rezip(source: Path, target: Path, change=None, extra=()) -> None:
    """Copy an archive, optionally changing members (change(name, data) -> data or None to drop)."""
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        for info in old.infolist():
            data = old.read(info.filename)
            if change is not None:
                data = change(info.filename, data)
                if data is None:
                    continue
            new.writestr(info, data)
        for info, data in extra:
            new.writestr(info, data)


def set_manifest(path: Path, edit) -> None:
    def change(name, data):
        if name != backup.MANIFEST:
            return data
        manifest = json.loads(data)
        edit(manifest)
        return json.dumps(manifest).encode()
    tmp = path.with_name("edit.zip")
    rezip(path, tmp, change)
    os.replace(tmp, path)


def staged(root, timeout: float = 5.0) -> bool:
    """The restore endpoint answers the page first and only then moves the staged copy into place (the
    supervisor stops the relay as soon as it sees it), so the copy can land a moment after the answer."""
    deadline = time.monotonic() + timeout
    while not backup.pending(root) and time.monotonic() < deadline:
        time.sleep(0.02)
    return backup.pending(root)

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"
        seed(self.root)
        self.out = Path(self.tmp.name) / "out"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_without_logins_carries_settings_and_data_only(self):
        manifest = backup.export(self.root, self.out / "b.zip", logins=False, version="9.9")
        found = names(self.out / "b.zip")
        for rel in ("tv.db", "config.toml", "channels.txt", "channels.txt.meta.json", "channels.txt.subs.json",
                    "library.json", "library-index.json", "library-meta.json", "library-art/douban-1.jpg",
                    "pc-progress.json", "live-plays.json", "feeds/1.json", "openlist/settings.json",
                    "devices/aaaaaaaaaaa1/channel-groups.json", backup.MANIFEST):
            self.assertIn(rel, found)
        for rel in found:
            self.assertFalse(rel.startswith(("secrets/", "openlist/data/", "youtube/", "library-cache/",
                                             "library-subs/", "covers/", "logos/", "epg/", "firmware/", "bin/",
                                             "run/", "backups/", "exports/", "logs/")), rel)
            self.assertNotIn(".bak-", rel)
        self.assertNotIn("devices/aaaaaaaaaaa1/now-playing.json", found)
        self.assertNotIn("desktop.json", found)
        self.assertFalse(manifest["logins"])
        with zipfile.ZipFile(self.out / "b.zip") as archive:
            library = json.loads(archive.read("library.json"))
            config = archive.read("config.toml").decode()
            listed = json.loads(archive.read(backup.MANIFEST))
            for row in listed["files"]:
                self.assertEqual(hashlib.sha256(archive.read(row["path"])).hexdigest(), row["sha256"])
            (self.out / "tv.db").write_bytes(archive.read("tv.db"))
        self.assertEqual([s.get("password", "") for s in library["sources"]], ["", ""])
        self.assertEqual(library["sources"][0]["user"], "me", "only the password goes")
        self.assertIn('proxy = "http://127.0.0.1:7890"', config)
        self.assertNotIn("hunter2", config)
        accounts = Accounts(self.out / "tv.db")
        self.assertEqual(accounts.user(1)["name"], "管理员", "the database is a real, consistent copy")
        accounts.close()
        self.assertEqual(listed["app_version"], "9.9")

    def test_with_logins_adds_secrets_drive_database_and_cookies(self):
        backup.export(self.root, self.out / "b.zip", logins=True)
        found = names(self.out / "b.zip")
        for rel in ("secrets/relay-key", "secrets/wifi-key", "secrets/openlist-admin", "secrets/bilibili/1.json",
                    "openlist/data/config.json", "openlist/data/data.db", "youtube/cookies.txt"):
            self.assertIn(rel, found)
        for rel in ("secrets/setup-code", "openlist/data/log/log.log", "openlist/data/temp/x", "youtube/bin/yt-dlp"):
            self.assertNotIn(rel, found)
        with zipfile.ZipFile(self.out / "b.zip") as archive:
            self.assertIn("hunter2", archive.read("config.toml").decode())
            self.assertEqual(json.loads(archive.read("library.json"))["sources"][0]["password"], "pw-nas")
            (self.out / "data.db").write_bytes(archive.read("openlist/data/data.db"))
        db = sqlite3.connect(str(self.out / "data.db"))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM storages").fetchone()[0], 1)
        db.close()

    def test_symlinks_in_the_data_folder_are_not_followed(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        (self.root / "feeds" / "2.json").symlink_to(outside)
        (self.root / "library-art" / "douban-2.jpg").symlink_to(outside)
        found = names(self.out / "b.zip") if backup.export(self.root, self.out / "b.zip") else set()
        self.assertNotIn("feeds/2.json", found)
        self.assertNotIn("library-art/douban-2.jpg", found)


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"
        seed(self.root)
        self.good = Path(self.tmp.name) / "good.zip"
        backup.export(self.root, self.good, logins=False)
        self.bad = Path(self.tmp.name) / "bad.zip"

    def tearDown(self):
        self.tmp.cleanup()

    def assertRefused(self, path, words):
        with self.assertRaises(backup.BackupError) as caught:
            backup.inspect(path)
        self.assertIn(words, str(caught.exception))

    def test_a_good_backup_passes(self):
        plan = backup.inspect(self.good)
        self.assertIn("tv.db", plan.files)
        self.assertFalse(plan.logins)

    def test_tampered_file_is_refused(self):
        rezip(self.good, self.bad, lambda n, d: b'{"m": 2}' if n == "library-meta.json" else d)
        self.assertRefused(self.bad, "校验和")

    def test_missing_manifest_and_not_a_zip(self):
        rezip(self.good, self.bad, lambda n, d: None if n == backup.MANIFEST else d)
        self.assertRefused(self.bad, "清单")
        self.bad.write_bytes(b"not a zip")
        self.assertRefused(self.bad, "不是 zip")

    def test_path_traversal_and_absolute_paths(self):
        for name in ("../evil.json", "/etc/passwd", "feeds/../../evil", "C:/x", "feeds\\x.json", "feeds/./1.json"):
            data = b"x"

            def edit(manifest, name=name):
                manifest["files"].append({"path": name, "size": 1, "sha256": hashlib.sha256(data).hexdigest()})
            rezip(self.good, self.bad, extra=[(zipfile.ZipInfo(name), data)])
            set_manifest(self.bad, edit)
            with self.assertRaises(backup.BackupError, msg=name):
                backup.inspect(self.bad)

    def test_symlink_member_is_refused(self):
        link = zipfile.ZipInfo("feeds/2.json")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        target = b"/etc/passwd"
        rezip(self.good, self.bad, extra=[(link, target)])
        set_manifest(self.bad, lambda m: m["files"].append(
            {"path": "feeds/2.json", "size": len(target), "sha256": hashlib.sha256(target).hexdigest()}))
        self.assertRefused(self.bad, "符号链接")

    def test_unknown_unlisted_and_login_paths(self):
        data = b"x"
        digest = hashlib.sha256(data).hexdigest()
        rezip(self.good, self.bad, extra=[(zipfile.ZipInfo("library-cache/a.bin"), data)])
        set_manifest(self.bad, lambda m: m["files"].append({"path": "library-cache/a.bin", "size": 1, "sha256": digest}))
        self.assertRefused(self.bad, "不认识的路径")
        rezip(self.good, self.bad, extra=[(zipfile.ZipInfo("feeds/9.json"), data)])
        self.assertRefused(self.bad, "清单没列")
        rezip(self.good, self.bad, extra=[(zipfile.ZipInfo("secrets/relay-key"), data)])
        set_manifest(self.bad, lambda m: m["files"].append({"path": "secrets/relay-key", "size": 1, "sha256": digest}))
        self.assertRefused(self.bad, "不含登录信息")

    def test_sizes_versions_and_the_database(self):
        rezip(self.good, self.bad)
        set_manifest(self.bad, lambda m: m.update(version=backup.FORMAT_VERSION + 1))
        self.assertRefused(self.bad, "更新版本")
        rezip(self.good, self.bad)
        set_manifest(self.bad, lambda m: [r.update(size=r["size"] + 1) for r in m["files"] if r["path"] == "feeds/1.json"])
        self.assertRefused(self.bad, "大小")
        rezip(self.good, self.bad, lambda n, d: None if n == "tv.db" else d)
        set_manifest(self.bad, lambda m: m.update(files=[r for r in m["files"] if r["path"] != "tv.db"]))
        self.assertRefused(self.bad, "tv.db")
        with unittest.mock.patch.object(backup, "UPLOAD_MAX", 100):
            self.assertRefused(self.good, "太大")
        with unittest.mock.patch.object(backup, "TOTAL_MAX", 100):
            self.assertRefused(self.good, "太大")


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"
        seed(self.root, users=("管理员", "Alice"))
        self.archive = Path(self.tmp.name) / "b.zip"
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def fresh(self, name="fresh"):
        other = Path(self.tmp.name) / name
        seed(other, users=("别人",))
        (other / "secrets" / "relay-key").write_text("other-relay", encoding="utf-8")
        (other / "secrets" / "extra").write_text("only here", encoding="utf-8")
        (other / "library.json").write_text(json.dumps({"sources": [dict(LIBRARY["sources"][0], password="pw-here")]}))
        (other / "config.toml").write_text(CONFIG.replace("alice:hunter2", "bob:pw2"), encoding="utf-8")
        (other / "feeds" / "7.json").write_text("{}", encoding="utf-8")
        return other

    def users(self, root):
        accounts = Accounts(root / "tv.db")
        try:
            return [accounts.user(i)["name"] for i in (1, 2) if accounts.user(i)]
        finally:
            accounts.close()

    def test_round_trip_into_a_fresh_folder_with_logins(self):
        backup.export(self.root, self.archive, logins=True)
        other = self.fresh()
        backup.stage(self.archive, other, actor="管理员")
        self.assertTrue(backup.pending(other))
        self.assertEqual(self.users(other), ["别人"], "staging does not touch anything")
        (other / "tv.db-wal").write_bytes(b"stale wal")
        result = backup.apply_pending(other, self.logs.append)
        self.assertTrue(result["ok"], result)
        self.assertFalse(backup.pending(other))
        self.assertEqual(self.users(other), ["管理员", "Alice"])
        self.assertEqual((other / "secrets" / "relay-key").read_text(), "relay-secret")
        self.assertFalse((other / "secrets" / "extra").exists(), "the secrets folder is the backup's")
        self.assertFalse((other / "feeds" / "7.json").exists())
        self.assertFalse((other / "tv.db-wal").exists(), "a stale WAL never pairs with the restored database")
        self.assertEqual((other / "secrets" / "setup-code").read_text(), "K7P4QX2M9A", "setup code untouched")
        self.assertEqual((other / "library-cache" / "a.bin").read_text(), "cache", "caches untouched")
        pre = other / "backups" / result["backup"]
        self.assertEqual((pre / "files" / "secrets" / "extra").read_text(), "only here")
        self.assertEqual((pre / "files" / "tv.db-wal").read_bytes(), b"stale wal")
        self.assertEqual(backup.last_result(other)["backup"], result["backup"])
        accounts = Accounts(other / "tv.db")
        self.assertTrue([r for r in accounts.audit_rows(10) if r["action"] == "backup.restored"])
        accounts.close()
        self.assertEqual(stat.S_IMODE((other / "secrets" / "relay-key").stat().st_mode), 0o600)

    def test_without_logins_keeps_this_machines_logins(self):
        backup.export(self.root, self.archive, logins=False)
        other = self.fresh()
        backup.stage(self.archive, other)
        result = backup.apply_pending(other)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.users(other), ["管理员", "Alice"])
        self.assertEqual((other / "secrets" / "relay-key").read_text(), "other-relay")
        self.assertEqual((other / "secrets" / "extra").read_text(), "only here")
        sources = json.loads((other / "library.json").read_text())["sources"]
        self.assertEqual(sources[0]["password"], "pw-here", "the password is filled in from the current file")
        self.assertIn("bob:pw2@", (other / "config.toml").read_text(), "the proxy keeps this machine's login")

    def test_failure_puts_everything_back(self):
        backup.export(self.root, self.archive, logins=True)
        other = self.fresh()
        before = {p.relative_to(other).as_posix(): p.read_bytes() for p in other.rglob("*") if p.is_file()}
        backup.stage(self.archive, other)
        real, calls = backup._place, []

        def flaky(source, target):
            calls.append(target)
            if len(calls) == 40:
                raise OSError("disk full")
            real(source, target)
        with unittest.mock.patch.object(backup, "_place", flaky):
            result = backup.apply_pending(other, self.logs.append)
        self.assertGreater(len(calls), 30, "the failure came after files had moved and been placed")
        self.assertFalse(result["ok"])
        self.assertIn("全部换回原来的", result["error"])
        after = {p.relative_to(other).as_posix(): p.read_bytes() for p in other.rglob("*") if p.is_file()
                 and not p.relative_to(other).as_posix().startswith(("backups/pre-restore-", "run/restore-result"))}
        self.assertEqual(after, before)
        self.assertFalse(backup.pending(other), "a failed restore is not tried again on every start")

    def test_tampered_stage_is_not_applied(self):
        backup.export(self.root, self.archive)
        other = self.fresh()
        backup.stage(self.archive, other)
        (backup.stage_dir(other) / "files" / "feeds" / "1.json").write_text("changed", encoding="utf-8")
        result = backup.apply_pending(other)
        self.assertFalse(result["ok"])
        self.assertEqual(self.users(other), ["别人"])

    def test_supervisor_stops_children_restores_and_starts_them(self):
        backup.export(self.root, self.archive)
        other = self.fresh()
        layout = bootstrap.Layout(other)
        order = []

        class Fake:
            name, delay, next_start = "relay", 0, 0

            def stop(self):
                order.append("stop")

            def wait_or_kill(self, _deadline):
                order.append("waited")

            def start(self):
                order.append(("start", self_test.users(other)))

        self_test = self
        supervisor = Supervisor(layout, [Fake()], 14, log=lambda _m: None)
        supervisor.started = True
        backup.stage(self.archive, other)
        supervisor._restore()
        self.assertEqual(order, ["stop", "waited", ("start", ["管理员", "Alice"])])

    def test_cli_apply_from_a_file(self):
        backup.export(self.root, self.archive)
        other = self.fresh()
        with unittest.mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(backup.main(["apply", "--data", str(other), "--file", str(self.archive)]), 0)
        self.assertEqual(self.users(other), ["管理员", "Alice"])


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"
        seed(self.root)
        self.now = time.mktime((2026, 10, 20, 4, 0, 0, 0, 0, -1))

    def tearDown(self):
        self.tmp.cleanup()

    def day(self, n):
        return lambda: self.now + n * 86400

    def test_skips_unchanged_days_and_keeps_seven(self):
        self.assertTrue(backup.daily_due(self.root, self.day(0)))
        self.assertEqual(backup.daily(self.root, clock=self.day(0)), "made")
        self.assertFalse(backup.daily_due(self.root, self.day(0)))
        made = self.root / "backups" / "settings-20261020.zip"
        self.assertTrue(made.is_file())
        self.assertFalse(any(n.startswith(("secrets/", "openlist/data/")) for n in names(made)))
        self.assertEqual(backup.daily(self.root, clock=self.day(1)), "unchanged")
        self.assertFalse((self.root / "backups" / "settings-20261021.zip").exists())
        # Touched but the same content: no second copy.
        os.utime(self.root / "pc-progress.json", None)
        self.assertEqual(backup.daily(self.root, clock=self.day(2)), "same")
        for n in range(3, 12):
            (self.root / "pc-progress.json").write_text(json.dumps({"day": n}), encoding="utf-8")
            self.assertEqual(backup.daily(self.root, clock=self.day(n)), "made")
        kept = sorted(p.name for p in (self.root / "backups").glob("settings-*.zip"))
        self.assertEqual(kept[0], "settings-20261024.zip", "older than 7 days are gone")
        self.assertEqual(kept[-1], "settings-20261031.zip")
        self.assertEqual(len(kept), 8)

    def test_newest_is_kept_however_old(self):
        backup.daily(self.root, clock=self.day(0))
        self.assertEqual(backup.daily(self.root, clock=self.day(30)), "unchanged")
        self.assertEqual([p.name for p in (self.root / "backups").glob("settings-*.zip")], ["settings-20261020.zip"])

    def test_not_before_three_in_the_morning(self):
        early = time.mktime((2026, 10, 20, 2, 0, 0, 0, 0, -1))
        self.assertFalse(backup.daily_due(self.root, lambda: early))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        seed(self.root, users=())
        self.now = time.time()
        self.accounts = Accounts(self.root / "tv.db", clock=lambda: self.now)
        self.admin = self.accounts.create_user("管理员", "admin")
        self.alice = self.accounts.create_user("Alice")
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, youtube=FakeYouTube(),
                               main_channels=self.root / "channels.txt", accounts=self.accounts,
                               devices_dir=self.root / "devices", log=lambda _m: None, clock=lambda: self.now,
                               selfhost=setup.SelfHost())
        self.relay.selfhost_data = self.root
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        self.streams.server_close()
        threading.Thread(target=self.manage.serve_forever, daemon=True).start()
        self.accounts.register_device("aaaaaaaaaaa1", "xingzhi-cube-154", "s" * 43)
        guest = self.accounts.new_session(None, "guest", device_id="aaaaaaaaaaa1")
        self.cookies = {"admin": "tvs=" + self.accounts.new_session(self.admin),
                        "alice": "tvs=" + self.accounts.new_session(self.alice), "guest": "tvs=" + guest, "nobody": ""}

    def tearDown(self):
        self.manage.shutdown()
        self.manage.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def raw(self, who, method, path, data=None, kind="application/json"):
        request = urllib.request.Request(f"http://127.0.0.1:{self.manage.server_address[1]}{path}", data=data,
                                         method=method)
        request.add_header("User-Agent", CHROME)
        if self.cookies[who]:
            request.add_header("Cookie", self.cookies[who])
        if data is not None:
            request.add_header("Content-Type", kind)
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=20) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def json(self, who, method, path, body=None):
        data = json.dumps(body or {}).encode() if method == "POST" else None
        status, _, payload = self.raw(who, method, path, data)
        return status, (json.loads(payload) if status == 200 else None)

    def audits(self, action):
        return [r for r in self.accounts.audit_rows(100) if r["action"] == action]

    def test_admin_only(self):
        archive = self.root / "x.zip"
        backup.export(self.root, archive)
        for who in ("alice", "guest", "nobody"):
            self.assertEqual(self.json(who, "GET", "/api/admin/backup")[0], 404, who)
            for path in ("/api/admin/backup/export", "/api/admin/backup/restore", "/api/admin/backup/restore/cancel"):
                self.assertEqual(self.json(who, "POST", path, {"logins": True, "id": "x"})[0], 404, (who, path))
            status, _, _ = self.raw(who, "POST", "/api/admin/backup/restore/upload", archive.read_bytes(), "application/zip")
            self.assertEqual(status, 404, who)
        self.assertFalse(self.audits("backup.export"))
        self.assertFalse((self.root / "run" / backup.UPLOAD).exists())
        self.assertFalse(backup.pending(self.root))

    def test_export_download_is_audited(self):
        status, headers, payload = self.raw("admin", "POST", "/api/admin/backup/export", b'{"logins": false}')
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertRegex(headers["Content-Disposition"], r'attachment; filename="RooTV-backup-\d{8}-\d{4}\.zip"')
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIn("tv.db", archive.namelist())
            self.assertFalse([n for n in archive.namelist() if n.startswith("secrets/")])
        status, headers, payload = self.raw("admin", "POST", "/api/admin/backup/export", b'{"logins": true}')
        self.assertIn("with-logins", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIn("secrets/relay-key", archive.namelist())
        rows = self.audits("backup.export")
        self.assertEqual(len(rows), 2)
        self.assertIn("含登录信息", rows[0]["detail"])
        self.assertFalse(list((self.root / "run").glob(".export-*")), "the temporary archive is removed")

    def test_upload_check_restore_and_cancel(self):
        archive = self.root / "x.zip"
        backup.export(self.root, archive)
        status, _, payload = self.raw("admin", "POST", "/api/admin/backup/restore/upload", b"garbage", "application/zip")
        self.assertFalse(json.loads(payload)["ok"])
        status, _, payload = self.raw("admin", "POST", "/api/admin/backup/restore/upload", archive.read_bytes(),
                                      "application/zip")
        answer = json.loads(payload)
        self.assertTrue(answer["ok"], answer)
        keys = [g["key"] for g in answer["summary"]["groups"]]
        self.assertIn("db", keys)
        self.assertNotIn("secrets", keys)
        self.assertEqual(self.json("admin", "POST", "/api/admin/backup/restore", {"id": "wrong"})[1]["ok"], False)
        status, done = self.json("admin", "POST", "/api/admin/backup/restore", {"id": answer["id"]})
        self.assertEqual((done["ok"], done["restart"]), (True, "manual"), "no supervisor: applied on the next start")
        self.assertTrue(staged(self.root))
        self.assertTrue(self.audits("backup.restore"))
        view = self.json("admin", "GET", "/api/admin/backup")[1]
        self.assertTrue(view["available"])
        self.assertIsNotNone(view["pending"])
        self.json("admin", "POST", "/api/admin/backup/restore/cancel")
        self.assertFalse(backup.pending(self.root))

    def test_restart_is_automatic_under_a_supervisor(self):
        archive = self.root / "x.zip"
        backup.export(self.root, archive)
        (self.root / "run" / "status.json").write_text(json.dumps({"at": self.now}), encoding="utf-8")
        _, _, payload = self.raw("admin", "POST", "/api/admin/backup/restore/upload", archive.read_bytes(),
                                 "application/zip")
        upload = json.loads(payload)
        self.assertTrue(upload["supervised"])
        done = self.json("admin", "POST", "/api/admin/backup/restore", {"id": upload["id"]})[1]
        self.assertEqual(done["restart"], "auto")
        self.assertTrue(staged(self.root))

    def test_not_available_without_a_data_folder(self):
        self.relay.selfhost_data = None
        self.assertFalse(self.json("admin", "GET", "/api/admin/backup")[1]["available"])
        self.assertFalse(self.json("admin", "POST", "/api/admin/backup/export")[1]["ok"])


if __name__ == "__main__":
    unittest.main()
