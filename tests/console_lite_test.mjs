// Tests for the lite edition's navigation filter in server/relay/assets/console.js
// (the block between "精简版导航的纯函数" and "精简版导航的纯函数到此"; cut out and evaluated on its own).
// Run by tests/test_lite_relay.py under Node (18+); also runnable on its own:
//   node tests/console_lite_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 精简版导航的纯函数');
const end = source.indexOf('// ---- 精简版导航的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { LITE_HIDE, liteNav, liteRoute } = new Function(source.slice(begin, end) + '\nreturn { LITE_HIDE, liteNav, liteRoute };')();

const top = [["home", "首页"], ["live", "直播"], ["series", "剧集"], ["up", "UP 主"], ["lib", "片库"]];
assert.deepEqual(liteNav(top, "full", "top"), top, 'full keeps everything');
assert.deepEqual(liteNav(top, "lite", "top").map(([id]) => id), ["home", "live", "up"]);
const sets = [["general"], ["account"], ["bili"], ["youtube"], ["history"]];
assert.deepEqual(liteNav(sets, "lite", "settings").map(([id]) => id), ["general", "account", "bili"]);
const adm = [["overview"], ["users"], ["devices"], ["channels"], ["audit"], ["system"]];
assert.deepEqual(liteNav(adm, "lite", "admin").map(([id]) => id), ["overview", "devices", "pack", "system"],
  'the channel editor becomes the pack page');
assert.deepEqual(liteNav([["home"],["live"]], "lite", "tabs").map(([id]) => id), ["home", "live"]);
assert.deepEqual(liteNav([["x"]], "lite", "nope"), [["x"]]);
assert.equal(liteRoute("lib", "lite"), "home", 'a hidden route falls back to home');
assert.equal(liteRoute("lib", "full"), "lib");
assert.equal(liteRoute("live", "lite"), "live");
assert.ok(Object.isFrozen(LITE_HIDE));
console.log('console_lite_test: ok');
