// Tests for packaging/flash/flasher-core.js, the web flasher's decisions.
// Run by tests/test_flasher.py under Node (18+); also runnable on its own:
//   node tests/flasher_core_test.mjs            run the tests
//   node tests/flasher_core_test.mjs --policy   print BOARD_POLICY as JSON
//   node tests/flasher_core_test.mjs --plan IMAGE
//        print updatePlan(IMAGE, IMAGE's own table) as JSON (test_flasher.py
//        compares it with tools/firmware_manifest.py's update_plan)
// With FLASHER_RELEASE_DIR set to a folder holding the published images
// (FoloToy-AI-Passport-tv.bin, Xingzhi-Cube-154-tv-public.bin), the update
// plan is also checked on those.
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as core from '../packaging/flash/flasher-core.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

if (process.argv.includes('--policy')) {
  console.log(JSON.stringify(core.BOARD_POLICY));
  process.exit(0);
}
if (process.argv.includes('--plan')) {
  const image = new Uint8Array(readFileSync(process.argv[process.argv.indexOf('--plan') + 1]));
  const table = image.subarray(core.PARTITION_TABLE_OFFSET, core.PARTITION_TABLE_OFFSET + core.PARTITION_TABLE_MAX);
  console.log(JSON.stringify(core.updatePlan(image, table)));
  process.exit(0);
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const throwsCode = (fn, code) => assert.throws(fn, (e) => e instanceof core.FlasherError && e.code === code);

const SHA = 'a'.repeat(64);
const manifest = () => ({
  schema: 1, project: 'xiaozhi-tv', version: 'v0.1.0',
  builds: [
    { board: 'xingzhi-cube-154', chip: 'ESP32-S3', erase: 'first-install', write_limit: 8 << 20, protect: [],
      parts: [{ path: 'Xingzhi-Cube-154-tv-public.bin', offset: 0, size: 0x200000, sha256: SHA }] },
    { board: 'ai-passport', chip: 'ESP32-C3', erase: 'never', write_limit: 0x356000, protect: [[0x356000, 0x35a000]],
      parts: [{ path: 'FoloToy-AI-Passport-tv.bin', offset: 0, size: 0x180000, sha256: SHA }] },
  ],
});

// ---- manifest ----

test('a good manifest is accepted and builds are picked by chip', () => {
  const m = core.parseManifest(manifest());
  assert.equal(core.pickBuild(m, 'ESP32-S3').board, 'xingzhi-cube-154');
  assert.equal(core.pickBuild(m, 'ESP32-C3').board, 'ai-passport');
  assert.equal(core.pickBuild(m, 'ESP32'), null);
});

test('a manifest cannot make a Passport erasable', () => {
  const m = manifest();
  m.builds[1].erase = 'first-install';
  throwsCode(() => core.parseManifest(m), 'manifest');
});

test('a manifest cannot write a Passport past its identity region', () => {
  const m = manifest();
  m.builds[1].parts[0].size = 0x356001;
  m.builds[1].write_limit = 8 << 20;           // the manifest's own limit is not trusted
  throwsCode(() => core.parseManifest(m), 'range');
});

test('a part placed on the identity region is refused', () => {
  const m = manifest();
  m.builds[1].parts.push({ path: 'x.bin', offset: 0x356000, size: 0x1000, sha256: SHA });
  throwsCode(() => core.parseManifest(m), 'range');
});

test('board and chip must belong together', () => {
  const m = manifest();
  m.builds[0].chip = 'ESP32-C3';
  throwsCode(() => core.parseManifest(m), 'manifest');
});

test('malformed entries are refused', () => {
  for (const change of [
    (m) => { m.builds[0].parts[0].path = '../index.html'; },
    (m) => { m.builds[0].parts[0].path = 'a/b.bin'; },
    (m) => { m.builds[0].parts[0].sha256 = 'XYZ'; },
    (m) => { m.builds[0].parts[0].offset = 0x800; },
    (m) => { m.builds[0].parts[0].size = 0; },
    (m) => { m.schema = 2; },
    (m) => { m.builds = []; },
    (m) => { m.builds.push(m.builds[0]); },
    (m) => { m.version = 'x'.repeat(40); },
  ]) {
    const m = manifest();
    change(m);
    assert.throws(() => core.parseManifest(m), core.FlasherError);
  }
});

// ---- ranges ----

test('range problems: limit, flash size, protect, overlap', () => {
  const s3 = core.BOARD_POLICY['ESP32-S3'];
  const c3 = core.BOARD_POLICY['ESP32-C3'];
  assert.deepEqual(core.rangeProblems([{ path: 'a', offset: 0, size: 0x200000 }], s3, 8 << 20), []);
  assert.equal(core.rangeProblems([{ path: 'a', offset: 0, size: 0x500000 }], s3, 4 << 20).length, 1);
  assert.equal(core.rangeProblems([{ path: 'a', offset: 0, size: 0x356000 }], c3, 8 << 20).length, 0);
  assert.ok(core.rangeProblems([{ path: 'a', offset: 0x350000, size: 0x10000 }], c3, 8 << 20).length >= 1);
  assert.equal(core.rangeProblems([{ path: 'a', offset: 0, size: 0x2000 }, { path: 'b', offset: 0x1000, size: 0x1000 }],
    s3, 8 << 20).length, 1);
});

// ---- the image and the device ----

const appImage = (project, version) => {
  const b = new Uint8Array(0x100).fill(0);
  b[0] = 0xe9;
  new DataView(b.buffer).setUint32(0x20, 0xabcd5432, true);
  b.set(Buffer.from(version), 0x30);
  b.set(Buffer.from(project), 0x50);
  return b;
};
const table = (entries) => {
  const b = new Uint8Array(0xc00).fill(0xff);
  const v = new DataView(b.buffer);
  entries.forEach(([type, subtype, offset, size, label], i) => {
    const at = i * 32;
    b[at] = 0xaa; b[at + 1] = 0x50; b[at + 2] = type; b[at + 3] = subtype;
    v.setUint32(at + 4, offset, true); v.setUint32(at + 8, size, true);
    b.set(Buffer.from(label.padEnd(16, '\0')), at + 12);
  });
  return b;
};
const tvTable = table([[1, 2, 0x9000, 0x6000, 'nvs'], [0, 0, 0x10000, 0x300000, 'factory'], [1, 2, 0x356000, 0x4000, 'cardid']]);

test('image chip id', () => {
  const b = new Uint8Array(32);
  b[0] = 0xe9; b[12] = 9;
  assert.equal(core.imageChipId(b), 9);
  b[12] = 5;
  assert.equal(core.imageChipId(b), 5);
  b[0] = 0;
  assert.equal(core.imageChipId(b), -1);
});

test('app description and partition table', () => {
  assert.deepEqual(core.parseAppDesc(appImage('FoloToy-AI-Passport', 'v1.2.3')),
    { version: 'v1.2.3', project: 'FoloToy-AI-Passport' });
  assert.equal(core.parseAppDesc(new Uint8Array(0x100)), null);
  const entries = core.parsePartitionTable(tvTable);
  assert.equal(entries.length, 3);
  assert.equal(entries[2].label, 'cardid');
  assert.equal(entries[2].offset, 0x356000);
});

test('what is installed', () => {
  assert.equal(core.classifyInstalled(tvTable, appImage('FoloToy-AI-Passport', 'v1')).kind, 'tv');
  // Xiaozhi AI: its own table, its app at 0x20000, not ours at 0x10000.
  const xiaozhi = table([[1, 2, 0x9000, 0x4000, 'nvs'], [0, 0x10, 0x20000, 0x3f0000, 'ota_0']]);
  assert.equal(core.classifyInstalled(xiaozhi, new Uint8Array(0x100).fill(0x12)).kind, 'other');
  assert.equal(core.classifyInstalled(xiaozhi, appImage('xiaozhi', '2.0.0')).kind, 'other');
  // Our project name but no factory slot at 0x10000 is not ours.
  assert.equal(core.classifyInstalled(xiaozhi, appImage('FoloToy-AI-Passport', 'v1')).kind, 'other');
  // Muse beside the TV: Muse's table sits at 0x10000.
  const museHead = table([[1, 2, 0x11000, 0xc000, 'nvs']]).subarray(0, 0x100);
  assert.equal(core.classifyInstalled(new Uint8Array(0xc00).fill(0xff), museHead).kind, 'muse-dual');
  assert.equal(core.classifyInstalled(new Uint8Array(0xc00).fill(0xff), new Uint8Array(0x100).fill(0xff)).kind, 'blank');
});

// ---- the partition table and the update plan ----

// gen_esp32part.py's binary table from a CSV of this project's shape: entries,
// then the EB EB entry with the MD5 of the entries, then FF to 0xC00.
const TYPES = { app: 0, data: 1 };
const SUBTYPES = { factory: 0, ota_0: 0x10, ota_1: 0x11, ota: 0, phy: 1, nvs: 2, coredump: 3, nvs_keys: 4, spiffs: 0x82 };
const tableFromRows = (rows, { md5 = true } = {}) => {
  const b = new Uint8Array(core.PARTITION_TABLE_MAX).fill(0xff);
  const v = new DataView(b.buffer);
  rows.forEach(([label, type, subtype, offset, size], i) => {
    const at = i * 32;
    b[at] = 0xaa; b[at + 1] = 0x50;
    b[at + 2] = typeof type === 'number' ? type : TYPES[type];
    b[at + 3] = typeof subtype === 'number' ? subtype : SUBTYPES[subtype];
    v.setUint32(at + 4, offset, true); v.setUint32(at + 8, size, true);
    b.fill(0, at + 12, at + 28);
    b.set(Buffer.from(label), at + 12);
    v.setUint32(at + 28, 0, true);
  });
  if (md5) {
    const at = rows.length * 32;
    b[at] = 0xeb; b[at + 1] = 0xeb;
    b.set(Buffer.from(createHash('md5').update(b.subarray(0, at)).digest()), at + 16);
  }
  return b;
};
const csvRows = (text) => text.split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('#'))
  .map((l) => l.split(',').map((x) => x.trim()))
  .map(([label, type, subtype, offset, size]) => [label, type, Number.isNaN(Number(subtype)) ? subtype : Number(subtype), Number(offset), Number(size)]);
