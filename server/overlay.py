"""重连提示：在调色板索引画面上绘制一行文字。

用内置 5x7 点阵字体直接写入索引数组，不依赖图像库。索引对应固定 3-3-2 调色板
（`media.default_palette`）：0 为黑，255 为白，1 为深蓝。
"""

from __future__ import annotations

from . import frames

NOTICE_TEXT = "RECONNECTING..."
# 手机上暂停时的提示。
PAUSE_TEXT = "PAUSED"

BLACK = 0
WHITE = 255
DARK_BLUE = 1

_GLYPHS: dict[str, tuple[str, ...]] = {
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    ".": ("..", "..", "..", "..", "..", "##", "##"),
    # 未绑定设备的提示画面要写绑定码（A–Z、2–7）和 "BIND THIS TV"，补齐字母和数字。
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("####.", "....#", "....#", ".###.", "....#", "....#", "####."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": (".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."),
    " ": ("...", "...", "...", "...", "...", "...", "..."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
}
_GLYPH_ROWS = 7
_LETTER_GAP = 1
_BOX_PAD = 8


def _text_columns(text: str) -> int:
    return sum(len(_GLYPHS[c][0]) for c in text) + _LETTER_GAP * (len(text) - 1)


def reconnect_frame(base: bytes | None, text: str = NOTICE_TEXT) -> bytes:
    """在 `base` 中央的黑框上绘制提示；`base` 为 None 时用纯深蓝底。"""
    if base is not None and len(base) != frames.FRAME_PIXELS:
        raise ValueError(f"frame is {len(base)} bytes, expected {frames.FRAME_PIXELS}")
    width, height = frames.WIDTH, frames.HEIGHT
    pixels = bytearray(base) if base is not None else bytearray([DARK_BLUE]) * frames.FRAME_PIXELS
    columns = _text_columns(text)
    scale = max(1, (width - 4 * _BOX_PAD) // columns)
    text_w, text_h = columns * scale, _GLYPH_ROWS * scale
    left, top = (width - text_w) // 2, (height - text_h) // 2

    if base is not None:
        for y in range(max(0, top - _BOX_PAD), min(height, top + text_h + _BOX_PAD)):
            row = y * width
            for x in range(max(0, left - _BOX_PAD), min(width, left + text_w + _BOX_PAD)):
                pixels[row + x] = BLACK

    x0 = left
    for char in text:
        glyph = _GLYPHS[char]
        for gy, line in enumerate(glyph):
            for gx, cell in enumerate(line):
                if cell != "#":
                    continue
                for dy in range(scale):
                    row = (top + gy * scale + dy) * width
                    for dx in range(scale):
                        pixels[row + x0 + gx * scale + dx] = WHITE
        x0 += (len(glyph[0]) + _LETTER_GAP) * scale
    return bytes(pixels)


# 行与行之间空几个点（按字的放大倍数算前的点数）。
_LINE_GAP = 4


def lines_frame(lines: list[str]) -> bytes:
    """深蓝底上居中写几行字，各行同一字号（按最长的一行放得下算）。未绑定设备的提示画面用它。"""
    lines = [line.upper() for line in lines if line]
    if not lines:
        raise ValueError("no text")
    width, height = frames.WIDTH, frames.HEIGHT
    pixels = bytearray([DARK_BLUE]) * frames.FRAME_PIXELS
    widest = max(_text_columns(line) for line in lines)
    rows = len(lines) * _GLYPH_ROWS + (len(lines) - 1) * _LINE_GAP
    scale = max(1, min((width - 2 * _BOX_PAD) // widest, (height - 2 * _BOX_PAD) // rows))
    top = (height - rows * scale) // 2
    for line in lines:
        x0 = (width - _text_columns(line) * scale) // 2
        for char in line:
            glyph = _GLYPHS[char]
            for gy, row_text in enumerate(glyph):
                for gx, cell in enumerate(row_text):
                    if cell != "#":
                        continue
                    for dy in range(scale):
                        row = (top + gy * scale + dy) * width
                        for dx in range(scale):
                            pixels[row + x0 + gx * scale + dx] = WHITE
            x0 += (len(glyph[0]) + _LETTER_GAP) * scale
        top += (_GLYPH_ROWS + _LINE_GAP) * scale
    return bytes(pixels)
