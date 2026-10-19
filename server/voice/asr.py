"""到 muse-asr（SenseVoice）的桥：本机 Unix 套接字上的一个普通 HTTP 请求。

媒体服务只用标准库，而 muse-asr 原有的接口要 AES-GCM（cryptography 包）。所以 muse-asr
另开一个 Unix 套接字（默认 /run/muse-asr/asr.sock，0660，组 tvserver），讲明文：

    POST /asr HTTP/1.1          正文：16 kHz 单声道 PCM16 小端
    X-Sample-Rate: 16000
    → 200 {"text": "…"}

认证就是文件权限：能连上这个套接字的只有 tvserver 组，不用共享密钥。

电脑客户端（docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md 第 5.9 节）：Windows 上的 CPython
没有 Unix 套接字，本机的识别服务改听回环 TCP（TV_ASR_URL=http://127.0.0.1:<端口>）。本机别的程序也能连
回环端口，所以请求带一个头 X-Asr-Key：由中转密钥派生（asr_key），只能用来识别，泄露了也拿不到中转密钥本身
（端口被别的程序占了时，发出去的就是它）。云端照旧用 Unix 套接字。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import urllib.parse

SAMPLE_RATE = 16000
# 识别本身在 1 核主机上约零点几秒；留够余量，又要比设备等结果的 8 秒短，让设备先看到错误。
TIMEOUT_S = 6.0
# muse-asr 那边一次最多收 20 秒；这里只会发 10 秒以内。
MAX_BYTES = 20 * SAMPLE_RATE * 2


class AsrError(RuntimeError):
    """识别服务连不上、超时或回答不对；消息里不含音频和识别结果。"""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("muse-asr", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


KEY_HEADER = "X-Asr-Key"
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def asr_key(relay_key: str) -> str:
    """回环 TCP 上的识别服务认的口令：中转密钥派生的，只给识别用。"""
    return hmac.new(relay_key.strip().encode("utf-8"), b"xiaozhi-tv asr v1", hashlib.sha256).hexdigest()


def loopback_url(url: str) -> tuple[str, int]:
    """http://127.0.0.1:<端口> → (主机, 端口)；不是本机回环地址的一律 ValueError（识别的音频不出这台电脑）。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK or not parts.port:
        raise ValueError("TV_ASR_URL must be http://127.0.0.1:<port>")
    return parts.hostname, parts.port


class AsrClient:
    """path：Unix 套接字（云端的 muse-asr）；url：回环 TCP（电脑客户端），这时 key 是 asr_key() 的结果。"""

    def __init__(self, path: str = "", timeout: float = TIMEOUT_S, url: str = "", key: str = ""):
        self.path = path
        self.timeout = timeout
        self.address = loopback_url(url) if url else None
        self.key = key
        if not path and self.address is None:
            raise ValueError("AsrClient needs a socket path or a loopback URL")

    def _connection(self) -> http.client.HTTPConnection:
        if self.address is not None:
            return http.client.HTTPConnection(self.address[0], self.address[1], timeout=self.timeout)
        return _UnixConnection(self.path, self.timeout)

    def transcribe(self, pcm: bytes, rate: int = SAMPLE_RATE) -> str:
        if not pcm:
            return ""
        if len(pcm) > MAX_BYTES or len(pcm) % 2:
            raise AsrError("audio length out of range")
        connection = self._connection()
        headers = {"Content-Type": "application/octet-stream", "X-Sample-Rate": str(rate)}
        if self.address is not None and self.key:
            headers[KEY_HEADER] = self.key
        try:
            connection.request("POST", "/asr", body=pcm, headers=headers)
            response = connection.getresponse()
            body = response.read(64 * 1024)
            status = response.status
        except (OSError, http.client.HTTPException) as error:
            raise AsrError(f"asr unreachable: {type(error).__name__}") from None
        finally:
            connection.close()
        if status != 200:
            raise AsrError(f"asr status {status}")
        try:
            text = json.loads(body.decode("utf-8")).get("text")
        except (UnicodeError, ValueError, AttributeError):
            raise AsrError("asr answer is not JSON") from None
        if not isinstance(text, str):
            raise AsrError("asr answer has no text")
        return text.strip()
