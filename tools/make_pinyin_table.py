#!/usr/bin/env python3
"""Generate server/voice/pinyin_table.py: toneless pinyin for every GB2312 hanzi.

Development time only. It needs `pypinyin` (pip install pypinyin); the media
server does not, because it reads the generated table, which is plain Python.

Every reading of a polyphone is kept (zhong/chong, chang/zhang, le/yue,
xing/hang), so the voice matcher accepts whichever one the speech recogniser
picked. The table is grouped by syllable to keep it small: one line per
syllable, followed by the characters that have that reading.

    python3 tools/make_pinyin_table.py            # writes server/voice/pinyin_table.py
    python3 tools/make_pinyin_table.py --check    # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server" / "voice" / "pinyin_table.py"

HEADER = '''"""GB2312 汉字的无声调拼音，由 tools/make_pinyin_table.py 生成，不要手改。

每行一个音节，后面是有这个读音的字；多音字在每个读音下都出现。
"""

DATA = """\\
'''


def gb2312_hanzi() -> list[str]:
    """The 6763 hanzi of GB2312: rows 0xB0-0xF7, columns 0xA1-0xFE."""
    chars = []
    for high in range(0xB0, 0xF8):
        for low in range(0xA1, 0xFF):
            try:
                char = bytes((high, low)).decode("gb2312")
            except UnicodeDecodeError:
                continue
            chars.append(char)
    return chars


def build() -> str:
    from pypinyin import Style, pinyin

    by_syllable: dict[str, list[str]] = {}
    for char in gb2312_hanzi():
        readings = pinyin(char, style=Style.NORMAL, heteronym=True)[0]
        for reading in dict.fromkeys(r.lower() for r in readings):
            if reading.isascii() and reading.isalpha():
                by_syllable.setdefault(reading, []).append(char)
    lines = [f"{syllable} {''.join(chars)}" for syllable, chars in sorted(by_syllable.items())]
    return HEADER + "\n".join(lines) + '\n"""\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only compare with the file")
    args = parser.parse_args(argv)
    text = build()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is stale; run tools/make_pinyin_table.py")
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text.encode())} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
