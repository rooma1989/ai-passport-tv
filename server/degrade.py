"""把一帧放进字节目标：选条带、推迟条带与降质档位，live.py 每帧调用一次。

TV_DEGRADE 选择帧放不下时的做法：

- `steady`（默认，无需 numpy）：先推迟变化最小的条带（设备保留上一帧的这几条），
  只有持续超出较多时才降一档；档位带滞回，一次只走一档，且每档至少停留 DWELL 帧。
  各档只做不改变整体色调、不产生横向拖影的变换（见 STEPS）。
- `ladder`：frames.encode_within，逐档降色，每帧重新选档。
- `perceptual` / `perceptual_2d` / `mosaic`：perceptual.encode_within，需 numpy。

旧模式保留以便对比；度量与对比图见 tools/degrade_lab.py 与
docs/superpowers/specs/2026-10-13-picture-degrade-design.md。
"""

from __future__ import annotations

import os
import sys
import zlib
from array import array

from . import frames, perceptual

WIDTH, HEIGHT = frames.WIDTH, frames.HEIGHT
STRIPES, STRIPE_PIXELS = frames.STRIPES, frames.STRIPE_PIXELS

LEGACY_MODES = ("ladder", *perceptual.MODES)
NUMPY_MODES = perceptual.MODES
MODES = ("steady", *LEGACY_MODES)
DEFAULT = "steady"

# --- 档位里的变换：索引帧（及设备已显示的画面）-> 索引帧，只用标准库 ---


def _channel_table(rg: list[int], b: list[int]) -> bytes:
    """按通道映射 3-3-2 索引的查表，供 bytes.translate 使用。"""
    return bytes((rg[i >> 5] << 5) | (rg[(i >> 2) & 7] << 2) | b[i & 3] for i in range(256))


# 色阶合并（只供 tools/degrade_lab.py 对比，不在默认档位里）：合并相邻的奇数个色阶取中间值，
# 均匀分布下误差对称；0 与满值保留。蓝只有 4 级（步长 85），合并会使白偏黄，故不动。
# n1：红绿 {2,3,4} -> 3，8 级变 6 级。n2：红绿 {1,2,3} -> 2、{4,5,6} -> 5，变 4 级。
# 实测仍偏色：实际画面里某一级常占多数（例如米色路面的红绿都是 4），只动红绿不动蓝时
# 这一片整体偏蓝（实测 6~14），所以默认档位只做取平均的空间合并。
_KEEP_B = [0, 1, 2, 3]
_N1 = _channel_table([0, 1, 3, 3, 3, 5, 6, 7], _KEEP_B)
_N2 = _channel_table([0, 2, 2, 2, 5, 5, 5, 7], _KEEP_B)


def _levels(i: int) -> tuple[int, int, int]:
    return i >> 5, (i >> 2) & 7, i & 3


def _pair_table(pick) -> bytes:
    """65536 项的查表：下标为 a | b << 8（a、b 为两个索引），值为 pick(a, b)。"""
    return bytes(pick(n & 255, n >> 8) for n in range(65536))


def _mean_index(a: int, b: int, up: bool) -> int:
    """两个索引逐通道取平均；恰在两级之间时 up 决定进位（见 _round_mask）。"""
    out = 0
    for shift, top, x, y in zip((5, 2, 0), (7, 7, 3), _levels(a), _levels(b)):
        total = x + y
        level = (total + 1) // 2 if up else total // 2
        out |= min(level, top) << shift
    return out


_MEAN = None
_MASKS: dict[tuple[int, int, int, int], bytes] = {}


def prepare() -> None:
    """建好 steady 用的查表；PictureEncoder 构造时调用，不让首个降质帧付建表的时间。"""
    _tables()


def _tables():
    """首次用到时才建 131072 项的平均色表（约 0.2 s），不用 steady 的进程不付这个代价。"""
    global _MEAN
    if _MEAN is None:
        _MEAN = (_pair_table(lambda a, b: _mean_index(a, b, False))
                 + _pair_table(lambda a, b: _mean_index(a, b, True)))



def _pairs(a: bytes, b: bytes) -> array:
    """两段等长字节逐位配对成 a | b << 8 的 16 位数组。"""
    both = bytearray(2 * len(a))
    both[0::2] = a
    both[1::2] = b
    out = array("H")
    out.frombytes(both)
    if sys.byteorder == "big":
        out.byteswap()
    return out


