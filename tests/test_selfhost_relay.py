"""The relay in self-host mode (server/relay/setup.py): first admin, cookies on plain HTTP, playlist import.

Phases S1b and S1c of docs/superpowers/specs/2026-10-15-self-hosting-design.md,
driven over real sockets like test_relay_tenants.py. Each self-host behaviour is
checked next to the cloud host's, which must not change: there the relay is
behind nginx over HTTPS, the cookie is always Secure, /tv/setup does not exist
and the admin backend has no playlist import.
"""

from __future__ import annotations

import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import admin, playlist, presets, setup, web
from server.tenancy.accounts import Accounts

from test_relay_web import KEY, FakeClient

CODE = "K7P4QX2M9A"
PLAYLIST = ("#EXTM3U\n#EXTINF:-1 tvg-id=\"a\",家庭台 | 一\nhttp://192.168.1.5/a.m3u8\n"
            "#EXTINF:-1 http-user-agent=\"VLC/3\",Two\nhttps://example.com/b.m3u8\n"
            "#EXTINF:-1,bad\nhttp://example.com/has space.m3u8\n")


class RelayHarness(unittest.TestCase):
    selfhost = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accounts = Accounts(root / "tv.db")
        self.code_file = root / "setup-code"
        self.code_file.write_text(CODE + "\n", encoding="ascii")
        self.main = root / "channels.txt"
        self.main.write_text("# empty\n", encoding="utf-8")
        self.logs = []
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, main_channels=self.main,
                               accounts=self.accounts, devices_dir=root / "devices", log=self.logs.append,
                               selfhost=setup.SelfHost(setup_code_file=self.code_file) if self.selfhost else None)
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        for server in (self.streams, self.manage):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        for server in (self.streams, self.manage):
            server.shutdown()
            server.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        """(status, headers, parsed JSON or raw bytes)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        sent = {"Content-Type": "application/json"} if data is not None else {}
        sent.update(headers or {})
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return response.status, response, parsed

    def setup_admin(self, headers=None):
        return self.request("POST", "/tv/api/setup", {"code": CODE[:5] + "-" + CODE[5:], "name": "小明"},
                            headers)


class SetupPendingTests(RelayHarness):
    def test_every_page_points_to_setup_until_there_is_an_admin(self):
        status, response, _ = self.request("GET", "/tv/")
        self.assertEqual((status, response.getheader("Location")), (302, "/tv/setup"))
        status, _, answer = self.request("GET", "/tv/api/state")
        self.assertEqual((status, answer["setup"]), (503, True))
        status, _, answer = self.request("POST", "/tv/api/creators", {"url": "x"})
        self.assertEqual(status, 503)
        status, _, page = self.request("GET", "/tv/setup")
        self.assertEqual(status, 200)
        self.assertIn("第一次设置".encode(), page)
        status, _, _ = self.request("GET", "/tv/icon-180.png")
        self.assertEqual(status, 200, "the setup page's icon is still served")
        self.assertEqual(self.request("GET", "/tv/api/setup/info")[2]["pending"], True)

    def test_wrong_code_is_refused_and_limited(self):
        for _ in range(setup.WRONG_LIMIT[0]):
            answer = self.request("POST", "/tv/api/setup", {"code": "AAAAA-BBBBB"})[2]
            self.assertIn("设置码不对", answer["error"])
        answer = self.setup_admin()[2]
        self.assertFalse(answer["ok"])
        self.assertIn("太多次", answer["error"], "even the right code waits once the network is limited")
        self.assertIsNone(self.accounts.admin_id())

    def test_right_code_makes_the_first_admin_once(self):
        status, response, answer = self.setup_admin({"Host": "192.168.1.20:8080"})
        self.assertTrue(answer["ok"], answer)
        admin = self.accounts.admin_id()
        self.assertEqual(self.accounts.user(admin)["name"], "小明")
        self.assertFalse(self.code_file.exists(), "the code is single use")
        cookie = response.getheader("Set-Cookie")
        self.assertTrue(cookie.startswith("tvs="))
        self.assertNotIn("Secure", cookie, "plain HTTP on the LAN: a Secure cookie would not be kept")
        self.assertIn("HttpOnly", cookie)
        self.assertEqual(answer["device_address"], "192.168.1.20")
        self.assertEqual(self.accounts.meta("device_address"), "192.168.1.20")
        self.assertEqual(self.accounts.meta("public_url"), "http://192.168.1.20:8080")
        self.assertEqual(self.relay.public_url, "http://192.168.1.20:8080")
        actions = [row["action"] for row in self.accounts.audit_rows()]
        self.assertIn("user.setup", actions)
        # Signed in with the new cookie; the setup page is gone.
        token = cookie.split(";")[0]
        self.assertEqual(self.request("GET", "/tv/api/state", headers={"Cookie": token})[0], 200)
        status, response, _ = self.request("GET", "/tv/setup")
        self.assertEqual((status, response.getheader("Location")), (302, "/tv/console"))
        again = self.setup_admin()[2]
        self.assertFalse(again["ok"])
        self.assertEqual(len([u for u in self.accounts.users() if u["role"] == "admin"]), 1)

    def test_https_from_the_front_keeps_secure(self):
        _, response, answer = self.setup_admin({"X-Forwarded-Proto": "https", "Host": "tv.example.com"})
        self.assertTrue(answer["ok"])
        self.assertIn("Secure", response.getheader("Set-Cookie"))
        self.assertEqual(self.accounts.meta("device_address"), "", "a tunnel name is not a LAN address")

    def test_loopback_and_public_hosts_are_not_recorded(self):
        for host in ("127.0.0.1:8080", "localhost:8080", "8.8.8.8:8080", "evil.example:8080"):
            self.assertEqual(setup.lan_host(host), "", host)
        for host, expected in (("192.168.1.20:8080", "192.168.1.20"), ("10.0.0.5", "10.0.0.5"),
                               ("my-nas.local:8080", "my-nas.local")):
            self.assertEqual(setup.lan_host(host), expected)

    def test_wifi_page_gets_the_device_address(self):
        _, _, page = self.request("GET", "/tv/wifi")
        self.assertFalse(b"<meta name=\"tv-device-address\"" in page, "nothing to inject before an address is known")
        self.setup_admin({"Host": "192.168.1.20:8080"})
        for path in ("/tv/wifi", "/TV/W"):
            _, _, page = self.request("GET", path)
            self.assertTrue(b'<meta name="tv-device-address" content="192.168.1.20">' in page, path)


class CloudHostUnchangedTests(RelayHarness):
    """No --selfhost: what the owner's cloud host runs."""
    selfhost = False

    def setUp(self):
        super().setUp()
        self.admin = self.accounts.create_user("管理员", "admin")

    def test_cookie_is_always_secure(self):
        status, response, _ = self.request("GET", f"/tv/?k={KEY}", headers={"X-Forwarded-Proto": "http"})
        self.assertEqual(status, 302)
        self.assertIn("Secure;", response.getheader("Set-Cookie"))

    def test_no_setup_page_and_no_import(self):
        self.assertEqual(self.request("GET", "/tv/setup")[0], 404)
        self.assertEqual(self.request("POST", "/tv/api/setup", {"code": CODE})[0], 404)
        self.assertEqual(self.request("GET", "/tv/api/setup/info")[0], 404)
        status, _, _ = self.request("POST", "/tv/api/admin/channels/import", {"text": PLAYLIST},
                                    headers={"X-Key": KEY})
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", "/tv/api/admin/channels/import", {"preset": presets.PRESETS[0]["id"]},
                                    headers={"X-Key": KEY})
        self.assertEqual(status, 404, "no public playlists on the cloud host either")
        channels = self.request("GET", "/tv/api/admin/channels", headers={"X-Key": KEY})[2]
        self.assertFalse(channels["import"])
        self.assertEqual(channels["presets"], [])
        big = {"version": channels["version"], "rows": [{"name": f"台{n}", "url": f"https://example.com/{n:04d}.m3u8"}
                                                        for n in range(200)]}
        status, _, _ = self.request("POST", "/tv/api/admin/channels", big, headers={"X-Key": KEY})
        self.assertEqual(status, 400, "the cloud keeps its 8 kB body limit (nginx in front)")
        self.assertFalse(self.request("GET", "/tv/api/admin/system", headers={"X-Key": KEY})[2]["selfhost"])

    def test_wifi_page_is_the_static_file(self):
        _, response, page = self.request("GET", "/tv/wifi")
        self.assertFalse(b"<meta name=\"tv-device-address\"" in page)
        self.assertEqual(response.getheader("Cache-Control"), "public, max-age=86400")


