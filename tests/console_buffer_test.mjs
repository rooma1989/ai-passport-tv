// Tests for the PC player's video buffering and "really ended" logic in server/relay/assets/console.js
// (the block between "视频缓冲的几个纯函数" and "视频缓冲的纯函数到此"; the page is a plain script, so the block
// is cut out and evaluated on its own).
// Run by tests/test_relay_watch.py under Node (18+); also runnable on its own:
//   node tests/console_buffer_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 视频缓冲的几个纯函数');
const end = source.indexOf('// ---- 视频缓冲的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { VOD_GATE, vodTarget, vodSpeed, vodHold, vodBufferText, mediaDone } = new Function(
  source.slice(begin, end) + '\nreturn { VOD_GATE, vodTarget, vodSpeed, vodHold, vodBufferText, mediaDone };')();

// The VOD buffer cap in console.js lets the player fetch at least as far as it waits for.
const cap = source.match(/const VOD_BUFFER = \{ AHEAD_S: (\d+), BEHIND_S: \d+, MIN_AHEAD_S: (\d+)/);
assert.ok(cap, 'VOD_BUFFER is where it was');
assert.ok(Number(cap[1]) - 1 >= VOD_GATE.START_S && Number(cap[1]) - 1 >= VOD_GATE.RESUME_S, 'the time cap is above both targets');
assert.ok(VOD_GATE.START_S >= VOD_GATE.RESUME_S && VOD_GATE.RESUME_S > VOD_GATE.LOW_S * 4, 'hysteresis, not flapping');

// ---- "really ended": the media element played to its end, not "every segment was appended" ----
assert.equal(mediaDone(true, true, 1500, 1500), true);
assert.equal(mediaDone(true, false, 1499.8, 1500), true, 'within END_NEAR_S of the end');
assert.equal(mediaDone(true, false, 1440, 1500), false, 'all data fed with a minute left: space resumes, no restart');
assert.equal(mediaDone(false, true, 1500, 1500), false, 'the player has not said it ended (a fresh player)');
assert.equal(mediaDone(true, false, 10, Infinity), false, 'no duration yet');
assert.equal(mediaDone(true, false, 10, NaN), false);
assert.equal(mediaDone(undefined, false, 0, 0), false);

// ---- targets: capped by what is left of the file and by how far the player may fetch ----
assert.equal(vodTarget(12, Infinity, 60), 12);
assert.equal(vodTarget(12, 5, 60), 5, 'near the end only what is left');
assert.equal(vodTarget(12, Infinity, 8), 7, 'a byte cap below the target (very high bitrate) becomes the target');
assert.equal(vodTarget(12, -3, 60), 0);

// ---- throughput: media seconds fetched per wall second, the first entry only a starting point ----
assert.equal(vodSpeed([]), 0);
assert.equal(vodSpeed([{ t: 0, secs: 6 }]), 0, 'one entry: unknown');
assert.equal(vodSpeed([{ t: 0, secs: 6 }, { t: 1000, secs: 1 }]), 0, 'less than MEASURE_S measured: unknown');
assert.equal(vodSpeed([{ t: 0, secs: 6 }, { t: 2000, secs: 6 }]), 3, '6 s of video in 2 s');
assert.equal(vodSpeed([{ t: 0, secs: 0 }, { t: 3000, secs: 6 }, { t: 9000, secs: 6 }]), 4 / 3, '12 s of video in 9 s, counted from the start marker');
{
  // Only the newest WINDOW segments count: a slow start is forgotten once the drive speeds up.
  const feed = [{ t: 0, secs: 0 }];
  let t = 0;
  for (let i = 0; i < 10; i++) feed.push({ t: (t += 20000), secs: 6 });   // 0.3x
  for (let i = 0; i < VOD_GATE.WINDOW; i++) feed.push({ t: (t += 1000), secs: 6 }); // 6x
  assert.equal(vodSpeed(feed), 6);
}

const base = { started: false, holding: false, wantPlay: true, ended: false, ahead: 0, speed: 0, left: Infinity, cap: 60 };
const hold = (x) => vodHold(Object.assign({}, base, x));

// ---- start: fast networks start at once, slow ones wait for START_S seconds ----
assert.equal(hold({ ahead: 4, speed: 3 }), 0, 'clearly faster than the bitrate: no extra wait');
assert.equal(hold({ ahead: 4, speed: VOD_GATE.FAST }), 0);
assert.equal(hold({ ahead: 4, speed: 1.1 }), VOD_GATE.START_S, 'barely keeping up: pre-buffer');
assert.equal(hold({ ahead: 4, speed: 0 }), VOD_GATE.START_S, 'speed not known yet: pre-buffer');
assert.equal(hold({ ahead: 1, speed: 5 }), VOD_GATE.START_S, 'fast but almost nothing at the start position yet');
assert.equal(hold({ ahead: 12.4, speed: 0.4 }), 0, 'START_S reached');
assert.equal(hold({ ahead: 3, speed: 0.4, left: 3 }), 0, 'the rest of the file is buffered');
assert.equal(hold({ ahead: 6, speed: 0.4, cap: 8 }), 7, 'byte-capped: waits for the cap, not forever');
assert.equal(hold({ ahead: 7, speed: 0.4, cap: 8 }), 0);
assert.equal(hold({ ahead: 1, ended: true }), 0, 'everything fetched');
assert.equal(hold({ ahead: 1, wantPlay: false }), 0, 'user paused: not the buffer\'s business');

// ---- rebuffer: ran dry while playing, hold until RESUME_S, then resume ----
const playing = { started: true };
assert.equal(hold(Object.assign({ ahead: 3 }, playing)), 0, 'enough ahead: keep playing');
assert.equal(hold(Object.assign({ ahead: 0.2 }, playing)), VOD_GATE.RESUME_S, 'ran dry: hold');
assert.equal(hold(Object.assign({ ahead: 0.2, speed: 9 }, playing)), VOD_GATE.RESUME_S, 'no early resume on a fast sample');
assert.equal(hold(Object.assign({ ahead: 6, holding: true }, playing)), VOD_GATE.RESUME_S, 'still short: keep holding (hysteresis)');
assert.equal(hold(Object.assign({ ahead: 10, holding: true }, playing)), 0, 'RESUME_S reached: resume');
assert.equal(hold(Object.assign({ ahead: 0.2, ended: true }, playing)), 0, 'all fed: play to the end, never hold');
assert.equal(hold(Object.assign({ ahead: 0.2, holding: true, ended: true }, playing)), 0, 'the rest arrived while holding: resume');
assert.equal(hold(Object.assign({ ahead: 0.2, left: 0.3 }, playing)), 0, 'at the very end of the file: no hold');
assert.equal(hold(Object.assign({ ahead: 0.2, left: 4 }, playing)), 4, 'near the end: only what is left');
assert.equal(hold(Object.assign({ ahead: 3, left: 4, holding: true }, playing)), 4);
assert.equal(hold(Object.assign({ ahead: 4, left: 4, holding: true }, playing)), 0);
assert.equal(hold(Object.assign({ ahead: 0.2, wantPlay: false }, playing)), 0, 'user paused meanwhile: no auto resume');
assert.equal(hold(Object.assign({ ahead: 0.2, cap: 8 }, playing)), 7, 'byte cap below RESUME_S');

// ---- status text ----
assert.equal(vodBufferText(6.7, 12), '正在缓冲 6/12 秒');
assert.equal(vodBufferText(0, 10), '正在缓冲 0/10 秒');
assert.equal(vodBufferText(-1, 10), '正在缓冲 0/10 秒');
assert.equal(vodBufferText(13, 12), '正在缓冲 12/12 秒');
assert.equal(vodBufferText(1, 6.2), '正在缓冲 1/7 秒');

console.log('console buffer tests passed');
