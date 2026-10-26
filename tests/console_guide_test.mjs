// Tests for the programme guide's time layout in server/relay/assets/console.js
// (the block between "节目表的几个纯函数" and "节目表的纯函数到此"; the page is a plain
// script, so the block is cut out and evaluated on its own).
// Run by tests/test_epg.py under Node (18+); also runnable on its own:
//   node tests/console_guide_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 节目表的几个纯函数');
const end = source.indexOf('// ---- 节目表的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { GUIDE, guideWindow, guideTicks, guideBlocks, nowNext, epgStale, progressOf, epgText, pinPad } = new Function(
  source.slice(begin, end) +
  '\nreturn { GUIDE, guideWindow, guideTicks, guideBlocks, nowNext, epgStale, progressOf, epgText, pinPad };')();

const H = 3600, M = 60;
// 2026-10-26 20:12:00 UTC: a fixed "now" that is not on a half hour.
const NOW = Date.UTC(2026, 9, 26, 20, 12) / 1000;
const HALF = 1800;

// The window: from an hour before now rounded down to the half hour, to spanH after now rounded up,
// and never past what the server keeps ("window" in /api/epg).
let w = guideWindow(NOW, 6, { before: H, after: 24 * H });
assert.equal(w.from, Date.UTC(2026, 9, 26, 19, 0) / 1000);
assert.equal(w.to, Date.UTC(2026, 9, 27, 2, 30) / 1000);
assert.equal(w.from % HALF, 0);
assert.equal(w.to % HALF, 0);
assert.deepEqual(guideWindow(NOW + 5 * M, 6, { before: H, after: 24 * H }), w, 'stable within the half hour (no refetch every minute)');
assert.equal(guideWindow(NOW, 12, { before: H, after: 4 * H }).to, NOW + 4 * H, 'capped by the kept window');
assert.equal(guideWindow(NOW, 6, { before: 0, after: H }).from, Date.UTC(2026, 9, 26, 20, 0) / 1000, 'nothing kept before now');
w = guideWindow(NOW, 3, null);
assert.ok(w.to - w.from >= 3 * H && w.to - w.from <= 5 * H, 'defaults when the server did not say');
assert.ok(guideWindow(NOW, 6, { before: H, after: 1 }).to > guideWindow(NOW, 6, { before: H, after: 1 }).from, 'never empty');

// Ticks every half hour inside the window.
assert.deepEqual(guideTicks(1000, 1000 + 2 * H, HALF), [1800, 3600, 5400, 7200]);
assert.deepEqual(guideTicks(1800, 1800 + H, HALF), [1800, 3600]);

// Blocks: pixels from the window start, clipped at both ends, past / now / future, progress for now.
const from = NOW - H, to = NOW + 2 * H, ppm = GUIDE.PPM;
const list = [
  { title: 'early', start: from - H, end: from - 10 * M },               // before the window: dropped
  { title: 'clipped', start: from - 30 * M, end: from + 30 * M },        // starts before: clipped to 0
  { title: 'past', start: from + 30 * M, end: NOW - 2 * M },
  { title: 'now', start: NOW - 2 * M, end: NOW + 28 * M },
  { title: 'later', start: NOW + 28 * M, end: to + H },                  // runs past the end: clipped
  { title: 'broken', start: NOW + 3 * H, end: NOW + 2 * H },             // end before start: dropped
  null,
];
const blocks = guideBlocks(list, from, to, NOW, ppm);
assert.deepEqual(blocks.map((b) => [b.p.title, b.state]), [['clipped', 'past'], ['past', 'past'], ['now', 'now'], ['later', 'future']]);
assert.equal(blocks[0].left, 0);
assert.equal(blocks[0].width, 30 * ppm);
assert.equal(blocks[1].left, 30 * ppm);
assert.equal(blocks[2].left, (H - 2 * M) / M * ppm);
assert.equal(blocks[2].pct, 2 / 30);
assert.equal(blocks[3].left + blocks[3].width, (to - from) / M * ppm, 'ends at the right edge');
assert.equal(blocks[0].pct + blocks[3].pct, 0, 'only the current one has progress');
// Overlapping programmes (a broken guide) do not draw over each other; unsorted input is sorted.
const overlap = guideBlocks([{ title: 'b', start: NOW + 10 * M, end: NOW + 40 * M }, { title: 'a', start: NOW, end: NOW + 20 * M }],
  NOW, NOW + H, NOW + 1, ppm);
assert.deepEqual(overlap.map((b) => b.p.title), ['a', 'b']);
assert.equal(overlap[1].left, 20 * ppm);
assert.equal(overlap[1].width, 20 * ppm);
assert.deepEqual(guideBlocks(null, from, to, NOW, ppm), []);
// A programme that starts exactly now is "now"; one that ended exactly now is "past".
assert.deepEqual(guideBlocks([{ title: 'x', start: NOW - H, end: NOW }, { title: 'y', start: NOW, end: NOW + H }], from, to, NOW, ppm)
  .map((b) => b.state), ['past', 'now']);

// Now and next from a guide slice or from the cached [now, next] pair (with nulls and duplicates).
const a = { title: 'A', start: NOW - 10 * M, end: NOW + 5 * M };
const b = { title: 'B', start: NOW + 5 * M, end: NOW + 35 * M };
const c = { title: 'C', start: NOW + 35 * M, end: NOW + 65 * M };
assert.deepEqual(nowNext([c, a, b, a, null], NOW), { now: a, next: b });
assert.deepEqual(nowNext([a, b], NOW + 6 * M), { now: b, next: null }, 'the cached pair rolls over at the boundary');
assert.deepEqual(nowNext([b, c], NOW), { now: null, next: b }, 'a gap: nothing now, next is coming');
assert.deepEqual(nowNext([], NOW), { now: null, next: null });

// Stale cache: a "now" that has ended, or a "next" that has started, asks again before the minute is up.
assert.equal(epgStale({ x: { now: a, next: b } }, ['x'], NOW), false);
assert.equal(epgStale({ x: { now: a, next: b } }, ['x'], a.end), true);
assert.equal(epgStale({ x: { now: null, next: b } }, ['x'], b.start), true);
assert.equal(epgStale({ x: { now: a, next: b } }, ['y'], a.end), false, 'only the channels asked about');
assert.equal(epgStale({}, ['x'], NOW), false);

// Progress, the tile line, and the pinned title in a block cut by the left edge.
assert.equal(progressOf(a, NOW), 10 / 15);
assert.equal(progressOf(a, NOW + H), 1);
assert.equal(progressOf(a, NOW - H), 0);
assert.equal(progressOf(null, NOW), 0);
const fmt = (t) => new Date(t * 1000).toISOString().slice(11, 16);
assert.equal(epgText({ title: '运动一起赢', start: NOW - 12 * M, end: NOW + 48 * M }, fmt), '正在播：运动一起赢 · 20:00–21:00');
assert.equal(epgText(null, fmt), '');
assert.equal(pinPad(0, 300, 0), 12, 'not scrolled: normal padding');
assert.equal(pinPad(0, 300, 100), 112, 'scrolled into the block: the title follows');
assert.equal(pinPad(0, 300, 290), 240, 'but always leaves room for the title');
assert.equal(pinPad(500, 300, 100), 12, 'a block to the right is untouched');
assert.equal(pinPad(0, 40, 100), 12, 'narrow blocks keep the normal padding');

console.log('console guide tests passed');
