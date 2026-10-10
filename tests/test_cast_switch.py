"""Turning relay events into SWITCH decisions for the session in progress."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.cast_switch import SwitchWatcher
from server.relay import events


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "relay-events.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_event_seen_at_start_is_not_replayed(self):
        events.write_event(self.path, "cast")
        watcher = SwitchWatcher(self.path)
        self.assertIsNone(watcher.poll("ch001", 10.0))

    def test_cast_then_return(self):
        watcher = SwitchWatcher(self.path)
        self.assertIsNone(watcher.poll("ch001", 1.0))
        events.write_event(self.path, "cast")
        self.assertEqual(watcher.poll("ch001", 2.0), ("switch", "cast"))
        self.assertIsNone(watcher.poll("cast", 3.0))
        events.write_event(self.path, "return")
        self.assertEqual(watcher.poll("cast", 4.0), ("switch", "ch001"))

    def test_polls_at_most_once_a_second(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("ch001", 1.0)
        events.write_event(self.path, "cast")
        self.assertIsNone(watcher.poll("ch001", 1.5))
        self.assertEqual(watcher.poll("ch001", 2.0), ("switch", "cast"))

    def test_new_cast_while_on_cast_restarts(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("ch001", 1.0)
        events.write_event(self.path, "cast")
        watcher.poll("ch001", 2.0)
        events.write_event(self.path, "cast")
        self.assertEqual(watcher.poll("cast", 3.0), ("restart", None))
        events.write_event(self.path, "return")
        self.assertEqual(watcher.poll("cast", 4.0), ("switch", "ch001"))

    def test_return_ignored_after_viewer_left_cast(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("ch001", 1.0)
        events.write_event(self.path, "cast")
        watcher.poll("ch001", 2.0)
        events.write_event(self.path, "return")
        self.assertIsNone(watcher.poll("ch005", 3.0))

    def test_return_without_previous_is_ignored(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("cast", 1.0)
        events.write_event(self.path, "return")
        self.assertIsNone(watcher.poll("cast", 2.0))

    def test_no_file_configured(self):
        self.assertIsNone(SwitchWatcher(None).poll("ch001", 5.0))


class SessionTests(unittest.TestCase):
    """A live session over a real socket gets SWITCH only if its HELLO asked for it."""

    def setUp(self):
        import os
        from server import live
        self.live = live
        self.saved = (dict(live._LOADED), dict(live.CHANNELS), dict(live.CHANNEL_LABELS),
                      dict(live.CHANNEL_AGENTS), live.DEFAULT_CHANNEL)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        main = root / "channels.txt"
        relay = root / "relay-channels.txt"
        main.write_text("ch000 | One | https://example.com/1.m3u8\n", encoding="utf-8")
        relay.write_text("cast | 我的投屏 | http://127.0.0.1:8099/ch/cast\n", encoding="utf-8")
        live.load_channels(main, relay)
        self.events = root / "relay-events.json"
        self.old_env = os.environ.get("TV_RELAY_EVENTS_FILE")
        os.environ["TV_RELAY_EVENTS_FILE"] = str(self.events)

    def tearDown(self):
        import os
        live = self.live
        loaded, channels, labels, agents, default = self.saved
        live._LOADED.clear(); live._LOADED.update(loaded)
        for table, saved in ((live.CHANNELS, channels), (live.CHANNEL_LABELS, labels),
                             (live.CHANNEL_AGENTS, agents)):
            table.clear(); table.update(saved)
        live.DEFAULT_CHANNEL = default
        if self.old_env is None:
            os.environ.pop("TV_RELAY_EVENTS_FILE", None)
        else:
            os.environ["TV_RELAY_EVENTS_FILE"] = self.old_env
        self.tmp.cleanup()

    def run_session(self, hello_extra):
        import json
        import socket
        import threading
        import time
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_live_transcode import StubFactory
        from server.protocol import Kind, Packet, json_bytes, receive_packet, send_packet
        from server.tv_server import AVServer

        token = b"t" * 32
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=lambda _m: None)
        server.live_enabled = True
        server.channel_factory = StubFactory()
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        hello = {"version": 1, "token": token.decode(), "channel": "ch000"}
        hello.update(hello_extra)
        switches, session, written = [], None, False
        start = time.monotonic()
        try:
            send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(hello)))
            while time.monotonic() - start < 3.0:
                if not written and time.monotonic() - start > 0.5:
                    events.write_event(self.events, "cast")
                    written = True
                try:
                    packet = receive_packet(client, timeout=0.5, expected_session=session)
                except TimeoutError:
                    continue
                session = packet.session
                if packet.kind == Kind.SWITCH:
                    switches.append(json.loads(packet.payload))
        except EOFError:
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        return switches

    def test_switch_sent_to_a_device_that_asked(self):
        self.assertEqual(self.run_session({"switch": 1}), [{"channel": "cast"}])

    def test_no_switch_for_older_firmware(self):
        self.assertEqual(self.run_session({}), [])


if __name__ == "__main__":
    unittest.main()
