"""The signed channel pack (server/channelpack.py): wrap/unwrap, validation, and writing
it into channels.txt and its meta without losing what this home learned about the lines.

Design: docs/superpowers/specs/2026-11-04-minimal-product-design.md section 4.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import channelpack, ed25519, routes

SECRET = bytes(range(32))
PUBLIC = ed25519.public_key(SECRET).hex()


def pack(version="2026-11-04.1", channels=None):
    return {"v": 1, "version": version, "channels": channels if channels is not None else [
        {"id": "cctv1", "name": "CCTV1", "tvg_id": "CCTV1",
         "lines": [{"url": "http://a.example/1.m3u8", "ua": "UA-1"}, {"url": "http://b.example/1.m3u8"}]},
        {"id": "cctv2", "name": "CCTV2", "lines": [{"url": "http://a.example/2.m3u8"}]},
    ]}


def signed(data):
    # 绕过 wrap 的校验，模拟一份“签了名但内容有问题”的包
    payload = json.dumps(data, ensure_ascii=False)
    sig = ed25519.sign(SECRET, payload.encode("utf-8")).hex()
    return json.dumps({"v": 1, "payload": payload, "sig": sig}).encode("utf-8")


class WrapTests(unittest.TestCase):
    def test_round_trip(self):
        raw = channelpack.wrap(pack(), SECRET)
        self.assertEqual(channelpack.unwrap(raw, PUBLIC), pack())

    def test_tampered_or_wrong_key_is_refused(self):
        envelope = json.loads(channelpack.wrap(pack(), SECRET))
        envelope["payload"] = envelope["payload"].replace("CCTV2", "CCTV9")
        with self.assertRaises(channelpack.PackError):
            channelpack.unwrap(json.dumps(envelope).encode(), PUBLIC)
        other = ed25519.public_key(bytes(32)).hex()
        with self.assertRaises(channelpack.PackError):
            channelpack.unwrap(channelpack.wrap(pack(), SECRET), other)
        with self.assertRaises(channelpack.PackError):
            channelpack.unwrap(b"not json", PUBLIC)

    def test_garbage_never_leaks_other_exceptions(self):
        for raw in (b"", b"[]", b"null", b"\xff", b'{"payload": 1, "sig": "00"}',
                    b'{"payload": "\\ud800", "sig": "00"}', b"[" * 100000):
            for public in (PUBLIC, "", "zz", None):
                with self.assertRaises(channelpack.PackError, msg=(raw[:20], public)):
                    channelpack.unwrap(raw, public)

    def test_unwritable_content_is_refused_even_when_signed(self):
        def one(**changes):
            return [{"id": "c1", "name": "x", "lines": [{"url": "http://x"}], **changes}]
        long_line = [{"url": "http://x/" + "a" * 380, "ua": "u" * 120}]
        for channels in (one(name="a b\u2028c"), one(name="   "), one(name=" A "), one(name="a\u0085b"),
                         one(id="#a"), one(id="a b"),
                         one(lines=[{"url": "http://x", "ua": "u" * 121}]), one(logo=42), one(tvg_id=" x"),
                         one(lines=long_line)):
            with self.assertRaises(channelpack.PackError, msg=channels):
                channelpack.unwrap(signed(pack(channels=channels)), PUBLIC)

    def test_bad_content_is_refused_even_when_signed(self):
        for channels in ([{"id": "a|b", "name": "x", "lines": [{"url": "http://x"}]}],
                         [{"id": "c1", "name": "x", "lines": []}],
                         [{"id": "c1", "name": "x", "lines": [{"url": "ftp://x"}]}],
                         [{"id": "c1", "name": "x", "lines": [{"url": "http://x"}]},
                          {"id": "c1", "name": "y", "lines": [{"url": "http://y"}]}]):
            with self.assertRaises(channelpack.PackError, msg=channels):
                channelpack.unwrap(channelpack.wrap(pack(channels=channels), SECRET), PUBLIC)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "channels.txt"
        self.path.write_text("# empty\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_primary_lines_and_backups(self):
        channelpack.apply(pack(), self.path, {})
        lines = [l for l in self.path.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines, ["cctv1 | CCTV1 | http://a.example/1.m3u8 | UA-1",
                                 "cctv2 | CCTV2 | http://a.example/2.m3u8"])
        meta = routes.read_meta(self.path)
        self.assertEqual(meta["cctv1"]["routes"], [{"url": "http://b.example/1.m3u8"}])
        self.assertEqual(meta["cctv1"]["tvg_id"], "CCTV1")

    def test_keeps_learned_fields_and_drops_removed_channels(self):
        channelpack.apply(pack(), self.path, {})
        routes.update_meta(self.path, lambda d: d["cctv1"].update(good="0123456789ab"))
        channelpack.apply(pack(version="2", channels=pack()["channels"][:1]), self.path, {})
        meta = routes.read_meta(self.path)
        self.assertEqual(meta["cctv1"]["good"], "0123456789ab")
        self.assertNotIn("cctv2", meta)
        self.assertNotIn("cctv2", self.path.read_text(encoding="utf-8"))

    def test_duplicate_ids_in_prefs_write_once(self):
        channelpack.apply(pack(), self.path, {"hidden": [], "order": ["cctv2", "cctv2", "cctv1"]})
        body = [l.split(" |")[0] for l in self.path.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
        self.assertEqual(body, ["cctv2", "cctv1"])

    def test_written_table_reads_back_through_the_tv_parser(self):
        from server import live
        channelpack.apply(pack(), self.path, {})
        channels, labels, agents = live.parse_channels(self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(channels), ["cctv1", "cctv2"])
        self.assertEqual(labels, {"cctv1": "CCTV1", "cctv2": "CCTV2"})
        self.assertEqual(channels["cctv1"], "http://a.example/1.m3u8")
        self.assertEqual(agents["cctv1"], "UA-1")

    def test_prefs_hide_and_reorder(self):
        channelpack.apply(pack(), self.path, {"hidden": ["cctv1"], "order": []})
        self.assertNotIn("cctv1 |", self.path.read_text(encoding="utf-8"))
        channelpack.apply(pack(), self.path, {"hidden": [], "order": ["cctv2", "cctv1"]})
        body = [l.split(" |")[0] for l in self.path.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
        self.assertEqual(body, ["cctv2", "cctv1"])


if __name__ == "__main__":
    unittest.main()
