// Tests for My library's sorting and filtering in server/relay/assets/console.js (the block between
// "片库筛选的几个纯函数" and "片库筛选的纯函数到此"; the page is a plain script, so the block is cut out and
// evaluated on its own).
// Run by tests/test_relay_libdouban.py under Node (18+); also runnable on its own:
//   node tests/console_libfilter_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 片库筛选的几个纯函数');
const end = source.indexOf('// ---- 片库筛选的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { LIB_SORTS, libMetaOf, libFacets, libFilter, libSort } = new Function(
  source.slice(begin, end) + '\nreturn { LIB_SORTS, libMetaOf, libFacets, libFilter, libSort };')();

const meta = (rating, genres, countries) => ({ state: 'matched', rating, genres, countries });
// In the order GET /api/library gives them: newest first.
const movies = [
  { id: 'a', title: '星际穿越', meta: meta(9.4, ['剧情', '科幻'], ['美国', '英国']) },
  { id: 'b', title: '霸王别姬', meta: meta(9.6, ['剧情', '爱情'], ['中国大陆', '中国香港']) },
  { id: 'c', title: 'Alien', meta: { state: 'miss' } },
  { id: 'd', title: '阿凡达', meta: meta(8.8, ['科幻'], ['美国']) },
  { id: 'e', title: '无评分', meta: meta(undefined, ['剧情'], ['美国']) },
];
const shows = [
  // the show itself did not match; its first matched season counts
  { id: 's1', title: 'X医生', meta: { state: 'miss' }, season_rows: [{ n: 1, meta: null }, { n: 2, meta: meta(9.0, ['剧情', '医疗'], ['日本']) }] },
  { id: 's2', title: '苏里南', meta: meta(6.9, ['剧情', '动作'], ['韩国']) },
];

// ---- the default order is the relay's (newest first), untouched ----
assert.deepEqual(LIB_SORTS.map(([id]) => id), ['added', 'rating', 'title']);
assert.equal(libSort(movies, 'added'), movies, 'the default keeps the list as given');
assert.equal(libSort(movies, 'nonsense'), movies);

// ---- by rating: highest first, unrated last, ties keep the given order ----
assert.deepEqual(libSort(movies, 'rating').map((x) => x.id), ['b', 'a', 'd', 'c', 'e']);
assert.deepEqual(movies.map((x) => x.id), ['a', 'b', 'c', 'd', 'e'], 'sorting does not change the original');
assert.deepEqual(libSort(shows, 'rating').map((x) => x.id), ['s1', 's2'], 'a season rating counts for its show');

// ---- by title: Chinese by pinyin (阿 a, 霸 b, 无 w, 星 x), Latin letters together ----
const byTitle = libSort(movies, 'title').map((x) => x.title);
assert.ok(byTitle.indexOf('阿凡达') < byTitle.indexOf('霸王别姬') && byTitle.indexOf('霸王别姬') < byTitle.indexOf('无评分')
  && byTitle.indexOf('无评分') < byTitle.indexOf('星际穿越'), byTitle.join(','));

// ---- facets: what the library holds, most common first ----
const facets = libFacets([movies, shows]);
assert.deepEqual(facets.genres[0], ['剧情', 5]);
assert.deepEqual(facets.genres.find(([g]) => g === '科幻'), ['科幻', 2]);
assert.deepEqual(facets.genres.find(([g]) => g === '医疗'), ['医疗', 1], 'from a season');
assert.deepEqual(facets.countries[0], ['美国', 3]);
assert.equal(facets.countries.length, 6);
assert.deepEqual(libFacets([[{ id: 'x', meta: null }]]), { genres: [], countries: [] }, 'nothing matched: no filters');

// ---- filters: genre and region together; unmatched ones drop out only while filtering ----
assert.equal(libFilter(movies, { genre: '', country: '' }), movies);
assert.deepEqual(libFilter(movies, { genre: '科幻', country: '' }).map((x) => x.id), ['a', 'd']);
assert.deepEqual(libFilter(movies, { genre: '剧情', country: '美国' }).map((x) => x.id), ['a', 'e']);
assert.deepEqual(libFilter(movies, { genre: '', country: '中国香港' }).map((x) => x.id), ['b']);
assert.deepEqual(libFilter(shows, { genre: '医疗', country: '' }).map((x) => x.id), ['s1']);
assert.deepEqual(libFilter(movies, { genre: '纪录片', country: '' }), []);
assert.equal(libMetaOf({ meta: { state: 'none' } }), null);

console.log('console libfilter: ok');
