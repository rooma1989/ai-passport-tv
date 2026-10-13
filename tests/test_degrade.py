"""Fitting a live frame into its byte target: server/degrade.py (TV_DEGRADE).

    PYTHONPATH=. python tests/test_degrade.py
"""
import os
import random
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import degrade, frames

W, H = frames.WIDTH, frames.HEIGHT
SP, N = frames.STRIPE_PIXELS, frames.STRIPES
FLAT = 0x49  # a mid grey: R 2, G 2, B 1


def noisy_stripes(seed, which, base=None):
    """A frame whose stripes in `which` are random indices (incompressible), the rest flat."""
    rng = random.Random(seed)
    out = bytearray(base if base is not None else bytes([FLAT]) * frames.FRAME_PIXELS)
    for at in which:
        out[at * SP:(at + 1) * SP] = rng.getrandbits(8 * SP).to_bytes(SP, "little")
    return bytes(out)


def wire(chosen):
    return frames._wire_size(chosen)


def packed(picture, at):
    return len(zlib.compress(picture[at * SP:(at + 1) * SP], frames._COMPRESS_LEVEL))


def sent(chosen):
    return {at for at, s in enumerate(chosen) if s}


class Steady(unittest.TestCase):
    def encoder(self, **kw):
        kw.setdefault("levels", ("rowsh", "rows", "rows+cols", "rows4+cols"))
        return degrade.PictureEncoder("steady", **kw)

    def test_a_frame_that_fits_is_sent_exactly_as_it_came(self):
        e = self.encoder()
        first = noisy_stripes(1, range(N))
        chosen, level = e.encode(first, 1 << 30)
        self.assertEqual((sent(chosen), level, e.shown), (set(range(N)), 0, first))
        second = noisy_stripes(2, range(N))
        chosen, level = e.encode(second, 1 << 30)
        self.assertEqual((sent(chosen), level, e.shown), (set(range(N)), 0, second))

    def test_a_small_overshoot_defers_the_least_changed_stripe_and_keeps_full_colour(self):
        e = self.encoder()
        base = noisy_stripes(1, range(N))
        e.encode(base, 1 << 30)
        # Every stripe changes; stripe 7 changes least.
        nxt = bytearray(noisy_stripes(2, [s for s in range(N) if s != 7], base))
        nxt[7 * SP:7 * SP + SP // 4] = bytes(SP // 4)
        nxt = bytes(nxt)
        full = 2 + 2 * N + sum(packed(nxt, at) for at in range(N))
        target = full - 10  # 10 bytes over: about 0.02 %
        chosen, level = e.encode(nxt, target)
        self.assertEqual(level, 0, "no colour or detail is given up for a small overshoot")
        self.assertLessEqual(wire(chosen), target)
        self.assertEqual(sent(chosen), set(range(N)) - {7})
        lo = 7 * SP
        self.assertEqual(e.shown[lo:lo + SP], base[lo:lo + SP], "the deferred stripe stays as shown")
        others = [at for at in range(N) if at != 7]
        self.assertTrue(all(e.shown[at * SP:(at + 1) * SP] == nxt[at * SP:(at + 1) * SP] for at in others))

    def test_a_deferred_stripe_waits_at_most_max_wait_frames(self):
        e = self.encoder(levels=(), max_wait=2)
        picture = noisy_stripes(0, range(N))
        e.encode(picture, 1 << 30)
        one = packed(picture, 0)
        target = 2 + 2 * N + (N - 3) * one  # room for 12 of the 15 changing stripes
        last = {at: 0 for at in range(N)}
        worst = 0
        for t in range(1, 40):
            picture = noisy_stripes(t, range(N))
            chosen, level = e.encode(picture, target)
            self.assertLessEqual(wire(chosen), target)
            for at in sent(chosen):
                worst = max(worst, t - last[at] - 1)
                last[at] = t
        self.assertLessEqual(worst, 2, "no stripe is more than two frames behind")

    def test_a_large_overshoot_climbs_one_level_per_frame(self):
        e = self.encoder(climb=1)
        e.encode(noisy_stripes(0, range(N)), 1 << 30)
        levels = []
        for t in range(1, 6):
            chosen, level = e.encode(noisy_stripes(t, range(N)), 6000)
            self.assertLessEqual(wire(chosen), 6000)
            levels.append(level)
        self.assertEqual(levels[:3], [1, 2, 3])
        self.assertTrue(all(b - a in (0, 1) for a, b in zip(levels, levels[1:])))

    def test_a_short_spike_is_deferred_not_degraded(self):
        e = self.encoder()
        picture = noisy_stripes(0, range(N))
        e.encode(picture, 1 << 30)
        levels = []
        for t in range(1, 3):  # two frames of a scene cut, far over the target
            chosen, level = e.encode(noisy_stripes(t, range(N)), 20000)
            self.assertLessEqual(wire(chosen), 20000)
            levels.append(level)
        self.assertEqual(levels, [0, 0], "no level is climbed for fewer than CLIMB frames")

    def test_the_level_holds_steady_under_a_steady_load(self):
        e = self.encoder(dwell=6)
        rng = random.Random(5)
        e.encode(noisy_stripes(0, range(N)), 1 << 30)
        levels = []
        for t in range(1, 60):
            # Most stripes change every frame, by different amounts: a busy scene.
            changing = [at for at in range(N) if rng.random() < 0.8]
            _, level = e.encode(noisy_stripes(t, changing, e.shown), 30000)
            levels.append(level)
        changes = sum(1 for a, b in zip(levels, levels[1:]) if a != b)
        self.assertLessEqual(changes, 4, f"levels {levels}")
        downs = [i for i, (a, b) in enumerate(zip(levels, levels[1:])) if b < a]
        ups = [i for i, (a, b) in enumerate(zip(levels, levels[1:])) if b > a]
        self.assertFalse(any(0 < d - u < 6 for u in ups for d in downs), "no step down within the dwell")

    def test_it_returns_to_full_colour_when_the_scene_calms(self):
        e = self.encoder(dwell=4)
        e.encode(noisy_stripes(0, range(N)), 1 << 30)
        for t in range(1, 5):
            e.encode(noisy_stripes(t, range(N)), 6000)
        self.assertGreater(e.level, 0)
        still = noisy_stripes(99, [3])
        for _ in range(30):
            chosen, level = e.encode(still, 6000)
            self.assertLessEqual(wire(chosen), 6000)
        self.assertEqual(level, 0)
        self.assertEqual(e.shown, still, "every stripe is redrawn in full colour")

    def test_the_refresh_resends_what_the_panel_has_without_changing_it(self):
        e = self.encoder()
        e.encode(noisy_stripes(0, range(N)), 1 << 30)
        for _ in range(4):
            e.encode(noisy_stripes(1, range(N)), 6000)
        before = e.shown
        tick = e.tick
        # Only a handful of pixels move: nothing is worth sending but the refresh.
        nudged = bytearray(e.shown)
        nudged[0] ^= 1
        chosen, _ = e.encode(bytes(nudged), 1 << 30)
        refresh = tick % N
        self.assertIn(refresh, sent(chosen))
        lo = refresh * SP
        self.assertEqual(zlib.decompress(chosen[refresh]), before[lo:lo + SP])

    def test_the_refresh_rotates(self):
        e = self.encoder()
        picture = noisy_stripes(0, range(N))
        e.encode(picture, 1 << 30)
        seen = [sent(e.encode(picture, 1 << 30)[0]) for _ in range(N)]
        self.assertEqual([s for (s,) in seen], [(t + 1) % N for t in range(N)])

    def test_the_first_frame_climbs_until_it_fits(self):
        e = self.encoder()
        picture = noisy_stripes(0, range(N))
        top = len(e.levels) - 1
        target = 2 + 2 * N + sum(packed(e.render(picture, top), at) for at in range(N))
        chosen, level = e.encode(picture, target)
        self.assertEqual(level, top)
        self.assertLessEqual(wire(chosen), target)
        self.assertEqual(sent(chosen), set(range(N)), "a whole picture, no stripe left blank")

    def test_a_first_frame_too_big_at_every_level_leaves_stripes_for_later(self):
        e = self.encoder()
        picture = noisy_stripes(0, range(N))
        chosen, level = e.encode(picture, 8000)
        self.assertLessEqual(wire(chosen), 8000)
        left = set(range(N)) - sent(chosen)
        self.assertTrue(left)
        later = set()
        for _ in range(4):
            chosen, _ = e.encode(picture, 8000)
            later |= sent(chosen)
        self.assertLessEqual(left, later, "the stripes left out are sent next")

    def test_a_target_nothing_meets_still_sends_the_refresh(self):
        e = self.encoder()
        e.encode(noisy_stripes(0, range(N)), 1 << 30)
        chosen, _ = e.encode(noisy_stripes(1, range(N)), 10)
        self.assertEqual(len(sent(chosen)), 1)

    def test_replacing_the_picture_starts_from_it(self):
        e = self.encoder()
        e.encode(noisy_stripes(0, range(N)), 1 << 30)
        notice = noisy_stripes(7, [])
        e.shown = notice
        chosen, _ = e.encode(notice, 1 << 30)
        self.assertEqual(len(sent(chosen)), 1, "only the refresh: the panel already has it")

    def test_a_wrong_sized_frame_or_unknown_setting_is_refused(self):
        with self.assertRaises(ValueError):
            self.encoder().encode(b"\x00" * 10, 1000)
        with self.assertRaises(ValueError):
            degrade.PictureEncoder("sharpen")
        with self.assertRaises(ValueError):
            degrade.PictureEncoder("steady", levels=("blur",))


class TintNeutralSteps(unittest.TestCase):
    def channel_error(self, table, shift, top):
        errors = []
        for level in range(top + 1):
            index = level << shift
            errors.append(((table[index] >> shift) & top) - level)
        return errors

    def test_colour_steps_round_symmetrically_and_keep_black_white_and_blue(self):
        for table in (degrade._N1, degrade._N2):
            for shift in (5, 2):
                errors = self.channel_error(table, shift, 7)
                self.assertEqual(sum(errors), 0, "no channel drifts up or down on average")
                self.assertEqual((errors[0], errors[7]), (0, 0))
                self.assertLessEqual(max(map(abs, errors)), 1)
            self.assertEqual(self.channel_error(table, 0, 3), [0, 0, 0, 0])

    def test_row_pairs_are_equal_and_keep_a_dithered_colour(self):
        # ffmpeg's rgb8 dithers: alternate G 3 and 4 by row, B 1 and 2 by column.
        picture = bytes((((3 + y % 2) << 2) | (1 + x % 2)) for y in range(H) for x in range(W))
        out = degrade._rows(picture, None)
        for y in range(0, H, 2):
            self.assertEqual(out[y * W:(y + 1) * W], out[(y + 1) * W:(y + 2) * W])

        def mean(p, field):
            return sum(field(i) for i in p) / len(p)
        for field in (lambda i: (i >> 2) & 7, lambda i: i & 3):
            self.assertAlmostEqual(mean(out, field), mean(picture, field), delta=0.01)

    def test_column_pairs_are_equal_and_keep_a_dithered_colour(self):
        picture = bytes((((3 + x % 2) << 5) | (1 + y % 2)) for y in range(H) for x in range(W))
        out = degrade._cols(picture, None)
        self.assertEqual(out[0::2], out[1::2])
        for field in (lambda i: i >> 5, lambda i: i & 3):
            self.assertAlmostEqual(sum(map(field, out)) / len(out),
                                   sum(map(field, picture)) / len(picture), delta=0.01)

    def test_quarter_rows_repeat_four_times(self):
        out = degrade._quarter_rows(noisy_stripes(4, range(N)), None)
        for y in range(0, H, 4):
            self.assertEqual(len({out[(y + k) * W:(y + k + 1) * W] for k in range(4)}), 1)

    def test_half_rows_merge_every_other_pair_only(self):
        picture = noisy_stripes(3, range(N))
        out = degrade._half_rows(picture, None)
        row = lambda p, y: p[y * W:(y + 1) * W]
        self.assertEqual(row(out, 0), row(out, 1))
        self.assertEqual(row(out, 4), row(out, 5))
        self.assertEqual((row(out, 2), row(out, 3)), (row(picture, 2), row(picture, 3)))


class LegacyModes(unittest.TestCase):
    def test_ladder_is_what_live_did_before(self):
        e = degrade.PictureEncoder("ladder")
        shown = None
        for t in range(6):
            raw = noisy_stripes(t, range(0, N, 1 + t % 3))
            chosen, rung = e.encode(raw, 20000)
            want, drawn, want_rung = frames.encode_within(raw, shown, t, 20000)
            want = frames.fill_stripes(drawn, shown, want, 20000)
            shown = frames.apply_stripes(drawn, shown, want)
            self.assertEqual((chosen, rung, e.shown), (want, want_rung, shown))

    def test_delta_without_fill_skips_the_fill_pass(self):
        e = degrade.PictureEncoder("ladder", fill=False)
        first = noisy_stripes(0, range(N))
        e.encode(first, 1 << 30)
        raw = noisy_stripes(1, [2], first)
        raw = raw[:5 * SP] + bytes([raw[5 * SP] ^ 1]) + raw[5 * SP + 1:]
        chosen, _ = e.encode(raw, 1 << 30)
        self.assertNotIn(5, sent(chosen), "a change under the tolerance waits without fill")


if __name__ == "__main__":
    unittest.main()
