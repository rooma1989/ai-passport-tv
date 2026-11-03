// Tests for the library play history's helpers in server/relay/assets/console.js
// (the block between "片库播放历史的几个纯函数" and "片库播放历史的纯函数到此"; the page is a plain script,
// so the block is cut out and evaluated on its own).
// Run by tests/test_relay_library_history.py under Node (18+); also runnable on its own:
//   node tests/console_libhist_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 片库播放历史的几个纯函数');
const end = source.indexOf('// ---- 片库播放历史的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { libHistItem, histMerge } = new Function(source.slice(begin, end) + '\nreturn { libHistItem, histMerge };')();

// An episode of a show with several seasons: the season is in the title line, the episode's own name below.
const ep = libHistItem({ id: 'a'.repeat(16), kind: 'episode', name: 'X医生', title: '急诊', season: 2, seasons: 2,
                         episode: 5, art: 'api/library/art/aaaa', position: 600, duration: 2700, at: 1000,
                         finished: false, device: '客厅', playable: true });
assert.equal(ep.title, 'X医生 · 第 2 季 第 5 集');
assert.deepEqual(ep.hist, { head: 'X医生 · 第 2 季 第 5 集', ep: '急诊', type: 'lib', device: '客厅' });
assert.deepEqual(ep.progress, { position: 600, duration: 2700, done: false });
assert.deepEqual(ep.play, { kind: 'library', id: 'a'.repeat(16), playable: true }, 'plays through the library path');
assert.equal(ep.watched_at, 1000);
assert.equal(ep.id, 'lh-' + 'a'.repeat(16));
assert.equal(ep.lib, 'a'.repeat(16));

// One season: no season in the line; an episode titled like the show shows no second line.
const one = libHistItem({ id: 'b'.repeat(16), kind: 'episode', name: '家庭剧', title: '家庭剧', season: 1, seasons: 1,
                          episode: 3, position: 0, duration: 0, at: 5, finished: true });
assert.equal(one.title, '家庭剧 · 第 3 集');
assert.equal(one.hist.ep, '');
assert.equal(one.progress.done, true);
for (const title of ['第 3 集', '第3集', 'S01E03', 'e03', 'EP 3']) {
  assert.equal(libHistItem({ id: 'b'.repeat(16), kind: 'episode', name: '家庭剧', title, episode: 3, at: 1 }).hist.ep, '',
               'a title that is only the number is not repeated: ' + title);
}
assert.equal(one.hist.device, '', 'watched on the PC: no device');

// Movies and other videos: the name, and what it is.
const movie = libHistItem({ id: 'c'.repeat(16), kind: 'movie', name: '盗梦空间', title: 'Inception', position: 30,
                            duration: 8000, at: 7, finished: false, playable: false });
assert.equal(movie.title, '盗梦空间');
assert.equal(movie.hist.ep, '电影');
assert.equal(movie.play.playable, false);
assert.equal(libHistItem({ id: 'd'.repeat(16), kind: 'video', name: '', title: 'Clip', at: 1 }).title, 'Clip');
assert.equal(libHistItem({ id: 'd'.repeat(16), kind: 'video', name: '', title: 'Clip', at: 1 }).hist.ep, '视频');

// "All": interleaved by time, newest first; with more Bilibili pages to load, older library rows wait.
const b = (id, at) => ({ id, watched_at: at });
const bili = [b('b1', 900), b('b2', 500), b('b3', 300)];
const libs = [b('l1', 950), b('l2', 600), b('l3', 100)];
assert.deepEqual(histMerge(bili, libs, false).map((x) => x.id), ['l1', 'b1', 'l2', 'b2', 'b3', 'l3']);
assert.deepEqual(histMerge(bili, libs, true).map((x) => x.id), ['l1', 'b1', 'l2', 'b2', 'b3'],
                 'older than the oldest loaded Bilibili row: after "load more"');
assert.deepEqual(histMerge([], libs, true).map((x) => x.id), ['l1', 'l2', 'l3'], 'no Bilibili rows: all of them');
assert.deepEqual(histMerge(bili, [], true).map((x) => x.id), ['b1', 'b2', 'b3'], 'Bilibili order unchanged');
assert.deepEqual(histMerge([b('b', 5)], [b('l', 5)], false).map((x) => x.id), ['b', 'l'], 'ties: Bilibili first');

console.log('console_libhist_test: ok');
