// Tests for the PC player's live-start logic in server/relay/assets/console.js
// (the block between "直播起播的几个纯函数" and "直播起播的纯函数到此"; the page is a
// plain script, so the block is cut out and evaluated on its own).
// Run by tests/test_relay_fastswitch.py under Node (18+); also runnable on its own:
//   node tests/console_live_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 直播起播的几个纯函数');
const end = source.indexOf('// ---- 直播起播的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { LIVE, liveStartSeq, liveRate, livePoll, liveNeighbours } = new Function(
  source.slice(begin, end) + '\nreturn { LIVE, liveStartSeq, liveRate, livePoll, liveNeighbours };')();

// Where the first playlist read starts: a fresh stream from its first segment, a warm or shared
// one LIVE.BACK segments behind the newest, never before the window.
assert.equal(liveStartSeq(0, 1), 0);
assert.equal(liveStartSeq(0, 3), 0);
assert.equal(liveStartSeq(40, 12), 46);
assert.equal(liveStartSeq(40, 12) + LIVE.BACK, 40 + 12, 'about 12 s of 2-second segments');

// Playback rate: slow below LIVE.LOW seconds ahead, normal from LIVE.OK, unchanged between (no flapping).
assert.equal(liveRate(2, 1), LIVE.SLOW);
assert.ok(LIVE.SLOW >= 0.9 && LIVE.SLOW < 1, 'slow, but not noticeably');
assert.equal(liveRate(8, LIVE.SLOW), LIVE.SLOW, 'keeps slowing until enough is buffered');
assert.equal(liveRate(8, 1), 1, 'and does not slow again until it runs low');
assert.equal(liveRate(LIVE.OK, LIVE.SLOW), 1);
assert.equal(liveRate(30, 1), 1);

// Playlist polling: every half second while starting and short of buffer, then every second.
assert.equal(livePoll(2, 1000), 500);
assert.equal(livePoll(LIVE.OK, 1000), 1000);
assert.equal(livePoll(2, LIVE.STARTUP_MS + 1), 1000, 'a source that never catches up is not polled fast forever');

// Neighbours within a group wrap around and skip creator, series and cast channels.
assert.deepEqual(liveNeighbours(['ch000', 'ch001', 'ch002'], 'ch001'), { prev: 'ch000', next: 'ch002' });
assert.deepEqual(liveNeighbours(['ch000', 'ch001', 'ch002'], 'ch000'), { prev: 'ch002', next: 'ch001' });
assert.deepEqual(liveNeighbours(['cast', 'up01', 'ch000', 'sr02', 'ch005'], 'ch005'), { prev: 'ch000', next: 'ch000' });
assert.deepEqual(liveNeighbours(['ch000'], 'ch000'), { prev: null, next: null });
assert.deepEqual(liveNeighbours(['ch000', 'ch001'], 'up01'), { prev: null, next: null });

console.log('console live-start tests passed');