class PlaylistImportTests(RelayHarness):
    def setUp(self):
        super().setUp()
        self.admin = self.accounts.create_user("管理员", "admin")
        self.cookie = "tvs=" + self.accounts.new_session(self.admin)

    def test_import_lists_valid_entries_only_for_admins(self):
        status, _, answer = self.request("POST", "/tv/api/admin/channels/import", {"text": PLAYLIST},
                                         headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual([e["name"] for e in answer["entries"]], ["家庭台 一", "Two"])
        self.assertEqual(answer["entries"][1]["agent"], "VLC/3")
        self.assertEqual(answer["skipped"], 1, "the address with a space is not offered")
        self.assertIn("channels.import", [row["action"] for row in self.accounts.audit_rows()])
        user = self.accounts.create_user("家人")
        status, _, _ = self.request("POST", "/tv/api/admin/channels/import", {"text": PLAYLIST},
                                    headers={"Cookie": "tvs=" + self.accounts.new_session(user)})
        self.assertEqual(status, 404, "not an admin: as if it did not exist")
        channels = self.request("GET", "/tv/api/admin/channels", headers={"Cookie": self.cookie})[2]
        self.assertTrue(channels["import"])
        self.assertEqual(channels["rows"], [], "a new household starts with an empty table")

    def test_presets_are_listed_for_the_admin_only(self):
        channels = self.request("GET", "/tv/api/admin/channels", headers={"Cookie": self.cookie})[2]
        self.assertEqual([p["id"] for p in channels["presets"]], [p["id"] for p in presets.PRESETS])
        self.assertTrue(all(p["urls"][0]["url"].startswith("https://") for p in channels["presets"]))
        user = self.accounts.create_user("家人")
        member = {"Cookie": "tvs=" + self.accounts.new_session(user)}
        self.assertEqual(self.request("GET", "/tv/api/admin/channels", headers=member)[0], 404)
        fetched = []
        with unittest.mock.patch.object(playlist, "_fetch", lambda url, *_a, **_k: fetched.append(url) or (PLAYLIST, "")):
            status, _, _ = self.request("POST", "/tv/api/admin/channels/import", {"preset": presets.PRESETS[0]["id"]},
                                        headers=member)
            self.assertEqual(status, 404)
            self.assertEqual(fetched, [], "a member's request fetches nothing")
            answer = self.request("POST", "/tv/api/admin/channels/import", {"preset": presets.PRESETS[0]["id"]},
                                  headers={"Cookie": self.cookie})[2]
        self.assertEqual(fetched, [presets.PRESETS[0]["url"]])
        self.assertEqual(answer["total"], 2)
        row = next(r for r in self.accounts.audit_rows() if r["action"] == "channels.import")
        self.assertEqual(row["target_id"], presets.PRESETS[0]["id"])

    def test_cross_site_import_is_refused(self):
        status, _, _ = self.request("POST", "/tv/api/admin/channels/import", {"text": PLAYLIST},
                                    headers={"Cookie": self.cookie, "Origin": "https://evil.example"})
        self.assertNotEqual(status, 200)

    def test_import_marks_what_the_table_already_has(self):
        self.main.write_text("ch000 | 家庭台一 | http://192.168.1.5/other.m3u8\n"
                             "ch001 | Elsewhere | https://example.com/b.m3u8\n", encoding="utf-8")
        answer = self.request("POST", "/tv/api/admin/channels/import", {"text": PLAYLIST},
                              headers={"Cookie": self.cookie})[2]
        self.assertEqual([(e["name"], e["dup"]) for e in answer["entries"]], [("家庭台 一", "name"), ("Two", "url")])
        self.assertEqual([e["kind"] for e in answer["entries"]], ["其他", "国际"])

    def test_a_hundred_imported_channels_save_in_one_go(self):
        text = "#EXTM3U\n" + "".join(f"#EXTINF:-1,卫视{n}\nhttp://192.168.1.5/live/{n:03d}/index.m3u8\n"
                                      for n in range(100))
        entries = self.request("POST", "/tv/api/admin/channels/import", {"text": text},
                               headers={"Cookie": self.cookie})[2]["entries"]
        version = self.request("GET", "/tv/api/admin/channels", headers={"Cookie": self.cookie})[2]["version"]
        body = {"version": version, "rows": [{k: e[k] for k in ("name", "url", "agent")} for e in entries]}
        self.assertGreater(len(json.dumps(body, ensure_ascii=False).encode()), admin.BODY_MAX,
                           "more than the cloud's 8 kB")
        answer = self.request("POST", "/tv/api/admin/channels", body, headers={"Cookie": self.cookie})[2]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["summary"]["total"], 100)

    def test_large_file_upload_fits(self):
        big = "#EXTM3U\n" + "".join(f"#EXTINF:-1,台{n}\nhttp://192.168.1.5/{n}.m3u8\n" for n in range(20000))
        self.assertGreater(len(big), web.MAX_BODY_BYTES * 100)
        answer = self.request("POST", "/tv/api/admin/channels/import", {"text": big},
                              headers={"Cookie": self.cookie})[2]
        self.assertEqual((answer["total"], len(answer["entries"])), (20000, playlist.ENTRIES_MAX))

    def test_imported_rows_save_through_the_normal_checks(self):
        entries = self.request("POST", "/tv/api/admin/channels/import", {"text": PLAYLIST},
                               headers={"Cookie": self.cookie})[2]["entries"]
        version = self.request("GET", "/tv/api/admin/channels", headers={"Cookie": self.cookie})[2]["version"]
        answer = self.request("POST", "/tv/api/admin/channels", {"version": version, "rows": entries},
                              headers={"Cookie": self.cookie})[2]
        self.assertTrue(answer["ok"], answer)
        text = self.main.read_text(encoding="utf-8")
        self.assertIn("ch000 | 家庭台 一 | http://192.168.1.5/a.m3u8", text)
        self.assertIn("ch001 | Two | https://example.com/b.m3u8 | VLC/3", text)


