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

    def test_switch_from_the_phone(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("ch001", 1.0)
        events.write_event(self.path, "switch", channel="ch013")
        self.assertEqual(watcher.poll("ch001", 2.0), ("switch", "ch013"))
        events.write_event(self.path, "switch", channel="ch013")
        self.assertIsNone(watcher.poll("ch013", 3.0))

    def test_switch_is_not_lost_behind_later_events(self):
        """A slider writes events quickly; the switch before them still happens."""
        watcher = SwitchWatcher(self.path)
        watcher.poll("ch001", 1.0)
        events.write_event(self.path, "switch", channel="ch013")
        events.write_event(self.path, "volume", value=60)
        events.write_event(self.path, "volume", value=70)
        self.assertEqual(watcher.poll("ch001", 2.0), ("switch", "ch013"))
        self.assertEqual(watcher.take_controls(), [("volume", 70)])
        self.assertEqual(watcher.take_controls(), [])

    def test_controls_in_order(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("up01", 1.0)
        events.write_event(self.path, "pause")
        events.write_event(self.path, "brightness", value=2)
        events.write_event(self.path, "resume")
        self.assertIsNone(watcher.poll("up01", 2.0))
        self.assertEqual(watcher.take_controls(), [("pause", None), ("brightness", 2), ("resume", None)])

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

    def test_switch_asked_between_sessions_is_applied_at_the_next(self):
        """The phone asked while the device was reconnecting: CONFIG names the new channel."""
        import json
        import socket
        import threading
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_live_transcode import StubFactory
        from server.protocol import Kind, Packet, json_bytes, receive_packet, send_packet
        from server.tv_server import AVServer

        token = b"t" * 32
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=lambda _m: None)
        server.live_enabled = True
        server.channel_factory = StubFactory()
        events.write_event(self.events, "switch", channel="cast")
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        try:
            send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(
                {"version": 1, "token": token.decode(), "channel": "ch000", "switch": 1})))
            config = receive_packet(client, timeout=3)
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        self.assertEqual(json.loads(config.payload)["channel"], "cast")


class PauseTests(unittest.TestCase):
    """Pause closes the source and holds the device; resume starts a new one."""

    setUp = SessionTests.setUp
    tearDown = SessionTests.tearDown

    def test_pause_then_resume(self):
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
        factory = StubFactory()
        server.channel_factory = factory
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        kinds_after_pause, session, phase = [], None, "playing"
        start = time.monotonic()
        try:
            send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(
                {"version": 1, "token": token.decode(), "channel": "ch000", "switch": 1})))
            while time.monotonic() - start < 6.0:
                elapsed = time.monotonic() - start
                if phase == "playing" and elapsed > 0.6:
                    events.write_event(self.events, "pause")
                    phase = "pausing"
                elif phase == "pausing" and elapsed > 2.0:
                    self.assertEqual(len(factory.created), 1, "no new source while paused")
                    phase = "paused"
                elif phase == "paused" and elapsed > 3.5:
                    events.write_event(self.events, "resume")
                    phase = "resumed"
                try:
                    packet = receive_packet(client, timeout=0.2, expected_session=session)
                except TimeoutError:
                    continue
                session = packet.session
                if phase == "paused":
                    kinds_after_pause.append(packet.kind)
        except EOFError:
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        self.assertIn(Kind.PCM, kinds_after_pause, "silence keeps the device's audio clock going")
        self.assertIn(Kind.JPEG, kinds_after_pause, "the paused frame is re-sent")
        self.assertGreaterEqual(len(factory.created), 2, "resume started a new source")


class DeviceControlTests(unittest.TestCase):
    """Volume and brightness: the device reports them, the phone sets them."""

    setUp = SessionTests.setUp
    tearDown = SessionTests.tearDown

    def session(self, hello_extra, script):
        """Run a session; `script(client, session, elapsed)` may send packets. Returns SWITCH payloads."""
        import json
        import socket
        import threading
        import time
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_live_transcode import StubFactory
        from server.protocol import Kind, Packet, json_bytes, receive_packet, send_packet
        from server.tv_server import AVServer

        token = b"t" * 32
        self.playing = Path(self.tmp.name) / "now-playing.json"
        import os
        os.environ["TV_NOW_PLAYING_FILE"] = str(self.playing)
        try:
            server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=lambda _m: None)
        finally:
            os.environ.pop("TV_NOW_PLAYING_FILE", None)
        server.live_enabled = True
        server.channel_factory = StubFactory()
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        hello = {"version": 1, "token": token.decode(), "channel": "ch000", "switch": 1}
        hello.update(hello_extra)
        payloads, session = [], None
        start = time.monotonic()
        try:
            send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(hello)))
            while time.monotonic() - start < 3.0:
                if session:
                    script(client, session, time.monotonic() - start)
                try:
                    packet = receive_packet(client, timeout=0.2, expected_session=session)
                except TimeoutError:
                    continue
                session = packet.session
                if packet.kind == Kind.SWITCH:
                    payloads.append(json.loads(packet.payload))
        except EOFError:
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        return payloads

    def test_status_is_recorded_and_controls_are_sent(self):
        from server import now_playing
        from server.protocol import Kind, Packet, json_bytes, send_packet
        sent = {"status": False, "events": False}

        def script(client, session, elapsed):
            if elapsed > 0.5 and not sent["status"]:
                sent["status"] = True
                send_packet(client, Packet(Kind.STATUS, session, 0, 0,
                                           json_bytes({"volume": 40, "brightness": 1})))
            if elapsed > 1.0 and not sent["events"]:
                sent["events"] = True
                self.seen_after_status = now_playing.read(self.playing)["device"]
                events.write_event(self.events, "volume", value=90)
                events.write_event(self.events, "brightness", value=3)

        payloads = self.session({"volume": 70, "brightness": 2, "brightness_steps": 5}, script)
        self.assertEqual(self.seen_after_status, {"volume": 40, "brightness": 1, "steps": 5})
        self.assertIn({"volume": 90}, payloads)
        self.assertIn({"brightness": 3}, payloads)

    def test_no_controls_for_firmware_without_them(self):
        def script(client, session, elapsed):
            if elapsed > 0.5 and not getattr(self, "wrote", False):
                self.wrote = True
                events.write_event(self.events, "volume", value=90)
        self.assertEqual(self.session({}, script), [])


if __name__ == "__main__":
    unittest.main()
