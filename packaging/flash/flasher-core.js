// The web flasher's decisions, kept apart from the page so they can be tested
// without a browser or a device (tests/test_flasher.py runs
// tests/flasher_core_test.mjs under Node).
//
// Nothing in here touches the DOM, the serial port or the network. The page
// asks these functions what to do -- which image belongs to the chip it found,
// which ranges of it an update writes (the settings partitions never), whether
// the write stays inside the allowed range, whether the chip may be erased --
// and does it.
//
// BOARD_POLICY repeats tools/firmware_manifest.py's BOARDS on purpose: the page
// does not take the manifest's word for what is safe to write. A manifest that
// asked to erase a Passport, or to write past its identity region, is refused
// here even though it came from the same site. tests/test_flasher.py fails when
// the two tables disagree.

export const MANIFEST_SCHEMA = 1;
export const APP_OFFSET = 0x10000;
export const PARTITION_TABLE_OFFSET = 0x8000;
export const APP_PROJECT_NAME = 'FoloToy-AI-Passport';
const MB = 1024 * 1024;

export const BOARD_POLICY = Object.freeze({
  'ESP32-S3': Object.freeze({
    board: 'xingzhi-cube-154',
    chipId: 9,
    flashBytes: 8 * MB,
    erase: 'first-install',
    writeLimit: 8 * MB,
    protect: Object.freeze([]),
  }),
  'ESP32-C3': Object.freeze({
    board: 'ai-passport',
    chipId: 5,
    flashBytes: 8 * MB,
    erase: 'never',
    writeLimit: 0x356000,
    // The factory identity (cardid). Nothing can recreate it.
    protect: Object.freeze([Object.freeze([0x356000, 0x35a000])]),
  }),
});

export class FlasherError extends Error {
  constructor(code, zh, en) {
    super(zh);
    this.code = code;
    this.zh = zh;
    this.en = en || zh;
  }
}

const fail = (code, zh, en) => { throw new FlasherError(code, zh, en); };
export const hex = (n) => '0x' + n.toString(16);

const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const VERSION = /^[ -~]{1,32}$/;

// ---- manifest ------------------------------------------------------------

// Checks a parsed manifest.json and returns it. Every rule is re-applied from
// BOARD_POLICY, so the page's safety does not depend on the file being right.
export function parseManifest(data) {
  if (!data || typeof data !== 'object' || data.schema !== MANIFEST_SCHEMA) {
    fail('manifest', '固件清单的格式不认识，请刷新页面；还不行就是网站上的文件不完整。',
      'The firmware manifest has an unknown format. Reload the page; if it persists the site is incomplete.');
  }
  if (typeof data.version !== 'string' || !VERSION.test(data.version)) {
    fail('manifest', '固件清单里的版本号不对。', 'The manifest version is invalid.');
  }
  if (!Array.isArray(data.builds) || data.builds.length === 0) {
    fail('manifest', '固件清单里没有固件。', 'The manifest lists no firmware.');
  }
  const seen = new Set();
  for (const build of data.builds) {
    const policy = BOARD_POLICY[build && build.chip];
    if (!policy || policy.board !== build.board) {
      fail('manifest', `固件清单里有不认识的设备：${build && build.board}`,
        `The manifest names an unknown board: ${build && build.board}`);
    }
    if (seen.has(build.chip)) fail('manifest', '固件清单里同一种芯片出现了两次。', 'The manifest lists one chip twice.');
    seen.add(build.chip);
    if (build.erase !== policy.erase) {
      fail('manifest', `固件清单要求的擦除方式和 ${build.board} 的规矩不符，已拒绝。`,
        `The manifest's erase rule does not match ${build.board}'s; refused.`);
    }
    if (!Array.isArray(build.parts) || build.parts.length === 0) {
      fail('manifest', '固件清单里缺文件。', 'The manifest lists no files.');
    }
    for (const part of build.parts) {
      if (!part || typeof part.path !== 'string' || !SAFE_NAME.test(part.path)
          || !Number.isInteger(part.offset) || !Number.isInteger(part.size)
          || part.offset < 0 || part.size <= 0 || part.offset % 0x1000 !== 0
          || typeof part.sha256 !== 'string' || !SHA256.test(part.sha256)) {
        fail('manifest', '固件清单里有一项写得不对，已拒绝。', 'A manifest entry is malformed; refused.');
      }
    }
    const problems = rangeProblems(build.parts, policy, policy.flashBytes);
    if (problems.length) fail('range', problems[0].zh, problems[0].en);
  }
  return data;
}

