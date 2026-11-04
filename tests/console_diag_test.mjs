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


// ---- the stuck-decoder watchdog (WatchPlayer.unstick / unstuck, cut out of the class) ----
{
  const stuckConst = source.match(/const STUCK = \{[\s\S]*?\};/);
  assert.ok(stuckConst, 'STUCK is where it was');
  const from = source.indexOf('  unstick() {'), to = source.indexOf('  async start() {', from);
  assert.ok(from > 0 && to > from, 'unstick and unstuck sit right before start()');
  let clock = 0, big = true;
  const notes = [];
  const make = new Function('performance', 'pcBig', 'diagNote', 'setTimeout',
    stuckConst[0] + '\nreturn { STUCK, Player: class {\n' + source.slice(from, to) + '\n} };');
  const timers = [];
  const { STUCK, Player } = make({ now: () => clock }, () => big, (kind, data) => notes.push({ kind, ...data }),
                                 (fn) => timers.push(fn));
  const video = { currentTime: 100, paused: false, seeking: false, readyState: 2, style: {},
                  buffered: { length: 1, start: () => 90, end: () => 130 } };
  const p = Object.assign(new Player(), { video, alive: true, started: true, hold: 0, sb: {}, stuck: null });
  const tick = (ms = STUCK.TICK_MS) => { clock += ms; p.unstick(); };
  assert.deepEqual(STUCK.LADDER, ['nudge', 'relayout', 'jump'], 'nudge, then re-layout like a manual resize, then jump');

  // playing normally: never acts
  for (let i = 0; i < 10; i++) { video.currentTime += 0.5; tick(); }
  assert.equal(notes.length, 0, 'moving playback is left alone');

  // frozen with data ahead: nudge first
  for (let i = 0; i < 4; i++) tick();
  assert.equal(notes.length, 1);
  assert.equal(notes[0].how, 'nudge');
  assert.equal(notes[0].big, 1);
  assert.ok(Math.abs(video.currentTime - (105 + STUCK.NUDGE_S)) < 1e-9, 'pushed by NUDGE_S');
  // the push itself is not "moving again"; still frozen (and now stuck seeking, readyState 4): re-layout next
  video.seeking = true; video.readyState = 4;
  for (let i = 0; i < 4; i++) tick();
  assert.equal(notes.length, 2, 'seeking forever or readyState 4 still counts as stuck');
  assert.equal(notes[1].how, 'relayout');
  assert.equal(notes[1].seeking, 1);
  assert.equal(video.style.width, 'calc(100% - 1px)', 'the video is re-laid out');
  timers.splice(0).forEach((fn) => fn());
  assert.equal(video.style.width, '', 'and put back');
  for (let i = 0; i < 4; i++) tick();
  assert.equal(notes[2].how, 'jump');
  assert.ok(Math.abs(video.currentTime - (105.1 + STUCK.JUMP_S)) < 1e-9, 'jumped by JUMP_S');
  for (let i = 0; i < 4; i++) tick();
  assert.equal(notes[3].how, 'jump', 'keeps jumping after the ladder runs out');

  // the user shrinks the player, it plays again: one unstuck note saying so
  big = false; video.seeking = false;
  video.currentTime += 0.5; tick();
  assert.equal(notes.length, 5);
  assert.equal(notes[4].kind, 'unstuck');
  assert.equal(notes[4].n, 4);
  assert.equal(notes[4].moved, 1);
  assert.equal(notes[4].resized, 1);
  video.currentTime += 0.5; tick();
  assert.equal(notes.length, 5, 'only once');

  // not enough data ahead: not the watchdog's business (the gate's)
  video.buffered = { length: 1, start: () => 90, end: () => video.currentTime + 0.5 };
  for (let i = 0; i < 10; i++) tick();
  assert.equal(notes.length, 5);
  // a user pause ends the episode without acting
  video.buffered = { length: 1, start: () => 90, end: () => 200 };
  for (let i = 0; i < 4; i++) tick();
  assert.equal(notes.length, 6);
  video.paused = true;
  for (let i = 0; i < 10; i++) tick();
  assert.equal(notes.length, 7);
  assert.equal(notes[6].kind, 'unstuck');
  assert.equal(notes[6].moved, 0, 'paused, not recovered');
}

console.log('console_diag_test: ok');
