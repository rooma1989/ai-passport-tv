"""把一个视频地址经 ffmpeg 封装成 MPEG-TS，按实时速度写给电视服务。

B 站 360p 的 MP4 已是 H.264 + AAC，只换封装不转码（2 核服务器）。`-re` 让输出按实时速度走，
所以已过的真实时间就是播放进度。
"""

from __future__ import annotations

import subprocess
import time

from .bilibili import REFERER, UA

CHUNK_BYTES = 64 * 1024


# B 站的视频服务器偶尔在播到一半时断开连接；让 ffmpeg 自己按字节位置接着下载，
# 而不是整路失败、由中转重开（那样电视上会跳一下）。读写 15 秒没动静也算断开。
RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
             "-reconnect_delay_max", "5", "-rw_timeout", "15000000"]


def ffmpeg_args(ffmpeg: str, url: str, offset: float) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
            "-user_agent", UA, "-headers", f"Referer: {REFERER}\r\n", *RECONNECT,
            "-ss", str(int(offset)), "-i", url,
            "-c", "copy", "-f", "mpegts", "pipe:1"]


def start(ffmpeg: str, url: str, offset: float) -> subprocess.Popen:
    # 参数列表、不经 shell；地址来自 B 站接口，不来自用户输入。
    return subprocess.Popen(ffmpeg_args(ffmpeg, url, offset), stdout=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pump(proc, write, on_tick, tick_s: float = 10.0, clock=time.monotonic) -> str:
    """复制到读完或对端断开；每 tick_s 秒回报一次已过秒数。

    返回 "ended"（ffmpeg 正常读完）、"failed"（ffmpeg 出错退出）或 "client_gone"。
    """
    began = clock()
    next_tick = began + tick_s
    outcome = None
    try:
        while True:
            chunk = proc.stdout.read1(CHUNK_BYTES)
            if not chunk:
                break
            try:
                write(chunk)
            except OSError:
                outcome = "client_gone"
                break
            now = clock()
            if now >= next_tick:
                on_tick(now - began)
                next_tick = now + tick_s
    finally:
        if outcome is None:
            code = proc.wait()
            outcome = "ended" if code == 0 else "failed"
        proc.kill()
        proc.wait()
    return outcome
