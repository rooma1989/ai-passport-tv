#!/usr/bin/env python3
"""Check that a self-hosted image (or a copy of its build context) ships no content.

The image must contain no channel list, no built-in channel addresses, no
community playlist address, no cookies and no keys
(docs/superpowers/specs/2026-10-15-self-hosting-design.md, sections 4.2 and 8.1).

    python3 tools/selfhost_content_check.py <root>            # a directory tree
    python3 tools/selfhost_content_check.py --context         # what .dockerignore lets in

CI runs it against the built image's /app (.github/workflows/build-image.yml);
tests/test_selfhost_image.py runs the --context form on every test run, so a
change to .dockerignore that would let content in fails before an image exists.

The forbidden strings are read from this checkout -- the built-in channels in
server/builtin_channels.py and DEFAULT_SOURCE in tools/channel_config.py -- so the
list cannot drift from what it guards. The script is not copied into the image.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# File names that must not exist anywhere in the image.
FORBIDDEN_NAMES = re.compile(r"^(channels\.txt(\..*)?|relay-key|wifi-key|setup-code|token|cookies.*\.txt|"
                             r"cstone\.json|youtube-node\.json|av_private_config\.h|builtin_channels\.py|"
                             r".*\.pem|.*\.key|.*\.db|.*\.m3u8?)$")
# What the Dockerfile copies, relative to the build context.
COPIED = ("LICENSE", "packaging/docker/NOTICE", "server", "packaging/help", "packaging/flash",
          "packaging/docker/firmware")
URL_RE = re.compile(r"https?://[^\s\"'|]+")
SCAN_LIMIT = 8 * 1024 * 1024


def forbidden_strings(root: Path = ROOT) -> list[str]:
    """The built-in channel addresses and the community playlist address, from this checkout."""
    found = URL_RE.findall((root / "server" / "builtin_channels.py").read_text(encoding="utf-8"))
    source = re.search(r'^DEFAULT_SOURCE\s*=\s*"([^"]+)"', (root / "tools" / "channel_config.py")
                       .read_text(encoding="utf-8"), re.MULTILINE)
    if source:
        found.append(source.group(1))
    if len(found) < 2:
        raise SystemExit("could not read the forbidden addresses from this checkout")
    return sorted(set(found))


def check_tree(tree: Path, strings: list[str]) -> list[str]:
    problems = []
    needles = [s.encode("utf-8") for s in strings]
    for directory, _dirs, files in os.walk(tree):
        for name in files:
            path = Path(directory) / name
            shown = path.relative_to(tree)
            if FORBIDDEN_NAMES.match(name):
                problems.append(f"{shown}: file must not be in the image")
                continue
            try:
                if path.is_symlink() or path.stat().st_size > SCAN_LIMIT:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            for needle in needles:
                if needle in data:
                    problems.append(f"{shown}: contains {needle.decode()}")
    return problems


# ---- .dockerignore, the way Docker reads it (enough of it for this file) ----

def _pattern(text: str) -> re.Pattern:
    out, i = "", 0
    while i < len(text):
        if text.startswith("**", i):
            out += ".*"
            i += 2
            if text.startswith("/", i):
                out = out[:-2] + "(?:.*/)?"
                i += 1
        elif text[i] == "*":
            out += "[^/]*"
            i += 1
        elif text[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(text[i])
            i += 1
    return re.compile(out + "$")


def ignore_rules(path: Path) -> list[tuple[bool, re.Pattern]]:
    rules = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        line = line[1:] if negate else line
        rules.append((negate, _pattern(line.strip("/"))))
    return rules


def excluded(relative: str, rules) -> bool:
    """Last matching rule wins; a rule matching a parent directory matches its contents."""
    parts = relative.split("/")
    prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    result = False
    for negate, pattern in rules:
        if any(pattern.match(prefix) for prefix in prefixes):
            result = not negate
    return result


def build_context(root: Path, target: Path) -> list[str]:
    """Copy what the Dockerfile copies, after .dockerignore, into target. Returns the copied paths."""
    rules = ignore_rules(root / ".dockerignore")
    copied = []
    for item in COPIED:
        source = root / item
        paths = [source] if source.is_file() else [p for p in source.rglob("*") if p.is_file()]
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if excluded(relative, rules):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied.append(relative)
    return copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tree", nargs="?", type=Path, help="directory to check (the image's /app)")
    parser.add_argument("--context", action="store_true", help="check what .dockerignore lets into the build")
    args = parser.parse_args(argv)
    strings = forbidden_strings()
    if args.context:
        with tempfile.TemporaryDirectory() as tmp:
            copied = build_context(ROOT, Path(tmp))
            problems = check_tree(Path(tmp), strings)
            if not any(c.startswith("server/") for c in copied):
                problems.append("the build context has no server/ at all")
    elif args.tree is not None:
        problems = check_tree(args.tree, strings)
    else:
        parser.error("give a directory or --context")
    for problem in problems:
        print(problem, file=sys.stderr)
    print("content check: " + ("FAIL" if problems else "ok, no channel lists, cookies or keys"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