export function pickBuild(manifest, chipName) {
  return manifest.builds.find((build) => build.chip === chipName) || null;
}

// ---- ranges --------------------------------------------------------------

// Every way the parts could land where they must not: past the board's limit,
// past the chip's flash, on a protected range, or on top of each other.
export function rangeProblems(parts, policy, flashBytes) {
  const problems = [];
  const limit = Math.min(policy.writeLimit, flashBytes || policy.flashBytes);
  const spans = parts.map((p) => [p.offset, p.offset + p.size, p.path]).sort((a, b) => a[0] - b[0]);
  for (const [start, end, path] of spans) {
    if (start < 0 || end > limit) {
      problems.push({
        zh: `${path} 会写到 ${hex(end)}，超过了允许的 ${hex(limit)}，已停止。`,
        en: `${path} would reach ${hex(end)}, past the allowed ${hex(limit)}; stopped.`,
      });
    }
    for (const [p0, p1] of policy.protect) {
      if (start < p1 && p0 < end) {
        problems.push({
          zh: `${path} 会碰到 ${hex(p0)} 起的出厂身份信息，已停止。`,
          en: `${path} would touch the factory identity at ${hex(p0)}; stopped.`,
        });
      }
    }
  }
  for (let i = 1; i < spans.length; i++) {
    if (spans[i - 1][1] > spans[i][0]) {
      problems.push({ zh: '固件的两部分互相重叠，已停止。', en: 'Two firmware parts overlap; stopped.' });
    }
  }
  return problems;
}

// ---- what the image and the chip say about themselves ---------------------

// The chip id an ESP-IDF image header was built for (5 = ESP32-C3,
// 9 = ESP32-S3), or -1 when the bytes are not an image.
export function imageChipId(bytes) {
  if (!bytes || bytes.length < 16 || bytes[0] !== 0xe9) return -1;
  return bytes[12] | (bytes[13] << 8);
}

const cstr = (bytes) => {
  let end = bytes.indexOf(0);
  if (end < 0) end = bytes.length;
  return String.fromCharCode(...bytes.subarray(0, end));
};

// esp_app_desc_t of the application whose image starts at bytes[0]: 24 bytes
// of image header, 8 of segment header, then the description.
export function parseAppDesc(bytes) {
  if (!bytes || bytes.length < 0x70 || bytes[0] !== 0xe9) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint32(0x20, true) !== 0xabcd5432) return null;
  return { version: cstr(bytes.subarray(0x30, 0x50)), project: cstr(bytes.subarray(0x50, 0x70)) };
}

// An ESP-IDF partition table: 32-byte entries starting AA 50, ended by FF FF or
// the EB EB MD5 marker. Returns null when the bytes do not start with one.
export function parsePartitionTable(bytes) {
  if (!bytes || bytes.length < 32 || bytes[0] !== 0xaa || bytes[1] !== 0x50) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const entries = [];
  for (let at = 0; at + 32 <= bytes.length; at += 32) {
    if (bytes[at] !== 0xaa || bytes[at + 1] !== 0x50) break;
    entries.push({
      type: bytes[at + 2], subtype: bytes[at + 3],
      offset: view.getUint32(at + 4, true), size: view.getUint32(at + 8, true),
      label: cstr(bytes.subarray(at + 12, at + 28)),
    });
  }
  return entries;
}

