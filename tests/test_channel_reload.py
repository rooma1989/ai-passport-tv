"""The TV server picks up a changed channel table without a restart.

The relay rewrites its table when a creator is added; the TV server re-reads
both files at the next session. A broken file must never empty the table the
device is using.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import live

MAIN = "ch000 | One | https://example.com/1.m3u8\nch001 | Two | https://example.com/2.m3u8\n"
RELAY = "up01 | 老番茄 | http://127.0.0.1:8099/ch/up01\n"


class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(live._LOADED), dict(live.CHANNELS), dict(live.CHANNEL_LABELS),
                      dict(live.CHANNEL_AGENTS), live.DEFAULT_CHANNEL)
        self.tmp = tempfile.TemporaryDirectory()
        self.main = Path(self.tmp.name) / "channels.txt"
        self.relay = Path(self.tmp.name) / "relay-channels.txt"
        self.main.write_text(MAIN, encoding="utf-8")

    def tearDown(self):
        loaded, channels, labels, agents, default = self.saved
        live._LOADED.clear(); live._LOADED.update(loaded)
        for table, saved in ((live.CHANNELS, channels), (live.CHANNEL_LABELS, labels),
                             (live.CHANNEL_AGENTS, agents)):
            table.clear(); table.update(saved)
        live.DEFAULT_CHANNEL = default
        self.tmp.cleanup()

    def bump(self, path, text):
        """Write and move the mtime on, as a later edit would."""
        path.write_text(text, encoding="utf-8")
        stamp = path.stat().st_mtime_ns + 10**9
        os.utime(path, ns=(stamp, stamp))

    def reload(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            changed = live.reload_channels_if_changed()
        return changed, out.getvalue()

    def test_relay_first(self):
        self.relay.write_text(RELAY, encoding="utf-8")
        live.load_channels(self.main, self.relay)
        self.assertEqual(list(live.CHANNELS), ["up01", "ch000", "ch001"])
        self.assertEqual(live.CHANNEL_LABELS["up01"], "老番茄")

    def test_missing_relay_file_is_fine(self):
        live.load_channels(self.main, self.relay)
        self.assertEqual(list(live.CHANNELS), ["ch000", "ch001"])

    def test_reload_on_change_only(self):
        live.load_channels(self.main, self.relay)
        self.assertEqual(self.reload(), (False, ""))
        self.bump(self.relay, RELAY)
        self.assertTrue(self.reload()[0])
        self.assertIn("up01", live.CHANNELS)
        self.assertFalse(self.reload()[0])
        self.bump(self.relay, "# nobody followed\n")
        self.assertTrue(self.reload()[0])
        self.assertNotIn("up01", live.CHANNELS)

    def test_bad_file_keeps_table_and_warns_once(self):
        live.load_channels(self.main, self.relay)
        self.bump(self.relay, "broken line without fields\n")
        changed, out = self.reload()
        self.assertFalse(changed)
        self.assertIn("警告", out)
        self.assertEqual(list(live.CHANNELS), ["ch000", "ch001"])
        self.assertEqual(self.reload(), (False, ""))

    def test_duplicate_across_files_keeps_table(self):
        live.load_channels(self.main, self.relay)
        self.bump(self.relay, "ch000 | Again | http://127.0.0.1:8099/ch/up01\n")
        self.assertFalse(self.reload()[0])
        self.assertEqual(live.CHANNEL_LABELS["ch000"], "One")

    def test_main_file_edit_also_reloads(self):
        live.load_channels(self.main, self.relay)
        self.bump(self.main, MAIN + "ch002 | Three | https://example.com/3.m3u8\n")
        self.assertTrue(self.reload()[0])
        self.assertIn("ch002", live.CHANNELS)


    def test_session_falls_back_when_last_channel_was_removed(self):
        """A removed creator must not crash the accept loop with a KeyError."""
        import json
        import socket
        import threading
        from server.protocol import Kind, Packet, json_bytes, receive_packet, send_packet
        from server.tv_server import AVServer

        class NoSource:
            def build_palette(self, timeout=None):
                raise live.LiveError("no source in this test")

            def failure(self):
                return None

            def close(self):
                pass

        live.DEFAULT_CHANNEL = "ch001"
        self.relay.write_text(RELAY, encoding="utf-8")
        live.load_channels(self.main, self.relay)
        token = b"t" * 32
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=lambda _m: None)
        server.live_enabled = True
        server.channel_name = "up01"
        server.channel_factory = lambda url, ffmpeg="ffmpeg", user_agent="": NoSource()
        self.bump(self.relay, "# nobody followed\n")
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                send_packet(client, Packet(Kind.HELLO, 0, 0, 0,
                                           json_bytes({"version": 1, "token": token.decode()})))
                config = receive_packet(client, timeout=3)
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        self.assertEqual(config.kind, Kind.CONFIG)
        payload = json.loads(config.payload)
        self.assertEqual(payload["channel"], "ch001")
        self.assertEqual([c["id"] for c in payload["channel_list"]], ["ch000", "ch001"])
        self.assertTrue(thread.is_alive() is False)

if __name__ == "__main__":
    unittest.main()
