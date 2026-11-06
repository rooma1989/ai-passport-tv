// Tests for the Websites page's pure functions in server/relay/assets/console.js
// (the block between "「网站」的纯函数" and "「网站」的纯函数到此"; cut out and evaluated on its own).
// Run by tests/test_relay_websites.py under Node (18+); also runnable on its own:
//   node tests/console_web_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 「网站」的纯函数');
const end = source.indexOf('// ---- 「网站」的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { webSiteKey, webSiteName, webUrlOf, webItem, WEB_SITES } = new Function(source.slice(begin, end)
  + '\nreturn { webSiteKey, webSiteName, webUrlOf, webItem, WEB_SITES };')();

// Same keys as the server's websites.site_key.
assert.equal(webSiteKey('https://v.youku.com/v_show/id_X.html'), 'youku.com');
assert.equal(webSiteKey('https://news.cctv.com.cn/x'), 'cctv.com.cn');
assert.equal(webSiteKey('https://www.bbc.co.uk/iplayer'), 'bbc.co.uk');
assert.equal(webSiteKey('https://1.2.3.4/x'), '');
assert.equal(webSiteKey('not a url'), '');

assert.equal(webSiteName('ixigua.com'), '西瓜视频');
assert.equal(webSiteName('example.org'), 'example.org', 'unknown sites show their domain');
assert.ok(Object.isFrozen(WEB_SITES));

assert.equal(webUrlOf('看这个 https://www.ixigua.com/7012345678901234567?logTag=abc，太好笑了'),
  'https://www.ixigua.com/7012345678901234567?logTag=abc');
assert.equal(webUrlOf('https://tv.cctv.com/2024/01/01/VIDE1.shtml.'), 'https://tv.cctv.com/2024/01/01/VIDE1.shtml');
assert.equal(webUrlOf('www.ixigua.com/123'), '', 'a link needs http(s)://');
assert.equal(webUrlOf(''), '');

const item = webItem({ url: 'https://www.dailymotion.com/video/x1', title: 'A clip', duration: 61,
  thumb: 'api/websites/thumb?u=x', uploader: '' }, 'dailymotion.com');
assert.deepEqual(item.play, { kind: 'cast', url: 'https://www.dailymotion.com/video/x1' },
  'plays like a pasted link: PC watch_direct or TV cast');
assert.equal(item.creator.name, 'Dailymotion');
assert.equal(item.art.url, 'api/websites/thumb?u=x');
assert.equal(webItem({ url: 'https://a.com/v' }, 'a.com').title, 'https://a.com/v');
console.log('console web: ok');
