#!/usr/bin/env python3
"""Release manifests for the public firmware, and the checks behind them.

The web flasher (packaging/flash/) reads one file, ``manifest.json``, to learn
which image belongs to which chip, where it is written, how big it is and what
its SHA-256 is. This tool writes that file and refuses to write it for an image
that is not what the board needs. Self-hosting design, section 9.1.

Sub-commands:

  make          one board's manifest (``manifest-<board>.json``) beside a copy of
                its image, after checking the image: chip id in the header,
                this project's application at 0x10000, size inside the board's
                write limit, version as given.
  merge         every ``manifest-*.json`` in a folder into ``manifest.json``,
                plus ``SHA256SUMS``; each file is re-hashed on the way.
  check         a folder holding ``manifest.json`` and the images it names.
  bundle        the flasher page and a checked firmware folder, assembled into
                one folder that a static site or the household server serves
                as ``/flash/``.
  check-public  a build directory: the public build's switches really were on
                (no private header, no default server, no public site).

Board facts live in BOARDS below and, for the browser, in
packaging/flash/flasher-core.js (BOARD_POLICY). tests/test_flasher.py fails when
the two disagree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLASHER_DIR = ROOT / "packaging" / "flash"
SCHEMA = 1
PROJECT = "xiaozhi-tv"
APP_OFFSET = 0x10000
PARTITION_TABLE_OFFSET = 0x8000
APP_PROJECT_NAME = "FoloToy-AI-Passport"   # project() in CMakeLists.txt
APP_DESC_MAGIC = 0xABCD5432
IMAGE_MAGIC = 0xE9
MB = 1024 * 1024

# What each board may have written to it. "erase" is the flasher's rule:
#   never          the chip is never erased (the AI Passport's factory identity
#                  lives at 0x356000 and cannot be recreated);
#   first-install  erased once, with the user's consent, when it replaces other
#                  firmware (the Xingzhi's Xiaozhi AI firmware keeps its NVS
#                  inside this firmware's NVS range); a re-flash over this
#                  firmware writes without erasing, so settings and binding stay.
# "write_limit" is where every write must end; "protect" lists ranges no write
# may touch even if the limit were raised.
BOARDS: dict[str, dict] = {
    "xingzhi-cube-154": {
        "name": "星智 Cube 1.54（小智 AI 4G 款）",
        "name_en": "Xingzhi Cube 1.54 (Xiaozhi AI 4G)",
        "chip": "ESP32-S3",
        "chip_id": 9,
        "flash_size": "8MB",
        "file": "Xingzhi-Cube-154-tv-public.bin",
        "erase": "first-install",
        "write_limit": 8 * MB,
        "protect": [],
    },
    "ai-passport": {
        "name": "FoloToy AI Passport",
        "name_en": "FoloToy AI Passport",
        "chip": "ESP32-C3",
        "chip_id": 5,
        "flash_size": "8MB",
        "file": "FoloToy-AI-Passport-tv.bin",
        "erase": "never",
        "write_limit": 0x356000,
        "protect": [[0x356000, 0x35A000]],
    },
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_VERSION = re.compile(r"^[ -~]{1,32}$")


class ManifestError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def app_description(image: bytes, offset: int = APP_OFFSET) -> dict | None:
    """The esp_app_desc_t of the application image at `offset`, or None.

    ESP-IDF puts it at the start of the first segment: 24 bytes of image header
    and 8 of segment header, then magic, secure version, reserved words,
    version[32] and project_name[32].
    """
    if len(image) < offset + 0x70 or image[offset] != IMAGE_MAGIC:
        return None
    base = offset + 0x20
    (magic,) = struct.unpack_from("<I", image, base)
    if magic != APP_DESC_MAGIC:
        return None
    return {"version": _cstr(image[base + 16: base + 48]),
            "project": _cstr(image[base + 48: base + 80])}


def check_image(board: str, image: bytes, version: str | None = None) -> dict:
    """Refuse an image that is not this project's firmware for `board`."""
    if board not in BOARDS:
        raise ManifestError(f"unknown board {board!r}; known: {', '.join(BOARDS)}")
    spec = BOARDS[board]
    if len(image) < APP_OFFSET + 0x100 or image[0] != IMAGE_MAGIC:
        raise ManifestError("not a merged image: no bootloader image header at 0x0")
    (chip_id,) = struct.unpack_from("<H", image, 12)
    if chip_id != spec["chip_id"]:
        raise ManifestError(f"image is built for chip id {chip_id}, {board} is {spec['chip']} "
                            f"(chip id {spec['chip_id']})")
    if image[PARTITION_TABLE_OFFSET: PARTITION_TABLE_OFFSET + 2] != b"\xaa\x50":
        raise ManifestError("no partition table at 0x8000")
    app = app_description(image)
    if app is None:
        raise ManifestError("no application description at 0x10000")
    if app["project"] != APP_PROJECT_NAME:
        raise ManifestError(f"application at 0x10000 is {app['project']!r}, not {APP_PROJECT_NAME!r}")
    if version is not None and app["version"] != version[:32]:
        raise ManifestError(f"image reports version {app['version']!r}, expected {version!r}")
    if len(image) > spec["write_limit"]:
        raise ManifestError(f"image is {len(image)} bytes, more than {board}'s write limit "
                            f"0x{spec['write_limit']:x}")
    for start, end in spec["protect"]:
        if len(image) > start:
            raise ManifestError(f"image reaches the protected range 0x{start:x}-0x{end:x}")
    return app


