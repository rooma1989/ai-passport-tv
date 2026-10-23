// The web flasher page: connect, look, confirm, write, verify.
//
// Every decision about what may be written where comes from flasher-core.js
// (tested under Node); this file only drives the page and esptool-js.
// esptool-js is vendored at an exact version (vendor/esptool-js-0.6.1/, with its
// licence and SOURCE.txt) so the page makes no third-party request and works
// from the household server's localhost copy without internet access.

import { ESPLoader, Transport } from './vendor/esptool-js-0.6.1/bundle.js';
import * as core from './flasher-core.js';

const $ = (id) => document.getElementById(id);
const FIRMWARE_DIR = 'firmware/';
const BAUD = 115200;          // both boards use the chip's built-in USB; the rate is not the limit
const READ_TIMEOUT_MS = 15000;
const BOARD_DOC = 'https://github.com/rooma1989/ai-passport-tv/blob/main/docs/boards/passport-muse-dual.zh_CN.md';

const state = {
  manifest: null, sums: null, transport: null, loader: null,
  chip: '', policy: null, build: null, flashBytes: 0, mac: '',
  installed: { kind: 'unknown', app: null }, busy: false, done: false,
  deviceTable: null,    // the 0xC00 bytes at 0x8000, as read at connect
  files: null,          // [{ part, data }]: the build's files, downloaded and checked at connect
  plan: null,           // core.updatePlan() for a device already running this firmware
  doneReason: '',       // what the last successful flash did (core.decide().reason)
  wifi: null,           // { url, address } from the household server; null on the static site
};

// ---- language ------------------------------------------------------------

const lang = () => (document.documentElement.lang === 'en' ? 'en' : 'zh');
const t = (zh, en) => (lang() === 'en' ? en : zh);

function setLang(next) {
  document.documentElement.lang = next === 'en' ? 'en' : 'zh-CN';
  $('langToggle').textContent = next === 'en' ? '中文' : 'English';
  document.title = t('小智电视 · 刷机', 'Xiaozhi TV · Flash');
  try { localStorage.setItem('flash-lang', next); } catch (_) { /* private mode: fine */ }
  renderAll();
}

// ---- log: short lines for the reader; esptool's own chatter kept aside ------

const entries = [];
const esptoolLines = [];
const log = (zh, en) => { entries.push([zh, en || zh]); renderLog(); };
function renderLog() {
  const box = $('log');
  box.textContent = entries.map(([zh, en]) => t(zh, en)).join('\n');
  box.scrollTop = box.scrollHeight;
}
// esptool writes a line for every block; the reader sees the progress bar
// instead. The last lines are kept and shown only when something fails.
const terminal = {
  clean: () => {},
  writeLine: (line) => { esptoolLines.push(String(line)); if (esptoolLines.length > 40) esptoolLines.shift(); },
  write: () => {},
};

function stage(zh, en, fraction) {
  $('progress').classList.remove('hidden');
  $('stage').textContent = t(zh, en);
  $('stage').dataset.zh = zh;
  $('stage').dataset.en = en;
  const meter = $('meter');
  if (fraction === null || fraction === undefined) {
    meter.classList.add('busy');
    $('fill').style.width = '';
  } else {
    meter.classList.remove('busy');
    $('fill').style.width = Math.round(Math.max(0, Math.min(1, fraction)) * 100) + '%';
  }
}

const withTimeout = (promise, ms) => Promise.race([
  promise,
  new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms)),
]);

const kib = (n) => (n / 1024 / 1024).toFixed(2) + ' MB';

// ---- page set-up ---------------------------------------------------------

