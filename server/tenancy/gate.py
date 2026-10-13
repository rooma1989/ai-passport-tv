"""电视服务一侧：认出连上来的设备，决定这次会话怎么服务它。

设置了 TV_DB（数据库）时启用；没设置时电视服务和以前完全一样（只认共享配对令牌）。

- 新固件在 HELLO 里报 device_id（出厂 MAC）、device_secret、model、fw。第一次见到的 id 记下
  密钥的哈希（首次信任），状态为未绑定；之后密钥对不上就拒绝，除非主人刚点了「重新配对」。
- 旧固件只带共享令牌、没有 id：令牌对了就当作这个端口上迁移来的那台旧设备（legacy_port）。
- 新固件同时带了令牌、但还没绑定：照旧当作这个端口的旧设备服务，免得刷完固件就看不了；
  管理员之后用 `python3 -m server.tenancy.admin adopt` 把旧记录的状态搬到真 id 上。
- 未绑定的设备不转码：只发绑定码的静止画面和静音（unbound.py）。

阶段 1 仍是每个型号一个电视服务、一次一台设备（静态单元）；设备认出来以后，这次会话用
devices/<id>/ 下它自己的那套文件（apply()）。
"""

from __future__ import annotations

import hmac
import os
import re
import time
from pathlib import Path
from typing import NamedTuple

from ..protocol import ProtocolError
from . import files as device_files_module
from . import wifi as device_wifi
from .accounts import Accounts
from .db import sha256
from .files import DeviceFiles

DB_ENV = "TV_DB"
DEVICES_ENV = "TV_DEVICES_DIR"
# 新固件的设备 id：出厂 MAC，12 位小写十六进制。
REAL_ID = re.compile(r"^[0-9a-f]{12}$")
# 32 字节随机数的 base64url（不带 =）：43 个字符。
SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
FW_MAX = 64
# 认证失败：每个 IP、每个设备 id 每分钟最多这么多次，之后一分钟内直接拒绝。
FAILURES_PER_MINUTE = 10
# 共享令牌可以由管理员关掉（meta.legacy_token = 0），所有设备都刷了新固件以后。
LEGACY_TOKEN_KEY = "legacy_token"
_REMOTE_URL = re.compile(r"^HTTPS?://[A-Z0-9.:/%-]+$")
REMOTE_URL_MAX = 64


class Admission(NamedTuple):
    device_id: str
    bound: bool                     # False：未绑定，只给绑定画面
    files: DeviceFiles | None       # 这次会话用的那套文件；未绑定时为 None
    claim: str = ""                 # 未绑定时的绑定码
    claim_url: str = ""             # 状态页二维码：HTTPS://<SITE>/TV/B/<码>，没有网站地址时为空
    legacy: bool = False            # 按共享令牌当作旧设备服务


def _token_matches(supplied, token: bytes | None) -> bool:
    if token is None or not isinstance(supplied, str):
        return False
    try:
        supplied_bytes = supplied.encode("ascii")
    except UnicodeError:
        return False
    return hmac.compare_digest(supplied_bytes, token)


