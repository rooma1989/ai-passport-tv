"""调视频中转的管理接口（本机回环，带密钥），并且总是带上 device：动作作用在说话的那台设备上，
不是手机正在遥控的那台（Relay.target() 跟着手机走）。

    TV_RELAY_MANAGE_URL  中转管理端口，默认 http://127.0.0.1:8100，只接受本机地址
    TV_RELAY_KEY_FILE    中转的密钥文件（与 tv-relay.service 的 --key-file 同一个）
    TV_RELAY_DEVICE      单用户中转（没有 --db）时本媒体服务对应的设备：中转里的名字
                         （--device-name / --extra-device 的名字，例如 Passport）或 id（d0、d1）。
                         多用户（电视服务设置了 TV_DB）时不用它：设备是每次会话认出来的那台
                         （legacy-passport、12 位 MAC……），见 VoiceService.bind_device()。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MANAGE_URL_ENV = "TV_RELAY_MANAGE_URL"
KEY_FILE_ENV = "TV_RELAY_KEY_FILE"
DEVICE_ENV = "TV_RELAY_DEVICE"
DEFAULT_URL = "http://127.0.0.1:8100"
TIMEOUT_S = 3.0
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class RelayError(RuntimeError):
    """中转连不上或回答不对；消息里不含密钥。"""


class RelayClient:
    def __init__(self, base_url: str, key: str, device: str, timeout: float = TIMEOUT_S):
        parsed = urllib.parse.urlsplit(base_url)
        # 密钥只发给本机：配置写错时宁可不用中转，也不把密钥发到别处。
        if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK:
            raise ValueError(f"{MANAGE_URL_ENV} must be http on the loopback address")
        if not key or not device:
            raise ValueError("relay key and device are required")
        self.base = base_url.rstrip("/")
        self._key = key
        self.device = device
        self.timeout = timeout
        # 直连本机，不走环境变量里的代理。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_env(cls, environ=os.environ) -> "RelayClient | None":
        """三个变量里缺密钥或设备名就返回 None（不用中转）。"""
        key_file, device = environ.get(KEY_FILE_ENV), environ.get(DEVICE_ENV)
        if not key_file or not device:
            return None
        key = Path(key_file).read_text(encoding="utf-8").strip()
        return cls(environ.get(MANAGE_URL_ENV, DEFAULT_URL), key, device)

    @classmethod
    def factory_from_env(cls, environ=os.environ):
        """多用户：只要密钥（设备每次会话才知道）。返回 设备 id → RelayClient；没有密钥返回 None。"""
        key_file = environ.get(KEY_FILE_ENV)
        if not key_file:
            return None
        key = Path(key_file).read_text(encoding="utf-8").strip()
        base = environ.get(MANAGE_URL_ENV, DEFAULT_URL)
        cls(base, key, "-")     # 现在就检查地址和密钥，配置错了在启动时就知道
        return lambda device: cls(base, key, device)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"X-Key": self._key,
                                                  "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(256 * 1024)
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(16 * 1024)
                answer = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, ValueError):
                raise RelayError(f"relay status {error.code}") from None
            if isinstance(answer, dict):
                return answer
            raise RelayError(f"relay status {error.code}") from None
        except (OSError, urllib.error.URLError) as error:
            raise RelayError(f"relay unreachable: {type(error).__name__}") from None
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise RelayError("relay answer is not JSON") from None
        if not isinstance(answer, dict):
            raise RelayError("relay answer is not an object")
        return answer

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._call("POST", path, dict(body or {}, device=self.device))

    def catalog(self) -> dict:
        query = urllib.parse.urlencode({"device": self.device})
        return self._call("GET", f"/api/voice/catalog?{query}")

    # ---- 动作：与 server/relay/web.py 的接口一一对应 ----

    def play_episode(self, series: str, episode: int) -> dict:
        return self.post(f"/api/series/{urllib.parse.quote(series)}/play", {"episode": episode})

    def pause(self) -> dict:
        return self.post("/api/pause")

    def resume(self) -> dict:
        return self.post("/api/resume")

    def next(self) -> dict:
        return self.post("/api/next")

    def previous(self) -> dict:
        return self.post("/api/previous")

    def seek(self, delta: float | None = None, position: float | None = None) -> dict:
        return self.post("/api/seek", {"delta": delta} if position is None else {"position": position})