async function loadManifest() {
  const response = await fetch(FIRMWARE_DIR + 'manifest.json', { cache: 'no-store' });
  if (!response.ok) {
    throw new core.FlasherError('manifest', `取不到固件清单（HTTP ${response.status}）。这个网站上还没有放固件，或者放错了位置。`,
      `Could not load the firmware manifest (HTTP ${response.status}); the firmware is missing from this site.`);
  }
  state.manifest = core.parseManifest(await response.json());
  const sums = await fetch(FIRMWARE_DIR + 'SHA256SUMS', { cache: 'no-store' });
  if (!sums.ok) {
    throw new core.FlasherError('manifest', '取不到 SHA256SUMS，无法核对固件，已停止。',
      'Could not load SHA256SUMS, so the firmware cannot be checked; stopped.');
  }
  state.sums = core.parseSha256Sums(await sums.text());
}

function renderVersion() {
  if (!state.manifest) return;
  const boards = state.manifest.builds.map((b) => (lang() === 'en' ? b.name_en || b.name : b.name)).join(t('、', ', '));
  $('version').textContent = t('固件版本 ', 'Firmware ') + state.manifest.version + ' · ' + boards;
}

function showUnsupported(reason) {
  const why = {
    mobile: ['这是手机或平板。刷机要在电脑上做。', 'This is a phone or tablet; flashing needs a computer.'],
    'no-serial': ['这个浏览器没有网页串口（Web Serial）功能。', 'This browser has no Web Serial.'],
    insecure: ['网页串口只在 https:// 页面或本机的 http://localhost 上可用。请用 https 地址打开；在家庭服务器那台电脑上，用 http://localhost:8080/flash/ 打开。',
      'Web Serial works only on https:// pages or http://localhost. Use the https address, or open http://localhost:8080/flash/ on the household server machine itself.'],
  }[reason];
  $('unsupported').classList.remove('hidden');
  $('unsupportedWhy').textContent = why ? t(why[0], why[1]) : '';
  $('unsupported').dataset.reason = reason;
}

// ---- connect and look ----------------------------------------------------

