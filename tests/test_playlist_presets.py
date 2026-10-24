"""The public playlist and programme-guide presets offered on the admin backend's Channels page
(server/relay/presets.py).

Only playlist links, never stream addresses: the project ships no content
(docs/superpowers/specs/2026-10-15-self-hosting-design.md, section 8). The table
must be well-formed, use https, stay within the import's own limits, and not
contain the community playlist the self-host content check forbids. Nothing
here goes to the network.
"""

import re
import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from server.relay import playlist, presets  # noqa: E402

import selfhost_content_check  # noqa: E402

FIELDS = {"id", "name", "url", "mirrors", "maintainer", "homepage", "description"}


def https(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and " " not in url


class PresetTableTests(unittest.TestCase):
    def test_every_preset_is_well_formed(self):
        self.check_table(presets.PRESETS, r"\.m3u$")

    def test_every_guide_preset_is_well_formed(self):
        # XMLTV programme guides: an .xml (possibly served gzip-compressed), never a playlist or a stream.
        self.check_table(presets.EPG_PRESETS, r"\.xml(\.gz)?$")
        ids = [p["id"] for p in presets.PRESETS + presets.EPG_PRESETS]
        self.assertEqual(len(ids), len(set(ids)), "guide ids differ from playlist ids")

    def check_table(self, table, path_pattern):
        self.assertGreaterEqual(len(table), 2)
        ids = [p["id"] for p in table]
        self.assertEqual(len(ids), len(set(ids)), "ids are unique")
        for preset in table:
            with self.subTest(preset=preset["id"]):
                self.assertEqual(set(preset), FIELDS)
                self.assertRegex(preset["id"], r"^[a-z0-9][a-z0-9-]{1,39}$")
                for key in ("name", "maintainer", "description"):
                    self.assertIsInstance(preset[key], str)
                    self.assertTrue(preset[key].strip(), key)
                self.assertLessEqual(len(preset["name"]), 40)
                self.assertTrue(https(preset["homepage"]), preset["homepage"])
                urls = [preset["url"]] + [url for _label, url in preset["mirrors"]]
                self.assertEqual(len(urls), len(set(urls)), "a mirror is not the main address again")
                for label, _url in preset["mirrors"]:
                    self.assertTrue(label.strip())
                for url in urls:
                    self.assertTrue(https(url), url)
                    self.assertLessEqual(len(url), playlist.URL_FIELD_MAX)
                    # A playlist link, not a stream: .m3u, never an .m3u8 or a media file.
                    self.assertRegex(urllib.parse.urlsplit(url).path, path_pattern, url)

    def test_no_forbidden_content(self):
        text = (ROOT / "server" / "relay" / "presets.py").read_text(encoding="utf-8")
        for needle in selfhost_content_check.forbidden_strings(ROOT):
            self.assertNotIn(needle, text)
        # The only addresses in the file are the presets' own links and homepages.
        every = presets.PRESETS + presets.EPG_PRESETS
        listed = {p["homepage"] for p in every} | {p["url"] for p in every} | \
            {url for p in every for _label, url in p["mirrors"]}
        self.assertEqual(set(re.findall(r"https?://[^\s\"')]+", text.split('"""', 2)[2])), listed)

    def test_public_view_and_lookup(self):
        shown = presets.public()
        self.assertEqual([p["id"] for p in shown], [p["id"] for p in presets.PRESETS])
        for preset, view in zip(presets.PRESETS, shown):
            self.assertEqual([u["url"] for u in view["urls"]],
                             [preset["url"]] + [url for _label, url in preset["mirrors"]])
            for index, item in enumerate(view["urls"]):
                self.assertEqual(presets.url_of(preset["id"], index), item["url"])
            self.assertIsNone(presets.url_of(preset["id"], len(view["urls"])))
        self.assertIsNone(presets.url_of("nope"))
        self.assertIsNone(presets.url_of(presets.PRESETS[0]["id"], -1))
        self.assertIsNone(presets.url_of(presets.PRESETS[0]["id"], "0"))
        self.assertEqual([p["id"] for p in presets.epg_public()], [p["id"] for p in presets.EPG_PRESETS])
        self.assertEqual(presets.url_of(presets.EPG_PRESETS[0]["id"], 0, presets.EPG_PRESETS),
                         presets.EPG_PRESETS[0]["url"])
        self.assertIsNone(presets.url_of(presets.EPG_PRESETS[0]["id"]), "guides are not playlists")


if __name__ == "__main__":
    unittest.main()