const PROJECT_ROWS = csvRows(readFileSync(join(ROOT, 'partitions.csv'), 'utf8'));
const PROJECT_TABLE = tableFromRows(PROJECT_ROWS);
// The MD5 in the table of both v0.2.0-beta images (and of gen_esp32part.py's
// output for partitions.csv): the builder above makes the same bytes.
const RELEASE_TABLE_MD5 = '5f252f71170f9a17a171258600f4c012';

// A merged image the way `esptool merge-bin` lays it out: bootloader at 0,
// table at 0x8000, 0xFF padding (over nvs and phy_init), app at 0x10000.
const mergedImage = (length, chipId, tableBytes = PROJECT_TABLE) => {
  const b = new Uint8Array(length).fill(0xff);
  b.set(randomBytes(0x5000), 0);
  b[0] = 0xe9; b[12] = chipId;
  b.set(tableBytes, 0x8000);
  b.set(randomBytes(length - 0x10000), 0x10000);
  b.set(appImage('FoloToy-AI-Passport', 'v0.2.0-beta'), 0x10000);
  return b;
};
// The two v0.2.0-beta images' sizes.
const PASSPORT_LEN = 1750672;   // 0x1ab690
const XINGZHI_LEN = 1551632;    // 0x17ad10

test('the partition table is read strictly, MD5 included', () => {
  const read = core.readPartitionTable(PROJECT_TABLE);
  assert.ok(read);
  assert.equal(read.md5, RELEASE_TABLE_MD5);
  assert.deepEqual(read.entries.map((e) => [e.label, e.type, e.subtype, e.offset, e.size]), [
    ['nvs', 1, 2, 0x9000, 0x6000], ['phy_init', 1, 1, 0xf000, 0x1000],
    ['factory', 0, 0, 0x10000, 0x300000], ['cardid', 1, 2, 0x356000, 0x4000]]);
  // Without the MD5 entry (gen_esp32part --disable-md5sum): FF FF ends it.
  assert.equal(core.readPartitionTable(tableFromRows(PROJECT_ROWS, { md5: false })).entries.length, 4);
  // A flipped bit anywhere in the entries fails the MD5.
  const bad = PROJECT_TABLE.slice();
  bad[8] ^= 1;
  assert.equal(core.readPartitionTable(bad), null);
  // Erased flash, garbage, an unterminated table, nothing.
  assert.equal(core.readPartitionTable(new Uint8Array(0xc00).fill(0xff)), null);
  const junk = PROJECT_TABLE.slice();
  junk[0x80] = 0x12;
  assert.equal(core.readPartitionTable(junk), null);
  const full = new Uint8Array(0xc00);
  for (let at = 0; at < 0xc00; at += 32) full.set(PROJECT_TABLE.subarray(0, 32), at);
  assert.equal(core.readPartitionTable(full), null);
  assert.equal(core.readPartitionTable(null), null);
  // The lenient reader classifyInstalled() uses still agrees on the entries.
  assert.equal(core.parsePartitionTable(PROJECT_TABLE).length, 4);
});

