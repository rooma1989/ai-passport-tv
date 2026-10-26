"""总后台频道表的「检测线路」：一个台的每条线路（主线路和备用线路，server/routes.py）能不能连上。

很轻：不开 ffmpeg，只取列表（跟着主列表到第一个子列表，至多 MAX_HOPS 层），再取最新的一个片段的头
SEGMENT_BYTES 字节（Range 请求）。地址不是列表（直接是 TS、FLV 流）就只读头几 KB。这样能分出最常见的
几种坏法：403 / 404（线路失效）、超时、片段取到一半断开（源站中途断开）、列表里没有片段。能连上不等于一定
能放（编码不对之类要真正解码才知道），页面上写「能连上」。

同时至多 CONCURRENCY 个台在测（页面的「检测全部」也只开两个并发），每条线路的每一步 TIMEOUT_S 秒、
整条至多 TOTAL_S 秒。只测频道表里已经保存的线路（按台 id），不接受页面给的任意地址：云端的中转也有这个
接口，管理员能让中转去连的地址和电视服务本来就会去连的一样。结果存进 meta 的 checks（routes.py），
页面再打开时还看得到。
"""

from __future__ import annotations

import http.client
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import routes
from . import channeltable

TIMEOUT_S = 6.0
TOTAL_S = 15.0
PLAYLIST_MAX = 512 * 1024
SEGMENT_BYTES = 16 * 1024
HEAD_BYTES = 4096
MAX_HOPS = 3
CONCURRENCY = 2
DEFAULT_USER_AGENT = "AptvPlayer-UA"
_running = threading.BoundedSemaphore(CONCURRENCY)


class _Failed(Exception):
    """这条线路不行：text 是原话（会经 routes.friendly 换成中文）。"""


def _read(response, limit: int, deadline: float, monotonic, data: bytes = b"", whole: bool = True) -> bytes:
    """接着 data 读到至多 limit 字节；whole 时连接提前断了（声明的长度没读够）当作中途断开。"""
    try:
        while len(data) < limit:
            if monotonic() > deadline:
                raise _Failed("timed out")
            chunk = response.read(min(16 * 1024, limit - len(data)))
            if not chunk:
                break
            data += chunk
    except http.client.IncompleteRead as error:
        raise _Failed("IncompleteRead: end of file") from error
    except (ConnectionError, OSError) as error:
        if isinstance(error, TimeoutError) or "timed out" in str(error):
            raise _Failed("timed out") from error
        raise _Failed(f"{type(error).__name__}: {error}") from error
    length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    if whole and length and length.isdigit() and len(data) < min(int(length), limit):
        raise _Failed("end of file")
    return data


def _get(url: str, agent: str, opener, deadline: float, monotonic, limit: int, ranged: bool = False,
         sniff: bool = False):
    """(跳转后的地址, 读到的字节)。sniff：先读 HEAD_BYTES，是列表（#EXTM3U）才接着读完（至多 limit），
    不是就停（直接是流的地址读不完）。"""
    headers = {"User-Agent": agent or DEFAULT_USER_AGENT}
    if ranged:
        headers["Range"] = f"bytes=0-{limit - 1}"
    request = urllib.request.Request(url, headers=headers)
    left = max(0.5, min(TIMEOUT_S, deadline - monotonic()))
    try:
        with opener(request, timeout=left) as response:
            final = response.geturl() if hasattr(response, "geturl") else url
            if not sniff:
                return final, _read(response, limit, deadline, monotonic)
            head = _read(response, HEAD_BYTES, deadline, monotonic)
            if not head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U"):
                return final, head
            return final, _read(response, limit, deadline, monotonic, head)
    except _Failed:
        raise
    except urllib.error.HTTPError as error:
        raise _Failed(f"HTTP error {error.code}") from error
    except urllib.error.URLError as error:
        raise _Failed(str(error.reason)) from error
    except (http.client.HTTPException, OSError, ValueError) as error:
        if isinstance(error, TimeoutError) or "timed out" in str(error):
            raise _Failed("timed out") from error
        raise _Failed(f"{type(error).__name__}: {error}") from error


def _playlist(text: str) -> tuple[list[str], list[str]]:
    """(子列表, 片段)：#EXT-X-STREAM-INF 后面的一行是子列表，别的不以 # 开头的行是片段。"""
    variants, segments, inf = [], [], False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            inf = True
            continue
        if line.startswith("#"):
            continue
        (variants if inf else segments).append(line)
        inf = False
    return variants, segments


def probe(url: str, agent: str = "", opener=urllib.request.urlopen, monotonic=time.monotonic) -> dict:
    """测一条线路：{ok, why（给人看的中文，ok 时为空）, code（routes.classify 的分类，ok 时为空）, ms}。"""
    started = monotonic()
    deadline = started + TOTAL_S
    try:
        current = url
        for _ in range(MAX_HOPS):
            final, head = _get(current, agent, opener, deadline, monotonic, PLAYLIST_MAX, sniff=True)
            text = head.decode("utf-8", "replace").lstrip("﻿").lstrip()
            if not text.startswith("#EXTM3U"):
                # 不是列表：直接是流（TS、FLV）。读到了一点东西就算能连上。
                if len(head) < 188:
                    raise _Failed("没有片段" if not head else "not a playlist")
                break
            variants, segments = _playlist(text)
            if variants:
                current = urllib.parse.urljoin(final, variants[0])
                continue
            if not segments:
                raise _Failed("没有片段")
            segment = urllib.parse.urljoin(final, segments[-1])
            _, data = _get(segment, agent, opener, deadline, monotonic, SEGMENT_BYTES, ranged=True)
            if not data:
                raise _Failed("end of file")
            break
        else:
            raise _Failed("not a playlist")
    except _Failed as failed:
        reason = str(failed)
        return {"ok": False, "why": routes.friendly(reason), "code": routes.classify(reason),
                "ms": int((monotonic() - started) * 1000), "raw": routes.redact(reason)}
    return {"ok": True, "why": "", "code": "", "ms": int((monotonic() - started) * 1000)}


def check_channel(path: Path, cid: str, opener=urllib.request.urlopen, clock=time.time, log=print) -> dict:
    """测频道表里这个台的每条线路（一条接一条），结果存进 meta 的 checks。
    {ok, id, results: [{url, ok, why, at, ms}]} 或 {ok: False, error}。"""
    row = next((r for r in channeltable.read(path)["rows"] if r["id"] == cid), None)
    if row is None:
        return {"ok": False, "error": "没有这个台（可能别人刚改过频道表，请刷新）"}
    lines = routes.table_routes(path, cid, row["url"], row["agent"], DEFAULT_USER_AGENT)
    results = []
    with _running:
        for n, (url, agent) in enumerate(lines, 1):
            found = probe(url, agent, opener)
            if not found["ok"]:
                log(f"检测线路：{row['name']} 线路 {n} 不行：{found['raw']}")
            results.append({"url": url, "ok": found["ok"], "why": found["why"], "at": int(clock()),
                            "ms": found["ms"]})
    marks = {routes.fingerprint(u) for u, _ in lines}

    def change(data: dict) -> None:
        entry = data.setdefault(cid, {})
        checks = {k: v for k, v in (entry.get("checks") or {}).items() if k in marks}
        for item in results:
            checks[routes.fingerprint(item["url"])] = {"ok": item["ok"], "why": item["why"][:routes.WHY_MAX],
                                                       "at": item["at"]}
        entry["checks"] = checks

    routes.update_meta(path, change)
    return {"ok": True, "id": cid, "results": results}
