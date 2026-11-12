// Tests for My library's episode wording, version labels and the "assign to a show" helpers in
// server/relay/assets/console.js (the block between "片库归集的几个纯函数" and "片库归集的纯函数到此"; the page is a
// plain script, so the block is cut out and evaluated on its own).
// Run by tests/test_relay_library_assign.py under Node (18+); also runnable on its own:
//   node tests/console_libassign_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 片库归集的几个纯函数');
const end = source.indexOf('// ---- 片库归集的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { libHave, libTotal, libRes, libVersionLabel, libFolderName, asgGuess, asgGroups, asgCheck, asgRange } = new Function(
  source.slice(begin, end) + '\nreturn { libHave, libTotal, libRes, libVersionLabel, libFolderName, asgGuess, asgGroups, asgCheck, asgRange };')();

// what the library holds vs what the metadata says, never mixed
assert.equal(libHave(22), '已收录 22 集');
assert.equal(libHave(0), '已收录 0 集');
assert.equal(libTotal({ type: 'tv', episodes: 22 }), '全 22 集');
assert.equal(libTotal({ type: 'tv' }), '');
assert.equal(libTotal({ type: 'movie', episodes: 1 }), '');
assert.equal(libTotal(null), '');

assert.equal(libRes(2160), '4K');
assert.equal(libRes(1080), '1080p');
assert.equal(libRes(720), '720p');
assert.equal(libRes(480), '480p');
assert.equal(libRes(0), '');
assert.equal(libFolderName('蒸蒸日上/4K'), '4K');
assert.equal(libFolderName(''), '根目录');
assert.equal(libVersionLabel({ folder: '蒸蒸日上/4K', height: 2160, size: 3 * 1073741824, ext: 'mkv', source: '网盘' }),
  '4K · 4K · 3.0 GB · MKV · 网盘');
assert.equal(libVersionLabel({ folder: '', height: 0, size: 0, ext: 'mp4', source: '' }), '根目录 · MP4');

// guessing the episode from a file name (the admin can change it)
assert.equal(asgGuess('MTJBYDD-01.mp4'), 1);
assert.equal(asgGuess('MTJBYDD.12.1080p.mkv'), 12);
assert.equal(asgGuess('蒸蒸日上.第7集.mp4'), 7);
assert.equal(asgGuess('Show.S02E05.2160p.mkv'), 5);
assert.equal(asgGuess('show EP03 x264.mp4'), 3);
assert.equal(asgGuess('剧名 2019 1080p 08.mp4'), 8);
assert.equal(asgGuess('福音的故事.mp4'), 0);

// groups by abbreviation are clues: same folder, two or more, biggest first
const videos = [
  ...Array.from({ length: 10 }, (_, i) => ({ id: 'm' + i, folder: '转存', file: `MTJBYDD-${String(i + 1).padStart(2, '0')}.mp4` })),
  { id: 'o1', folder: '其他', file: 'MTJBYDD-01.mp4' },
  { id: 'o2', folder: '其他', file: 'mtjbydd-02.mp4' },
  { id: 'x', folder: '转存', file: '福音的故事.mp4' },
  { id: 'y', folder: '转存', file: 'AB-01.mp4' },
];
const groups = asgGroups(videos);
assert.deepEqual(groups.map((g) => [g.label, g.ids.length]), [['MTJBYDD', 10], ['MTJBYDD', 2]]);
assert.ok(!groups.some((g) => g.ids.includes('x') || g.ids.includes('y')));

// checks before saving
assert.deepEqual(asgCheck([{ id: 'a', episode: 1 }, { id: 'b', episode: 3 }], []),
  { bad: [], twice: [], clash: [], missing: [2] });
assert.deepEqual(asgCheck([{ id: 'a', episode: 1 }, { id: 'b', episode: 1 }, { id: 'c', episode: 2 }], [2, 4]),
  { bad: [], twice: [1], clash: [2], missing: [3] });
assert.deepEqual(asgCheck([{ id: 'a', episode: 0 }, { id: 'b', episode: NaN }, { id: 'c', episode: 1.5 }], []).bad, ['a', 'b', 'c']);
assert.deepEqual(asgCheck([], []), { bad: [], twice: [], clash: [], missing: [] });
assert.equal(asgRange([1, 2, 3, 5, 7, 8]), '1–3、5、7–8');
assert.equal(asgRange([4]), '4');
assert.equal(asgRange([]), '');

console.log('console_libassign_test: ok');