def build_entry(board: str, image_path: Path, app: dict) -> dict:
    spec = BOARDS[board]
    return {
        "board": board,
        "name": spec["name"],
        "name_en": spec["name_en"],
        "chip": spec["chip"],
        "flash_size": spec["flash_size"],
        "erase": spec["erase"],
        "write_limit": spec["write_limit"],
        "protect": spec["protect"],
        "version": app["version"],
        "parts": [{
            "path": spec["file"],
            "offset": 0,
            "size": image_path.stat().st_size,
            "sha256": sha256_file(image_path),
        }],
    }


def validate_manifest(manifest: dict) -> None:
    """The same rules the flasher applies (flasher-core.js parseManifest)."""
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ManifestError(f"manifest schema must be {SCHEMA}")
    if not isinstance(manifest.get("version"), str) or not _VERSION.match(manifest["version"]):
        raise ManifestError("manifest version must be 1-32 printable characters")
    builds = manifest.get("builds")
    if not isinstance(builds, list) or not builds:
        raise ManifestError("manifest lists no builds")
    chips = set()
    for build in builds:
        board = build.get("board")
        if board not in BOARDS:
            raise ManifestError(f"unknown board {board!r}")
        spec = BOARDS[board]
        if build.get("chip") != spec["chip"]:
            raise ManifestError(f"{board}: chip must be {spec['chip']}")
        if build["chip"] in chips:
            raise ManifestError(f"two builds for {build['chip']}")
        chips.add(build["chip"])
        if build.get("erase") != spec["erase"]:
            raise ManifestError(f"{board}: erase rule must be {spec['erase']!r}")
        limit = min(int(build.get("write_limit", 0)), spec["write_limit"])
        parts = build.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ManifestError(f"{board}: no parts")
        spans = []
        for part in parts:
            path, offset, size, digest = (part.get(k) for k in ("path", "offset", "size", "sha256"))
            if not isinstance(path, str) or not _SAFE_NAME.match(path):
                raise ManifestError(f"{board}: unsafe file name {path!r}")
            if not isinstance(offset, int) or not isinstance(size, int) or offset < 0 or size <= 0:
                raise ManifestError(f"{board}: bad offset or size for {path}")
            if offset % 0x1000:
                raise ManifestError(f"{board}: {path} offset 0x{offset:x} is not sector aligned")
            if not isinstance(digest, str) or not _SHA256.match(digest):
                raise ManifestError(f"{board}: bad sha256 for {path}")
            if offset + size > limit:
                raise ManifestError(f"{board}: {path} ends at 0x{offset + size:x}, past 0x{limit:x}")
            for start, end in spec["protect"]:
                if offset < end and start < offset + size:
                    raise ManifestError(f"{board}: {path} overlaps protected 0x{start:x}-0x{end:x}")
            spans.append((offset, offset + size, path))
        spans.sort()
        for (a0, a1, an), (b0, _b1, bn) in zip(spans, spans[1:]):
            if a1 > b0:
                raise ManifestError(f"{board}: {an} and {bn} overlap")


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_make(args) -> int:
    image_path = Path(args.image)
    image = image_path.read_bytes()
    app = check_image(args.board, image, args.version)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / BOARDS[args.board]["file"]
    if target.resolve() != image_path.resolve():
        shutil.copyfile(image_path, target)
    manifest = {"schema": SCHEMA, "project": PROJECT, "version": app["version"],
                "builds": [build_entry(args.board, target, app)]}
    validate_manifest(manifest)
    write_json(out / f"manifest-{args.board}.json", manifest)
    print(f"{args.board}: {target.name} {target.stat().st_size} bytes, version {app['version']}, "
          f"sha256 {manifest['builds'][0]['parts'][0]['sha256']}")
    return 0


