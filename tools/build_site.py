#!/usr/bin/env python3
"""Build the RooTV website (packaging/website/) into build-site/site/.

    python3 tools/build_site.py [--out build-site] [--manifest rootv-update.json]

- the HTML pages are fixed shells (SHELLS) whose inline boot code fetches
  data/build.json with a time parameter; it names style.<hash>.css,
  app.<hash>.js and data/pages.<hash>.json (header, body, footer from
  packaging/website/pages/). Qiniu sends a one-year Cache-Control for every
  file, so nothing a browser may keep that long ever changes;
- data/site.json is packaging/website/content.json;
- data/changelog.json is docs/CHANGELOG.zh_CN.md as sections of items;
- the icons come from packaging/site/assets/, the screenshots from
  packaging/website/shots/ (hashed names, their paths written into site.json).

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
PAGES = ("index.html", "download.html", "changelog.html")      # 生成的外壳（SHELLS）
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


# 三个页面的外壳：只有标题、图标和一段内联的启动代码，永远不用改。七牛给所有文件都带一年的 Cache-Control，
# 浏览器把外壳缓存一年也没关系：启动代码每次带时间参数（每 5 分钟变一次）去取 data/build.json，里面写着现在的
# 样式、脚本和页面内容（pages.<散列>.json：导航、正文、页脚）各是哪个带散列的文件。
SHELLS = {
    "index.html": ("home", "小鼠电视 · 把家里的电脑变成小电视的电视台",
                   "小鼠电视（RooTV）电脑客户端：直播、B 站、YouTube、网盘片库，送到家里的小电视，也能在电脑上看。免费、开源。"),
    "download.html": ("download", "下载 · 小鼠电视", "下载小鼠电视电脑客户端：Mac（Apple 芯片、Intel）、Windows、Linux。"),
    "changelog.html": ("changelog", "更新日志 · 小鼠电视", "小鼠电视每个版本改了什么。"),
}
BOOT = ("(function(){var t=Math.floor(Date.now()/3e5);"
        "fetch('data/build.json?t='+t,{cache:'no-store'}).then(function(r){if(!r.ok)throw r;return r.json()})"
        ".then(function(b){window.ROOTV_BUILD=b;var l=document.createElement('link');l.rel='stylesheet';l.href=b.style;"
        "l.onload=l.onerror=function(){var s=document.createElement('script');s.src=b.app;document.head.appendChild(s)};"
        "document.head.appendChild(l)})"
        ".catch(function(){document.body.insertAdjacentHTML('beforeend','<p style=\"font:16px sans-serif;text-align:center;"
        "margin-top:30vh\">页面没加载出来，刷新一下试试。</p>')})})();")
SHELL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#fbfbfd" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="icon" href="assets/favicon.svg" type="image/svg+xml">
<link rel="icon" href="assets/favicon.ico" sizes="32x32">
<link rel="apple-touch-icon" href="assets/apple-touch-icon.png">
<style>body{{margin:0;background:#fbfbfd}}@media (prefers-color-scheme:dark){{body{{background:#000;color:#f5f5f7}}}}</style>
<script>{boot}</script>
</head>
<!-- 小鼠电视官网（packaging/website/，tools/build_site.py 生成）。这是不会变的外壳，内容见 data/build.json。 -->
<body data-page="{page}">
<noscript><p style="text-align:center;margin-top:30vh">这个页面要开着 JavaScript 才能显示。下载也可以直接去
  <a href="https://github.com/rooma1989/ai-passport-tv/releases">GitHub 发布页</a>。</p></noscript>
</body>
</html>
"""


def shell(page: str, title: str, desc: str) -> str:
    return SHELL.format(page=page, title=title, desc=desc, boot=BOOT)


def _hashed(site: Path, folder: str, name: str, data: bytes) -> str:
    stem, ext = name.rsplit(".", 1)
    rel = f"{folder}/{stem}.{digest(data)}.{ext}" if folder else f"{stem}.{digest(data)}.{ext}"
    (site / rel).parent.mkdir(parents=True, exist_ok=True)
    (site / rel).write_bytes(data)
    return rel


def build(out: Path, manifest: Path | None = None) -> Path:
    site = out / "site"
    if site.exists():
        shutil.rmtree(site)
    (site / "data").mkdir(parents=True)
    (site / "assets").mkdir()
    built = {name.split(".")[0]: _hashed(site, "", name, (SOURCE / name).read_bytes()) for name in HASHED}
    pages = {name: (SOURCE / "pages" / f"{name}.html").read_text(encoding="utf-8")
             for name in ("header", "footer", "home", "download", "changelog")}
    built["pages"] = _hashed(site, "data", "pages.json", json.dumps(pages, ensure_ascii=False).encode("utf-8"))
    (site / "data" / "build.json").write_text(json.dumps(built), encoding="utf-8")
    for name, (page, title, desc) in SHELLS.items():
        (site / name).write_text(shell(page, title, desc), encoding="utf-8")
    content = json.loads((SOURCE / "content.json").read_text(encoding="utf-8"))
    # 截图：带内容散列的文件名（换了图就换了地址），路径写进 site.json。
    for shot in content.get("shots", []):
        shot["src"] = _hashed(site, "assets/shots", shot["file"], (SOURCE / "shots" / shot["file"]).read_bytes())
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
    """上传的顺序：先带散列的文件和数据，再 data/build.json（它一换，页面就用新的文件），最后外壳 HTML。"""
    found = sorted(p for p in site.rglob("*") if p.is_file())
    last = [p for p in found if p.name == "build.json"] + [p for p in found if p.suffix == ".html"]
    return [p for p in found if p not in last] + last


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
