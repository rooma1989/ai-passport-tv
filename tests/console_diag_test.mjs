// Tests for the PC player's playback diagnostics in server/relay/assets/console.js (the block between
// "播放诊断的纯函数（" and "播放诊断的纯函数到此"; the page is a plain script, so the block is cut out and evaluated
// on its own). Run by tests/test_relay_watch.py under Node (18+); also runnable on its own:
//   node tests/console_diag_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 播放诊断的纯函数（');
const end = source.indexOf('// ---- 播放诊断的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const constLine = source.match(/const DIAG = \{[^\n]*\};/);
assert.ok(constLine, 'DIAG is where it was');
const { DIAG, diagJump, diagDropped } = new Function(
  constLine[0] + '\n' + source.slice(begin, end) + '\nreturn { DIAG, diagJump, diagDropped };')();

// ---- a jump: the position moved further than the wall clock allows ----
assert.equal(diagJump(100, 101, 1, 1), 0, 'normal playback');
assert.equal(diagJump(100, 101.3, 1, 1), 0, 'timer jitter is not a jump');
assert.equal(diagJump(100, 102.2, 1, 1), 1.2, 'skipped 1.2 s');
assert.equal(diagJump(100, 102, 1, 2), 0, 'double speed covers 2 s a second');
assert.equal(diagJump(100, 100, 1, 1), 0, 'stalled is not a jump');
assert.equal(diagJump(100, 99, 1, 1), 0, 'backwards (a seek back) is not a skip');
assert.ok(DIAG.JUMP_S >= 0.3, 'tolerant enough for a 1 s tick');

// ---- dropped frames over a window ----
assert.equal(diagDropped(null, { dropped: 9, total: 240 }), null, 'nothing to compare yet');
assert.equal(diagDropped({ dropped: 2, total: 100 }, { dropped: 4, total: 340 }), null, 'a couple dropped: fine');
assert.deepEqual(diagDropped({ dropped: 2, total: 100 }, { dropped: 14, total: 340 }), { n: 12, of: 240 });
assert.equal(diagDropped({ dropped: 20, total: 900 }, { dropped: 0, total: 10 }), null, 'counters reset (new stream)');

console.log('console_diag_test: ok');