def verify_files(folder: Path, manifest: dict) -> None:
    for build in manifest["builds"]:
        for part in build["parts"]:
            path = folder / part["path"]
            if not path.is_file():
                raise ManifestError(f"missing {part['path']}")
            if path.stat().st_size != part["size"]:
                raise ManifestError(f"{part['path']} is {path.stat().st_size} bytes, manifest says {part['size']}")
            if sha256_file(path) != part["sha256"]:
                raise ManifestError(f"{part['path']} does not match its sha256")
            app = check_image(build["board"], path.read_bytes())
            if app["version"] != build["version"]:
                raise ManifestError(f"{part['path']} reports {app['version']!r}, manifest says {build['version']!r}")


def cmd_merge(args) -> int:
    folder = Path(args.folder)
    pieces = sorted(folder.glob("manifest-*.json"))
    if not pieces:
        raise ManifestError(f"no manifest-*.json in {folder}")
    builds, versions = [], set()
    for piece in pieces:
        data = json.loads(piece.read_text(encoding="utf-8"))
        validate_manifest(data)
        verify_files(folder, data)
        builds.extend(data["builds"])
        versions.add(data["version"])
    if len(versions) != 1:
        raise ManifestError(f"builds disagree on the version: {sorted(versions)}")
    merged = {"schema": SCHEMA, "project": PROJECT, "version": versions.pop(),
              "builds": sorted(builds, key=lambda b: list(BOARDS).index(b["board"]))}
    validate_manifest(merged)
    write_json(folder / "manifest.json", merged)
    names = [part["path"] for build in merged["builds"] for part in build["parts"]]
    names += [piece.name for piece in pieces] + ["manifest.json"]
    lines = [f"{sha256_file(folder / name)}  {name}" for name in names]
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"manifest.json: {len(merged['builds'])} builds, version {merged['version']}")
    return 0


def load_checked(folder: Path) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    validate_manifest(manifest)
    verify_files(folder, manifest)
    sums = folder / "SHA256SUMS"
    if sums.is_file():
        listed = {}
        for line in sums.read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, name = line.split(None, 1)
                listed[name.lstrip("*")] = digest
        for build in manifest["builds"]:
            for part in build["parts"]:
                if listed.get(part["path"]) != part["sha256"]:
                    raise ManifestError(f"SHA256SUMS disagrees with manifest.json on {part['path']}")
    return manifest


