// Tests for the PC player's "the relay lost this stream" decisions in server/relay/assets/console.js
// (the block between "片源断了自动重连的几个纯函数" and "片源断了自动重连的纯函数到此"; the page is a plain
// script, so the block is cut out and evaluated on its own). Also checks that the page wires those decisions up:
// the players hand a lost stream to sourceGone instead of printing "这一路已经停了" over a picture that keeps
// playing, and the reconnect asks for the same episode from the current position.
// Run by tests/test_relay_watch.py under Node (18+); also runnable on its own:
//   node tests/console_reconnect_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const html = readFileSync(join(ROOT, 'server/relay/assets/console.html'), 'utf8');
const begin = source.indexOf('// ---- 片源断了自动重连的几个纯函数');
const end = source.indexOf('// ---- 片源断了自动重连的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { RECONNECT, reconnectWait, reconnectStep, lostState } = new Function(
  source.slice(begin, end) + '\nreturn { RECONNECT, reconnectWait, reconnectStep, lostState };')();

// ---- retries: a video gets TRIES attempts with growing waits, the first at once; a live channel one ----
assert.equal(reconnectWait(0, false, 0), 0, 'the first attempt is immediate');
assert.ok(reconnectWait(1, false, 0) > 0 && reconnectWait(2, false, 0) > reconnectWait(1, false, 0), 'backoff grows');
assert.equal(reconnectWait(RECONNECT.TRIES, false, 0), -1, 'bounded once the cache is out');
assert.equal(reconnectWait(RECONNECT.TRIES, false, RECONNECT.SWITCH_S), -1);
assert.equal(RECONNECT.TRIES, 3);
// A relay restart takes longer than three quick tries, and the cache can hold a minute: while it plays keep
// trying every last-wait, so a relay that comes back within the cache is picked up without a click.
const last = RECONNECT.WAIT_MS[RECONNECT.WAIT_MS.length - 1];
assert.equal(reconnectWait(RECONNECT.TRIES, false, 40), last);
assert.equal(reconnectWait(99, false, 40), last, 'bounded by the cache, not by a count');
assert.equal(reconnectWait(99, false, 0), -1);
assert.equal(reconnectWait(0, true, 0), 0, 'live: reopen once');
assert.equal(reconnectWait(1, true, 0), -1, 'live: then the message');
assert.equal(reconnectWait(1, true, 9), -1, 'live: a live cache does not buy more tries');

// ---- waiting for the new stream before switching: keep the old buffer playing until it is ready ----
const R = RECONNECT.READY_S;
assert.equal(reconnectStep(200, R, false, 30, 0), 'switch', 'READY_S seconds listed: switch, the picture comes at once');
assert.equal(reconnectStep(200, 2, true, 30, 0), 'switch', 'the whole (short) rest is out: switch');
assert.equal(reconnectStep(200, 2, false, 30, 1000), 'wait', 'one segment so far, plenty cached: keep playing the cache');
assert.equal(reconnectStep(503, 0, false, 30, 1000), 'wait', 'still starting: wait');
assert.equal(reconnectStep(0, 0, false, 30, 1000), 'wait', 'relay unreachable for a moment: wait');
assert.equal(reconnectStep(503, 0, false, 1, 1000), 'switch', 'cache nearly gone: switch, the new player waits on its own');
assert.equal(reconnectStep(200, 2, false, 1, 1000), 'switch');
assert.equal(reconnectStep(503, 0, false, 30, RECONNECT.READY_MS), 'switch', 'waited long enough');
assert.equal(reconnectStep(404, 0, false, 30, 0), 'fail', 'the new stream is gone too: this attempt failed');
assert.equal(reconnectStep(410, 0, false, 1, 0), 'fail');

