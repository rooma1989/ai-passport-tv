"""The signed channel pack (server/channelpack.py): wrap/unwrap, validation, and writing
it into channels.txt and its meta without losing what this home learned about the lines.

Design: docs/superpowers/specs/2026-11-04-minimal-product-design.md section 4.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
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

    def test_version_must_be_dotted_numbers(self):
        for version in ("v1", "", "2026-11-04.", "2026--11", ".1", "1.2a", "2026-11-04 1", "1" * 10, "1.2.3.4.5.6.7.8.9"):
            with self.subTest(version=version), self.assertRaises(channelpack.PackError):
                channelpack.unwrap(signed(pack(version=version)), PUBLIC)
        for version in ("1", "2026-11-04.1", "2026.11.04-2"):
            self.assertEqual(channelpack.unwrap(signed(pack(version=version)), PUBLIC)["version"], version)

    def test_versions_compare_as_numbers(self):
        self.assertTrue(channelpack.newer("2026-11-04.10", "2026-11-04.9"))
        self.assertTrue(channelpack.newer("2026-11-04.1", "2026-11-04"))
        self.assertTrue(channelpack.newer("2026-12-01.1", "2026-11-30.9"))
        self.assertFalse(channelpack.newer("2026-11-04.1", "2026-11-04.1"))
        self.assertFalse(channelpack.newer("2026-11-03.5", "2026-11-04.1"))

    def test_bad_content_is_refused_even_when_signed(self):
        for channels in ([{"id": "a|b", "name": "x", "lines": [{"url": "http://x"}]}],
                         [{"id": "c1", "name": "x", "lines": []}],
                         [{"id": "c1", "name": "x", "lines": [{"url": "ftp://x"}]}],
                         [{"id": "c1", "name": "x", "lines": [{"url": "http://x"}]},
                          {"id": "c1", "name": "y", "lines": [{"url": "http://y"}]}]):
            with self.assertRaises(channelpack.PackError, msg=channels):
                channelpack.unwrap(channelpack.wrap(pack(channels=channels), SECRET), PUBLIC)


class LimitsTests(unittest.TestCase):
    def test_limits_match_the_tv_parser(self):
        from server import live      # 只在测试里导入：channelpack 自己不能拖进 server.live
        self.assertEqual(channelpack.ID_MAX, live.TV_CHANNEL_ID_MAX - 1)
        self.assertEqual(channelpack.LINE_MAX, live.MAX_CHANNEL_LINE_BYTES)
        self.assertEqual(channelpack.CHANNELS_MAX, live.TV_TABLE_MAX)


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

    def test_admin_view_and_set_prefs_rewrite_the_table(self):
        raw = channelpack.wrap(pack(), SECRET)
        channelpack.remember(raw, self.path)
        channelpack.apply(pack(), self.path, {})
        view = channelpack.admin_view(self.path, PUBLIC)
        self.assertEqual(view["version"], "2026-11-04.1")
        self.assertEqual([(c["id"], c["hidden"]) for c in view["channels"]], [("cctv1", False), ("cctv2", False)])
        answer = channelpack.set_prefs(self.path, PUBLIC, {"hidden": ["cctv2"], "order": ["cctv2", "cctv1"]})
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([(c["id"], c["hidden"]) for c in channelpack.admin_view(self.path, PUBLIC)["channels"]],
                         [("cctv2", True), ("cctv1", False)])
        self.assertNotIn("cctv2 |", self.path.read_text(encoding="utf-8"))
        self.assertFalse(channelpack.set_prefs(self.path, PUBLIC, {"hidden": "x"})["ok"])

    def test_set_prefs_keeps_at_least_one_channel(self):
        channelpack.remember(channelpack.wrap(pack(), SECRET), self.path)
        channelpack.apply(pack(), self.path, {})
        before = self.path.read_text(encoding="utf-8")
        answer = channelpack.set_prefs(self.path, PUBLIC, {"hidden": ["cctv1", "cctv2"], "order": []})
        self.assertEqual(answer, {"ok": False, "error": "至少留一个台"})
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(channelpack.read_prefs(self.path), {"hidden": [], "order": []})

    def test_set_prefs_dedupes_and_needs_a_remembered_pack(self):
        self.assertEqual(channelpack.admin_view(self.path, PUBLIC), {"ok": True, "configured": False})
        self.assertFalse(channelpack.set_prefs(self.path, PUBLIC, {"hidden": [], "order": []})["ok"])
        channelpack.remember(channelpack.wrap(pack(), SECRET), self.path)
        channelpack.set_prefs(self.path, PUBLIC, {"hidden": ["cctv2", "cctv2"], "order": ["cctv1", "cctv1"]})
        self.assertEqual(channelpack.read_prefs(self.path), {"hidden": ["cctv2"], "order": ["cctv1"]})
        self.assertEqual(json.loads((self.path.parent / channelpack.PREFS_NAME).read_text(encoding="utf-8")),
                         {"hidden": ["cctv2"], "order": ["cctv1"]})


class SeedAndUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "channels.txt"
        self.bundled = Path(self.tmp.name) / "bundled.json"
        self.bundled.write_bytes(channelpack.wrap(pack(), SECRET))

    def tearDown(self):
        self.tmp.cleanup()

    def updater(self, raw, logs=None):
        return channelpack.Updater(self.path, "http://pack.example/channels.json", PUBLIC,
                                   fetch=lambda url: raw, log=(logs if logs is not None else []).append)

    def failing_once(self):
        """channeltable.save that raises OSError on its first call and works afterwards."""
        real = channelpack.channeltable.save
        calls = []

        def save(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk")
            return real(*args, **kwargs)

        return mock.patch.object(channelpack.channeltable, "save", side_effect=save)

    def test_seed_writes_the_bundled_pack_once(self):
        self.assertTrue(channelpack.seed(self.path, PUBLIC, self.bundled))
        self.assertIn("cctv1 | CCTV1", self.path.read_text(encoding="utf-8"))
        self.assertFalse(channelpack.seed(self.path, PUBLIC, self.bundled), "same version: already has it")

    def test_seed_applies_a_newer_bundled_pack(self):
        channelpack.seed(self.path, PUBLIC, self.bundled)
        self.bundled.write_bytes(channelpack.wrap(pack(version="2026-11-04.2", channels=pack()["channels"][:1]), SECRET))
        self.assertTrue(channelpack.seed(self.path, PUBLIC, self.bundled), "an app upgrade ships a newer pack")
        self.assertNotIn("cctv2", self.path.read_text(encoding="utf-8"))
        self.assertEqual(channelpack.remembered(self.path, PUBLIC)["version"], "2026-11-04.2")

    def test_seed_never_goes_back_to_an_older_bundled_pack(self):
        self.assertEqual(self.updater(channelpack.wrap(pack(version="2026-11-05.1"), SECRET)).check(), "2026-11-05.1")
        self.assertFalse(channelpack.seed(self.path, PUBLIC, self.bundled))
        self.assertEqual(channelpack.remembered(self.path, PUBLIC)["version"], "2026-11-05.1")

    def test_seed_retries_after_a_failed_write(self):
        with self.failing_once():
            with self.assertRaises(OSError):
                channelpack.seed(self.path, PUBLIC, self.bundled)
            self.assertIsNone(channelpack.remembered(self.path, PUBLIC), "not remembered when the write failed")
            self.assertTrue(channelpack.seed(self.path, PUBLIC, self.bundled), "the next start applies it")
        self.assertIn("cctv1 | CCTV1", self.path.read_text(encoding="utf-8"))
        self.assertEqual(channelpack.remembered(self.path, PUBLIC)["version"], "2026-11-04.1")

    def test_updater_retries_after_a_failed_write(self):
        channelpack.seed(self.path, PUBLIC, self.bundled)
        newer = channelpack.wrap(pack(version="2026-11-05.1", channels=pack()["channels"][:1]), SECRET)
        logs = []
        updater = self.updater(newer, logs)
        with self.failing_once():
            self.assertIsNone(updater.check())
            self.assertEqual(channelpack.remembered(self.path, PUBLIC)["version"], "2026-11-04.1")
            self.assertEqual(updater.check(), "2026-11-05.1", "the next check applies it")
        self.assertNotIn("cctv2", self.path.read_text(encoding="utf-8"))
        self.assertTrue(any("写频道表失败" in line for line in logs), logs)

    def test_updater_ignores_older_and_equal_versions(self):
        channelpack.seed(self.path, PUBLIC, self.bundled)
        self.assertEqual(self.updater(channelpack.wrap(pack(version="2026-11-05.1"), SECRET)).check(), "2026-11-05.1")
        table = self.path.read_text(encoding="utf-8")
        logs = []
        older = channelpack.wrap(pack(version="2026-11-04.9", channels=pack()["channels"][:1]), SECRET)
        self.assertIsNone(self.updater(older, logs).check(), "older: possible replay")
        self.assertTrue(any("旧" in line for line in logs), logs)
        equal = channelpack.wrap(pack(version="2026-11-05.1", channels=pack()["channels"][:1]), SECRET)
        self.assertIsNone(self.updater(equal).check(), "equal: nothing to do")
        self.assertEqual(self.path.read_text(encoding="utf-8"), table)
        self.assertEqual(channelpack.remembered(self.path, PUBLIC)["version"], "2026-11-05.1")

    def test_fetch_refuses_an_oversized_body(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                return b"x" * n

        with mock.patch.object(channelpack.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaises(channelpack.TooLarge):
                channelpack._fetch("https://pack.example/c.json")
            logs = []
            channelpack.Updater(self.path, "https://pack.example/c.json", PUBLIC, log=logs.append).check()
        self.assertTrue(any("频道包太大" in line for line in logs), logs)
        self.assertFalse(any("签名" in line for line in logs), logs)

    def test_seed_without_a_bundled_file_does_nothing(self):
        self.assertFalse(channelpack.seed(self.path, PUBLIC, Path(self.tmp.name) / "missing.json"))
        self.assertFalse(self.path.exists())

    def test_updater_applies_newer_and_ignores_bad(self):
        channelpack.seed(self.path, PUBLIC, self.bundled)
        logs = []
        newer = channelpack.wrap(pack(version="2026-11-05.1", channels=pack()["channels"][:1]), SECRET)
        updater = channelpack.Updater(self.path, "http://pack.example/channels.json", PUBLIC,
                                      fetch=lambda url: newer, log=logs.append)
        self.assertEqual(updater.check(), "2026-11-05.1")
        self.assertNotIn("cctv2", self.path.read_text(encoding="utf-8"))
        self.assertIsNone(updater.check(), "same version: nothing to do")
        bad = channelpack.Updater(self.path, "http://pack.example/channels.json", PUBLIC,
                                  fetch=lambda url: b'{"v":1,"payload":"{}","sig":"00"}', log=logs.append)
        self.assertIsNone(bad.check())
        self.assertIn("cctv1", self.path.read_text(encoding="utf-8"), "a bad pack never touches the table")
        self.assertTrue(any("签名" in line for line in logs))

    def test_set_prefs_does_not_save_when_apply_fails(self):
        channelpack.seed(self.path, PUBLIC, self.bundled)
        with mock.patch.object(channelpack, "apply", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                channelpack.set_prefs(self.path, PUBLIC, {"hidden": ["cctv2"], "order": []})
        self.assertEqual(channelpack.read_prefs(self.path), {"hidden": [], "order": []})

    def test_concurrent_set_prefs_and_updates_keep_the_table_consistent(self):
        channelpack.seed(self.path, PUBLIC, self.bundled)
        versions = [channelpack.wrap(pack(version=f"2026-12-{n:02d}.1"), SECRET) for n in range(1, 41)]
        counter = iter(range(1000))
        errors = []

        def update():
            updater = channelpack.Updater(self.path, "http://x", PUBLIC, log=lambda m: None,
                                          fetch=lambda url: versions[next(counter) % len(versions)])
            for _ in range(10):
                updater.check()

        def prefs():
            for n in range(10):
                answer = channelpack.set_prefs(self.path, PUBLIC, {"hidden": ["cctv2"] if n % 2 else [], "order": []})
                if not answer["ok"]:
                    errors.append(answer)

        def guarded(fn):
            def run():
                try:
                    fn()
                except Exception as error:  # noqa: BLE001
                    errors.append(error)
            return run

        threads = [threading.Thread(target=guarded(fn)) for fn in (update, prefs, update, prefs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        ids = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                self.assertIn(line.count("|"), (2, 3), line)
                ids.append(line.split("|")[0].strip())
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(ids)
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])


class ToolTests(unittest.TestCase):
    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "channelpack.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_keygen_from_table_sign_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            key = tmp / "signing.key"
            result = self.run_tool("keygen", "--out", str(key))
            self.assertEqual(result.returncode, 0, result.stderr)
            public = result.stdout.strip()
            self.assertEqual(len(public), 64)
            self.assertNotIn(key.read_text().strip(), result.stdout, "the private key is never printed")
            self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")
            self.assertNotEqual(self.run_tool("keygen", "--out", str(key)).returncode, 0, "never overwrites")
            table = tmp / "channels.txt"
            table.write_text("# t\ncctv1 | CCTV1 | http://a.example/1.m3u8 | UA\n", encoding="utf-8")
            routes.write_meta(table, {"cctv1": {"routes": [{"url": "http://b.example/1.m3u8"}], "good": "x"}})
            draft = tmp / "pack.json"
            self.assertEqual(self.run_tool("from-table", str(table), "--version", "2026-11-04.1", "--out", str(draft)).returncode, 0)
            self.assertEqual(json.loads(draft.read_text())["channels"][0]["lines"][1]["url"], "http://b.example/1.m3u8")
            signed = tmp / "signed.json"
            self.assertEqual(self.run_tool("sign", str(draft), "--key", str(key), "--out", str(signed)).returncode, 0)
            ok = self.run_tool("verify", str(signed), "--public", public)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertIn("2026-11-04.1", ok.stdout)

    def test_from_table_skips_rows_the_pack_would_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            table = tmp / "channels.txt"
            table.write_text("\n".join([
                "good1 | Good | http://a.example/1.m3u8",
                "bad url | Bad | ftp://a.example/2",
                "good1 | Dup | http://a.example/3.m3u8",
                "toolongidentifier_xx | Long | http://a.example/4.m3u8",
                "short",
                "good2 | Good2 | http://a.example/5.m3u8",
            ]) + "\n", encoding="utf-8")
            draft = tmp / "pack.json"
            result = self.run_tool("from-table", str(table), "--version", "2026-11-04.1", "--out", str(draft))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([c["id"] for c in json.loads(draft.read_text())["channels"]], ["good1", "good2"])
            for needle in ("bad url", "ftp", "toolongidentifier_xx", "short"):
                self.assertIn(needle, result.stderr)


if __name__ == "__main__":
    unittest.main()