// What is on the device now, from three small reads: the partition table at
// 0x8000, the first bytes at 0x10000 and nothing else.
//
//   tv         this project's firmware (factory app at 0x10000 named
//              FoloToy-AI-Passport): an update writes only the code
//              (updatePlan) and keeps settings and binding
//   muse-dual  the Passport carrying Muse in ota_0 with Muse's own table at
//              0x10000 (docs/boards/passport-muse-dual.md): writing a full
//              image would delete Muse and its pairing
//   blank      erased flash
//   other      something else (the Xiaozhi AI firmware, a factory image...)
export function classifyInstalled(table8000, head10000) {
  const table = parsePartitionTable(table8000);
  if (head10000 && head10000[0] === 0xaa && head10000[1] === 0x50) {
    return { kind: 'muse-dual', app: null };
  }
  const app = parseAppDesc(head10000);
  const factory = table && table.find((e) => e.type === 0 && e.offset === APP_OFFSET);
  if (app && app.project === APP_PROJECT_NAME && factory) return { kind: 'tv', app };
  const blank = (b) => b && b.length > 0 && b.every((x) => x === 0xff);
  if (blank(table8000) && blank(head10000)) return { kind: 'blank', app: null };
  return { kind: 'other', app };
}

// ---- the partition table and the update plan ------------------------------

export const PARTITION_TABLE_MAX = 0xc00;
const SECTOR = 0x1000;

// The strict reading of an ESP-IDF partition table (gen_esp32part.py's binary
// format): 32-byte entries starting AA 50 (type, subtype, offset, size, a
// 16-byte label, flags), then either the EB EB entry -- 14 bytes of FF and the
// MD5 of every entry before it -- or FF FF. Anything else, an MD5 that does
// not match, or no entries at all: null. parsePartitionTable() above is the
// lenient reading classifyInstalled() uses to recognise a layout.
export function readPartitionTable(bytes) {
  if (!bytes || bytes.length < 32) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const entries = [];
  const end = Math.min(bytes.length, PARTITION_TABLE_MAX);
  for (let at = 0; at + 32 <= end; at += 32) {
    const m0 = bytes[at], m1 = bytes[at + 1];
    if (m0 === 0xaa && m1 === 0x50) {
      entries.push({
        type: bytes[at + 2], subtype: bytes[at + 3],
        offset: view.getUint32(at + 4, true), size: view.getUint32(at + 8, true),
        label: cstr(bytes.subarray(at + 12, at + 28)), flags: view.getUint32(at + 28, true),
      });
      continue;
    }
    if (!entries.length) return null;
    if (m0 === 0xeb && m1 === 0xeb) {
      const stored = Array.from(bytes.subarray(at + 16, at + 32), (b) => b.toString(16).padStart(2, '0')).join('');
      return md5Hex(bytes.subarray(0, at)) === stored ? { entries, md5: stored } : null;
    }
    if (m0 === 0xff && m1 === 0xff) return { entries, md5: '' };
    return null;
  }
  return null;
}

// The same layout: every entry equal, in the same order.
export function sameLayout(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  return a.every((e, i) => ['type', 'subtype', 'offset', 'size', 'label', 'flags'].every((k) => e[k] === b[i][k]));
}

// Data partitions whose content belongs to the device, never to a release:
// otadata (0) is the boot selection, phy (1) the radio calibration, nvs (2)
// Wi-Fi, server address, the device's secret and every setting, coredump (3)
// and nvs_keys (4) likewise.
const DEVICE_DATA = new Map([[0x00, 'otadata'], [0x01, 'phy'], [0x02, 'nvs'], [0x03, 'coredump'], [0x04, 'nvs_keys']]);

const blankRange = (bytes, start, end) => {
  for (let i = start; i < end; i++) if (bytes[i] !== 0xff) return false;
  return true;
};

