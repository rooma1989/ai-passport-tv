"""网页的静态文件（页面、样式、脚本）：启动时压一次 gzip，浏览器说收 gzip 就给压好的那份。

电脑版的 console.js 有近 400 KB、console.css 一百 KB，原来每次都原样发；压成 gzip 只剩四分之一左右。
- 只在启动时压一次（pack，和 console.py 算内容散列在同一处），回答时不再花 CPU；压了反而不小的（太小的、
  图片这类本来就压过的）不压。
- 请求的 Accept-Encoding 收 gzip（q 不是 0）才给压过的，带 Content-Encoding: gzip；能压的文件两种回答都带
  Vary: Accept-Encoding（中间的缓存按它分开存）。
- ETag 两种各一个（压过的后面多「-gz」），If-None-Match 对上了回 304（不带正文）。
- 带 Range 的请求一律给原样的（压过的字节和原文件的位置对不上；这里的静态文件本来也不分段给）。

只用标准库。
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass

# 这些类型才压：文字、脚本、清单、SVG。PNG、ICO 这类本来就压过，再压只会更大。
_TEXT_KINDS = ("text/", "application/javascript", "application/json", "application/manifest+json", "image/svg+xml")
# 比这还小的不压：省下的字节还不够多出来的头。
MIN_BYTES = 1024


@dataclass(frozen=True)
class Static:
    """一个静态文件：类型、原样的字节和 ETag；能压时 gz 是压好的字节（不能压时 None）、gz_etag 是它的 ETag。"""
    kind: str
    data: bytes
    etag: str
    gz: bytes | None = None
    gz_etag: str = ""


def compressible(kind: str) -> bool:
    return kind.split(";")[0].strip().lower().startswith(_TEXT_KINDS)


def pack(kind: str, data: bytes) -> Static:
    """启动时：算 ETag（内容散列），能压的压一份（mtime=0：同一份内容每次压出来一样）。"""
    digest = hashlib.sha256(data).hexdigest()[:16]
    packed = None
    if compressible(kind) and len(data) >= MIN_BYTES:
        packed = gzip.compress(data, compresslevel=9, mtime=0)
        if len(packed) >= len(data):
            packed = None
    return Static(kind, data, f'"{digest}"', packed, f'"{digest}-gz"' if packed is not None else "")


def accepts_gzip(header: str | None) -> bool:
    """Accept-Encoding 收不收 gzip：写了 gzip（或 x-gzip）看它的 q；没写时看 *；q=0 是不要。"""
    if not header:
        return False
    named = star = None
    for part in header.split(","):
        coding, _, params = part.strip().partition(";")
        coding = coding.strip().lower()
        q = 1.0
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0
        if coding in ("gzip", "x-gzip"):
            named = q if named is None else max(named, q)
        elif coding == "*":
            star = q
    if named is not None:
        return named > 0
    return star is not None and star > 0


def _matches(header: str | None, etag: str) -> bool:
    """If-None-Match 里有没有这个 ETag（弱比较：W/ 不算区别）；「*」都算。"""
    if not header:
        return False
    if header.strip() == "*":
        return True
    bare = etag.removeprefix("W/")
    return any(tag.strip().removeprefix("W/") == bare for tag in header.split(","))


def choose(item: Static, request_headers) -> tuple[bytes, dict]:
    """按请求挑一份：(正文, 要加的头 ETag / Vary / Content-Encoding)。"""
    use_gz = item.gz is not None and not request_headers.get("Range") \
        and accepts_gzip(request_headers.get("Accept-Encoding"))
    headers = {"ETag": item.gz_etag if use_gz else item.etag}
    if item.gz is not None:
        headers["Vary"] = "Accept-Encoding"
    if use_gz:
        headers["Content-Encoding"] = "gzip"
        return item.gz, headers
    return item.data, headers


def send(handler, item: Static, headers: dict | None = None) -> None:
    """回答一个静态文件（handler 是 web.py 的处理类，用它的 _send）。headers：Cache-Control、CSP 之类，原样带上。"""
    body, extra = choose(item, handler.headers)
    if _matches(handler.headers.get("If-None-Match"), extra["ETag"]):
        # 304：不带正文；Content-Length 写成 200 时会有的长度（RFC 9110 允许），前面的 front.py 才不会等到连接关掉。
        handler.send_response(304)
        handler.send_header("Content-Length", str(len(body)))
        for name in ("ETag", "Vary"):
            if name in extra:
                handler.send_header(name, extra[name])
        cache = (headers or {}).get("Cache-Control")
        handler.send_header("Cache-Control", cache or "no-store")
        handler.end_headers()
        return
    handler._send(200, body, item.kind, {**(headers or {}), **extra})