// ---- what the picture says ----
const base = { gone: true, failed: false, ahead: 0, userPaused: false, deferred: false };
const state = (x) => lostState(Object.assign({}, base, x));
assert.deepEqual(lostState({ gone: false, ahead: 0 }), { kind: '', text: '' }, 'source fine: not ours (buffering/hold says its own thing)');
assert.deepEqual(state({ ahead: 40 }), { kind: 'cached', text: '当前线路暂时不可用，正在播放已缓存内容' }, 'FR-1: buffer still playing');
assert.equal(state({ ahead: 40, failed: true }).kind, 'cached', 'retries used up but still playing the cache: still the small notice');
assert.deepEqual(state({ ahead: 0.2 }), { kind: 'reconnecting', text: '正在重新连接…' }, 'cache ran out while reconnecting: not alarming');
assert.deepEqual(state({ ahead: 0, failed: true }), { kind: 'interrupted', text: '播放已中断' }, 'FR-3: cache out and reconnect failed');
{
  const s = state({ ahead: 23.7, userPaused: true });
  assert.equal(s.kind, 'paused', 'FR-2: user paused with cache left');
  assert.ok(s.text.includes('23 秒') && s.text.includes('缓存'), s.text);
}
assert.equal(state({ ahead: 23.7, deferred: true }).kind, 'paused');
assert.equal(state({ ahead: 0, userPaused: true }).kind, 'paused', 'paused at the end of the cache: still the user\'s pause');
assert.ok(!state({ ahead: 0, userPaused: true }).text.includes('秒'), 'nothing cached: no seconds');
for (const x of [{ ahead: 40 }, { ahead: 0 }, { ahead: 0, failed: true }, { ahead: 9, userPaused: true }]) {
  assert.ok(!state(x).text.includes('已经停了'), 'never "这一路已经停了"');
}

// ---- the page uses them ----
const body = (name) => {
  const i = source.indexOf(name);
  assert.ok(i > 0, name + ' exists');
  const j = source.indexOf('\n}\n', i);
  return source.slice(i, j);
};
// The HLS player hands a lost stream (404 on playlist / init, 410) to the page instead of printing it.
const start = body('  async start() {\n    const v = this.video;\n    if (!window.MediaSource)');
assert.match(start, /error\.gone[^\n]*this\.lose\(/, 'WatchPlayer.start routes gone to lose()');
assert.match(source, /response\.status === 404\) throw Object\.assign\(new Error\("这一路已经停了"\), \{ gone: true \}\)/);
assert.match(source, /reopen: !!response\.headers\.get\("X-RTV-Reopen"\), gone: true/);
assert.match(body('  gate() {'), /this\.gone\) return;/, 'no buffering hold once the stream is gone');
// At the end of the cache (the position can run a little past the buffered intersection of the two tracks) the
// player waits; it never jumps back to the start of the buffer and replays what was already watched.
assert.match(body('  kick() {\n    if (!this.alive || !this.sb) return;'), /else if \(!this\.started\) v\.currentTime = b\.start\(0\)/);
// Every player is wired to sourceGone; the old vodBroken (one retry, then "片源断了") is gone.
assert.equal((source.match(/gone: \(why\) => sourceGone\(player, why\)/g) || []).length, 2, 'HLS and DASH');
assert.match(source, /broken: \(\) => sourceGone\(player, ""\)/, 'VOD');
assert.ok(!source.includes('vodBroken'), 'vodBroken replaced');
assert.match(body('  fail(text, gone) {'), /if \(gone && this\.opts\.gone\) \{ this\.gone = true; this\.opts\.gone\(text\); \}/, 'DASH: gone is not a format fallback');
// The reconnect: same episode (watching.again), same way of playing (watching.hls), from pcPos().
const attempt = body('async function lostAttempt(gen) {');
assert.match(attempt, /start: Math\.round\(pcPos\(\) \* 10\) \/ 10/);
assert.match(attempt, /w\.again/);
assert.match(attempt, /w\.hls/);
assert.match(attempt, /watchKey\(data\.now\) !== w\.key/, 'following the TV: never reconnect into another episode');
assert.match(body('function lostSwitch(w, answer) {'), /openDirect\(answer, \{[^}]*again: true[^}]*reconnect: true/);
// Retry / 重连 restart from the current position.
const retry = body('function retryWatch() {');
assert.match(retry, /lost\.gone\) \{ lostManual\(\); return; \}/);
assert.match(retry, /watching\.seekable && watching\.player\) \{ seekRestart\(pcPos\(\)\); return; \}/);
// Stale messages go away when the picture moves on.
assert.match(body('function lostTick(event) {'), /badAt != null/);
assert.ok(html.includes('id="pcLost"') && html.includes('id="pcLostText"') && html.includes('id="pcLostGo"'), 'the notice is in the page');

console.log('console reconnect tests: ok');