// What an update writes from a merged release image (bootloader at 0, table
// at 0x8000, app at 0x10000, 0xFF padding between) so that the device keeps
// its settings: the bootloader and the partition table, [0, first partition),
// and each partition the release fills (the app, up to the image's end). Every
// device-data partition is left alone, and so is any partition the image
// leaves blank.
//
// `image` is the merged image; `deviceTable` the 0xC00 bytes read from the
// device at 0x8000. Returns { ok: true, regions: [[start, end], ...], keep:
// [{ label, start, end }] }, or { ok: false, code, zh, en } when an update
// cannot keep the settings safely and the full path has to be taken:
//
//   image-table  the image's own table is missing, damaged or odd
//   device-table the device's table cannot be read
//   layout       the two tables differ: the settings may sit somewhere else
//                in the new layout, and the old ones would be read as garbage
//   otadata      the layout has OTA boot selection. Writing the image's 0xFF
//                there resets the selection (the bootloader then starts ota_0,
//                whatever ran before); keeping it may go on starting an older
//                slot. Neither is safe without knowing the device, and no
//                layout of this project has one, so such a layout is refused
//   gap          the image has content outside any partition it would write
export function updatePlan(image, deviceTable) {
  const no = (code, zh, en) => ({ ok: false, code, zh, en });
  const table = image && image.length > PARTITION_TABLE_OFFSET
    ? readPartitionTable(image.subarray(PARTITION_TABLE_OFFSET, PARTITION_TABLE_OFFSET + PARTITION_TABLE_MAX)) : null;
  const badImage = no('image-table', '新固件里的分区表读不出来，没法只更新固件。',
    'The new image has no readable partition table, so an update that keeps the settings is not possible.');
  if (!table) return badImage;
  const sorted = [...table.entries].sort((a, b) => a.offset - b.offset);
  let previousEnd = PARTITION_TABLE_OFFSET + SECTOR;
  for (const e of sorted) {
    if (e.offset % SECTOR || e.size <= 0 || e.size % SECTOR || e.offset < previousEnd) return badImage;
    previousEnd = e.offset + e.size;
  }
  if (!sorted.some((e) => e.type === 0 && e.offset === APP_OFFSET)) return badImage;
  if (sorted.some((e) => e.type === 1 && e.subtype === 0)) {
    return no('otadata', '新固件的分区里有 OTA 启动选择（otadata），保留设置的更新不支持这种布局。',
      'The new layout has OTA boot selection (otadata); an update that keeps the settings does not support it.');
  }
  const device = readPartitionTable(deviceTable);
  if (!device) {
    return no('device-table', '设备里的分区表读不出来，没法确认设置区的位置。',
      'The partition table on the device cannot be read, so the settings area cannot be located.');
  }
  if (!sameLayout(table.entries, device.entries)) {
    return no('layout', '这次的固件改了分区表（设置区的位置或大小变了），旧的设置没法原样保留。',
      'This firmware changes the partition table (the settings area moved or changed size); the old settings cannot be kept as they are.');
  }
  const regions = [[0, sorted[0].offset]];
  const keep = [];
  for (const e of sorted) {
    const start = e.offset;
    const end = e.offset + e.size;
    const inImage = Math.min(end, image.length);
    const deviceOwned = e.type !== 0 && (DEVICE_DATA.has(e.subtype) && e.type === 1
      || start >= image.length || blankRange(image, start, inImage));
    if (deviceOwned) keep.push({ label: e.label, start, end });
    else regions.push([start, inImage]);
  }
  // Whatever the plan writes nothing to must be merge-bin's 0xFF padding, or
  // one of the device's own partitions.
  const covered = [...regions, ...keep.map((k) => [k.start, k.end])].sort((a, b) => a[0] - b[0]);
  let at = 0;
  for (const [start, end] of covered) {
    if (start > at && !blankRange(image, at, Math.min(start, image.length))) {
      return no('gap', '新固件在分区之外还有内容，没法只更新固件。',
        'The new image has content outside its partitions; an update that keeps the settings is not possible.');
    }
    at = Math.max(at, end);
  }
  if (at < image.length && !blankRange(image, at, image.length)) {
    return no('gap', '新固件在分区之外还有内容，没法只更新固件。',
      'The new image has content outside its partitions; an update that keeps the settings is not possible.');
  }
  const merged = [];
  for (const [start, end] of regions.filter(([s, e]) => e > s).sort((a, b) => a[0] - b[0])) {
    const last = merged[merged.length - 1];
    if (last && last[1] === start) last[1] = end;
    else merged.push([start, end]);
  }
  return { ok: true, regions: merged, keep };
}