test('an update writes only the bootloader, the table and the app, on both boards', () => {
  for (const [length, chip] of [[PASSPORT_LEN, 5], [XINGZHI_LEN, 9]]) {
    const image = mergedImage(length, chip);
    const plan = core.updatePlan(image, PROJECT_TABLE);
    assert.equal(plan.ok, true, plan.zh);
    assert.deepEqual(plan.regions, [[0, 0x9000], [0x10000, length]]);
    assert.deepEqual(plan.keep.map((k) => [k.label, k.start, k.end]),
      [['nvs', 0x9000, 0xf000], ['phy_init', 0xf000, 0x10000], ['cardid', 0x356000, 0x35a000]]);
    // Nothing the image carries is lost: every byte outside the regions is 0xFF.
    const written = new Uint8Array(length);
    for (const [s, e] of plan.regions) written.fill(1, s, e);
    for (let i = 0; i < length; i++) if (!written[i]) assert.equal(image[i], 0xff, `0x${i.toString(16)}`);
    // And no region touches nvs or phy_init.
    for (const [s, e] of plan.regions) assert.ok(e <= 0x9000 || s >= 0x10000);
  }
});

test('the regions stay inside the Passport policy', () => {
  const c3 = core.BOARD_POLICY['ESP32-C3'];
  const plan = core.updatePlan(mergedImage(PASSPORT_LEN, 5), PROJECT_TABLE);
  const parts = plan.regions.map(([s, e]) => ({ path: 'r', offset: s, size: e - s }));
  assert.deepEqual(core.rangeProblems(parts, c3, 8 << 20), []);
});

