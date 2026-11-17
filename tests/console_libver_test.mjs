// Tests for the library show versions in server/relay/assets/console.js: the detail page's version picker
// ("剧集版本的纯函数") and the player's next / previous episode ("片库剧集的上一集 / 下一集"). The page is a plain
// script, so each marked block is cut out and evaluated on its own.
// Run by tests/test_relay_libversions.py under Node (18+); also runnable on its own:
//   node tests/console_libver_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
function block(start, stop, names) {
  const begin = source.indexOf(start), end = source.indexOf(stop);
  assert.ok(begin > 0 && end > begin, 'the block is marked in console.js: ' + start);
  return new Function(source.slice(begin, end) + '\nreturn { ' + names.join(', ') + ' };')();
}
const { libVersIn, libVerChoose, libVerRows } = block('// ---- 剧集版本的纯函数', '// ---- 剧集版本的纯函数到此',
                                                      ['libVersIn', 'libVerChoose', 'libVerRows']);
const { libStep, libGap, libWhere } = block('// ---- 片库剧集的上一集 / 下一集', '// ---- 片库剧集的纯函数到此',
                                            ['libStep', 'libGap', 'libWhere']);
const { miniStepLabel } = block('// ---- 正在播放栏的上一集 / 下一集', '// ---- 正在播放栏的纯函数到此', ['miniStepLabel']);

// ---- detail page: versions ----
const card = (id, season, episode) => ({ id, season, episode });
const cards = new Map();
const A = [], B = [];
for (let n = 1; n <= 22; n++) {
  cards.set('a' + n, card('a' + n, 1, n)); A.push('a' + n);
  cards.set('b' + n, card('b' + n, 1, n)); B.push('b' + n);
}
const versions = [{ key: 'aA', label: 'A', ids: A, conflicts: [] }, { key: 'aB', label: 'B', ids: B, conflicts: [] }];
const vers = libVersIn(versions, cards, null);
assert.deepEqual(vers.map((v) => [v.key, v.count, v.cards.length]), [['aA', 22, 22], ['aB', 22, 22]]);
// choice: clicked > remembered > last watched > first; a remembered version that is gone is not silently replaced
assert.equal(libVerChoose(vers, 'aB', 'aA', 'aA').cur.key, 'aB');
assert.equal(libVerChoose(vers, '', 'aB', 'aA').cur.key, 'aB');
assert.equal(libVerChoose(vers, '', '', 'aB').cur.key, 'aB');
assert.equal(libVerChoose(vers, '', '', '').cur.key, 'aA');
assert.deepEqual(libVerChoose(vers, '', 'agone', 'aB'), { cur: null, gone: true });
assert.equal(libVerChoose(vers, 'aA', 'agone', '').cur.key, 'aA', 'picking again clears the notice');
// one row per episode; two files of one episode are one row with both; unknown numbers listed apart
const mixed = { cards: [card('x1', 1, 1), card('x1b', 1, 1), card('x2', 1, 2), card('xq', null, null)] };
const got = libVerRows(mixed);
assert.deepEqual(got.rows.map((r) => [r.card.id, r.all.map((c) => c.id)]), [['x1', ['x1', 'x1b']], ['x2', ['x2']]]);
assert.deepEqual(got.unknown.map((c) => c.id), ['xq']);
// seasons: a version only shows in a season it has
const s2 = libVersIn([{ key: 'k', label: 'K', ids: ['s1', 's2'] }],
                     new Map([['s1', card('s1', 1, 1)], ['s2', card('s2', 2, 1)]]), 2);
assert.deepEqual(s2.map((v) => [v.count, v.cards.map((c) => c.id)]), [[1, ['s2']]]);

// ---- player: next / previous by real episode number ----
const items = [{ n: 1, ep: 3, label: '第 3 集' }, { n: 2, ep: 5, label: '第 5 集' }, { n: 3, ep: 6, label: '第 6 集', playable: false },
               { n: 4, ep: 8, label: '第 8 集' }, { n: 5, ep: null, label: '集号未知' }];
assert.deepEqual(libStep(items, items[0], 1), { item: items[1], note: '第 4 集缺失，下一集为第 5 集' });
assert.deepEqual(libStep(items, items[1], 1), { item: items[3], note: '第 6–7 集缺失，下一集为第 8 集' }, 'unplayable skipped');
assert.equal(libStep(items, items[3], 1), null, 'the last episode has no next (never the unknown one)');
assert.equal(libStep(items, items[0], -1), null, 'the first has no previous');
assert.deepEqual(libStep(items, items[1], -1), { item: items[0], note: '第 4 集缺失，上一集为第 3 集' });
assert.equal(libStep(items, items[4], 1), null, 'an unknown number has no neighbours');
assert.equal(libGap(3, 4), '');
assert.equal(libWhere({ current: 1, count: 22, items: [{ n: 1, label: '第 3 集' }] }), '第 3 集 / 已收录 22 集');

// ---- bottom bar (正在播放栏): the same steps as the player, never a silent dead button ----
// a middle episode: both sides live, labelled with the real episode and the gap note
const mid = (dir) => { const f = libStep(items, items[1], dir); return miniStepLabel(dir, true, true, f && { title: f.item.label, note: f.note }); };
assert.deepEqual(mid(1), { label: '下一集：第 8 集（第 6–7 集缺失，下一集为第 8 集）', disabled: false });
assert.deepEqual(mid(-1), { label: '上一集：第 3 集（第 4 集缺失，上一集为第 3 集）', disabled: false });
// first / last / unknown number: greyed out and saying why (same rule as the player's toast)
assert.deepEqual(miniStepLabel(-1, true, true, null), { label: '已是当前版本的第一集', disabled: true });
assert.deepEqual(miniStepLabel(1, true, true, null), { label: '当前版本已播放完毕', disabled: true });
// a season-2 list only steps inside itself: the other season's episodes are not in `items`, so they are never reached
const s2items = [{ n: 1, ep: 1, label: '第 2 季第 1 集' }, { n: 2, ep: 2, label: '第 2 季第 2 集' }];
assert.equal(libStep(s2items, s2items[1], 1), null);
// not the library: other parts, UP channel, live channel keep their own wording and stay clickable
assert.deepEqual(miniStepLabel(1, false, true, { title: 'P2' }), { label: '下一集：P2', disabled: false });
assert.deepEqual(miniStepLabel(1, false, false, { title: '第 3 个' }), { label: '下一个：第 3 个', disabled: false });
assert.deepEqual(miniStepLabel(-1, false, false, null), { label: '上一台', disabled: false });

console.log('console_libver_test: ok');
