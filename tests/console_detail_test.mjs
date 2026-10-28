// Tests for the series / creator detail pages' helpers in server/relay/assets/console.js
// (the block between "资料页的几个纯函数" and "资料页的纯函数到此"; the page is a plain script, so the
// block is cut out and evaluated on its own with the few globals it uses stubbed).
// Run by tests/test_relay_detail.py under Node (18+); also runnable on its own:
//   node tests/console_detail_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 资料页的几个纯函数');
const end = source.indexOf('// ---- 资料页的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
// The page's own DETAIL_ID, pad and clock (times are shown with them); routeParts stands for the address bar.
const line = (re) => { const m = source.match(re); assert.ok(m, String(re)); return m[0] + '\n'; };
const prelude = line(/^const DETAIL_ID = .*$/m) + line(/^function pad\(n\) .*$/m) +
  line(/^function clock\(seconds\) \{[\s\S]*?\n\}/m) + 'const routeParts = () => ["home"];\n';
const { detailRoute, seriesLine, seriesKind, seriesPct, resumeLabel, markText, episodeWindow, applyMarks } = new Function(
  prelude + source.slice(begin, end) +
  '\nreturn { detailRoute, seriesLine, seriesKind, seriesPct, resumeLabel, markText, episodeWindow, applyMarks };')();

// Routes: #/series/<id>, #/up/<channel id or follow key>; anything else is a list or another page.
assert.deepEqual(detailRoute('series/sr01'), { kind: 'series', id: 'sr01' });
assert.deepEqual(detailRoute('up/bilibili:42'), { kind: 'up', id: 'bilibili:42' });
assert.deepEqual(detailRoute('up/youtube:UCabcdefghijklmnopqrstuv'), { kind: 'up', id: 'youtube:UCabcdefghijklmnopqrstuv' });
assert.equal(detailRoute('series'), null);
assert.equal(detailRoute('up/'), null);
assert.equal(detailRoute('lib/sr01'), null);
assert.equal(detailRoute('series/a/b'), null, 'one level only');
assert.equal(detailRoute('series/<script>'), null, 'ids are checked before they reach a request');
assert.equal(detailRoute(), null, 'no path: the current hash (home here)');

// Labels follow the merged progress: never started, half-way, finished.
const fresh = { total: 12, watched: 0, started: false, finished: false, episode: 1, position: 0, duration: 600 };
const going = { total: 12, watched: 3, started: true, finished: false, episode: 4, position: 300, duration: 600 };
const done = { total: 12, watched: 12, started: true, finished: true, episode: 1, position: 0, duration: 600 };
assert.equal(seriesLine(fresh), '共 12 集');
assert.equal(seriesLine(going), '看到第 4 集 · 共 12 集');
assert.equal(seriesLine(done), '看完了 · 共 12 集');
assert.deepEqual([fresh, going, done].map(seriesKind), ['fresh', 'going', 'done']);
assert.deepEqual([fresh, going, done].map(resumeLabel), ['播放第 1 集', '继续看第 4 集', '重看第 1 集']);
assert.equal(seriesPct(going), 3.5 / 12, 'finished episodes plus the part of the current one');
assert.equal(seriesPct(done), 1);
assert.equal(seriesPct({ total: 0 }), 0);

assert.equal(markText(null, 600), '10:00', 'nothing watched: the length');
assert.equal(markText({ finished: true, device: '这台电脑' }), '看过 · 这台电脑');
assert.equal(markText({ position: 95, duration: 600, device: '小电视' }), '看到 1:35 / 10:00 · 小电视');
assert.equal(markText(null, 0), '');

// Long series list a window around the current episode; short ones list everything.
assert.deepEqual(episodeWindow(30, 10, 120), [0, 30]);
assert.deepEqual(episodeWindow(900, 1, 120), [0, 120]);
assert.deepEqual(episodeWindow(900, 500, 120), [479, 599], 'some before, most after');
assert.deepEqual(episodeWindow(900, 900, 120), [780, 900], 'clamped at the end');

// Marks from GET api/progress replace the page's copies; only a real change asks for a redraw.
const page = { id: 'sr01', progress: going, episodes: [{ key: 'v:bilibili:e1', mark: null }, { key: 'v:bilibili:e2', mark: null }] };
assert.equal(applyMarks(page, { 'v:bilibili:e1': null }), false);
const later = { position: 120, duration: 600, finished: false, device: '这台电脑', at: 5 };
assert.equal(applyMarks(page, { 'v:bilibili:e2': later, 'ch:sr01': going }), true);
assert.deepEqual(page.episodes[1].mark, later);
assert.equal(applyMarks(page, { 'v:bilibili:e2': { ...later } }), false, 'same values: no redraw');
assert.equal(applyMarks(page, { 'ch:sr01': done }), true);
assert.equal(page.progress, done);
const up = { id: '', videos: [{ key: 'v:youtube:x', mark: null }] };
assert.equal(applyMarks(up, { 'v:youtube:x': later }), true, 'a followed-only creator has videos and no channel');

console.log('console detail helpers: ok');