test('a different layout on the device means no update', () => {
  const image = mergedImage(PASSPORT_LEN, 5);
  const moved = tableFromRows([['nvs', 'data', 'nvs', 0x9000, 0x4000], ['phy_init', 'data', 'phy', 0xd000, 0x1000],
    ['factory', 'app', 'factory', 0x10000, 0x300000], ['cardid', 'data', 'nvs', 0x356000, 0x4000]]);
  assert.equal(core.updatePlan(image, moved).code, 'layout');
  // Renamed, or one more partition, is a different layout too.
  const renamed = tableFromRows(PROJECT_ROWS.map((r) => (r[0] === 'nvs' ? ['nvs2', ...r.slice(1)] : r)));
  assert.equal(core.updatePlan(image, renamed).code, 'layout');
  // The Xiaozhi AI firmware's table; erased flash; a read that failed.
  const xiaozhi = tableFromRows([['nvs', 'data', 'nvs', 0x9000, 0x4000], ['otadata', 'data', 'ota', 0xd000, 0x2000],
    ['phy_init', 'data', 'phy', 0xf000, 0x1000], ['ota_0', 'app', 'ota_0', 0x20000, 0x3f0000]]);
  assert.equal(core.updatePlan(image, xiaozhi).code, 'layout');
  assert.equal(core.updatePlan(image, new Uint8Array(0xc00).fill(0xff)).code, 'device-table');
  assert.equal(core.updatePlan(image, null).code, 'device-table');
});

