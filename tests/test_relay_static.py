"""Compressed web assets and the on-demand admin script (server/relay/static.py, console.py, web.py).

What is checked: Accept-Encoding negotiation (gzip, x-gzip, q-values, "*",
q=0 refusals); text assets are compressed once at startup (deterministic,
only when it saves bytes) and images are never compressed; a request that
accepts gzip gets Content-Encoding: gzip, Vary: Accept-Encoding and an ETag
distinct from the plain variant's, and the body unpacks to the file itself;
a request without it (or with Range) gets the plain bytes with the plain
ETag and still Vary; If-None-Match answers 304 per variant without a body,
and the other variant's ETag does not match; the console page, the login
page and the phone remote are compressed the same way and keep their own
headers (CSP, no-store); and the console page no longer loads the admin
script -- console.js fetches it (versioned URL in data-admin) only when an
admin opens the admin section.
"""

from __future__ import annotations

import gzip
import http.client
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.relay import console, setup, static, web  # noqa: E402
from server.tenancy.accounts import Accounts  # noqa: E402

from test_relay_web import KEY, FakeClient, FakeYouTube  # noqa: E402

ASSETS_DIR = Path(__file__).resolve().parents[1] / "server" / "relay" / "assets"


class NegotiationTests(unittest.TestCase):
    def test_accept_encoding(self):
        for header in ("gzip", "gzip, deflate, br", "br;q=1.0, gzip;q=0.8", "x-gzip", "*", "deflate, *;q=0.5",
                       "GZIP", "gzip;q=0.001"):
            self.assertTrue(static.accepts_gzip(header), header)
        for header in (None, "", "identity", "br, deflate", "gzip;q=0", "gzip;q=0.0, *", "*;q=0",
                       "*, gzip;q=0", "gzip;q=bad"):
            self.assertFalse(static.accepts_gzip(header), header)

    def test_pack(self):
        js = b"function f() { return 1; }\n" * 400
        item = static.pack("text/javascript; charset=utf-8", js)
        self.assertEqual(gzip.decompress(item.gz), js)
        self.assertEqual(item, static.pack("text/javascript; charset=utf-8", js), "same bytes every start")
        self.assertNotEqual(item.etag, item.gz_etag)
        self.assertTrue(item.etag.startswith('"') and item.gz_etag.endswith('-gz"'))
        self.assertIsNone(static.pack("image/png", js).gz, "images are never compressed")
        self.assertIsNone(static.pack("text/css", b"a{}").gz, "too small to bother")
        self.assertIsNone(static.pack("text/plain", os.urandom(4096)).gz, "would not shrink")
        self.assertIsNotNone(static.pack("image/svg+xml", b"<svg><path d='M0 0'/></svg>" * 100).gz)

    def test_startup_tables(self):
        self.assertEqual(set(web.PACKED), set(web.ASSETS))
        for path in ("/console.js", "/console.css", "/console-admin.js", "/scan.js", "/jsqr.js", "/wifi"):
            self.assertIsNotNone(web.PACKED[path].gz, path)
            self.assertLess(len(web.PACKED[path].gz), len(web.PACKED[path].data) // 2, path)
        for path in ("/icon-512.png", "/favicon.ico"):
            self.assertIsNone(web.PACKED[path].gz, path)


class ServedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accounts = Accounts(root / "tv.db")
        self.accounts.create_user("管理员", "admin")
        alice = self.accounts.create_user("Alice")
        self.cookie = "tvs=" + self.accounts.new_session(alice, "pc")
        main = root / "channels.txt"
        main.write_text("ch000 | CCTV1 | https://x/1.m3u8\n", encoding="utf-8")
        self.relay = web.Relay(None, FakeClient(), None, "/opt/ff", KEY, youtube=FakeYouTube(), main_channels=main,
                               accounts=self.accounts, devices_dir=root / "devices", log=lambda _m: None,
                               selfhost=setup.SelfHost())
        streams, self.manage = web.make_servers(self.relay, 0, 0)
        streams.server_close()
        threading.Thread(target=self.manage.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        self.manage.shutdown()
        self.manage.server_close()
        if self.relay.watch is not None:
            self.relay.watch.stop_all()
        self.accounts.close()
        self.tmp.cleanup()

    def get(self, path, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=20)
        # http.client sends "Accept-Encoding: identity" unless told otherwise; the tests set it explicitly.
        connection.putrequest("GET", path, skip_accept_encoding=True)
        for name, value in headers.items():
            connection.putheader(name.replace("_", "-"), value)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        result = response.status, {k.lower(): v for k, v in response.getheaders()}, body
        connection.close()
        return result

    def test_script_gzip_and_plain(self):
        plain = (ASSETS_DIR / "console.js").read_bytes()
        status, headers, body = self.get("/tv/console.js", Accept_Encoding="gzip, deflate, br")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertEqual(headers["vary"], "Accept-Encoding")
        self.assertEqual(int(headers["content-length"]), len(body))
        self.assertLess(len(body), len(plain) // 2)
        self.assertEqual(gzip.decompress(body), plain)
        self.assertTrue(headers["content-type"].startswith("text/javascript"))
        self.assertEqual(headers["cache-control"], "public, max-age=86400")
        gz_tag = headers["etag"]

        status, headers, body = self.get("/tv/console.js")
        self.assertEqual(status, 200)
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(headers["vary"], "Accept-Encoding")
        self.assertEqual(body, plain)
        self.assertNotEqual(headers["etag"], gz_tag, "each variant has its own ETag")

        status, headers, body = self.get("/tv/console.js", Accept_Encoding="gzip;q=0, br")
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(body, plain)

    def test_conditional_requests_per_variant(self):
        _, headers, _ = self.get("/tv/console.css", Accept_Encoding="gzip")
        gz_tag = headers["etag"]
        _, headers, _ = self.get("/tv/console.css")
        plain_tag = headers["etag"]
        status, headers, body = self.get("/tv/console.css", Accept_Encoding="gzip", If_None_Match=gz_tag)
        self.assertEqual((status, body), (304, b""))
        self.assertEqual((headers["etag"], headers["vary"]), (gz_tag, "Accept-Encoding"))
        self.assertEqual(headers["cache-control"], "public, max-age=86400")
        self.assertEqual(self.get("/tv/console.css", If_None_Match=plain_tag)[0], 304)
        self.assertEqual(self.get("/tv/console.css", If_None_Match="W/" + plain_tag)[0], 304, "weak comparison")
        # A cached gzip copy does not validate a plain request, nor the other way round.
        status, headers, body = self.get("/tv/console.css", If_None_Match=gz_tag)
        self.assertEqual(status, 200)
        self.assertEqual(body, (ASSETS_DIR / "console.css").read_bytes())
        self.assertEqual(self.get("/tv/console.css", Accept_Encoding="gzip", If_None_Match=plain_tag)[0], 200)

    def test_range_requests_are_never_compressed(self):
        status, headers, body = self.get("/tv/console.js", Accept_Encoding="gzip", Range="bytes=0-99")
        self.assertNotIn("content-encoding", headers)
        self.assertEqual(body, (ASSETS_DIR / "console.js").read_bytes())

    def test_images_are_sent_as_they_are(self):
        status, headers, body = self.get("/tv/icon-512.png", Accept_Encoding="gzip")
        self.assertEqual(status, 200)
        self.assertNotIn("content-encoding", headers)
        self.assertNotIn("vary", headers)
        self.assertEqual(body, (ASSETS_DIR / "icon-512.png").read_bytes())
        self.assertIn("etag", headers)

    def test_pages_keep_their_headers(self):
        status, headers, body = self.get("/tv/console", Accept_Encoding="gzip", Cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-encoding"], "gzip")
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(gzip.decompress(body), console.CONSOLE_PAGE)
        status, headers, body = self.get("/tv/login", Accept_Encoding="gzip")
        self.assertEqual((status, headers["content-encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(body), console.LOGIN_PAGE)
        status, headers, body = self.get("/tv/", Accept_Encoding="gzip", Cookie=self.cookie)
        self.assertEqual((status, headers["content-encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(body).decode("utf-8"), web.PAGE)

    def test_admin_script_is_not_in_the_base_page(self):
        _, _, body = self.get("/tv/console", Cookie=self.cookie)
        page = body.decode("utf-8")
        self.assertNotIn('src="console-admin.js', page)
        self.assertNotIn('<link rel="preload"', page)
        self.assertRegex(page, r'<script src="console\.js\?v=[0-9a-f]{12}" data-admin="console-admin\.js\?v=[0-9a-f]{12}">')
        script = (ASSETS_DIR / "console.js").read_text(encoding="utf-8")
        # Fetched on demand, as a classic script sharing console.js's globals, only from the admin route
        # (non-admins are sent home before it, showRoute).
        self.assertIn('document.currentScript.dataset.admin', script)
        self.assertIn('document.createElement("script")', script)
        self.assertIn('else if (name === "admin") openAdmin();', script)
        self.assertLess(script.index('if (name === "admin" && !isAdmin()) name = "home";'),
                        script.index('else if (name === "admin") openAdmin();'))
        # Still served to whoever asks (the section itself is guarded by /api/admin/*).
        self.assertEqual(self.get("/tv/console-admin.js")[0], 200)


if __name__ == "__main__":
    unittest.main()
