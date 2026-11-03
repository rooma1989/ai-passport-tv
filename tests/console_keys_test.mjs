// Tests for the PC player's keyboard shortcuts in server/relay/assets/console.js (the block between
// "播放器快捷键的纯函数" and "播放器快捷键的纯函数到此"; the page is a plain script, so the block is cut out and
// evaluated on its own). Also checks that the page wires the mapping up and that the help table, the
// player's aria-label and the button titles name the same keys.
// Run by tests/test_relay_watch.py under Node (18+); also runnable on its own:
//   node tests/console_keys_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const html = readFileSync(join(ROOT, 'server/relay/assets/console.html'), 'utf8');
const begin = source.indexOf('// ---- 播放器快捷键的纯函数');
const end = source.indexOf('// ---- 播放器快捷键的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { pcKeyAction, pcKeyRepeats, pcVolStep, PC_KEY_HELP } = new Function(
  source.slice(begin, end) + '\nreturn { pcKeyAction, pcKeyRepeats, pcVolStep, PC_KEY_HELP };')();

const base = { shift: false, mod: false, target: 'page', watching: true, full: false, big: false, seekable: true, vod: true };
const act = (key, extra) => pcKeyAction(Object.assign({}, base, { key }, extra || {}));

// ---- nothing plays: no key belongs to the player ----
for (const key of [' ', 'k', 'ArrowLeft', 'm', 'f', 'Escape']) assert.equal(act(key, { watching: false }), null, key);

// ---- typing: never the player's, not even Space or Esc (Esc leaves the field, the page does that) ----
for (const key of [' ', 'k', 'j', 'l', 'm', 'f', 'p', 'c', 'n', 'N', '<', '>', '5', '?', 'ArrowLeft', 'ArrowUp', 'Escape'])
  assert.equal(act(key, { target: 'input', full: true }), null, 'typing ' + key);

// ---- Ctrl / Alt / Cmd: the browser's and the page's shortcuts ----
for (const key of ['f', 'k', 'n', 'p', ' ', 'ArrowLeft']) assert.equal(act(key, { mod: true }), null, 'mod ' + key);

// ---- Space and K play / pause wherever the focus is (a page button, the player's own buttons, fullscreen) ----
for (const target of ['page', 'button', 'player', 'slider']) {
  assert.equal(act(' ', { target }), 'play', 'space on ' + target);
  assert.equal(act('k', { target }), 'play');
  assert.equal(act('K', { target, shift: true }), 'play', 'caps');
}
assert.equal(act(' ', { target: 'button', full: true }), 'play', 'fullscreen, focus on the Fullscreen button');
// In the episode list or a menu, Space and Enter choose the item.
assert.equal(act(' ', { target: 'menu' }), null);
assert.equal(act('Enter', { target: 'menu' }), null);
// Enter always presses the focused button.
for (const target of ['page', 'button', 'player']) assert.equal(act('Enter', { target, full: true }), null);

// ---- letters reach the player from anywhere ----
assert.equal(act('j'), 'back10');
assert.equal(act('l'), 'fwd10');
assert.equal(act('J', { shift: true }), 'back10');
assert.equal(act('m'), 'mute');
assert.equal(act('f'), 'full');
assert.equal(act('p'), 'pip');
assert.equal(act('c'), 'subs');
assert.equal(act('n'), 'next');
assert.equal(act('N', { shift: true }), 'prev');
assert.equal(act('?', { shift: true }), 'help');
assert.equal(act('m', { target: 'menu' }), 'mute', 'letters work in the episode list too');
assert.equal(act('x'), null);
assert.equal(act('Tab'), null, 'Tab keeps moving the focus');
assert.equal(act('/'), null, '/ is the page\'s search');

// ---- seeking only where the video can seek (live channels cannot) ----
assert.equal(act('j', { seekable: false }), null);
assert.equal(act('l', { seekable: false }), null);
assert.equal(act('0'), 'pct0');
assert.equal(act('5'), 'pct5');
assert.equal(act('9'), 'pct9');
assert.equal(act('5', { seekable: false }), null);

// ---- speed only for on-demand video ----
assert.equal(act('>', { shift: true }), 'rateUp');
assert.equal(act('<', { shift: true }), 'rateDown');
assert.equal(act('>', { shift: true, vod: false }), null);