test('an image the plan cannot split safely is refused', () => {
  // No table, or a damaged one.
  const noTable = mergedImage(PASSPORT_LEN, 5);
  noTable.fill(0xff, 0x8000, 0x9000);
  assert.equal(core.updatePlan(noTable, PROJECT_TABLE).code, 'image-table');
  // OTA boot selection: neither writing 0xFF over otadata nor keeping it is
  // safe without knowing which slot the device starts.
  const otaRows = [['nvs', 'data', 'nvs', 0x9000, 0x4000], ['otadata', 'data', 'ota', 0xd000, 0x2000],
    ['phy_init', 'data', 'phy', 0xf000, 0x1000], ['factory', 'app', 'factory', 0x10000, 0x300000]];
  const ota = mergedImage(PASSPORT_LEN, 5, tableFromRows(otaRows));
  assert.equal(core.updatePlan(ota, tableFromRows(otaRows)).code, 'otadata');
  // Content outside every partition (past the app's end at 0x310000).
  const gap = mergedImage(0x320000, 5);
  gap.fill(0xff, 0x1ab690);
  assert.equal(core.updatePlan(gap, PROJECT_TABLE).ok, true);
  gap[0x315000] = 0;
  assert.equal(core.updatePlan(gap, PROJECT_TABLE).code, 'gap');
  // Misaligned or overlapping partitions.
  const overlapRows = [['nvs', 'data', 'nvs', 0x9000, 0x8000], ['factory', 'app', 'factory', 0x10000, 0x300000]];
  assert.equal(core.updatePlan(mergedImage(PASSPORT_LEN, 5, tableFromRows(overlapRows)), tableFromRows(overlapRows)).code,
    'image-table');
});

test('a data partition the release fills is written; one it leaves blank is kept', () => {
  const rows = [...PROJECT_ROWS.slice(0, 3), ['assets', 'data', 'spiffs', 0x310000, 0x10000], PROJECT_ROWS[3]];
  const table = tableFromRows(rows);
  const image = mergedImage(0x320000, 5, table);
  image.fill(0xff, 0x1ab690, 0x310000);    // the app ends where the Passport's does
  let plan = core.updatePlan(image, table);
  assert.deepEqual(plan.regions, [[0, 0x9000], [0x10000, 0x320000]]);
  image.fill(0xff, 0x310000, 0x320000);
  plan = core.updatePlan(image, table);
  assert.deepEqual(plan.regions, [[0, 0x9000], [0x10000, 0x310000]]);
  assert.ok(plan.keep.some((k) => k.label === 'assets'));
});

const RELEASE_DIR = process.env.FLASHER_RELEASE_DIR;
if (RELEASE_DIR) {
  test('the published images split as expected', () => {
    for (const [name, length] of [['FoloToy-AI-Passport-tv.bin', PASSPORT_LEN], ['Xingzhi-Cube-154-tv-public.bin', XINGZHI_LEN]]) {
      const path = join(RELEASE_DIR, name);
      assert.ok(existsSync(path), path);
      const image = new Uint8Array(readFileSync(path));
      const table = image.subarray(0x8000, 0x8c00);
      assert.equal(core.readPartitionTable(table).md5, RELEASE_TABLE_MD5);
      const plan = core.updatePlan(image, table);
      assert.deepEqual(plan.regions, [[0, 0x9000], [0x10000, length]], name);
    }
  });
}

// ---- the decision ----

const OK_PLAN = { ok: true, regions: [[0, 0x9000], [0x10000, 0x20000]], keep: [] };
const NO_PLAN = { ok: false, code: 'layout', zh: 'x', en: 'x' };

test('a Passport is never erased, whatever is installed, chosen or planned', () => {
  const c3 = core.BOARD_POLICY['ESP32-C3'];
  for (const kind of ['tv', 'other', 'blank', 'unknown', 'muse-dual']) {
    for (const choice of ['update', 'clean', undefined]) {
      for (const plan of [OK_PLAN, NO_PLAN, null]) {
        assert.equal(core.decide(c3, kind, choice, plan).erase, false, `${kind}/${choice}`);
      }
    }
  }
});