async function connect() {
  if (state.busy) return;
  state.busy = true;
  state.done = false;
  state.files = null;
  state.plan = null;
  entries.length = 0;
  esptoolLines.length = 0;
  renderLog();
  $('refuse').classList.add('hidden');
  $('planSection').classList.add('hidden');
  $('doneSection').classList.add('hidden');
  $('device').classList.add('hidden');
  $('connect').disabled = true;
  let phase = 'port';
  try {
    await disconnect(true);
    const port = await navigator.serial.requestPort();
    state.transport = new Transport(port, false);
    state.loader = new ESPLoader({ transport: state.transport, baudrate: BAUD, terminal });
    phase = 'sync';
    stage('正在识别芯片…', 'Identifying the chip…', null);
    await state.loader.main();
    state.chip = (state.loader.chip && state.loader.chip.CHIP_NAME) || '';
    log(`识别到 ${state.chip}。`, `Found ${state.chip}.`);
    state.policy = core.BOARD_POLICY[state.chip] || null;
    state.build = state.policy ? core.pickBuild(state.manifest, state.chip) : null;
    if (!state.policy || !state.build) {
      refuse(`这块芯片是 ${state.chip || '未知型号'}。这里只能刷星智 Cube 1.54（ESP32-S3）和 FoloToy AI Passport（ESP32-C3）。没有写入任何东西。`,
        `This chip is ${state.chip || 'unknown'}. Only the Xingzhi Cube 1.54 (ESP32-S3) and the FoloToy AI Passport (ESP32-C3) are supported. Nothing was written.`);
      await disconnect(true);
      return;
    }
    phase = 'read';
    stage('正在读取设备信息…', 'Reading the device…', null);
    try { state.mac = await state.loader.chip.readMac(state.loader); } catch (_) { state.mac = ''; }
    try { state.flashBytes = core.flashBytesFromId(await state.loader.readFlashId()); } catch (_) { state.flashBytes = 0; }
    if (state.flashBytes && state.flashBytes < state.policy.flashBytes) {
      refuse(`这台设备的 flash 只有 ${kib(state.flashBytes)}，固件需要 ${kib(state.policy.flashBytes)}。没有写入任何东西。`,
        `This device has ${kib(state.flashBytes)} of flash; the firmware needs ${kib(state.policy.flashBytes)}. Nothing was written.`);
      await disconnect(true);
      return;
    }
    state.deviceTable = null;
    state.plan = null;
    try {
      const table = await withTimeout(state.loader.readFlash(core.PARTITION_TABLE_OFFSET, core.PARTITION_TABLE_MAX), READ_TIMEOUT_MS);
      const head = await withTimeout(state.loader.readFlash(core.APP_OFFSET, 0x100), READ_TIMEOUT_MS);
      state.installed = core.classifyInstalled(table, head);
      state.deviceTable = table;
    } catch (_) {
      state.installed = { kind: 'unknown', app: null };
    }
    if (state.installed.kind === 'muse-dual') {
      refuse('这台 Passport 装的是 Muse 双系统（Muse 和电视两个固件并存）。在这里整块写入会删掉 Muse 和它的配对，所以不刷。请按项目文档「AI Passport 与 Muse 并存」手动更新电视固件。没有写入任何东西。',
        'This Passport carries the Muse dual setup (Muse beside the TV). A full write here would delete Muse and its pairing, so it is refused. Update the TV firmware by hand as the "Passport beside Muse" document describes. Nothing was written.',
        BOARD_DOC);
      await disconnect(true);
      return;
    }
    // The firmware is fetched and checked now, before anything is offered:
    // whether an update can keep the settings depends on its partition table.
    phase = 'fetch';
    state.files = await downloadBuild(state.build);
    state.plan = state.installed.kind === 'tv' ? planFor(state.files, state.deviceTable) : null;
    if (state.plan && !state.plan.ok) log(state.plan.zh, state.plan.en);
    $('progress').classList.add('hidden');
    renderFacts();
    renderPlan();
    $('device').classList.remove('hidden');
    $('planSection').classList.remove('hidden');
    log('设备已连接，请确认下面的选项。', 'Connected. Check the options below.');
  } catch (error) {
    if (error && error.name === 'NotFoundError') {
      log('已取消，没有选设备。', 'Cancelled; no device chosen.');
    } else {
      failed(error, phase);
    }
    await disconnect(true);
    $('progress').classList.add('hidden');
  } finally {
    state.busy = false;
    $('connect').disabled = false;
    // renderPlan() above ran while busy, so it left the flash button disabled;
    // a Passport has no consent box whose click would redraw it.
    if (state.loader && state.policy) renderPlan();
  }
}

function refuse(zh, en, link) {
  const box = $('refuse');
  box.replaceChildren();
  const p = document.createElement('p');
  p.textContent = t(zh, en);
  box.appendChild(p);
  if (link) {
    const a = document.createElement('a');
    a.href = link;
    a.rel = 'noopener noreferrer';
    a.textContent = t('Passport 与 Muse 并存的说明', 'Passport beside Muse');
    box.appendChild(a);
  }
  box.dataset.zh = zh;
  box.dataset.en = en;
  box.dataset.link = link || '';
  box.classList.remove('hidden');
  $('progress').classList.add('hidden');
  log(zh, en);
}

function renderFacts() {
  const facts = $('facts');
  facts.replaceChildren();
  if (!state.build) return;
  const installed = state.installed;
  const now = {
    tv: [`小智电视 ${installed.app ? installed.app.version : ''}`, `Xiaozhi TV ${installed.app ? installed.app.version : ''}`],
    blank: ['空的（已擦除）', 'empty (erased)'],
    other: [installed.app && installed.app.project ? `别的固件（${installed.app.project}）` : '别的固件', installed.app && installed.app.project ? `other firmware (${installed.app.project})` : 'other firmware'],
    unknown: ['没读出来', 'could not read'],
  }[installed.kind] || ['?', '?'];
  const rows = [
    [t('设备', 'Device'), lang() === 'en' ? state.build.name_en || state.build.name : state.build.name],
    [t('芯片', 'Chip'), state.chip + (state.mac ? ' · ' + state.mac : '')],
    [t('Flash', 'Flash'), state.flashBytes ? kib(state.flashBytes) : t('未知', 'unknown')],
    [t('现在装的', 'Installed'), t(now[0], now[1])],
    [t('将要写入', 'Will write'), willWrite()],
  ];
  for (const [k, v] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    facts.append(dt, dd);
  }
}

