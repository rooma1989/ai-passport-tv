"""Bilibili account login on the household server / PC client (server/relay/bililogin.py).

Bilibili is never contacted: the answers below have the shapes of the web APIs
(passport QR login, nav, relation/followings, player/wbi/playurl, pgc playurl)
as documented by bilibili-API-collect and as the web player receives them. The
cookie values are sentinels; every test that can see an output (log lines, the
audit log, HTTP answers, the diagnostics bundle) checks that none of them leaks.
"""

from __future__ import annotations

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import bilibili, bililogin, setup, web
from server.relay.bilibili import Bilibili, MemberOnly
from server.relay.state import Video
from server.tenancy.accounts import Accounts

from test_relay_web import KEY, FakeClient

SESSDATA = "SESSSECRET0a1b%2C1790000000%2Cc0ffee%2A42"
JCT = "JCTSECRET5f4e3d2c1b0a99887766"
CKMD5 = "CKMD5SECRETaabbccdd"
REFRESH = "REFRESHSECRET0123456789abcdef"
QRKEY = "QRKEYSECRET0000000000000000000a"
SECRETS = (SESSDATA, JCT, CKMD5, REFRESH, QRKEY, "SESSSECRET0a1b")
MID = "3493110"
LOGIN_URL = ("https://passport.biligame.com/x/passport-login/web/crossDomain?DedeUserID=" + MID +
             "&DedeUserID__ckMd5=" + CKMD5 + "&Expires=1790000000&SESSDATA=" + SESSDATA +
             "&bili_jct=" + JCT + "&gourl=https%3A%2F%2Fwww.bilibili.com")
