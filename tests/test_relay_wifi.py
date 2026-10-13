"""Managing a device's saved Wi-Fi from the phone and the PC console (multi-user relay).

The whole server path with the device played by test code: the device's HELLO
pins its key and reports its list (server/tenancy/gate.py, wifi.py); the owner's
request is sealed for that device and written as an event (server/relay/wifi.py);
the media server forwards it as SWITCH {"wifi": …} and stores the device's ack
from STATUS (server/tv_server.py). What is checked: only the owner (or an admin
who is a member) gets anywhere, the password is never written down anywhere,
the envelope opens with the device's key and nobody else's, and the page sees
"saved", "failed", "timeout" or "offline".
"""

import json
import socket
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import wifi_crypto  # noqa: E402
from server.cast_switch import SwitchWatcher  # noqa: E402
from server.protocol import Kind, Packet, json_bytes, receive_packet, send_packet  # noqa: E402
from server.relay import events  # noqa: E402
from server.relay import wifi as relay_wifi  # noqa: E402
from server.tenancy import wifi as device_wifi  # noqa: E402
from server.tenancy.gate import Gate  # noqa: E402
from server.tv_server import AVServer  # noqa: E402

import test_relay_tenants as base  # noqa: E402

ALICE_DEVICE, BOB_DEVICE, SECRET = base.ALICE_DEVICE, base.BOB_DEVICE, base.SECRET
PASSWORD = "correct horse battery"


def device_hello(private: bytes, saved=("家", "office"), current="家", device_id=ALICE_DEVICE) -> dict:
    return {"version": 1, "device_id": device_id, "device_secret": SECRET, "model": "xingzhi-cube-154",
            "wifimgr": 1, "wkey": wifi_crypto.b64e(wifi_crypto.public_key(private)),
            "wifi_saved": list(saved), "wifi": {"ssid": current, "rssi": -50}}


