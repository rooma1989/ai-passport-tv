#!/usr/bin/env python3
"""Release helpers for the PC client (.github/workflows/build-desktop.yml).

    python3 packaging/desktop/release.py version [--tag v0.2.0-beta] [--stamp]
    python3 packaging/desktop/release.py collect <artifacts folder> --out <folder> [--version 0.2.0-beta]

`version` prints the version the installers are named after (desktop.__version__).
With --tag (a tag push) the tag must be "v" + that version, so a release never
carries files named after another version; --stamp also writes it into the
core (server/selfhost/version.py), which a checkout reports as "<ver>.dev".

`collect` takes the per-platform build artifacts (each folder with the files
and that platform's SHA256SUMS, as build.py and the Inno Setup step leave them),
checks every installer against the SHA256SUMS it was built with, requires the
whole set (two DMGs, the Windows installer and portable zip, the Linux tarball),
and writes them with one SHA256SUMS-desktop into --out. Its own file name, not
SHA256SUMS: the firmware workflow publishes SHA256SUMS into the same release
for the web flasher, and whichever job ran second would replace the other's.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from desktop import __version__  # noqa: E402

CORE_VERSION_FILE = ROOT / "server" / "selfhost" / "version.py"
SUMS = "SHA256SUMS-desktop"


class ReleaseError(Exception):
    pass


def expected_files(version: str) -> list[str]:
    """The release's PC client downloads, in the order SHA256SUMS-desktop lists them."""
    # RooTV since the rename; 0.2.5-beta and older were XiaozhiTV-... / xiaozhi-tv-...
    return [
        f"RooTV-{version}-windows-x64-setup.exe",
        f"RooTV-{version}-windows-x64.zip",
        f"RooTV-{version}-macos-arm64.dmg",
        f"RooTV-{version}-macos-x86_64.dmg",
        f"rootv-{version}-linux-x86_64.tar.gz",
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def read_sums(path: Path) -> dict[str, str]:
    listed = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split(None, 1)
            listed[name.strip().lstrip("*")] = digest.lower()
    return listed


def check_version(tag: str | None, version: str = __version__) -> str:
    if tag is not None and tag != f"v{version}":
        raise ReleaseError(f"tag {tag} does not match the client's version {version} (desktop/__init__.py); "
                           f"tag v{version}, or change __version__ first")
    return version


def stamp_core(version: str, path: Path = CORE_VERSION_FILE) -> None:
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(r'^VERSION = "[^"]*"$', f'VERSION = "{version}"', text, flags=re.MULTILINE)
    if count != 1:
        raise ReleaseError(f"no VERSION line in {path}")
    path.write_text(new, encoding="utf-8")


def collect(parts: Path, out: Path, version: str = __version__) -> list[Path]:
    """Find, check and copy the set; returns the copied files (SHA256SUMS-desktop last)."""
    found: dict[str, Path] = {}
    for sums in sorted(parts.rglob("SHA256SUMS")):
        for name, digest in read_sums(sums).items():
            path = sums.parent / name
            if name not in expected_files(version) or not path.is_file():
                continue
            actual = sha256(path)
            if actual != digest:
                raise ReleaseError(f"{name}: sha256 {actual} but its SHA256SUMS says {digest}")
            if name in found:
                raise ReleaseError(f"{name} is in two artifacts")
            found[name] = path
    missing = [name for name in expected_files(version) if name not in found]
    if missing:
        raise ReleaseError("missing from the build artifacts (or not in their SHA256SUMS): " + ", ".join(missing))
    out.mkdir(parents=True, exist_ok=True)
    copied, lines = [], []
    for name in expected_files(version):
        target = out / name
        shutil.copyfile(found[name], target)
        digest = sha256(target)
        lines.append(f"{digest}  {name}")
        copied.append(target)
    (out / SUMS).write_text("\n".join(lines) + "\n", encoding="ascii")
    copied.append(out / SUMS)
    return copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("version")
    v.add_argument("--tag", help="the pushed tag (e.g. v0.2.0-beta); must match the version")
    v.add_argument("--stamp", action="store_true", help="write the version into server/selfhost/version.py")
    c = sub.add_parser("collect")
    c.add_argument("parts", type=Path)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--version", default=__version__)
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            version = check_version(args.tag or None)
            if args.stamp:
                stamp_core(version)
            print(version)
        else:
            for path in collect(args.parts, args.out, args.version):
                print(f"{path.name}  {path.stat().st_size} bytes")
            print((args.out / SUMS).read_text(encoding="ascii"), end="")
    except ReleaseError as error:
        print(f"::error::release: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