test('a Passport running the TV is updated in place, settings kept', () => {
  const c3 = core.BOARD_POLICY['ESP32-C3'];
  assert.deepEqual(core.decide(c3, 'tv', 'update', OK_PLAN),
    { reason: 'update', erase: false, needsConsent: false, choosable: true, partial: true });
  // Clean reinstall: the whole image (blank settings) with consent.
  assert.deepEqual(core.decide(c3, 'tv', 'clean', OK_PLAN),
    { reason: 'clean', erase: false, needsConsent: true, choosable: true, partial: false });
  // The layout changed: only the whole image, with consent, no choice.
  assert.deepEqual(core.decide(c3, 'tv', 'update', NO_PLAN),
    { reason: 'mismatch', erase: false, needsConsent: true, choosable: false, partial: false });
  assert.equal(core.decide(c3, 'tv', 'update', null).reason, 'mismatch');
  // Unknown layout: whole image, and the user says it is not the Muse setup.
  assert.equal(core.decide(c3, 'unknown', 'update', null).needsConsent, true);
  assert.equal(core.decide(c3, 'unknown', 'update', null).partial, false);
  for (const kind of ['other', 'blank']) assert.equal(core.decide(c3, kind, 'update', null).partial, false);
});

test('a Xingzhi replacing other firmware is erased, with consent', () => {
  const s3 = core.BOARD_POLICY['ESP32-S3'];
  for (const kind of ['other', 'unknown']) {
    for (const choice of ['update', 'clean']) {
      assert.deepEqual(core.decide(s3, kind, choice, null),
        { reason: 'replace', erase: true, needsConsent: true, choosable: false, partial: false });
    }
  }
});

test('a Xingzhi already running the TV keeps its settings unless told otherwise', () => {
  const s3 = core.BOARD_POLICY['ESP32-S3'];
  const update = core.decide(s3, 'tv', 'update', OK_PLAN);
  assert.deepEqual(update, { reason: 'update', erase: false, needsConsent: false, choosable: true, partial: true });
  assert.deepEqual(core.decide(s3, 'tv', 'clean', OK_PLAN),
    { reason: 'clean', erase: true, needsConsent: true, choosable: true, partial: false });
  // A changed layout falls back to the clean path, erase and consent included.
  assert.deepEqual(core.decide(s3, 'tv', 'update', NO_PLAN),
    { reason: 'mismatch', erase: true, needsConsent: true, choosable: false, partial: false });
  // Erased flash: the whole image, no erase, nothing to agree to.
  assert.deepEqual(core.decide(s3, 'blank', 'update', null),
    { reason: 'fresh', erase: false, needsConsent: false, choosable: false, partial: false });
});

test('write regions follow the decision', () => {
  const s3 = core.BOARD_POLICY['ESP32-S3'];
  assert.deepEqual(core.writeRegions(core.decide(s3, 'tv', 'update', OK_PLAN), OK_PLAN, 0x20000), OK_PLAN.regions);
  assert.deepEqual(core.writeRegions(core.decide(s3, 'tv', 'clean', OK_PLAN), OK_PLAN, 0x20000), [[0, 0x20000]]);
  assert.deepEqual(core.writeRegions(core.decide(s3, 'tv', 'update', NO_PLAN), NO_PLAN, 0x20000), [[0, 0x20000]]);
});

// ---- sizes and checksums ----

test('flash size from the JEDEC id', () => {
  assert.equal(core.flashBytesFromId(0x1740ef), 8 << 20);   // ef4017, the Xingzhi's
  assert.equal(core.flashBytesFromId(0x1640c8), 4 << 20);
  assert.equal(core.flashBytesFromId(0x1840ef), 16 << 20);
  assert.equal(core.flashBytesFromId(0x0000ff), 0);
});

test('md5 matches node for every padding boundary and a large input', () => {
  for (const n of [0, 1, 3, 55, 56, 57, 63, 64, 65, 119, 120, 1000, 100003]) {
    const data = randomBytes(n);
    assert.equal(core.md5Hex(new Uint8Array(data)), createHash('md5').update(data).digest('hex'), `n=${n}`);
  }
  assert.equal(core.md5Hex(new TextEncoder().encode('abc')), '900150983cd24fb0d6963f7d28e17f72');
});