class WifiWebTests(unittest.TestCase):
    setUp = base.TenantWebTests.setUp
    tearDown = base.TenantWebTests.tearDown
    fake_start = base.TenantWebTests.fake_start
    fake_pump = base.TenantWebTests.fake_pump
    files = base.TenantWebTests.files
    online = base.TenantWebTests.online
    raw = base.TenantWebTests.raw
    call = base.TenantWebTests.call

    def ready(self, device=ALICE_DEVICE):
        """The relay has its key; the device said hello with its own."""
        self.relay.wifi_private = wifi_crypto.new_private_key()
        self.accounts.set_meta(relay_wifi.PUB_META, wifi_crypto.b64e(wifi_crypto.public_key(self.relay.wifi_private)))
        self.device_private = wifi_crypto.new_private_key()
        self.assertEqual(device_wifi.hello(self.accounts, device, device_hello(self.device_private, device_id=device),
                                           logger=lambda _m: None),
                         "pinned")

    def last_wifi_event(self, device=ALICE_DEVICE):
        found = [e for e in events.read_events(self.files(device).events) if e.action == "wifi"]
        return found[-1] if found else None

    def open_event(self, event) -> dict:
        server_public = wifi_crypto.public_key(self.relay.wifi_private)
        return json.loads(wifi_crypto.open_sealed(self.device_private, server_public, json.loads(event.text)))

    # ---- the list ----

    def test_owner_sees_the_saved_names(self):
        self.ready()
        status, answer = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertEqual(status, 200)
        self.assertTrue(answer["supported"])
        self.assertEqual(answer["networks"], ["家", "office"])
        self.assertEqual(answer["current"], "家")
        self.assertFalse(answer["online"])
        self.assertIsNone(answer["pending"])

    def test_old_firmware_is_not_supported(self):
        status, answer = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertEqual(status, 200)
        self.assertFalse(answer["supported"])
        self.online(ALICE_DEVICE)
        self.relay.wifi_private = wifi_crypto.new_private_key()
        status, answer = self.call("alice", "POST", "/api/wifi",
                                   {"device": ALICE_DEVICE, "op": "add", "ssid": "x", "password": PASSWORD})
        self.assertFalse(answer["ok"])
        self.assertIn("固件", answer["error"])

    def test_only_the_owner(self):
        self.ready(BOB_DEVICE)
        self.online(BOB_DEVICE)
        # Another user, an admin who is not a member, no session: 404 like everything else.
        for who in ("alice", "admin", "nobody"):
            self.assertEqual(self.call(who, "GET", f"/api/wifi?device={BOB_DEVICE}")[0], 404, who)
            self.assertEqual(self.call(who, "POST", "/api/wifi", {"device": BOB_DEVICE, "op": "add",
                                                                  "ssid": "evil", "password": PASSWORD})[0], 404, who)
        # A guest of that very device.
        guest = "tvs=" + self.accounts.new_session(None, "guest", device_id=BOB_DEVICE)
        self.assertEqual(self.call(guest, "GET", f"/api/wifi?device={BOB_DEVICE}")[0], 404)
        self.assertEqual(self.call(guest, "POST", "/api/wifi", {"device": BOB_DEVICE, "op": "remove", "i": 0,
                                                                "ssid": "家"})[0], 404)
        # A family member who is not the owner.
        self.accounts._run("INSERT INTO device_member(device_id, user_id, role) VALUES (?, ?, 'member')",
                           (BOB_DEVICE, self.alice))
        self.assertEqual(self.call("alice", "GET", f"/api/wifi?device={BOB_DEVICE}")[0], 404)
        self.assertIsNone(self.last_wifi_event(BOB_DEVICE))
        # The owner can.
        status, answer = self.call("bob", "GET", f"/api/wifi?device={BOB_DEVICE}")
        self.assertEqual(status, 200)
        self.assertTrue(answer["supported"])

    # ---- a change ----

    def test_add_is_sealed_for_the_device(self):
        self.ready()
        status, answer = self.call("alice", "POST", "/api/wifi",
                                   {"device": ALICE_DEVICE, "op": "add", "ssid": "咖啡馆", "password": PASSWORD})
        self.assertEqual(answer, {"ok": False, "offline": True, "error": "小电视不在线：打开它、连上网以后再改"})
        self.assertIsNone(self.last_wifi_event())

        self.online(ALICE_DEVICE)
        status, answer = self.call("alice", "POST", "/api/wifi",
                                   {"device": ALICE_DEVICE, "op": "add", "ssid": "咖啡馆", "password": PASSWORD})
        self.assertTrue(answer["ok"])
        seq = answer["seq"]
        self.assertEqual(seq, int(self.now * 1000))
        event = self.last_wifi_event()
        self.assertEqual(event.value, int(self.now))
        self.assertEqual(self.open_event(event), {"seq": seq, "op": "add", "ssid": "咖啡馆", "password": PASSWORD})
        # Nobody else's key opens it.
        with self.assertRaises(wifi_crypto.CryptoError):
            wifi_crypto.open_sealed(wifi_crypto.new_private_key(), wifi_crypto.public_key(self.relay.wifi_private),
                                    json.loads(event.text))
        # The password is nowhere on disk: not in the events file, not in the database.
        root = Path(self.tmp.name)
        for path in root.rglob("*"):
            if path.is_file():
                self.assertNotIn(PASSWORD.encode(), path.read_bytes(), str(path))

        # Waiting for the answer: a second change is refused meanwhile.
        status, answer = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertEqual(answer["pending"]["state"], "sent")
        status, again = self.call("alice", "POST", "/api/wifi",
                                  {"device": ALICE_DEVICE, "op": "remove", "i": 1, "ssid": "office"})
        self.assertFalse(again["ok"])

        # The device answers in STATUS.
        self.assertTrue(device_wifi.ack(self.accounts, ALICE_DEVICE,
                                        {"ack": seq, "ok": 1, "err": "ok", "saved": ["咖啡馆", "家", "office"],
                                         "cur": "家"}))
        status, answer = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertEqual(answer["pending"]["state"], "saved")
        self.assertEqual(answer["networks"], ["咖啡馆", "家", "office"])

        # The next one gets a larger seq even within the same millisecond.
        status, answer = self.call("alice", "POST", "/api/wifi",
                                   {"device": ALICE_DEVICE, "op": "up", "i": 2, "ssid": "office"})
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["seq"], seq + 1)
        self.assertEqual(self.open_event(self.last_wifi_event()),
                         {"seq": seq + 1, "op": "up", "ssid": "office", "i": 2})

    def test_failure_and_timeout(self):
        self.ready()
        self.online(ALICE_DEVICE)
        _, answer = self.call("alice", "POST", "/api/wifi",
                              {"device": ALICE_DEVICE, "op": "add", "ssid": "eleventh", "password": PASSWORD})
        device_wifi.ack(self.accounts, ALICE_DEVICE, {"ack": answer["seq"], "ok": 0, "err": "full",
                                                      "saved": ["家"], "cur": "家"})
        _, state = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertEqual(state["pending"]["state"], "failed")
        self.assertIn("存满", state["pending"]["error"])

        # A message the device could not decrypt is answered with ack 0.
        self.now += 1
        _, answer = self.call("alice", "POST", "/api/wifi", {"device": ALICE_DEVICE, "op": "switch", "i": 0,
                                                            "ssid": "家"})
        self.assertTrue(answer["ok"])
        device_wifi.ack(self.accounts, ALICE_DEVICE, {"ack": 0, "ok": 0, "err": "key", "saved": ["家"], "cur": "家"})
        _, state = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertEqual(state["pending"]["state"], "failed")
        self.assertIn("钥匙", state["pending"]["error"])

        # No answer at all.
        self.now += 1
        _, answer = self.call("alice", "POST", "/api/wifi", {"device": ALICE_DEVICE, "op": "remove", "i": 0,
                                                            "ssid": "家"})
        self.assertTrue(answer["ok"])
        self.now += device_wifi.ACK_TIMEOUT_S + 1
        self.online(ALICE_DEVICE)
        _, state = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertEqual(state["pending"]["state"], "timeout")

    def test_bad_requests(self):
        self.ready()
        self.online(ALICE_DEVICE)
        for body in ({"op": "rename", "ssid": "x"},
                     {"op": "add", "ssid": "", "password": PASSWORD},
                     {"op": "add", "ssid": "x" * 33, "password": PASSWORD},
                     {"op": "add", "ssid": "家" * 11, "password": PASSWORD},
                     {"op": "add", "ssid": "x", "password": "short"},
                     {"op": "add", "ssid": "x", "password": "y" * 65},
                     {"op": "add", "ssid": "x\n", "password": PASSWORD},
                     {"op": "remove", "ssid": "家"},
                     {"op": "remove", "i": 10, "ssid": "家"},
                     {"op": "up", "i": True, "ssid": "家"}):
            status, answer = self.call("alice", "POST", "/api/wifi", dict(body, device=ALICE_DEVICE))
            self.assertEqual(status, 200)
            self.assertFalse(answer["ok"], body)
        self.assertIsNone(self.last_wifi_event())
        # An open network (no password) is fine.
        status, answer = self.call("alice", "POST", "/api/wifi", {"device": ALICE_DEVICE, "op": "add", "ssid": "open"})
        self.assertTrue(answer["ok"])

    def test_changed_key_stops_changes_until_repair(self):
        self.ready()
        self.online(ALICE_DEVICE)
        other = wifi_crypto.new_private_key()
        self.assertEqual(device_wifi.hello(self.accounts, ALICE_DEVICE, device_hello(other), logger=lambda _m: None),
                         "mismatch")
        _, state = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertTrue(state["key_changed"])
        _, answer = self.call("alice", "POST", "/api/wifi", {"device": ALICE_DEVICE, "op": "add", "ssid": "x",
                                                            "password": PASSWORD})
        self.assertFalse(answer["ok"])
        self.assertIn("重新配对", answer["error"])
        # The pinned key is still the old one.
        self.assertEqual(device_wifi.device_key(self.accounts, ALICE_DEVICE),
                         wifi_crypto.public_key(self.device_private))
        # Re-pairing pins the new one.
        self.assertEqual(device_wifi.hello(self.accounts, ALICE_DEVICE, device_hello(other), repin=True,
                                           logger=lambda _m: None), "pinned")
        self.assertEqual(device_wifi.device_key(self.accounts, ALICE_DEVICE), wifi_crypto.public_key(other))
        _, state = self.call("alice", "GET", f"/api/wifi?device={ALICE_DEVICE}")
        self.assertFalse(state["key_changed"])

    def test_key_file_and_meta(self):
        self.relay.wifi_private = None
        relay_wifi.setup_key(self.relay, None)
        key_file = Path(self.tmp.name) / "wifi-key"
        self.assertEqual(key_file.read_bytes(), self.relay.wifi_private)
        self.assertEqual(self.accounts.meta(relay_wifi.PUB_META),
                         wifi_crypto.b64e(wifi_crypto.public_key(self.relay.wifi_private)))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.now = 1000.0
        self.logged = []
        self.gate = Gate(root / "tv.db", root / "devices", logger=self.logged.append, clock=lambda: self.now)
        user = self.gate.accounts.create_user("Alice")
        row = self.gate.accounts.register_device(ALICE_DEVICE, "xingzhi-cube-154", SECRET)
        self.gate.accounts.bind(row["claim_code"], user)

    def tearDown(self):
        self.gate.accounts.close()
        self.tmp.cleanup()

    def test_hello_pins_and_reports(self):
        private = wifi_crypto.new_private_key()
        admission = self.gate.admit(device_hello(private), None, 8098, "10.0.0.2")
        self.assertTrue(admission.bound)
        state = device_wifi.state(self.gate.accounts, ALICE_DEVICE)
        self.assertTrue(state["supported"])
        self.assertEqual(state["networks"], ["家", "office"])
        # Older firmware leaves the feature off and nothing is stored.
        hello = device_hello(private)
        for key in ("wifimgr", "wkey", "wifi_saved"):
            del hello[key]
        self.gate.admit(hello, None, 8098, "10.0.0.2")
        self.assertEqual(device_wifi.state(self.gate.accounts, ALICE_DEVICE)["networks"], ["家", "office"])

    def test_repair_window_repins_the_key(self):
        first, second = wifi_crypto.new_private_key(), wifi_crypto.new_private_key()
        self.gate.admit(device_hello(first), None, 8098, "10.0.0.2")
        self.gate.admit(device_hello(second), None, 8098, "10.0.0.2")
        self.assertEqual(device_wifi.device_key(self.gate.accounts, ALICE_DEVICE), wifi_crypto.public_key(first))
        self.gate.accounts.start_repair(ALICE_DEVICE, None)
        self.gate.admit(device_hello(second), None, 8098, "10.0.0.2")
        self.assertEqual(device_wifi.device_key(self.gate.accounts, ALICE_DEVICE), wifi_crypto.public_key(second))


