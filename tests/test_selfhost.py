"""The self-hosted server's start-up (server/selfhost): config, data folder, secrets, setup code.

Phase S1a of docs/superpowers/specs/2026-10-15-self-hosting-design.md. What is
checked: the default config parses to the documented defaults and bad values are
refused with the key named; secrets are generated once, private, and never in the
banner; a setup code exists exactly while there is no admin and changes on every
start; the database is marked self-hosted with the shared token off; backups are
made and pruned; the children get the self-host flags and nothing of the cloud
host's (no pairing token, no built-in channels).
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.selfhost import banner, bootstrap, config
from server.selfhost.__main__ import build_children
from server.tenancy import db
from server.tenancy.gate import LEGACY_TOKEN_KEY


@unittest.skipIf(sys.version_info < (3, 11), "tomllib needs Python 3.11")
class ConfigTests(unittest.TestCase):
    def test_default_file_parses_to_the_defaults(self):
        self.assertEqual(config.parse(config.DEFAULT_TEXT), config.Config())

    def test_values_are_read(self):
        parsed = config.parse('[server]\nlan_port = 18080\npublic_port = 8081\ndevice_address = "nas.local"\n'
                              '[accounts]\nsignup_via_device = "off"\n[devices]\nallow_internet = true\n')
        self.assertEqual((parsed.lan_port, parsed.public_port, parsed.device_address), (18080, 8081, "nas.local"))
        self.assertEqual(parsed.signup_via_device, "off")
        self.assertTrue(parsed.allow_internet)

    def test_bad_values_name_the_key(self):
        cases = {
            "[server]\nlan_port = \"8080\"\n": "lan_port",
            "[server]\nlan_port = true\n": "lan_port",
            "[server]\nlan_port = 8098\n": "端口重复",
            "[server]\nlan_port = 8100\n": "端口重复",
            "[server]\ndevice_address = \"http://192.168.1.2:8098\"\n": "device_address",
            "[server]\npublic_url = \"tv.example.com\"\n": "public_url",
            "[server]\nbind = \"localhost\"\n": "bind",
            "[accounts]\nsignup_via_device = \"everyone\"\n": "signup_via_device",
            "[youtube]\nmode = \"proxy\"\n": "mode",
            "[server\n": "格式不对",
        }
        for text, key in cases.items():
            with self.assertRaises(config.ConfigError, msg=text) as raised:
                config.parse(text)
            self.assertIn(key, str(raised.exception), text)

    def test_ensure_writes_once_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            self.assertTrue(config.ensure(path))
            path.write_text("[server]\nlan_port = 9090\n", encoding="utf-8")
            self.assertFalse(config.ensure(path))
            self.assertEqual(config.load(path).lan_port, 9090)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.layout = bootstrap.Layout(Path(self.tmp.name) / "data")
        bootstrap.prepare(self.layout)

    def tearDown(self):
        self.tmp.cleanup()

    def test_layout_and_private_secrets_folder(self):
        for name in bootstrap.DIRS:
            self.assertTrue((self.layout.root / name).is_dir(), name)
        self.assertEqual(stat.S_IMODE(self.layout.secrets.stat().st_mode), 0o700)
        table = self.layout.channels.read_text(encoding="utf-8")
        self.assertTrue(all(line.startswith("#") for line in table.splitlines()), "the live table starts empty")

    def test_unwritable_folder_is_explained(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir(mode=0o500)
        try:
            with self.assertRaises(bootstrap.DataDirError) as raised:
                bootstrap.prepare(bootstrap.Layout(locked / "data"))
        finally:
            locked.chmod(0o700)
        self.assertIn("PUID", str(raised.exception))

    def test_relay_key_is_generated_once_and_private(self):
        self.assertTrue(bootstrap.relay_key(self.layout))
        first = self.layout.relay_key.read_text()
        self.assertGreaterEqual(len(first.strip()), 40)
        self.assertEqual(stat.S_IMODE(self.layout.relay_key.stat().st_mode), 0o600)
        self.assertFalse(bootstrap.relay_key(self.layout))
        self.assertEqual(self.layout.relay_key.read_text(), first, "a restart keeps the key")

    def test_database_is_self_hosted_with_no_shared_token(self):
        accounts = bootstrap.database(self.layout)
        self.addCleanup(accounts.close)
        self.assertEqual(accounts.meta(LEGACY_TOKEN_KEY), "0")
        self.assertEqual(accounts.meta(bootstrap.SELFHOST_META), "1")
        self.assertEqual(accounts.meta(bootstrap.SCHEMA_META), str(db.SCHEMA_VERSION))

    def test_schema_change_backs_up_first(self):
        accounts = bootstrap.database(self.layout)
        accounts.set_meta(bootstrap.SCHEMA_META, "0")
        accounts.close()
        accounts = bootstrap.database(self.layout)
        self.addCleanup(accounts.close)
        self.assertTrue((self.layout.backups / f"tv-pre-{db.SCHEMA_VERSION}.db").is_file())

    def test_setup_code_exists_only_while_there_is_no_admin(self):
        accounts = bootstrap.database(self.layout)
        self.addCleanup(accounts.close)
        first = bootstrap.setup_code(self.layout, accounts)
        self.assertEqual(len(first), bootstrap.SETUP_CODE_LENGTH)
        self.assertTrue(set(first) <= set(db.CODE_ALPHABET))
        self.assertEqual(self.layout.setup_code.read_text().strip(), first)
        self.assertEqual(stat.S_IMODE(self.layout.setup_code.stat().st_mode), 0o600)
        second = bootstrap.setup_code(self.layout, accounts)
        self.assertNotEqual(first, second, "every start makes a new code until the admin exists")
        accounts.create_user("管理员", "admin")
        self.assertEqual(bootstrap.setup_code(self.layout, accounts), "")
        self.assertFalse(self.layout.setup_code.exists())

    def test_backup_and_prune(self):
        accounts = bootstrap.database(self.layout)
        accounts.create_user("管理员", "admin")
        accounts.close()
        now = time.mktime((2026, 10, 16, 4, 0, 0, 0, 0, -1))
        self.assertTrue(bootstrap.nightly_due(self.layout, clock=lambda: now))
        made = bootstrap.backup(self.layout, clock=lambda: now)
        self.assertEqual(made.name, "tv-20261016.db")
        self.assertFalse(bootstrap.nightly_due(self.layout, clock=lambda: now))
        import sqlite3
        copy = sqlite3.connect(str(made))
        self.assertEqual(copy.execute("SELECT name FROM user").fetchone()[0], "管理员")
        copy.close()
        old = self.layout.backups / "tv-20260901.db"
        old.write_bytes(b"")
        keep = self.layout.backups / "tv-pre-1.db"
        keep.write_bytes(b"")
        self.assertEqual(bootstrap.prune_backups(self.layout, 14, clock=lambda: now), ["tv-20260901.db"])
        self.assertTrue(keep.exists() and made.exists())
        early = time.mktime((2026, 10, 17, 1, 0, 0, 0, 0, -1))
        self.assertFalse(bootstrap.nightly_due(self.layout, clock=lambda: early), "not before 3 o'clock")


class FirmwareTests(unittest.TestCase):
    def test_data_folder_first_then_the_unzipped_bundle_then_the_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp) / "data")
            bootstrap.prepare(layout)
            image = Path(tmp) / "image-firmware"
            image.mkdir()
            self.assertIsNone(bootstrap.firmware_dir(layout, image), "a folder without manifest.json is no firmware")
            (image / "manifest.json").write_text("{}")
            self.assertEqual(bootstrap.firmware_dir(layout, image), image)
            unzipped = layout.firmware / "flash" / "firmware"
            unzipped.mkdir(parents=True)
            (unzipped / "manifest.json").write_text("{}")
            self.assertEqual(bootstrap.firmware_dir(layout, image), unzipped)
            (layout.firmware / "manifest.json").write_text("{}")
            self.assertEqual(bootstrap.firmware_dir(layout, image), layout.firmware)

    def test_relay_and_front_get_the_firmware(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp))
            children = {c.name: c for c in build_children(layout, config.Config(), "ffmpeg", "", "", lambda _m: None,
                                                          firmware=layout.firmware)}
            bare = {c.name: c for c in build_children(layout, config.Config(), "ffmpeg", "", "", lambda _m: None)}
        self.assertEqual(children["relay"].env["TV_FIRMWARE_MANIFEST"], str(layout.firmware / "manifest.json"))
        self.assertEqual(children["relay"].env["TV_FLASHER_URL"], "/flash/")
        front = children["front"].argv
        self.assertEqual(front[front.index("--firmware-dir") + 1], str(layout.firmware))
        self.assertTrue(Path(front[front.index("--flasher-dir") + 1], "index.html").is_file())
        self.assertNotIn("TV_FIRMWARE_MANIFEST", bare["relay"].env)
        self.assertNotIn("--firmware-dir", bare["front"].argv)

    def test_banner_says_localhost_for_web_serial(self):
        text = "\n".join(banner.flash_lines(8080, None, in_container=False))
        self.assertIn("http://localhost:8080/flash/", text)
        self.assertIn("--fetch-firmware", text)
        self.assertNotIn("fetch-firmware", "\n".join(banner.flash_lines(8080, Path("/x"), in_container=False)))


class BannerTests(unittest.TestCase):
    def test_setup_banner_has_the_address_code_and_qr(self):
        lines = banner.lines("192.168.1.20", 8080, "K7P4QX2M9A", "192.168.1.20", in_container=False)
        text = "\n".join(lines)
        self.assertIn("http://192.168.1.20:8080/tv/setup", text)
        self.assertIn("K7P4Q-X2M9A", text)
        self.assertIn("小电视上的服务器地址填：192.168.1.20", text)
        self.assertTrue(any("█" in line for line in lines), "a terminal QR code")

    def test_container_banner_does_not_invent_an_address(self):
        text = "\n".join(banner.lines("", 8080, "K7P4QX2M9A", "", in_container=True))
        self.assertIn(banner.THIS_COMPUTER, text)
        self.assertNotIn("█", text, "no QR code for an address it does not know")

    def test_loopback_is_never_offered_to_devices(self):
        text = "\n".join(banner.lines("127.0.0.1", 8080, "", "", in_container=False))
        self.assertIn(f"服务器地址填：{banner.THIS_COMPUTER}", text)

    def test_no_code_once_set_up(self):
        text = "\n".join(banner.lines("192.168.1.20", 8080, "", "192.168.1.20", in_container=False))
        self.assertNotIn("设置码", text)
        self.assertIn("/tv/admin", text)

    def test_terminal_qr_is_square(self):
        lines = banner.terminal_qr("http://192.168.1.20:8080/tv/setup#K7P4QX2M9A")
        width = len(lines[0])
        self.assertTrue(all(len(line) == width for line in lines))
        self.assertEqual(len(lines), (width + 1) // 2)


class ChildrenTests(unittest.TestCase):
    def test_children_run_in_self_host_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = bootstrap.Layout(Path(tmp))
            settings = config.Config(public_port=8081, signup_via_device="anywhere")
            children = build_children(layout, settings, "/usr/bin/ffmpeg", "192.168.1.20",
                                      "http://192.168.1.20:8080", log=lambda _m: None)
        by_name = {child.name: child for child in children}
        self.assertEqual(list(by_name), ["relay", "xingzhi", "passport", "front"])
        relay = by_name["relay"].argv
        for flag in ("--selfhost", "--db", "--setup-code-file", "--main-channels"):
            self.assertIn(flag, relay)
        self.assertEqual(relay[relay.index("--device-address") + 1], "192.168.1.20")
        self.assertNotIn("--cstone", relay, "YouTube is off by default")
        for name, port, geometry in (("xingzhi", "8098", "240x180"), ("passport", "8097", "320x180")):
            child = by_name[name]
            self.assertEqual(child.argv[child.argv.index("--port") + 1], port)
            self.assertNotIn("--token-file", child.argv, "no shared pairing token")
            self.assertNotIn("--channel", child.argv, "the table starts empty")
            self.assertEqual(child.env["TV_GEOMETRY"], geometry)
            self.assertEqual(child.env["TV_NO_BUILTIN_CHANNELS"], "1")
            self.assertEqual(child.env["TV_ALLOW_PUBLIC"], "0")
            self.assertNotIn("TV_ASR_SOCKET", child.env)
        front = by_name["front"].argv
        self.assertEqual(front[front.index("--public-port") + 1], "8081")
        self.assertEqual(front[front.index("--public-bind") + 1], "127.0.0.1",
                         "without Docker the tunnel program is on the same machine")
        self.assertEqual(front[front.index("--signup") + 1], "anywhere")
        self.assertIn("xingzhi=8198", front)


if __name__ == "__main__":
    unittest.main()