def _round_mask(n: int, line: int, phase: int = 0, band: int = 1) -> bytes:
    """每个位置平均恰在两级之间时是否进位：逐像素交替，每 8 个像素换一次相位，
    每 `band` 行再换一次相位（棋盘格）。

    ffmpeg 的 rgb8 用 8x8 以内的有序抖动，"恰在两级之间"多出现在特定的奇偶列上：
    单纯按奇偶列交替会与抖动相关而整体偏色（实测蓝偏 7~11）；伪随机不偏但 deflate
    压不动；8 个像素一组交替不偏，但蓝一级为 85，成组进退是肉眼可见的竖条，各行
    相同的逐像素交替也成竖线。逐像素交替、每 8 个像素换相位、逐行（组）错开时，
    每种抖动相位进位、舍去各半，整体不偏，图样细如原有的抖动，且规整可压。
    `band` 让 rows 合并出的相同各行仍然相同。图样每帧相同，静止画面不会因此闪动。
    两级平均（rows4）中两对行用相反的 `phase`，两次舍入才相互抵消而不是叠加。
    """
    key = (n, line, phase, band)
    if key not in _MASKS:
        _MASKS[key] = bytes((i % line + (i % line >> 3) + i // line // band + phase) & 1
                            for i in range(n))
    return _MASKS[key]


def _means(a: bytes, b: bytes, line: int, phase: int = 0, band: int = 1) -> bytes:
    """a、b 逐位取平均色（见 _mean_index），恰在两级之间时按 _round_mask 进位；`line` 为行长。"""
    _tables()
    n = len(a)
    keys = bytearray(4 * n)
    keys[0::4] = a
    keys[1::4] = b
    keys[2::4] = _round_mask(n, line, phase, band)
    packed = array("I")
    if packed.itemsize != 4:
        packed = array("L")
    packed.frombytes(keys)
    if sys.byteorder == "big":
        packed.byteswap()
    return _lookup(_MEAN, packed)


def _lookup(table: bytes, keys) -> bytes:
    return bytes(map(table.__getitem__, keys))


def _merge_rows(raw: bytes, every: int, size: int = 2) -> bytes:
    """每 `every` 行中的头 `size` 行（2 或 4）合成一行写 `size` 遍（取平均色），
    deflate 把重复的行编成对上一行的引用。

    取平均而非复制某一行：ffmpeg 的 rgb8 按行列抖动，只留一行会只留一种相位而偏色。
    """
    w = WIDTH
    tops = range(0, HEIGHT - size + 1, every)

    def lines(offset: int) -> bytes:
        return b"".join(raw[(y + offset) * w:(y + offset + 1) * w] for y in tops)
    mean = _means(lines(0), lines(1), w)
    if size == 4:
        mean = _means(mean, _means(lines(2), lines(3), w, 1), w)
    out = bytearray(raw)
    for j, y in enumerate(tops):
        line = mean[j * w:(j + 1) * w]
        for k in range(size):
            out[(y + k) * w:(y + k + 1) * w] = line
    return bytes(out)


def _rows(raw: bytes, shown: bytes | None) -> bytes:
    """垂直分辨率减半：每两行取平均写两遍。"""
    return _merge_rows(raw, 2)


def _half_rows(raw: bytes, shown: bytes | None) -> bytes:
    """只合并一半的行对（每四行的头两行），代价与画质介于原帧与 rows 之间。"""
    return _merge_rows(raw, 4)


def _quarter_rows(raw: bytes, shown: bytes | None) -> bytes:
    """垂直分辨率降到四分之一：每四行取平均写四遍，只在字节目标极紧时用。"""
    return _merge_rows(raw, 4, 4)


def _cols(raw: bytes, shown: bytes | None) -> bytes:
    """水平分辨率减半：每两列取平均写两遍。"""
    # 每 4 行同一图样：在 rows / rows4 之后用时，合并出的相同各行仍然相同。
    mean = _means(raw[0::2], raw[1::2], WIDTH // 2, band=4)
    out = bytearray(len(raw))
    out[0::2] = mean
    out[1::2] = mean
    return bytes(out)


STEPS = {
    "n1": lambda raw, shown: raw.translate(_N1),
    "n2": lambda raw, shown: raw.translate(_N2),
    "rows": _rows,
    "rowsh": _half_rows,
    "rows4": _quarter_rows,
    "cols": _cols,
}

_PAIR_STEPS = ("rows", "rowsh", "rows4", "cols")

# steady 的档位，0 档为原帧；每档是若干变换依次作用（"+" 连接）。
# 由 tools/degrade_lab.py 在多类片源上比较后选定，见设计说明。
STEADY_LEVELS = tuple(os.environ.get("TV_STEADY_LEVELS", "rowsh,rows,rows+cols,rows4+cols").split(","))

# 控制参数（见设计说明的测量）：
# DEFER：变化条带的总字节不超过目标的 1 + DEFER 倍时只推迟条带，不降档。
# CLIMB：连续这么多帧超出 1 + DEFER 倍才升一档；镜头切换这类一两帧的尖峰由推迟吸收。
# MAX_WAIT：一条有变化的条带最多连续推迟几帧，之后排在最前（仍须放得下）。
# DWELL：换档后至少停留几帧才试探回升；回升要求低一档的变化条带不超过目标的 DOWN 倍
# （DOWN < 1 + DEFER，两者之间的差就是滞回，档位不会来回跳）。
DEFER = float(os.environ.get("TV_STEADY_DEFER", "0.25"))
CLIMB = int(os.environ.get("TV_STEADY_CLIMB", "3"))
MAX_WAIT = int(os.environ.get("TV_STEADY_MAX_WAIT", "2"))
DWELL = int(os.environ.get("TV_STEADY_DWELL", "12"))
DOWN = float(os.environ.get("TV_STEADY_DOWN", "1.1"))

_HEADER_BYTES = 2


def _stripe(picture: bytes, at: int) -> bytes:
    return picture[at * STRIPE_PIXELS:(at + 1) * STRIPE_PIXELS]


def _deflate(data: bytes) -> bytes:
    return zlib.compress(data, frames._COMPRESS_LEVEL)


class _Plan:
    """某一档下这一帧的画面、需要发的条带及其压缩结果。"""

    def __init__(self, level: int, drawn: bytes):
        self.level, self.drawn = level, drawn
        self.changed: dict[int, int] = {}   # 内容有变化的条带 -> 变化的像素数
        self.small: dict[int, int] = {}     # 有差异但不足门限的条带
        self.packed: dict[int, bytes] = {}
        self.need = 0

    def packed_stripe(self, at: int) -> bytes:
        if at not in self.packed:
            self.packed[at] = _deflate(_stripe(self.drawn, at))
        return self.packed[at]


class PictureEncoder:
    """一个频道的画面编码状态：设备已显示的画面、强制刷新的轮转、当前档位。

    encode() 返回（待发的压缩条带，档位）；条带为空串表示不发，设备保留原样。
    """

    def __init__(self, mode: str = DEFAULT, fill: bool = True,
                 min_diff: float = frames.DELTA_MAX_DIFF,
                 levels: tuple[str, ...] | None = None, defer: float = DEFER,
                 max_wait: int = MAX_WAIT, dwell: int = DWELL, down: float = DOWN,
                 climb: int = CLIMB):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if mode in NUMPY_MODES and not perceptual.AVAILABLE:
            raise RuntimeError(f"{mode} needs numpy")
        self.mode, self.fill, self.min_diff = mode, fill, min_diff
        self.defer, self.max_wait, self.dwell_frames, self.down = defer, max_wait, dwell, down
        self.climb = climb
        names = STEADY_LEVELS if levels is None else levels
        self.levels = [()] + [tuple(level.split("+")) for level in names if level]
        for level in self.levels:
            for step in level:
                if step not in STEPS:
                    raise ValueError(f"unknown step {step!r}; choose from {', '.join(STEPS)}")
        if mode == "steady" and any(step in _PAIR_STEPS for level in self.levels for step in level):
            prepare()
        self.tick = 0
        self.level = 0
        self._dwell = 0
        self._over = 0
        self._since_down = None   # 上次降档以来的帧数；None 表示不在观察期
        self._backoff = 1         # 降档后很快又升档时，下次试探回升前多等几倍 DWELL
        self.shown = None

    @property
    def shown(self) -> bytes | None:
        return self._shown

    @shown.setter
    def shown(self, picture: bytes | None) -> None:
        """设备画面被整帧替换（新频道、提示画面）：每条带都视为刚以 0 档发过。"""
        self._shown = picture
        self._source = picture          # 每条带上次发送时的原帧内容
        self._stripe_level = [0] * STRIPES
        self._waiting = [0] * STRIPES

    def encode(self, raw: bytes, target: int) -> tuple[list[bytes], int]:
        if len(raw) != frames.FRAME_PIXELS:
            raise ValueError(f"frame is {len(raw)} bytes, expected {frames.FRAME_PIXELS}")
        if self.mode == "steady":
            chosen = self._steady(raw, target)
        else:
            chosen = self._legacy(raw, target)
        self.tick += 1
        return chosen, self.level

    # --- 旧模式：与此前 live.py 中的写法逐行一致 ---

    def _legacy(self, raw: bytes, target: int) -> list[bytes]:
        shown = self._shown
        if self.mode in perceptual.MODES:
            chosen, drawn, rung = perceptual.encode_within(
                raw, shown, self.tick, target, self.level, min_diff=self.min_diff, mode=self.mode)
        else:
            chosen, drawn, rung = frames.encode_within(raw, shown, self.tick, target, self.min_diff)
        if self.fill:
            chosen = frames.fill_stripes(drawn, shown, chosen, target)
        self.level = rung
        self._shown = frames.apply_stripes(drawn, shown, chosen)
        return chosen

    # --- steady ---

    def render(self, raw: bytes, level: int) -> bytes:
        drawn = raw
        for step in self.levels[level]:
            drawn = STEPS[step](drawn, self._shown)
        return drawn

    def _plan(self, raw: bytes, level: int) -> _Plan:
        plan = _Plan(level, self.render(raw, level))
        floor = int(STRIPE_PIXELS * self.min_diff)
        shown, source = self._shown, self._source
        for at in range(STRIPES):
            lo, hi = at * STRIPE_PIXELS, (at + 1) * STRIPE_PIXELS
            if self._stripe_level[at] == level:
                # 同一档画出的条带：直接比画面，粗档吸收掉的细小变化不算。
                changed = frames.differing_pixels(plan.drawn[lo:hi], shown[lo:hi])
            else:
                # 别的档画出的条带：比原帧内容；内容没变时只差画质，不算变化。
                changed = frames.differing_pixels(raw[lo:hi], source[lo:hi])
            if changed > floor:
                plan.changed[at] = changed
            elif changed:
                plan.small[at] = changed
        refresh = self.tick % STRIPES
        plan.need = _HEADER_BYTES + 2 * STRIPES + sum(
            len(plan.packed_stripe(at)) for at in plan.changed)
        if refresh not in plan.changed:
            plan.need += len(self._refresh_payload(plan, refresh))
        return plan

    def _refresh_fresh(self, plan: _Plan, at: int) -> bool:
        """强制刷新的条带发本帧画面（有任何变化，或设备上的是更粗一档），还是重发设备已有的。"""
        return at in plan.changed or at in plan.small or self._stripe_level[at] > plan.level

    def _refresh_payload(self, plan: _Plan, at: int) -> bytes:
        """强制刷新的条带：有变化或可提升画质就发本帧画面，否则重发设备已有的画面
        （修复丢包，且不会把更细一档画的条带换成粗的）。"""
        if self._refresh_fresh(plan, at):
            return plan.packed_stripe(at)
        key = ("shown", at)
        if key not in plan.packed:
            plan.packed[key] = _deflate(_stripe(self._shown, at))
        return plan.packed[key]

    def _first(self, raw: bytes, target: int) -> list[bytes]:
        """首帧：设备上没有画面可留，逐档变粗直到整帧放得下，最粗仍放不下则按序丢条带。"""
        best = None
        for level in range(len(self.levels)):
            drawn = self.render(raw, level)
            packed = [_deflate(_stripe(drawn, at)) for at in range(STRIPES)]
            size = frames._wire_size(packed)
            if best is None or size < best[0]:
                best = (size, level, drawn, packed)
            if size <= target:
                break
        # 都放不下时取最小的一档（合成画面上粗档未必更小）。
        size, level, drawn, packed = best if size > target else (size, level, drawn, packed)
        spent = _HEADER_BYTES + 2 * STRIPES
        for at, data in enumerate(packed):
            if spent + len(data) > target:
                packed[at] = b""
            else:
                spent += len(data)
        self.level = level
        self._dwell = self.dwell_frames - 1 if level else 0
        # 没发出的条带设备上还没有画面（按黑记）；其"原帧内容"记成与本帧处处不同，
        # 下一帧它必是有变化的条带，按推迟规则尽快补上。
        shown, source = bytearray(drawn), bytearray(raw)
        for at, data in enumerate(packed):
            if not data:
                lo, hi = at * STRIPE_PIXELS, (at + 1) * STRIPE_PIXELS
                shown[lo:hi] = bytes(STRIPE_PIXELS)
                source[lo:hi] = bytes(b ^ 0xFF for b in raw[lo:hi])
        self.shown = bytes(shown)
        self._source = bytes(source)
        self._stripe_level = [level] * STRIPES
        return packed

    def _steady(self, raw: bytes, target: int) -> list[bytes]:
        if self._shown is None:
            return self._first(raw, target)
        top = len(self.levels) - 1
        plan = self._plan(raw, self.level)
        self._over = self._over + 1 if plan.need > target * (1 + self.defer) else 0
        if self._since_down is not None:
            self._since_down += 1
            if self._since_down > 2 * self.dwell_frames * self._backoff:
                self._since_down, self._backoff = None, 1  # 回升站住了
        if self._over >= self.climb and self.level < top:
            # 持续明显超出：升一档（每帧至多一档），其余由推迟吸收。
            if self._since_down is not None:
                # 刚回升又要升：下次多等一倍再试，最多 8 倍，免得在两档间来回。
                self._backoff = min(self._backoff * 2, 8)
                self._since_down = None
            higher = self._plan(raw, self.level + 1)
            self._over = 0
            if higher.need < plan.need:  # 粗一档确实更省才升（合成画面上未必）
                self.level += 1
                self._dwell = 0
                plan = higher
        elif self.level > 0:
            self._dwell += 1
            if self._dwell >= self.dwell_frames * self._backoff:
                lower = self._plan(raw, self.level - 1)
                self._dwell = 0
                if lower.need <= target * self.down:
                    self.level -= 1
                    self._since_down = 0
                    plan = lower
        return self._send(plan, raw, target)

    def _send(self, plan: _Plan, raw: bytes, target: int) -> list[bytes]:
        out = [b""] * STRIPES
        refresh = self.tick % STRIPES
        out[refresh] = self._refresh_payload(plan, refresh)
        spent = _HEADER_BYTES + 2 * STRIPES + len(out[refresh])
        sent = {refresh} if self._refresh_fresh(plan, refresh) else set()
        # 推迟到期的条带在前，其余按变化像素数从多到少；放不下的推迟，不阻挡后面更小的条带。
        order = sorted((at for at in plan.changed if at != refresh),
                       key=lambda at: (self._waiting[at] < self.max_wait, -plan.changed[at]))
        for at in order:
            data = plan.packed_stripe(at)
            if spent + len(data) <= target:
                out[at] = data
                spent += len(data)
                sent.add(at)
        for at in range(STRIPES):
            self._waiting[at] = self._waiting[at] + 1 if at in plan.changed and at not in sent else 0
        if self.fill:
            # 余下的字节：先把较粗档画出的条带补回本档，再补变化不足门限的条带。
            extra = [at for at in range(STRIPES) if not out[at] and at not in plan.changed
                     and self._stripe_level[at] > plan.level]
            extra.sort(key=lambda at: -self._stripe_level[at])
            extra += sorted((at for at in plan.small if not out[at]), key=lambda at: -plan.small[at])
            for at in extra:
                if out[at]:
                    continue
                data = plan.packed_stripe(at)
                if spent + len(data) <= target:
                    out[at] = data
                    spent += len(data)
                    sent.add(at)
        shown = bytearray(self._shown)
        source = bytearray(self._source)
        for at in sent:
            lo, hi = at * STRIPE_PIXELS, (at + 1) * STRIPE_PIXELS
            shown[lo:hi] = plan.drawn[lo:hi]
            source[lo:hi] = raw[lo:hi]
            self._stripe_level[at] = plan.level
        self._shown, self._source = bytes(shown), bytes(source)
        return out