class MediaServerTests(unittest.TestCase):
    """The media server's three parts: CONFIG key, SWITCH forwarding, STATUS ack."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.events = root / "events.json"
        self.gate = Gate(root / "tv.db", root / "devices", logger=lambda _m: None)
        self.gate.accounts.set_meta("wifi_pub", "A" * 43)
        self.logged = []
        self.server = types.SimpleNamespace(device_wifimgr=True, tenancy=self.gate, device_id=ALICE_DEVICE,
                                            logger=self.logged.append, phase="", device_status=None)
        device_wifi.hello(self.gate.accounts, ALICE_DEVICE, device_hello(wifi_crypto.new_private_key()))

    def tearDown(self):
        self.gate.accounts.close()
        self.tmp.cleanup()

    def test_config_carries_the_key_only_for_new_firmware(self):
        self.assertEqual(AVServer._wifi_config(self.server), {"wifi_key": "A" * 43})
        self.server.device_wifimgr = False
        self.assertEqual(AVServer._wifi_config(self.server), {})

    def test_event_becomes_a_switch_packet(self):
        watcher = SwitchWatcher(self.events)
        envelope = {"v": 1, "n": "AAECAwQFBgcICQoL", "c": "Xa5ImP6K9whZ"}
        events.write_event(self.events, "wifi", value=int(time.time()), text=json.dumps(envelope))
        self.assertIsNone(watcher.poll("ch000", time.monotonic(), force=True))
        controls = watcher.take_controls()
        self.assertEqual([c[0] for c in controls], ["wifi"])
        a, b = socket.socketpair()
        try:
            seq = AVServer._wifi_send(self.server, a, 7, 3, controls[0][1])
            self.assertEqual(seq, 4)
            packet = receive_packet(b, 1.0, expected_session=7)
            self.assertEqual(packet.kind, Kind.SWITCH)
            self.assertEqual(json.loads(packet.payload), {"wifi": envelope})
            # Old news is not delivered, and firmware without the feature gets nothing.
            self.assertEqual(AVServer._wifi_send(self.server, a, 7, 4, (json.dumps(envelope), time.time() - 600)), 4)
            self.server.device_wifimgr = False
            self.assertEqual(AVServer._wifi_send(self.server, a, 7, 4, (json.dumps(envelope), time.time())), 4)
            b.setblocking(False)
            with self.assertRaises(BlockingIOError):
                b.recv(1)
        finally:
            a.close()
            b.close()

    def test_status_ack_is_stored(self):
        report = {"wifi": {"ack": 12, "ok": 1, "err": "ok", "saved": ["n1"], "cur": "n1"}}
        AVServer._take_status(self.server, Packet(Kind.STATUS, 1, 0, 0, json_bytes(report)))
        state = device_wifi.state(self.gate.accounts, ALICE_DEVICE)
        self.assertEqual(state["networks"], ["n1"])
        row = self.gate.accounts._one("SELECT ack_seq, ack_ok FROM device_wifi WHERE device_id=?", (ALICE_DEVICE,))
        self.assertEqual((row["ack_seq"], row["ack_ok"]), (12, 1))

    def test_paused_session_keeps_the_change(self):
        watcher = SwitchWatcher(self.events)
        watcher.requeue([("wifi", ("{}", 1.0))])
        self.assertEqual(watcher.take_controls(), [("wifi", ("{}", 1.0))])

    def test_events_round_trip(self):
        envelope = '{"v":1,"n":"' + "A" * 16 + '","c":"' + "B" * 400 + '"}'
        events.write_event(self.events, "volume", value=30)
        events.write_event(self.events, "wifi", value=123, text=envelope)
        events.write_event(self.events, "volume", value=40)
        listed = events.read_events(self.events)
        self.assertEqual([(e.action, e.value, e.text) for e in listed],
                         [("volume", 30, ""), ("wifi", 123, envelope), ("volume", 40, "")])
        with self.assertRaises(ValueError):
            events.write_event(self.events, "wifi", value=1, text="")
        with self.assertRaises(ValueError):
            events.write_event(self.events, "wifi", value=None, text="{}")
        with self.assertRaises(ValueError):
            events.write_event(self.events, "wifi", value=1, text="x" * (events.DATA_MAX + 1))

if __name__ == "__main__":
    unittest.main()
