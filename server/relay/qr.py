"""二维码：把一段文字编成模块矩阵，再画成 SVG。电脑版登录页、访客码、邀请码用，不经任何外部服务。

算法照搬 Project Nayuki 的 QR Code generator library（MIT 许可，和固件用的
components/qrcodegen/qrcodegen.c 同一个库）：自动选数字 / 字母数字 / 字节模式、最小版本、
在不变大的前提下提高纠错等级、按罚分自动选掩码。tests/test_relay_qr.py 拿固件那份 C 代码
编出来的矩阵逐个模块对照。

Copyright (c) Project Nayuki. (MIT License)
https://www.nayuki.io/page/qr-code-generator-library
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions: The above copyright notice and this
permission notice shall be included in all copies or substantial portions of the Software. The
Software is provided "as is", without warranty of any kind, express or implied.
"""

from __future__ import annotations

import re
from collections import deque

# 纠错等级：(表里的行号, 格式信息里的两位)。
LOW, MEDIUM, QUARTILE, HIGH = (0, 1), (1, 0), (2, 3), (3, 2)

_ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28,
     30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28,
     28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28,
     30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30,
     30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
_NUM_ERROR_CORRECTION_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17,
     18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29,
     31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38,
     40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45,
     48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)
_PENALTY_N1, _PENALTY_N2, _PENALTY_N3, _PENALTY_N4 = 3, 3, 40, 10
_MASKS = (
    lambda x, y: (x + y) % 2,
    lambda x, y: y % 2,
    lambda x, y: x % 3,
    lambda x, y: (x + y) % 3,
    lambda x, y: (x // 3 + y // 2) % 2,
    lambda x, y: x * y % 2 + x * y % 3,
    lambda x, y: (x * y % 2 + x * y % 3) % 2,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
)
_ALPHANUMERIC = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
_NUMERIC_RE = re.compile(r"[0-9]*")
_ALPHANUMERIC_RE = re.compile(r"[A-Z0-9 $%*+./:-]*")
# 模式：(模式位, 各版本段的字符数位数)
_NUMERIC, _ALNUM, _BYTE = (0x1, (10, 12, 14)), (0x2, (9, 11, 13)), (0x4, (8, 16, 16))


class DataTooLong(ValueError):
    pass


def _segment(text: str):
    """(模式, 字符数, 数据位)。和 Nayuki 的 make_segments 一样只出一段。"""
    bits: list[int] = []

    def put(value: int, count: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(count)))

    if _NUMERIC_RE.fullmatch(text):
        for i in range(0, len(text), 3):
            chunk = text[i:i + 3]
            put(int(chunk), len(chunk) * 3 + 1)
        return _NUMERIC, len(text), bits
    if _ALPHANUMERIC_RE.fullmatch(text):
        for i in range(0, len(text) - 1, 2):
            put(_ALPHANUMERIC.index(text[i]) * 45 + _ALPHANUMERIC.index(text[i + 1]), 11)
        if len(text) % 2:
            put(_ALPHANUMERIC.index(text[-1]), 6)
        return _ALNUM, len(text), bits
    data = text.encode("utf-8")
    for byte in data:
        put(byte, 8)
    return _BYTE, len(data), bits


def _char_count_bits(mode, version: int) -> int:
    return mode[1][(version + 7) // 17]


def _raw_data_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        count = version // 7 + 2
        result -= (25 * count - 10) * count - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int, ecl) -> int:
    return (_raw_data_modules(version) // 8
            - _ECC_CODEWORDS_PER_BLOCK[ecl[0]][version] * _NUM_ERROR_CORRECTION_BLOCKS[ecl[0]][version])


def _rs_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_multiply(root, 0x02)
    return result


