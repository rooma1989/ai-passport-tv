// Tests for where an imported channel goes in the channel table, server/relay/assets/console-admin.js
// (the block between "导入的新台放在哪：纯函数" and "导入的新台放在哪：纯函数到此").
// Run by tests/test_relay_admin.py under Node (18+); also runnable on its own.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console-admin.js'), 'utf8');
const begin = source.indexOf('// ---- 导入的新台放在哪：纯函数');
const end = source.indexOf('// ---- 导入的新台放在哪：纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console-admin.js');
const { achInsertAt } = new Function(source.slice(begin, end) + '\nreturn { achInsertAt };')();

const GROUPS = ['央视', '卫视', '港澳台', '国际', '音乐', '动画影视', '节目回看', '其他'];
const of = (row) => row.g;
const table = (...gs) => gs.map((g, i) => ({ g, n: i }));

// The owner's case: every 央视 channel deleted, the table starts with 卫视 → CCTV goes back to the top.
const rows = table('卫视', '卫视', '港澳台', '节目回看');
assert.equal(achInsertAt(rows, '央视', GROUPS, of), 0, 'no 央视 left: before the first later group');
// One at a time keeps the list's order: each lands after the last one of its group.
const imported = [];
for (const name of ['CCTV1', 'CCTV2', 'CCTV3']) {
  const row = { g: '央视', name };
  rows.splice(achInsertAt(rows, row.g, GROUPS, of), 0, row);
  imported.push(row);
}
assert.deepEqual(rows.slice(0, 4).map((r) => r.name || r.g), ['CCTV1', 'CCTV2', 'CCTV3', '卫视']);
assert.equal(achInsertAt(rows, '卫视', GROUPS, of), 5, 'after the last 卫视');
assert.equal(achInsertAt(rows, '国际', GROUPS, of), 6, 'no 国际 yet: before 节目回看');
assert.equal(achInsertAt(rows, '其他', GROUPS, of), rows.length, 'nothing after 其他: at the end');
assert.equal(achInsertAt(rows, '', GROUPS, of), rows.length, 'unknown group: at the end');
assert.equal(achInsertAt([], '央视', GROUPS, of), 0);
// Rows whose group is unknown are skipped when looking for the place.
assert.equal(achInsertAt(table('', '卫视'), '央视', GROUPS, of), 1);
// A hand-sorted table keeps its order: a group in the middle grows where it already is.
assert.equal(achInsertAt(table('卫视', '央视', '卫视'), '央视', GROUPS, of), 2);
console.log('console admin import placement: ok');