class Response(io.BytesIO):
    """What urlopen returns, enough of it: read(n) until b"" at the end."""


class PlaylistReadTests(unittest.TestCase):
    def test_url_is_fetched_once_with_http_only(self):
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return Response(PLAYLIST.encode())

        self.assertEqual(playlist.read({"url": "https://example.com/list.m3u"}, opener)["total"], 2)
        self.assertEqual(seen, ["https://example.com/list.m3u"])
        for url in ("ftp://example.com/x.m3u", "file:///etc/passwd", "javascript:alert(1)", "http://"):
            answer = playlist.read({"url": url}, opener)
            self.assertFalse(answer["ok"], url)
        self.assertEqual(len(seen), 1, "refused addresses are never fetched")

    def test_errors(self):
        self.assertFalse(playlist.read({})["ok"])
        self.assertFalse(playlist.read({"url": "https://x/a", "text": PLAYLIST})["ok"], "one or the other")
        self.assertIn("没有找到频道", playlist.read({"text": "<html>not a playlist</html>"})["error"])
        self.assertIn("太大", playlist.read({"text": "#" * (playlist.IMPORT_MAX + 1)})["error"])

    def test_preset_is_fetched_by_id_and_mirror(self):
        seen = []

        def opener(request, timeout):
            seen.append(request.full_url)
            return Response(PLAYLIST.encode())

        first = presets.PRESETS[0]
        self.assertEqual(playlist.read({"preset": first["id"]}, opener)["total"], 2)
        with_mirror = next(p for p in presets.PRESETS if p["mirrors"])
        playlist.read({"preset": with_mirror["id"], "mirror": 1}, opener)
        self.assertEqual(seen, [first["url"], with_mirror["mirrors"][0][1]])
        for body in ({"preset": "nope"}, {"preset": first["id"], "mirror": 99}, {"preset": first["id"], "mirror": True},
                     {"preset": first["id"], "url": "https://example.com/x.m3u"}, {"preset": None}):
            self.assertFalse(playlist.read(body, opener)["ok"], body)
        self.assertEqual(len(seen), 2, "an unknown preset or index is never fetched")

    def test_fetch_limits_size_time_and_timeout(self):
        timeouts = []

        def opener_of(data):
            def opener(request, timeout):
                timeouts.append(timeout)
                return Response(data)
            return opener

        text, error = playlist._fetch("https://example.com/big.m3u", opener_of(b"#" * (playlist.IMPORT_MAX + 10)))
        self.assertIsNone(text)
        self.assertIn("太大", error)
        self.assertEqual(timeouts, [playlist.FETCH_TIMEOUT_S])

        class Drip:
            """A server that sends a few bytes at a time, forever."""
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _n):
                return b"#EXTM3U\n"

        clock = iter(range(0, 1000, 10))
        text, error = playlist._fetch("https://example.com/slow.m3u", lambda *_a, **_k: Drip(),
                                      monotonic=lambda: next(clock))
        self.assertIsNone(text)
        self.assertIn("太慢", error)

        def refused(request, timeout):
            raise urllib.error.URLError("timed out")
        self.assertFalse(playlist.read({"url": "https://example.com/x.m3u"}, refused)["ok"])

    def test_mark_duplicates_and_kinds(self):
        entries = [{"name": "CCTV-1", "url": "https://a/1", "agent": ""},
                   {"name": "湖南卫视", "url": "https://x/2.m3u8", "agent": ""},
                   {"name": "cctv 1", "url": "https://a/1b", "agent": ""},
                   {"name": "Hunan Satellite TV", "url": "https://a/3", "agent": ""},
                   {"name": "Hunan Satellite TV", "url": "https://a/3b", "agent": ""},
                   {"name": "某地新闻", "url": "https://a/4", "agent": ""}]
        rows = [{"id": "ch000", "name": "CCTV1", "url": "https://x/1.m3u8", "agent": ""},
                {"id": "ch001", "name": "湖南台", "url": "https://x/2.m3u8", "agent": ""}]
        playlist.mark(entries, rows)
        self.assertEqual([e["dup"] for e in entries], ["name", "url", "name", "", "list", ""])
        self.assertEqual([e["kind"] for e in entries], ["央视", "卫视", "央视", "卫视", "卫视", "其他"])
        self.assertEqual(playlist.name_key("CCTV-1 "), playlist.name_key("cctv_1"))

    def test_name_after_a_comma_inside_quotes(self):
        from server import m3u
        text = ('#EXTINF:-1 http-user-agent="Mozilla/5.0 (KHTML, like Gecko) Chrome/144",CCTV-1 (720p)\n'
                "https://example.com/1.m3u8\n"
                '#EXTINF:-1 tvg-name="broken,CCTV2\nhttps://example.com/2.m3u8\n')
        found = m3u.parse_playlist(text)
        self.assertEqual([c["name"] for c in found], ["CCTV-1 (720p)", "CCTV2"])
        self.assertEqual(found[0]["agent"], "Mozilla/5.0 (KHTML, like Gecko) Chrome/144")

    def test_probe_states(self):
        class Done:
            def __init__(self, out):
                self.stdout = out

        clock = iter([0.0, 1.0, 0.0, 20.0, 0.0, 2.0])
        ok = playlist.probe({"url": "http://x/a"}, "ffmpeg", run=lambda *a, **k: Done(b"\xff" * 500),
                            monotonic=lambda: next(clock))
        slow = playlist.probe({"url": "http://x/a"}, "ffmpeg", run=lambda *a, **k: Done(b"\xff" * 500),
                              monotonic=lambda: next(clock))
        dead = playlist.probe({"url": "http://x/a"}, "ffmpeg", run=lambda *a, **k: Done(b""),
                              monotonic=lambda: next(clock))
        self.assertEqual([ok["state"], slow["state"], dead["state"]], ["ok", "slow", "dead"])

        def missing(*_a, **_k):
            raise FileNotFoundError("ffmpeg")
        self.assertFalse(playlist.probe({"url": "http://x/a"}, "ffmpeg", run=missing)["ok"])
        self.assertFalse(playlist.probe({"url": "rtsp://x/a"}, "ffmpeg")["ok"])

    def test_probe_passes_the_agent_and_one_frame(self):
        calls = []

        class Done:
            stdout = b""

        playlist.probe({"url": "http://x/a", "agent": "VLC/3"}, "/usr/bin/ffmpeg",
                       run=lambda command, **_k: calls.append(command) or Done())
        command = calls[0]
        self.assertEqual(command[0], "/usr/bin/ffmpeg")
        self.assertEqual(command[command.index("-user_agent") + 1], "VLC/3")
        self.assertEqual(command[command.index("-frames:v") + 1], "1")


if __name__ == "__main__":
    unittest.main()
