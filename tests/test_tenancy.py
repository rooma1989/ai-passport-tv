"""Accounts, device admission and the unbound-device session (server/tenancy).

The media-server side is driven through a real AVServer over a socket, so the
handshake hook in server/tv_server.py is covered too: a flashed device with the
shared token is served as its legacy record, a new device is pinned on first
use and gets the bind picture, and a wrong secret is refused.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import frames, overlay
from server.media import FRAME_COUNT, Media
from server.protocol import Kind, Packet, ProtocolError, json_bytes, json_object, receive_packet, send_packet
from server.tenancy import db, gate, unbound
from server.tenancy.accounts import Accounts
from server.tv_server import AVServer

TOKEN = b"host-test-only-not-a-real-secret"
SECRET_A = "A" * 43
SECRET_B = "B" * 43
DEVICE = "0123456789ab"


def hello(**fields):
    return {"version": 1, **fields}


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.accounts = Accounts(Path(self.tmp.name) / "tv.db", clock=lambda: self.now)
        self.admin = self.accounts.create_user("管理员", "admin")

    def tearDown(self):
        self.accounts.close()
        self.tmp.cleanup()

    def test_sessions_are_stored_hashed_and_expire(self):
        token = self.accounts.new_session(self.admin)
        raw = (Path(self.tmp.name) / "tv.db").read_bytes()
        self.assertNotIn(token.encode(), raw, "only the SHA-256 reaches the file")
        self.assertEqual(self.accounts.session(token)["role"], "admin")
        self.now += 180 * 24 * 3600 + 1
        self.assertIsNone(self.accounts.session(token))

    def test_ban_ends_sessions(self):
        user = self.accounts.create_user("x")
        token = self.accounts.new_session(user)
        self.accounts.set_user_status(user, "banned", self.admin)
        self.assertIsNone(self.accounts.session(token))
        self.accounts.set_user_status(user, "active", self.admin)
        self.assertIsNone(self.accounts.session(token), "a ban revokes, an unban does not restore")

    def test_invite_is_single_use_and_expires(self):
        code = self.accounts.create_invite(self.admin, name="妈妈")
        self.assertEqual(self.accounts.invite(code)["name"], "妈妈")
        user, token = self.accounts.accept_invite(code, "")
        self.assertEqual(self.accounts.user(user)["name"], "妈妈")
        self.assertEqual(self.accounts.session(token)["user_id"], user)
        with self.assertRaises(ValueError):
            self.accounts.accept_invite(code, "again")
        late = self.accounts.create_invite(self.admin)
        self.now += 8 * 24 * 3600
        self.assertIsNone(self.accounts.invite(late))
        with self.assertRaises(ValueError):
            self.accounts.accept_invite(late, "late")

    def test_bind_unbind_and_guests(self):
        row = self.accounts.register_device(DEVICE, "ai-passport", SECRET_A)
        self.assertEqual(row["status"], "unclaimed")
        self.assertEqual(len(row["claim_code"]), db.CODE_LENGTH)
        user = self.accounts.create_user("Alice")
        bound = self.accounts.bind(row["claim_code"].lower(), user)
        self.assertEqual((bound["status"], bound["claim_code"], bound["name"]), ("active", None, "Passport 89AB"))
        guest = self.accounts.new_session(None, "guest", device_id=DEVICE)
        own = self.accounts.new_session(user)
        self.accounts.new_remote_code(DEVICE, user)
        self.assertIsNone(self.accounts.session(guest), "a new code ends the device's guests")
        self.assertIsNotNone(self.accounts.session(own))
        self.accounts.save_device_state(DEVICE, {"cast": {"video": {}, "position": 1, "done": False}})
        self.accounts.unbind(DEVICE, user)
        after = self.accounts.device(DEVICE)
        self.assertEqual(after["status"], "unclaimed")
        self.assertNotEqual(after["claim_code"], row["claim_code"])
        self.assertIsNone(self.accounts.load_device_state(DEVICE))
        self.assertEqual(self.accounts.devices_of(user), [])
        self.assertEqual(bytes(after["secret_sha256"]), db.sha256(SECRET_A), "the secret stays pinned")

    def test_adopt_moves_the_legacy_record(self):
        self.accounts.add_legacy_device("legacy-xingzhi", 8098, "xingzhi-cube-154", "小智", self.admin,
                                        remote_code="ABCDEFGHIJKLMNOPQRST")
        self.accounts.save_device_state("legacy-xingzhi", {"cast": None})
        guest = self.accounts.new_session(None, "guest", device_id="legacy-xingzhi")
        self.accounts.register_device(DEVICE, "xingzhi-cube-154", SECRET_A)
        self.accounts.adopt("legacy-xingzhi", DEVICE, self.admin)
        real = self.accounts.device(DEVICE)
        self.assertEqual((real["status"], real["remote_code"], real["name"]), ("active", "ABCDEFGHIJKLMNOPQRST", "小智"))
        self.assertEqual(self.accounts.owner(DEVICE), self.admin)
        self.assertEqual(self.accounts.device("legacy-xingzhi")["status"], "retired")
        self.assertEqual(self.accounts.load_device_state(DEVICE), {"cast": None})
        self.assertEqual(self.accounts.session(guest)["device_id"], DEVICE, "the printed QR's guests follow")
        with self.assertRaises(ValueError):
            self.accounts.adopt("legacy-xingzhi", DEVICE)

    def test_newer_schema_is_refused(self):
        self.accounts.set_meta("schema_version", str(db.SCHEMA_VERSION + 1))
        with self.assertRaises(RuntimeError):
            Accounts(Path(self.tmp.name) / "tv.db")

    def test_ip_prefix(self):
        self.assertEqual(db.ip_prefix("203.0.113.77"), "203.0.113.0/24")
        self.assertEqual(db.ip_prefix("2001:db8:1:2::5"), "2001:db8:1::/48")
        self.assertEqual(db.ip_prefix("junk"), "")


class AdminCommandTests(unittest.TestCase):
    def test_invite_devices_adopt_and_legacy_switch(self):
        import contextlib
        import io
        from server.tenancy import admin
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.db"
            accounts = Accounts(path)
            owner = accounts.create_user("管理员", "admin")
            accounts.set_meta("public_url", "https://example.com")
            accounts.add_legacy_device("legacy-xingzhi", 8098, "xingzhi-cube-154", "小智", owner)
            accounts.register_device(DEVICE, "xingzhi-cube-154", SECRET_A)
            accounts.close()

            def run(*argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    code = admin.main(["--db", str(path), *argv])
                return code, out.getvalue()

            code, out = run("invite", "--name", "Mum")
            self.assertEqual(code, 0)
            self.assertRegex(out, r"https://example.com/tv/i/[A-Z2-7]{20}")
            self.assertIn(DEVICE, run("devices")[1])
            self.assertEqual(run("adopt", "legacy-xingzhi", DEVICE)[0], 0)
            self.assertEqual(run("adopt", "legacy-xingzhi", DEVICE)[0], 1, "a retired record cannot move twice")
            self.assertEqual(run("legacy-token", "off")[0], 0)
            accounts = Accounts(path)
            self.assertEqual(accounts.meta(gate.LEGACY_TOKEN_KEY), "0")
            self.assertEqual(accounts.owner(DEVICE), owner)
            accounts.close()
            self.assertEqual(run("--db", str(Path(tmp) / "missing.db"), "users")[0], 2)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.mono = 0.0
        root = Path(self.tmp.name)
        self.logs = []
        self.gate = gate.Gate(root / "tv.db", root / "devices", logger=self.logs.append,
                              clock=lambda: self.now, monotonic=lambda: self.mono)
        self.accounts = self.gate.accounts
        self.admin = self.accounts.create_user("管理员", "admin")
        self.accounts.add_legacy_device("legacy-xingzhi", 8098, "xingzhi-cube-154", "小智", self.admin)
        self.accounts.set_meta("public_url", "https://example.com")

    def tearDown(self):
        self.accounts.close()
        self.tmp.cleanup()

    def test_old_firmware_with_the_token_is_its_ports_legacy_device(self):
        admission = self.gate.admit(hello(token=TOKEN.decode()), TOKEN, 8098, "203.0.113.5")
        self.assertEqual((admission.device_id, admission.bound, admission.legacy), ("legacy-xingzhi", True, True))
        self.assertEqual(admission.files.now, Path(self.tmp.name) / "devices" / "legacy-xingzhi" / "now-playing.json")
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(token="wrong-token-but-long-enough"), TOKEN, 8098)
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(token=TOKEN.decode()), TOKEN, 8097, "203.0.113.6")
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(), TOKEN, 8098)
        self.accounts.set_meta(gate.LEGACY_TOKEN_KEY, "0")
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(token=TOKEN.decode()), TOKEN, 8098, "203.0.113.7")

    def test_new_device_is_pinned_on_first_use_and_unbound(self):
        first = self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_A, model="ai-passport", fw="v1"),
                                None, 8097, "203.0.113.5")
        self.assertFalse(first.bound)
        self.assertEqual(first.claim_url, f"HTTPS://EXAMPLE.COM/TV/B/{first.claim}")
        self.assertLessEqual(len(first.claim_url), 64)
        row = self.accounts.device(DEVICE)
        self.assertEqual((row["model"], row["fw"], row["last_ip_prefix"]), ("ai-passport", "v1", "203.0.113.0/24"))
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_B), None, 8097, "203.0.113.5")
        self.assertEqual(self.accounts.audit_rows(1)[0]["action"], "device.secret_mismatch")
        user = self.accounts.create_user("Alice")
        self.accounts.bind(first.claim, user)
        bound = self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_A), None, 8097, "203.0.113.5")
        self.assertEqual((bound.device_id, bound.bound, bound.legacy), (DEVICE, True, False))

    def test_repair_window_pins_a_new_secret(self):
        self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_A), None, 8097)
        self.accounts.start_repair(DEVICE, self.admin)
        self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_B), None, 8097)
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_A), None, 8097)
        self.accounts.start_repair(DEVICE, self.admin)
        self.now += 11 * 60
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_A), None, 8097, "198.51.100.1")

    def test_new_firmware_with_the_token_keeps_playing_as_legacy_until_adopted(self):
        fields = hello(device_id=DEVICE, device_secret=SECRET_A, token=TOKEN.decode(), model="xingzhi-cube-154")
        admission = self.gate.admit(fields, TOKEN, 8098)
        self.assertEqual((admission.device_id, admission.legacy), ("legacy-xingzhi", True))
        self.assertEqual(self.accounts.device(DEVICE)["status"], "unclaimed", "registered meanwhile")
        self.accounts.adopt("legacy-xingzhi", DEVICE, self.admin)
        admission = self.gate.admit(fields, TOKEN, 8098)
        self.assertEqual((admission.device_id, admission.legacy), (DEVICE, False))
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(token=TOKEN.decode()), TOKEN, 8098, "203.0.113.9")

    def test_banned_device_and_failure_limit(self):
        self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_A), None, 8097)
        self.accounts.set_device_status(DEVICE, "banned", self.admin)
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(device_id=DEVICE, device_secret=SECRET_A), None, 8097, "198.51.100.2")
        for _ in range(gate.FAILURES_PER_MINUTE):
            with self.assertRaises(ProtocolError):
                self.gate.admit(hello(token="wrong-token-but-long-enough"), TOKEN, 8098, "198.51.100.3")
        with self.assertRaises(ProtocolError):
            self.gate.admit(hello(token=TOKEN.decode()), TOKEN, 8098, "198.51.100.3")
        self.mono += 61
        self.assertTrue(self.gate.admit(hello(token=TOKEN.decode()), TOKEN, 8098, "198.51.100.3").bound)


class UnboundPictureTests(unittest.TestCase):
    def test_picture_lines_fit_and_draw(self):
        lines = unbound.picture_lines("ABCDEFGHIJKLMNOPQRST")
        self.assertEqual(lines, ["BIND THIS TV", "ABCDE FGHIJ", "KLMNO PQRST"])
        frame = overlay.lines_frame(lines)
        self.assertEqual(len(frame), frames.FRAME_PIXELS)
        self.assertIn(overlay.WHITE, frame)
        for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567 ":
            overlay.lines_frame([char])  # every character a code can contain has a glyph

    def test_config_carries_the_bind_url_and_one_channel(self):
        payload = unbound.config_payload({"width": 1}, 7, "HTTPS://EXAMPLE.COM/TV/B/X")
        self.assertEqual((payload["session"], payload["bound"], payload["remote_url"]),
                         (7, 0, "HTTPS://EXAMPLE.COM/TV/B/X"))
        self.assertEqual(payload["channel_list"], [{"id": "bind", "name": "扫码绑定", "g": 0}])
        self.assertNotIn("remote_url", unbound.config_payload({}, 7, ""))


def indexed_frame(fill=0):
    return bytes((fill,)) * frames.FRAME_PIXELS


class ServerHookTests(unittest.TestCase):
    """AVServer with TV_DB set: the handshake goes through the gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "tv.db"
        accounts = Accounts(self.db)
        self.admin = accounts.create_user("管理员", "admin")
        accounts.set_meta("public_url", "https://example.com")
        accounts.close()
        env = {gate.DB_ENV: str(self.db), gate.DEVICES_ENV: str(root / "devices")}
        with patch.dict(os.environ, env):
            self.server = AVServer(Media(bytes(320000), (indexed_frame(9),) * FRAME_COUNT),
                                   TOKEN, port=0, duration_ms=250)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.assertTrue(self.server.ready.wait(2))
        self.server.tenancy.accounts.add_legacy_device("legacy-xingzhi", self.server.port, "xingzhi-cube-154",
                                                       "小智", self.admin)

    def tearDown(self):
        self.server.stop.set()
        self.thread.join(3)
        self.server.tenancy.accounts.close()
        self.tmp.cleanup()

    def connect(self):
        connection = socket.create_connection(("127.0.0.1", self.server.port), timeout=1)
        connection.setblocking(False)
        self.addCleanup(connection.close)
        return connection

    def test_legacy_token_is_served_with_the_legacy_devices_files(self):
        connection = self.connect()
        send_packet(connection, Packet(Kind.HELLO, 0, 0, 0, json_bytes(hello(token=TOKEN.decode()))))
        config = receive_packet(connection, 2)
        self.assertEqual(config.kind, Kind.CONFIG)
        self.assertNotIn("bound", json_object(config.payload))
        self.assertEqual(self.server.now_playing_path,
                         Path(self.tmp.name) / "devices" / "legacy-xingzhi" / "now-playing.json")

    def test_unknown_device_gets_the_bind_picture_until_bound(self):
        connection = self.connect()
        with patch.object(unbound, "CHECK_S", 0.2):
            send_packet(connection, Packet(Kind.HELLO, 0, 0, 0, json_bytes(
                hello(device_id=DEVICE, device_secret=SECRET_A, model="ai-passport", fw="test"))))
            config = receive_packet(connection, 2)
            payload = json_object(config.payload)
            self.assertEqual((payload["bound"], payload["channel"]), (0, "bind"))
            self.assertTrue(payload["remote_url"].startswith("HTTPS://EXAMPLE.COM/TV/B/"))
            self.assertEqual(receive_packet(connection, 2, config.session).kind, Kind.PALETTE)
            kinds = set()
            for _ in range(100):
                kinds.add(receive_packet(connection, 2, config.session).kind)
                if Kind.JPEG in kinds:
                    break
            self.assertEqual(kinds, {Kind.PCM, Kind.JPEG}, "silence and the picture, nothing else")
            claim = payload["remote_url"].rsplit("/", 1)[1]
            accounts = Accounts(self.db)
            accounts.bind(claim, self.admin)
            accounts.close()
            deadline = time.monotonic() + 3
            ended = False
            while time.monotonic() < deadline and not ended:
                try:
                    receive_packet(connection, 1, config.session)
                except (EOFError, OSError, ProtocolError, TimeoutError):
                    ended = True
            self.assertTrue(ended, "the session ends once bound, so the device reconnects as itself")

    def test_wrong_secret_is_refused_before_config(self):
        accounts = Accounts(self.db)
        accounts.register_device(DEVICE, "ai-passport", SECRET_A)
        accounts.close()
        connection = self.connect()
        send_packet(connection, Packet(Kind.HELLO, 0, 0, 0, json_bytes(
            hello(device_id=DEVICE, device_secret=SECRET_B))))
        with self.assertRaises((EOFError, OSError, ProtocolError, TimeoutError)):
            receive_packet(connection, 2)


if __name__ == "__main__":
    unittest.main()