// ---- the plan ------------------------------------------------------------

function choice() {
  const picked = document.querySelector('input[name="mode"]:checked');
  return picked ? picked.value : 'update';
}

function decision() {
  return core.decide(state.policy, state.installed.kind, choice(), state.plan);
}

// The update plan reads the partition table inside the merged image; a build
// of several files (none published so far) is always written whole.
function planFor(files, deviceTable) {
  if (files.length !== 1 || files[0].part.offset !== 0) {
    return { ok: false, code: 'parts', zh: '这个固件不是单个合并文件，只能整个写入。', en: 'This firmware is not one merged image; it can only be written whole.' };
  }
  return core.updatePlan(files[0].data, deviceTable);
}

// The writes this flash makes: [{ data, address }], from the update plan or
// the whole of each file.
function writes(d) {
  if (d.partial) {
    const image = state.files[0].data;
    return core.writeRegions(d, state.plan, image.length).map(([start, end]) => ({ data: image.slice(start, end), address: start }));
  }
  return state.files.map(({ part, data }) => ({ data, address: part.offset }));
}

const span = (start, end) => `${core.hex(start)}–${core.hex(end)}`;

function willWrite() {
  if (!state.files) return state.manifest.version;
  const d = decision();
  const parts = writes(d).map((w) => span(w.address, w.address + w.data.length)).join(t('、', ', '));
  let text = `${state.manifest.version} · ${parts}`;
  if (d.partial && state.plan.keep.length) {
    const kept = state.plan.keep.map((k) => k.label).join(t('、', ', '));
    text += t(`（不动：${kept}）`, ` (untouched: ${kept})`);
  }
  if (d.erase) text = t('先擦除整块芯片，再写 ', 'erase the chip, then ') + text;
  return text;
}

function renderPlan() {
  if (!state.policy) return;
  const d = decision();
  const xingzhi = state.policy.erase === 'first-install';
  $('planReplace').classList.toggle('hidden', d.reason !== 'replace');
  $('planChoose').classList.toggle('hidden', !d.choosable);
  $('cleanXingzhiZh').classList.toggle('hidden', !xingzhi);
  $('cleanXingzhiEn').classList.toggle('hidden', !xingzhi);
  $('cleanPassportZh').classList.toggle('hidden', xingzhi);
  $('cleanPassportEn').classList.toggle('hidden', xingzhi);
  $('planMismatch').classList.toggle('hidden', d.reason !== 'mismatch');
  if (d.reason === 'mismatch') {
    const why = state.plan && !state.plan.ok ? state.plan : { zh: '', en: '' };
    $('mismatchWhy').textContent = t(why.zh, why.en);
  }
  $('planPassport').classList.toggle('hidden', xingzhi);
  $('planUnknownLayout').classList.toggle('hidden', d.reason !== 'unknown');
  if (state.files) renderFacts();
  let agree = null;
  if (d.reason === 'replace') {
    agree = ['我知道会擦除整块芯片，小智 AI 固件会被删掉，要换成小智电视。',
      'I understand the whole chip is erased and the Xiaozhi AI firmware is deleted.'];
  } else if (d.reason === 'clean' || d.reason === 'mismatch') {
    agree = xingzhi
      ? ['我知道会擦除整块芯片，Wi-Fi、服务器地址要重新设置，设备要在管理后台「重新配对」。',
        'I understand the chip is erased; Wi-Fi and server must be set again and the device re-paired in the admin.']
      : ['我知道设置会清空：Wi-Fi、服务器地址要重新设置，设备要在管理后台「重新配对」。',
        'I understand the settings are cleared; Wi-Fi and server must be set again and the device re-paired in the admin.'];
  } else if (d.reason === 'unknown') {
    agree = ['这台 Passport 没有装 Muse 双系统；我知道设置会清空，可能要重新配网、在管理后台「重新配对」。',
      'This Passport does not carry the Muse dual setup; I understand its settings are cleared and it may need Wi-Fi setup and re-pairing.'];
  }
  const row = $('agreeRow');
  if (agree) {
    if (row.dataset.key !== agree[0]) $('agree').checked = false;
    row.dataset.key = agree[0];
    $('agreeText').textContent = t(agree[0], agree[1]);
    row.classList.remove('hidden');
  } else {
    row.dataset.key = '';
    $('agree').checked = false;
    row.classList.add('hidden');
  }
  $('flash').disabled = state.busy || !state.loader || (agree !== null && !$('agree').checked);
}