class Gate:
    def __init__(self, db_path: Path | str, devices_dir: Path | str, logger=print, clock=time.time,
                 monotonic=time.monotonic):
        self.accounts = Accounts(db_path, clock=clock)
        self.devices_dir = Path(devices_dir)
        self.logger = logger
        self.clock = clock
        self.monotonic = monotonic
        self._failures: dict[str, list[float]] = {}

    # ---- 失败限流 ----

    def _limited(self, *keys: str) -> bool:
        now = self.monotonic()
        for key in keys:
            recent = [t for t in self._failures.get(key, []) if now - t < 60]
            self._failures[key] = recent
            if len(recent) >= FAILURES_PER_MINUTE:
                return True
        return False

    def _fail(self, reason: str, keys: tuple[str, ...], audit: tuple | None = None) -> None:
        now = self.monotonic()
        first = all(not self._failures.get(k) for k in keys)
        for key in keys:
            self._failures.setdefault(key, []).append(now)
        # 日志和操作记录只写第一次：被人反复撞时不刷屏。
        if first:
            self.logger(f"device refused: {reason}")
            if audit is not None:
                self.accounts.audit(*audit)
        raise ProtocolError("authentication failed")

    # ---- 认设备 ----

    def admit(self, hello: dict, token: bytes | None, port: int, peer: str = "") -> Admission:
        """HELLO 已由 tv_server.authenticate(…, None) 查过形状。拒绝时抛 ProtocolError。"""
        did, secret = hello.get("device_id"), hello.get("device_secret")
        identified = isinstance(did, str) and REAL_ID.match(did) and isinstance(secret, str) \
            and SECRET.match(secret)
        keys = (f"ip:{peer}",) + ((f"id:{did}",) if identified else ())
        if self._limited(*keys):
            raise ProtocolError("authentication failed")
        legacy_on = self.accounts.meta(LEGACY_TOKEN_KEY, "1") != "0"
        token_ok = legacy_on and _token_matches(hello.get("token"), token)
        fw = hello.get("fw") if isinstance(hello.get("fw"), str) else ""
        fw = "".join(c for c in fw if 32 <= ord(c) < 127)[:FW_MAX]
        model = hello.get("model") if isinstance(hello.get("model"), str) else ""
        if identified:
            repaired = False
            row = self.accounts.device(did)
            if row is None:
                row = self.accounts.register_device(did, model, secret, fw, peer)
                self.logger(f"device {did} registered model={model or '?'} (unclaimed)")
            else:
                if row["status"] in ("banned", "retired"):
                    self._fail(f"device {did} is {row['status']}", keys)
                matches = row["secret_sha256"] is not None and hmac.compare_digest(
                    bytes(row["secret_sha256"]), sha256(secret))
                # 重新配对窗口：刷过机的设备密钥和 Wi-Fi 钥匙都是新的，两样都换成新的。
                repaired = row["repair_until"] is not None and row["repair_until"] > self.clock()
                if not matches:
                    if repaired:
                        self.accounts.pin_secret(did, secret)
                        self.logger(f"device {did} re-paired: new secret pinned")
                    else:
                        self._fail(f"device {did} secret mismatch", keys,
                                   ("device.secret_mismatch", None, peer, "device", did))
                self.accounts.touch_device(did, fw=fw, model=model, ip=peer)
                row = self.accounts.device(did)
            if row["status"] == "active":
                self.wifi_hello(did, hello, repaired)
                return Admission(did, True, self.files(did))
            if token_ok:
                legacy = self._legacy(port)
                if legacy is not None:
                    self.logger(f"device {did} unbound but has the pairing token: "
                                f"served as {legacy} (adopt it to move its state)")
                    self.wifi_hello(legacy, hello, repaired)
                    return Admission(legacy, True, self.files(legacy), legacy=True)
            claim = row["claim_code"] or ""
            return Admission(did, False, None, claim=claim, claim_url=self.claim_url(claim))
        if token_ok:
            legacy = self._legacy(port)
            if legacy is not None:
                self.accounts.touch_device(legacy, ip=peer)
                return Admission(legacy, True, self.files(legacy), legacy=True)
            self._fail(f"pairing token on port {port}, which has no legacy device", keys)
        self._fail("no device identity and no valid pairing token", keys)
        raise AssertionError("unreachable")

    def wifi_hello(self, device_id: str, hello: dict, repin: bool = False) -> None:
        """新固件的 Wi-Fi 管理：公钥（首次信任）、保存的 Wi-Fi 名字、正在用的（server/tenancy/wifi.py）。"""
        try:
            device_wifi.hello(self.accounts, device_id, hello, repin=repin, logger=self.logger)
        except Exception as error:      # noqa: BLE001 —— 记不下来不该挡住播放
            self.logger(f"device {device_id} wifi state not stored: {type(error).__name__}")

    def _legacy(self, port: int) -> str | None:
        row = self.accounts.legacy_device(port)
        return row["id"] if row is not None and row["status"] == "active" else None

    def files(self, device_id: str) -> DeviceFiles:
        return device_files_module.device_files(self.devices_dir, device_id)

    def claim_url(self, claim: str) -> str:
        site = self.accounts.meta("public_url", "").rstrip("/").upper()
        url = f"{site}/TV/B/{claim}" if site and claim else ""
        return url if len(url) <= REMOTE_URL_MAX and _REMOTE_URL.match(url) else ""

    def is_bound(self, device_id: str) -> bool:
        row = self.accounts.device(device_id)
        return row is not None and row["status"] == "active"


def from_env(logger=print) -> Gate | None:
    """TV_DB 设置了才启用；TV_DEVICES_DIR 默认是数据库旁边的 devices/。"""
    path = os.environ.get(DB_ENV)
    if not path:
        return None
    devices = os.environ.get(DEVICES_ENV) or str(Path(path).parent / "devices")
    return Gate(path, devices, logger=logger)


def apply(server, files: DeviceFiles) -> None:
    """让这次会话用这台设备的文件：正在播放、事件、频道表、分组、遥控地址。

    阶段 1 一个电视服务一次只服务一台设备，所以直接换掉进程里的这几处；阶段 4 每台设备
    一个工作进程，环境变量就够了，不再需要这个函数。
    """
    from .. import live
    from ..cast_switch import SwitchWatcher
    server.now_playing_path = files.now
    server.remote_url_path = files.remote
    watcher = getattr(server, "switch_watcher", None)
    if watcher is None or watcher.path != files.events:
        # 换了一台设备：上一台没处理完的换台不能带过来。
        server.switch_watcher = SwitchWatcher(files.events)
        server.pending_switch = ""
    if not getattr(server, "live_enabled", True):
        return
    os.environ[live.GROUPS_ENV] = str(files.groups)
    if live._LOADED.get("relay") != files.channels:
        try:
            live.load_channels(live._LOADED.get("main"), files.channels)
        except (ValueError, OSError, UnicodeError) as error:
            server.logger(f"device channel table unreadable, keeping the old one: {type(error).__name__}")