// ---- arrows follow the focus ----
// On a page card (not enlarged, not fullscreen) the arrows move the page focus.
for (const key of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) assert.equal(act(key), null, 'page ' + key);
// On the picture, the seek bar or a player button: seek 5 s and volume.
for (const target of ['player', 'button']) {
  assert.equal(act('ArrowLeft', { target }), 'back5');
  assert.equal(act('ArrowRight', { target }), 'fwd5');
  assert.equal(act('ArrowUp', { target }), 'volUp');
  assert.equal(act('ArrowDown', { target }), 'volDown');
}
// Enlarged or fullscreen: the arrows are the player's wherever the focus is.
assert.equal(act('ArrowLeft', { big: true }), 'back5');
assert.equal(act('ArrowUp', { full: true }), 'volUp');
// The volume slider and the lists keep their own arrows.
for (const key of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) {
  assert.equal(act(key, { target: 'slider', full: true }), null, 'slider ' + key);
  assert.equal(act(key, { target: 'menu', big: true }), null, 'menu ' + key);
}
// Live: left / right do not seek; up / down still change the volume.
assert.equal(act('ArrowLeft', { target: 'player', seekable: false }), null);
assert.equal(act('ArrowDown', { target: 'player', seekable: false }), 'volDown');

// ---- Esc: the caller closes a menu / list / up-next, otherwise leaves it to fullscreen and the page ----
assert.equal(act('Escape'), 'esc');
assert.equal(act('Escape', { target: 'menu' }), 'esc');

// ---- holding a key: seeking and volume repeat, toggles fire once ----
for (const a of ['back5', 'fwd5', 'back10', 'fwd10', 'volUp', 'volDown']) assert.equal(pcKeyRepeats(a), true, a);
for (const a of ['play', 'mute', 'full', 'pip', 'subs', 'next', 'prev', 'help', 'rateUp', 'pct3']) assert.equal(pcKeyRepeats(a), false, a);

// ---- volume steps of 10 %, clamped, no float drift ----
assert.equal(pcVolStep(0.5, 0.1), 0.6);
assert.equal(pcVolStep(0.7, 0.1), 0.8, '0.7 + 0.1 is 0.8, not 0.7999');
assert.equal(pcVolStep(0.95, 0.1), 1);
assert.equal(pcVolStep(1, 0.1), 1);
assert.equal(pcVolStep(0.05, -0.1), 0);
assert.equal(pcVolStep(0, -0.1), 0);
assert.equal(pcVolStep(0.33, -0.1), 0.2);

// ---- the help table lists every key the mapping knows ----
const help = PC_KEY_HELP.map(([k]) => k).join(' ');
for (const k of ['空格', 'K', '←', '→', 'J', 'L', '↑', '↓', 'M', 'F', 'Esc', 'P', 'C', 'N', 'Shift+N', '<', '>', '0–9', '?']) assert.ok(help.includes(k), 'help names ' + k);
assert.ok(PC_KEY_HELP.every(([k, what]) => k && what && !/[A-Za-z]{3,}/.test(what.replace(/Shift/g, ''))), 'help text is Chinese');

// ---- the page wires it up ----
assert.match(source, /pcKeyAction\(\{ key: event\.key, shift: event\.shiftKey,/, 'the keydown handler asks pcKeyAction');
assert.match(source, /addEventListener\("keydown", \(event\) => \{\n  if \(!watching \|\| \$\("pcwin"\)\.hidden/, 'one capture-phase handler for the player');
assert.match(source, /addEventListener\("keyup", \(event\) => \{\n  if \(event\.key !== " " \|\| !spaceEaten\) return;/, 'the Space keyup does not press the focused button');
assert.ok(!/if \(key === " " && onButton && !full\) return;/.test(source), 'Space is no longer left to a focused button');
// The help overlay, its button and the volume indication exist in the page.
for (const id of ['pcKeys', 'pcKeysList', 'pcKeysX', 'pcKeysBtn', 'pcOsd']) assert.ok(html.includes(`id="${id}"`), id);
// Titles and the picture's label name the new keys.
assert.ok(html.includes('title="后退 10 秒（J）"') && html.includes('title="前进 10 秒（L）"'));
assert.ok(html.includes('title="字幕（C）"'));
assert.ok(/aria-label="画面：空格或 K 暂停，左右键快退快进 5 秒/.test(html));

console.log('console keys: ok');
