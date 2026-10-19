#!/usr/bin/env python3
"""Put the latest released firmware into packaging/desktop/firmware/ before the core is built.

The core's spec (xiaozhi-tv-core.spec) bundles that folder when it holds a
manifest.json, so the client's /flash/ page works offline on the first run.
The set comes from the release asset xiaozhi-tv-flasher.zip and is checked the
way the flasher checks it (tools/firmware_manifest.install_bundle: manifest,
SHA256SUMS, sizes, hashes) before it replaces anything.

    python3 packaging/desktop/fetch_firmware.py [--url <xiaozhi-tv-flasher.zip URL>]
        [--required] [--wait <seconds>] [--expect-version <tag>]

Exit 0 with a notice when the download fails or no release has the bundle: the client then shows
"no firmware" on its flasher page and the data folder's firmware/ still works.

A tag build (build-desktop.yml) takes the firmware of the same release instead: the firmware
workflow builds it at the same time, so --wait keeps asking (every 30 s) until the release has
the bundle, --expect-version refuses a bundle of another version, and --required fails the build
rather than shipping a client whose /flash/ page offers other firmware than its release.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import firmware_manifest  # noqa: E402
from tools.launch import FIRMWARE_BUNDLE_MAX, FIRMWARE_BUNDLE_URL  # noqa: E402

TARGET = Path(__file__).resolve().parent / "firmware"


def download(url: str, archive: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "xiaozhi-tv-build/1"})
    with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as out:
        total = 0
        while chunk := response.read(1 << 16):
            total += len(chunk)
            if total > FIRMWARE_BUNDLE_MAX:
                raise OSError("larger than a firmware bundle can be")
            out.write(chunk)


def main(argv: list[str] | None = None, sleep=time.sleep, clock=time.monotonic) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=FIRMWARE_BUNDLE_URL)
    parser.add_argument("--required", action="store_true", help="fail instead of continuing without firmware")
    parser.add_argument("--wait", type=float, default=0, help="keep asking this many seconds while the URL is 404")
    parser.add_argument("--expect-version", help="refuse a bundle whose manifest has another version")
    args = parser.parse_args(argv)
    level = "error" if args.required else "notice"
    deadline = clock() + args.wait
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "xiaozhi-tv-flasher.zip"
        while True:
            try:
                download(args.url, archive)
                break
            except urllib.error.HTTPError as error:
                if error.code == 404 and clock() < deadline:
                    print(f"firmware bundle not published yet ({args.url}); asking again in 30 s", flush=True)
                    sleep(30)
                    continue
                print(f"::{level}::no firmware bundled: HTTP {error.code} ({args.url})", flush=True)
                return 1 if args.required else 0
            except (urllib.error.URLError, OSError, ValueError) as error:
                print(f"::{level}::no firmware bundled: {getattr(error, 'reason', error)} ({args.url})", flush=True)
                return 1 if args.required else 0
        try:
            manifest = firmware_manifest.install_bundle(archive, TARGET)
        except Exception as error:      # noqa: BLE001 - ManifestError, BadZipFile, ...
            print(f"::{level}::firmware bundle refused: {type(error).__name__}: {error}", flush=True)
            return 1 if args.required else 0
    if args.expect_version and manifest["version"] != args.expect_version:
        shutil.rmtree(TARGET, ignore_errors=True)
        print(f"::{level}::firmware bundle is {manifest['version']}, expected {args.expect_version}", flush=True)
        return 1 if args.required else 0
    boards = ", ".join(b["board"] for b in manifest["builds"])
    print(f"::notice::firmware {manifest['version']} ({boards}) bundled into the core", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