// ---- write ---------------------------------------------------------------

async function flash() {
  if (state.busy || !state.loader || !state.build || !state.files) return;
  const d = decision();
  if (d.needsConsent && !$('agree').checked) return;
  state.busy = true;
  $('flash').disabled = true;
  $('connect').disabled = true;
  $('doneSection').classList.add('hidden');
  let phase = 'fetch';
  try {
    // 1. What goes where: the update plan's regions or the whole image, both
    //    from the files checked at connect.
    const fileArray = writes(d);
    // 2. Where it goes, once more, against the policy rather than the manifest.
    const ranges = fileArray.map((w) => ({ path: span(w.address, w.address + w.data.length), offset: w.address, size: w.data.length }));
    const problems = core.rangeProblems(ranges, state.policy, state.flashBytes || state.policy.flashBytes);
    if (problems.length) throw new core.FlasherError('range', problems[0].zh, problems[0].en);
    // 3. Erase, only when the plan says so; never on a Passport (core.decide).
    if (d.erase) {
      phase = 'erase';
      stage('正在擦除整块芯片，约半分钟到一分钟…', 'Erasing the chip, 30-60 seconds…', null);
      await state.loader.eraseFlash();
      log('已擦除。', 'Erased.');
    }
    // 4. Write. eraseAll stays false: each sector is erased as it is written,
    //    so only the listed ranges change -- in an update, not the settings.
    //    calculateMD5Hash makes the stub hash each range in flash and compare.
    phase = 'write';
    if (d.partial) {
      const kept = state.plan.keep.map((k) => `${k.label} ${span(k.start, k.end)}`).join(t('、', ', '));
      log(`只写 ${ranges.map((r) => r.path).join('、')}；不动 ${kept}。`,
        `Writing only ${ranges.map((r) => r.path).join(', ')}; leaving ${kept} as it is.`);
    }
    const total = fileArray.reduce((sum, w) => sum + w.data.length, 0);
    const before = fileArray.map((_, i) => fileArray.slice(0, i).reduce((sum, w) => sum + w.data.length, 0));
    stage('正在写入…', 'Writing…', 0);
    await state.loader.writeFlash({
      fileArray,
      flashSize: 'keep', flashMode: 'keep', flashFreq: 'keep',
      eraseAll: false,
      compress: true,
      reportProgress: (i, written, size) => stage('正在写入…', 'Writing…',
        total ? (before[i] + (size ? written / size : 0) * fileArray[i].data.length) / total : 0),
      calculateMD5Hash: (image) => core.md5Hex(image),
    });
    stage('写入完成，已核对。', 'Written and verified.', 1);
    log('写入完成，设备里的内容和固件一致。', 'Written; flash contents match the firmware.');
    state.done = true;
    state.doneReason = d.reason;
    renderDone();
    $('doneSection').classList.remove('hidden');
    $('doneSection').scrollIntoView({ behavior: 'smooth', block: 'start' });
    await disconnect(true);
    $('planSection').classList.add('hidden');
  } catch (error) {
    failed(error, phase);
  } finally {
    state.busy = false;
    $('connect').disabled = false;
    renderPlan();
  }
}