def cmd_check(args) -> int:
    manifest = load_checked(Path(args.folder))
    for build in manifest["builds"]:
        part = build["parts"][0]
        print(f"{build['board']}: {part['path']} {part['size']} bytes at 0x{part['offset']:x}, "
              f"erase={build['erase']}, version {build['version']}")
    return 0


def cmd_bundle(args) -> int:
    firmware = Path(args.firmware)
    load_checked(firmware)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(FLASHER_DIR, out, ignore=shutil.ignore_patterns("README*", "firmware"))
    (out / "firmware").mkdir()
    manifest = json.loads((firmware / "manifest.json").read_text(encoding="utf-8"))
    names = ["manifest.json", "SHA256SUMS"] + [p["path"] for b in manifest["builds"] for p in b["parts"]]
    for name in names:
        shutil.copyfile(firmware / name, out / "firmware" / name)
    print(f"flasher bundle: {out} ({len(names)} firmware files)")
    return 0


def _sdkconfig(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def check_public(build_dir: Path, forbidden: list[str]) -> list[str]:
    """Problems that make a build unfit to publish; empty when it is fine.

    Searching the image for a token proves little -- the compiler folds short
    literals into instructions (see .gitignore) -- so the decisive checks are on
    how it was built: AV_PUBLIC_BUILD reached the compiler, so av_config.h
    skipped the private header, and the sdkconfig names no server and no site.
    """
    problems = []
    config = _sdkconfig(build_dir / "sdkconfig")
    for key in ("CONFIG_AV_DEFAULT_SERVER", "CONFIG_AV_PUBLIC_SITE"):
        if config.get(key, '""') != '""':
            problems.append(f"{key} is set; public builds name no server or site")
    commands = json.loads((build_dir / "compile_commands.json").read_text(encoding="utf-8"))
    player = [c for c in commands if c.get("file", "").endswith("main/av_player.c")]
    if not player:
        problems.append("compile_commands.json has no main/av_player.c")
    elif not all("-DAV_PUBLIC_BUILD=1" in (c.get("command") or " ".join(c.get("arguments", [])))
                 for c in player):
        problems.append("main/av_player.c was compiled without -DAV_PUBLIC_BUILD=1")
    image = (build_dir / "FoloToy-AI-Passport-full.bin").read_bytes()
    for index, text in enumerate(forbidden, 1):
        if text and text.encode() in image:
            # Not echoed: the list comes from the caller's environment and may
            # itself be private (a host name, an address).
            problems.append(f"forbidden string #{index} from AV_PUBLIC_FORBID is in the image")
    return problems


def cmd_check_public(args) -> int:
    forbidden = [s.strip() for s in os.environ.get("AV_PUBLIC_FORBID", "").split(",") if s.strip()]
    problems = check_public(Path(args.build_dir), forbidden)
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        return 1
    extra = f", {len(forbidden)} forbidden strings absent" if forbidden else ""
    print(f"Public build: PASS (no private header, no default server, no site{extra})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make", help="one board's manifest and image copy")
    make.add_argument("--board", required=True, choices=sorted(BOARDS))
    make.add_argument("--image", required=True)
    make.add_argument("--version", help="refuse the image unless it reports this version")
    make.add_argument("--out-dir", required=True)
    make.set_defaults(func=cmd_make)
    merge = sub.add_parser("merge", help="manifest-*.json -> manifest.json + SHA256SUMS")
    merge.add_argument("folder")
    merge.set_defaults(func=cmd_merge)
    check = sub.add_parser("check", help="check manifest.json and its files")
    check.add_argument("folder")
    check.set_defaults(func=cmd_check)
    bundle = sub.add_parser("bundle", help="flasher page + firmware folder, ready to serve as /flash/")
    bundle.add_argument("firmware")
    bundle.add_argument("--out", required=True)
    bundle.set_defaults(func=cmd_bundle)
    public = sub.add_parser("check-public", help="a build directory is a public build")
    public.add_argument("build_dir")
    public.set_defaults(func=cmd_check_public)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ManifestError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
