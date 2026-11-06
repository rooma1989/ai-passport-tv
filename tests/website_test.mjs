// Tests for the website's pure functions in packaging/website/app.js
// (the block between "官网的纯函数" and "官网的纯函数到此"; cut out and evaluated on its own).
// Run by tests/test_build_site.py under Node (18+); also runnable on its own.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'packaging/website/app.js'), 'utf8');
const begin = source.indexOf('// ---- 官网的纯函数');
const end = source.indexOf('// ---- 官网的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in app.js');
const { bust, esc, inline, guess, size, rows } = new Function(source.slice(begin, end)
  + '\nreturn { bust, esc, inline, guess, size, rows };')();

assert.equal(bust(0), 0);
assert.equal(bust(299999), 0);
assert.equal(bust(300000), 1, 'the JSON address changes every 5 minutes');
assert.equal(esc('<a href="x">&'), '&lt;a href=&quot;x&quot;&gt;&amp;');
assert.equal(inline('**新**：`x` <b>'), '<b>新</b>：<code>x</code> &lt;b&gt;');
assert.equal(inline('见 [文档](../docs/x.md)'), '见 文档', 'links show their text only');
assert.equal(guess('Mozilla/5.0 (Windows NT 10.0; Win64; x64)', ''), 'windows-x64');
assert.equal(guess('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', ''), 'macos-arm64', 'Safari hides the chip: Apple silicon first');
assert.equal(guess('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 'x86'), 'macos-x86_64');
assert.equal(guess('Mozilla/5.0 (X11; Linux x86_64)', ''), 'linux-x86_64');
assert.equal(guess('Mozilla/5.0 (Linux; Android 14)', ''), '');
assert.equal(size(118000000), '113 MB');
assert.equal(size(2000), '2 KB');

const manifest = { version: '0.3.9-beta', files: {
  'macos-arm64': { name: 'RooTV-0.3.9-beta-macos-arm64.dmg', size: 1, sha256: 'a' },
  'windows-x64': { name: 'RooTV-0.3.9-beta-windows-x64-setup.exe', size: 1, sha256: 'b' },
  'macos-arm64-lite': { name: 'RooTV-0.3.9-beta-lite-macos-arm64.dmg', size: 1, sha256: 'c' },
  'linux-x86_64': { name: '../evil', size: 1, sha256: 'd' } } };
const list = rows(manifest, 'windows-x64');
assert.deepEqual(list.map((r) => r.key), ['windows-x64', 'macos-arm64'], 'own platform first; lite and odd names left out');
assert.equal(list[0].mirror, '../rootv/0.3.9-beta/RooTV-0.3.9-beta-windows-x64-setup.exe');
assert.equal(list[0].github, 'https://github.com/rooma1989/ai-passport-tv/releases/download/v0.3.9-beta/RooTV-0.3.9-beta-windows-x64-setup.exe');
assert.deepEqual(rows(null, ''), []);
console.log('website: ok');