test('sha256 and digest checks', async () => {
  const data = new TextEncoder().encode('firmware');
  const actual = await core.sha256Hex(data);
  assert.equal(actual, createHash('sha256').update(data).digest('hex'));
  const part = { path: 'f.bin', sha256: actual };
  core.checkDigest(part, actual, core.parseSha256Sums(`${actual}  f.bin\n`));
  core.checkDigest(part, actual, new Map());
  throwsCode(() => core.checkDigest(part, 'b'.repeat(64), null), 'sha256');
  throwsCode(() => core.checkDigest(part, actual, core.parseSha256Sums(`${'c'.repeat(64)}  f.bin\n`)), 'sha256');
  throwsCode(() => core.checkDigest(part, actual, core.parseSha256Sums(`${actual}  other.bin\n`)), 'sha256');
});

test('SHA256SUMS parsing', () => {
  const sums = core.parseSha256Sums(`${'A'.repeat(64)}  a.bin\n${'b'.repeat(64)} *b.bin\r\nnot a line\n`);
  assert.equal(sums.get('a.bin'), 'a'.repeat(64));
  assert.equal(sums.get('b.bin'), 'b'.repeat(64));
  assert.equal(sums.size, 2);
});

// ---- browser ----

test('browser support', () => {
  const desktop = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0 Safari/537.36';
  assert.deepEqual(core.browserSupport({ userAgent: desktop, serial: {} }, true), { ok: true, reason: '' });
  assert.equal(core.browserSupport({ userAgent: desktop, serial: {} }, false).reason, 'insecure');
  assert.equal(core.browserSupport({ userAgent: desktop }, true).reason, 'no-serial');
  assert.equal(core.browserSupport({ userAgent: 'Mozilla/5.0 (Linux; Android 14) Chrome/141.0 Mobile', serial: {} }, true).reason, 'mobile');
  assert.equal(core.browserSupport({ userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari' }, true).reason, 'mobile');
});

// ---- the household server ----

test('the sound setup link comes only from a well-formed server answer', () => {
  const good = { ok: true, pending: false, device_address: '192.168.1.118', wifi_url: 'http://192.168.1.118:8080/tv/wifi' };
  assert.deepEqual(core.serverWifiLink(good), { url: good.wifi_url, address: '192.168.1.118' });
  assert.deepEqual(core.serverWifiLink({ ...good, device_address: 'nas.local', wifi_url: 'http://nas.local/tv/wifi' }),
    { url: 'http://nas.local/tv/wifi', address: 'nas.local' });
  // The static site, a cloud relay, an old server without wifi_url, no address yet: the generic text stays.
  for (const bad of [null, undefined, 'x', {}, { ok: false }, { ...good, ok: 'yes' }, { ...good, wifi_url: undefined },
    { ...good, wifi_url: '' }, { ...good, device_address: '' },
    { ...good, wifi_url: 'https://192.168.1.118:8080/tv/wifi' },          // only the LAN page over plain HTTP
    { ...good, wifi_url: 'http://192.168.1.118:8080/tv/wifi?x=1' },
    { ...good, wifi_url: 'javascript:alert(1)//192.168.1.118' },
    { ...good, wifi_url: 'http://evil.example:8080/tv/wifi' },            // must be the device address
    { ...good, wifi_url: 'http://192.168.1.118:99999/tv/wifi' },
    { ...good, device_address: '<b>', wifi_url: 'http://<b>/tv/wifi' }]) {
    assert.equal(core.serverWifiLink(bad), null, JSON.stringify(bad));
  }
  assert.equal(core.SETUP_INFO_PATH, '/tv/api/setup/info');
  assert.equal(core.WIFI_QR_PATH, '/tv/api/setup/wifi-qr.svg');
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
  } catch (error) {
    failed++;
    console.error(`FAIL ${name}\n  ${error && error.stack ? error.stack.split('\n').slice(0, 3).join('\n  ') : error}`);
  }
}
console.log(`flasher-core: ${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
