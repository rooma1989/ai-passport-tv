"""The self-hosted image's build files ship no content (packaging/docker/, /.dockerignore).

Phase S1d of docs/superpowers/specs/2026-10-15-self-hosting-design.md, sections
4.2 and 8.1. Docker is not needed: the build context is reproduced from the
Dockerfile's COPY lines and .dockerignore's rules (tools/selfhost_content_check.py
--context), and checked for channel tables, the built-in channel addresses, the
community playlist address, cookies and keys. CI repeats the check on the built
image (.github/workflows/build-image.yml).
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import selfhost_content_check as check  # noqa: E402

DOCKERFILE = ROOT / "packaging" / "docker" / "Dockerfile"
COMPOSE = ROOT / "packaging" / "docker" / "compose.yaml"


class BuildContextTests(unittest.TestCase):
    def test_context_has_the_server_and_no_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            copied = check.build_context(ROOT, Path(tmp))
            self.assertIn("server/selfhost/__main__.py", copied)
            self.assertIn("server/front.py", copied)
            self.assertIn("server/relay/assets/setup.html", copied)
            self.assertIn("packaging/help/index.html", copied)
            self.assertNotIn("server/builtin_channels.py", copied)
            self.assertFalse(any(c.endswith("channels.txt") for c in copied))
            self.assertFalse(any(c.startswith(("tools/", "main/", "tests/")) for c in copied))
            self.assertEqual(check.check_tree(Path(tmp), check.forbidden_strings()), [])

    def test_the_check_catches_content(self):
        strings = check.forbidden_strings()
        self.assertTrue(any("m3u" in s for s in strings), "the community playlist address is on the list")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server").mkdir()
            (root / "server" / "x.py").write_text(f"URL = '{strings[0]}'\n", encoding="utf-8")
            (root / "channels.txt").write_text("", encoding="utf-8")
            (root / "relay-key").write_text("k", encoding="utf-8")
            problems = check.check_tree(root, strings)
        self.assertEqual(len(problems), 3, problems)

    def test_ignore_rules(self):
        rules = check.ignore_rules(ROOT / ".dockerignore")
        for path in ("server/live.py", "server/relay/web.py", "packaging/help/index.html", "LICENSE",
                     "packaging/docker/NOTICE"):
            self.assertFalse(check.excluded(path, rules), path)
        for path in ("channels.txt", "server/builtin_channels.py", "server/x/channels.txt", "tools/channel_config.py",
                     "main/av_private_config.h", "server/__pycache__/live.cpython-312.pyc", "server/.local/a",
                     "packaging/docker/data/tv.db", "packaging/systemd/tv-relay.service", "docs/README.md",
                     "server/relay/cookies.txt", "server/a.pem"):
            self.assertTrue(check.excluded(path, rules), path)


class DockerfileTests(unittest.TestCase):
    def test_copies_only_the_allow_list(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        sources = re.findall(r"^COPY\s+(\S+)\s+\S+\s*$", text, re.MULTILINE)
        self.assertEqual(set(sources), set(check.COPIED), "the content check must cover every COPY")
        self.assertIn("USER 1000:1000", text)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--"]', text)
        self.assertIn("HEALTHCHECK", text)
        self.assertIn("python:3.12-slim", text)

    def test_compose_is_lan_only_and_unprivileged(self):
        text = COMPOSE.read_text(encoding="utf-8")
        for port in ("8080:8080", "8097:8097", "8098:8098"):
            self.assertIn(f'"{port}"', text)
        self.assertNotIn('"8081:8081"', text, "the tunnel port is never published on the host")
        for line in ("read_only: true", "cap_drop:", "no-new-privileges:true", "./data:/data"):
            self.assertIn(line, text)
        self.assertEqual(check.check_tree(COMPOSE.parent, check.forbidden_strings()), [])


if __name__ == "__main__":
    unittest.main()
