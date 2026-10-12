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

    def test_loading_is_a_control_and_takes_the_next_hint_with_it(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("sr01", 1.0)
        events.write_event(self.path, "volume", value=40)
        events.write_event(self.path, "loading", channel="sr01", value=2, text="第2集", total=3)
        events.write_event(self.path, "next")
        self.assertIsNone(watcher.poll("sr01", 2.0))
        self.assertEqual(watcher.take_loading(), ("sr01", {"title": "第2集", "episode": 2, "total": 3}))
        self.assertEqual(watcher.take_controls(), [("volume", 40)], "the next box is not needed")
        self.assertIsNone(watcher.take_loading())
        # Firmware without the loading screen: the hint is still there for it.
        events.write_event(self.path, "loading", channel="up01", value=0, text="Some video")
        events.write_event(self.path, "next")
        watcher.poll("up01", 3.0)
        self.assertEqual(watcher.take_controls(),
                         [("loading", ("up01", {"title": "Some video"})), ("next", None)])

    def test_audio_is_a_control_latest_wins(self):
        watcher = SwitchWatcher(self.path)
        watcher.poll("up01", 1.0)
        events.write_event(self.path, "audio", value=1)
        events.write_event(self.path, "audio", value=0)
        self.assertIsNone(watcher.poll("up01", 2.0))
        self.assertEqual(watcher.take_controls(), [("audio", 0)])

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
        self.kinds = []
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
                self.kinds.append(packet.kind)
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

    def test_audio_only_session_has_no_video(self):
        from server.protocol import Kind
        hello = {"volume": 70, "brightness": 2, "brightness_steps": 5}
        self.session(hello, lambda *_: None)
        self.assertIn(Kind.JPEG, self.kinds, "the stub sends pictures normally")
        self.session(dict(hello, audio_only=1), lambda *_: None)
        self.assertNotIn(Kind.JPEG, self.kinds, "no pictures for an audio-only device")
        self.assertIn(Kind.PCM, self.kinds, "sound as usual")

    def test_audio_toggle_only_for_firmware_that_reports_it(self):
        def script(client, session, elapsed):
            if elapsed > 0.5 and not getattr(self, "wrote_audio", False):
                self.wrote_audio = True
                events.write_event(self.events, "audio", value=1)
        hello = {"volume": 70, "brightness": 2, "brightness_steps": 5}
        self.assertIn({"audio_only": 1}, self.session(dict(hello, audio_only=0), script))
        self.wrote_audio = False
        self.assertNotIn({"audio_only": 1}, self.session(hello, script))

    def test_wifi_and_rssi_are_logged(self):
        from server.tv_server import AVServer
        lines = []
        server = AVServer.__new__(AVServer)
        server.logger = lines.append
        server.note_device_wifi({"ssid": "PHP2.4G", "rssi": -78, "ch": 6, "tx": 11.0})
        server.note_device_wifi("nonsense")
        self.assertEqual(lines, ["device wifi ssid=PHP2.4G rssi=-78 ch=6 tx=11.0"])

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

    def test_boot_noticed_after_a_long_session(self):
        """A reboot whose new uptime exceeds the last HELLO's is still a reboot."""
        from server.tv_server import AVServer
        lines = []
        server = AVServer(None, b"t" * 32, "127.0.0.1", 0, 60000, logger=lines.append)
        server.note_device_boot({"uptime_s": 4, "reset_reason": 11}, now=1000.0)
        # Ten minutes later the device rebooted and came back 26 s into its new life.
        server.note_device_boot({"uptime_s": 26, "reset_reason": 11}, now=1600.0)
        # And a plain reconnect a minute after that is not a boot.
        server.note_device_boot({"uptime_s": 87, "reset_reason": 11}, now=1661.0)
        boots = [l for l in lines if "device boot" in l]
        self.assertEqual(boots, ["device boot reason=USB(11) uptime=4s",
                                 "device boot reason=USB(11) uptime=26s"])

    def test_server_restart_does_not_fake_a_boot(self):
        from server.tv_server import AVServer
        lines = []
        server = AVServer(None, b"t" * 32, "127.0.0.1", 0, 60000, logger=lines.append)
        server.note_device_boot({"uptime_s": 5000, "reset_reason": 1})
        self.assertEqual([l for l in lines if "device boot" in l], [])


if __name__ == "__main__":
    unittest.main()


class EpisodeInfoTests(unittest.TestCase):
    """Firmware that says "info": 1 is told which episode plays and which one loads."""

    setUp = SessionTests.setUp
    tearDown = SessionTests.tearDown

    def relay(self, answers):
        """A stand-in for the relay's /info/<channel>, answering `answers` in turn (the last repeats)."""
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        asked = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append(self.path)
                body = json.dumps(answers[min(len(asked), len(answers)) - 1]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        main = Path(self.tmp.name) / "channels.txt"
        relay_table = Path(self.tmp.name) / "relay-channels.txt"
        relay_table.write_text(f"sr01 | 犬夜叉 | http://127.0.0.1:{port}/ch/sr01\n", encoding="utf-8")
        self.live.load_channels(main, relay_table)
        return asked

    def session(self, hello_extra, script, factory=None, seconds=3.0):
        """Run a session on sr01; returns (CONFIG, SWITCH payloads)."""
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
        server.channel_factory = factory or StubFactory()
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        self.assertTrue(server.ready.wait(5))
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.setblocking(False)
        hello = {"version": 1, "token": token.decode(), "channel": "sr01", "switch": 1}
        hello.update(hello_extra)
        config, payloads, session = None, [], None
        start = time.monotonic()
        try:
            send_packet(client, Packet(Kind.HELLO, 0, 0, 0, json_bytes(hello)))
            while time.monotonic() - start < seconds:
                if session:
                    script(time.monotonic() - start)
                try:
                    packet = receive_packet(client, timeout=0.2, expected_session=session)
                except TimeoutError:
                    continue
                session = packet.session
                if packet.kind == Kind.CONFIG:
                    config = json.loads(packet.payload)
                if packet.kind == Kind.SWITCH:
                    payloads.append(json.loads(packet.payload))
        except EOFError:
            pass
        finally:
            server.stop.set()
            client.close()
            thread.join(timeout=3)
        return config, payloads

    def test_config_says_what_plays_only_to_firmware_that_draws_it(self):
        asked = self.relay([{"channel": "sr01", "title": "第3集 " + "长" * 40, "episode": 3, "total": 26}])
        config, _ = self.session({"info": 1, "hint": 1}, lambda _t: None, seconds=0.5)
        playing = config["playing"]
        self.assertEqual((playing["episode"], playing["total"]), (3, 26))
        self.assertLessEqual(len(playing["title"].encode()), 60, "fits the device's buffer")
        self.assertTrue(playing["title"].startswith("第3集 长"))
        self.assertEqual(asked, ["/info/sr01"])
        config, _ = self.session({"hint": 1}, lambda _t: None, seconds=0.5)
        self.assertNotIn("playing", config, "older firmware gets nothing new")
        self.assertEqual(len(asked), 1, "and the relay is not even asked")

    def test_relay_down_means_no_playing(self):
        self.relay([{}])
        self.live.CHANNELS["sr01"] = "http://127.0.0.1:9/ch/sr01"
        config, _ = self.session({"info": 1}, lambda _t: None, seconds=0.5)
        self.assertNotIn("playing", config)

    def test_loading_replaces_the_next_box(self):
        self.relay([{"channel": "sr01", "title": "第1集", "episode": 1, "total": 3}])
        wrote = []

        def script(elapsed):
            if elapsed > 0.5 and not wrote:
                wrote.append(1)
                events.write_event(self.events, "loading", channel="sr01", value=2, text="第2集", total=3)
                events.write_event(self.events, "next")

        _, payloads = self.session({"info": 1, "hint": 1}, script)
        self.assertIn({"loading": {"title": "第2集", "episode": 2, "total": 3}}, payloads)
        self.assertNotIn({"next": 1}, payloads)
        wrote.clear()
        _, payloads = self.session({"hint": 1}, script)
        self.assertIn({"next": 1}, payloads, "firmware without it keeps the old box")
        self.assertFalse([p for p in payloads if "loading" in p])

    def test_loading_for_another_channel_is_not_sent(self):
        self.relay([{"channel": "sr01", "title": "第1集", "episode": 1, "total": 3}])
        wrote = []

        def script(elapsed):
            if elapsed > 0.5 and not wrote:
                wrote.append(1)
                events.write_event(self.events, "loading", channel="sr02", value=2, text="x", total=3)

        _, payloads = self.session({"info": 1}, script, seconds=1.5)
        self.assertFalse([p for p in payloads if "loading" in p])

    def test_source_restart_shows_the_new_episode_then_clears(self):
        """An episode ends: the relay opens the next; the device is told before, and cleared after."""
        import time
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_live_transcode import StubChannel
        self.relay([{"channel": "sr01", "title": "第1集", "episode": 1, "total": 3},
                    {"channel": "sr01", "title": "第2集", "episode": 2, "total": 3}])

        class Ending(StubChannel):
            def __init__(self):
                super().__init__(audio_chunks=20, video_frames=6)
                self.failed = True     # the source has ended; what is queued still plays

        class Slow(StubChannel):
            def start(self):
                time.sleep(0.4)        # a real source takes seconds to open

        made = []

        def factory(url, ffmpeg="ffmpeg", user_agent=""):
            made.append(url)
            return Ending() if len(made) == 1 else Slow()

        config, payloads = self.session({"info": 1}, lambda _t: None, factory=factory, seconds=4.0)
        self.assertEqual(config["playing"]["episode"], 1)
        self.assertGreaterEqual(len(made), 2, "the source was reopened")
        loading = [p["loading"] for p in payloads if "loading" in p]
        self.assertEqual(loading, [{"title": "第2集", "episode": 2, "total": 3}, 0],
                         "the next episode while it opens, then cleared")


class ProgressTests(unittest.TestCase):
    """Firmware that says "progress": 1 is given anchors for its progress bar (server/progress.py)."""

    setUp = SessionTests.setUp
    tearDown = SessionTests.tearDown
    relay = EpisodeInfoTests.relay
    session = EpisodeInfoTests.session

    ANSWER = {"channel": "sr01", "title": "第1集", "episode": 1, "total": 3, "duration": 600, "offset": 12}

    def test_anchor_only_to_firmware_that_draws_it(self):
        asked = self.relay([self.ANSWER])
        _, payloads = self.session({"progress": 1}, lambda _t: None, seconds=1.0)
        self.assertEqual([p for p in payloads if "progress" in p],
                         [{"progress": {"dur": 600000, "pos": 12000, "at": 0}}],
                         "once, at the session's first sound")
        self.assertEqual(asked, ["/info/sr01"])
        _, payloads = self.session({"info": 1}, lambda _t: None, seconds=1.0)
        self.assertFalse([p for p in payloads if "progress" in p], "older firmware gets nothing new")

    def test_live_channel_gets_nothing(self):
        self.relay([self.ANSWER])
        self.live.CHANNELS["sr01"] = "https://example.com/live.m3u8"
        _, payloads = self.session({"progress": 1}, lambda _t: None, seconds=1.0)
        self.assertFalse([p for p in payloads if "progress" in p])

    def test_source_restart_stops_then_anchors_the_new_source(self):
        import time
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_live_transcode import StubChannel
        self.relay([self.ANSWER, dict(self.ANSWER, offset=0)])

        class Ending(StubChannel):
            def __init__(self):
                super().__init__(audio_chunks=20, video_frames=6)
                self.failed = True

        class Slow(StubChannel):
            def start(self):
                time.sleep(0.4)

        made = []

        def factory(url, ffmpeg="ffmpeg", user_agent=""):
            made.append(url)
            return Ending() if len(made) == 1 else Slow()

        _, payloads = self.session({"progress": 1}, lambda _t: None, factory=factory, seconds=4.0)
        anchors = [p["progress"] for p in payloads if "progress" in p]
        self.assertGreaterEqual(len(anchors), 3, anchors)
        self.assertEqual(anchors[0], {"dur": 600000, "pos": 12000, "at": 0})
        self.assertEqual(anchors[1], {"dur": 600000, "pos": 12000, "at": 0, "stop": 800},
                         "stopped after the 20 blocks of sound")
        new = anchors[2]
        self.assertEqual((new["dur"], new["pos"]), (600000, 0))
        self.assertGreater(new["at"], 800, "the new source's first sound comes after the silence")
        self.assertEqual(new["at"] % 40, 0)
        self.assertNotIn("stop", new)

    def test_pause_stops_the_bar_before_the_band(self):
        self.relay([self.ANSWER])
        wrote = []

        def script(elapsed):
            if elapsed > 0.5 and not wrote:
                wrote.append(1)
                events.write_event(self.events, "pause", text="第1集")

        _, payloads = self.session({"progress": 1, "pause": 1}, script, seconds=1.5)
        keys = [next(iter(p)) for p in payloads if "progress" in p or "paused" in p]
        self.assertEqual(keys, ["progress", "progress", "paused"], payloads)
        self.assertIn("stop", payloads[[next(iter(p)) for p in payloads].index("paused") - 1]["progress"])
