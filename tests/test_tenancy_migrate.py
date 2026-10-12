"""The single-user → multi-user migration (server/tenancy/migrate.py).

The contract: nothing is lost (counts match on both sides), the old files are
never touched (they are the rollback), a dry run writes nothing, the tool
refuses a non-empty database, and --export turns the database back into a
relay-state.json the old relay can read. Phones that were signed in stay signed
in, and the printed QR code keeps working.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.relay.state import State
from server.relay.tenants import Caller
from server.relay.web import Relay
from server.tenancy import migrate
from server.tenancy.accounts import Accounts

NOW = 2_000_000_000.0
TOKEN_A = "phone-a-" + "a" * 35
TOKEN_B = "phone-b-" + "b" * 35
CODE = "ABCDEFGHIJKLMNOPQRST"


def video(bvid, site="bilibili"):
    return {"bvid": bvid, "title": f"t{bvid}", "duration": 600, "pubdate": 0, "site": site}


STATE = {
    "creators": [
        {"id": "up01", "site": "bilibili", "source": "42", "name": "老番茄",
         "videos": [video("a"), video("b"), video("c")], "fetched_at": NOW - 60,
         "watched": ["a"], "positions": {"b": 120.0}, "kind": "up"},
        {"id": "sr01", "site": "bilibili", "source": "ss1", "name": "蜡笔小新",
         "videos": [video("ep1/1"), video("ep2/2")], "fetched_at": NOW - 60,
         "watched": [], "positions": {"ep1/1": 33.5}, "kind": "series"},
    ],
    "cast": {"video": video("BVcast"), "position": 12.0, "done": False},
    "history": [{"site": "bilibili", "id": "BVcast", "title": "tBVcast", "at": NOW - 10}],
    "sessions": {TOKEN_A: NOW - 86400, TOKEN_B: NOW - 60},
    "code": CODE,
}


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = migrate.main([str(a) for a in argv])
    return code, out.getvalue()


def digests(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.passport = root / "passport"
        self.data.mkdir()
        self.passport.mkdir()
        (self.data / "relay-state.json").write_text(json.dumps(STATE, ensure_ascii=False), encoding="utf-8")
        (self.data / "channel-groups.json").write_text(json.dumps({"ch001": "音乐", "x": "不存在的组"},
                                                                  ensure_ascii=False), encoding="utf-8")
        (self.data / "channels.txt").write_text("ch000 | CCTV1 | https://x/1\nch001 | 湖南卫视 | https://x/2\n",
                                                encoding="utf-8")
        (self.data / "now-playing.json").write_text('{"channel": "up01", "at": 1}', encoding="utf-8")
        (self.data / "relay-events.json").write_text('{"events": [{"seq": 4, "action": "cast"}]}',
                                                     encoding="utf-8")
        (self.passport / "now-playing.json").write_text('{"channel": "ch000", "at": 1}', encoding="utf-8")
        self.before = digests(root)
        self.args = ["--data-dir", self.data, "--passport-dir", self.passport]

    def tearDown(self):
        self.tmp.cleanup()

    def old_files_untouched(self):
        after = digests(Path(self.tmp.name))
        for name, digest in self.before.items():
            self.assertEqual(after.get(name), digest, name)

    def test_dry_run_writes_nothing(self):
        code, out = run(*self.args, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("演练完成", out)
        self.assertEqual(digests(Path(self.tmp.name)), self.before, "not one file added or changed")

    def test_everything_arrives(self):
        code, out = run(*self.args, "--public-url", "https://example.com")
        self.assertEqual(code, 0, out)
        self.assertNotIn("不一致", out)
        self.old_files_untouched()
        accounts = Accounts(self.data / "tv.db")
        self.addCleanup(accounts.close)
        admin = accounts.admin_id()
        data = accounts.load_user_state(admin)
        self.assertEqual(data["creators"], STATE["creators"])
        self.assertEqual(data["history"], STATE["history"])
        self.assertEqual(data["group_overrides"], {"ch001": "音乐"}, "unknown groups dropped, like the reader does")
        for token in (TOKEN_A, TOKEN_B):
            self.assertEqual(accounts.session(token)["role"], "admin", "phones stay signed in")
        devices = {d["id"]: d for d in accounts.all_devices()}
        self.assertEqual(set(devices), {"legacy-xingzhi", "legacy-passport"})
        self.assertEqual(devices["legacy-xingzhi"]["remote_code"], CODE, "the printed QR keeps working")
        self.assertEqual((devices["legacy-xingzhi"]["legacy_port"], devices["legacy-passport"]["legacy_port"]),
                         (8098, 8097))
        for device in devices:
            self.assertEqual(accounts.load_device_state(device)["cast"], STATE["cast"])
            self.assertEqual(accounts.owner(device), admin)
        self.assertEqual(accounts.cached_source("bilibili", "42", "up")["videos"], STATE["creators"][0]["videos"])
        xingzhi = self.data / "devices" / "legacy-xingzhi"
        self.assertIn("http://127.0.0.1:8099/ch/legacy-xingzhi/up01", (xingzhi / "relay-channels.txt").read_text())
        self.assertEqual((xingzhi / "remote-url.txt").read_text().strip(), f"HTTPS://EXAMPLE.COM/TV/R/{CODE}")
        self.assertEqual((xingzhi / "relay-events.json").read_text(), (self.data / "relay-events.json").read_text())
        self.assertEqual((self.data / "devices" / "legacy-passport" / "now-playing.json").read_text(),
                         (self.passport / "now-playing.json").read_text())

    def test_refuses_a_database_with_data_and_replace_keeps_a_copy(self):
        self.assertEqual(run(*self.args)[0], 0)
        code, out = run(*self.args)
        self.assertEqual(code, 2)
        self.assertIn("--replace", out)
        code, out = run(*self.args, "--replace")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(list(self.data.glob("tv.db.bak-*"))) >= 1, True)
        self.old_files_untouched()

    def test_corrupt_state_is_refused_and_nothing_written(self):
        (self.data / "relay-state.json").write_text("{not json", encoding="utf-8")
        code, out = run(*self.args)
        self.assertEqual(code, 2)
        self.assertFalse((self.data / "tv.db").exists())

    def test_export_for_rollback(self):
        self.assertEqual(run(*self.args)[0], 0)
        accounts = Accounts(self.data / "tv.db")
        self.addCleanup(accounts.close)
        relay = Relay(None, None, None, "ffmpeg", "k" * 16, accounts=accounts, devices_dir=self.data / "devices",
                      log=lambda _m: None)
        state = relay.user_state(accounts.admin_id())
        state.mark_watched("up01", "b")       # progress made after the cut-over
        state.save()
        out = Path(self.tmp.name) / "export.json"
        code, text = run(*self.args, "--export", out)
        self.assertEqual(code, 0, text)
        exported = State(out)
        self.assertEqual(exported.get("up01").watched, ["a", "b"])
        self.assertEqual(exported.sessions, STATE["sessions"], "sessions come from the old file")
        self.assertEqual(exported.code, CODE)
        self.assertEqual(exported.cast.bvid, "BVcast")
        self.assertEqual(run(*self.args, "--export", out)[0], 2, "never overwrites")
        self.old_files_untouched()

    def test_migrated_phone_and_qr_work_against_the_new_relay(self):
        self.assertEqual(run(*self.args, "--public-url", "https://example.com")[0], 0)
        accounts = Accounts(self.data / "tv.db")
        self.addCleanup(accounts.close)
        relay = Relay(None, None, None, "ffmpeg", "k" * 16, accounts=accounts, devices_dir=self.data / "devices",
                      main_channels=self.data / "channels.txt", log=lambda _m: None, clock=lambda: NOW)
        info = accounts.session(TOKEN_B)
        caller = Caller(user=info["user_id"], role=info["role"], name=info["name"])
        summary = relay.summary(caller)
        self.assertEqual([d["id"] for d in summary["devices"]], ["legacy-xingzhi", "legacy-passport"])
        self.assertEqual([c["name"] for c in summary["creators"]], ["老番茄"])
        groups = {g["name"]: [c["id"] for c in g["channels"]] for g in summary["groups"]}
        self.assertIn("ch001", groups["音乐"])
        self.assertEqual(accounts.device_by_remote_code(CODE)["id"], "legacy-xingzhi")


if __name__ == "__main__":
    unittest.main()
