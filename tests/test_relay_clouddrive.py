"""Adding cloud drives from inside RooTV (server/relay/clouddrive.py, the /api/admin/clouddrive/* drive endpoints).

A fake OpenList (no network) answers the admin API the way v4.2.9 does: /api/auth/login hands out a
token for the Authorization header, /api/admin/driver/info gives the defaults, storage/create runs
the driver's Init and answers code 500 with the storage already created when Init fails, and the
storage's status carries the result. QuarkTV's Init without tokens asks for a login QR and ends with
"need verify: <body><img src=data:image/jpeg;base64,…>"; re-running it (storage/update) with the
saved query_token answers 用户未确认授权 until the code is scanned, 授权码Code二维码过期 when it
expired, and logs in (status "work") once scanned. BaiduNetdisk works only with the online API
defaults filled in and a good refresh token.

What is checked: the QR is taken out of the status, re-encoded and handed to the page; a free mount
path is chosen (/夸克, then /夸克2); a pending storage is reused instead of mounting another; check
answers waiting / expired / done and done rescans the managed library source; statuses read as
正常 / 需要扫码 / 登录过期 / 出错 with a short safe reason; a Baidu token creates the storage with
native_proxy and use_online_api; delete removes it; nothing the page or the log gets carries a
token, the addition, the QR's URL or the admin password; the endpoints are admin-only and only on a
self-hosted relay; a stale OpenList login token is renewed once.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server.relay import clouddrive
from server.selfhost import openlist

import test_relay_library as lib_base
import test_selfhost_openlist as ol_base

QR_URL = b"https://su.quark.cn/qr-payload-secret-4f1c"
QR_JPEG = b"\xff\xd8\xff\xe0" + b"JFIF" + QR_URL + b"\x00" * 64 + b"\xff\xd9"
QUARK_RT = "quark-refresh-token-SECRET-0123456789"
BAIDU_RT = "122.baidu-refresh-token-SECRET-abcdefABCDEF"
BAIDU_RT2 = "122.baidu-rotated-token-SECRET-zyxwvuZYXWVU"
DRIVERS = {
    "QuarkTV": [("root_folder_id", "string", "0"), ("order_by", "select", "updated_at"),
                ("order_direction", "select", "desc"), ("refresh_token", "string", ""), ("device_id", "string", ""),
                ("query_token", "string", ""), ("link_method", "select", "download")],
    "BaiduNetdisk": [("root_folder_path", "string", "/"), ("order_by", "select", "name"),
                     ("download_api", "select", "official"), ("use_online_api", "bool", "true"),
                     ("api_url_address", "string", "https://api.oplist.org/baiduyun/renewapi"),
                     ("client_id", "string", ""), ("client_secret", "string", ""),
                     ("refresh_token", "string", ""), ("upload_timeout", "number", "60")],
}
DRIVERS["UCTV"] = DRIVERS["QuarkTV"]


class FakeOpenList(ol_base.PingDav):
    """OpenList v4.2.9's admin API (the parts clouddrive.py uses) plus /ping and the WebDAV of the drives."""
    state: dict = {}

    def _json(self, data):
        self._plain(200, json.dumps(data, ensure_ascii=False).encode(), {"Content-Type": "application/json"})

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") in self.state["tokens"]

    def _body(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        return json.loads(raw) if raw else {}

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if not url.path.startswith("/api/"):
            super().do_GET()
            return
        query = dict(urllib.parse.parse_qsl(url.query))
        self.state["calls"].append(("GET", url.path))
        if not self._authorized():
            self._json({"code": 401, "message": "token is invalidated", "data": None})
            return
        storages = self.state["storages"]
        if url.path == "/api/admin/storage/list":
            self._json({"code": 200, "message": "success",
                        "data": {"content": [dict(s) for s in storages.values()], "total": len(storages)}})
        elif url.path == "/api/admin/storage/get":
            found = storages.get(int(query.get("id", 0)))
            self._json({"code": 200, "data": dict(found)} if found else {"code": 500, "message": "record not found"})
        elif url.path == "/api/admin/driver/info":
            items = DRIVERS.get(query.get("driver"))
            if items is None:
                self._json({"code": 404, "message": "driver not found"})
                return
            self._json({"code": 200, "data": {"common": [], "additional": [
                {"name": n, "type": t, "default": d, "options": "", "required": False, "help": ""} for n, t, d in items]}})
        else:
            self._json({"code": 404, "message": "not found"})

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        body = self._body()
        self.state["calls"].append(("POST", url.path))
        if url.path == "/api/auth/login":
            if body == {"username": "admin", "password": self.password}:
                self.state["n"] += 1
                token = f"jwt-SECRET-{self.state['n']}"
                self.state["tokens"].add(token)
                self._json({"code": 200, "data": {"token": token}})
            else:
                self._json({"code": 400, "message": "password is incorrect"})
            return
        if not self._authorized():
            self._json({"code": 401, "message": "token is invalidated", "data": None})
            return
        storages = self.state["storages"]
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path == "/api/admin/storage/create":
            if any(s["mount_path"] == body["mount_path"] for s in storages.values()):
                self._json({"code": 500, "message": "UNIQUE constraint failed: x_storages.mount_path"})
                return
            self.state["n"] += 1
            sid = self.state["n"]
            storage = dict(body, id=sid, status="", disabled=False)
            self.state["created"].append(dict(body))
            storages[sid] = storage
            error = self.init(storage)
            self._json({"code": 500, "message": "failed init storage but storage is already created: " + error,
                        "data": {"id": sid}} if error else {"code": 200, "data": {"id": sid}})
        elif url.path == "/api/admin/storage/update":
            old = storages.get(body.get("id"))
            if old is None or old["driver"] != body.get("driver"):
                self._json({"code": 500, "message": "failed get old storage"})
                return
            storage = dict(body)
            storages[storage["id"]] = storage
            error = self.init(storage)
            self._json({"code": 500, "message": error} if error else {"code": 200, "message": "success"})
        elif url.path == "/api/admin/storage/delete":
            storages.pop(int(query.get("id", 0)), None)
            self._json({"code": 200, "message": "success"})
        elif url.path == "/api/fs/list":
            self.state["refreshed"].append((body.get("path"), body.get("refresh")))
            self._json({"code": 200, "message": "success", "data": {"content": [], "total": 0}})
        else:
            self._json({"code": 404, "message": "not found"})

    def init(self, storage) -> str:
        """The driver's Init: sets status (and the addition, like op.MustSaveDriverStorage); "" when it works."""
        add = json.loads(storage["addition"])
        error = ""
        if storage["driver"] in ("QuarkTV", "UCTV"):
            if not add.get("refresh_token"):
                if not add.get("query_token"):
                    self.state["n"] += 1
                    add["query_token"] = f"query-token-SECRET-{self.state['n']}"
                    qr = base64.b64encode(QR_JPEG).decode()
                    error = f'need verify: \n<body>\n        <img src="data:image/jpeg;base64,{qr}"/>\n    </body>'
                elif self.state["expired"]:
                    error = "授权码Code二维码过期"
                elif not self.state["scanned"]:
                    error = "用户未确认授权"
                else:
                    add["refresh_token"] = QUARK_RT
            elif add["refresh_token"] != QUARK_RT:
                error = "refresh token is empty"
        elif storage["driver"] == "BaiduNetdisk":
            if not (add.get("use_online_api") is True and add.get("api_url_address")):
                error = "empty ClientID or ClientSecret"
            elif add.get("refresh_token") not in (BAIDU_RT, BAIDU_RT2):
                error = "failed to refresh token: refresh token is invalid"
            else:
                add["refresh_token"] = BAIDU_RT2
                add["AccessToken"] = "baidu-access-SECRET"
        storage["addition"] = json.dumps(add)
        storage["status"] = error or "work"
        return ("failed init storage: " + error) if error else ""


def fake_openlist(password, files):
    state = {"storages": {}, "tokens": set(), "n": 0, "calls": [], "created": [], "scanned": False, "expired": False, "refreshed": []}
    handler = type("OL", (FakeOpenList,), {"files": dict(files), "redirect": set(), "seen": [], "user": "admin",
                                           "password": password, "state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server, state


class MappingTests(unittest.TestCase):
    def test_status_reads_plainly_and_never_carries_secrets(self):
        cases = [
            ({"status": "work"}, ("ok", "")),
            ({"status": "work", "disabled": True}, ("off", "")),
            ({"status": "need verify: \n<body><img src=\"data:image/jpeg;base64,AAAA\"/></body>"}, ("scan", "")),
            ({"status": "用户未确认授权"}, ("scan", "")),
            ({"status": "授权码Code二维码过期"}, ("scan", "二维码过期了")),
            ({"status": "failed to refresh token: refresh token is invalid"}, ("expired", "令牌不对或过期了")),
            ({"status": 'Get "https://open-api-drive.quark.cn/oauth/authorize?access_token=SECRET&x=1": '
                        "context deadline exceeded"}, ("error", "连不上网盘（网络不通或太慢）")),
            ({"status": "文件夹不存在"}, ("error", "文件夹不存在")),
            ({"status": "errno 31045 ABCDEFGHIJKLMNOPQRSTUVWXYZ"}, ("error", "网盘那边出错了")),
            ({"status": "panic: runtime error <stack>"}, ("error", "网盘那边出错了")),
        ]
        for storage, wanted in cases:
            self.assertEqual(clouddrive._state(storage), wanted, storage)
        public = clouddrive._public({"mount_path": "/夸克", "driver": "QuarkTV", "status": "用户未确认授权",
                                     "addition": json.dumps({"query_token": "q-SECRET"})})
        self.assertEqual((public["name"], public["kind"], public["label"], public["relogin"]),
                         ("夸克", "夸克", "需要扫码", "quark"))
        self.assertNotIn("SECRET", json.dumps(public, ensure_ascii=False))

    def test_qr_is_reencoded_and_only_images_are_taken(self):
        status = 'need verify: \n<body>\n<img src="data:image/jpeg;base64,' + base64.b64encode(QR_JPEG).decode() + '"/></body>'
        self.assertEqual(clouddrive._qr(status), "data:image/jpeg;base64," + base64.b64encode(QR_JPEG).decode())
        html = base64.b64encode(b"<script>alert(1)</script>").decode()
        self.assertEqual(clouddrive._qr(f'<img src="data:image/jpeg;base64,{html}">'), "")
        self.assertEqual(clouddrive._qr("need verify"), "")
        self.assertEqual(clouddrive._qr('<img src="data:image/jpeg;base64,!!!">'), "")

    def test_free_mount_path(self):
        self.assertEqual(clouddrive._free_mount([], "/夸克"), "/夸克")
        taken = [{"mount_path": "/夸克"}, {"mount_path": "/夸克2"}, {"mount_path": "/百度网盘"}]
        self.assertEqual(clouddrive._free_mount(taken, "/夸克"), "/夸克3")
        self.assertEqual(clouddrive._free_mount(taken, "/百度网盘"), "/百度网盘2")


class DriveApiTests(lib_base.ApiTests):
    """Through the relay's real HTTP port with the fake OpenList as the running component."""

    def setUp(self):
        super().setUp()
        self.data = Path(self.accounts.path).parent
        self.relay.selfhost_data = self.data
        self.relay.log = self.logs.append
        self.paths = openlist.Paths(self.data)
        self.paths.bin.mkdir(parents=True)
        (self.paths.bin / "openlist-v4.2.9").write_bytes(b"x")
        (self.paths.bin / "components.json").write_text(json.dumps({"openlist": {"version": "v4.2.9",
                                                                                 "file": "openlist-v4.2.9"}}))
        self.password, _ = openlist.ensure_password(self.paths)
        self.ol, self.state = fake_openlist(self.password, {"夸克/电影/Inception.2010.mp4": lib_base.MP4})
        self.paths.status.parent.mkdir(parents=True, exist_ok=True)
        self.paths.status.write_text(json.dumps({"state": "running", "port": self.ol.server_address[1]}))
        self.answers = []

    def tearDown(self):
        self.ol.shutdown()
        self.ol.server_close()
        super().tearDown()

    def drive(self, path, body=None, who="admin"):
        method = "GET" if path == "storages" else "POST"
        status, answer = self.call(who, method, "/tv/api/admin/clouddrive/" + path, body)
        self.assertEqual(status, 200, path)
        self.answers.append(answer)
        return answer

    def assert_no_secrets(self):
        seen = json.dumps(self.answers, ensure_ascii=False) + "\n".join(self.logs)
        seen += "\n".join(str(r) for r in self.accounts.audit_rows(200))
        for secret in (QUARK_RT, BAIDU_RT, BAIDU_RT2, "SECRET", self.password, QR_URL.decode(), "addition"):
            self.assertNotIn(secret, seen)

    def test_quark_qr_login_end_to_end(self):
        self.assertEqual(self.drive("storages"), {"ok": True, "running": True, "storages": []})
        start = self.drive("quark/start", {"kind": "quark"})
        self.assertEqual((start["state"], start["mount_path"], start["kind"]), ("waiting", "/夸克", "夸克"))
        self.assertEqual(start["qr"], "data:image/jpeg;base64," + base64.b64encode(QR_JPEG).decode())
        created = self.state["created"][0]
        self.assertEqual((created["driver"], created["webdav_policy"]), ("QuarkTV", "native_proxy"))
        addition = json.loads(created["addition"])
        self.assertEqual((addition["root_folder_id"], addition["link_method"]), ("0", "download"), "defaults filled")
        row = self.drive("storages")["storages"][0]
        self.assertEqual((row["name"], row["state"], row["label"]), ("夸克", "scan", "需要扫码"))
        # Not scanned yet: update re-runs Init with the saved query token -> 用户未确认授权.
        check = self.drive("quark/check", {"mount_path": "/夸克"})
        self.assertEqual(check["state"], "waiting")
        self.assertIn(("POST", "/api/admin/storage/update"), self.state["calls"])
        # Asked again at once: no second round trip to Quark.
        updates = self.state["calls"].count(("POST", "/api/admin/storage/update"))
        self.assertEqual(self.drive("quark/check", {"mount_path": "/夸克"})["state"], "waiting")
        self.assertEqual(self.state["calls"].count(("POST", "/api/admin/storage/update")), updates)
        # Scanned: the next check logs in, the library's managed source appears and is scanned.
        self.state["scanned"] = True
        self.relay._clouddrive_checked.clear()
        done = self.drive("quark/check", {"mount_path": "/夸克"})
        self.assertEqual((done["state"], done["kind"]), ("done", "夸克"))
        self.assertEqual(self.drive("storages")["storages"][0]["label"], "正常")
        self.assertIsNotNone(self.relay.library.managed())
        self.assertEqual([i["path"] for i in self.relay.library.items()], ["夸克/电影/Inception.2010.mp4"])
        # A second account gets the next free mount path.
        again = self.drive("quark/start", {"kind": "quark"})
        self.assertEqual(again["mount_path"], "/夸克2")
        actions = [r["action"] for r in self.accounts.audit_rows(100) if r["action"].startswith("clouddrive.")]
        self.assertEqual(sorted(actions), ["clouddrive.drive_add", "clouddrive.drive_qr", "clouddrive.drive_qr"])
        self.assert_no_secrets()

    def test_quark_expiry_reuse_and_relogin(self):
        self.drive("quark/start", {"kind": "quark"})
        self.state["expired"] = True
        self.assertEqual(self.drive("quark/check", {"mount_path": "/夸克"})["state"], "expired")
        self.assertEqual(self.drive("storages")["storages"][0]["reason"], "二维码过期了")
        # 换一个: the same storage gets a fresh QR (query token cleared), nothing new is mounted.
        self.state["expired"] = False
        fresh = self.drive("quark/start", {"kind": "quark", "mount_path": "/夸克"})
        self.assertEqual((fresh["state"], fresh["mount_path"]), ("waiting", "/夸克"))
        self.assertTrue(fresh["qr"])
        # Pressing 添加夸克 again while one is still pending reuses it as well.
        self.assertEqual(self.drive("quark/start", {"kind": "quark"})["mount_path"], "/夸克")
        self.assertEqual(len(self.state["storages"]), 1)
        # A check without a QR from this relay (restarted) asks for a new one.
        self.relay._clouddrive_qr.clear()
        self.assertEqual(self.drive("quark/check", {"mount_path": "/夸克"})["state"], "expired")
        # UC uses the same TV driver.
        uc = self.drive("quark/start", {"kind": "uc"})
        self.assertEqual((uc["mount_path"], uc["kind"]), ("/UC网盘", "UC 网盘"))
        self.assertFalse(self.drive("quark/start", {"kind": "baidu"})["ok"])
        self.assertFalse(self.drive("quark/check", {"mount_path": "/nope"})["ok"])
        self.assert_no_secrets()

    def test_baidu_refresh_token(self):
        bad = self.drive("baidu", {"refresh_token": "short"})
        self.assertFalse(bad["ok"])
        self.assertIn("不像刷新令牌", bad["error"])
        self.assertEqual(self.state["created"], [])
        wrong = self.drive("baidu", {"refresh_token": "122.not-the-right-token-0000000000"})
        self.assertFalse(wrong["ok"])
        self.assertIn("令牌不对或过期了", wrong["error"])
        self.assertEqual(self.drive("storages")["storages"][0]["label"], "登录过期")
        # The right token: the broken /百度网盘 is reused (no /百度网盘2), with the online API defaults.
        good = self.drive("baidu", {"refresh_token": "  " + BAIDU_RT + "\n"})
        self.assertEqual((good["ok"], good["mount_path"], good["kind"]), (True, "/百度网盘", "百度网盘"))
        self.assertEqual(len(self.state["storages"]), 1)
        created = self.state["created"][0]
        self.assertEqual((created["driver"], created["webdav_policy"]), ("BaiduNetdisk", "native_proxy"))
        addition = json.loads(created["addition"])
        self.assertEqual((addition["use_online_api"], addition["api_url_address"], addition["upload_timeout"]),
                         (True, "https://api.oplist.org/baiduyun/renewapi", 60))
        self.assertEqual(self.drive("storages")["storages"][0]["state"], "ok")
        self.assertIn("clouddrive.drive_add", [r["action"] for r in self.accounts.audit_rows(100)])
        self.assert_no_secrets()

    def test_delete_rescan_and_stale_login(self):
        self.drive("baidu", {"refresh_token": BAIDU_RT})
        # OpenList restarted (its tokens are gone): RooTV logs in again once.
        self.state["tokens"].clear()
        self.assertEqual(len(self.drive("storages")["storages"]), 1)
        self.assertTrue(self.drive("rescan", {})["ok"])
        # Reconnect: the component drops the drive's session and runs Init again, with the stored login untouched.
        stored = lambda: json.loads(next(iter(self.state["storages"].values()))["addition"])
        before, calls = stored(), len(self.state["calls"])
        again = self.drive("storages/reconnect", {"mount_path": "/百度网盘"})
        self.assertEqual((again["ok"], again["state"], again["label"]), (True, "ok", "正常"), again)
        self.assertIn(("POST", "/api/admin/storage/update"), self.state["calls"][calls:], "Init runs again")
        self.assertEqual(stored(), before, "the stored login is untouched")
        self.assertFalse(self.drive("storages/reconnect", {"mount_path": "/nope"})["ok"])
        self.assertFalse(self.drive("storages/reconnect", {})["ok"])
        self.assertFalse(self.drive("storages/delete", {"mount_path": "/nope"})["ok"])
        self.assertFalse(self.drive("storages/delete", {"mount_path": 3})["ok"])
        gone = self.drive("storages/delete", {"mount_path": "/百度网盘"})
        self.assertTrue(gone["ok"])
        self.assertEqual(self.drive("storages")["storages"], [])
        actions = [r["action"] for r in self.accounts.audit_rows(100) if r["action"].startswith("clouddrive.")]
        self.assertIn("clouddrive.drive_delete", actions)
        self.assertIn("clouddrive.rescan", actions)
        self.assertIn("clouddrive.reconnect", actions)
        self.assert_no_secrets()

    def test_scanning_the_drive_refreshes_every_folder_first(self):
        """OpenList caches a folder's listing for 30 minutes: without a refresh, a video just saved to Quark is missed."""
        clouddrive.attach(self.relay, start=False)
        self.drive("baidu", {"refresh_token": BAIDU_RT})
        self.relay.library.scan_now(self.relay.library.managed()["id"])
        self.assertEqual(self.state["refreshed"][:3], [("/", True), ("/夸克", True), ("/夸克/电影", True)])
        self.assertIn("Inception", json.dumps(self.relay.library.view(), ensure_ascii=False))
        # The component refusing a refresh never stops the scan: the cached listing is read as before.
        self.relay.library.refresh_dir = lambda rel: (_ for _ in ()).throw(clouddrive.DriveError("x"))
        self.assertTrue(self.relay.library.scan_now(self.relay.library.managed()["id"])["ok"])

    def test_admin_only_self_host_only_and_not_running(self):
        for who in ("alice", "guest", None):
            self.assertEqual(self.request("GET", "/tv/api/admin/clouddrive/storages", who)[0], 404, who)
            for path in ("quark/start", "quark/check", "baidu", "storages/delete", "storages/reconnect", "rescan"):
                self.assertEqual(self.request("POST", "/tv/api/admin/clouddrive/" + path, who, {})[0], 404, (who, path))
        self.assertEqual(self.state["calls"], [])
        self.paths.status.write_text(json.dumps({"state": "stopped", "port": self.ol.server_address[1]}))
        stopped = self.drive("storages")
        self.assertEqual((stopped["ok"], stopped["running"], stopped["storages"]), (True, False, []))
        self.assertFalse(self.drive("quark/start", {"kind": "quark"})["ok"])
        self.relay.selfhost = None
        self.assertFalse(self.drive("storages")["ok"])
        self.assertFalse(self.drive("quark/start", {"kind": "quark"})["ok"])
        self.assertEqual(self.state["calls"], [])

    # The library's own API tests are inherited; they run once in test_relay_library.
    test_admin_only_management_and_family_only_reading = None
    test_cloud_relay_has_no_library = None


for _name in [n for n in dir(lib_base.ApiTests) if n.startswith("test_")]:
    if _name not in DriveApiTests.__dict__:
        setattr(DriveApiTests, _name, None)


if __name__ == "__main__":
    unittest.main()