// The build's files, downloaded and checked before the device is touched:
// size, SHA-256 against the manifest and SHA256SUMS, chip id in the header.
async function downloadBuild(build) {
  const files = [];
  for (const part of build.parts) {
    stage(`正在下载 ${part.path}…`, `Downloading ${part.path}…`, null);
    const response = await fetch(FIRMWARE_DIR + part.path, { cache: 'no-store' });
    if (!response.ok) {
      throw new core.FlasherError('fetch', `下载 ${part.path} 失败（HTTP ${response.status}），设备没被改动。`,
        `Downloading ${part.path} failed (HTTP ${response.status}); the device was not touched.`);
    }
    const data = new Uint8Array(await response.arrayBuffer());
    if (data.length !== part.size) {
      throw new core.FlasherError('fetch', `${part.path} 大小不对，可能没下载完整，设备没被改动。`,
        `${part.path} has the wrong size (incomplete download?); the device was not touched.`);
    }
    core.checkDigest(part, await core.sha256Hex(data), state.sums);
    if (part.offset === 0 && core.imageChipId(data) !== state.policy.chipId) {
      throw new core.FlasherError('chip', `${part.path} 不是给 ${state.chip} 的固件，已停止，设备没被改动。`,
        `${part.path} is not built for ${state.chip}; stopped, the device was not touched.`);
    }
    files.push({ part, data });
  }
  log('固件已下载，校验通过。', 'Firmware downloaded and checked.');
  return files;
}

function renderDone() {
  const xingzhi = state.policy && state.policy.erase === 'first-install';
  // An update kept Wi-Fi, server and the device's secret: it reconnects by
  // itself. Everything else starts at the setup screens.
  const kept = state.doneReason === 'update';
  $('doneKept').classList.toggle('hidden', !kept);
  $('doneSetup').classList.toggle('hidden', kept);
  $('restartZh').textContent = xingzhi
    ? '按一下最下面的键（复位键）。屏幕还是黑的，就长按它关机，再按一下开机。'
    : '按住电源键约 2 秒关机，再按住约 0.5 秒开机。它有电池，拔线不会重启。';
  $('restartEn').textContent = xingzhi
    ? 'Tap the bottom key (reset). If the screen stays dark, hold it to switch off, then press it to switch on.'
    : 'Hold the power button about 2 s to switch off, then about 0.5 s to switch on. It has a battery; unplugging does not restart it.';
}

// The household server (or the PC client) serving this page knows its LAN
// address: show the real sound setup link and a QR code for the phone instead
// of "<server address>". Same origin only (CSP connect-src/img-src 'self').
async function loadServerInfo() {
  let info = null;
  try {
    const response = await fetch(core.SETUP_INFO_PATH, { cache: 'no-store', credentials: 'omit' });
    const type = response.headers.get('content-type') || '';
    if (response.ok && type.includes('application/json')) info = await response.json();
  } catch (_) { /* the static site: no such API */ }
  state.wifi = core.serverWifiLink(info);
  renderWifi();
}

function renderWifi() {
  const wifi = state.wifi;
  if (!wifi) return;
  $('wifiCodeZh').textContent = wifi.url;
  $('wifiCodeEn').textContent = wifi.url;
  $('wifiAddrZh').textContent = wifi.address;
  $('wifiAddrEn').textContent = wifi.address;
  const link = $('wifiUrl');
  link.href = wifi.url;
  link.textContent = wifi.url;
  const qr = $('wifiQr');
  if (!qr.getAttribute('src')) {
    qr.alt = t('声音配网页的二维码', 'QR code of the sound setup page');
    qr.addEventListener('error', () => qr.classList.add('hidden'), { once: true });
    qr.src = core.WIFI_QR_PATH;
  }
  $('wifiBox').classList.remove('hidden');
}

