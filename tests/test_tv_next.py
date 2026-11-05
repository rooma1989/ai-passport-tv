"""Next video from the device (P1c): CONFIG offers it, STATUS {"next": 1} asks, the media server
calls the relay's /api/next for that device with the relay key."""

from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import channel_groups
from server.protocol import Kind, Packet, json_bytes
from server.tv_server import AVServer


class NextTests(unittest.TestCase):
    def setUp(self):
        self.logs, self.calls = [], []
        self.done, self.release = threading.Event(), threading.Event()
        self.release.set()

        class Client:
            def __init__(client_self, device):
                client_self.device = device

            def next(client_self):
                self.calls.append(client_self.device)
                self.done.set()
                self.release.wait(2)
                return {"ok": True}

        self.server = types.SimpleNamespace(device_next=True, relay_for=Client, device_id="aaaaaaaaaaa1",
                                            logger=self.logs.append, device_recover=False, tenancy=None,
                                            _next_busy=False, _recover_out=[])
        for name in ("_next_config", "_next_ask", "_recover_ask"):
            setattr(self.server, name, types.MethodType(getattr(AVServer, name), self.server))

    def status(self, report):
        AVServer._take_status(self.server, Packet(Kind.STATUS, 1, 0, 0, json_bytes(report)))

    def test_config_offers_next_with_the_prefixes(self):
        self.assertEqual(self.server._next_config(), {"next": list(channel_groups.NEXT_PREFIXES)})
        self.server.device_next = False
        self.assertEqual(self.server._next_config(), {}, "firmware that did not announce it")
        self.server.device_next, self.server.relay_for = True, None
        self.assertEqual(self.server._next_config(), {}, "no relay key")

    def test_status_next_calls_the_relay_for_this_device(self):
        self.status({"next": 1})
        self.assertTrue(self.done.wait(2))
        self.assertEqual(self.calls, ["aaaaaaaaaaa1"])

    def test_status_next_without_announcing_is_ignored(self):
        self.server.device_next = False
        self.status({"next": 1})
        self.assertFalse(self.done.wait(0.3))
        self.assertTrue(any("next" in line for line in self.logs))

    def test_unbound_device_is_ignored(self):
        self.server.device_id = ""
        self.status({"next": 1})
        self.assertFalse(self.done.wait(0.3))

    def test_does_not_pile_up_and_does_not_block(self):
        self.release.clear()
        self.status({"next": 1})                # returns at once although the relay call is stuck
        self.assertTrue(self.done.wait(2))
        self.status({"next": 1})
        self.assertEqual(self.calls, ["aaaaaaaaaaa1"], "second ask dropped while one is in flight")
        self.release.set()
        for _ in range(100):
            if not self.server._next_busy:
                break
            threading.Event().wait(0.02)
        self.assertFalse(self.server._next_busy)
        self.done.clear()
        self.status({"next": 1})
        self.assertTrue(self.done.wait(2))


if __name__ == "__main__":
    unittest.main()
