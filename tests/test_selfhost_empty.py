"""An empty channel table in self-host mode (server/live.py, server/no_channels.py).

Phase S1c of docs/superpowers/specs/2026-10-15-self-hosting-design.md, section
8.1: the image ships no channels, so a new household starts with nothing. With
TV_NO_BUILTIN_CHANNELS=1 the media server starts on an empty table, never falls
back to the built-in channels or a channels.txt next to the code, and a bound
device with nothing to play gets a still "no channels yet" frame instead of
"reconnecting". Without the variable (the cloud host) an empty table is still an
error, as before.

The media-server cases run in a subprocess: server.live loads its table at
import, and this suite's other tests share that module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import live, no_channels

SESSION_SCRIPT = textwrap.dedent("""
    import json, os, socket, sys, threading, time
    sys.path.insert(0, sys.argv[1])
    root = sys.argv[2]
    from server import no_channels
    from server.protocol import Kind, Packet, json_bytes, json_object, receive_packet, send_packet
    from server.relay import table
    from server.tenancy.accounts import Accounts
    from server.tv_server import AVServer
    no_channels.CHECK_S = 0.2
    accounts = Accounts(os.path.join(root, "tv.db"))
    admin = accounts.create_user("admin", "admin")
    row = accounts.register_device("a1b2c3d4e5f6", "xingzhi-cube-154", "S" * 43)
    accounts.bind(row["claim_code"], admin)
    device_dir = os.path.join(root, "devices", "a1b2c3d4e5f6")
    os.makedirs(device_dir)
    # What the relay writes for a bound device: only the cast slot, here with nothing cast
    # (no relay is running, so asking it what is cast gets no answer).
    with open(os.path.join(device_dir, "relay-channels.txt"), "w") as handle:
        handle.write(table.render([], base=table.STREAM_BASE + "a1b2c3d4e5f6/"))
    server = AVServer(None, "127.0.0.1", 0, logger=lambda m: None)
    threading.Thread(target=server.serve, daemon=True).start()
    server.ready.wait(3)
    connection = socket.create_connection(("127.0.0.1", server.port), timeout=2)
    connection.setblocking(False)
    send_packet(connection, Packet(Kind.HELLO, 0, 0, 0, json_bytes(
        {"version": 1, "device_id": "a1b2c3d4e5f6", "device_secret": "S" * 43, "model": "xingzhi-cube-154"})))
    config = json_object(receive_packet(connection, 2).payload)
    kinds = {}
    for _ in range(80):
        packet = receive_packet(connection, 2, config["session"])
        kinds[packet.kind.name] = kinds.get(packet.kind.name, 0) + 1
        if kinds.get("JPEG"):
            break
    # Someone adds a live channel: the still frame ends so the device reconnects to it.
    with open(os.environ["TV_CHANNELS_FILE"], "w") as handle:
        handle.write("ch000 | Home | http://192.168.1.5/a.m3u8\\n")
    ended, deadline = False, time.monotonic() + 4
    while not ended and time.monotonic() < deadline:
        try:
            receive_packet(connection, 1, config["session"])
        except Exception:
            ended = True
    print(json.dumps({"channels": [c["id"] for c in config["channel_list"]], "channel": config.get("channel"),
                      "kinds": kinds, "ended": ended}))
    server.stop.set()
