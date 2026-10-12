"""模糊匹配：字的最长公共子序列，加上无声调拼音的音节编辑距离。

识别结果常把同音字认错（「湖南为是」），所以分数取两者中较大的：字面相似度，和按拼音
逐音节比较的相似度。多音字的每个读音都算（pinyin_table），平翘舌、前后鼻音、n/l 不分
的读音按半个错算。
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from . import numbers
from .pinyin_table import DATA

_READINGS: dict[str, tuple[str, ...]] = {}
for _line in DATA.splitlines():
    _syllable, _, _chars = _line.partition(" ")
    for _char in _chars:
        _READINGS[_char] = _READINGS.get(_char, ()) + (_syllable,)
del _line, _syllable, _chars

# 阿拉伯数字按中文读音比较：「5套」和「舞套」读起来一样。
_DIGIT_READINGS = {"0": ("ling",), "1": ("yi", "yao"), "2": ("er", "liang"), "3": ("san",),
                   "4": ("si",), "5": ("wu",), "6": ("liu",), "7": ("qi",), "8": ("ba",),
                   "9": ("jiu",)}
# 去掉的标点与空白；「+」先换成「加」（CCTV-5+ 读作「五加」）。
_PUNCT = re.compile(r"[\s\-_·•・.,，。、!！?？:：;；'\"“”‘’()（）\[\]【】《》<>〈〉「」『』~～…/\\|*#&^%$@`]+")
_TAG = re.compile(r"<\|[^|]*\|>")
_PAREN = re.compile(r"[(（][^)）]*[)）]")


def normalize(text: str) -> str:
    """全角转半角、转小写、去标点和 SenseVoice 的标签、中文数字转阿拉伯数字。"""
    text = _TAG.sub("", text or "")
    text = unicodedata.normalize("NFKC", text).lower().replace("+", "加").replace("百分之", "")
    text = _PUNCT.sub("", text)
    return numbers.to_digits(text)


def strip_parens(name: str) -> str:
    """去掉名字里括号中的说明：「王菲精选145首(10小时)」→「王菲精选145首」。"""
    return _PAREN.sub("", name)


def _fuzzy(syllable: str) -> str:
    """平翘舌、n/l、前后鼻音合并成一个写法。"""
    for hard, soft in (("zh", "z"), ("ch", "c"), ("sh", "s")):
        if syllable.startswith(hard):
            syllable = soft + syllable[2:]
    if syllable.startswith("l"):
        syllable = "n" + syllable[1:]
    for back, front in (("ang", "an"), ("eng", "en"), ("ing", "in")):
        if syllable.endswith(back):
            syllable = syllable[:-len(back)] + front
    return syllable


_LATIN = re.compile(r"[a-z]+|.", re.S)


@lru_cache(maxsize=4096)
def syllables(text: str) -> tuple[tuple[frozenset, frozenset], ...]:
    """每个音节的 (读音集合, 模糊读音集合)。

    一串拉丁字母算一个音节（「小Lin说」读作 xiao lin shuo，和「小林说」一样）；
    数字按中文读音；其他符号按字本身比较。"""
    out = []
    for token in _LATIN.findall(text):
        readings = _READINGS.get(token) or _DIGIT_READINGS.get(token) or (token,)
        out.append((frozenset(readings), frozenset(_fuzzy(r) for r in readings)))
    return tuple(out)


_DIGIT_RUN = re.compile(r"\d+")


def digits(text: str) -> tuple[str, ...]:
    """文本里的数字串；两边都有数字而数字不同时，名字一定不是同一个（CCTV-5 不是 CCTV-15）。"""
    return tuple(_DIGIT_RUN.findall(text))


def common_run(a: str, b: str) -> int:
    """最长公共子串的长度（连续的字）。"""
    best = 0
    previous = [0] * (len(b) + 1)
    for ca in a:
        current = [0]
        for j, cb in enumerate(b, 1):
            run = previous[j - 1] + 1 if ca == cb else 0
            current.append(run)
            best = max(best, run)
        previous = current
    return best


def pinyin_ratio(a: str, b: str) -> float:
    """按音节的编辑距离换成 0..1 的相似度：读音有交集算对，模糊读音有交集算半个错。"""
    sa, sb = syllables(a), syllables(b)
    if not sa or not sb:
        return 0.0
    previous = [float(j) for j in range(len(sb) + 1)]
    for i, (exact_a, fuzzy_a) in enumerate(sa, 1):
        current = [float(i)]
        for j, (exact_b, fuzzy_b) in enumerate(sb, 1):
            cost = 0.0 if exact_a & exact_b else 0.5 if fuzzy_a & fuzzy_b else 1.0
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return max(0.0, 1.0 - previous[-1] / max(len(sa), len(sb)))


def lcs_ratio(a: str, b: str) -> float:
    """字的最长公共子序列占两者平均长度的比例。"""
    if not a or not b:
        return 0.0
    previous = [0] * (len(b) + 1)
    for ca in a:
        current = [0]
        for j, cb in enumerate(b, 1):
            current.append(previous[j - 1] + 1 if ca == cb else max(previous[j], current[j - 1]))
        previous = current
    return 2.0 * previous[-1] / (len(a) + len(b))


def score(query: str, name: str) -> float:
    """query 与 name（都已 normalize）的相似度，0..1。"""
    if not query or not name:
        return 0.0
    if query == name:
        return 1.0
    return max(lcs_ratio(query, name), pinyin_ratio(query, name))
