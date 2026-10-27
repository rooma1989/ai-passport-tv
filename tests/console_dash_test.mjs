// Tests for the PC player's DASH segment math in server/relay/assets/console.js
// (the block between "DASH 的几个纯函数" and "DASH 的纯函数到此"; the page is a plain
// script, so the block is cut out and evaluated on its own).
// Run by tests/test_relay_ytvod.py under Node (18+); also runnable on its own:
//   node tests/console_dash_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- DASH 的几个纯函数');
const end = source.indexOf('// ---- DASH 的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { DASH, dashIndex, dashRange, dashCovering, dashResume, dashWant, dashEvict } = new Function(
  source.slice(begin, end) + '\nreturn { DASH, dashIndex, dashRange, dashCovering, dashResume, dashWant, dashEvict };')();

// Five-second segments starting at 0.067 s (an H.264 file with B-frames), the last one shorter.
const t = [0.067, 5.067, 10.067, 15.067, 20.067, 22.5];
const o = [1000, 3000, 5500, 8000, 10000, 11000];

// Which segment holds a time: binary search, clamped at both ends.
assert.equal(dashIndex(t, 0), 0, 'before the first boundary is the first segment');
assert.equal(dashIndex(t, 0.067), 0);
assert.equal(dashIndex(t, 5.066), 0);
assert.equal(dashIndex(t, 5.067), 1, 'a boundary starts the next segment');
assert.equal(dashIndex(t, 21), 4);
assert.equal(dashIndex(t, 99), 4, 'past the end is the last segment');
assert.equal(dashIndex([0], 3), -1, 'no segments');
const many = Array.from({ length: 1441 }, (_, i) => i * 5);
for (const x of [0, 4.99, 5, 3599.5, 7199.9, 7200]) assert.equal(dashIndex(many, x), Math.min(1439, Math.floor(x / 5)));

// Byte ranges are inclusive and adjacent.
assert.equal(dashRange(o, 0), 'bytes=1000-2999');
assert.equal(dashRange(o, 4), 'bytes=10000-10999');

// Buffered ranges: a small gap before the range still counts as inside it.
assert.deepEqual(dashCovering([[0, 10], [30, 40]], 5), [0, 10]);
assert.deepEqual(dashCovering([[0, 10], [30, 40]], 30 - DASH.NEAR / 2), [30, 40]);
assert.equal(dashCovering([[0, 10], [30, 40]], 20), null);

// After a seek: inside the buffer, carry on after its end; outside, start at the segment holding the target.
assert.equal(dashResume(t, [[0.067, 10.06]], 3), 2, 'buffered to the end of segment 1: fetch segment 2 next');
assert.equal(dashResume(t, [[0.067, 10.1]], 3), 2, 'audio frames overrunning a boundary a little');
assert.equal(dashResume(t, [[0.067, 10.06]], 16), 3, 'beyond the buffer');
assert.equal(dashResume(t, [], 0), 0);
assert.equal(dashResume(t, [[0.067, 22.5]], 21), 5, 'everything buffered: nothing more to fetch');

// Fetch while the next segment starts within AHEAD seconds of the playhead, and not past the last one.
assert.equal(dashWant(t, 0, 0), true);
assert.equal(dashWant(many, DASH.AHEAD / 5 - 1, 0), true, 'a segment starting inside the window');
assert.equal(dashWant(many, DASH.AHEAD / 5, 0), false, 'one starting AHEAD seconds out waits');
assert.equal(dashWant(many, DASH.AHEAD / 5, 1), true, 'until the playhead moves on');
assert.equal(dashWant(t, 5, 0), false, 'all fetched');
assert.equal(dashWant(t, -1, 0), false);

// Eviction keeps BEHIND seconds behind the playhead and drops what was left far ahead after seeking back.
assert.deepEqual(dashEvict([[0, 100]], 80), [[0, 80 - DASH.BEHIND]]);
assert.deepEqual(dashEvict([[0, 40]], 20), [], 'nothing old enough');
assert.deepEqual(dashEvict([[0, 25], [500, 560]], 10), [[500, 560]], 'a far-ahead range after a seek back');
assert.deepEqual(dashEvict([[50, 50.3]], 100), [], 'slivers are left alone');

console.log('console dash logic: ok');