function failed(error, phase) {
  const message = error instanceof core.FlasherError ? t(error.zh, error.en)
    : (error && error.message) || String(error);
  if (error instanceof core.FlasherError) log(error.zh, error.en);
  else log('出错了：' + message, 'Error: ' + message);
  const hints = {
    port: ['打不开串口：关掉别的正在用这个串口的程序（串口监视器、另一个刷机页），再试。', 'Could not open the port: close other programs using it (serial monitors, another flasher tab) and retry.'],
    sync: ['设备没有应答。让它进入下载模式（见「连不上？」），再点「连接设备」。', 'The device did not answer. Put it in download mode (see "Not found?") and press Connect again.'],
    read: ['读设备信息时断开了。重新连接试试。', 'The connection dropped while reading. Connect again.'],
    fetch: ['设备没被改动。刷新页面再试。', 'The device was not touched. Reload and retry.'],
    erase: ['擦除没完成，设备现在没有能用的固件。让它进入下载模式，重新连接再刷一次。', 'The erase did not finish and the device has no working firmware. Put it in download mode, connect and flash again.'],
    write: ['写入没完成，设备现在没有能用的固件。让它进入下载模式，重新连接再刷一次就好。', 'The write did not finish and the device has no working firmware. Put it in download mode, connect and flash again.'],
  }[phase];
  // A FlasherError from the download already says the device was not touched.
  if (hints && !(error instanceof core.FlasherError && phase === 'fetch')) log(hints[0], hints[1]);
  if (esptoolLines.length && !(error instanceof core.FlasherError)) {
    const tail = esptoolLines.slice(-8).join('\n');
    log('详细信息：\n' + tail, 'Details:\n' + tail);
  }
  stage('出错了，见下面的说明。', 'Failed; see below.', 0);
}

async function disconnect(quiet) {
  const transport = state.transport;
  state.transport = null;
  state.loader = null;
  if (transport) {
    try { await transport.disconnect(); } catch (_) { /* already gone */ }
  }
  if (!quiet) {
    $('planSection').classList.add('hidden');
    $('device').classList.add('hidden');
    $('progress').classList.add('hidden');
    log('已断开。', 'Disconnected.');
  }
}

function renderAll() {
  renderVersion();
  renderLog();
  if (state.policy && state.build) { renderFacts(); renderPlan(); }
  if (state.done) renderDone();
  if (state.wifi) $('wifiQr').alt = t('声音配网页的二维码', 'QR code of the sound setup page');
  const box = $('refuse');
  if (!box.classList.contains('hidden') && box.dataset.zh) {
    box.firstChild.textContent = t(box.dataset.zh, box.dataset.en);
    if (box.dataset.link) box.lastChild.textContent = t('Passport 与 Muse 并存的说明', 'Passport beside Muse');
  }
  const st = $('stage');
  if (st.dataset.zh) st.textContent = t(st.dataset.zh, st.dataset.en);
  const un = $('unsupported');
  if (!un.classList.contains('hidden')) showUnsupported(un.dataset.reason);
}

// ---- start ---------------------------------------------------------------

async function start() {
  let saved = null;
  try { saved = localStorage.getItem('flash-lang'); } catch (_) { /* no storage */ }
  const asked = new URLSearchParams(location.search).get('lang');
  setLang(asked === 'en' || asked === 'zh' ? asked : saved || 'zh');
  $('langToggle').addEventListener('click', () => setLang(lang() === 'en' ? 'zh' : 'en'));
  $('connect').addEventListener('click', connect);
  $('flash').addEventListener('click', flash);
  $('disconnect').addEventListener('click', () => disconnect(false));
  $('agree').addEventListener('change', renderPlan);
  for (const radio of document.querySelectorAll('input[name="mode"]')) radio.addEventListener('change', renderPlan);

  const support = core.browserSupport(navigator, window.isSecureContext);
  if (!support.ok) showUnsupported(support.reason);
  loadServerInfo();     // not awaited: the static site answers 404 and nothing changes
  try {
    await loadManifest();
    renderVersion();
  } catch (error) {
    failed(error, 'fetch');
    $('progress').classList.add('hidden');
    return;
  }
  $('connect').disabled = !support.ok;
}

start();
