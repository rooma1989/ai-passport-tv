// Tests for the home page's shelves and hero image in server/relay/assets/console.js
// (the block between "货架和大图的几个纯函数" and "货架和大图的纯函数到此"; the page is a plain
// script, so the block is cut out and evaluated on its own).
// Run by tests/test_relay_home.py under Node (18+); also runnable on its own:
//   node tests/console_shelves_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 货架和大图的几个纯函数');
const end = source.indexOf('// ---- 货架和大图的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { heroFits, heroSrc, mouseWheel, wheelNotches, shelfCan, cfOf, cfPeople, cfFilter } = new Function(
  source.slice(begin, end) + '\nreturn { heroFits, heroSrc, mouseWheel, wheelNotches, shelfCan, cfOf, cfPeople, cfFilter };')();

// Hero: a wide image at least as wide as the hero (85 % on a normal screen) fills it; a small cover
// (葫芦小金刚's 240 x 320 poster) or a portrait one becomes the blurred backdrop + poster card.
assert.equal(heroFits(1920, 1080, 1440, 2), true);
assert.equal(heroFits(1280, 720, 1440, 2), false, 'retina: needs the hero width');
assert.equal(heroFits(1280, 720, 1440, 1), true, '1x screen: 85 % is enough');
assert.equal(heroFits(1146, 717, 1440, 1), false);
assert.equal(heroFits(240, 320, 1440, 1), false, 'small poster');
assert.equal(heroFits(480, 270, 1440, 1), false, "a live channel's 480 px frame (api/art/live) is small too");
assert.equal(heroFits(3000, 4000, 1440, 2), false, 'portrait never fills a wide hero');
assert.equal(heroFits(0, 0, 1440, 1), false);
assert.equal(heroFits(1920, 1080, 1440, 3), true, 'device pixel ratio counted up to 2');

// The big image: Bilibili through the proxy at s=h, YouTube's largest thumbnail; anything else unchanged.
const BASE = 'http://127.0.0.1:8080/tv/console';
const bili = 'api/bili/thumb?s=p&u=' + encodeURIComponent('https://i0.hdslb.com/bfs/bangumi/p.jpg');
const big = new URL(heroSrc(bili, BASE), BASE);
assert.equal(big.pathname, '/tv/api/bili/thumb');
assert.equal(big.searchParams.get('s'), 'h');
assert.equal(big.searchParams.get('u'), 'https://i0.hdslb.com/bfs/bangumi/p.jpg');
const yt = 'api/bili/thumb?s=v&u=' + encodeURIComponent('https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg');
const ybig = new URL(heroSrc(yt, BASE), BASE);
assert.equal(ybig.searchParams.get('u'), 'https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg');
assert.equal(ybig.searchParams.get('s'), 'v', 'the proxy ignores the size for YouTube');
assert.equal(heroSrc('api/art/logo/ch001', BASE), 'api/art/logo/ch001');
assert.equal(heroSrc('api/art/live/ch001?v=3', BASE), 'api/art/live/ch001?v=3', 'live frames are used as they are');

// Mouse wheel vs trackpad: only a real wheel turns into horizontal shelf scrolling.
assert.equal(mouseWheel({ deltaY: 100, deltaX: 0, deltaMode: 0, wheelDeltaY: -120 }), true, 'Chrome/Edge wheel notch');
assert.equal(mouseWheel({ deltaY: 3, deltaX: 0, deltaMode: 1 }), true, 'Firefox: lines');
assert.equal(mouseWheel({ deltaY: 53, deltaX: 0, deltaMode: 0 }), true, 'large integer step');
assert.equal(mouseWheel({ deltaY: 4.5, deltaX: 0, deltaMode: 0, wheelDeltaY: -13 }), false, 'trackpad: small fractional');
assert.equal(mouseWheel({ deltaY: 12, deltaX: 0, deltaMode: 0, wheelDeltaY: -36 }), false, 'trackpad: small integer');
assert.equal(mouseWheel({ deltaY: 80, deltaX: 3, deltaMode: 0 }), false, 'trackpad: any sideways part');
assert.equal(mouseWheel({ deltaY: 100, deltaX: 0, deltaMode: 0, shiftKey: true }), false, 'Shift: the browser scrolls sideways itself');
assert.equal(mouseWheel({ deltaY: 100, deltaX: 0, deltaMode: 0, ctrlKey: true }), false, 'pinch zoom');
assert.equal(mouseWheel({ deltaY: 0, deltaX: 0, deltaMode: 0 }), false);
assert.equal(mouseWheel({ deltaY: 1, deltaX: 0, deltaMode: 2 }), false, 'pages: leave it');
assert.equal(wheelNotches({ deltaY: 100, deltaMode: 0 }), 1);
assert.equal(wheelNotches({ deltaY: -300, deltaMode: 0 }), 3);
assert.equal(wheelNotches({ deltaY: 3, deltaMode: 1 }), 1);
assert.equal(wheelNotches({ deltaY: 40, deltaMode: 0 }), 1, 'at least one card');

// The shelf hands the wheel back to the page at either end.
assert.equal(shelfCan(0, 3000, 1440, -1), false, 'at the start: the page scrolls up');
assert.equal(shelfCan(0, 3000, 1440, 1), true);
assert.equal(shelfCan(1560, 3000, 1440, 1), false, 'at the end');
assert.equal(shelfCan(1557.5, 3000, 1440, 1), false, 'sub-pixel end');
assert.equal(shelfCan(1560, 3000, 1440, -1), true);
assert.equal(shelfCan(0, 1000, 1440, 1), false, 'nothing to scroll');

// 关注的更新 by creator: one avatar per creator per site, newest first, filled in from later items.
const items = [
  { id: 'a', source: 'bilibili', creator: { id: '1', name: '大狸子切切里', face: '' } },
  { id: 'b', source: 'youtube', creator: { id: 'UCx', name: 'Chan', face: '' } },
  { id: 'c', source: 'bilibili', creator: { id: '1', name: '大狸子切切里', face: 'f1' } },
  { id: 'd', source: 'bilibili', creator: { id: '2', name: '极速拍档', face: 'f2' } },
  { id: 'e', source: 'youtube', creator: { name: 'NoId' } },
];
const who = cfPeople(items);
assert.deepEqual(who.map((c) => c.key), ['bilibili:1', 'youtube:UCx', 'bilibili:2', 'youtube:NoId']);
assert.equal(who[0].face, 'f1', 'a later item with a face fills it in');
assert.equal(who[1].site, 'youtube');
assert.deepEqual(cfFilter(items, 'bilibili:1').map((x) => x.id), ['a', 'c']);
assert.deepEqual(cfFilter(items, '').map((x) => x.id), ['a', 'b', 'c', 'd', 'e']);
assert.deepEqual(cfFilter(items, 'youtube:1'), [], 'same id on another site is someone else');
assert.equal(cfOf({ subtitle: '旧的' }), 'bilibili:旧的');

console.log('console shelves: ok');
