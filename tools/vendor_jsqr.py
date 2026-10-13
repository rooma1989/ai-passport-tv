#!/usr/bin/env python3
"""Rebuild server/relay/assets/jsqr.js from the upstream jsQR 1.4.0 bundle.

The phone remote's "scan to sign in the PC" falls back to jsQR when the browser
has no BarcodeDetector (Safari on iPhone). The upstream bundle is 251 KiB; two
mechanical changes bring it to about 77 KiB without touching any logic:

- the Shift-JIS table (Kanji-mode QR codes) is replaced by an empty table:
  login QR codes are alphanumeric, and Kanji characters would decode to U+0000
  instead of throwing;
- comment-only lines, webpack's "/***/" markers and indentation are dropped.

Usage (the tarball comes from https://registry.npmjs.org/jsqr/-/jsqr-1.4.0.tgz):

    python3 tools/vendor_jsqr.py path/to/package/dist/jsQR.js

The input is checked against the upstream hash so the output is reproducible.
tests/test_relay_console.py decodes the relay's own login QR with the result.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

UPSTREAM_SHA256 = "bc40c8a15196236b2314db0856f72ca0b49980cd5413b8c852a7349f5fee0859"
OUT = Path(__file__).resolve().parents[1] / "server" / "relay" / "assets" / "jsqr.js"
HEADER = """\
/*! jsQR 1.4.0 (https://github.com/cozmo/jsQR), Copyright Cosmo Wolfe and contributors.
 * Licensed under the Apache License, Version 2.0; see LICENSE.jsqr.txt next to this file
 * or http://www.apache.org/licenses/LICENSE-2.0
 * Modified for ai-passport-tv by tools/vendor_jsqr.py: Shift-JIS (Kanji) table emptied;
 * comment-only lines and indentation removed. No other changes. */
"""


def strip(source: str) -> str:
    start = source.index("exports.shiftJISTable = {")
    end = source.index("};", start) + 2
    source = source[:start] + "exports.shiftJISTable = {};" + source[end:]
    lines = []
    for line in source.splitlines():
        text = line.strip()
        if not text or text.startswith("//") or re.fullmatch(r"/\*+/|/\* \d+ \*/", text):
            continue
        text = re.sub(r"^/\*{3,6}/\s*", "", text)
        lines.append(text)
    return HEADER + "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    data = Path(sys.argv[1]).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != UPSTREAM_SHA256:
        print(f"not the jsQR 1.4.0 dist/jsQR.js (sha256 {digest})", file=sys.stderr)
        return 1
    OUT.write_text(strip(data.decode("utf-8")), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
