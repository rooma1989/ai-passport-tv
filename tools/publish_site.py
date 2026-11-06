#!/usr/bin/env python3
"""Publish the RooTV website to Qiniu (site/ in the same bucket as the update files).

    python3 tools/publish_site.py [--dry-run]

Builds with tools/build_site.py, uploads assets and data first and the HTML
last, each with its content type, then asks the CDN to drop its copies of the
HTML and JSON (the pages also fetch their JSON with a time parameter). Qiniu
settings: ~/.config/xiaozhi-tv/qiniu.json (see tools/publish_update.py).
tools/publish_update.py runs this after a release unless --no-site.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from tools.console import use_utf8  # noqa: E402

import build_site  # noqa: E402
import publish_update  # noqa: E402

PREFIX = "site"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
         ".svg": "image/svg+xml", ".ico": "image/x-icon", ".png": "image/png"}
REFRESH_MAX = 60        # 七牛一次刷新最多这么多个地址


def content_type(path: Path) -> str:
    return TYPES.get(path.suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def publish(store, site: Path, say=print) -> list[str]:
    """传上去，返回刷新了的地址。"""
    refresh = []
    for path in build_site.files_of(site):
        rel = path.relative_to(site).as_posix()
        store.put(f"{PREFIX}/{rel}", path.read_bytes(), path.name, content_type(path))
        if path.suffix in (".html", ".json"):
            refresh.append(f"{store.domain}/{PREFIX}/{rel}")
    refresh.append(f"{store.domain}/{PREFIX}/")
    store.refresh(refresh[:REFRESH_MAX])
    say(f"官网传好了：{store.domain}/{PREFIX}/index.html")
    return refresh


def main(argv: list[str] | None = None, opener=None) -> int:
    use_utf8()
    parser = argparse.ArgumentParser(prog="publish_site.py")
    parser.add_argument("--qiniu", type=Path, default=publish_update.QINIU)
    parser.add_argument("--dry-run", action="store_true", help="只生成，不上传")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        site = build_site.build(Path(tmp))
        print(f"生成了 {len(build_site.files_of(site))} 个文件")
        if args.dry_run:
            return 0
        try:
            store = publish_update.Qiniu(publish_update.read_qiniu(args.qiniu), opener=opener)
            publish(store, site)
        except publish_update.PublishError as error:
            print(f"官网没发出去：{error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
