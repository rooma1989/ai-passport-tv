"""中文数字转阿拉伯数字：「中央五套」→「中央5套」，「第十二集」→「第12集」，「一百」→「100」。

SenseVoice 的 ITN 多数时候已经给出数字，这里补上它没转的。频道名和识别结果用同一个函数
转换，两边写法一致，匹配不受影响。

「一点」「一下」「下一集」「上一个」里的「一」不是数量，保持原样，免得「大声一点」变成「大声1点」。
"""

from __future__ import annotations

import re

DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}
UNITS = {"十": 10, "百": 100, "千": 1000}
_RUN = re.compile("[零〇一二两三四五六七八九十百千]+")
# 单独一个「一」后面跟着这些字时是虚指，不是数：一点、一下、一些、一会儿、一样、一直、一起、一个。
_SOFT_AFTER = set("点下些会样直起个遍次切")
# 前面是这些字时「一」也不是数量：下一集、上一个、再一次、换一个、统一。
_SOFT_BEFORE = set("下上再换统唯")


def parse(text: str) -> int | None:
    """一段中文数字的值；不是合法写法时返回 None。支持到「千」，够频道号、集数和音量用。"""
    if not text:
        return None
    if all(ch in DIGITS for ch in text):
        # 逐位读：「一二」= 12，「二零」= 20。
        value = 0
        for ch in text:
            value = value * 10 + DIGITS[ch]
        return value
    total, digit, zero = 0, None, False
    last_unit = 10_000
    for ch in text:
        if ch in "零〇":
            if digit is not None:
                return None
            zero = True
        elif ch in DIGITS:
            if digit is not None:
                return None
            digit = DIGITS[ch]
        else:
            unit = UNITS[ch]
            if unit >= last_unit:
                return None
            total += (1 if digit is None else digit) * unit
            digit, last_unit, zero = None, unit, False
    if digit is not None:
        # 「一百五」= 150：末位省略的单位是上一个单位的十分之一；「一百零五」的末位就是个位。
        total += digit if zero or last_unit in (10, 10_000) else digit * (last_unit // 10)
    return total


def to_digits(text: str) -> str:
    """把文本里的中文数字换成阿拉伯数字，其余字符不动。"""

    def replace(match: re.Match) -> str:
        run = match.group(0)
        start, end = match.start(), match.end()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        if run == "一" and before != "第" and (after in _SOFT_AFTER or before in _SOFT_BEFORE):
            return run
        value = parse(run)
        return run if value is None else str(value)

    return _RUN.sub(replace, text)
