"""The website (packaging/website/, tools/build_site.py, tools/publish_site.py): hashed
asset names, the JSON files, the changelog sections, the upload order and types,
and the page script's pure functions (tests/website_test.mjs)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import build_site  # noqa: E402
import publish_site  # noqa: E402


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build(self):
        site = build_site.build(self.out)
        built = json.loads((site / "data" / "build.json").read_text(encoding="utf-8"))
        self.assertRegex(built["app"], r"^app\.[0-9a-f]{10}\.js$")
        self.assertRegex(built["style"], r"^style\.[0-9a-f]{10}\.css$")
        self.assertRegex(built["pages"], r"^data/pages\.[0-9a-f]{10}\.json$")
        for rel in built.values():
            self.assertTrue((site / rel).is_file(), rel)
        pages = json.loads((site / built["pages"]).read_text(encoding="utf-8"))
        self.assertEqual(set(pages), {"header", "footer", "home", "download", "changelog"})
        self.assertIn('id="featureList"', pages["home"])
        for page in build_site.PAGES:
            html = (site / page).read_text(encoding="utf-8")
            self.assertIn("data/build.json?t=", html)
            self.assertNotIn("app.", html.split("<script>")[0], "the shell names no hashed file")
            for ref in re.findall(r'(?:src|href)="([^":#]+)"', html):
                self.assertTrue((site / ref).is_file(), f"{page} points at {ref}")
        content = json.loads((site / "data" / "site.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(content["features"]), 6)
        for shot in content["shots"]:
            self.assertTrue((site / shot["src"]).is_file(), shot["src"])
        log = json.loads((site / "data" / "changelog.json").read_text(encoding="utf-8"))
        self.assertTrue(log["sections"])
        self.assertFalse((self.out / "rootv").exists())

    def test_shells_do_not_change_with_the_content(self):
        first = {p: (build_site.build(self.out) / p).read_text(encoding="utf-8") for p in build_site.PAGES}
        original = (build_site.SOURCE / "style.css").read_text(encoding="utf-8")
        try:
            (build_site.SOURCE / "style.css").write_text(original + "\n/* changed */\n", encoding="utf-8")
            site = build_site.build(self.out)
            self.assertEqual({p: (site / p).read_text(encoding="utf-8") for p in build_site.PAGES}, first,
                             "a browser may keep the shells for a year: they must not depend on the content")
        finally:
            (build_site.SOURCE / "style.css").write_text(original, encoding="utf-8")

    def test_preview_manifest(self):
        manifest = self.out / "m.json"
        manifest.write_text("{}")
        build_site.build(self.out, manifest)
        self.assertTrue((self.out / "rootv" / "latest" / "rootv-update.json").is_file())

    def test_changelog_sections(self):
        text = ("# Changelog\n\n## 未发布\n\n- **一**：第一行\n  接着写\n- 二\n\n### 小标题\n- 三\n\n"
                "## v1.2.0\n- 旧的\n\n## B02R 试验\n- 内部的\n")
        sections = build_site.changelog_sections(text)
        self.assertEqual([s["title"] for s in sections], ["未发布", "v1.2.0"], "internal notes are left out")
        self.assertEqual(sections[0]["items"], ["**一**：第一行 接着写", "二", "三"])

    def test_upload_order(self):
        site = build_site.build(self.out)
        order = [p.relative_to(site).as_posix() for p in build_site.files_of(site)]
        self.assertEqual([n.rsplit(".", 1)[-1] for n in order[-3:]], ["html"] * 3, "the shells go last")
        self.assertEqual(order[-4], "data/build.json", "build.json switches to the new files after they are up")


class FakeStore:
    domain = "https://cdn.example.com"

    def __init__(self):
        self.puts, self.refreshed = [], []

    def put(self, key, data, name, mime="application/octet-stream"):
        self.puts.append((key, mime))

    def refresh(self, urls):
        self.refreshed = list(urls)


class PublishTests(unittest.TestCase):
    def test_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = build_site.build(Path(tmp))
            store = FakeStore()
            publish_site.publish(store, site, say=lambda _m: None)
        types = dict(store.puts)
        self.assertEqual(types["site/index.html"], "text/html; charset=utf-8")
        self.assertEqual(types["site/data/site.json"], "application/json; charset=utf-8")
        self.assertTrue(any(k.endswith(".js") and v.startswith("text/javascript") for k, v in types.items()))
        self.assertIn("https://cdn.example.com/site/data/changelog.json", store.refreshed)
        self.assertIn("https://cdn.example.com/site/download.html", store.refreshed)
        self.assertIn("https://cdn.example.com/site/data/build.json", store.refreshed)
        self.assertFalse(any(u.endswith(".js") for u in store.refreshed), "hashed files never need a refresh")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ScriptTests(unittest.TestCase):
    def test_page_script(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "website_test.mjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
