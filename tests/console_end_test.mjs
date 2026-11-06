// Tests for the player's end-screen helpers in server/relay/assets/console.js
// (the block between "「放完以后」的纯函数" and "「放完以后」的纯函数到此").
// Run by tests/test_relay_pcplayer.py under Node (18+); also runnable on its own.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 「放完以后」的纯函数');
const end = source.indexOf('// ---- 「放完以后」的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { endNeighbours } = new Function(source.slice(begin, end) + '\nreturn { endNeighbours };')();

const row = ['a', 'b', 'c', 'd'];
assert.deepEqual(endNeighbours(row, 1), { next: 'c', prev: 'a', prevIsBack: true }, 'middle of a row: back and forward');
assert.deepEqual(endNeighbours(row, 0), { next: 'b', prev: 'c', prevIsBack: false }, 'first card: the left one is another suggestion');
assert.deepEqual(endNeighbours(row, 3), { next: null, prev: 'c', prevIsBack: true }, 'last card: nothing plays next');
assert.deepEqual(endNeighbours(['a'], 0), { next: null, prev: null, prevIsBack: false });
assert.deepEqual(endNeighbours(row, -1), { next: null, prev: null, prevIsBack: false });
assert.deepEqual(endNeighbours(null, 0), { next: null, prev: null, prevIsBack: false });
console.log('console end screen: ok');
