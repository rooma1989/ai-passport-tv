#!/usr/bin/env python3
"""Build the RooTV website (packaging/website/) into build-site/site/.

    python3 tools/build_site.py [--out build-site] [--manifest rootv-update.json]

- the HTML shells are copied with style.css and app.js renamed to
  style.<hash>.css / app.<hash>.js (a changed file gets a new name, so a CDN
  that still caches the old one cannot serve it to the new page);
- data/site.json is packaging/website/content.json;
- data/changelog.json is docs/CHANGELOG.zh_CN.md as sections of items;
- the icons come from packaging/site/assets/.

--manifest also writes build-site/rootv/latest/rootv-update.json, for looking
at the download page locally (python3 -m http.server -d build-site). The real
manifest is published by tools/publish_update.py. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.console import use_utf8  # noqa: E402

SOURCE = ROOT / "packaging" / "website"
ICONS = ROOT / "packaging" / "site" / "assets"
CHANGELOG = ROOT / "docs" / "CHANGELOG.zh_CN.md"
PAGES = ("index.html", "download.html", "changelog.html")
HASHED = ("style.css", "app.js")
ICON_FILES = ("favicon.svg", "favicon.ico", "apple-touch-icon.png")
SECTIONS_MAX = 12


def changelog_sections(text: str, limit: int = SECTIONS_MAX) -> list[dict]:
    """「## 标题」下面「- 」开头的条目（缩进的续行接上去）→ [{"title", "items"}]，最多 limit 节。"""
    sections: list[dict] = []
    item: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if len(sections) >= limit:
                break
            sections.append({"title": line[3:].strip(), "items": []})
            item = None
            continue
        if not sections or line.startswith("#"):
            item = None
            continue
        if line.startswith("- "):
            item = [line[2:].strip()]
            sections[-1]["items"].append(item)
        elif line.strip() and item is not None and raw.startswith((" ", "\t")):
            item.append(line.strip())
        elif not line.strip():
            item = None
    for section in sections:
        section["items"] = [" ".join(parts) for parts in section["items"]]
    # 只要「未发布」和带版本号的节（别的是内部的试验记录）。
    return [s for s in sections if s["items"] and re.match(r"^(未发布|v?\d+\.\d+)", s["title"])]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:10]


def build(out: Path, manifest: Path | None = None) -> Path:
    site = out / "site"
    if site.exists():
        shutil.rmtree(site)
    (site / "data").mkdir(parents=True)
    (site / "assets").mkdir()
    names = {}
    for name in HASHED:
        data = (SOURCE / name).read_bytes()
        stem, ext = name.rsplit(".", 1)
        hashed = f"{stem}.{digest(data)}.{ext}"
        (site / hashed).write_bytes(data)
        names[name] = hashed
    for page in PAGES:
        html = (SOURCE / page).read_text(encoding="utf-8")
        for name, hashed in names.items():
            html = html.replace(f'"{name}"', f'"{hashed}"')
        (site / page).write_text(html, encoding="utf-8")
    content = json.loads((SOURCE / "content.json").read_text(encoding="utf-8"))
    (site / "data" / "site.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    log = {"sections": changelog_sections(CHANGELOG.read_text(encoding="utf-8"))}
    (site / "data" / "changelog.json").write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")
    for icon in ICON_FILES:
        shutil.copyfile(ICONS / icon, site / "assets" / icon)
    if manifest is not None:
        latest = out / "rootv" / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest, latest / "rootv-update.json")
    return site


def files_of(site: Path) -> list[Path]:
    """上传的顺序：先资源和数据，最后 HTML（新的 HTML 不会指向还没传上去的文件）。"""
    found = sorted(p for p in site.rglob("*") if p.is_file())
    return [p for p in found if p.suffix != ".html"] + [p for p in found if p.suffix == ".html"]


def main(argv: list[str] | None = None) -> int:
    use_utf8()
    parser = argparse.ArgumentParser(prog="build_site.py")
    parser.add_argument("--out", type=Path, default=ROOT / "build-site")
    parser.add_argument("--manifest", type=Path, help="本机预览下载页用的版本清单")
    args = parser.parse_args(argv)
    site = build(args.out, args.manifest)
    print(f"官网生成在 {site}（{len(files_of(site))} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