WBI = {"img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
       "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}
NAV_VIP = {"code": 0, "message": "0", "data": {
    "isLogin": True, "face": "https://i1.hdslb.com/bfs/face/abc123.jpg", "level_info": {"current_level": 5},
    "mid": int(MID), "uname": "小鼠看番", "vipStatus": 1, "vipType": 2, "vip_label": {"text": "年度大会员"},
    "wbi_img": WBI}}
NAV_PLAIN = {"code": 0, "data": dict(NAV_VIP["data"], vipStatus=0, vipType=0, vip_label={"text": ""}, uname="普通人")}
NAV_OUT = {"code": -101, "message": "账号未登录", "data": {"isLogin": False, "wbi_img": WBI}}


def poll_answer(code, url=""):
    return {"code": 0, "message": "0", "data": {"url": url, "refresh_token": REFRESH if code == 0 else "",
                                                "timestamp": 1760000000000 if code == 0 else 0, "code": code,
                                                "message": {0: "", 86101: "未扫码", 86090: "二维码已扫码未确认",
                                                            86038: "二维码已失效"}[code]}}


def followings_page(start, count, total):
    return {"code": 0, "data": {"re_version": 0, "total": total, "list": [
        {"mid": 1000 + n, "attribute": 2, "uname": f"UP 主{n}", "face": f"https://i0.hdslb.com/bfs/face/f{n}.jpg",
         "sign": f"签名{n}", "official_verify": {"type": -1, "desc": ""}} for n in range(start, start + count)]}}


class FakeBili:
    """bililogin.request's stand-in: (answer, set-cookies), records every call."""

    def __init__(self):
        self.polls = [poll_answer(86101)]
        self.nav = NAV_VIP
        self.follow_total = 3
        self.calls = []
        self.down = False

    def __call__(self, url, cookie="", data=None, timeout=None):
        self.calls.append((url, cookie, data))
        if self.down:
            raise OSError("network unreachable")
        if "/qrcode/generate" in url:
            return {"code": 0, "data": {"url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?"
                                               "navhide=1&callback=close&qrcode_key=" + QRKEY + "&from=",
                                        "qrcode_key": QRKEY}}, {}
        if "/qrcode/poll" in url:
            answer = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            cookies = {"SESSDATA": SESSDATA, "bili_jct": JCT, "DedeUserID": MID, "DedeUserID__ckMd5": CKMD5,
                       "sid": "sid12345"} if answer["data"]["code"] == 0 else {}
            return answer, cookies
        if "/finger/spi" in url:
            return {"code": 0, "data": {"b_3": "B3-1111-2222", "b_4": "B4-3333"}}, {}
        if "/web-interface/nav" in url:
            return self.nav, {}
        if "/relation/followings" in url:
            page = int(url.split("pn=")[1].split("&")[0])
            start = (page - 1) * bililogin.FOLLOW_PAGE_SIZE
            count = max(0, min(bililogin.FOLLOW_PAGE_SIZE, self.follow_total - start))
            return followings_page(start, count, self.follow_total), {}
        if "/login/exit/v2" in url:
            return {"code": 0, "status": True, "data": {"redirectUrl": "https://www.bilibili.com"}}, {}
        raise AssertionError("unexpected " + url)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class WbiTests(unittest.TestCase):
    def test_documented_vector(self):
        # bilibili-API-collect docs/misc/sign/wbi.md: the example keys and parameters.
        self.assertEqual(bilibili.mixin_key("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"),
                         "ea1db124af3c7062474693fa704f4ff8")
        signed = bilibili.wbi_sign({"foo": "114", "bar": "514", "zab": 1919810},
                                   "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45", 1702204169)
        self.assertEqual(signed, {"bar": "514", "foo": "114", "wts": "1702204169", "zab": "1919810",
                                  "w_rid": "8f6f2b5b3d485fe1886cec6a0be8c5d4"})

    def test_values_drop_reserved_characters_and_keys_come_from_nav(self):
        a = bilibili.wbi_sign({"q": "a(b)!*'c"}, "7cd084941338484aae1ad9425b84077c",
                              "4932caff0ff746eab6f01bf08b70ac45", 1)
        b = bilibili.wbi_sign({"q": "abc"}, "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45", 1)
        self.assertEqual(a, b)
        self.assertEqual(bilibili.wbi_keys(NAV_OUT["data"]),
                         ("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"))
        self.assertIsNone(bilibili.wbi_keys({"wbi_img": {"img_url": "x", "sub_url": "y"}}))


class RecordingFetch:
    """bilibili.Bilibili's fetch_json stand-in."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, cookie, timeout=None):
        self.calls.append((url, cookie))
        for prefix, answer in self.answers.items():
            if prefix in url:
                if isinstance(answer, list):
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(url)


SPI = {"code": 0, "data": {"b_3": "ANON-B3"}}
VIEW = {"code": 0, "data": {"cid": 777, "title": "t", "duration": 60}}
H5 = {"code": 0, "data": {"quality": 64, "durl": [{"url": "https://upos.example/720.mp4"}]}}
FLV = {"code": 0, "data": {"quality": 80, "format": "flv", "durl": [{"order": 1, "url": "https://upos.example/1080.flv"}]}}
FLV_SPLIT = {"code": 0, "data": {"quality": 80, "durl": [{"url": "https://upos.example/a.flv"},
                                                         {"url": "https://upos.example/b.flv"}]}}
PGC_PREVIEW = {"code": 0, "result": {"is_preview": 1, "durl": [{"url": "https://upos.example/preview.mp4"}]}}
PGC_FULL = {"code": 0, "result": {"is_preview": 0, "quality": 112, "durl": [{"url": "https://upos.example/ep.flv"}]}}
PGC_FULL_MP4 = {"code": 0, "result": {"is_preview": 0, "quality": 64, "durl": [{"url": "https://upos.example/ep.mp4"}]}}
SEASON = {"code": 0, "result": {"season_id": 2307, "season_title": "海尔兄弟", "episodes": [
    {"id": 1, "cid": 11, "title": "1", "show_title": "第1话", "status": 2, "badge": "", "duration": 60000},
    {"id": 2, "cid": 12, "title": "2", "show_title": "第2话", "status": 13, "badge": "会员", "duration": 60000},
    {"id": 3, "cid": 13, "title": "3", "show_title": "第3话", "status": 6, "badge": "付费", "duration": 60000}]}}


def login(vip=False, rejected=None):
    cookies = {"SESSDATA": SESSDATA, "bili_jct": JCT, "DedeUserID": MID, "buvid3": "B3-1111-2222"}
    return bililogin.Login(7, cookies, vip=vip, on_rejected=rejected)


class QualityTests(unittest.TestCase):
    def client(self, answers):
        fetch = RecordingFetch(answers)
        return Bilibili(fetch_json=fetch, quality=64, clock=lambda: 1702204169), fetch

    def test_anonymous_keeps_the_unsigned_html5_request(self):
        client, fetch = self.client({"/finger/spi": SPI, "/web-interface/view": VIEW, "/x/player/playurl": H5})
        self.assertEqual(client.media_url("BV1aZpw6DEKJ"), "https://upos.example/720.mp4")
        url, cookie = fetch.calls[-1]
        self.assertIn("/x/player/playurl?", url)
        self.assertIn("qn=64", url)
        self.assertIn("platform=html5", url)
        self.assertNotIn("w_rid", url)
        self.assertEqual(cookie, "buvid3=ANON-B3")

    def test_tv_with_a_login_stays_at_720p_with_cookies(self):
        client, fetch = self.client({"/finger/spi": SPI, "/web-interface/view": VIEW, "/x/player/playurl": H5})
        self.assertEqual(client.media_url("BV1aZpw6DEKJ", login=login()), "https://upos.example/720.mp4")
        url, cookie = fetch.calls[-1]
        self.assertIn("qn=64", url)
        self.assertIn("SESSDATA=" + SESSDATA, cookie)
        self.assertFalse(any("wbi" in u for u, _ in fetch.calls))

    def test_pc_with_a_login_signs_and_asks_for_1080p_flv(self):
        client, fetch = self.client({"/finger/spi": SPI, "/web-interface/view": VIEW,
                                     "/web-interface/nav": NAV_VIP, "/x/player/wbi/playurl": FLV})
        url = client.media_url("BV1aZpw6DEKJ", login=login(), quality=bilibili.PC_QUALITY)
        self.assertEqual(url, "https://upos.example/1080.flv")
        played, cookie = fetch.calls[-1]
        self.assertIn("/x/player/wbi/playurl?", played)
        for part in ("qn=80", "fnval=0", "wts=1702204169", "w_rid="):
            self.assertIn(part, played)
        self.assertIn("bili_jct=" + JCT, cookie)
        # The signature covers exactly the query that was sent.
        query = played.split("?", 1)[1]
        unsigned, w_rid = query.rsplit("&w_rid=", 1)
        import hashlib
        self.assertEqual(hashlib.md5((unsigned + "ea1db124af3c7062474693fa704f4ff8").encode()).hexdigest(), w_rid)

    def test_split_flv_falls_back_to_a_single_mp4(self):
        client, fetch = self.client({"/finger/spi": SPI, "/web-interface/view": VIEW, "/web-interface/nav": NAV_VIP,
                                     "/x/player/wbi/playurl": FLV_SPLIT, "/x/player/playurl": H5})
        self.assertEqual(client.media_url("BV1aZpw6DEKJ", login=login(), quality=80), "https://upos.example/720.mp4")
        self.assertIn("SESSDATA", fetch.calls[-1][1], "still with the login")

    def test_rejected_cookies_mark_the_login_and_fall_back_to_anonymous(self):
        seen = []
        client, fetch = self.client({"/finger/spi": SPI, "/web-interface/view": VIEW, "/web-interface/nav": NAV_OUT,
                                     "/x/player/wbi/playurl": {"code": -101, "message": "账号未登录"},
                                     "/x/player/playurl": H5})
        url = client.media_url("BV1aZpw6DEKJ", login=login(rejected=lambda: seen.append(1)), quality=80)
        self.assertEqual(url, "https://upos.example/720.mp4")
        self.assertEqual(seen, [1])
        self.assertEqual(fetch.calls[-1][1], "buvid3=ANON-B3", "the fallback carries no login cookie")

    def test_network_failure_with_a_login_still_tries_anonymously_once(self):
        client, fetch = self.client({"/finger/spi": SPI, "/web-interface/view": VIEW,
                                     "/web-interface/nav": OSError("down"), "/x/player/playurl": H5})
        self.assertEqual(client.media_url("BV1aZpw6DEKJ", login=login(), quality=80), "https://upos.example/720.mp4")
        self.assertLessEqual(sum("playurl" in u for u, _ in fetch.calls), 2)

    def test_member_episode_preview_versus_vip(self):
        client, fetch = self.client({"/finger/spi": SPI, "/pgc/player/web/playurl": PGC_PREVIEW})
        with self.assertRaises(MemberOnly) as caught:
            client.media_url("ep2/12")
        self.assertIn("需要大会员", str(caught.exception))
        self.assertTrue(caught.exception.permanent)
        client, fetch = self.client({"/pgc/player/web/playurl": PGC_FULL})
        self.assertEqual(client.media_url("ep2/12", login=login(vip=True), quality=112), "https://upos.example/ep.flv")
        self.assertIn("qn=112", fetch.calls[-1][0])
        self.assertIn("SESSDATA", fetch.calls[-1][1])
        client, fetch = self.client({"/pgc/player/web/playurl": PGC_FULL_MP4})
        self.assertEqual(client.media_url("ep2/12", login=login(vip=True)), "https://upos.example/ep.mp4")
        self.assertIn("qn=64", fetch.calls[-1][0], "the TV keeps 720p")

    def test_vip_series_keeps_member_episodes_only_for_vip(self):
        client, _ = self.client({"/finger/spi": SPI, "/pgc/view/web/season": SEASON})
        self.assertEqual([v.bvid for v in client.series("ss2307")[1]], ["ep1/11"])
        self.assertEqual([v.bvid for v in client.series("ss2307", login=login(vip=False))[1]], ["ep1/11"])
        self.assertEqual([v.bvid for v in client.series("ss2307", login=login(vip=True))[1]], ["ep1/11", "ep2/12"],
                         "paid (status 6) episodes stay out even for VIP")

    def test_login_repr_has_no_cookies(self):
        text = repr(login()) + str(login())
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class StoreAndFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "secrets" / "bilibili"
        self.fake = FakeBili()
        self.mono = Clock()
        self.logs = []
        self.accounts = bililogin.BiliAccounts(self.root, fetch=self.fake, monotonic=self.mono, sleep=lambda _s: None,
                                               log=self.logs.append)

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, user=1):
        self.fake.polls = [poll_answer(0, LOGIN_URL)]
        self.assertTrue(self.accounts.start(user)["ok"])
        self.mono.t += 3
        return self.accounts.poll(user)

    def test_qr_states_and_poll_rate(self):
        self.fake.polls = [poll_answer(86101), poll_answer(86090), poll_answer(0, LOGIN_URL)]
        started = self.accounts.start(1)
        self.assertTrue(started["ok"])
        self.assertTrue(started["svg"].startswith("<svg"))
        self.assertEqual((started["state"], started["text"]), ("waiting", "等待扫码"))
        self.assertNotIn(QRKEY, json.dumps(started))
        self.mono.t += 2.5
        self.assertEqual(self.accounts.poll(1)["text"], "等待扫码")
        before = len(self.fake.calls)
        self.mono.t += 0.5
        self.assertEqual(self.accounts.poll(1)["state"], "waiting")
        self.assertEqual(len(self.fake.calls), before, "a second poll within POLL_GAP_S does not reach Bilibili")
        self.mono.t += 2.5
        self.assertEqual(self.accounts.poll(1)["text"], "已扫码，请在手机上确认")
        self.mono.t += 2.5
        done = self.accounts.poll(1)
        self.assertEqual((done["state"], done["text"], done["name"], done["vip"]), ("done", "登录成功", "小鼠看番", True))
        self.assertEqual(done["vip_label"], "年度大会员")
        self.assertEqual(done["face"], "https://i1.hdslb.com/bfs/face/abc123.jpg")
        blob = json.dumps(done, ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
        record = self.accounts.store.load(1)
        self.assertEqual(record["cookies"]["SESSDATA"], SESSDATA, "kept encoded, as the browser keeps it")
        self.assertEqual(record["cookies"]["buvid3"], "B3-1111-2222")
        self.assertEqual(record["refresh_token"], REFRESH)

    def test_expiry_by_answer_and_by_lifetime_stops_polling(self):
        self.fake.polls = [poll_answer(86038)]
        self.accounts.start(1)
        self.mono.t += 3
        self.assertEqual(self.accounts.poll(1)["text"], "二维码过期")
        before = len(self.fake.calls)
        self.mono.t += 3
        self.assertEqual(self.accounts.poll(1)["state"], "expired")
        self.assertEqual(len(self.fake.calls), before, "no more polling after expiry")
        self.fake.polls = [poll_answer(86101)]
        self.mono.t += 10
        self.accounts.start(1)
        self.mono.t += bililogin.QR_LIFETIME_S
        before = len(self.fake.calls)
        self.assertEqual(self.accounts.poll(1)["state"], "expired")
        self.assertEqual(len(self.fake.calls), before)

    def test_unreachable_is_graceful(self):
        self.fake.down = True
        answer = self.accounts.start(1)
        self.assertEqual(answer, {"ok": False, "error": "B 站连不上：检查路由器或代理"})
        self.fake.down = False
        self.mono.t += 5
        self.accounts.start(1)
        self.fake.down = True
        self.mono.t += 3
        self.assertEqual(self.accounts.poll(1)["state"], "error")

    def test_file_permissions_and_logout(self):
        self.login()
        path = self.root / "1.json"
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(list(self.root.iterdir()), [path], "no temp files left behind")
        for line in self.logs:
            for secret in SECRETS:
                self.assertNotIn(secret, line)
        self.assertEqual(self.accounts.logout(1), {"ok": True, "login": False})
        self.assertFalse(path.exists(), "logout deletes the cookies immediately")
        self.assertIsNone(self.accounts.login_for(1))

    def test_per_user_isolation(self):
        self.login(1)
        self.assertIsNotNone(self.accounts.login_for(1))
        self.assertIsNone(self.accounts.login_for(2))
        self.assertEqual(self.accounts.account(2), {"ok": True, "login": False})
        self.assertEqual(self.accounts.followings(2)["ok"], False)
        self.assertEqual(self.accounts.poll(2)["state"], "expired", "user 2 cannot finish user 1's QR")
        (self.root / "2.json").write_text((self.root / "1.json").read_text())
        self.accounts._records.clear()
        self.assertIsNone(self.accounts.login_for(2), "a copied file with another user's id is ignored")

    def test_rejected_login_is_marked_and_falls_back(self):
        self.login(1)
        self.fake.nav = NAV_OUT
        answer = self.accounts.account(1, fresh=True)
        self.assertTrue(answer["invalid"])
        self.assertEqual(answer["message"], "登录已失效，请重新扫码")
        self.assertIsNone(self.accounts.login_for(1))
        self.assertEqual(self.accounts.status(1), {"login": True, "invalid": True, "vip": True})
        # A login rejected while playing (-101) does the same.
        self.fake.nav = NAV_VIP
        self.login(1)
        self.assertIsNotNone(self.accounts.login_for(1))
        self.accounts.login_for(1).rejected()
        self.assertIsNone(self.accounts.login_for(1))

    def test_followings_are_paged_and_cached(self):
        self.login(1)
        self.fake.follow_total = 120
        answer = self.accounts.followings(1)
        self.assertEqual((answer["ok"], len(answer["items"]), answer["total"]), (True, 120, 120))
        pages = [c for c in self.fake.calls if "followings" in c[0]]
        self.assertEqual(len(pages), 3)
        self.assertIn("vmid=" + MID, pages[0][0])
        self.accounts.followings(1)
        self.assertEqual(len([c for c in self.fake.calls if "followings" in c[0]]), 3, "cached")


class RelayHarness(unittest.TestCase):
    selfhost = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.accounts = Accounts(root / "tv.db")
        main = root / "channels.txt"
        main.write_text("# empty\n", encoding="utf-8")
        self.logs = []
        self.client = FakeClient()
        self.client.calls = []
        original = self.client.media_url

        def media_url(bvid, login=None, quality=None):
            self.client.calls.append((bvid, login, quality))
            return original(bvid)
        self.client.media_url = media_url
        self.relay = web.Relay(None, self.client, None, "/opt/ff", KEY, main_channels=main, accounts=self.accounts,
                               devices_dir=root / "devices", log=self.logs.append,
                               selfhost=setup.SelfHost(setup_code_file=root / "code") if self.selfhost else None)
        self.fake = FakeBili()
        self.mono = Clock()
        if self.relay.bili_accounts is not None:
            self.relay.bili_accounts.fetch = self.fake
            self.relay.bili_accounts.monotonic = self.mono
            self.relay.bili_accounts.sleep = lambda _s: None
        if self.relay.account_feeds is not None:
            # The login answer starts a sync of the followings (follows.py): run it inline, no thread races.
            self.relay.account_feeds.background = False
            self.relay.account_feeds.follows.background = False
        self.admin = self.accounts.create_user("管理员", "admin")
        self.kid = self.accounts.create_user("小孩")
        self.tokens = {u: self.accounts.new_session(u, "pc") for u in (self.admin, self.kid)}
        self.streams, self.manage = web.make_servers(self.relay, 0, 0)
        for server in (self.streams, self.manage):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.answers = []

    def tearDown(self):
        for server in (self.streams, self.manage):
            server.shutdown()
            server.server_close()
        self.accounts.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, user=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if user is not None:
            headers["Cookie"] = "tvs=" + self.tokens[user]
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        self.answers.append(raw.decode("utf-8", "replace"))
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw

    def sign_in(self, user):
        self.fake.polls = [poll_answer(0, LOGIN_URL)]
        self.assertTrue(self.request("POST", "/tv/api/bili/login/start", {}, user)[1]["ok"])
        self.mono.t += 3
        status, answer = self.request("POST", "/tv/api/bili/login/poll", {}, user)
        self.assertEqual(answer["state"], "done", answer)
        return answer

    def assert_no_leak(self):
        texts = list(self.answers) + list(self.logs)
        texts += [json.dumps(row, ensure_ascii=False, default=str) for row in self.accounts.audit_rows()]
        for text in texts:
            for secret in SECRETS:
                self.assertNotIn(secret, text)


class CloudGatingTests(RelayHarness):
    selfhost = False

    def test_cloud_has_no_bilibili_login(self):
        self.assertIsNone(self.relay.bili_accounts)
        for path in ("/tv/api/bili/account", "/tv/api/bili/followings"):
            self.assertEqual(self.request("GET", path, user=self.admin)[0], 404, path)
        for path in ("/tv/api/bili/login/start", "/tv/api/bili/login/poll", "/tv/api/bili/logout",
                     "/tv/api/bili/followings/import"):
            self.assertEqual(self.request("POST", path, {}, user=self.admin)[0], 404, path)
        state = self.request("GET", "/tv/api/state", user=self.admin)[1]
        self.assertNotIn("bili", state)
        self.assertEqual(self.fake.calls, [])


class SelfhostRelayTests(RelayHarness):
    def test_gating_guests_and_strangers(self):
        self.assertEqual(self.request("GET", "/tv/api/bili/account")[0], 404, "not signed in")
        self.accounts.register_device("aabbccddeeff", "xingzhi-cube-154", "s" * 32)
        guest = self.accounts.new_session(None, "guest", device_id="aabbccddeeff")
        for method, path in (("GET", "/tv/api/bili/account"), ("POST", "/tv/api/bili/login/start")):
            connection = http.client.HTTPConnection("127.0.0.1", self.manage.server_address[1], timeout=5)
            connection.request(method, path, body=b"{}" if method == "POST" else None,
                               headers={"Cookie": "tvs=" + guest, "Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 404, "a guest has no Bilibili account here")
            connection.close()
        state = self.request("GET", "/tv/api/state", user=self.kid)[1]
        self.assertEqual(state["bili"], {"login": False, "invalid": False, "vip": False})
        self.assertEqual(self.request("GET", "/tv/api/bili/account", user=self.kid)[1], {"ok": True, "login": False})

    def test_login_isolation_audit_and_no_leak(self):
        answer = self.sign_in(self.admin)
        self.assertEqual(answer["name"], "小鼠看番")
        self.assertEqual(self.request("GET", "/tv/api/state", user=self.admin)[1]["bili"],
                         {"login": True, "invalid": False, "vip": True})
        self.assertEqual(self.request("GET", "/tv/api/bili/account", user=self.kid)[1]["login"], False)
        path = self.root / "secrets" / "bilibili" / f"{self.admin}.json"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        actions = [row["action"] for row in self.accounts.audit_rows()]
        self.assertIn("bili.login", actions)
        self.request("POST", "/tv/api/bili/logout", {}, self.admin)
        self.assertFalse(path.exists())
        self.assertIn("bili.logout", [row["action"] for row in self.accounts.audit_rows()])
        self.assert_no_leak()

    def test_quality_by_context(self):
        self.sign_in(self.admin)
        self.relay.starter = lambda ffmpeg, url, offset: url
        url = self.relay._start(Video(bvid="BV1aZpw6DEKJ", title="t", duration=60, pubdate=0), 0, self.admin)
        self.assertTrue(url.endswith(".mp4"))
        bvid, used, quality = self.client.calls[-1]
        self.assertEqual((used.user, quality), (self.admin, None), "the TV: the owner's login, the TV's quality")
        dev = {"id": "aabbccddeeff", "owner": self.admin}
        video = Video(bvid="BV1aZpw6DEKJ", title="t", duration=60, pubdate=0)
        with mock.patch.object(self.relay, "_paused_on", return_value=None), \
                mock.patch.object(self.relay, "_current", return_value=(None, video, 12.0, None)):
            source = self.relay._watch_source(dev, "up01", self.kid)
            source.url()
            self.assertEqual(self.client.calls[-1][1].user, self.admin, "the viewer has none: the owner's")
            self.assertEqual(self.client.calls[-1][2], bilibili.PC_QUALITY_VIP)
            self.sign_in(self.kid)
            self.relay.bili_accounts.mark_invalid(self.admin)
            source = self.relay._watch_source(dev, "up01", self.kid)
            source.url()
            self.assertEqual(self.client.calls[-1][1].user, self.kid, "the viewer's own login comes first")
            self.relay.bili_accounts.forget(self.kid)
            source = self.relay._watch_source(dev, "up01", self.kid)
            source.url()
            self.assertEqual(self.client.calls[-1][1:], (None, None), "nobody logged in: anonymous, as before")
        self.assert_no_leak()

    def test_a_device_whose_owner_has_no_login_plays_as_before(self):
        self.sign_in(self.admin)
        self.relay.starter = lambda ffmpeg, url, offset: url
        self.relay._start(Video(bvid="BV1aZpw6DEKJ", title="t", duration=60, pubdate=0), 0, self.kid)
        self.assertEqual(self.client.calls[-1][1:], (None, None), "another user's login is never used")

    def test_member_only_with_an_expired_login_is_temporary(self):
        self.sign_in(self.admin)

        def member(bvid, login=None, quality=None):
            raise MemberOnly(f"{bvid}: 只能试看（需要大会员）")
        self.client.media_url = member
        self.fake.nav = NAV_OUT
        with self.assertRaises(bilibili.SourceError) as caught:
            self.relay._bili_url("ep2/12", self.relay.bili_login(self.admin), self.admin)
        self.assertFalse(caught.exception.permanent, "the login lapsed: do not skip the episode for good")
        self.assertIn("登录已失效", str(caught.exception))
        with self.assertRaises(MemberOnly):
            self.relay._bili_url("ep2/12", None, self.kid)

    def test_vip_series_bypasses_the_shared_cache(self):
        self.sign_in(self.admin)
        seen = []

        def series(source, login=None):
            seen.append(login)
            return "番剧", [Video(bvid="ep1/1", title="1", duration=60, pubdate=0),
                          Video(bvid="ep2/2", title="2 会员", duration=60, pubdate=0)]
        self.client.series = series
        status, answer = self.request("POST", "/tv/api/series", {"url": "https://www.bilibili.com/bangumi/play/ss5"},
                                      self.admin)
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(seen[-1].vip)
        self.assertIsNone(self.accounts.cached_source("bilibili", "ss5", "series"),
                          "a VIP list (with member episodes) is not shared with other users")

    def test_series_needing_vip_explains_login(self):
        status, answer = self.request("POST", "/tv/api/series", {"url": "https://www.bilibili.com/bangumi/play/ss9"},
                                      self.kid)
        self.assertIn("设置 → B 站账号", answer["error"])

    def test_import_followings_with_caps(self):
        self.fake.follow_total = 6
        self.sign_in(self.kid)
        state = self.relay.user_state(self.kid)
        for n in range(9):
            state.add("bilibili", str(5000 + n), f"老 UP{n}", [Video(bvid="BVx", title="x", duration=1, pubdate=0)], 0)
        state.add("bilibili", "1000", "UP 主0", [Video(bvid="BVy", title="y", duration=1, pubdate=0)], 0)
        state.save()
        status, listed = self.request("GET", "/tv/api/bili/followings", user=self.kid)
        self.assertEqual((listed["count"], listed["max"], listed["total"]), (10, 12, 6))
        self.assertEqual([i["subscribed"] for i in listed["items"]], [True] + [False] * 5)
        empty = {"1003"}
        uploads = self.client.uploads
        self.client.uploads = lambda mid: [] if mid in empty else uploads(mid)
        status, answer = self.request("POST", "/tv/api/bili/followings/import",
                                      {"mids": ["1000", "1003", "1001", "1002", "1004", "999999"]}, self.kid)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual([a["mid"] for a in answer["added"]], ["1001", "1002"])
        reasons = {s["mid"]: s["reason"] for s in answer["skipped"]}
        self.assertEqual(reasons, {"1000": "已经订阅了", "1003": "没有投稿", "1004": "最多 12 个 UP 主",
                                   "999999": "不在你的关注列表里"})
        self.assertEqual(sum(c.kind == "up" for c in self.relay.user_state(self.kid).creators), 12)
        self.assertEqual(self.request("POST", "/tv/api/bili/followings/import", {"mids": "1001"}, self.kid)[1]["ok"],
                         False)
        self.assertEqual(self.request("GET", "/tv/api/bili/followings", user=self.admin)[1]["ok"], False,
                         "the admin is not logged in to Bilibili; the kid's list is not his")
        self.assert_no_leak()

    def test_deleting_a_user_forgets_the_login(self):
        from server.relay import admin
        self.sign_in(self.kid)
        path = self.root / "secrets" / "bilibili" / f"{self.kid}.json"
        self.assertTrue(path.exists())
        user = self.accounts.user(self.kid)
        answer = admin._delete_user(self.relay, dict(user, id=self.kid), {"confirm": user["name"]},
                                    web.Caller(user=self.admin, role="admin"), "127.0.0.1")
        self.assertTrue(answer["ok"], answer)
        self.assertFalse(path.exists())


class DiagnosticsTests(unittest.TestCase):
    def test_bundle_and_logs_never_hold_the_cookies(self):
        from server.selfhost import bootstrap, config, desktop, diagnostics
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = bootstrap.Layout(root)
            bootstrap.prepare(layout)
            config.ensure(layout.config)
            store = bililogin.Store(layout.secrets / "bilibili")
            store.save(1, {"cookies": {"SESSDATA": SESSDATA, "bili_jct": JCT}, "refresh_token": REFRESH})
            logs = root / "logs"
            log = desktop.CoreLog(logs / "core.log", echo=False)
            log(f"oops Cookie: SESSDATA={SESSDATA}; bili_jct={JCT}")
            log(json.dumps({"refresh_token": REFRESH}))
            log._handler.close()
            state = desktop.Desktop(layout, logs, lambda _m: None, "127.0.0.1", 8090, 8080,
                                    {"xingzhi": 8098, "passport": 8097}, "192.168.1.5", desktop.Ffmpeg(root / "bin"),
                                    desktop.Settings(root / "desktop.json"))
            with mock.patch.object(diagnostics, "port_holder", return_value=""):
                path = diagnostics.build(state, root / "out")
            with zipfile.ZipFile(path) as archive:
                blob = b"".join(archive.read(n) for n in archive.namelist()).decode("utf-8", "replace")
        for secret in SECRETS[:4]:
            self.assertNotIn(secret, blob)
        self.assertIn("SESSDATA=***", blob)


if __name__ == "__main__":
    unittest.main()
