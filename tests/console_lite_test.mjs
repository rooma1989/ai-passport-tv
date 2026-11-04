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
const { LITE_HIDE, LITE_TEXT, liteNav, liteRoute, liteOff, liteText, liteGroups } = new Function(source.slice(begin, end)
  + '\nreturn { LITE_HIDE, LITE_TEXT, liteNav, liteRoute, liteOff, liteText, liteGroups };')();

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
assert.ok(Object.isFrozen(LITE_TEXT));

for (const f of ["pc", "cast", "youtube", "invites"]) {
  assert.equal(liteOff(f, "lite"), true, f + ' is off in lite');
  assert.equal(liteOff(f, "full"), false, f + ' stays in full');
}
assert.equal(liteOff("live", "lite"), false);
assert.equal(liteText("adminSub", "用户、设备、频道表、节目单", "lite"), "设备、频道");
assert.equal(liteText("adminSub", "用户、设备、频道表、节目单", "full"), "用户、设备、频道表、节目单");
assert.equal(liteText("nope", "原样", "lite"), "原样", 'unlisted keys keep the full wording');
assert.equal(liteText("upCard", "B 站或 YouTube 的 UP 主，每个都变成一个台，新视频自动排上。", "lite"),
  "B 站的 UP 主，每个都变成一个台，新视频自动排上。", 'the home onboarding card drops YouTube');
const quick = ["series", "up", "lib", "live"];
assert.deepEqual(quick.filter((id) => liteRoute(id, "lite") === id), ["up", "live"], 'home quick links in lite');
assert.deepEqual(quick.filter((id) => liteRoute(id, "full") === id), quick, 'home quick links in full');
assert.equal(liteText("toString", "原样", "lite"), "原样", 'no prototype keys');
for (const [k, v] of Object.entries(LITE_TEXT)) assert.ok(!/YouTube|用户|节目单/.test(v), k + ' mentions nothing lite lacks');

const groups = [{ name: "我的投屏", channels: [{ id: "cast" }] }, { name: "UP 主", channels: [{ id: "up01" }] },
                { name: "央视", channels: [{ id: "cctv1" }, { id: "cast" }] }];
assert.equal(liteGroups(groups, "full"), groups, 'full gets the very same list');
const lite = liteGroups(groups, "lite");
assert.deepEqual(lite.map((g) => g.name), ["UP 主", "央视"], 'the cast group is gone');
assert.equal(lite[0], groups[1], 'untouched groups are passed through');
assert.deepEqual(lite[1].channels.map((c) => c.id), ["cctv1"]);
assert.equal(groups[2].channels.length, 2, 'the input is not mutated');
console.log('console_lite_test: ok');
