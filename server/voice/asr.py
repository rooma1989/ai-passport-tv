"""到 muse-asr（SenseVoice）的桥：本机 Unix 套接字上的一个普通 HTTP 请求。

媒体服务只用标准库，而 muse-asr 原有的接口要 AES-GCM（cryptography 包）。所以 muse-asr
另开一个 Unix 套接字（默认 /run/muse-asr/asr.sock，0660，组 tvserver），讲明文：

    POST /asr HTTP/1.1          正文：16 kHz 单声道 PCM16 小端
    X-Sample-Rate: 16000
    → 200 {"text": "…"}

认证就是文件权限：能连上这个套接字的只有 tvserver 组，不用共享密钥。
"""

from __future__ import annotations

import http.client
import json
import socket

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


class AsrClient:
    def __init__(self, path: str, timeout: float = TIMEOUT_S):
        self.path = path
        self.timeout = timeout

    def transcribe(self, pcm: bytes, rate: int = SAMPLE_RATE) -> str:
        if not pcm:
            return ""
        if len(pcm) > MAX_BYTES or len(pcm) % 2:
            raise AsrError("audio length out of range")
        connection = _UnixConnection(self.path, self.timeout)
        try:
            connection.request("POST", "/asr", body=pcm,
                               headers={"Content-Type": "application/octet-stream",
                                        "X-Sample-Rate": str(rate)})
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