def _rs_remainder(data, divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _rs_multiply(coef, factor)
    return result


class QrCode:
    """size × size 的模块矩阵；modules[y][x] 为真是深色。"""

    def __init__(self, text: str, ecl=MEDIUM, boost_ecl: bool = True):
        mode, count, bits = _segment(text)
        for version in range(1, 41):
            ccbits = _char_count_bits(mode, version)
            used = 4 + ccbits + len(bits) if count < (1 << ccbits) else None
            if used is not None and used <= _data_codewords(version, ecl) * 8:
                break
        else:
            raise DataTooLong("二维码装不下这么长的内容")
        for better in (MEDIUM, QUARTILE, HIGH):
            if boost_ecl and used <= _data_codewords(version, better) * 8:
                ecl = better
        self.version, self.ecl = version, ecl
        self.size = version * 4 + 17

        stream: list[int] = []
        for value, width in ((mode[0], 4), (count, _char_count_bits(mode, version))):
            stream.extend((value >> i) & 1 for i in reversed(range(width)))
        stream.extend(bits)
        capacity = _data_codewords(version, ecl) * 8
        stream.extend([0] * min(4, capacity - len(stream)))
        stream.extend([0] * (-len(stream) % 8))
        pad = 0xEC
        while len(stream) < capacity:
            stream.extend((pad >> i) & 1 for i in reversed(range(8)))
            pad ^= 0xEC ^ 0x11
        data = bytearray(len(stream) // 8)
        for i, bit in enumerate(stream):
            data[i >> 3] |= bit << (7 - (i & 7))

        self.modules = [[False] * self.size for _ in range(self.size)]
        self._function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()
        self._draw_codewords(self._add_ecc_and_interleave(data))
        best, best_penalty = 0, None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            penalty = self._penalty()
            if best_penalty is None or penalty < best_penalty:
                best, best_penalty = mask, penalty
            self._apply_mask(mask)      # 异或两次复原
        self.mask = best
        self._apply_mask(best)
        self._draw_format_bits(best)
        del self._function

    # ---- 功能图形 ----

    def _set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self._function[y][x] = True

    def _draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= x + dx < size and 0 <= y + dy < size:
                        self._set(x + dx, y + dy, max(abs(dx), abs(dy)) not in (2, 4))
        positions = self._alignment_positions()
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format_bits(0)
        self._draw_version()

    def _alignment_positions(self) -> list[int]:
        if self.version == 1:
            return []
        count = self.version // 7 + 2
        step = (self.version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
        return [6] + [self.size - 7 - i * step for i in reversed(range(count - 1))]

    def _draw_format_bits(self, mask: int) -> None:
        data = self.ecl[1] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 != 0
        size = self.size
        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, size - 15 + i, bit(i))
        self._set(8, size - 8, True)

    def _draw_version(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self._set(a, b, dark)
            self._set(b, a, dark)

    # ---- 数据 ----

    def _add_ecc_and_interleave(self, data: bytearray) -> list[int]:
        row = self.ecl[0]
        blocks_n = _NUM_ERROR_CORRECTION_BLOCKS[row][self.version]
        ecc_len = _ECC_CODEWORDS_PER_BLOCK[row][self.version]
        raw = _raw_data_modules(self.version) // 8
        short_n = blocks_n - raw % blocks_n
        short_len = raw // blocks_n
        divisor = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(blocks_n):
            chunk = list(data[k:k + short_len - ecc_len + (0 if i < short_n else 1)])
            k += len(chunk)
            ecc = _rs_remainder(chunk, divisor)
            if i < short_n:
                chunk.append(0)
            blocks.append(chunk + ecc)
        result = []
        for i in range(len(blocks[0])):
            for j, block in enumerate(blocks):
                if i != short_len - ecc_len or j >= short_n:
                    result.append(block[i])
        return result

    def _draw_codewords(self, data: list[int]) -> None:
        size, i = self.size, 0
        for right in range(size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = size - 1 - vert if upward else vert
                    if not self._function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1

    def _apply_mask(self, mask: int) -> None:
        pattern = _MASKS[mask]
        for y in range(self.size):
            row, function = self.modules[y], self._function[y]
            for x in range(self.size):
                if pattern(x, y) == 0 and not function[x]:
                    row[x] = not row[x]

    # ---- 罚分 ----

    def _penalty(self) -> int:
        size, modules = self.size, self.modules
        result = 0
        lines = [modules[y] for y in range(size)] + [[modules[y][x] for y in range(size)] for x in range(size)]
        for line in lines:
            color, run = False, 0
            history = deque([0] * 7, 7)
            for cell in line:
                if cell == color:
                    run += 1
                    if run == 5:
                        result += _PENALTY_N1
                    elif run > 5:
                        result += 1
                else:
                    self._add_history(run, history)
                    if not color:
                        result += self._count_finder(history) * _PENALTY_N3
                    color, run = cell, 1
            if color:
                self._add_history(run, history)
                run = 0
            self._add_history(run + size, history)
            result += self._count_finder(history) * _PENALTY_N3
        for y in range(size - 1):
            for x in range(size - 1):
                if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                    result += _PENALTY_N2
        dark = sum(cell for row in modules for cell in row)
        total = size * size
        result += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * _PENALTY_N4
        return result

    def _add_history(self, run: int, history: deque) -> None:
        if history[0] == 0:
            run += self.size
        history.appendleft(run)

    @staticmethod
    def _count_finder(h: deque) -> int:
        n = h[1]
        core = n > 0 and h[2] == h[4] == h[5] == n and h[3] == n * 3
        return int(core and h[0] >= n * 4 and h[6] >= n) + int(core and h[6] >= n * 4 and h[0] >= n)


def svg(text: str, border: int = 4, dark: str = "#1c1915", light: str = "#efe5d0") -> str:
    """二维码的 SVG：一个底色方块加一条路径（每行连着的深色模块合成一段），边上留 border 个模块的空白。"""
    code = QrCode(text)
    side = code.size + 2 * border
    parts = []
    for y, row in enumerate(code.modules):
        x = 0
        while x < code.size:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < code.size and row[x]:
                x += 1
            parts.append(f"M{start + border} {y + border}h{x - start}v1h-{x - start}z")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" shape-rendering="crispEdges">'
            f'<rect width="{side}" height="{side}" fill="{light}"/>'
            f'<path fill="{dark}" d="{"".join(parts)}"/></svg>')
