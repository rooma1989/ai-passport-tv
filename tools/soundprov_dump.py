#!/usr/bin/env python3
"""Turn the setup screen's microphone dump into WAV files.

A firmware built with CONFIG_AV_SOUNDPROV_DUMP prints, whenever a sound
message starts arriving on the setup screen, half a second from before it and
then a few seconds of the 16 kHz microphone signal as console lines:

    SNDDUMP begin rate=16000
    SNDDUMP <base64url of little-endian int16 samples>
    ...
    SNDDUMP end

Save the serial console to a file (log prefixes before "SNDDUMP" are fine) and
run this on it. Each begin/end block becomes one mono 16-bit WAV, which the
decoder's host test replays:

    python3 tools/soundprov_dump.py console.log -o capture
    tests/test_av_soundprov --wav capture-1.wav
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import wave
from pathlib import Path

LINE = re.compile(r"SNDDUMP (\S+)(?: rate=(\d+))?")


def blocks(text: str) -> list[tuple[int, bytes]]:
    found, rate, data = [], None, None
    for line in text.splitlines():
        match = LINE.search(line)
        if not match:
            continue
        word = match.group(1)
        if word == "begin":
            rate, data = int(match.group(2) or 16000), bytearray()
        elif word == "end":
            if data is not None and data:
                found.append((rate, bytes(data)))
            rate, data = None, None
        elif data is not None:
            try:
                data += base64.urlsafe_b64decode(word + "=" * (-len(word) % 4))
            except ValueError:
                print(f"skipping a damaged line ({len(word)} characters)", file=sys.stderr)
    if data:
        # The device stopped (or the log was cut) before "end": keep what came.
        found.append((rate, bytes(data)))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path, help="saved serial console output")
    parser.add_argument("-o", "--out", default="soundprov", help="output name prefix (default soundprov)")
    args = parser.parse_args(argv)
    found = blocks(args.log.read_text(encoding="utf-8", errors="replace"))
    if not found:
        print("no SNDDUMP blocks in that log", file=sys.stderr)
        return 1
    for n, (rate, data) in enumerate(found, start=1):
        path = Path(f"{args.out}-{n}.wav")
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(data[: len(data) // 2 * 2])
        print(f"{path}: {len(data) // 2 / rate:.1f} s at {rate} Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