// ---- the decision --------------------------------------------------------

// What this flash does: write only the update regions (partial) or the whole
// image, erase the chip first or not, and whether the user has to agree.
// `choice` is what the user picked when there is a choice: 'update' (keep
// settings) or 'clean' (start over). `plan` is updatePlan()'s answer for a
// device already running this firmware.
//
// - never (Passport): the chip is never erased, whatever was asked. A clean
//   reinstall writes the whole image, which writes 0xFF over the settings.
// - first-install (Xingzhi) replacing other firmware or unknown: erase is
//   required, because the previous firmware's settings sit inside this
//   firmware's settings area; the user must agree, since it deletes the
//   Xiaozhi AI firmware.
// - this firmware already there: an update writes only the code and keeps the
//   settings; when the plan says it cannot (the layout changed), the clean
//   path with the user's agreement is the only one offered.
// - blank flash: the whole image, nothing to agree to.
export function decide(policy, installedKind, choice, plan) {
  const passport = policy.erase === 'never';
  const full = (reason, erase, needsConsent, choosable = false) =>
    ({ reason, erase: passport ? false : erase, needsConsent, choosable, partial: false });
  if (installedKind === 'blank') return full('fresh', false, false);
  if (installedKind === 'tv') {
    if (choice === 'clean') return full('clean', true, true, true);
    if (plan && plan.ok) return { reason: 'update', erase: false, needsConsent: false, choosable: true, partial: true };
    return full('mismatch', true, true);
  }
  if (passport) return installedKind === 'other' ? full('fresh', false, false) : full('unknown', false, true);
  return full('replace', true, true);
}

// The ranges a flash writes: the update plan's, or the whole image.
export function writeRegions(decision, plan, imageLength) {
  return decision.partial ? plan.regions.map(([s, e]) => [s, e]) : [[0, imageLength]];
}

// ---- flash size ----------------------------------------------------------

// Bytes of flash from the JEDEC id's capacity byte, or 0 when unknown.
export function flashBytesFromId(flashId) {
  const code = (flashId >> 16) & 0xff;
  if (code >= 0x12 && code <= 0x1c) return 2 ** (code - 0x12) * 256 * 1024;
  if (code >= 0x32 && code <= 0x3c) return 2 ** (code - 0x32) * 256 * 1024;
  if (code >= 0x20 && code <= 0x22) return 2 ** (code - 0x20) * 64 * MB;
  return 0;
}

// ---- checksums -----------------------------------------------------------

export function parseSha256Sums(text) {
  const sums = new Map();
  for (const line of String(text).split(/\r?\n/)) {
    const match = /^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$/.exec(line);
    if (match) sums.set(match[2], match[1].toLowerCase());
  }
  return sums;
}

