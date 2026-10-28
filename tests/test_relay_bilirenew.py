"""Bilibili sign-in renewal (server/relay/bililogin.py: the web cookie refresh).

Bilibili is never contacted and no real account is used. The fakes answer with
the shapes documented by bilibili-API-collect (docs/login/cookie_refresh.md):
cookie/info -> {refresh, timestamp}, the correspond page's <div id="1-name">,
cookie/refresh -> Set-Cookie + data.refresh_token, confirm/refresh -> code 0.
The cookie values are sentinels and every log line is checked for them.

The RSA-OAEP encryption is standard-library code, so it is checked against
OpenSSL: a throwaway key is generated, the code encrypts with its modulus and
`openssl pkeyutl` decrypts. Skipped where no OpenSSL with OAEP-SHA-256 exists.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import bililogin

from test_relay_bililogin import CKMD5, JCT, LOGIN_URL, NAV_OUT, NAV_VIP, REFRESH, SECRETS, SESSDATA, Clock, \
    FakeBili, poll_answer

NEW_SESSDATA = "NEWSESSSECRET9f8e%2C1800000000%2Cbeef%2A42"
NEW_JCT = "NEWJCTSECRET00112233445566778899"
NEW_REFRESH = "NEWREFRESHSECRETfedcba9876543210"
REFRESH_CSRF = "b0cc8411ded2f9db2cff2edb3123acac"
ALL_SECRETS = SECRETS + (NEW_SESSDATA, NEW_JCT, NEW_REFRESH, REFRESH_CSRF)
# The JWK modulus as bilibili-API-collect publishes it.
DOC_JWK_N = ("y4HdjgJHBlbaBN04VERG4qNBIFHP6a3GozCl75AihQloSWCXC5HDNgyinEnhaQ_4-gaMud_GF50elYXLlCToR9se9Z8z433U3KjM"
             "-3Yx7ptKkmQNAMggQwAVKgq3zYAoidNEWuxpkY_mAitTSRLnsJW-NCTa0bqBFF6Wm1MxgfE")
PAGE = ('<!DOCTYPE html><html lang="zh-Hans"><head><title>Correspond</title></head><body>\n'
        f'  <div id="1-name">{REFRESH_CSRF}</div>\n  <div id="token-iframe-app"></div></body></html>')


class FakeRenew(FakeBili):
    """FakeBili plus the four cookie-refresh endpoints."""

    def __init__(self):
        super().__init__()
        self.info = {"code": 0, "message": "0", "ttl": 1, "data": {"refresh": False, "timestamp": 1684466082562}}
        self.refresh = {"code": 0, "message": "0", "ttl": 1,
                        "data": {"status": 0, "message": "", "refresh_token": NEW_REFRESH}}
        self.confirm = {"code": 0, "message": "0", "ttl": 1}
        self.page = PAGE
        self.page_calls = []
        self.expires = 1800000000.0

    def __call__(self, url, cookie="", data=None, timeout=None):
        if "/web/cookie/info" in url:
            self.calls.append((url, cookie, data))
            if self.down:
                raise OSError("network unreachable")
            return self.info, {}
        if "/web/cookie/refresh" in url:
            self.calls.append((url, cookie, data))
            if self.down:
                raise OSError("network unreachable")
            cookies = {}
            if self.refresh.get("code") == 0:
                cookies = {"SESSDATA": NEW_SESSDATA, "bili_jct": NEW_JCT, "DedeUserID": "3493110",
                           "DedeUserID__ckMd5": CKMD5, "sid": "newsid99", bililogin.EXPIRES_KEY: self.expires}
            return self.refresh, cookies
        if "/web/confirm/refresh" in url:
            self.calls.append((url, cookie, data))
            if self.down:
                raise OSError("network unreachable")
            return self.confirm, {}
        return super().__call__(url, cookie, data, timeout)

    def fetch_page(self, url, cookie=""):
        self.page_calls.append((url, cookie))
        if self.down:
            raise OSError("network unreachable")
        return self.page

    def urls(self, part):
        return [call for call in self.calls if part in call[0]]


class Fixed:
    """A stand-in for os.urandom so a correspondPath can be compared."""

    def __call__(self, n):
        return b"\x5a" * n


class OaepTests(unittest.TestCase):
    def test_key_is_the_documented_one(self):
        raw = base64.urlsafe_b64decode(DOC_JWK_N + "=" * (-len(DOC_JWK_N) % 4))
        self.assertEqual(int.from_bytes(raw, "big"), bililogin.RSA_N)
        self.assertEqual(bililogin.RSA_N.bit_length(), 1024)
        self.assertEqual(bililogin.RSA_E, 65537)

    def test_correspond_path_shape(self):
        first = bililogin.correspond_path(1684466082562)
        second = bililogin.correspond_path(1684466082562)
        self.assertRegex(first, r"^[0-9a-f]{256}$", "128 bytes, lower-case Base16 as the docs say")
        self.assertNotEqual(first, second, "OAEP is randomised")
        self.assertEqual(bililogin.correspond_path(1, Fixed()), bililogin.correspond_path(1, Fixed()))
        with self.assertRaises(ValueError):
            bililogin.oaep_encrypt(b"x" * 63)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is not installed")
    def test_openssl_decrypts_what_the_code_encrypts(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "test.key"
            made = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:1024",
                                   "-out", str(key)], capture_output=True, timeout=60)
            if made.returncode != 0:
                self.skipTest("openssl cannot generate an RSA key here")
            modulus = subprocess.run(["openssl", "rsa", "-in", str(key), "-noout", "-modulus"],
                                     capture_output=True, text=True, encoding="ascii", timeout=60).stdout
            n = int(modulus.strip().split("=", 1)[1], 16)
            for message in (b"refresh_1684466082562", b"", b"refresh_" + b"9" * 54):
                sealed = Path(tmp) / "sealed"
                sealed.write_bytes(bililogin.oaep_encrypt(message, n=n))
                opened = subprocess.run(["openssl", "pkeyutl", "-decrypt", "-inkey", str(key), "-in", str(sealed),
                                         "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
                                         "-pkeyopt", "rsa_mgf1_md:sha256"], capture_output=True, timeout=60)
                if opened.returncode != 0 and b"parameter" in opened.stderr.lower():
                    self.skipTest("this openssl has no OAEP-SHA-256")
                self.assertEqual(opened.returncode, 0, opened.stderr)
                self.assertEqual(opened.stdout, message)

    def test_set_cookie_expiry(self):
        self.assertEqual(bililogin._cookie_expiry(" Path=/; Domain=bilibili.com; Expires=Wed, 15 Nov 2023 07:34:10 GMT;"
                                                  " HttpOnly; Secure", 0), 1700033650.0)
        self.assertEqual(bililogin._cookie_expiry(" Max-Age=100; Path=/", 1000.0), 1100.0)
        self.assertIsNone(bililogin._cookie_expiry(" Path=/; Expires=nonsense", 0))
        self.assertEqual(bililogin.expires_from_url(LOGIN_URL), 1790000000.0)
        self.assertIsNone(bililogin.expires_from_url("https://example.com/?Expires=x"))


class RenewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "secrets" / "bilibili"
        self.fake = FakeRenew()
        self.mono = Clock()
        self.wall = Clock()
        self.wall.t = 1760000000.0
        self.logs = []
        self.accounts = bililogin.BiliAccounts(self.root, fetch=self.fake, clock=self.wall, monotonic=self.mono,
                                               sleep=lambda _s: None, log=self.logs.append,
                                               fetch_page=self.fake.fetch_page)

    def tearDown(self):
        for line in self.logs:
            for secret in ALL_SECRETS:
                self.assertNotIn(secret, line)
        self.tmp.cleanup()

    def login(self, user=1):
        self.fake.polls = [poll_answer(0, LOGIN_URL)]
        self.assertTrue(self.accounts.start(user)["ok"])
        self.mono.t += 3
        self.assertEqual(self.accounts.poll(user)["state"], "done")
        self.fake.calls.clear()

    def record(self, user=1):
        return self.accounts.store.load(user)

    def test_login_keeps_what_renewal_needs(self):
        self.login()
        record = self.record()
        self.assertEqual(record["refresh_token"], REFRESH)
        self.assertEqual(record["expires_at"], 1790000000.0, "Expires from the QR answer's address")
        self.assertEqual(record["renew_at"], self.wall.t + bililogin.RENEW_EVERY_S)

    def test_no_refresh_needed_only_asks(self):
        self.login()
        self.assertEqual(self.accounts.renew(1), "fresh")
        (url, cookie, data), = self.fake.calls
        self.assertIn("/x/passport-login/web/cookie/info?csrf=" + JCT, url)
        self.assertIn("SESSDATA=" + SESSDATA, cookie)
        self.assertIsNone(data)
        self.assertEqual(self.fake.page_calls, [])
        self.assertEqual(self.record()["renew_at"], self.wall.t + bililogin.RENEW_EVERY_S)
        self.assertEqual(self.record()["cookies"]["SESSDATA"], SESSDATA)

    def test_refresh_follows_the_documented_steps(self):
        self.login()
        self.fake.info["data"]["refresh"] = True
        self.wall.t += 5
        self.assertEqual(self.accounts.renew(1), "renewed")
        # The correspond page: a fresh 128-byte RSA-OAEP ciphertext of refresh_<timestamp from cookie/info>.
        (page_url, page_cookie), = self.fake.page_calls
        self.assertRegex(page_url, r"^https://www\.bilibili\.com/correspond/1/[0-9a-f]{256}$")
        self.assertIn("SESSDATA=" + SESSDATA, page_cookie)
        (_, cookie, form), = self.fake.urls("/web/cookie/refresh")
        self.assertIn("SESSDATA=" + SESSDATA, cookie, "refresh is asked with the old cookies")
        self.assertEqual(form, {"csrf": JCT, "refresh_csrf": REFRESH_CSRF, "source": "main_web",
                                "refresh_token": REFRESH})
        (_, cookie, form), = self.fake.urls("/web/confirm/refresh")
        self.assertIn("SESSDATA=" + NEW_SESSDATA, cookie, "confirm uses the new cookies")
        self.assertEqual(form, {"csrf": NEW_JCT, "refresh_token": REFRESH}, "…and the OLD refresh_token")
        order = [url.split("/web/")[1].split("?")[0] for url, _, _ in self.fake.calls]
        self.assertEqual(order, ["cookie/info", "cookie/refresh", "confirm/refresh"])
        record = self.record()
        self.assertEqual(record["cookies"]["SESSDATA"], NEW_SESSDATA)
        self.assertEqual(record["cookies"]["bili_jct"], NEW_JCT)
        self.assertEqual(record["cookies"]["sid"], "newsid99")
        self.assertEqual(record["cookies"]["buvid3"], "B3-1111-2222", "cookies the refresh does not set are kept")
        self.assertNotIn(bililogin.EXPIRES_KEY, record["cookies"])
        self.assertEqual(record["refresh_token"], NEW_REFRESH)
        self.assertNotIn("confirm_token", record)
        self.assertEqual(record["expires_at"], 1800000000.0)
        self.assertEqual(record["renewed_at"], self.wall.t)
        self.assertFalse(record["invalid"])
        # Stored exactly like the QR login: one 0600 file in a 0700 folder, nothing else left behind.
        path = self.root / "1.json"
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertIn("SESSDATA=" + NEW_SESSDATA, self.accounts.login_for(1).header())
        self.assertIn("已自动续期", self.logs[-1])

    def test_nearly_expired_refreshes_without_being_told(self):
        self.login()
        self.wall.t = 1790000000.0 - bililogin.RENEW_BEFORE_S + 60
        self.assertEqual(self.accounts.renew(1), "renewed")

    def test_failures_keep_the_old_login(self):
        self.login()
        self.fake.info["data"]["refresh"] = True
        cases = {
            "refused": lambda: self.fake.refresh.update(code=86095, message="refresh_csrf 错误", data=None),
            "page": lambda: setattr(self.fake, "page", "<html>404</html>"),
            "down": lambda: setattr(self.fake, "down", True),
            "info": lambda: self.fake.info.update(code=-412),
        }
        for name, breaks in cases.items():
            with self.subTest(name):
                saved = dict(self.fake.refresh, data=dict(self.fake.refresh["data"])), self.fake.page
                breaks()
                self.assertEqual(self.accounts.renew(1), "failed")
                record = self.record()
                self.assertEqual(record["cookies"]["SESSDATA"], SESSDATA)
                self.assertEqual(record["refresh_token"], REFRESH)
                self.assertFalse(record["invalid"], "a failed renewal is not an expired login")
                self.assertEqual(record["renew_at"], self.wall.t + bililogin.RENEW_RETRY_S)
                self.assertIsNotNone(self.accounts.login_for(1))
                self.assertIn("续期没成", self.logs[-1])
                self.fake.refresh, self.fake.page = saved
                self.fake.down = False
                self.fake.info["code"] = 0
        self.assertEqual(self.fake.urls("/web/confirm/refresh"), [])

    def test_signed_out_is_marked_only_when_nav_agrees(self):
        self.login()
        self.fake.info = {"code": -101, "message": "账号未登录", "ttl": 1}
        self.assertEqual(self.accounts.renew(1), "failed", "nav still says signed in: keep it")
        self.assertFalse(self.record()["invalid"])
        self.fake.nav = NAV_OUT
        self.assertEqual(self.accounts.renew(1), "invalid")
        self.assertTrue(self.record()["invalid"])
        self.assertEqual(self.accounts.account(1)["message"], "登录已失效，请重新扫码")
        self.assertEqual(self.accounts.renew(1), "skipped")

    def test_unconfirmed_refresh_is_confirmed_next_time(self):
        self.login()
        self.fake.info["data"]["refresh"] = True
        self.fake.confirm = {"code": -111, "message": "csrf 校验失败"}
        self.assertEqual(self.accounts.renew(1), "renewed")
        self.assertEqual(self.record()["confirm_token"], REFRESH)
        self.fake.confirm = {"code": 0}
        self.fake.info["data"]["refresh"] = False
        self.fake.calls.clear()
        self.assertEqual(self.accounts.renew(1), "fresh")
        (_, _, form), = self.fake.urls("/web/confirm/refresh")
        self.assertEqual(form, {"csrf": NEW_JCT, "refresh_token": REFRESH})
        self.assertNotIn("confirm_token", self.record())

    def test_a_rejection_of_the_old_cookies_after_renewal_is_ignored(self):
        self.login()
        before = self.accounts.login_for(1)
        self.fake.info["data"]["refresh"] = True
        self.assertEqual(self.accounts.renew(1), "renewed")
        before.rejected()       # a play that started with the old cookies, refused after confirm
        self.assertFalse(self.record()["invalid"])
        self.accounts.login_for(1).rejected()
        self.assertTrue(self.record()["invalid"], "a rejection of the current cookies still counts")

    def test_logout_during_refresh_drops_the_new_cookies(self):
        self.login()
        self.fake.info["data"]["refresh"] = True
        page = self.fake.fetch_page

        def logout_meanwhile(url, cookie=""):
            self.accounts.forget(1)
            return page(url, cookie)

        self.accounts.fetch_page = logout_meanwhile
        self.assertEqual(self.accounts.renew(1), "skipped")
        self.assertFalse((self.root / "1.json").exists())
        self.assertEqual(self.fake.urls("/web/confirm/refresh"), [])

    def test_old_login_without_refresh_token(self):
        self.login()
        record = self.record()
        record["refresh_token"] = ""
        self.accounts._save(1, record)
        self.fake.info["data"]["refresh"] = True
        self.assertEqual(self.accounts.renew(1), "failed")
        self.assertIn("没有存 refresh_token", self.logs[-1])
        self.assertEqual(self.fake.page_calls, [])

    def test_tick_runs_daily_in_the_background(self):
        self.login(1)
        self.login(2)
        self.accounts.tick()
        self.assertEqual(self.fake.calls, [], "nothing is due right after signing in")
        self.wall.t += bililogin.RENEW_EVERY_S
        self.accounts.tick()
        self.assertEqual(self.fake.calls, [], "the scan itself is limited to every RENEW_SCAN_S")
        self.mono.t += bililogin.RENEW_SCAN_S
        self.accounts.tick()
        for thread in threading.enumerate():
            if thread.name == "bili-renew":
                thread.join(5)
        self.assertEqual(len(self.fake.urls("/web/cookie/info")), 2, "one check per signed-in user")
        self.assertFalse(self.accounts._renewing)
        self.mono.t += bililogin.RENEW_SCAN_S
        self.accounts.tick()
        self.assertEqual(len(self.fake.urls("/web/cookie/info")), 2, "both are fresh for another day")


if __name__ == "__main__":
    unittest.main()
