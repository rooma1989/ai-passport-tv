// Tests for the update line in server/relay/assets/console.js
// (the block between "「更新」的纯函数" and "「更新」的纯函数到此"; cut out and evaluated on its own).
// Run by tests/test_relay_updates.py under Node (18+); also runnable on its own.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 「更新」的纯函数');
const end = source.indexOf('// ---- 「更新」的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { updLine } = new Function(source.slice(begin, end) + '\nreturn { updLine };')();

assert.equal(updLine(null), '');
assert.equal(updLine({ on: false }), '', 'a checkout or Docker: nothing to say');
assert.equal(updLine({ on: true, state: 'ready', version: '0.4.0' }), '新版本 0.4.0 已就绪');
assert.equal(updLine({ on: true, state: 'downloading', version: '0.4.0', done: 30, total: 120 }), '正在下载新版本 0.4.0（25%）');
assert.equal(updLine({ on: true, state: 'downloading', version: '0.4.0' }), '正在下载新版本 0.4.0');
assert.equal(updLine({ on: true, state: 'checking' }), '正在检查更新…');
assert.equal(updLine({ on: true, state: 'failed', error: '连不上GitHub' }), '检查更新没成功：连不上GitHub');
assert.equal(updLine({ on: true, state: 'latest', current: '0.3.8-beta' }), '已经是最新版本（0.3.8-beta）');
console.log('console update: ok');
