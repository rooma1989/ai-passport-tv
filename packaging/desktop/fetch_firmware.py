#!/usr/bin/env python3
"""Put the latest released firmware into packaging/desktop/firmware/ before the core is built.

The core's spec (xiaozhi-tv-core.spec) bundles that folder when it holds a
manifest.json, so the client's /flash/ page works offline on the first run.
The set comes from the release asset xiaozhi-tv-flasher.zip and is checked the
way the flasher checks it (tools/firmware_manifest.install_bundle: manifest,
SHA256SUMS, sizes, hashes) before it replaces anything.

    python3 packaging/desktop/fetch_firmware.py [--url <xiaozhi-tv-flasher.zip URL>]

Exit 0 with a notice when the download fails or no release has the bundle: the client then shows
"no firmware" on its flasher page and the data folder's firmware/ still works.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import firmware_manifest  # noqa: E402
from tools.launch import FIRMWARE_BUNDLE_MAX, FIRMWARE_BUNDLE_URL  # noqa: E402

TARGET = Path(__file__).resolve().parent / "firmware"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=FIRMWARE_BUNDLE_URL)
    parser.add_argument("--required", action="store_true", help="fail instead of continuing without firmware")
    args = parser.parse_args(argv)
    level = "error" if args.required else "notice"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "xiaozhi-tv-flasher.zip"
        try:
            request = urllib.request.Request(args.url, headers={"User-Agent": "xiaozhi-tv-build/1"})
            with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as out:
                total = 0
                while chunk := response.read(1 << 16):
                    total += len(chunk)
                    if total > FIRMWARE_BUNDLE_MAX:
                        raise OSError("larger than a firmware bundle can be")
                    out.write(chunk)
        except (urllib.error.URLError, OSError, ValueError) as error:
            print(f"::{level}::no firmware bundled: {getattr(error, 'reason', error)} ({args.url})", flush=True)
            return 1 if args.required else 0
        try:
            manifest = firmware_manifest.install_bundle(archive, TARGET)
        except Exception as error:      # noqa: BLE001 - ManifestError, BadZipFile, ...
            print(f"::{level}::firmware bundle refused: {type(error).__name__}: {error}", flush=True)
            return 1 if args.required else 0
    boards = ", ".join(b["board"] for b in manifest["builds"])
    print(f"::notice::firmware {manifest['version']} ({boards}) bundled into the core", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
