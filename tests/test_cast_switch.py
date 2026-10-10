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
        self.assertEqual(watcher.take_controls(), [("pause", ""), ("brightness", 2), ("resume", None)])

    def test_seek_and_next_are_controls(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("up01", 1.0)
        events.write_event(self.path, "seek", value=-30)
        events.write_event(self.path, "next")
        self.assertIsNone(watcher.poll("up01", 2.0))
        self.assertEqual(watcher.take_controls(), [("seek", -30), ("next", None)])

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

    def test_hints_only_for_firmware_that_draws_them(self):
        def script(client, session, elapsed):
            if elapsed > 0.5 and not getattr(self, "wrote_hint", False):
                self.wrote_hint = True
                events.write_event(self.events, "seek", value=30)
                events.write_event(self.events, "next")
        payloads = self.session({"hint": 1}, script)
        self.assertIn({"seek": 30}, payloads)
        self.assertIn({"next": 1}, payloads)
        self.wrote_hint = False
        self.assertEqual(self.session({}, script), [])

    def test_pet_only_for_firmware_that_reports_one(self):
        def script(client, session, elapsed):
            if elapsed > 0.5 and not getattr(self, "wrote_pet", False):
                self.wrote_pet = True
                events.write_event(self.events, "pet", value=4)
        payloads = self.session({"volume": 70, "brightness": 2, "brightness_steps": 5, "pet": 0}, script)
        self.assertIn({"pet": 4}, payloads)
        self.wrote_pet = False
        payloads = self.session({"volume": 70, "brightness": 2, "brightness_steps": 5}, script)
        self.assertNotIn({"pet": 4}, payloads)

    def test_no_controls_for_firmware_without_them(self):
        def script(client, session, elapsed):
            if elapsed > 0.5 and not getattr(self, "wrote", False):
                self.wrote = True
                events.write_event(self.events, "volume", value=90)
        self.assertEqual(self.session({}, script), [])


class DevicePauseTests(unittest.TestCase):
    """Firmware that draws its own pause band gets messages, not frames."""

    setUp = SessionTests.setUp
    tearDown = SessionTests.tearDown

    def test_pause_band_messages(self):
        import json
        import socket
        import threading
        import time
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_live_transcode import StubFactory
        from server.protocol import Kind, Packet, json_bytes, receive_packet, send_packet
        from server.tv_server import AVServer

        from server import now_playing
        token = b"t" * 32
        server = AVServer(None, token, "127.0.0.1", 0, 60000, logger=lambda _m: None)
        server.live_enabled = True
        server.channel_factory = StubFactory()
        self.refreshes_while_paused = []
        writes = now_playing.write
        old_refresh = now_playing.REFRESH_S
        now_playing.REFRESH_S = 0.3

        def record(*args, **kwargs):
            if phase == "paused":
                self.refreshes_while_paused.append(args[1:3])
            return writes(*args, **kwargs)

        now_playing.write = record
        self.addCleanup(setattr, now_playing, "write", writes)
        self.addCleanup(setattr, now_playing, "REFRESH_S", old_refresh)
        server.now_playing_path = Path(self.tmp.name) / "now-playing.json"
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        switches, frames_while_paused, session, phase = [], 0, None, "playing"
        start = time.monotonic()
        try:
            send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(
                {"version": 1, "token": token.decode(), "channel": "ch000", "switch": 1, "pause": 1})))
            while time.monotonic() - start < 5.0:
                elapsed = time.monotonic() - start
                if phase == "playing" and elapsed > 0.6:
                    events.write_event(self.events, "pause", text="小Lin说 · 日元")
                    phase = "pausing"
                elif phase == "pausing" and elapsed > 1.8:
                    phase = "paused"
                elif phase == "paused" and elapsed > 3.0:
                    events.write_event(self.events, "resume")
                    phase = "resumed"
                try:
                    packet = receive_packet(client, timeout=0.2, expected_session=session)
                except TimeoutError:
                    continue
                session = packet.session
                if packet.kind == Kind.SWITCH:
                    switches.append(json.loads(packet.payload))
                if phase == "paused" and packet.kind == Kind.JPEG:
                    frames_while_paused += 1
        except EOFError:
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        self.assertEqual(switches[:2], [{"paused": 1, "title": "小Lin说 · 日元"}, {"paused": 0}])
        self.assertEqual(frames_while_paused, 0, "the device keeps the frozen frame")
        self.assertGreaterEqual(len(self.refreshes_while_paused), 2,
                                "now-playing stays fresh during a pause, or the phone calls the device offline")


class DeviceBootLogTests(unittest.TestCase):
    """The device's uptime and last reset reason tell a reboot from a network hiccup."""

    def test_reason_names(self):
        from server.tv_server import reset_reason_name
        self.assertEqual(reset_reason_name(4), "PANIC")
        self.assertEqual(reset_reason_name(9), "BROWNOUT")
        self.assertEqual(reset_reason_name(99), "99")

    def test_boot_noticed_once_per_boot(self):
        from server.tv_server import AVServer
        lines = []
        server = AVServer(None, b"t" * 32, "127.0.0.1", 0, 60000, logger=lines.append)
        server.note_device_boot({"uptime_s": 8, "reset_reason": 4})
        server.note_device_boot({"uptime_s": 300, "reset_reason": 4})
        server.note_device_boot({"uptime_s": 6, "reset_reason": 9})
        server.note_device_boot({})
        boots = [l for l in lines if "device boot" in l]
        self.assertEqual(boots, ["device boot reason=PANIC(4) uptime=8s",
                                 "device boot reason=BROWNOUT(9) uptime=6s"])

    def test_server_restart_does_not_fake_a_boot(self):
        from server.tv_server import AVServer
        lines = []
        server = AVServer(None, b"t" * 32, "127.0.0.1", 0, 60000, logger=lines.append)
        server.note_device_boot({"uptime_s": 5000, "reset_reason": 1})
        self.assertEqual([l for l in lines if "device boot" in l], [])


if __name__ == "__main__":
    unittest.main()
