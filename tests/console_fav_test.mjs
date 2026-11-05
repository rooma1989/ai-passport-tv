// Tests for the favourite-channel helpers in server/relay/assets/console.js
// (the block between "收藏夹台的纯函数" and "收藏夹台的纯函数到此"; cut out and evaluated on its own).
// Run by tests/test_relay_bilifav.py under Node (18+); also runnable on its own:
//   node tests/console_fav_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
for (const file of ['console.js', 'remote.html']) {
  const source = readFileSync(join(ROOT, 'server/relay/assets', file), 'utf8');
  const begin = source.indexOf('// ---- 收藏夹台的纯函数');
  const end = source.indexOf('// ---- 收藏夹台的纯函数到此');
  assert.ok(begin > 0 && end > begin, 'the pure block is marked in ' + file);
  const { FIXED_GROUPS, isFav, favChannels, seriesOnly, movableGroup } = new Function(source.slice(begin, end)
    + '\nreturn { FIXED_GROUPS, isFav, favChannels, seriesOnly, movableGroup };')();

  assert.ok(Object.isFrozen(FIXED_GROUPS), file);
  assert.equal(isFav("fv01"), true, file);
  assert.equal(isFav("fv1"), false, file);
  assert.equal(isFav("sr01"), false, file);
  assert.equal(isFav("fv01x"), false, file);
  const series = [{ id: "sr01" }, { id: "fv01" }, { id: "sr02" }, { id: "fv02" }];
  assert.deepEqual(favChannels(series).map((s) => s.id), ["fv01", "fv02"], file);
  assert.deepEqual(seriesOnly(series).map((s) => s.id), ["sr01", "sr02"], file);
  assert.deepEqual(favChannels(undefined), [], file);
  assert.deepEqual(seriesOnly(null), [], file);
  for (const g of ["我的投屏", "UP 主", "B站收藏", "常看"]) assert.equal(movableGroup(g), false, file + ' ' + g);
  for (const g of ["央视", "动画影视", "其他"]) assert.equal(movableGroup(g), true, file + ' ' + g);
}
console.log('console_fav_test: ok');