""")


def _run(code: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, encoding="utf-8",
                          timeout=60, cwd=str(ROOT), env=dict(os.environ, PYTHONIOENCODING="utf-8", **env))


class ParseTests(unittest.TestCase):
    def test_empty_table_is_an_error_unless_allowed(self):
        with self.assertRaises(ValueError):
            live.parse_channels("# nothing\n")
        self.assertEqual(live.parse_channels("# nothing\n", allow_empty=True), ({}, {}, {}))

    def test_nothing_to_play(self):
        cast = {"cast": "http://127.0.0.1:8099/ch/a1b2c3d4e5f6/cast"}
        with patch.dict(live.CHANNELS, cast, clear=True):
            self.assertTrue(no_channels.nothing_to_play(info=lambda url: None))
            self.assertFalse(no_channels.nothing_to_play(info=lambda url: {"title": "x"}), "something is cast")
        with patch.dict(live.CHANNELS, dict(cast, up00="http://127.0.0.1:8099/ch/a1b2c3d4e5f6/up00"), clear=True):
            self.assertFalse(no_channels.nothing_to_play(info=lambda url: None), "a creator channel plays")
        with patch.dict(live.CHANNELS, {}, clear=True):
            self.assertTrue(no_channels.nothing_to_play(info=lambda url: None))


class SelfHostStartTests(unittest.TestCase):
    def test_starts_empty_without_builtin_channels_or_the_repositorys_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            table.write_text("# empty\n", encoding="utf-8")
            code = ("import json; from server import live, tv_server; "
                    "print(json.dumps([sorted(live.CHANNELS), live.DEFAULT_CHANNEL]))")
            done = _run(code, {"TV_NO_BUILTIN_CHANNELS": "1", "TV_CHANNELS_FILE": str(table)})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout.splitlines()[-1]), [[], ""])
            # The table file missing altogether is also an empty table, and the repository's own
            # channels.txt in the working directory is never read.
            table.unlink()
            done = _run(code, {"TV_NO_BUILTIN_CHANNELS": "1", "TV_CHANNELS_FILE": str(table)})
            self.assertEqual(json.loads(done.stdout.splitlines()[-1]), [[], ""], done.stderr)
            done = _run(code, {"TV_NO_BUILTIN_CHANNELS": "1", "TV_CHANNELS_FILE": ""})
            self.assertEqual(json.loads(done.stdout.splitlines()[-1]), [[], ""], done.stderr)

    def test_channel_argument_is_optional_only_in_self_host_mode(self):
        code = ("import sys; from server import tv_server; "
                "p = tv_server.argparse.ArgumentParser(); "
                "sys.argv = ['x', 'live', '--port', '0', '--bind', '127.0.0.1']; "
                "tv_server.AVServer.serve = lambda self: None; sys.exit(tv_server.main(sys.argv[1:]))")
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            table.write_text("", encoding="utf-8")
            done = _run(code, {"TV_NO_BUILTIN_CHANNELS": "1", "TV_CHANNELS_FILE": str(table)})
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn("局域网里的设备", done.stdout, "the supervisor's banner says where to connect")
        cloud = _run(code, {"TV_NO_BUILTIN_CHANNELS": "", "TV_CHANNELS_FILE": str(ROOT / "tests" / "test_channels.txt")})
        self.assertEqual(cloud.returncode, 2, "the cloud units always pass --channel; missing is still an error")

    def test_cloud_host_still_refuses_an_empty_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            table.write_text("# empty\n", encoding="utf-8")
            done = _run("from server import live", {"TV_NO_BUILTIN_CHANNELS": "", "TV_CHANNELS_FILE": str(table)})
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("contains no channels", done.stderr)


class NoChannelsSessionTests(unittest.TestCase):
    def test_bound_device_gets_the_still_frame_until_something_is_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "channels.txt"
            table.write_text("# empty\n", encoding="utf-8")
            env = {"TV_NO_BUILTIN_CHANNELS": "1", "TV_CHANNELS_FILE": str(table),
                   "TV_DB": str(Path(tmp) / "tv.db"), "TV_DEVICES_DIR": str(Path(tmp) / "devices")}
            done = _run(SESSION_SCRIPT, env, str(ROOT), tmp)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            result = json.loads(done.stdout.splitlines()[-1])
        self.assertEqual(result["channels"], ["cast"])
        self.assertEqual(result["channel"], "cast")
        self.assertEqual(set(result["kinds"]), {"PALETTE", "PCM", "JPEG"}, "silence and the still frame")
        self.assertTrue(result["ended"], "adding a channel ends the frame so the device reconnects")


if __name__ == "__main__":
    unittest.main()