export async function sha256Hex(data) {
  const digest = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

// The file must match the manifest and, when SHA256SUMS is there, that too.
export function checkDigest(part, actual, sums) {
  if (actual !== part.sha256) {
    fail('sha256', `${part.path} 的校验值不对，可能没下载完整，或文件被换过。已停止，设备没被改动。`,
      `${part.path} does not match its checksum (incomplete download or replaced file). Stopped; the device was not touched.`);
  }
  if (sums && sums.size && sums.get(part.path) !== actual) {
    fail('sha256', `${part.path} 和 SHA256SUMS 对不上，已停止，设备没被改动。`,
      `${part.path} disagrees with SHA256SUMS. Stopped; the device was not touched.`);
  }
}

// MD5, for esptool-js's read-back check (calculateMD5Hash): after the write the
// stub hashes what is in flash and the two must agree. Web Crypto has no MD5.
const MD5_S = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
const MD5_K = Array.from({ length: 64 }, (_, i) => Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32) >>> 0);

export function md5Hex(input) {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  const length = bytes.length;
  const padded = new Uint8Array(((length + 8) >> 6 << 6) + 64);
  padded.set(bytes);
  padded[length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, (length * 8) >>> 0, true);
  view.setUint32(padded.length - 4, Math.floor(length / 0x20000000), true);
  let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
  const m = new Uint32Array(16);
  for (let block = 0; block < padded.length; block += 64) {
    for (let i = 0; i < 16; i++) m[i] = view.getUint32(block + i * 4, true);
    let a = a0, b = b0, c = c0, d = d0;
    for (let i = 0; i < 64; i++) {
      let f, g;
      if (i < 16) { f = (b & c) | (~b & d); g = i; }
      else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
      else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) % 16; }
      else { f = c ^ (b | ~d); g = (7 * i) % 16; }
      const s = MD5_S[(i >> 4) * 4 + (i % 4)];
      const sum = (a + f + MD5_K[i] + m[g]) >>> 0;
      a = d; d = c; c = b;
      b = (b + ((sum << s) | (sum >>> (32 - s)))) >>> 0;
    }
    a0 = (a0 + a) >>> 0; b0 = (b0 + b) >>> 0; c0 = (c0 + c) >>> 0; d0 = (d0 + d) >>> 0;
  }
  const out = new DataView(new ArrayBuffer(16));
  [a0, b0, c0, d0].forEach((word, i) => out.setUint32(i * 4, word, true));
  return Array.from(new Uint8Array(out.buffer), (b) => b.toString(16).padStart(2, '0')).join('');
}

// ---- the browser ---------------------------------------------------------

// Whether this browser can flash at all, and if not, why -- in words a reader
// can act on. `nav` and `secure` are passed in so the test can play every case.
export function browserSupport(nav, secure) {
  const ua = (nav && nav.userAgent) || '';
  const mobile = /Android|iPhone|iPad|iPod|Mobile/i.test(ua)
    || (nav && nav.userAgentData && nav.userAgentData.mobile === true);
  if (mobile) return { ok: false, reason: 'mobile' };
  if (!nav || !('serial' in nav)) return { ok: false, reason: 'no-serial' };
  if (!secure) return { ok: false, reason: 'insecure' };
  return { ok: true, reason: '' };
}

// ---- the household server ------------------------------------------------

// When the page is served by the household server or the PC client (at
// /flash/), /tv/api/setup/info names the address the devices and phones use
// and the sound setup page's link (server/relay/setup.py). On the static site
// there is no such API, and anything that does not look exactly like that
// answer keeps the page's generic text: null.
export const SETUP_INFO_PATH = '/tv/api/setup/info';
export const WIFI_QR_PATH = '/tv/api/setup/wifi-qr.svg';
const HOST = /^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$/;

export function serverWifiLink(info) {
  if (!info || info.ok !== true || typeof info.wifi_url !== 'string' || typeof info.device_address !== 'string') {
    return null;
  }
  const address = info.device_address;
  if (!HOST.test(address)) return null;
  const match = /^http:\/\/([A-Za-z0-9.-]+)(?::(\d{1,5}))?\/tv\/wifi$/.exec(info.wifi_url);
  if (!match || match[1] !== address) return null;
  if (match[2] !== undefined && !(Number(match[2]) >= 1 && Number(match[2]) <= 65535)) return null;
  return { url: info.wifi_url, address };
}
