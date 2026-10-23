// Tests for packaging/flash/flasher-core.js, the web flasher's decisions.
// Run by tests/test_flasher.py under Node (18+); also runnable on its own:
//   node tests/flasher_core_test.mjs            run the tests
//   node tests/flasher_core_test.mjs --policy   print BOARD_POLICY as JSON
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import * as core from '../packaging/flash/flasher-core.js';

if (process.argv.includes('--policy')) {
  console.log(JSON.stringify(core.BOARD_POLICY));
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

// ---- erase ----

test('a Passport is never erased, whatever is installed or chosen', () => {
  const c3 = core.BOARD_POLICY['ESP32-C3'];
  for (const kind of ['tv', 'other', 'blank', 'unknown', 'muse-dual']) {
    for (const choice of ['update', 'clean', undefined]) {
      const d = core.eraseDecision(c3, kind, choice);
      assert.equal(d.erase, false, `${kind}/${choice}`);
      assert.equal(d.choosable, false);
    }
  }
});

test('a Xingzhi replacing other firmware is erased, with consent', () => {
  const s3 = core.BOARD_POLICY['ESP32-S3'];
  for (const kind of ['other', 'unknown']) {
    for (const choice of ['update', 'clean']) {
      assert.deepEqual(core.eraseDecision(s3, kind, choice),
        { erase: true, needsConsent: true, choosable: false, reason: 'replace' });
    }
  }
});

test('a Xingzhi already running the TV keeps its settings unless told otherwise', () => {
  const s3 = core.BOARD_POLICY['ESP32-S3'];
  for (const kind of ['tv', 'blank']) {
    assert.equal(core.eraseDecision(s3, kind, 'update').erase, false);
    assert.equal(core.eraseDecision(s3, kind, 'update').needsConsent, false);
    assert.equal(core.eraseDecision(s3, kind, 'clean').erase, true);
    assert.equal(core.eraseDecision(s3, kind, 'clean').needsConsent, true);
  }
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
