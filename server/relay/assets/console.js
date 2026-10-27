/* 小鼠电视 RooTV 电脑版（媒体中心界面，阶段 M0–M3）。
   设计：docs/superpowers/specs/2026-10-24-media-center-ui-design.md；原型：docs/design/media-center/index.html。
   - 外框：顶部标签栏（窗口窄于 700px 时换成底部五个标签）、页面路由（#/home、#/live …）、正在播放栏、
     电视遥控器式的方向键焦点（spatial）、深色默认 + 浅色（记在 localStorage）。
   - 「播放到」：每个播放按钮播到记住的地方（某台小电视或这台电脑），旁边的 ▾ 换地方；POST /api/play。
   - 遥控面板：右边的抽屉（手机上是「遥控」标签）：屏幕、换台、暂停、跳转、音量、亮度、只听、定时关、宠物。
   - 首页：GET /api/home 拼好的几排。
   原来电脑版的各项功能（订阅管理、B 站搜索、投屏、设备、Wi-Fi、访客码、绑定、登录列表、B 站账号、YouTube、
   让手机登录、恢复登录提示）都搬进来了，逻辑基本照旧；原来的总后台是管理员的「管理」（#/admin，console-admin.js），
   邀请家人也在那里。旧版电脑版和总后台的页面已经去掉（对照表见设计文档的「Classic parity」一节）。 */
"use strict";

// 产品名：界面上的字标写 RooTV；中文界面需要产品名的地方写「小鼠电视」。只在这两个常量里。
const BRAND = "RooTV";
const BRAND_ZH = "小鼠电视";

// ======================================================================= 小工具

const $ = (id) => document.getElementById(id);
const qs = (sel, root) => (root || document).querySelector(sel);
const qsa = (sel, root) => [...(root || document).querySelectorAll(sel)];
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hsh = (s) => { let h = 2166136261; for (const c of String(s)) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619) >>> 0; } return h; };
const RM = matchMedia("(prefers-reduced-motion: reduce)");
const SANS = "-apple-system,PingFang SC,Helvetica Neue,Microsoft YaHei,sans-serif";

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, cls, onclick) {
  const b = el("button", cls || "linkbtn", text);
  b.onclick = onclick;
  return b;
}
function lsGet(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (error) { return fallback; }
}
function lsSet(key, value) {
  try { localStorage.setItem(key, value); } catch (error) { /* 无痕窗口、禁用了存储：只是不记住 */ }
}
let toastTimer = 0;
function toast(text, bad, ms) {
  const t = $("toast");
  t.textContent = text;
  t.className = "on" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ""; }, ms || 2800);
}
function untoast() { clearTimeout(toastTimer); $("toast").className = ""; }
function pad(n) { return String(n).padStart(2, "0"); }
function clock(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds / 60) % 60, s = seconds % 60;
  return (h ? h + ":" + pad(m) : m) + ":" + pad(s);
}
function when(at) {
  if (!at) return "—";
  const d = new Date(at * 1000), now = new Date();
  const time = pad(d.getHours()) + ":" + pad(d.getMinutes());
  if (d.toDateString() === now.toDateString()) return "今天 " + time;
  if (d.toDateString() === new Date(now.getTime() - 86400000).toDateString()) return "昨天 " + time;
  return (d.getMonth() + 1) + "月" + d.getDate() + "日 " + time;
}
function ago(at) {
  if (!at) return "";
  const s = Date.now() / 1000 - at;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + " 分钟前";
  if (s < 86400) return Math.round(s / 3600) + " 小时前";
  if (s < 86400 * 30) return Math.round(s / 86400) + " 天前";
  return when(at);
}
function hm(at) { const date = new Date(at * 1000); return pad(date.getHours()) + ":" + pad(date.getMinutes()); }
function siteTag(site) { return site === "youtube" ? "YouTube" : "B 站"; }
function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

class SignedOut extends Error {}
async function call(method, path, body) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 404) throw new SignedOut("登录已失效");
  return response.json();
}
async function busy(btn, work) {
  btn.disabled = true;
  try { await work(); }
  catch (error) { if (error instanceof SignedOut) signedOut(); else toast(error.message || "网络不通", true); }
  finally { btn.disabled = false; }
}

// ======================================================================= 图标（为本项目画的简单线条图标）

const IC = {
  home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
  live: '<rect x="3" y="7" width="18" height="13" rx="3"/><path d="M8 3l4 4 4-4"/>',
  series: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M7 5v14M17 5v14M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  up: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6"/>',
  lib: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  guide: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M8 9v11M12 14h6M12 17h4"/>',
  remote: '<rect x="7" y="2" width="10" height="20" rx="4"/><circle cx="12" cy="8" r="2"/><path d="M10 14h4M10 17h4"/>',
  set: '<path d="M19.18 9.51 L21.77 9.86 L21.77 14.14 L19.18 14.49 L18.84 15.32 L20.42 17.39 L17.39 20.42 L15.32 18.84 L14.49 19.18 L14.14 21.77 L9.86 21.77 L9.51 19.18 L8.68 18.84 L6.61 20.42 L3.58 17.39 L5.16 15.32 L4.82 14.49 L2.23 14.14 L2.23 9.86 L4.82 9.51 L5.16 8.68 L3.58 6.61 L6.61 3.58 L8.68 5.16 L9.51 4.82 L9.86 2.23 L14.14 2.23 L14.49 4.82 L15.32 5.16 L17.39 3.58 L20.42 6.61 L18.84 8.68Z"/><circle cx="12" cy="12" r="3.2"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2.2"/><circle cx="9" cy="17" r="2.2"/>',
  play: '<path d="M7 4.5v15a1 1 0 0 0 1.5.86l12-7.5a1 1 0 0 0 0-1.72l-12-7.5A1 1 0 0 0 7 4.5z" fill="currentColor" stroke="none"/>',
  pause: '<rect x="6" y="4" width="4.5" height="16" rx="1.2" fill="currentColor" stroke="none"/><rect x="13.5" y="4" width="4.5" height="16" rx="1.2" fill="currentColor" stroke="none"/>',
  next: '<path d="M4 5.5v13a1 1 0 0 0 1.6.8l9-6.5a1 1 0 0 0 0-1.6l-9-6.5A1 1 0 0 0 4 5.5zM17 5h2.5v14H17z" fill="currentColor" stroke="none"/>',
  prev: '<path d="M20 5.5v13a1 1 0 0 1-1.6.8l-9-6.5a1 1 0 0 1 0-1.6l9-6.5A1 1 0 0 1 20 5.5zM4.5 5H7v14H4.5z" fill="currentColor" stroke="none"/>',
  tv: '<rect x="2" y="5" width="20" height="13" rx="3"/><path d="M8 21h8"/>',
  pc: '<rect x="4" y="4" width="16" height="11" rx="2"/><path d="M2 19h20"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  upc: '<path d="M6 15l6-6 6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  vol: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16 9a4 4 0 0 1 0 6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
  moon: '<path d="M20 15A8 8 0 1 1 9 4a7 7 0 0 0 11 11z"/>',
  ear: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="4" height="6" rx="1.5"/><rect x="17" y="14" width="4" height="6" rx="1.5"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  fwd: '<path d="M9 5l7 7-7 7"/>',
  warn: '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  r30: '<path d="M4 12a8 8 0 1 0 3-6.2"/><path d="M4 4v5h5"/>',
  f30: '<path d="M20 12a8 8 0 1 1-3-6.2"/><path d="M20 4v5h-5"/>',
  link: '<path d="M10 14a4 4 0 0 0 6 0l3-3a4 4 0 0 0-6-6l-1 1"/><path d="M14 10a4 4 0 0 0-6 0l-3 3a4 4 0 0 0 6 6l1-1"/>',
  hist: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  expand: '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>',
  shrink: '<path d="M20 10h-6V4M4 14h6v6M14 10l7-7M10 14l-7 7"/>',
  full: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  unfull: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
  mute: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16.5 9.5l5 5M21.5 9.5l-5 5"/>',
  r10: '<path d="M4.6 14.5A8 8 0 1 0 7 5.8"/><path d="M7.5 2.5 7 5.8l3.3.8"/><text x="12.6" y="15.4" font-size="7.4" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none">10</text>',
  f10: '<path d="M19.4 14.5A8 8 0 1 1 17 5.8"/><path d="M16.5 2.5l.5 3.3-3.3.8"/><text x="11.4" y="15.4" font-size="7.4" font-weight="700" text-anchor="middle" fill="currentColor" stroke="none">10</text>',
  pip: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><rect x="12" y="11" width="7" height="6" rx="1.2" fill="currentColor" stroke="none"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  chart: '<path d="M4 20V4M4 20h16"/><path d="M7 15l4-5 3 3 5-6"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.3-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.8c1.9.8 3.1 2.6 3.5 5.2"/>',
  log: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
  sys: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  list: '<path d="M8 6h12M8 12h12M8 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
};
const ic = (n, s = 20) => `<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${IC[n] || ""}</svg>`;
function hydrateIcons(root) {
  for (const node of qsa("[data-ic]", root)) node.innerHTML = ic(node.dataset.ic, Number(node.dataset.ics || 20));
}

// ======================================================================= 标志（图标 B「鼠耳小电视」和字标）
// 和原型 docs/design/media-center/index.html 的 BRAND-ICONS 是同一份画法（只留选定的 B）；
// 应用图标的 PNG / ICNS / ICO 由改名那一步从 docs/design/media-center/brand/icon-b*.svg 导出。

const MC = { body: "#dcd6e6", shade: "#c3bcd2", ear: "#ffb3c4", nose: "#ff6f86" };
const SQUIRCLE = (() => { const n = 5, pts = []; for (let i = 0; i < 72; i++) { const t = i / 72 * 2 * Math.PI, c = Math.cos(t), s = Math.sin(t);
  pts.push([512 + 512 * Math.sign(c) * Math.abs(c) ** (2 / n), 512 + 512 * Math.sign(s) * Math.abs(s) ** (2 / n)]); }
  return "M" + pts.map((p) => p.map((v) => v.toFixed(1)).join(" ")).join("L") + "Z"; })();
const rr = (x, y, w, h, r) => `M${x + r} ${y}H${x + w - r}A${r} ${r} 0 0 1 ${x + w} ${y + r}V${y + h - r}A${r} ${r} 0 0 1 ${x + w - r} ${y + h}H${x + r}A${r} ${r} 0 0 1 ${x} ${y + h - r}V${y + r}A${r} ${r} 0 0 1 ${x + r} ${y}Z`;
const ear = (cx, cy, r, ri) => `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${MC.body}"/><circle cx="${cx}" cy="${cy}" r="${ri}" fill="${MC.ear}"/>`;
let BUID = 0;
function iconSVG(size, small, label) {
  const u = "bi" + (BUID++);
  const g = small
    ? `${ear(282, 300, 176, 100)}${ear(742, 300, 176, 100)}<path d="${rr(112, 300, 800, 610, 200)}" fill="${MC.body}"/><path d="${rr(204, 404, 616, 416, 132)}" fill="#20264a"/>
      <circle cx="420" cy="592" r="50" fill="#fff3e6"/><circle cx="604" cy="592" r="50" fill="#fff3e6"/><ellipse cx="512" cy="686" rx="42" ry="32" fill="${MC.nose}"/>`
    : `${ear(300, 300, 150, 92)}${ear(724, 300, 150, 92)}<path d="M214 604l-118 -22M214 654l-112 20M810 604l118 -22M810 654l112 20" stroke="${MC.body}" stroke-width="16" stroke-linecap="round"/>
      <path d="${rr(306, 820, 112, 76, 38)}" fill="${MC.shade}"/><path d="${rr(606, 820, 112, 76, 38)}" fill="${MC.shade}"/>
      <path d="${rr(152, 334, 720, 548, 180)}" fill="#000" opacity=".22"/><path d="${rr(152, 300, 720, 548, 180)}" fill="${MC.body}"/>
      <defs><radialGradient id="${u}g" cx=".5" cy=".38" r=".7"><stop offset="0" stop-color="#3f4c92"/><stop offset="1" stop-color="#1f2446"/></radialGradient></defs>
      <path d="${rr(236, 392, 552, 372, 120)}" fill="url(#${u}g)"/>
      <circle cx="380" cy="640" r="28" fill="${MC.nose}" opacity=".45"/><circle cx="644" cy="640" r="28" fill="${MC.nose}" opacity=".45"/>
      <circle cx="436" cy="560" r="38" fill="#fff3e6"/><circle cx="588" cy="560" r="38" fill="#fff3e6"/><ellipse cx="512" cy="628" rx="30" ry="22" fill="${MC.nose}"/>
      <path d="M464 668q24 28 48 0q24 28 48 0" stroke="#fff3e6" stroke-width="16" stroke-linecap="round" stroke-linejoin="round" fill="none"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="${size}" height="${size}"${label ? ` role="img" aria-label="${esc(label)}"` : ' aria-hidden="true"'}><defs><linearGradient id="${u}b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4a5aa0"/><stop offset="1" stop-color="#1b2145"/></linearGradient>
<linearGradient id="${u}h" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".22"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></linearGradient></defs>
<path d="${SQUIRCLE}" fill="url(#${u}b)"/><path d="${SQUIRCLE}" fill="url(#${u}h)"/>${g}</svg>`;
}
// 字标「RooTV」：圆头粗线条画的几何字母，不依赖字体。
function wordmarkSVG(h, label) {
  return `<svg class="wmk" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 322 104" height="${h}" width="${(h * 322 / 104).toFixed(1)}"${label === false ? ' aria-hidden="true"' : ' role="img" aria-label="RooTV"'}><g fill="none" stroke-width="17" stroke-linecap="round" stroke-linejoin="round">
<path d="M12 20V86M12 20H36a20 20 0 0 1 0 40H12M33 60 54 86" stroke="currentColor"/><circle cx="96" cy="63" r="22.5" stroke="currentColor"/><circle cx="158" cy="63" r="22.5" stroke="currentColor"/>
<path d="M202 20H256M229 20V86M270 20 290 86 310 20" stroke="#ff6b3d"/></g></svg>`;
}

// ======================================================================= 占位图：封面、台标、头像
// 每个图位都有一张由标题决定的占位图（取不到封面、还在加载时），网格不会塌。

let UID = 0;
function lcg(seed) { let s = seed || 1; return () => (s = (Math.imul(s, 1103515245) + 12345) >>> 0) / 4294967296; }
function art(t, ratio, opt) {
  opt = opt || {};
  const W = ratio === "p" ? 200 : 320, H = ratio === "p" ? 300 : 180, id = "g" + (UID++), v = hsh(t), rnd = lcg(v);
  const hue = opt.hue != null ? opt.hue : v % 360;
  const c1 = `hsl(${hue} 46% 34%)`, c2 = `hsl(${(hue + 35) % 360} 52% 11%)`, c3 = `hsl(${(hue + 15) % 360} 82% 70%)`, c4 = `hsl(${(hue + 190) % 360} 48% 56%)`;
  let m = "";
  const k = opt.motif != null ? opt.motif : (v >> 3) % 5;
  if (k === 0) {
    m = `<circle cx="${W * .7}" cy="${H * .4}" r="${W * .15}" fill="${c3}" opacity=".85"/><path d="M0 ${H * .72}L${W * .28} ${H * .5}L${W * .5} ${H * .63}L${W * .76} ${H * .44}L${W} ${H * .64}V${H}H0Z" fill="${c2}" opacity=".92"/><path d="M0 ${H * .84}L${W * .42} ${H * .68}L${W} ${H * .88}V${H}H0Z" fill="#000" opacity=".45"/>`;
  } else if (k === 1) {
    for (let i = 0; i < 22; i++) m += `<circle cx="${(rnd() * W).toFixed(1)}" cy="${(rnd() * H * .75).toFixed(1)}" r="${(rnd() * 1.6 + .4).toFixed(1)}" fill="#fff" opacity="${(rnd() * .6 + .3).toFixed(2)}"/>`;
    m += `<circle cx="${W * .3}" cy="${H * .32}" r="${W * .12}" fill="${c3}"/><circle cx="${W * .35}" cy="${H * .28}" r="${W * .11}" fill="${c1}"/><path d="M0 ${H * .8}Q${W * .5} ${H * .62} ${W} ${H * .82}V${H}H0Z" fill="${c2}"/>`;
  } else if (k === 2) {
    m = `<g transform="rotate(-20 ${W / 2} ${H / 2})" opacity=".6"><rect x="${-W}" y="${H * .3}" width="${W * 3}" height="${H * .09}" fill="${c3}"/><rect x="${-W}" y="${H * .46}" width="${W * 3}" height="${H * .04}" fill="${c4}"/><rect x="${-W}" y="${H * .58}" width="${W * 3}" height="${H * .14}" fill="${c2}"/></g>`;
  } else if (k === 3) {
    for (let i = 0; i < 4; i++) { const y = H * (.5 + i * .12), a = H * .05; m += `<path d="M0 ${y}Q${W * .25} ${y - a} ${W * .5} ${y}T${W} ${y}V${H}H0Z" fill="${i % 2 ? c2 : c4}" opacity="${.35 + i * .15}"/>`; }
    m += `<circle cx="${W * .78}" cy="${H * .26}" r="${W * .07}" fill="${c3}"/>`;
  } else {
    m = `<rect x="${W * .22}" y="${H * .14}" width="${W * .56}" height="${H * .56}" rx="${W * .28}" fill="${c4}" opacity=".3"/><rect x="${W * .32}" y="${H * .24}" width="${W * .36}" height="${H * .46}" rx="${W * .18}" fill="${c3}" opacity=".5"/><rect x="0" y="${H * .7}" width="${W}" height="${H * .3}" fill="${c2}"/>`;
  }
  let txt = "";
  if (opt.title) {
    const L = [...opt.title].length;
    if (ratio === "p") {
      const fs = Math.min(30, 168 / Math.max(L, 2));
      txt = `<text x="${W / 2}" y="${H * .86}" text-anchor="middle" font-family="${SANS}" font-weight="800" font-size="${fs}" fill="#fff">${esc(opt.title)}</text>`;
    } else {
      const fs = Math.min(24, 250 / Math.max(L, 2));
      txt = `<text x="18" y="${H - 22}" font-family="${SANS}" font-weight="800" font-size="${fs}" fill="#fff" opacity=".94">${esc(opt.title)}</text>`;
    }
  }
  if (opt.kick) txt += `<text x="14" y="24" font-family="${SANS}" font-weight="700" font-size="11" fill="#fff" opacity=".8" letter-spacing="1.5">${esc(opt.kick)}</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid slice" aria-hidden="true"><defs><linearGradient id="${id}" x1="0" y1="0" x2=".35" y2="1"><stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs><rect width="${W}" height="${H}" fill="url(#${id})"/>${m}${txt}</svg>`;
}
// 台名 → 台标徽标上的两行字：「CCTV-1 综合」→ CCTV / 1，「湖南卫视」→ 湖南 / 卫视。
function shortName(name) {
  const text = String(name || "").replace(/\s+/g, "");
  const m = text.match(/^(CCTV|CGTN|CETV)-?(\d+\+?|4K|8K|少儿|新闻|音乐)?/i);
  if (m) return [m[1].toUpperCase(), m[2] || ""];
  const w = text.replace(/(HD|高清|超清|标清|4K)$/i, "");
  if (/卫视$/.test(w) && w.length > 2) return [w.slice(0, -2).slice(0, 4), "卫视"];
  return [[...w].slice(0, 4).join(""), ""];
}
function badgeSVG(name) {
  const [a, b] = shortName(name), hue = hsh(name) % 360;
  const t = b ? `<text x="32" y="16" text-anchor="middle" font-family="${SANS}" font-size="10" font-weight="600" fill="#fff" opacity=".85">${esc(a)}</text><text x="32" y="33" text-anchor="middle" font-family="${SANS}" font-size="${[...b].length > 2 ? 12 : 16}" font-weight="800" fill="#fff">${esc(b)}</text>`
    : `<text x="32" y="26" text-anchor="middle" font-family="${SANS}" font-size="${[...a].length > 3 ? 11 : 14}" font-weight="800" fill="#fff">${esc(a)}</text>`;
  return `<svg viewBox="0 0 64 40" width="64" height="40" aria-hidden="true"><rect width="64" height="40" rx="9" fill="hsl(${hue} 52% 36%)"/><rect x=".5" y=".5" width="63" height="39" rx="8.5" fill="none" stroke="#fff" stroke-opacity=".18"/>${t}</svg>`;
}
function avatarSVG(name, s) {
  const hue = hsh(name) % 360, ch = [...String(name || "?")][0];
  return `<svg viewBox="0 0 72 72" width="${s}" height="${s}" aria-hidden="true"><circle cx="36" cy="36" r="36" fill="hsl(${hue} 44% 40%)"/><circle cx="36" cy="80" r="34" fill="hsl(${(hue + 30) % 360} 48% 24%)"/><text x="36" y="45" text-anchor="middle" font-family="${SANS}" font-size="27" font-weight="700" fill="#fff">${esc(ch)}</text></svg>`;
}
const imgTag = (url, cls) => url ? `<img src="${esc(url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer"${cls ? ` class="${cls}"` : ""}>` : "";
// 取不到的图（404、502、图床连不上）：去掉，露出下面的占位图。
document.addEventListener("error", (event) => {
  const t = event.target;
  if (t && t.id === "pcPoster") { t.hidden = true; t.removeAttribute("src"); return; }   // 播放器的封面留着下次用
  if (t && t.tagName === "IMG" && !t.closest("#petgrid")) t.remove();
}, true);

// ======================================================================= 主题

function applyTheme(theme) {
  theme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  const meta = qs('meta[name="theme-color"]');
  if (meta) meta.content = theme === "light" ? "#f2f2f6" : "#0b0b0d";
  for (const b of qsa("[data-theme-pick]")) b.setAttribute("aria-pressed", String(b.dataset.themePick === theme));
}
applyTheme(lsGet("rootv.theme", "dark"));
for (const b of qsa("[data-theme-pick]")) b.onclick = () => { lsSet("rootv.theme", b.dataset.themePick); applyTheme(b.dataset.themePick); };

// ======================================================================= 状态

// 和固件 xiaozhi_tv_ui_assets.h 的 PETS[] 同序（同 remote.html）。
const PETS = [["diandian", "电电"], ["doudou", "豆豆"], ["mimi", "咪咪"], ["bobo", "波波"], ["kaka", "卡卡"], ["dengdeng", "灯灯"],
              ["anan", "按按"], ["tietie", "铁铁"], ["piaopiao", "飘飘"], ["jiujiu", "啾啾"], ["cici", "刺刺"]];
const SLEEP_STEPS = [0, 15, 30, 60, 90];
const PC = "pc";
let me = null;            // {name, role, kind}
let data = null;          // /api/state（电脑选的那台）
let route = "home";
let chosen = "";          // 遥控面板和屏幕看的那台；空 = 跟着服务器的「自动」
let group = "";
let pending = null;       // 换台、跳转后到设备真的过去之前
let adjusting = false;
let fetchedAt = 0;
let dragging = false;
let dialBusy = "";
let listenPending = null;
let homeData = null, homeAt = 0;
let targetsData = null, targetsAt = 0;
let targetPref = "";      // 「播放到」记住的地方：设备 id、"pc"；空 = 服务器的自动
let remember = true;
let heroIndex = 0;
let miniWho = "tv";       // 正在播放栏跟着谁：最近一次在哪儿开始播的（"tv" / "pc"）
let sleepSet = {};        // 每台设备最近一次在这里设的定时（分钟），给「定时关」按钮标选中

const isGuest = () => !!me && me.role === "guest";
const isAdmin = () => !!me && me.role === "admin";
const isM = () => $("app").classList.contains("m");

// ======================================================================= 登录（和原来一样）

let ticketRun = 0;
async function boot() {
  let status;
  try { status = await call("GET", "api/login/status"); }
  catch (error) { setTimeout(boot, 3000); return; }
  if (!status.signed_in) { showLogin(); return; }
  me = status.me;
  showApp();
  recoveryNotice(status.notice);
}

// 别的手机通过设备上的「恢复登录」登进了这个账号：提示一次（按 id 记在这个浏览器里），登录列表里可以退出它。
function recoveryNotice(notice) {
  if (!notice) return;
  if (lsGet("tvRecoveryNotice", "") === notice.id) return;
  lsSet("tvRecoveryNotice", notice.id);
  toast(notice.text + "（" + notice.browser + "，" + when(notice.at) + "）。不是你的话，在「设置」里退出它", true, 9000);
}

function showLogin() {
  if (watching) stopWatch();
  closeSheets();
  me = null;
  data = null;
  homeData = null;
  channelsSig = seriesSig = subsSig = historySig = "";
  targetsData = null;
  bili = null;          // 换了人登录电脑版：B 站账号重新读（bililogin.py）
  biliCancel();
  $("app").hidden = true;
  $("login").hidden = false;
  $("loginBrand").innerHTML = iconSVG(44, true) + wordmarkSVG(26) + `<span style="font:600 15px var(--sans);color:rgba(235,235,245,.6)">${esc(BRAND_ZH)} · 电脑版</span>`;
  document.title = "登录 · " + BRAND_ZH;
  newTicket(0);
}

function signedOut() {
  if (!$("login").hidden) return;
  toast("登录已失效，请重新扫码", true);
  showLogin();
}

// 二维码 2 分钟过期；页面开着时自动换新的，最多换 5 次（约 10 分钟），之后点一下再换。
async function newTicket(round) {
  const run = ++ticketRun;
  const over = $("qrOver");
  over.hidden = false;
  over.replaceChildren(el("span", "", "正在生成二维码…"));
  let answer;
  try {
    const response = await fetch("api/login/ticket", { method: "POST", credentials: "same-origin" });
    answer = await response.json();
  } catch (error) { answer = { ok: false, error: "网络不通，过一会儿再试" }; }
  if (run !== ticketRun) return;
  if (!answer.ok) { expired(answer.error); return; }
  const old = $("qr").querySelector("svg");
  if (old) old.remove();
  $("qr").insertAdjacentHTML("afterbegin", answer.svg);   // 服务器画的二维码，只有数字和固定颜色
  over.hidden = true;
  $("code").textContent = answer.code;
  const deadline = Date.now() + answer.ttl * 1000;
  const state = $("loginState");
  const countdown = setInterval(() => {
    if (run !== ticketRun) { clearInterval(countdown); return; }
    const left = Math.round((deadline - Date.now()) / 1000);
    if (state.dataset.fixed) return;
    state.className = "meta2";
    state.textContent = left > 0 ? "等待手机确认 · " + clock(left) + " 后过期" : "过期了";
  }, 1000);
  state.dataset.fixed = "";
  state.className = "meta2";
  state.textContent = "等待手机确认 · " + clock(answer.ttl) + " 后过期";
  while (run === ticketRun) {
    let result;
    const began = Date.now();
    try {
      const response = await fetch("api/login/poll", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ poll: answer.poll }) });
      result = await response.json();
    } catch (error) { result = { state: "pending" }; await sleep(3000); }
    if (run !== ticketRun) return;
    if (result.state === "ok") {
      clearInterval(countdown);
      state.className = "meta2 ok";
      state.dataset.fixed = "1";
      state.textContent = "手机已确认，正在登录…";
      ticketRun++;
      boot();
      return;
    }
    if (result.state === "denied") { clearInterval(countdown); expired("手机上点了取消"); return; }
    if (result.state === "expired") {
      clearInterval(countdown);
      if (round < 5 && !document.hidden) { newTicket(round + 1); return; }
      expired("二维码过期了");
      return;
    }
    if (Date.now() - began < 1500) await sleep(2000);   // 服务器忙时会立刻回答「还在等」
  }
}

function expired(text) {
  const over = $("qrOver");
  over.hidden = false;
  over.replaceChildren(el("span", "", text), button("刷新二维码", "go", () => newTicket(0)));
  $("code").textContent = "··· ···";
  $("loginState").textContent = "";
}

async function logout() {
  try { await call("POST", "api/logout"); } catch (error) { /* 反正要回到登录页 */ }
  showLogin();
}
$("logout2").onclick = logout;

// ======================================================================= 外框：路由、顶栏、底栏

const TITLES = { home: "首页", live: "直播", series: "剧集与番剧", up: "UP 主", lib: "片库", search: "搜索",
                 devices: "设备与遥控", settings: "设置", remote: "遥控", me: "我的", admin: "管理" };
const GUEST_ROUTES = ["home", "live", "search", "settings", "remote", "me"];
const TOPT = [["home", "首页"], ["live", "直播"], ["series", "剧集"], ["up", "UP 主"], ["lib", "片库"], ["search", "搜索", "search"]];
const TABS = [["home", "首页", "home"], ["live", "直播", "live"], ["search", "搜索", "search"], ["remote", "遥控", "remote"], ["me", "我的", "set"]];

function showApp() {
  ticketRun++;
  $("login").hidden = true;
  $("app").hidden = false;
  for (const b of qsa(".noguest")) b.hidden = isGuest();
  $("adminList").hidden = !isAdmin();            // 「管理」（console-admin.js）：服务端也只给管理员
  chosen = lsGet("console.device", "");
  targetPref = lsGet("rootv.target:" + me.name, "");
  remember = lsGet("rootv.remember", "1") !== "0";
  $("rememberSw").setAttribute("aria-checked", String(remember));
  $("aboutIcon").innerHTML = iconSVG(30, true);
  applyLayout(true);
  if (!location.hash || location.hash === "#") {
    const saved = lsGet("rootv.route", "home");
    history.replaceState(null, "", "#/" + (TITLES[saved] ? saved : "home"));
  }
  showRoute();
  refresh();
  loadHome(true);
  loadTargets();
  schedule(5000);
}

function routeParts() {
  const h = decodeURIComponent(location.hash.slice(1)) || "/home";
  return h.split("?")[0].split("/").filter(Boolean);
}
function parseRoute() { return routeParts()[0] || "home"; }

function showRoute() {
  if (!me) return;
  let name = parseRoute();
  if (!TITLES[name]) name = "home";
  if (isGuest() && !GUEST_ROUTES.includes(name)) name = "home";
  if (name === "admin" && !isAdmin()) name = "home";
  // 进不去的地址（访客、不是管理员打开 #/admin）：地址栏也改成真正显示的那一页。
  if (name !== parseRoute()) { history.replaceState(null, "", "#/" + name); lastHash = location.hash; }
  const was = route;
  route = name;
  lsSet("rootv.route", name === "remote" || name === "me" ? "home" : name);
  for (const p of qsa(".page")) p.classList.toggle("on", p.dataset.route === name);
  $("app").classList.toggle("onremote", name === "remote");
  placeRemote();
  renderTop();
  renderTabs();
  $("view").classList.toggle("bleed", name === "home" && !!(homeData && homeData.hero && homeData.hero.length));
  document.title = TITLES[name] + " · " + BRAND_ZH;
  if (was !== name) $("main").scrollTop = 0;
  updateTop();
  if (name === "home") { renderHome(); loadHome(); }
  if (name === "live" && was !== name) loadEpg();
  if (name === "devices") loadDevices();
  if (name === "settings") { if (!isGuest()) loadFeeds(true);
    if (!isGuest()) loadSessions(); if (was !== name) ytBrowsers = null; loadYouTube(); loadBili(); renderAbout(); settingsNav(); }
  if (typeof adminRoute === "function") adminRoute(name === "admin" ? routeParts()[1] || "" : null);
  if (name !== "settings") biliCancel();
  if (name === "me") renderMe();
  if (name === "lib") libNav();
  if (name === "up") { renderUpLatest(); renderFollows(); loadFollows(); }
  if (name === "home") loadFeeds();
  if (name === "up" || name === "series") loadHome();
  if (data) render();
}
// 「管理 → 频道表」有没保存的修改时，离开前先问（console-admin.js 的 adminCanLeave；不离开就把地址改回去）。
let lastHash = location.hash;
addEventListener("hashchange", () => {
  if (typeof adminCanLeave === "function" && !adminCanLeave(location.hash)) { history.replaceState(null, "", lastHash); return; }
  lastHash = location.hash;
  closeSheets(true);
  closeGuidePop(false);
  showRoute();
});

function tabOf(name) { return name; }
function renderTop() {
  const cur = tabOf(route);
  const cr = (id) => (cur === id ? 'aria-current="page"' : "");
  const tabs = TOPT.filter(([id]) => !isGuest() || GUEST_ROUTES.includes(id));
  const initial = me ? [...me.name][0] || "?" : "?";
  const role = me ? (me.role === "admin" ? "管理员" : me.role === "guest" ? "访客 · 只能遥控一台" : "家人") : "";
  $("top").innerHTML = `<div class="tl"><a class="wml" href="#/home" aria-label="${BRAND} ${BRAND_ZH} 首页">${iconSVG(40, true)}${wordmarkSVG(28, false)}</a></div>
    <nav class="ttabs" aria-label="主导航">${tabs.map(([id, t, i]) => `<a href="#/${id}" ${cr(id)}>${i ? ic(i, 17) : ""}${t}</a>`).join("")}</nav>
    <div class="tr">${chipHTML()}<div class="tbtns">${isGuest() ? "" : `<a class="iconbtn" href="#/devices" aria-label="设备与遥控" title="设备与遥控" ${cr("devices")}>${ic("tv", 20)}</a>`}${isAdmin() ? `<a class="iconbtn" href="#/admin" aria-label="管理" title="管理：用户、设备、频道表" ${cr("admin")}>${ic("sliders", 20)}</a>` : ""}<a class="iconbtn" href="#/settings" aria-label="设置" title="设置" ${cr("settings")}>${ic("set", 20)}</a></div><a class="me" href="#/settings" title="${esc((me ? me.name : "") + " · " + role)}" aria-label="当前用户：${esc(me ? me.name : "")}，${role}">${esc(initial)}</a></div>
    <span class="ntitle" aria-hidden="true">${esc(TITLES[route])}</span>`;
  $("top").classList.toggle("home", route === "home");
}
function renderTabs() {
  const cur = ["series", "up", "lib", "devices", "settings", "admin"].includes(route) ? "me" : route;
  $("tabs").innerHTML = TABS.map(([id, t, i]) => `<a href="#/${id}" ${id === cur ? 'aria-current="page"' : ""}>${ic(i, 24)}${t}</a>`).join("");
}
// 滚动时：顶栏从透明变成毛玻璃；手机上大标题滚走后，导航栏中间出现小标题。
function updateTop() {
  const st = $("main").scrollTop, solid = st > 8;
  const hero = route === "home" && $("view").classList.contains("bleed");
  $("top").classList.toggle("solid", solid);
  $("top").classList.toggle("dk", hero && !solid);
  $("top").classList.toggle("tt", st > 44);
}
$("main").addEventListener("scroll", () => requestAnimationFrame(updateTop), { passive: true });

function applyLayout(force) {
  const m = innerWidth < 700, was = isM();
  $("app").classList.toggle("m", m);
  $("app").classList.toggle("d", !m);
  if (force || was !== m) { if (me) { renderTop(); renderTabs(); placeRemote(); } }
}
addEventListener("resize", () => applyLayout(false));

function renderMe() {
  const rows = [["series", "剧集与番剧", "series"], ["up", "UP 主", "up"], ["lib", "片库", "lib"], ["live", "直播和分组", "live"],
                ["devices", "设备", "tv"], ["settings", "设置", "set"], ["admin", "管理", "sliders"]]
    .filter(([id]) => (!isGuest() || GUEST_ROUTES.includes(id)) && (id !== "admin" || isAdmin()));
  $("meList").innerHTML = rows.map(([id, t, i]) => `<a class="li" href="#/${id}"><span class="ico">${ic(i, 18)}</span><span class="tx">${t}</span><em>›</em></a>`).join("")
    + `<div class="li"><span class="me">${esc(me ? [...me.name][0] : "?")}</span><span class="tx">${esc(me ? me.name : "")}<small>${me && me.role === "admin" ? "管理员" : isGuest() ? "访客" : "家人"}</small></span></div>`;
}
function renderAbout() {
  $("aboutText").innerHTML = `${esc(BRAND_ZH)} 电脑版<small>${esc(BRAND)} · ${data && data.watch ? "这台电脑就是家里的电视服务器" : "在浏览器里管理和遥控你的小电视"}</small>`;
}

// 设置分成几个标签（#/settings/<标签>），一次只显示一个。B 站、YouTube 那两个标签跟着各自的块走：
// biliBlock、ytBlock、ytLocal 的 hidden 由 loadBili、renderYouTube 等管，这里只看结果（下面的 MutationObserver）。
// 地址里的标签还没出现（比如刚打开、B 站状态还没读到）时先显示「通用」，不改地址，读到了就换过去。
const SET_TABS = [["general", "通用", "set"], ["account", "账号与登录", "up"], ["bili", "B 站", "tv"],
                  ["youtube", "YouTube", "play"], ["history", "放过的", "hist"]];
let setTab = "", setNavHTML = "";
function setTabOn(id) {
  if (id === "bili") return !$("biliBlock").hidden;
  if (id === "youtube") return !$("ytBlock").hidden || !$("ytLocal").hidden;
  if (id === "history") return !isGuest();
  return true;
}
function settingsNav() {
  if (!me) return;
  const want = route === "settings" ? routeParts()[1] || "" : "";
  const tabs = SET_TABS.filter(([id]) => setTabOn(id));
  const cur = tabs.some(([id]) => id === want) ? want : "general";
  const html = tabs.map(([id, t, i]) => `<a href="#/settings/${id}" data-k="set-${id}"${id === cur ? ' aria-current="page"' : ""}>${ic(i, 16)}${t}</a>`).join("");
  if (html !== setNavHTML) {
    const k = focusKey();      // 用键盘在标签上按回车：重画后焦点还在那个标签上
    $("setNav").innerHTML = setNavHTML = html;
    restoreFocus(k);
  }
  for (const pane of qsa("#app .spane")) pane.hidden = pane.dataset.pane !== cur;
  if (cur !== setTab && route === "settings") $("main").scrollTop = 0;
  setTab = cur;
}
new MutationObserver(() => settingsNav()).observe($("biliBlock"), { attributes: true, attributeFilter: ["hidden"] });
for (const id of ["ytBlock", "ytLocal"]) new MutationObserver(() => settingsNav()).observe($(id), { attributes: true, attributeFilter: ["hidden"] });

// ======================================================================= 数据

async function refresh() {
  if ($("app").hidden) return;
  const id = chosen;
  try {
    data = await call("GET", "api/state" + (id ? "?device=" + encodeURIComponent(id) : ""));
  } catch (error) {
    if (!(error instanceof SignedOut)) return;
    if (id) { chosen = ""; return refresh(); }   // 选的设备没了（解绑、换了主人）
    signedOut();
    return;
  }
  fetchedAt = Date.now();
  settlePending();
  claimField();
  render();
  loadEpg();
}

let timer = 0, ticks = 0;
function schedule(delay) {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    if (!document.hidden) {
      await refresh();
      ticks++;
      if (ticks % 3 === 0) loadTargets();
      if (route === "home" && (ticks % 6 === 0 || homeDue())) loadHome(true);
      if (route === "home" && ticks % 12 === 0) loadFeeds();
    }
    schedule(pending || listenPending ? 1000 : 5000);
  }, delay);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("app").hidden) { refresh(); loadTargets(); } });

// 首页「直播 · 正在播」里有节目到点了（正在播的结束了）：马上再问，不等半分钟。
function homeDue() {
  const now = Date.now() / 1000;
  return !!homeData && Date.now() - homeAt > 15000 && (homeData.live_now || []).some((x) => x.live && x.live.end && x.live.end <= now);
}
async function loadHome(force) {
  if (!me) return;
  if (!force && homeData && Date.now() - homeAt < 20000) return;
  homeAt = Date.now();
  let answer;
  try { answer = await call("GET", "api/home"); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  if (!answer || !answer.ok) return;
  homeData = answer;
  if (route === "home") { renderHome(); $("view").classList.toggle("bleed", !!homeData.hero.length); updateTop(); }
  if (route === "up") renderUpLatest();
  if (route === "series" && data) renderSeriesGrid();
}

async function loadTargets() {
  if (!me) return;
  try { targetsData = await call("GET", "api/targets"); targetsAt = Date.now(); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  renderChips();
  if (!$("sheet").hidden) renderTargetRows();
}

// ---------- 节目单 ----------
// 服务器打开了节目单（/api/state 的 epg，server/relay/epg.py）才问 /api/epg。卡片、遥控屏幕、播放器标题用
// now / next：只问看得见的那一组台和正在播的台，一分钟一次，某个台的节目到点换了就马上再问（不等一分钟）。
// 直播 → 节目表另问一段时间（from / to，每台的节目列表），见下面的 loadGuide。

// ---- 节目表的几个纯函数（tests/console_guide_test.mjs 截出这一段单独测）----
// SPANS：节目表往后看几个小时（可选）；BEFORE_S：往前看多久（服务器只留前 1 小时）；PPM：每分钟几像素。
const GUIDE = { BEFORE_S: 3600, SPANS: [3, 6, 12], SPAN: 6, STEP_S: 1800, PPM: 5, CHW: 210, ROWS: 120, CHUNK: 60, MAX: 64 };
// 节目表的时间段：现在前 1 小时（往前取整到半点）到现在后 spanH 小时（往后取整到半点），夹在服务器留的那段
// （/api/epg 回答的 window：{before, after} 秒）里。每过半点才变一次，所以不会每分钟重新问。
function guideWindow(now, spanH, win) {
  const before = Math.min(GUIDE.BEFORE_S, win && win.before >= 0 ? win.before : GUIDE.BEFORE_S);
  const after = win && win.after > 0 ? win.after : 24 * 3600;
  const from = Math.floor((now - before) / GUIDE.STEP_S) * GUIDE.STEP_S;
  const to = Math.max(from + GUIDE.STEP_S, Math.min(Math.ceil((now + spanH * 3600) / GUIDE.STEP_S) * GUIDE.STEP_S, Math.floor(now + after)));
  return { from, to };
}
// 时间尺上的刻度：每半点一个。
function guideTicks(from, to, step) {
  const out = [];
  for (let t = Math.ceil(from / step) * step; t < to; t += step) out.push(t);
  return out;
}
// 一个台的节目 → 网格里的块（像素）：left、width，state 是 past / now / future，正在播的有进度 pct（0–1）。
// 只要和 [from, to) 有重叠的；两个节目时间重叠（节目单写错）时后一个从前一个结束处开始。
function guideBlocks(list, from, to, now, ppm) {
  const out = [];
  let edge = from;
  const sorted = (list || []).filter((p) => p && p.end > p.start).slice().sort((a, b) => a.start - b.start);
  for (const p of sorted) {
    const s = Math.max(p.start, from, edge), e = Math.min(p.end, to);
    if (e <= s) continue;
    edge = e;
    const state = p.end <= now ? "past" : p.start > now ? "future" : "now";
    out.push({ p, left: (s - from) / 60 * ppm, width: (e - s) / 60 * ppm, state,
               pct: state === "now" ? (now - p.start) / (p.end - p.start) : 0 });
  }
  return out;
}
// 一串节目（节目表的一段，或者只是 [now, next]，可以有 null 和重复）里现在播的和下一个。
function nowNext(list, now) {
  const items = (list || []).filter((p) => p && p.end > p.start);
  const cur = items.find((p) => p.start <= now && now < p.end) || null;
  const after = cur ? cur.end : now;
  let next = null;
  for (const p of items) if (p.start >= after && (!next || p.start < next.start)) next = p;
  return { now: cur, next };
}
// 缓存的 now / next 里有没有已经过了点的（正在播的结束了，或者下一个开始了）：有就该马上再问。
function epgStale(map, ids, now) {
  return ids.some((id) => {
    const e = map[id];
    if (!e) return false;
    return (e.now && e.now.end <= now) || (!e.now && e.next && e.next.start <= now);
  });
}
// 正在播的进度（0–1）。
function progressOf(p, now) { return p && p.end > p.start ? Math.min(1, Math.max(0, (now - p.start) / (p.end - p.start))) : 0; }
// 卡片第二行：「正在播：运动一起赢 · 20:00–21:00」。fmt 把 Unix 秒写成「时:分」。
function epgText(p, fmt) { return p ? "正在播：" + p.title + " · " + fmt(p.start) + "–" + fmt(p.end) : ""; }
// 横向滚动时，被左边切掉一截的节目块把标题挪到看得见的地方：返回块的左内边距（像素）。
function pinPad(left, width, scrollX) { return Math.max(12, Math.min(scrollX - left + 12, width - 60)); }
// ---- 节目表的纯函数到此 ----

let epg = {}, epgAt = 0, epgAsked = new Set(), epgBusy = false, epgWin = null;
// 要问的台：直播页这一组里的直播台，加上小电视和电脑上正在播的台（UP 主、剧集、投屏台没有节目单）。
function epgIds() {
  const ids = new Set();
  if (route === "live") {
    const g = data.groups.find((x) => x.name === group);
    if (g) for (const c of g.channels) if (!isRelay(c.id)) ids.add(c.id);
  }
  for (const id of [data.now && data.now.channel, watching && watching.channel]) if (id && !isRelay(id)) ids.add(id);
  return [...ids].slice(0, 200);
}
async function loadEpg(force) {
  if (!data || !data.epg) { epg = {}; epgAsked = new Set(); guideData = null; return; }
  const ids = epgIds();
  if (!ids.length || epgBusy) return;
  const since = Date.now() - epgAt;
  const due = force || since >= 60000 || ids.some((id) => !epgAsked.has(id)) || (since >= 15000 && epgStale(epg, ids, Date.now() / 1000));
  if (!due) return;
  epgBusy = true;
  let answer = null;
  try { answer = await call("GET", "api/epg?channels=" + ids.map(encodeURIComponent).join(",")); } catch (error) { /* 下次再问 */ }
  epgBusy = false;
  if (!answer || !answer.ok) return;
  epgAt = Date.now();
  if (answer.window) epgWin = answer.window;
  for (const id of ids) {
    epgAsked.add(id);
    const e = (answer.channels || {})[id];
    if (e) epg[id] = { now: e.now, next: e.next }; else delete epg[id];
  }
  epgChanged();
}
function epgChanged() {
  if (!data) return;
  renderChannels();
  renderGuide();
  renderWatchTitle();
  renderScreen();
  renderMini();
}
function epgNow(id) {
  const e = data && data.epg ? epg[id] : null;
  const g = guideData && guideData.channels[id];
  if (!e && !g) return { now: null, next: null };
  return nowNext((g || []).concat(e ? [e.now, e.next] : []), Date.now() / 1000);
}
// 「正在播：新闻联播 · 19:00–19:30」；没有节目单的台为空。
function epgLine(id) { return epgText(epgNow(id).now, hm); }

function render() {
  if (!data) return;
  renderNotice();
  renderChips();
  renderRemoteDevs();
  renderScreen();
  renderPlayer();
  renderDials();
  renderPets();
  renderWatch();
  renderMini();
  if (route === "live") renderLive();
  if (route === "series") renderSeriesGrid();
  renderSubs();
  renderHistory();
  if (route === "search") renderSearchSource();
  if (route === "lib" && libTab === "history" && (!hist.state || (hist.state === "off" && feedsOn()))) loadHist(false, true);
  followEpisodes();
  renderBiliGate();
  if (route === "settings") renderAbout();
  if (feedsOn() && !feedsInfo && !feedsAt) loadFeeds();
  if (route === "up" && feedsOn() && !fw && $("followsBox").hidden) { renderFollows(); loadFollows(); }
  if (feedsInfo) applyFeedSettings();
  ytPrefetchView();
}

function renderNotice() {
  const state = data.youtube ? data.youtube.state : "";
  const local = data.youtube && data.youtube.kind === "local";
  const text = state === "login" ? "YouTube 需要更新 cookies（管理员用小号重新导出，见使用说明），B 站不受影响"
    : state === "unreachable" ? (local ? "YouTube 暂时不可用（本机的 YouTube 组件没有响应）" : "YouTube 暂时不可用（连不上海外节点）") : "";
  $("castYt").hidden = !data.youtube_off;
  $("notice").hidden = !text || route === "home";
  $("noticeText").textContent = text;
  $("ytBlock").hidden = !data.youtube || !$("ytLocal").hidden;
  if (data.youtube) $("ytState").textContent = { ok: "正常", login: "cookies 过期了，需要重新导出", error: "出错了",
    unreachable: "连不上", unknown: "还不知道" }[state] || state;
}

// 遥控类的请求都点名设备：电脑选的那台，不影响手机。
function deviceId() {
  if (!data) return "";
  const d = (data.devices || []).find((x) => x.id === chosen) || (data.devices || []).find((x) => x.target);
  return d ? d.id : "";
}
function deviceName() {
  const d = data && (data.devices || []).find((x) => x.id === deviceId());
  return d ? d.name : "小电视";
}
function withDevice(body) {
  const id = deviceId();
  return id ? Object.assign({ device: id }, body || {}) : (body || {});
}
function pick(id) {
  chosen = id;
  lsSet("console.device", id);
  pending = null;
  group = "";      // 跟着这台正在播的台所在的组
  refresh();
}
function hasTv() { return !!(data && (data.devices || []).length); }
function tvOnline() { return !!(data && data.now.online && data.now.channel); }
function groupOf(id) {
  const g = data && data.groups.find((x) => x.channels.some((c) => c.id === id));
  return g ? g.name : "";
}

// ======================================================================= 播放到

function canPc() { return !!(data && data.watch) || !!(targetsData && targetsData.targets.some((t) => t.id === PC)); }
function tvRows() {
  if (targetsData) return targetsData.targets.filter((t) => t.kind === "tv");
  return ((data && data.devices) || []).map((d) => ({ id: d.id, kind: "tv", name: d.name, online: d.online, auto: d.target }));
}
// 现在点「播放」会播到哪：记住的那个（还在的话），否则服务器的「自动」那台，没有小电视时是这台电脑。
function currentTarget() {
  const tvs = tvRows();
  if (targetPref === PC && canPc()) return PC;
  if (targetPref && targetPref !== PC && tvs.some((d) => d.id === targetPref)) return targetPref;
  const auto = tvs.find((d) => d.auto) || tvs[0];
  if (auto) return auto.id;
  return canPc() ? PC : "";
}
function targetRow(id) { return id === PC ? { id: PC, kind: "pc", name: "这台电脑", online: true } : tvRows().find((d) => d.id === id) || null; }
function targetName(id) { const r = targetRow(id); return r ? r.name : "小电视"; }
function targetOnline(id) { const r = targetRow(id); return !!(r && r.online); }
function setTarget(id, quiet) {
  targetPref = id;
  if (me) lsSet("rootv.target:" + me.name, id);
  if (id && id !== PC && id !== chosen) pick(id);
  renderChips();
  if (!quiet) toast("以后播放到「" + targetName(id) + "」");
}
function chipHTML() {
  const id = currentTarget();
  if (!id) return isGuest() ? "" : `<a class="tchip" data-k="chip" href="#/devices"><span class="dot off"></span><small>播放到</small><span class="nm">还没有小电视</span></a>`;
  const r = targetRow(id) || { name: "小电视", online: false };
  const short = isM() ? (id === PC ? "电脑" : r.name) : r.name;
  return `<button class="tchip" data-k="chip" data-sheet="target" aria-haspopup="dialog" aria-label="播放到：${esc(r.name)}，点一下换">
    <span class="dot ${r.online ? "" : "off"}"></span><small>播放到</small><span class="nm">${esc(short)}</span>${ic("down", 15)}</button>`;
}
function renderChips() {
  const html = chipHTML();
  for (const c of qsa("#top .tchip, #mini .tchip")) {
    if (c.dataset.sig === html) continue;
    const had = document.activeElement === c;
    c.outerHTML = html;
    const fresh = qsa("#top .tchip, #mini .tchip").find((x) => !x.dataset.sig);
    if (fresh) { fresh.dataset.sig = html; if (had) fresh.focus({ preventScroll: true }); }
  }
}

// 点了「播放」：卡片（封面上）或按钮上马上转圈，直到 play 有了结果——播到小电视时要等中转取好片源才回答，
// 以前这几秒什么都不动，像是没点上；在电脑上看的窗口是马上开的，这里转到中转回答为止。
function pressed(node, promise) {
  if (!node || !promise || typeof promise.then !== "function" || node.classList.contains("opening")) return promise;
  node.classList.add("opening");
  node.setAttribute("aria-busy", "true");
  const art = node.classList.contains("card") || node.classList.contains("hrow") ? node.querySelector(".art") : null;
  const spin = art ? art.appendChild(el("span", "opspin")) : null;
  const done = () => { node.classList.remove("opening"); node.removeAttribute("aria-busy"); if (spin) spin.remove(); };
  promise.then(done, done);
  return promise;
}

// 播放一个节目（/api/home 的 play，或者页面自己拼的 {kind, channel, …}）：播到 to，不给就是记住的地方。
async function play(item, to, label, poster) {
  if (!item || !me) return null;
  if (!to) {
    to = currentTarget();
    if (!to) { openTarget(item, "", label, poster); return null; }
    if (!remember) { openTarget(item, "", label, poster); return null; }
    if (!targetOnline(to)) { openTarget(item, targetName(to) + "没在线，换一个地方", label, poster); return null; }
  }
  if (remember && to !== targetPref) setTarget(to, true);
  if (to === PC) return playOnPc(item, label, poster);
  // 中转要先取好片源才回答（YouTube 要几秒到十几秒）：先说一声在做，回答到了换成结果。
  toast("正在发给「" + targetName(to) + "」" + (label ? "：「" + label + "」" : "") + "…", false, 20000);
  let answer;
  try { answer = await call("POST", "api/play", { target: to, item }); }
  catch (error) { untoast(); if (error instanceof SignedOut) signedOut(); else toast("网络不通", true); return null; }
  untoast();
  if (!answer.ok) {
    if (answer.reason === "offline") { openTarget(item, answer.error, label); return answer; }
    toast(answer.error, true);
    return answer;
  }
  miniWho = "tv";
  if (to !== chosen) { chosen = to; lsSet("console.device", to); }
  const what = label || answer.title || answer.name || "";
  toast(answer.note || ("正在让「" + (answer.target_name || targetName(to)) + "」播放" + (what ? "「" + what + "」" : "")));
  if (answer.at) {
    startPending(answer.channel || "", answer.name || what || answer.channel, answer.at, true,
                 answer.cut ? "seek" : "switch", null, answer.episode ? "换到第 " + answer.episode + " 集…" : null);
  } else refresh();
  setTimeout(() => { loadHome(true); loadTargets(); }, 2500);
  return answer;
}
async function playOnPc(item, label, poster) {
  if (watching) watching.busy = true;
  // 不等中转回答：窗口马上打开，封面上写着在做什么（直播台是上一台的画面停住、「正在切换…」），可以直接关掉。
  const n = pcOpening(item, label, poster);
  const body = vodAsk({ target: PC, item, viewer });
  if (item.channel === "cast" && deviceId()) body.device = deviceId();
  let answer;
  try { answer = await call("POST", "api/play", body); }
  catch (error) {
    if (!openingIs(n)) return null;
    opening = null;
    if (watching) watching.busy = false;
    openFailed();
    if (error instanceof SignedOut) signedOut(); else toast("网络不通", true);
    return null;
  }
  if (!openingIs(n)) {
    // 等回答时关掉了窗口（取消）：中转那边刚开的这一路马上停掉。换成了别的节目时不管（新的那次会顶掉它）。
    if (answer.ok && opening === null && !watching) call("POST", "api/watch/stop", { viewer }).catch(() => {});
    return answer;
  }
  opening = null;
  if (!answer.ok) {
    if (watching) watching.busy = false;
    openFailed();
    toast(answer.error, true);
    return answer;
  }
  miniWho = "pc";
  openDirect(answer, { group: groupOf(answer.channel) });
  if (label) toast("在这台电脑上播放「" + label + "」");
  return answer;
}

// 「播放到」面板：每台小电视（在线、在播什么）+ 这台电脑；item 给了就是「选一个地方马上播」。
let pendingItem = null, pendingLabel = "", pendingPoster = "";
function openTarget(item, message, label, poster) {
  pendingItem = item || null;
  pendingLabel = label || "";
  pendingPoster = poster || "";
  $("tgTitle").textContent = "播放到";
  $("tgSub").textContent = item ? "选一个地方，马上开始播" : "以后点「播放」默认播到这里";
  $("tgBody").dataset.msg = message || "";
  renderTargetRows();
  openSheet($("sheet"));
  if (!targetsData || Date.now() - targetsAt > 3000) loadTargets();
}
function targetSub(t) {
  if (t.kind === "pc") {
    return watching ? "窗口里正在放 " + (watching.title || watching.name || "") : "在电脑上看，不需要小电视";
  }
  if (!t.online) return "离线" + (t.last_seen ? " · 最后在线 " + ago(t.last_seen) : "") + "，看看电源和 Wi-Fi";
  const n = t.now;
  const what = n && n.channel ? (n.name || n.channel) + (n.title && n.title !== n.name ? " · " + n.title : "") : "";
  return (t.model_name ? t.model_name + " · " : "") + (t.listen ? "只听 · " : "") + (what ? (n.paused ? "已暂停 " : "正在播 ") + what : "在线");
}
function renderTargetRows() {
  const body = $("tgBody");
  const cur = currentTarget();
  const rows = targetsData ? targetsData.targets : tvRows().concat(canPc() ? [{ id: PC, kind: "pc", name: "这台电脑", online: true }] : []);
  const msg = body.dataset.msg;
  let pend = "";
  if (pendingItem) {
    const t = pendingLabel || pendingItem.title || "";
    pend = `<div class="pending"><span class="mart">${art(t || "节目", "w")}</span><span><b>${esc(t || "这个节目")}</b><span>${esc(pendingItem.subtitle || "")}</span></span></div>`;
  }
  const list = rows.map((t) => {
    const on = t.online, picked = t.id === cur;
    return `<button class="tg ${on ? "" : "off"}" role="radio" aria-checked="${picked}" data-tg="${esc(t.id)}" ${on ? "" : 'aria-disabled="true"'}>
      <span class="ic">${ic(t.kind === "pc" ? "pc" : "tv", 24)}</span><span><b><span class="dot ${on ? "" : "off"}"></span>${esc(t.name)}${t.id === targetPref ? ' <span class="last">上次用的</span>' : ""}</b><span class="sub">${esc(targetSub(t))}</span></span>
      <span class="chk">${picked ? ic("check") : ""}</span></button>`;
  }).join("");
  const none = !rows.length ? `<div class="offbox">${ic("tv", 48)}<b>还没有小电视</b><p>${isGuest() ? "" : "到「设备」页输入小电视屏幕上的绑定码，所有「播放」就能播到它。"}</p></div>` : "";
  const bind = !isGuest() && !rows.some((t) => t.kind === "tv") && rows.length ? `<p class="hint">还没有小电视。<a href="#/devices" class="linkbtn">绑定一台 ›</a></p>` : "";
  body.innerHTML = (msg ? `<p class="pmsg">${esc(msg)}</p>` : "") + pend + `<div role="radiogroup" aria-label="播放到">${list}</div>` + none + bind
    + `<div class="opt"><span>记住这次的选择<small>下次点「播放」直接播到这里，不再问</small></span><button class="sw" role="switch" aria-checked="${remember}" data-remember aria-label="记住这次的选择"></button></div>`
    + (pendingItem ? "" : `<p class="hint">换了以后，所有「播放」按钮都播到新选的地方；正在播的不会被打断。</p>`);
}
function setRemember(on) {
  remember = on;
  lsSet("rootv.remember", on ? "1" : "0");
  for (const s of qsa("[data-remember], #rememberSw")) s.setAttribute("aria-checked", String(on));
}
$("rememberSw").onclick = () => setRemember(!remember);

// ======================================================================= 抽屉、弹窗

let lastFocus = null;
const SHEETS = ["sheet", "rsheet", "drawer", "csheet"];
function anyOverlay() { return SHEETS.some((id) => !$(id).hidden) || !$("modal").hidden; }
function openSheet(node) {
  if (!anyOverlay()) lastFocus = document.activeElement;
  for (const id of SHEETS) if ($(id) !== node) $(id).hidden = true;
  $("modal").hidden = true;
  node.hidden = false;
  $("scrim").hidden = false;
  const f = node.querySelector('[aria-checked="true"].tg, .shb button:not(:disabled), .shb input, [data-close]');
  if (f) f.focus({ preventScroll: true });
}
function closeSheets(quiet) {
  for (const id of SHEETS) $(id).hidden = true;
  $("modal").hidden = true;
  $("modal").className = "";
  $("scrim").hidden = true;
  pendingItem = null;
  eps = null;
  if (typeof closeWifi === "function" && wifiDev) closeWifi();
  if (follow) follow = null;
  if (ysub) { ysub = null; clearTimeout(ysubPoll); }
  cs = null;
  if (!quiet && lastFocus && document.contains(lastFocus)) lastFocus.focus({ preventScroll: true });
  lastFocus = null;
}
$("scrim").onclick = () => closeSheets();
function modal(title, ...nodes) {
  const box = $("modal");
  if (!anyOverlay()) lastFocus = document.activeElement;
  for (const id of SHEETS) $(id).hidden = true;
  box.replaceChildren(el("h3", "", title), ...nodes);
  $("scrim").hidden = box.hidden = false;
  const f = box.querySelector("input, button");
  if (f) f.focus({ preventScroll: true });
}
const closeOverlays = () => closeSheets();

// ======================================================================= 遥控

function openRemote(id) {
  if (id && id !== PC && id !== deviceId()) pick(id);
  if (isM() || route === "remote") { if (route !== "remote") location.hash = "#/remote"; return; }
  openSheet($("rsheet"));
}
// 手机的「遥控」标签：遥控面板挪到页面里；别的时候它在右边的抽屉里。
function placeRemote() {
  const panel = $("rpanel"), slot = route === "remote" ? $("remoteSlot") : $("rhost");
  if (panel.parentNode !== slot) slot.appendChild(panel);
  if (route === "remote") $("rsheet").hidden = true;
}
function renderRemoteDevs() {
  const box = $("rdevs");
  const list = data.devices || [];
  box.hidden = list.length < 2;
  const current = deviceId();
  const html = list.map((d) => `<button data-rdev="${esc(d.id)}" data-k="rd-${esc(d.id)}" aria-pressed="${d.id === current}"><span class="dot ${d.online ? "" : "off"}"></span>${esc(d.name)}</button>`).join("");
  if (box.dataset.sig !== html) {
    const k = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.k : "";
    box.innerHTML = html;
    box.dataset.sig = html;
    const again = k && box.querySelector(`[data-k="${CSS.escape(k)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
  const none = !list.length;
  $("rnone").hidden = !none;
  $("rbody").hidden = none;
  if (none) {
    $("rnone").innerHTML = `${ic("tv", 48)}<b>还没有小电视</b><p>遥控是给小电视用的。${data.watch ? "在这台电脑上看，选「播放到 → 这台电脑」就行。" : ""}</p>${isGuest() ? "" : '<a class="btn pri sm" href="#/devices">绑定小电视</a>'}`;
  }
}

function startPending(channel, name, at, slow, kind, target, label) {
  pending = { kind: kind || "switch", channel, name, at, target, label, deadline: Date.now() + (slow ? 45000 : 20000) };
  render();
  schedule(800);
}

function settlePending() {
  if (!pending) return;
  const now = data.now, v = now.video;
  const done = pending.kind === "seek"
    ? now.channel !== pending.channel || (v && !v.paused && v.started >= pending.at - 1)
    : now.online && (!pending.channel || now.channel === pending.channel) && now.since >= pending.at - 1;
  if (done) pending = null;
  else if (Date.now() > pending.deadline) {
    toast(pending.kind === "seek" ? "跳转好像没成功，再试一次" : "切换好像没成功，再点一次试试", true);
    pending = null;
  }
}

function nowTitle(now) {
  if (!now.episode) return now.title || "";
  return "第 " + now.episode + " / " + now.total + " 集" + (now.title ? " · " + now.title : "");
}

function renderScreen() {
  const now = data.now, screen = $("screen");
  let cls = "rscreen", ch = "CH " + pad(now.n || 0), grp = now.group, nm = now.name || now.channel, ti = nowTitle(now);
  if (pending && pending.kind === "seek") { cls += " switching"; ti = pending.label || "跳转中…"; }
  else if (pending) { cls += " switching"; ch = "CH ··"; grp = ""; nm = "正在切到「" + pending.name + "」"; ti = "切换中…"; }
  else if (now.online && now.video && now.video.paused) ti = "已暂停 · " + nowTitle(now);
  else if (!(now.online && now.channel)) {
    cls += " off"; ch = "— —"; grp = "";
    const none = !hasTv();
    nm = none ? "还没有绑定小电视" : deviceName() + "没在线";
    ti = data.watch ? "现在点任何节目，就在这台电脑上看；打开小电视后这里会显示它正在播的台"
      : none ? "到「设备」页输入小电视屏幕上的绑定码" : "看看电源和 Wi-Fi；打开小电视后这里会显示正在播的台";
  } else if (!ti) {
    const line = epgLine(now.channel);
    if (line) ti = line;
  }
  if (now.online && data.now.listen && data.now.device && data.now.device.audio_only === 1) grp = (grp ? grp + " · " : "") + "只听";
  screen.className = cls;
  $("ch").textContent = ch;
  $("grp").textContent = grp;
  $("nm").textContent = nm;
  $("ti").textContent = ti;
}

function renderPlayer() {
  const online = tvOnline();
  const v = online ? data.now.video : null;
  const series = online && data.now.total > 0;
  $("openEps").hidden = !series;
  if (series) $("epnow").textContent = data.now.episode ? "第 " + data.now.episode + " / " + data.now.total + " 集" : "共 " + data.now.total + " 集";
  $("player").hidden = !v;
  for (const id of ["chUp", "chDown", "pause"]) $(id).disabled = !online || !!pending;
  for (const id of ["back", "fwd", "next"]) $(id).disabled = !v;
  $("prev").hidden = !series;
  $("prev").disabled = !v || data.now.episode === 1;
  $("nextT").textContent = series ? "下一集" : "下一个";
  const paused = !!(v && v.paused);
  qs("#pause [data-ic]").innerHTML = ic(paused ? "play" : "pause");
  $("pauseT").textContent = paused ? "继续" : "暂停";
  if (!v) return;
  const seeking = pending && pending.kind === "seek" && pending.target != null;
  const position = seeking ? pending.target : v.paused || !v.moving ? v.position : v.position + (Date.now() - fetchedAt) / 1000;
  const duration = v.duration || 0;
  const bar = $("seek");
  bar.disabled = !duration;
  if (!dragging) {
    bar.max = String(Math.max(1, Math.floor(duration)));
    bar.value = String(Math.min(Math.floor(position), Math.floor(duration)));
    $("pos").textContent = clock(position);
  }
  $("dur").textContent = duration ? clock(duration) : "--:--";
}
setInterval(() => { if (data && !document.hidden) { renderPlayer(); renderMiniProgress(); } }, 1000);

async function control(path, body) {
  try {
    const answer = await call("POST", path, withDevice(body));
    if (!answer.ok) { toast(answer.error, true); return; }
    if (path === "api/pause") { toast("已暂停"); refresh(); return; }
    if (path === "api/resume") { toast("继续播放"); refresh(); return; }
    if (path === "api/next") { toast(data.now.total ? "下一集" : "下一个"); startPending(data.now.channel, data.now.name, answer.at, true, "seek"); return; }
    if (path === "api/previous") {
      if (answer.note) { toast(answer.note); return; }
      toast("上一集：第 " + answer.episode + " 集");
      startPending(data.now.channel, data.now.name, answer.at, true, "seek", null, "换到第 " + answer.episode + " 集…");
      return;
    }
    toast("已跳到 " + clock(answer.position) + "，马上接上");
    startPending(data.now.channel, data.now.name, answer.at, true, "seek", answer.position);
  } catch (error) { if (error instanceof SignedOut) signedOut(); else toast("网络不通", true); }
}
function togglePause() {
  const v = data && data.now.video;
  control(v && v.paused ? "api/resume" : "api/pause");
}
// 上一台 / 下一台：按频道表的顺序（各组连起来）挨着换，和小电视上的 ▲▼ 一样。
async function stepTv(delta) {
  if (!data || !tvOnline() || pending) return;
  const list = data.groups.flatMap((g) => g.channels);
  if (!list.length) return;
  const at = list.findIndex((c) => c.id === data.now.channel);
  const next = list[((at < 0 ? 0 : at) + delta + list.length) % list.length];
  try {
    const answer = await call("POST", "api/switch", withDevice({ channel: next.id }));
    if (answer.ok) { toast("换到 " + next.name); startPending(next.id, next.name, answer.at, /^(up|sr)\d\d$|^cast$/.test(next.id)); }
    else toast(answer.error, true);
  } catch (error) { if (error instanceof SignedOut) signedOut(); else toast("网络不通", true); }
}
$("back").onclick = () => control("api/seek", { delta: -30 });
$("fwd").onclick = () => control("api/seek", { delta: 30 });
$("next").onclick = () => control("api/next");
$("prev").onclick = () => control("api/previous");
$("pause").onclick = togglePause;
$("chUp").onclick = () => stepTv(-1);
$("chDown").onclick = () => stepTv(1);
$("seek").addEventListener("input", () => { dragging = true; $("pos").textContent = clock(Number($("seek").value)); });
$("seek").addEventListener("change", () => { dragging = false; control("api/seek", { position: Number($("seek").value) }); });

function renderDials() {
  const d = data.now.online ? data.now.device : null;
  $("dials").className = "dials" + (d ? "" : " off");
  $("vol").disabled = !d;
  if (!d) { $("volv").textContent = $("briv").textContent = "--"; }
  else if (dialBusy !== "vol") { $("vol").value = String(d.volume); $("volv").textContent = d.volume + "%"; }
  // 亮度：设备有几档就几个按钮（越大越亮）。
  const steps = d ? d.steps : 5;
  const seg = $("briSeg");
  if (seg.childElementCount !== steps) {
    seg.replaceChildren(...Array.from({ length: steps }, (_, i) => {
      const b = el("button", "", String(i + 1));
      b.dataset.bri = String(i);
      b.setAttribute("aria-label", "亮度 " + (i + 1) + " 档");
      return b;
    }));
  }
  for (const b of seg.children) { b.disabled = !d; b.setAttribute("aria-pressed", String(!!d && Number(b.dataset.bri) === d.brightness)); }
  if (d) $("briv").textContent = (d.brightness + 1) + "/" + d.steps;
  // 只听
  const known = d && typeof d.audio_only === "number";
  let on = known && d.audio_only === 1;
  if (listenPending) {
    if (known && (d.audio_only === 1) === listenPending.want) listenPending = null;
    else if (Date.now() > listenPending.deadline) { listenPending = null; toast("小电视好像没切过去，再点一次试试", true); }
    else on = listenPending.want;
  }
  const sw = $("listen");
  sw.disabled = !known || !!listenPending;
  sw.setAttribute("aria-checked", String(!!on));
  $("listenNote").textContent = listenPending ? "切换中…" : !known ? (d ? "固件太旧，用不了" : deviceName() + "没在线")
    : on ? "只放声音，省流量也省电" : "只放声音，屏幕只显示宠物";
  // 定时关（/api/state 的 sleep：语音和这里设的是同一个）
  const timer = data.sleep || null;
  const left = timer && timer.kind === "time" ? Math.ceil(timer.left / 60) : 0;
  const mine = sleepSet[deviceId()] || 0;
  const sseg = $("sleepSeg");
  if (!sseg.childElementCount) {
    sseg.replaceChildren(...SLEEP_STEPS.map((m) => { const b = el("button", "", m ? String(m) : "关"); b.dataset.sleep = String(m); b.setAttribute("aria-label", m ? m + " 分钟后关" : "不定时"); return b; }));
  }
  for (const b of sseg.children) {
    const m = Number(b.dataset.sleep);
    b.disabled = !d && m !== 0;
    b.setAttribute("aria-pressed", String(timer ? (m === mine || (!mine && m && left <= m && left > (SLEEP_STEPS[SLEEP_STEPS.indexOf(m) - 1] || 0))) : m === 0));
  }
  $("sleepv").textContent = timer ? (timer.kind === "end" ? "看完停" : "剩 " + left + " 分") : "";
}
function dial(id, key, label) {
  const input = $(id);
  input.addEventListener("input", () => { dialBusy = id; $(id + "v").textContent = label(Number(input.value)); });
  input.addEventListener("change", async () => {
    const body = {};
    body[key] = Number(input.value);
    try {
      const answer = await call("POST", "api/device", withDevice(body));
      if (!answer.ok) toast(answer.error, true);
    } catch (error) { if (error instanceof SignedOut) signedOut(); }
    finally { setTimeout(() => { dialBusy = ""; refresh(); }, 1500); }
  });
}
dial("vol", "volume", (v) => v + "%");
$("briSeg").onclick = async (event) => {
  const b = event.target.closest("button");
  if (!b) return;
  for (const x of $("briSeg").children) x.setAttribute("aria-pressed", String(x === b));
  try {
    const answer = await call("POST", "api/device", withDevice({ brightness: Number(b.dataset.bri) }));
    if (!answer.ok) toast(answer.error, true);
  } catch (error) { if (error instanceof SignedOut) signedOut(); }
  setTimeout(refresh, 1500);
};
$("sleepSeg").onclick = (event) => {
  const b = event.target.closest("button");
  if (!b) return;
  busy(b, async () => {
    const minutes = Number(b.dataset.sleep);
    const answer = await call("POST", "api/sleep", withDevice({ minutes }));
    if (!answer.ok) { toast(answer.error, true); return; }
    sleepSet[deviceId()] = minutes;
    toast(minutes ? deviceName() + " " + minutes + " 分钟后关（先调暗再暂停）" : "定时关已取消");
    refresh();
  });
};
$("listen").onclick = () => busy($("listen"), async () => {
  const d = data && data.now.device;
  if (!d || typeof d.audio_only !== "number") return;
  const want = d.audio_only !== 1;
  const answer = await call("POST", "api/audio", withDevice({ on: want }));
  if (!answer.ok) { toast(answer.error, true); return; }
  listenPending = { want, deadline: Date.now() + 15000 };
  render();
  schedule(1000);
});

function renderPets() {
  const d = data.now.online ? data.now.device : null;
  const cur = d && typeof d.pet === "number" ? d.pet : null;
  $("petNote").textContent = cur === null ? (d ? "固件太旧，换不了" : deviceName() + "没在线") : "现在是" + PETS[cur][1] + " · 只听时屏幕上陪着你的那只";
  const grid = $("petgrid");
  if (grid.childElementCount !== PETS.length) {
    PETS.forEach((pet, i) => {
      const b = el("button");
      const img = el("img");
      img.src = "pets/" + pet[0] + "_mood0.png"; img.alt = "";
      b.append(img, el("span", "", pet[1]));
      b.setAttribute("aria-label", "换成" + pet[1]);
      b.onclick = () => busy(b, async () => {
        const answer = await call("POST", "api/pet", withDevice({ pet: i }));
        if (!answer.ok) { toast(answer.error, true); return; }
        toast("已换上" + pet[1]);
        setTimeout(refresh, 2500);
      });
      grid.appendChild(b);
    });
  }
  [...grid.children].forEach((b, i) => { b.className = i === cur ? "cur" : ""; b.disabled = cur === null; b.setAttribute("aria-pressed", String(i === cur)); });
}

// ======================================================================= 正在播放栏

function renderMini() {
  const fk = focusKey();
  renderMini2();
  restoreFocus(fk);
}
function renderMini2() {
  const m = $("mini");
  const pc = watching && (miniWho === "pc" || !tvOnline());
  if (pc) {
    const paused = $("watchVideo").paused;
    m.className = "";
    m.innerHTML = `<div class="mprog"><i id="miniBar" style="width:0%"></i></div>
      <button class="mart" data-mini-open aria-label="打开播放器">${art(watching.name || watching.title || "电脑", "w")}${watching.kind === "live" ? '<span class="badge live">直播</span>' : ""}</button>
      <div class="mtx"><b>${esc(watchHeadline())}</b><span><span class="dot"></span>这台电脑 · ${esc(watching.mode === "follow" ? "跟着" + deviceName() : watching.name || "窗口里播放")}</span></div>
      <div class="mctl">${watching.mode === "direct" ? `<button class="iconbtn hidem" data-mk="pcprev" aria-label="上一台">${ic("prev", 18)}</button>` : ""}<button class="iconbtn big" data-mk="pcpp" aria-label="${paused ? "继续" : "暂停"}">${ic(paused ? "play" : "pause", 18)}</button>${watching.mode === "direct" ? `<button class="iconbtn hidem" data-mk="pcnext" aria-label="下一台">${ic("next", 18)}</button>` : ""}
      <button class="iconbtn" data-mk="pcclose" aria-label="关闭电脑上的播放器">${ic("x", 18)}</button></div>`;
    renderMiniProgress();
    return;
  }
  const now = data && data.now;
  if (!now || !(now.online && now.channel)) {
    m.className = "idle";
    m.innerHTML = `<div class="mtx"><b>没有在播</b><span>${hasTv() ? esc(deviceName()) + (now && !now.online ? "没在线 · " : " · ") : ""}点任何节目开始；「播放到」选播在哪</span></div>${chipHTML()}`;
    return;
  }
  const v = now.video;
  const live = !/^(up|sr)\d\d$|^cast$/.test(now.channel);
  const title = now.title || now.name || now.channel;
  const sub = deviceName() + (now.device && now.device.audio_only === 1 ? " · 只听" : "") + " · " + (now.title ? now.name : (epgLine(now.channel) || now.group || ""));
  const paused = !!(v && v.paused);
  m.className = "";
  m.innerHTML = `<div class="mprog"><i id="miniBar" style="width:0%"></i></div>
    <button class="mart" data-mini-open aria-label="打开遥控">${art(now.name || now.channel, "w")}${live ? '<span class="badge live">直播</span>' : ""}</button>
    <div class="mtx"><b>${esc(title)}</b><span><span class="dot"></span>${esc(sub)}</span></div>
    <div class="mctl"><button class="iconbtn hidem" data-mk="chup" aria-label="上一台">${ic("prev", 18)}</button><button class="iconbtn big" data-mk="pp" aria-label="${paused ? "继续" : "暂停"}" ${pending ? "disabled" : ""}>${ic(paused ? "play" : "pause", 18)}</button><button class="iconbtn hidem" data-mk="chdn" aria-label="下一台">${ic("next", 18)}</button>
    <button class="iconbtn" data-mk="remote" aria-label="遥控 ${esc(deviceName())}">${ic("remote")}</button></div>`;
  renderMiniProgress();
}
function renderMiniProgress() {
  const bar = $("miniBar");
  if (!bar) return;
  let pct = 0;
  if (watching && (miniWho === "pc" || !tvOnline())) {
    pct = watching.duration ? pcPos() / watching.duration : 0;
  } else if (data && data.now.video && data.now.video.duration) {
    const v = data.now.video;
    const position = v.paused || !v.moving ? v.position : v.position + (Date.now() - fetchedAt) / 1000;
    pct = position / v.duration;
  } else if (data && data.now.online) {
    const p = epgNow(data.now.channel).now;
    if (p) pct = (Date.now() / 1000 - p.start) / (p.end - p.start);
  }
  bar.style.width = (Math.max(0, Math.min(1, pct)) * 100).toFixed(1) + "%";
}
$("mini").addEventListener("click", (event) => {
  const b = event.target.closest("button");
  if (!b) return;
  const k = b.dataset.mk;
  if (b.dataset.miniOpen !== undefined) { if (watching && (miniWho === "pc" || !tvOnline())) $("watchVideo").focus(); else openRemote(); return; }
  if (k === "pp") togglePause();
  else if (k === "chup") stepTv(-1);
  else if (k === "chdn") stepTv(1);
  else if (k === "remote") openRemote();
  else if (k === "pcpp") pcTogglePlay();
  else if (k === "pcprev") stepChannel(-1);
  else if (k === "pcnext") stepChannel(1);
  else if (k === "pcclose") stopWatch();
});

// ======================================================================= 在电脑上看（浮动窗口里的播放器）
// 自建 / 电脑客户端才有（/api/state 的 watch）。两种放法，按回答里的 mode：
// - "vod"（点播）：B 站的视频，中转按 Range 转发原文件（server/relay/vod.py），<video> 直接放（VodPlayer）：
//   秒开、拖到哪里浏览器自己去取、能倍速和画中画。放不了原文件的（分段、FLV、HEVC、选了 1080P）走下面的。
// - "dash"：YouTube（本机模式，server/relay/ytvod.py）：画面、声音两条轨按清单用 Range 取、MediaSource 拼（DashPlayer），
//   和点播一样秒开、拖哪到哪；浏览器放不了时退到 360p 的合体文件（"vod"），再不行 HLS。
// - "hls"：中转把片源转成 HLS（fMP4 段），这里用浏览器自带的 MediaSource 一段段接上放（server/relay/watch.py，
//   WatchPlayer）。直播一律这样。
// 两种看法：
// - 跟随（mode "follow"）：看选中的小电视正在播的台，小电视换台这里也跟着换。
// - 直接看（mode "direct"）：「播放到 → 这台电脑」，不动小电视（web.py 的 watch_direct）。
//   UP 主和剧集从电脑上次看到的地方起（没有就从小电视下次会播的地方起），电脑上的进度另记，不标看过。
// 控制条是自己画的：浏览器自带的进度条只是内存里那几段（缓冲窗口），不是视频的时间线。视频里的位置 =
// 这一路从第几秒起（回答里的 start）+ 媒体时间 − 第一段的媒体时间（列表里的 #EXT-X-RTV-ORIGIN）；
// 总长是回答里的 duration（B 站、YouTube 的视频信息）。跳到缓冲里没有的地方 = 中转从那里重起一路（start），
// 换一个新的 MediaSource。

let watching = null;      // {mode, device, sid, key, player, busy, channel, group, name, title, kind, index, total, series, again,
                          //  start, duration, seekable, seeking, parts, part, video, own}
// 这个页面的编号：同一个页面换台只占一路（每个用户最多两路）；每次打开页面换一个。
const viewer = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, "0")).join("");
const PC_REPORT_MS = 10000;   // 电脑上看的进度多久报一次（web.py 的 watch_progress）
const UPNEXT_S = 5;           // 放完以后几秒自动放下一集

function watchKey(now) { return [now.channel, now.title, now.episode].join("|"); }

function watchSay(text, bad, retry) {
  if (bad || text) switched();
  const box = $("watchMsg");
  box.className = "watchmsg" + (bad ? " bad" : "");
  box.replaceChildren();
  if (!text) return;
  box.appendChild(el("div", "", text));
  if (retry) box.appendChild(button("重试", "ghost", retryWatch));
}
function retryWatch() {
  if (!watching) return;
  // 视频放到一半断了：从断的地方重起；别的（直播、还没开始放）重新要一次。
  if (watching.seekable && watching.player && watching.player.origin != null && pcPos() > 1) { seekRestart(pcPos()); return; }
  if (watching.mode === "direct") startDirect(watching.again, { group: watching.group, again: true });
  else startWatch(false, true);
}

// ---- 直播起播的几个纯函数（tests/console_live_test.mjs 截出这一段单独测）----
// 直播源常常几秒才来一大段：第一段一到就开始放，先放慢一点（LIVE.SLOW）把缓冲攒到 LIVE.OK 秒，攒够了再恢复原速；
// 起播阶段（缓冲不到 LIVE.OK 秒、开始放的头 LIVE.STARTUP_MS）每半秒取一次列表，把片源一开始的那一阵段尽快接上。
const LIVE = { BACK: 6, LOW: 6, OK: 10, SLOW: 0.94, FAST_POLL_MS: 500, POLL_MS: 1000, STARTUP_MS: 30000 };
// 第一次取列表时从第几段起：倒数第 LIVE.BACK 段（约 12 秒的余量；接上预热的、别人已经在看的那一路时马上就有）；
// 刚起的一路只有一两段时从第一段起。
function liveStartSeq(first, count) { return Math.max(first, first + count - LIVE.BACK); }
// 播放速度：缓冲少于 LIVE.LOW 秒放慢，到 LIVE.OK 秒恢复原速，中间保持（不来回抖）。
function liveRate(ahead, rate) { return ahead < LIVE.LOW ? LIVE.SLOW : ahead >= LIVE.OK ? 1 : rate; }
// 多久再取一次列表：起播阶段半秒，之后一秒。
function livePoll(ahead, sinceStartMs) { return ahead < LIVE.OK && sinceStartMs < LIVE.STARTUP_MS ? LIVE.FAST_POLL_MS : LIVE.POLL_MS; }
// 一组台里 id 的上一台、下一台（首尾相接）；只有直播台（不是 UP 主、剧集、投屏）。没有时 null。
function liveNeighbours(ids, id) {
  const live = ids.filter((x) => !/^(up|sr)\d\d$/.test(x) && x !== "cast");
  const at = live.indexOf(id);
  if (at < 0 || live.length < 2) return { prev: null, next: null };
  return { prev: live[(at - 1 + live.length) % live.length], next: live[(at + 1) % live.length] };
}
// ---- 直播起播的纯函数到此 ----

// 一路 HLS（fMP4 段）用 MediaSource 放：取 master 里的 CODECS，接上初始化段，再按列表一段段接。
// opts：say（画面上的提示）、ended（放完了）、live（直播：落后太多时追到最新附近）、gap（视频落到中转的窗口外了）、
// notice（换线路时中转说的话）、reopen（直播放了一阵断了、中转说还有别的线路：页面重开一次，返回 true 表示接手了）。
class WatchPlayer {
  constructor(video, base, opts) {
    this.video = video; this.base = base; this.say = opts.say; this.live = !!opts.live; this.gap = opts.gap || (() => {});
    this.notice = opts.notice || this.say; this.reopen = opts.reopen || (() => false);
    this.line = opts.line || (() => {});
    this.alive = true; this.next = -1; this.ms = null; this.sb = null; this.started = false; this.origin = null; this.ended = false;
    this.t0 = performance.now(); this.playedAt = 0;
    this.onWaiting = () => this.kick();
    // 出画面了（换台时收起上一台停住的画面，见 switching）。
    this.onPlaying = () => {
      if (!this.alive || this.playedAt) return;
      this.playedAt = performance.now();
      if (opts.playing) opts.playing(Math.round(this.playedAt - this.t0));
    };
    video.addEventListener("playing", this.onPlaying);
    // 放完了：视频播完，或者直播源断了。跟随小电视时，小电视换到下一个就会自动接上。
    this.onEnded = () => { if (this.alive) opts.ended(); };
    video.addEventListener("waiting", this.onWaiting);
    video.addEventListener("ended", this.onEnded);
  }
  async start() {
    const v = this.video;
    if (!window.MediaSource) {
      // 没有 MediaSource（老 iPhone Safari）：浏览器自己放 HLS。时间从 0 起，算位置时当作 origin 0。
      if (!v.canPlayType("application/vnd.apple.mpegurl")) { this.say("这个浏览器放不了，换 Chrome、Edge 或 Safari 试试", true); return; }
      this.origin = 0;
      v.src = this.base + "master.m3u8";
      v.play().catch(() => this.say("点一下画面开始播放"));
      return;
    }
    // 直播：画面上是「正在切换…」（switching），不再另说。
    if (!this.live) pcStage("正在连片源…（要几秒钟）");
    try {
      const master = await this.fetch("master.m3u8", "text");
      if (!this.alive) return;
      const codecs = (master.match(/CODECS="([^"]+)"/) || [])[1] || "avc1.640028,mp4a.40.2";
      const mime = 'video/mp4; codecs="' + codecs + '"';
      if (!MediaSource.isTypeSupported(mime)) { this.say("这个浏览器放不了这种格式（" + codecs + "）", true); return; }
      this.ms = new MediaSource();
      v.src = URL.createObjectURL(this.ms);
      await new Promise((ok) => this.ms.addEventListener("sourceopen", ok, { once: true }));
      if (!this.alive) return;
      this.sb = this.ms.addSourceBuffer(mime);
      await this.append(await this.fetch("init.mp4", "arrayBuffer"));
      await this.loop();
    } catch (error) {
      if (this.alive && error.reopen && this.reopen(error.message)) return;
      if (this.alive) this.say(error.message || "放不了", true, true);
    }
  }
  // 取一个文件：503（还在连片源）过一会儿再来；410 是这一路停了，正文是原因（B 站连不上之类）；404 是地址失效了。
  // 直播台有备用线路时（server/routes.py）：503 带 X-RTV-Notice 是在换线路（正文「CCTV2 线路 1 失效…正在换线路 2…」），
  // 显示出来接着等，不算进 8 次（线路有几条就可能等几轮，至多约两分钟）；410 带 X-RTV-Reopen 是放了一阵断了、
  // 还有别的线路，交给 reopen。
  async fetch(name, kind) {
    for (let round = 0; ; round++) {
      if (!this.alive) throw new Error("");
      const response = await fetch(this.base + name, { credentials: "same-origin", cache: "no-store" });
      if (response.ok) {
        // 几条线路的直播台：列表的 X-RTV-Route 是现在放第几条（从 1 数），自动换了线路以后头上的「线路 n/N」跟着变。
        const line = Number(response.headers.get("X-RTV-Route"));
        if (line > 0) this.line(line - 1);
        return response[kind]();
      }
      const notice = response.status === 503 && response.headers.get("X-RTV-Notice");
      if (notice && round < 120) { const text = await response.text(); if (this.alive) this.notice(text); await sleep(1000); continue; }
      if (response.status === 503 && round < 8) { await sleep(1000); continue; }
      if (response.status === 410) {
        const error = new Error((await response.text()) || "片源断了");
        error.reopen = !!response.headers.get("X-RTV-Reopen");
        throw error;
      }
      if (response.status === 404 && name.endsWith(".m4s")) return null;      // 落在窗口外了，跳过
      throw new Error(response.status === 404 ? "这一路已经停了" : "片源还没准备好");
    }
  }
  append(buffer) {
    return new Promise((ok, bad) => {
      const sb = this.sb;
      const done = () => { sb.removeEventListener("updateend", done); sb.removeEventListener("error", fail); ok(); };
      const fail = () => { sb.removeEventListener("updateend", done); sb.removeEventListener("error", fail); bad(new Error("浏览器解不了这一段")); };
      sb.addEventListener("updateend", done);
      sb.addEventListener("error", fail);
      try { sb.appendBuffer(buffer); } catch (error) { sb.removeEventListener("updateend", done); sb.removeEventListener("error", fail); bad(error); }
    });
  }
  remove(from, to) {
    return new Promise((ok) => {
      const sb = this.sb;
      const done = () => { sb.removeEventListener("updateend", done); ok(); };
      sb.addEventListener("updateend", done);
      try { sb.remove(from, to); } catch (error) { sb.removeEventListener("updateend", done); ok(); }
    });
  }
  ahead() {
    const v = this.video, b = v.buffered;
    return b.length ? b.end(b.length - 1) - v.currentTime : 0;
  }
  async loop() {
    while (this.alive) {
      const text = await this.fetch("index.m3u8", "text");
      const first = Number((text.match(/#EXT-X-MEDIA-SEQUENCE:(\d+)/) || [])[1] || 0);
      const count = (text.match(/^\d+\.m4s$/gm) || []).length;
      const ended = text.includes("#EXT-X-ENDLIST");
      const origin = text.match(/#EXT-X-RTV-ORIGIN:(-?[\d.]+)/);
      if (origin && this.origin === null) this.origin = Number(origin[1]);
      // 第一次：直播从倒数第六段起（约 12 秒的余量，直播源常常几秒才来一大段；刚起的一路从第一段起，
      // 见 liveStartSeq）；视频的这一路还从头留着时从第一段起（从 start 那一秒起，不跳过开头），
      // 接上别人已经在看的那一路时和直播一样。
      if (this.next < 0) this.next = !this.live && first === 0 ? 0 : liveStartSeq(first, count);
      if (this.next < first) {
        // 视频：要的那一段已经不在中转的窗口里了（暂停太久）。从现在的位置重起一路，免得跳过一截。
        if (!this.live) { this.gap(); return; }
        this.next = first;
      }
      // 视频暂停着、缓冲里已经有一分钟：先不取了（列表照取，中转知道还有人看）。
      const full = !this.live && this.ahead() > 60;
      for (; !full && this.next < first + count && this.alive; this.next++) {
        const chunk = await this.fetch(this.next + ".m4s", "arrayBuffer");
        if (!chunk || !this.alive) continue;
        await this.trim();
        await this.append(chunk);
        this.kick();
      }
      if (ended && this.next >= first + count) {
        // 直播放了一阵断了、中转还有别的线路（#EXT-X-RTV-REOPEN）：马上重开，不等缓冲放完。
        if (this.live && text.includes("#EXT-X-RTV-REOPEN") && this.reopen("线路断了，正在换线路…")) return;
        this.ended = true;
        if (this.ms.readyState === "open") this.ms.endOfStream();
        return;
      }
      this.pace();
      await sleep(this.live ? livePoll(this.ahead(), performance.now() - this.t0) : 1000);
    }
  }
  // 直播：缓冲少时放慢一点，攒够了恢复原速（liveRate）。视频不动速度。
  pace() {
    if (!this.live || !this.started) return;
    const v = this.video, rate = liveRate(this.ahead(), v.playbackRate);
    if (v.playbackRate !== rate) v.playbackRate = rate;
  }
  // 起播、跳过空档；直播落后太多时追到最新附近（视频不追：那是暂停过）。
  kick() {
    if (!this.alive || !this.sb) return;
    const v = this.video, b = v.buffered;
    if (!b.length) return;
    const end = b.end(b.length - 1);
    let inside = -1;
    for (let i = 0; i < b.length; i++) if (v.currentTime >= b.start(i) - 0.05 && v.currentTime <= b.end(i)) inside = i;
    if (inside < 0) {
      const ahead = [...Array(b.length).keys()].map((i) => b.start(i)).find((t) => t > v.currentTime);
      v.currentTime = (ahead !== undefined ? ahead : b.start(0)) + 0.05;
    } else if (this.live && end - v.currentTime > 30) v.currentTime = end - 12;
    if (!this.started) {
      this.started = true;
      this.say("");
      v.play().catch(() => this.say("点一下画面开始播放"));
    }
    this.pace();
  }
  async trim() {
    const v = this.video, b = this.sb.buffered;
    if (b.length && v.currentTime - b.start(0) > 90) await this.remove(b.start(0), v.currentTime - 30);
  }
  stop() {
    this.alive = false;
    const v = this.video;
    v.removeEventListener("waiting", this.onWaiting);
    v.removeEventListener("ended", this.onEnded);
    v.removeEventListener("playing", this.onPlaying);
    v.playbackRate = 1;
    v.pause();
    v.removeAttribute("src");
    v.load();
  }
}

// 点播：<video> 直接放中转转发的原文件（server/relay/vod.py）。位置就是 currentTime，跳转、倍速、缓冲都是浏览器的。
// opts：say、ended、start（从第几秒起）、fallback（还没放起来就出错：多半是浏览器放不了，改要 HLS）、
// broken（放到一半出错：地址过期、这一路被收了，按现在的位置重要一路）。
const VOD_OK = (() => { try { return !!document.createElement("video").canPlayType('video/mp4; codecs="avc1.64001F, mp4a.40.2"'); } catch (error) { return false; } })();
const RATES = [0.75, 1, 1.25, 1.5, 2];
function savedRate() { const r = Number(lsGet("rootv.rate", "1")); return RATES.includes(r) ? r : 1; }
class VodPlayer {
  constructor(video, url, opts) {
    this.vod = true; this.video = video; this.url = url; this.say = opts.say; this.at = Math.max(0, opts.start || 0);
    this.alive = true; this.started = false; this.ready = false; this.ended = false; this.origin = 0; this.failed = false;
    this.stats = { start: null, seeks: [] };
    const on = (name, fn) => { video.addEventListener(name, fn); this.offs.push(() => video.removeEventListener(name, fn)); };
    this.offs = [];
    on("loadedmetadata", () => {
      if (!this.alive) return;
      // 有的浏览器不认 #t=：自己跳过去。倍速每次换片源都会被重置。
      if (this.at > 0 && Math.abs(video.currentTime - this.at) > 1.5) video.currentTime = this.at;
      video.defaultPlaybackRate = video.playbackRate = savedRate();
      this.ready = true;
      if (opts.meta) opts.meta();
    });
    on("playing", () => {
      if (!this.alive) return;
      if (!this.started) { this.started = true; this.stats.start = Math.round(performance.now() - this.t0); this.say(""); }
      if (this.seekT) { this.stats.seeks.push(Math.round(performance.now() - this.seekT)); this.seekT = 0; }
    });
    on("seeking", () => { if (this.alive) { this.ended = false; this.seekT = performance.now(); } });
    on("seeked", () => { if (this.alive && video.paused && this.seekT) { this.stats.seeks.push(Math.round(performance.now() - this.seekT)); this.seekT = 0; } });
    on("waiting", () => { if (this.alive && this.started) this.slow = setTimeout(() => { if (this.alive && video.readyState < 3) this.say("缓冲中…"); }, 1500); });
    on("canplay", () => { clearTimeout(this.slow); if (this.alive && this.started) this.say(""); });
    on("ended", () => { if (this.alive) { this.ended = true; opts.ended(); } });
    on("error", () => {
      if (!this.alive || this.failed) return;
      this.failed = true;
      const code = video.error ? video.error.code : 0;
      if (!this.started) opts.fallback(code); else opts.broken(code);
    });
  }
  start() {
    const v = this.video;
    this.t0 = performance.now();
    pcStage("正在打开…");
    v.src = this.url + (this.at > 0 ? "#t=" + this.at.toFixed(1) : "");
    v.play().catch((error) => { if (this.alive && error && error.name === "NotAllowedError") this.say("点一下画面开始播放"); });
  }
  seek(target) { if (this.alive) this.video.currentTime = Math.max(0, target); }
  stop() {
    this.alive = false;
    clearTimeout(this.slow);
    for (const off of this.offs) off();
    const v = this.video;
    v.pause();
    // 倍速只给点播：换成 HLS（ffmpeg 按实时速度出段）时不能还是 2 倍。
    v.defaultPlaybackRate = v.playbackRate = 1;
    v.removeAttribute("src");
    v.load();
  }
}
// ---- DASH 的几个纯函数（tests/console_dash_test.mjs 截出这一段单独测）----
// YouTube 的点播（server/relay/ytvod.py）：画面、声音各一条轨，清单里每条轨有 t（n + 1 个时间边界，秒）和
// o（n + 1 个字节边界）：第 i 段从 t[i] 放到 t[i + 1]，字节是 o[i] 到 o[i + 1] − 1。
// 缓冲：当前位置往后 AHEAD 秒，往前留 BEHIND 秒；跳回来以后远处（AHEAD_MAX 秒以外）的清掉；NEAR：算「接得上」的缝。
// 一个请求取几段：相邻的段合成一个 Range，至多 MERGE_S 秒、MERGE_BYTES 字节（经代理时每个请求要等 0.2–0.3 秒才有
// 第一个字节，小请求还跑不满带宽：量过 8 个 512 KB 一个一个取 3.5 秒，一个 4 MB 0.9 秒）。
// 边收边接：起播前每攒够 FIRST_CHUNK 字节、之后每 CHUNK 字节就交给 SourceBuffer，收到第一个关键帧就出画面。
const DASH = { AHEAD: 60, BEHIND: 30, AHEAD_MAX: 150, NEAR: 0.3, MERGE_S: 20, MERGE_BYTES: 4 * 1024 * 1024,
               FIRST_CHUNK: 48 * 1024, CHUNK: 256 * 1024 };
// 包含 t 秒的是第几段（t 在第一段之前算第 0 段，在最后之后算最后一段）。
function dashIndex(times, t) {
  let lo = 0, hi = times.length - 2;
  if (hi < 0) return -1;
  if (t <= times[0]) return 0;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (times[mid] <= t) lo = mid; else hi = mid - 1; }
  return lo;
}
// 第 i 段（给了 j 就是第 i 到 j − 1 段）的 Range 头。
function dashRange(offsets, i, j) { return "bytes=" + offsets[i] + "-" + (offsets[j === undefined ? i + 1 : j] - 1); }
// 从第 next 段起一个请求取到第几段为止（不含）：至少一段；后面的段在 now + AHEAD 秒以内、合起来不超过 MERGE_S 秒
// 和 MERGE_BYTES 字节的都带上。
function dashSpan(times, offsets, next, now) {
  const last = times.length - 1;
  let end = next + 1;
  while (end < last && times[end] < now + DASH.AHEAD && times[end + 1] - times[next] <= DASH.MERGE_S
         && offsets[end + 1] - offsets[next] <= DASH.MERGE_BYTES) end++;
  return end;
}
// 几块收到的字节拼成一块。
function dashJoin(parts, size) {
  if (parts.length === 1) return parts[0];
  const out = new Uint8Array(size);
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.byteLength; }
  return out;
}
// 缓冲（[[起, 止], …]）里包含 t 的那一截（往前容 NEAR 秒的缝），没有时 null。
function dashCovering(ranges, t) {
  for (const r of ranges) if (t >= r[0] - DASH.NEAR && t <= r[1]) return r;
  return null;
}
// 跳到 t 以后从第几段接着取：t 已经在缓冲里时从那一截末尾接着取——末尾正好是一段的结尾就是下一段，停在一段中间
// （边收边接时掐断了）就是那一段（重取一遍，重叠的部分浏览器替换掉），否则从包含 t 的那一段。
function dashResume(times, ranges, t) {
  const r = dashCovering(ranges, t);
  if (!r) return dashIndex(times, t);
  const i = dashIndex(times, Math.max(t, r[1] - 0.2));
  return Math.min(times.length - 1, Math.abs(r[1] - times[i + 1]) <= DASH.NEAR ? i + 1 : i);
}
// 第 next 段要不要现在取：还没取完，而且它在 now + AHEAD 秒以内。
function dashWant(times, next, now) { return next >= 0 && next < times.length - 1 && times[next] < now + DASH.AHEAD; }
// 要清掉的缓冲：now − BEHIND 以前的，now + AHEAD_MAX 以后的。
function dashEvict(ranges, now) {
  const out = [];
  for (const [a, b] of ranges) {
    if (a < now - DASH.BEHIND) out.push([a, Math.min(b, now - DASH.BEHIND)]);
    if (b > now + DASH.AHEAD_MAX) out.push([Math.max(a, now + DASH.AHEAD_MAX), b]);
  }
  return out.filter((r) => r[1] - r[0] > 0.5);
}
// 清单里带着的初始化段（ib，base64）→ 字节；没带、坏了时 null（照旧按 init 的范围去取）。
function dashBytes(b64) {
  if (!b64) return null;
  try {
    const s = atob(b64), out = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  } catch (error) { return null; }
}
// ---- DASH 的纯函数到此 ----

// YouTube 的点播（mode "dash"）：两个 SourceBuffer（画面、声音），按清单把要的那几段用 Range 取来接上（取的是中转的
// vod/<编号>/<令牌>/v 和 …/a，中转经 YouTube 的代理转发）。位置就是 currentTime（和 VodPlayer 一样，vod 为真）：
// 跳转时只重算从第几段取、掐掉在取的，缓冲里有就直接放；倍速、画中画、进度、自动下一集都和 VodPlayer 一样。
// opts：say、ended、start、meta、fallback（还没放起来就出错：多半是浏览器放不了，改要合体文件或 HLS）、broken。
const DASH_OK = (() => {
  try { return !!window.MediaSource && MediaSource.isTypeSupported('video/mp4; codecs="avc1.640028"') && MediaSource.isTypeSupported('audio/mp4; codecs="mp4a.40.2"'); }
  catch (error) { return false; }
})();
function rangesOf(buffered) {
  const out = [];
  try { for (let i = 0; i < buffered.length; i++) out.push([buffered.start(i), buffered.end(i)]); } catch (error) { /* 已经拆掉了 */ }
  return out;
}
class DashPlayer {
  constructor(video, base, opts) {
    this.vod = true; this.dash = true; this.video = video; this.base = base; this.opts = opts; this.say = opts.say;
    this.at = Math.max(0, opts.start || 0);
    this.alive = true; this.started = false; this.ready = false; this.ended = false; this.origin = 0; this.failed = false;
    this.tracks = []; this.offs = [];
    this.stats = { start: null, manifest: null, seeks: [] };
    const on = (name, fn) => { video.addEventListener(name, fn); this.offs.push(() => video.removeEventListener(name, fn)); };
    on("playing", () => {
      if (!this.alive) return;
      if (!this.started) { this.started = true; this.stats.start = Math.round(performance.now() - this.t0); this.say(""); }
      if (this.seekT) { this.stats.seeks.push(Math.round(performance.now() - this.seekT)); this.seekT = 0; }
    });
    on("seeking", () => {
      if (!this.alive || !this.ready) return;
      this.ended = false; this.seekT = performance.now();
      this.reseek();
    });
    on("seeked", () => { if (this.alive && video.paused && this.seekT) { this.stats.seeks.push(Math.round(performance.now() - this.seekT)); this.seekT = 0; } });
    on("waiting", () => {
      if (!this.alive) return;
      this.kick(); this.pump();
      if (this.started) this.slow = setTimeout(() => { if (this.alive && video.readyState < 3) this.say("缓冲中…"); }, 1500);
    });
    on("canplay", () => { clearTimeout(this.slow); if (this.alive && this.started) this.say(""); });
    on("timeupdate", () => this.pump());
    on("ended", () => { if (this.alive) { this.ended = true; opts.ended(); } });
    on("error", () => { if (this.alive && this.ms) this.fail("浏览器解不了这个视频"); });
  }
  async start() {
    const v = this.video;
    this.t0 = performance.now();
    pcStage("正在连接…");
    let m;
    try {
      const r = await fetch(this.base + "manifest", { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw Object.assign(new Error((await r.text()) || "取不到视频的索引"), { gone: r.status === 404 });
      m = await r.json();
    } catch (error) { if (this.alive) this.fail(error.message || "取不到视频的索引", error.gone); return; }
    if (!this.alive) return;
    this.stats.manifest = Math.round(performance.now() - this.t0);
    const kinds = ["v", "a"];
    if (!window.MediaSource || !kinds.every((k) => m.tracks[k] && MediaSource.isTypeSupported(m.tracks[k].mime))) { this.fail("这个浏览器放不了这种格式"); return; }
    this.ms = new MediaSource();
    this.objectUrl = URL.createObjectURL(this.ms);
    v.src = this.objectUrl;
    await new Promise((ok) => this.ms.addEventListener("sourceopen", ok, { once: true }));
    if (!this.alive) return;
    try {
      this.ms.duration = m.duration;
      this.tracks = kinds.map((k) => ({ k, t: m.tracks[k].t, o: m.tracks[k].o, init: m.tracks[k].init, sb: this.ms.addSourceBuffer(m.tracks[k].mime),
                                        ib: m.tracks[k].ib, next: 0, busy: false, gen: 0, ctl: null, q: Promise.resolve(),
                                        partial: false, errors: 0 }));
      pcStage("缓冲中…");
      // 初始化段：清单里带着就直接用（中转取索引时已经读到了），省掉经代理再取一次。
      await Promise.all(this.tracks.map(async (t) => this.append(t, dashBytes(t.ib) || await this.get(t, "bytes=" + t.init[0] + "-" + t.init[1]))));
    } catch (error) { if (this.alive) this.fail(error.message || "放不了", error.gone); return; }
    if (!this.alive) return;
    for (const t of this.tracks) t.next = dashIndex(t.t, this.at);
    if (this.at > 0) v.currentTime = this.at;
    v.defaultPlaybackRate = v.playbackRate = savedRate();
    this.ready = true;
    if (this.opts.meta) this.opts.meta();
    this.pump();
    v.play().catch((error) => { if (this.alive && error && error.name === "NotAllowedError") this.say("点一下画面开始播放"); });
  }
  // 取一段字节（中转按 Range 转发）：网络断了、中转说片源连不上（502）时再试两次；404 是这一路已经停了。
  // 回答是 fetch 的 Response（读正文的是调用的人）；whole 为真时直接给字节。
  async get(t, range, whole = true) {
    for (let round = 0; ; round++) {
      if (!this.alive) throw new Error("");
      t.ctl = new AbortController();
      let r;
      try { r = await fetch(this.base + t.k, { credentials: "same-origin", cache: "no-store", headers: { Range: range }, signal: t.ctl.signal }); }
      catch (error) { if (error.name === "AbortError" || round >= 2) throw error; await sleep(600); continue; }
      if (r.ok) return whole ? r.arrayBuffer() : r;
      if (r.status === 404 || r.status === 410) throw Object.assign(new Error("这一路已经停了"), { gone: true });
      const text = await r.text();
      if (round >= 2) throw new Error(text || "片源连不上");
      await sleep(600);
    }
  }
  // 把回答的正文边收边接给 SourceBuffer（攒够 FIRST_CHUNK / CHUNK 字节交一次）；跳走了（gen 变了）就不再接。
  // 接了一半的段由 resetParser 收拾。
  async pour(t, r, gen) {
    if (!r.body || typeof r.body.getReader !== "function") {
      const data = await r.arrayBuffer();
      if (this.alive && gen === t.gen) await this.append(t, data);
      return;
    }
    const reader = r.body.getReader();
    let parts = [], size = 0;
    const flush = async () => {
      if (!size) return;
      const data = dashJoin(parts, size);
      parts = []; size = 0;
      t.partial = true;
      await this.append(t, data);
      this.kick();
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (!this.alive || gen !== t.gen) { reader.cancel().catch(() => {}); return; }
      if (done) break;
      parts.push(value); size += value.byteLength;
      if (size >= (this.started ? DASH.CHUNK : DASH.FIRST_CHUNK)) await flush();
      if (!this.alive || gen !== t.gen) { reader.cancel().catch(() => {}); return; }
    }
    await flush();
    t.partial = false;
  }
  // 接了半段就不接了（跳走了、断了）：让 SourceBuffer 丢掉那半段的解析状态，下一段从头解析。排在这条轨的队里。
  resetParser(t) {
    t.q = t.q.then(() => {
      if (!t.partial) return;
      t.partial = false;
      try { if (this.ms && this.ms.readyState === "open" && !t.sb.updating) t.sb.abort(); } catch (error) { /* 已经拆掉了 */ }
    });
  }
  // SourceBuffer 一次只能做一件事：每条轨的追加、清除排成一队。
  op(t, fn) {
    const run = t.q.then(() => new Promise((ok, bad) => {
      if (!this.alive) { ok(); return; }
      const sb = t.sb;
      const done = () => { sb.removeEventListener("updateend", done); sb.removeEventListener("error", fail); ok(); };
      const fail = () => { sb.removeEventListener("updateend", done); sb.removeEventListener("error", fail); bad(Object.assign(new Error("浏览器解不了这一段"), { decode: true })); };
      sb.addEventListener("updateend", done);
      sb.addEventListener("error", fail);
      try { fn(sb); } catch (error) { sb.removeEventListener("updateend", done); sb.removeEventListener("error", fail); bad(error); }
    }));
    t.q = run.catch(() => {});
    return run;
  }
  async append(t, buffer) {
    try { await this.op(t, (sb) => sb.appendBuffer(buffer)); }
    catch (error) {
      if (!error || error.name !== "QuotaExceededError") throw error;
      // 浏览器给的缓冲用完了：清掉当前位置 10 秒以前的，再试一次。
      const now = this.video.currentTime;
      if (now > 12) await this.op(t, (sb) => sb.remove(0, now - 10));
      await this.op(t, (sb) => sb.appendBuffer(buffer));
    }
  }
  async evict(t) {
    for (const [a, b] of dashEvict(rangesOf(t.sb.buffered), this.video.currentTime)) await this.op(t, (sb) => sb.remove(a, b));
  }
  pump() {
    if (!this.alive || !this.ready) return;
    const now = this.video.currentTime;
    for (const t of this.tracks) if (!t.busy && dashWant(t.t, t.next, now)) this.fill(t);
    this.maybeEnd();
  }
  // 取从第 next 段起的几段（dashSpan），边收边接。半路断了（不是跳走）：已经接上的留着，从缓冲的末尾接着取，
  // 连着断 3 次才算坏了。
  async fill(t) {
    t.busy = true;
    const gen = t.gen, i = t.next, j = dashSpan(t.t, t.o, i, this.video.currentTime);
    try {
      const r = await this.get(t, dashRange(t.o, i, j), false);
      if (!this.alive || gen !== t.gen) return;
      await this.evict(t);
      try { await this.pour(t, r, gen); } catch (error) { if (error && typeof error === "object") error.midway = true; throw error; }
      if (!this.alive || gen !== t.gen) return;
      t.next = j; t.errors = 0;
      this.kick();
    } catch (error) {
      if (!this.alive || gen !== t.gen || (error && error.name === "AbortError")) return;
      if (error && error.midway && !error.decode && error.name !== "QuotaExceededError" && (t.errors = (t.errors || 0) + 1) < 3) {
        this.resetParser(t);
        t.next = dashResume(t.t, rangesOf(t.sb.buffered), this.video.currentTime);
        if (gen === t.gen) t.busy = false;
        setTimeout(() => this.pump(), 600);
        return;
      }
      this.fail(error.message || "片源断了", error.gone);
      return;
    } finally { if (gen === t.gen) t.busy = false; }
    this.pump();
  }
  // 跳转：每条轨重算从第几段接着取（缓冲里有就从那一截的末尾），正在取的不是那一段就掐掉。
  reseek() {
    const now = this.video.currentTime;
    for (const t of this.tracks) {
      const next = dashResume(t.t, rangesOf(t.sb.buffered), now);
      if (t.busy && next === t.next) continue;
      t.gen++;
      if (t.ctl) t.ctl.abort();
      if (t.busy) this.resetParser(t);
      t.busy = false; t.next = next;
    }
    this.pump();
  }
  // 两条轨都取完了：告诉浏览器到头了（放到结尾才会有 ended）；之后再往回跳，追加时浏览器自己重新打开。
  maybeEnd() {
    if (!this.ms || this.ms.readyState !== "open" || !this.tracks.every((t) => t.next >= t.t.length - 1 && !t.busy)) return;
    Promise.all(this.tracks.map((t) => t.q)).then(() => {
      if (!this.alive || this.ms.readyState !== "open" || this.tracks.some((t) => t.sb.updating || t.next < t.t.length - 1)) return;
      try { this.ms.endOfStream(); } catch (error) { /* 正好又在追加 */ }
    });
  }
  // 画面和声音的段的边界不完全对齐：停在一条小缝前面时跳过去。
  kick() {
    const v = this.video;
    if (!this.alive || v.seeking) return;
    const ranges = rangesOf(v.buffered), now = v.currentTime;
    if (!ranges.length || ranges.some((r) => now >= r[0] - 0.05 && now < r[1])) return;
    const ahead = ranges.map((r) => r[0]).find((a) => a > now && a - now < 1);
    if (ahead !== undefined) v.currentTime = ahead + 0.01;
  }
  fail(text, gone) {
    if (!this.alive || this.failed) return;
    this.failed = true;
    if (!this.started) this.opts.fallback(text, gone); else this.opts.broken(text, gone);
  }
  seek(target) { if (this.alive) this.video.currentTime = Math.max(0, target); }
  stop() {
    this.alive = false;
    clearTimeout(this.slow);
    for (const t of this.tracks) if (t.ctl) t.ctl.abort();
    for (const off of this.offs) off();
    const v = this.video;
    v.pause();
    v.defaultPlaybackRate = v.playbackRate = 1;
    v.removeAttribute("src");
    v.load();
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
  }
}
function playerFor(answer) {
  const live = answer.kind === "live";
  if (live) switching(answer.name || answer.title || answer.channel);
  if (watching) watching.player.stop();
  $("pcwin").hidden = false;
  if (answer.mode === "dash") {
    const player = new DashPlayer($("watchVideo"), answer.url, {
      say: watchSay, ended: watchEnded, start: answer.start || 0,
      meta: () => {
        const d = $("watchVideo").duration;
        if (watching && watching.player === player && isFinite(d) && d > 0) { watching.duration = d; renderPc(); }
      },
      fallback: (why) => { if (watching && watching.player === player) dashFallback(why); },
      broken: () => { if (watching && watching.player === player) vodBroken(); },
    });
    return player;
  }
  if (answer.mode === "vod") {
    const player = new VodPlayer($("watchVideo"), answer.url, {
      say: watchSay, ended: watchEnded, start: answer.start || 0,
      meta: () => {
        // 文件自己的时长最准（B 站信息里的时长有时差几秒、投屏历史里重播的可能没有）。
        const d = $("watchVideo").duration;
        if (watching && watching.player === player && isFinite(d) && d > 0) { watching.duration = d; renderPc(); }
      },
      fallback: () => { if (watching && watching.player === player) vodFallback(); },
      broken: () => { if (watching && watching.player === player) vodBroken(); },
    });
    return player;
  }
  const player = new WatchPlayer($("watchVideo"), answer.url, {
    say: watchSay, ended: watchEnded, live,
    notice: (text) => { if (watching && watching.player !== player) return; routeNotice(answer.name || answer.title || answer.channel, text); },
    reopen: (text) => (!watching || watching.player === player) && routeReopen(answer, text),
    line: (i) => { if (watching && watching.player === player && watching.route !== i) { watching.route = i; renderLine(); } },
    playing: (ms) => {
      if (!watching || watching.player !== player) return;
      switched();
      if (live) warmSoon();
      if (window.console && console.debug) console.debug("在电脑上看：出画面用了 " + ms + " ms");
    },
    // 暂停太久、中转那边已经往前转过了：暂停着就等按「播放」时再从这里重起（那之前不占 ffmpeg），在放就马上重起。
    gap: () => {
      if (!watching || watching.player !== player) return;
      if ($("watchVideo").paused) watching.stale = true; else seekRestart(pcPos());
    },
  });
  return player;
}

// ---- 换台：上一台的画面停住，盖上台名和「正在切换…」，新的台出画面时收起 ----

function switching(name, text) {
  const v = $("watchVideo"), canvas = $("pcFreeze");
  $("pcSwitchName").textContent = name || "";
  $("pcSwitchText").textContent = text || (watching ? "正在切换…" : "正在连接…");
  $("pcSwitch").classList.toggle("wrap", !!text);
  $("pcSwitch").hidden = false;
  watchSay("");
  // 画面停在上一台最后一帧（还在放、有画面时；MediaSource 的画面同源，能画到 canvas 上）。
  if (canvas.hidden && watching && v.readyState >= 2 && v.videoWidth) {
    try {
      canvas.width = v.videoWidth; canvas.height = v.videoHeight;
      canvas.getContext("2d").drawImage(v, 0, 0);
      canvas.hidden = false;
    } catch (error) { canvas.hidden = true; }
  }
}
function switched() {
  $("pcSwitch").hidden = true;
  $("pcFreeze").hidden = true;
  setPoster("");
}

// ---- 点了马上开窗口（每一种节目都这样，playOnPc）：封面，上面写着在做什么，关掉窗口就是取消 ----
// YouTube：正在解析 YouTube…（中转问节点取地址，预取过的马上就有）→ 正在连接…（取索引）→ 缓冲中…（第一段），出画面时收起。
let opening = null;        // {n}：正在等中转回答的那一次；关窗口时清掉
let openSeq = 0;
function openingIs(n) { return !!opening && opening.n === n; }
function setPoster(url) {
  const img = $("pcPoster");
  if (!img) return;
  if (url) { if (img.getAttribute("src") !== url) img.src = url; img.hidden = false; }
  else if (!img.hidden) { img.hidden = true; img.removeAttribute("src"); }
}
function ytIdOfItem(item) {
  if (!item) return "";
  if (item.site === "youtube" && /^[A-Za-z0-9_-]{11}$/.test(item.id || "")) return item.id;
  if (item.video && item.video.site === "youtube") return item.video.id || "";
  const m = String(item.url || "").match(/(?:youtu\.be\/|[?&]v=|\/shorts\/|\/live\/)([A-Za-z0-9_-]{11})/);
  return m ? m[1] : "";
}
function ytPoster(id) { return id ? "api/bili/thumb?" + new URLSearchParams({ s: "v", u: "https://i.ytimg.com/vi/" + id + "/mqdefault.jpg" }) : ""; }
function pcOpening(item, label, poster) {
  const n = ++openSeq;
  opening = { n };
  const yt = ytIdOfItem(item) || ytIdOfCard({ art: { url: poster } }), live = item.kind === "live";
  const name = label || item.title || item.channel || "";
  $("pcwin").hidden = false;
  if (!watching) { $("watchWhat").textContent = name || "在电脑上看"; $("watchWhat").title = name; $("watchSub").textContent = "这台电脑"; }
  switching(name, live ? "" : yt || item.site === "youtube" ? "正在解析 YouTube…" : "正在打开…");
  $("pcSwitch").classList.remove("wrap");
  const img = poster || ytPoster(yt);
  if (img && !live) { $("pcFreeze").hidden = true; setPoster(img); }
  openingClock(n);
  return n;
}
// 等中转回答久了（YouTube 没预取过时要跑一次 yt-dlp，几秒到十几秒）：遮罩上的字后面数秒，看得出没卡住。
// 回答到了（opening 清掉或换了一次）就停。
const OPENING_COUNT_AFTER_S = 3;
function openingClock(n) {
  const base = $("pcSwitchText").textContent, t0 = Date.now();
  const timer = setInterval(() => {
    if (!openingIs(n)) { clearInterval(timer); return; }
    const s = Math.floor((Date.now() - t0) / 1000);
    if (s >= OPENING_COUNT_AFTER_S && !$("pcSwitch").hidden) $("pcSwitchText").textContent = base + " " + s + " 秒";
  }, 1000);
}
// 起播的每一步：遮罩还在时写在遮罩上，否则写在画面上的提示里。
function pcStage(text) {
  if (!$("pcSwitch").hidden) { $("pcSwitchText").textContent = text; return; }
  watchSay(text);
}
// 没开成（中转说不行、网络不通）：之前在放的照旧放，什么都没在放就收起窗口。
function openFailed() {
  switched();
  if (!watching) $("pcwin").hidden = true;
}
// 不管哪种播放器，出画面了就收起遮罩和封面。
$("watchVideo").addEventListener("playing", () => { if (watching) switched(); });

// ---- 直播台的备用线路（server/routes.py）：中转在一路里按顺序换线路，这里只把它说的话显示出来 ----
// 换台的遮罩还在时写在遮罩上（台名下面），否则写在画面上的提示里。
function routeNotice(name, text) {
  if (!$("pcSwitch").hidden) {
    // 遮罩上已经有台名：话里开头的台名不再重复。
    const shown = $("pcSwitchName").textContent;
    $("pcSwitchText").textContent = shown && text.startsWith(shown + " ") ? text.slice(shown.length + 1) : text;
    $("pcSwitch").classList.add("wrap");
    return;
  }
  watchSay(text);
}
// 放了一阵断了、中转说还有别的线路：自动重开（中转把断了的线路排到最后，从下一条起）。同一个台两分钟内至多
// 重开「线路条数」次，再断就照常显示原因和「重试」。
const reopened = {};
const REOPEN_WINDOW_MS = 120000;
function routeReopen(answer, text) {
  if (!watching || watching.kind !== "live") return false;
  const id = answer.channel || "", now = Date.now(), seen = reopened[id];
  const count = seen && now - seen.at < REOPEN_WINDOW_MS ? seen.n + 1 : 1;
  if (count > Math.max(1, answer.routes || 1)) return false;
  reopened[id] = { n: count, at: now };
  switching(answer.name || answer.title || id, text || "线路断了，正在换线路…");
  setTimeout(() => { if (watching && watching.channel === answer.channel) retryWatch(); }, 300);
  return true;
}

// ---- 预热：电脑上看直播时，让中转先把上一台、下一台（和鼠标停着的台）转起来，换过去马上有画面 ----
// 中转没开预热（云端没有在电脑上看；自建看 config.toml 的 [pc] warm_channels 和内存）时 /api/state 没有 watch_warm。
const WARM_AFTER_MS = 2000;     // 一个台放了几秒以后预热它的上一台、下一台
const WARM_AGAIN_MS = 20000;    // 同一个台多久内不再问（中转那边转 30 秒）
let warmTimer = 0, warmOff = false;
const warmedAt = {};
function canWarm() {
  return !warmOff && !!(data && data.watch_warm) && !!watching && watching.mode === "direct" && watching.kind === "live";
}
function warm(ids) {
  if (!canWarm()) return;
  const now = Date.now();
  const want = ids.filter((id) => id && id !== watching.channel && !(now - (warmedAt[id] || 0) < WARM_AGAIN_MS)).slice(0, 2);
  if (!want.length) return;
  for (const id of want) warmedAt[id] = now;
  call("POST", "api/watch/warm", { viewer, channels: want }).then((answer) => { if (answer && answer.off) warmOff = true; }).catch(() => {});
}
// 正在看的台在它那一组里的上一台、下一台。
function neighbourIds() {
  if (!watching || !data) return { prev: null, next: null };
  const g = data.groups.find((x) => x.name === watching.group && x.channels.some((c) => c.id === watching.channel))
    || data.groups.find((x) => x.channels.some((c) => c.id === watching.channel));
  return g ? liveNeighbours(g.channels.map((c) => c.id), watching.channel) : { prev: null, next: null };
}
function warmSoon() {
  clearTimeout(warmTimer);
  warmTimer = setTimeout(() => { const n = neighbourIds(); warm([n.next, n.prev]); }, WARM_AFTER_MS);
}
// 鼠标停在上一台 / 下一台的按钮上、停在直播台的卡片上（这台电脑是播放的地方时）：先转起来。
let hoverTimer = 0;
function warmOnHover(node, id) {
  const go = () => { clearTimeout(hoverTimer); hoverTimer = setTimeout(() => warm([typeof id === "function" ? id() : id]), 300); };
  node.addEventListener("mouseenter", go);
  node.addEventListener("focus", go);
  node.addEventListener("mouseleave", () => clearTimeout(hoverTimer));
}
warmOnHover($("watchPrevCh"), () => neighbourIds().prev);
warmOnHover($("watchNextCh"), () => neighbourIds().next);

// ---- 预先解析：直播页上这一组台，让中转先把跳转和主列表走一遍（不起 ffmpeg，watch.WatchService.prepare），
// 点下去起播少等几秒。只在这台电脑是播放的地方时问；同一个台 PREPARE_AGAIN_MS 内不再问（中转记的地址 10 分钟内有效）。
const PREPARE_AGAIN_MS = 240000;
const preparedAt = {};
let prepareTimer = 0;
function prepareGroupSoon() {
  clearTimeout(prepareTimer);
  prepareTimer = setTimeout(() => {
    if (route !== "live" || document.hidden || !data || !data.watch || isGuest() || currentTarget() !== PC) return;
    const g = data.groups.find((x) => x.name === group);
    const now = Date.now();
    const want = (g ? g.channels : []).map((c) => c.id)
      .filter((id) => !isRelay(id) && id !== "cast" && !(now - (preparedAt[id] || 0) < PREPARE_AGAIN_MS)).slice(0, 40);
    if (!want.length) return;
    for (const id of want) preparedAt[id] = now;
    call("POST", "api/watch/prepare", { channels: want }).catch(() => {});
  }, 400);
}

// ---- 视频里的位置、跳转 ----

function pcPos() {
  if (!watching) return 0;
  if (watching.seeking != null) return watching.seeking;
  const p = watching.player, v = $("watchVideo");
  if (p && p.vod) return p.ready ? v.currentTime : watching.start || 0;      // 点播：就是 currentTime
  if (!p || p.origin == null || !p.started) return watching.start || 0;
  return Math.max(0, (watching.start || 0) + v.currentTime - p.origin);
}
let seekTimer = 0;
// 跳到 target 秒：缓冲里有就直接跳，没有就让中转从那里重起一路（拖动时停手一会儿才发）。
function pcSeek(target, now) {
  if (!watching || !watching.seekable) return;
  const dur = watching.duration;
  target = Math.max(0, dur ? Math.min(target, dur - 3) : target);
  const p = watching.player, v = $("watchVideo");
  if (p && p.vod && !p.failed) {
    // 点播：浏览器自己去取那一段（中转转发 Range），不用重起什么。
    clearTimeout(seekTimer);
    watching.seeking = null;
    hideUpNext();
    if (p.ready) p.seek(target); else p.at = target;
    renderPc();
    return;
  }
  if (p && p.origin != null && p.started && watching.seeking == null) {
    const t = target - watching.start + p.origin, b = v.buffered;
    for (let i = 0; i < b.length; i++) {
      if (t >= b.start(i) && t <= b.end(i) - 1) { v.currentTime = t; renderPc(); return; }
    }
  }
  watching.seeking = target;
  renderPc();
  clearTimeout(seekTimer);
  seekTimer = setTimeout(() => seekRestart(target), now ? 0 : 450);
}
async function seekRestart(target) {
  if (!watching) return;
  if (watching.player.vod && !watching.player.failed) { pcSeek(target); return; }
  clearTimeout(seekTimer);
  watching.seeking = target;
  hideUpNext();
  renderPc();
  watchSay("跳转中…");
  // 点播的播放器坏了（地址过期、这一路被收了）才会走到这里：同一路也要换一个新的播放器（again）。
  const again = !!watching.player.vod;
  const ok = watching.mode === "direct"
    ? await startDirect(Object.assign({}, watching.again, { start: Math.round(target * 10) / 10 }), { group: watching.group, seek: true, again })
    : await startWatch(false, true, Math.round(target * 10) / 10);
  if (!ok && watching) { watching.seeking = null; watchSay(""); renderPc(); }
}
function pcSkip(delta) { if (watching && watching.seekable) pcSeek(pcPos() + delta); }

// ---- 进度（电脑上看的，和小电视的分开记） ----

function pcBody(extra) {
  return Object.assign({ viewer, sid: watching.sid, position: Math.round(pcPos() * 10) / 10 }, extra || {});
}
function pcTracked() { return watching && watching.mode === "direct" && watching.kind === "video" && watching.seeking == null && watching.player.started; }
function pcReport(extra) {
  if (!pcTracked()) return Promise.resolve();
  pcReported = Date.now();
  return call("POST", "api/watch/progress", pcBody(extra)).catch(() => {});
}
let pcReported = 0;
setInterval(() => {
  if (!pcTracked()) return;
  // 点播暂停着时浏览器不取数据：每分钟报一次，中转知道这一路还有人看（vod.py 的 IDLE_S）。
  if (!$("watchVideo").paused || (watching.player.vod && Date.now() - pcReported > 60000)) pcReport();
}, PC_REPORT_MS);
// 关页面、刷新：最后看到的地方也记下（keepalive，页面没了请求照样发出去）。
addEventListener("pagehide", () => {
  if (!pcTracked()) return;
  try {
    fetch("api/watch/stop", { method: "POST", credentials: "same-origin", keepalive: true,
                               headers: { "Content-Type": "application/json" }, body: JSON.stringify(pcBody()) });
  } catch (error) { /* 关页面时发不出去也只是少记几秒 */ }
});

// ---- 选集、下一集 ----

function partReq(n) {
  if (!watching) return null;
  if (/^(up|sr)\d\d$/.test(watching.channel)) return { channel: watching.channel, episode: n };
  if (watching.video) return { video: watching.video, part: n };
  return null;
}
function partWord(n) {
  const kind = watching && watching.parts ? watching.parts.kind : "";
  return kind === "pages" ? "P" + n : kind === "up" ? "第 " + n + " 个" : "第 " + n + " 集";
}
function nextPart() {
  if (!watching || watching.mode !== "direct" || !watching.parts) return null;
  const items = watching.parts.items, cur = watching.parts.current;
  if (!cur || cur >= items.length) return null;
  const item = items[cur];
  return { n: item.n, title: item.title, body: partReq(item.n) };
}
async function playPart(n) {
  if (!watching || watching.busy) return;
  const body = partReq(n);
  if (!body) return;
  hideUpNext();
  closeParts();
  await pcReport();
  watchSay("正在换到" + partWord(n) + "…");
  startDirect(body, { group: watching.group });
}
function openParts() {
  if (!watching || !watching.parts) return;
  const box = $("pcParts"), list = $("pcPartsList"), parts = watching.parts;
  $("pcPartsName").textContent = parts.name || watching.name || "选集";
  const seen = parts.items.filter((x) => x.watched).length;
  $("pcPartsSub").textContent = "共 " + parts.items.length + (parts.kind === "pages" ? " P" : parts.kind === "up" ? " 个视频" : " 集")
    + (seen ? " · 电脑上看完 " + seen : "") + " · 正在放" + partWord(parts.current);
  list.replaceChildren();
  for (const item of parts.items) {
    const row = el("button", "pcpart" + (item.n === parts.current ? " cur" : "") + (item.watched ? " seen" : ""));
    row.dataset.part = item.n;
    row.setAttribute("aria-current", item.n === parts.current ? "true" : "false");
    row.innerHTML = `<span class="n">${parts.kind === "pages" ? "P" + item.n : item.n}</span><span class="t">${esc(item.title || partWord(item.n))}</span>`
      + `<span class="d">${item.watched ? ic("check", 14) : ""}${item.duration ? clock(item.duration) : ""}</span>`;
    row.setAttribute("aria-label", partWord(item.n) + " " + (item.title || "") + (item.watched ? "，看完了" : "") + (item.n === parts.current ? "，正在放" : ""));
    row.onclick = () => { if (item.n === parts.current) closeParts(); else playPart(item.n); };
    list.appendChild(row);
  }
  box.hidden = false;
  $("pcResume").hidden = true;
  const cur = list.querySelector(".cur") || list.firstChild;
  if (cur) { cur.scrollIntoView({ block: "center" }); cur.focus({ preventScroll: true }); }
  pcActive();
}
function closeParts() {
  if ($("pcParts").hidden) return;
  $("pcParts").hidden = true;
  if ($("pcParts").contains(document.activeElement)) $("pcPartsBtn").focus({ preventScroll: true });
}

// 放完以后的「下一集」：倒数 5 秒自动放，可以马上放或取消。
let upNext = null;
function showUpNext(next) {
  hideUpNext();
  let left = UPNEXT_S;
  $("pcUpNextKick").textContent = "接下来 · " + partWord(next.n);
  $("pcUpNextTitle").textContent = next.title || partWord(next.n);
  const ring = $("pcUpNextRing");
  const draw = () => { $("pcUpNextN").textContent = left; ring.style.strokeDashoffset = String(106.8 * (1 - left / UPNEXT_S)); };
  ring.style.transition = "none"; draw(); void ring.getBoundingClientRect(); ring.style.transition = "";
  $("pcUpNext").hidden = false;
  upNext = { next, timer: setInterval(() => {
    left -= 1;
    if (left <= 0) { const n = next.n; hideUpNext(); playPart(n); return; }
    draw();
  }, 1000) };
  pcActive();
}
function hideUpNext() {
  if (upNext) clearInterval(upNext.timer);
  upNext = null;
  $("pcUpNext").hidden = true;
}
$("pcUpNextGo").onclick = () => { if (upNext) { const n = upNext.next.n; hideUpNext(); playPart(n); } };
$("pcUpNextStop").onclick = () => { hideUpNext(); watchSay("放完了。点「选集」或「下一集」接着看", false); };

// 一个视频放完了：电脑上记「看完了」，有下一集就倒数着放下一集；片源半路断了时可以从断的地方重试。
function watchEnded() {
  if (!watching) return;
  if (watching.kind === "live") { watchSay("直播断了", false, true); return; }
  const pos = pcPos(), dur = watching.duration;
  if (dur && pos < dur - 30) { watchSay("片源断了", true, true); return; }
  if (watching.mode !== "direct") { watchSay("这一段放完了", false, true); return; }
  pcReport({ ended: true, position: dur || pos });
  if (watching.parts) {
    const cur = watching.parts.items[watching.parts.current - 1];
    if (cur) cur.watched = true;
  }
  const next = nextPart();
  if (next) showUpNext(next);
  else watchSay(watching.parts ? "全部放完了" : "放完了", false);
  renderPc();
}

// ---- 控制条 ----

function renderPc() {
  if (!watching) return;
  const v = $("watchVideo"), live = watching.kind === "live", dur = watching.duration || 0, pos = pcPos();
  const seekable = !!watching.seekable;
  $("pcLive").hidden = !live;
  $("pcTime").hidden = live;
  if (!live) $("pcTime").textContent = clock(pos) + (dur ? " / " + clock(dur) : "");
  const bar = $("pcSeek");
  bar.classList.toggle("off", !seekable || !dur);
  bar.hidden = live;
  const frac = dur ? Math.min(1, pos / dur) : 0;
  $("pcCur").style.width = (frac * 100).toFixed(2) + "%";
  $("pcKnob").style.left = (frac * 100).toFixed(2) + "%";
  let buf = 0;
  const p = watching.player;
  if (dur && p && p.vod && v.buffered.length && watching.seeking == null) {
    // 点播：现在的位置所在的那一段缓冲（跳过以后前面的缓冲不算）。
    for (let i = 0; i < v.buffered.length; i++) {
      if (v.buffered.start(i) <= pos + 0.5 && v.buffered.end(i) >= pos) buf = Math.min(1, v.buffered.end(i) / dur);
    }
  } else if (dur && p && p.origin != null && v.buffered.length && watching.seeking == null) {
    buf = Math.min(1, (watching.start + v.buffered.end(v.buffered.length - 1) - p.origin) / dur);
  }
  $("pcBuf").style.width = (Math.max(buf, frac) * 100).toFixed(2) + "%";
  bar.setAttribute("aria-valuemax", String(Math.round(dur)));
  bar.setAttribute("aria-valuenow", String(Math.round(pos)));
  bar.setAttribute("aria-valuetext", clock(pos) + (dur ? " / " + clock(dur) : ""));
  const paused = v.paused;
  const pp = $("pcPlay");
  if (pp.dataset.state !== String(paused)) {
    pp.dataset.state = String(paused);
    pp.innerHTML = ic(paused ? "play" : "pause", 20);
    pp.setAttribute("aria-label", paused ? "播放" : "暂停");
    pp.title = paused ? "播放（空格）" : "暂停（空格）";
  }
  $("pcBack").hidden = $("pcFwd").hidden = !seekable;
  const next = nextPart();
  const stepUp = watching.mode === "direct" && watching.kind === "video" && /^up\d\d$/.test(watching.channel);
  $("pcNext").hidden = !(next || stepUp);
  $("pcNext").setAttribute("aria-label", next ? "下一" + (watching.parts.kind === "up" ? "个" : "集") + "：" + (next.title || partWord(next.n)) : "下一个");
  $("pcNext").title = $("pcNext").getAttribute("aria-label");
  $("pcPartsBtn").hidden = !(watching.parts && watching.parts.items.length > 1 && watching.mode === "direct");
  const vod = !!(p && p.vod);
  $("pcRate").hidden = !vod;
  if (vod) $("pcRate").textContent = rateLabel(v.playbackRate);
  $("pcQuality").hidden = !watching.quality;
  if (watching.quality) $("pcQuality").textContent = watching.quality.current.toUpperCase();
  $("pcPip").hidden = !pipOk(v);
  $("pcPartsBtn").textContent = watching.parts && watching.parts.kind === "up" ? "视频" : "选集";
  const muted = v.muted || v.volume === 0;
  const mb = $("pcMute");
  if (mb.dataset.state !== String(muted)) {
    mb.dataset.state = String(muted);
    mb.innerHTML = ic(muted ? "mute" : "vol", 20);
    mb.setAttribute("aria-label", muted ? "取消静音" : "静音");
    mb.title = (muted ? "取消静音" : "静音") + "（M）";
  }
  $("pcVol").value = String(v.muted ? 0 : v.volume);
  const full = document.fullscreenElement === $("pcBox");
  const fb = $("pcFull");
  if (fb.dataset.state !== String(full)) {
    fb.dataset.state = String(full);
    fb.innerHTML = ic(full ? "unfull" : "full", 20);
    fb.setAttribute("aria-label", full ? "退出全屏" : "全屏");
    fb.title = (full ? "退出全屏" : "全屏") + "（F）";
  }
  $("pcwin").classList.toggle("compact", $("pcwin").offsetWidth < 440 && !full);
}
function pcTogglePlay() {
  const v = $("watchVideo");
  if (!watching) return;
  if (v.paused) {
    if (watching.stale) { watching.stale = false; seekRestart(pcPos()); return; }
    if (watching.player.ended && watching.kind === "video") {
      hideUpNext();
      if (watching.player.vod && !watching.player.failed) { watching.player.seek(0); v.play().catch(() => {}); return; }
      seekRestart(watching.seekable ? 0 : pcPos());
      return;
    }
    v.play().catch(() => {});
  } else v.pause();
}
function pcToggleFull() {
  const box = $("pcBox");
  if (document.fullscreenElement === box) document.exitFullscreen().catch(() => {});
  else if (box.requestFullscreen) box.requestFullscreen().catch(() => toast("这个浏览器不让全屏", true));
}
function pcToggleMute() {
  const v = $("watchVideo");
  v.muted = !v.muted;
  if (!v.muted && v.volume === 0) v.volume = 0.5;
  lsSet("rootv.muted", v.muted ? "1" : "0");
  renderPc();
}
// ---- 点播：倍速、清晰度、画中画，和出错时换一种放法 ----

function qualityPref() { return lsGet("rootv.quality", "720p") === "1080p" ? "1080p" : "720p"; }
const YT_QUALITIES = ["360p", "480p", "720p", "1080p"];
function ytQualityPref() { const q = lsGet("rootv.ytQuality", "720p"); return YT_QUALITIES.includes(q) ? q : "720p"; }
// 请求里说这个页面会放点播（新的电脑版才说；旧版只会放 HLS），和要的清晰度（web.py 的 _prefer）；能用 MediaSource
// 放 H.264 + AAC 时再说 dash（YouTube 按 DASH 放，ytq 是 YouTube 的清晰度）。hls：true 只要 HLS，"nodash" 不要 DASH。
function vodAsk(body, hls) {
  if (VOD_OK && hls !== true) {
    body.vod = true; body.quality = qualityPref(); body.ytq = ytQualityPref();
    if (DASH_OK && hls !== "nodash") body.dash = true;
  }
  return body;
}
function rateLabel(r) { return (Math.round(r * 100) / 100) + "×"; }
function setRate(r) {
  const v = $("watchVideo");
  v.defaultPlaybackRate = v.playbackRate = r;
  lsSet("rootv.rate", String(r));
  renderPc();
}
function stepRate(delta) {
  if (!watching || !watching.player.vod) return;
  const at = RATES.indexOf(savedRate()), next = RATES[Math.min(RATES.length - 1, Math.max(0, at + delta))];
  setRate(next);
  toast("播放速度 " + rateLabel(next));
}
function pipOk(v) {
  if (document.pictureInPictureEnabled && typeof v.requestPictureInPicture === "function" && !v.disablePictureInPicture) return true;
  return typeof v.webkitSupportsPresentationMode === "function" && v.webkitSupportsPresentationMode("picture-in-picture");
}
function pcTogglePip() {
  const v = $("watchVideo");
  if (!watching || !pipOk(v)) return;
  if (document.pictureInPictureElement) { document.exitPictureInPicture().catch(() => {}); return; }
  if (typeof v.requestPictureInPicture === "function") v.requestPictureInPicture().catch(() => toast("这个浏览器现在不让画中画", true));
  else v.webkitSetPresentationMode(v.webkitPresentationMode === "picture-in-picture" ? "inline" : "picture-in-picture");
}
// 换清晰度：B 站 720P 是点播，1080P 走 HLS（要登录 B 站）；YouTube 是这个视频有的 360P…1080P（都是 DASH，换一个就是
// 另一路）。从现在的位置接着放，选的记在这个浏览器里（两个网站分开记）。
function pickQuality(q) {
  if (!watching || !watching.quality) return;
  lsSet(watching.quality.site === "youtube" ? "rootv.ytQuality" : "rootv.quality", q);
  if (watching.quality.current === q) return;
  const at = Math.round(pcPos() * 10) / 10;
  watchSay("正在换成 " + q.toUpperCase() + "…");
  if (watching.mode === "direct") startDirect(Object.assign({}, watching.again, { start: at }), { group: watching.group, seek: true, again: true });
  else startWatch(false, true, at);
}
// ---- 直播台的线路（server/routes.py）：头上「线路 2/3」，点开选一条，马上从那条重起；起不来中转照常换别的 ----
// 只在直接看（不是跟着小电视）、有几条线路时有。每条后面是上次量到的用时（真放起来的，没有时是检测的）或「打不开」。
function canPickLine() { return !!watching && watching.mode === "direct" && watching.kind === "live" && watching.routes > 1; }
function renderLine() {
  const b = $("watchLine");
  b.hidden = !canPickLine();
  if (b.hidden) { if ($("pcMenu").dataset.kind === "line") closeMenu(); return; }
  b.textContent = "线路 " + (watching.route + 1) + "/" + watching.routes;
  b.setAttribute("aria-label", "线路 " + (watching.route + 1) + "，共 " + watching.routes + " 条：换线路");
}
function lineNote(x) {
  if (!x) return "—";
  if (x.bad) return "✗ 打不开";
  return x.ms ? "✓ " + (x.ms / 1000).toFixed(1) + " 秒" : "—";
}
function pickLine(i) {
  if (!canPickLine() || watching.busy || i === watching.route) return;
  switching(watching.name || watching.channel, "正在换线路 " + (i + 1) + "…");
  startDirect(Object.assign({}, watching.again, { route: i }), { group: watching.group, again: true });
}
// 速度、清晰度、线路的小菜单。
const QUALITY_NOTES = { "720p": "秒开，拖到哪里都快", "1080p": "要转一下，拖动时稍等" };
function menuAnchor(kind) { return kind === "rate" ? $("pcRate") : kind === "line" ? $("watchLine") : $("pcQuality"); }
function openMenu(kind) {
  const menu = $("pcMenu"), anchor = menuAnchor(kind);
  if (!menu.hidden && menu.dataset.kind === kind) { closeMenu(); return; }
  menu.dataset.kind = kind;
  menu.setAttribute("aria-label", kind === "rate" ? "播放速度" : kind === "line" ? "线路" : "清晰度");
  menu.replaceChildren();
  const items = kind === "rate"
    ? RATES.map((r) => ({ value: r, label: rateLabel(r) + (r === 1 ? " 正常" : ""), on: r === savedRate() }))
    : kind === "line"
    ? Array.from({ length: watching.routes }, (_, i) => ({ value: i, label: "线路 " + (i + 1), note: lineNote(watching.lines[i]), on: i === watching.route }))
    : (watching.quality.choices || ["720p", "1080p"]).map((v) => ({
        value: v, label: v.toUpperCase(), note: watching.quality.site === "youtube" ? "" : (QUALITY_NOTES[v] || ""),
        on: watching.quality.current === v }));
  for (const item of items) {
    const b = el("button");
    b.setAttribute("role", "menuitemradio");
    b.setAttribute("aria-checked", String(item.on));
    b.innerHTML = ic("check", 16) + `<span><b>${esc(item.label)}</b>${item.note ? `<small>${esc(item.note)}</small>` : ""}</span>`;
    b.onclick = () => { closeMenu(); if (kind === "rate") setRate(item.value); else if (kind === "line") pickLine(item.value); else pickQuality(item.value); };
    menu.appendChild(b);
  }
  menu.hidden = false;
  const box = $("pcBox").getBoundingClientRect(), r = anchor.getBoundingClientRect();
  menu.style.right = Math.max(6, box.right - r.right) + "px";
  // 线路的按钮在画面上面的标题栏里：菜单从画面顶上往下开；别的在控制条上，往上开。
  menu.style.top = kind === "line" ? "6px" : "";
  menu.style.bottom = kind === "line" ? "auto" : "";
  (menu.querySelector('[aria-checked="true"]') || menu.firstChild).focus({ preventScroll: true });
  pcActive();
}
function closeMenu() {
  const menu = $("pcMenu");
  if (menu.hidden) return;
  const back = menuAnchor(menu.dataset.kind);
  menu.hidden = true;
  if (menu.contains(document.activeElement)) back.focus({ preventScroll: true });
}
document.addEventListener("pointerdown", (event) => {
  const menu = $("pcMenu");
  if (!menu.hidden && !menu.contains(event.target) && ![$("pcRate"), $("pcQuality"), $("watchLine")].includes(event.target)) closeMenu();
}, true);
// YouTube 的 DASH 还没放起来就出错（浏览器放不了、取不到索引）：改要合体的 360p 文件（没有时中转给 HLS）。
function dashFallback(why) {
  if (window.console && console.debug) console.debug("DASH 放不了：" + (why || ""));
  pcStage("换一种方式放…");
  if (watching.mode === "direct") startDirect(Object.assign({}, watching.again, { start: Math.round((watching.start || 0) * 10) / 10 }), { group: watching.group, seek: true, again: true, hls: "nodash" });
  else startWatch(false, true, undefined, "nodash");
}
// 点播还没放起来就出错：多半是这个浏览器放不了这个文件（或者地址不通），改要 HLS（中转用 ffmpeg 转）。
function vodFallback() {
  pcStage("换一种方式放…");
  if (watching.mode === "direct") startDirect(Object.assign({}, watching.again, { start: Math.round((watching.start || 0) * 10) / 10 }), { group: watching.group, seek: true, again: true, hls: true });
  else startWatch(false, true, undefined, true);
}
// 放到一半出错（地址过期、这一路空闲太久被收了）：从现在的位置重要一路；30 秒内又坏了就让人点「重试」。
let vodBrokenAt = 0;
function vodBroken() {
  const pos = pcPos();
  if (Date.now() - vodBrokenAt < 30000) { watchSay("片源断了", true, true); return; }
  vodBrokenAt = Date.now();
  seekRestart(pos);
}

// 不动鼠标 3 秒后收起控制条（暂停、选集或菜单开着、焦点在控制条上时不收）。
let idleTimer = 0;
function pcActive() {
  const box = $("pcBox");
  box.classList.remove("idle");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    const v = $("watchVideo");
    if (!watching || v.paused || !$("pcParts").hidden || !$("pcMenu").hidden || !$("pcUpNext").hidden || $("pcCtl").contains(document.activeElement)) return;
    box.classList.add("idle");
  }, 3000);
}

// 进度条：点、拖（停手 0.45 秒后才让中转重起）、键盘（← → 10 秒，Home / End）。
(function seekBar() {
  const bar = $("pcSeek"), tip = $("pcTip");
  let drag = false;
  const at = (event) => {
    const r = bar.getBoundingClientRect();
    return Math.min(1, Math.max(0, (event.clientX - r.left) / r.width)) * (watching ? watching.duration || 0 : 0);
  };
  const showTip = (event) => {
    if (!watching || !watching.duration || !watching.seekable) { tip.hidden = true; return; }
    const r = bar.getBoundingClientRect();
    tip.hidden = false;
    tip.textContent = clock(at(event));
    tip.style.left = Math.min(r.width - 24, Math.max(24, event.clientX - r.left)) + "px";
  };
  bar.addEventListener("pointerdown", (event) => {
    if (!watching || !watching.seekable || !watching.duration) return;
    drag = true;
    bar.classList.add("drag");
    bar.setPointerCapture(event.pointerId);
    watching.seeking = at(event);
    clearTimeout(seekTimer);
    renderPc(); showTip(event);
  });
  bar.addEventListener("pointermove", (event) => {
    showTip(event);
    if (!drag || !watching) return;
    watching.seeking = at(event);
    renderPc();
  });
  const end = (event) => {
    if (!drag) return;
    drag = false;
    bar.classList.remove("drag");
    if (!watching) return;
    const target = at(event);
    watching.seeking = null;
    pcSeek(target);
  };
  bar.addEventListener("pointerup", end);
  bar.addEventListener("pointercancel", end);
  bar.addEventListener("pointerleave", () => { if (!drag) tip.hidden = true; });
  bar.addEventListener("keydown", (event) => {
    if (!watching || !watching.seekable) return;
    if (event.key === "Home") { event.preventDefault(); event.stopPropagation(); pcSeek(0); }
    else if (event.key === "End" && watching.duration) { event.preventDefault(); event.stopPropagation(); pcSeek(watching.duration - 3); }
  });
})();

$("pcPlay").onclick = pcTogglePlay;
$("pcBack").onclick = () => pcSkip(-10);
$("pcFwd").onclick = () => pcSkip(10);
$("pcNext").onclick = () => {
  const next = nextPart();
  if (next) playPart(next.n);
  else if (watching && !watching.busy) startDirect({ channel: watching.channel, step: 1 }, { group: watching.group });
};
$("pcMute").onclick = pcToggleMute;
$("pcVol").oninput = () => {
  const v = $("watchVideo");
  v.volume = Number($("pcVol").value);
  v.muted = v.volume === 0;
  lsSet("rootv.volume", String(v.volume));
  lsSet("rootv.muted", v.muted ? "1" : "0");
  renderPc();
};
$("pcPartsBtn").onclick = () => { if ($("pcParts").hidden) openParts(); else closeParts(); };
$("pcPartsX").onclick = closeParts;
$("pcFull").onclick = pcToggleFull;
$("pcPip").onclick = pcTogglePip;
$("pcRate").onclick = () => openMenu("rate");
$("pcQuality").onclick = () => openMenu("quality");
$("watchLine").onclick = () => { if (canPickLine()) openMenu("line"); };
$("pcMenu").addEventListener("keydown", (event) => {
  const items = [...$("pcMenu").querySelectorAll("button")], at = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault(); event.stopPropagation();
    items[(at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
  }
});
$("pcFromStart").onclick = () => { $("pcResume").hidden = true; seekRestart(0); };
$("pcResumeX").onclick = () => { $("pcResume").hidden = true; };
$("watchVideo").addEventListener("click", () => { pcTogglePlay(); $("pcBox").focus({ preventScroll: true }); });
$("watchVideo").addEventListener("dblclick", pcToggleFull);
$("pcBox").addEventListener("pointermove", pcActive);
$("pcBox").addEventListener("focusin", pcActive);
document.addEventListener("fullscreenchange", () => { renderPc(); pcActive(); });
{
  const v = $("watchVideo"), volume = Number(lsGet("rootv.volume", "1"));
  v.volume = isFinite(volume) ? Math.min(1, Math.max(0, volume)) : 1;
  v.muted = lsGet("rootv.muted", "0") === "1";
}
for (const ev of ["play", "pause", "volumechange", "progress", "seeked", "ratechange", "durationchange"]) $("watchVideo").addEventListener(ev, () => { renderPc(); if (ev === "play" || ev === "pause") { renderMini(); pcActive(); } });
$("watchVideo").addEventListener("play", () => { if (watching && watching.stale) { watching.stale = false; seekRestart(pcPos()); } });
$("watchVideo").addEventListener("timeupdate", () => { renderPc(); renderMiniProgress(); });

// 播放器的键：画面（或控制条）有焦点、放大了、全屏时——空格 暂停 / 继续，← → 10 秒，F 全屏，M 静音，P 画中画，
// < > 慢一档 / 快一档（点播），Esc 关菜单 / 选集 / 取消下一集。
// 先于整页的方向键焦点引擎（捕获阶段），用掉的键不再往下传。
addEventListener("keydown", (event) => {
  if (!watching || $("pcwin").hidden || $("app").hidden || anyOverlay() || event.altKey || event.metaKey || event.ctrlKey) return;
  const a = document.activeElement, tag = (event.target.tagName || "").toLowerCase();
  if (["input", "select", "textarea"].includes(tag) && event.target.type !== "range") return;
  const box = $("pcBox"), inBox = box.contains(a), full = document.fullscreenElement === box;
  const big = $("pcwin").classList.contains("big") && !(a && a.closest && a.closest("#view, #top, #mini, #tabs"));
  if (!inBox && !full && !big) return;
  const key = event.key, inParts = $("pcParts").contains(a);
  const eat = () => { event.preventDefault(); event.stopPropagation(); pcActive(); };
  if (key === "Escape") {
    if (!$("pcMenu").hidden) { eat(); closeMenu(); return; }
    if (!$("pcParts").hidden) { eat(); closeParts(); return; }
    if (upNext) { eat(); $("pcUpNextStop").click(); return; }
    return;                          // 全屏时浏览器自己退出；放大时整页的处理缩小
  }
  if (inParts || $("pcMenu").contains(a)) return;   // 选集列表、菜单里方向键照常移动焦点
  const onButton = a && a.tagName === "BUTTON" && box.contains(a);
  if (key === " " || key === "k" || key === "K") {
    if (key === " " && onButton) return; // 空格按的是那个按钮
    eat(); pcTogglePlay();
  } else if ((key === "ArrowLeft" || key === "ArrowRight") && !onButton && a !== $("pcVol")) {
    if (!watching.seekable) return;
    eat(); pcSkip(key === "ArrowLeft" ? -10 : 10);
  } else if (key === "f" || key === "F") { eat(); pcToggleFull(); }
  else if (key === "m" || key === "M") { eat(); pcToggleMute(); }
  else if (key === "p" || key === "P") { if (pipOk($("watchVideo"))) { eat(); pcTogglePip(); } }
  else if (key === "<" || key === ">") { if (watching.player.vod) { eat(); stepRate(key === ">" ? 1 : -1); } }
}, true);

// 跟随小电视：看它正在播的台。start 给了是在电脑上跳转（只动电脑：这个页面另起一路）。
async function startWatch(auto, again, start, hls) {
  const device = deviceId(), key = watchKey(data.now);
  if (watching) watching.busy = true;
  let answer;
  const body = vodAsk(withDevice({ viewer }), hls);
  if (start !== undefined) body.start = start;
  try { answer = await call("POST", "api/watch", body); }
  catch (error) {
    if (watching) watching.busy = false;
    if (error instanceof SignedOut) signedOut(); else toast("网络不通", true);
    return false;
  }
  if (!answer.ok) {
    if (watching && auto) { watching.key = key; watching.busy = false; watchSay(answer.error, true, true); }
    else { if (watching) watching.busy = false; toast(answer.error, true); }
    return false;
  }
  if (watching && watching.mode === "follow" && watching.sid === answer.sid && watching.device === device && !again) {
    watching.key = key; watching.busy = false; return true;       // 还是同一个片源，接着放
  }
  const player = playerFor(answer);
  miniWho = "pc";
  watching = { mode: "follow", device, sid: answer.sid, key, player, busy: false, channel: answer.channel, kind: answer.kind,
               name: data.now.name || answer.channel, title: answer.title || data.now.name,
               start: answer.start || 0, duration: answer.duration || 0, seekable: !!answer.seekable, seeking: null,
               parts: null, own: !!answer.own, quality: answer.quality || null };
  $("watchFollow").checked = true;
  watching.label = deviceName() + " · " + (data.now.name || answer.channel) + (answer.title && answer.title !== data.now.name ? " · " + answer.title : "");
  $("pcResume").hidden = true;
  hideUpNext(); closeParts();
  renderWatchTitle();
  renderWatch();
  renderMini();
  renderPc();
  player.start();
  return true;
}

// 直接看：body 是 {channel[, episode | step]}、{url}（投到电脑）或 {video: {site, id}}（投屏历史），
// 再可以带 part（第几集）、start（从第几秒起：跳转、从头播放）。
async function startDirect(body, ctx) {
  ctx = ctx || {};
  if (watching) watching.busy = true;
  let answer;
  const request = vodAsk(Object.assign({ viewer }, body), ctx.hls);
  if (body.channel === "cast" && deviceId()) request.device = deviceId();
  try { answer = await call("POST", "api/watch", request); }
  catch (error) {
    if (watching) watching.busy = false;
    switched();
    if (error instanceof SignedOut) signedOut(); else toast("网络不通", true);
    return false;
  }
  if (!answer.ok) {
    if (watching) watching.busy = false;
    switched();
    toast(answer.error, true);
    return false;
  }
  openDirect(answer, ctx);
  return true;
}
// 「在电脑上看」开始了（POST /api/watch 或 /api/play 的回答）：接上播放器。
function openDirect(answer, ctx) {
  ctx = ctx || {};
  const same = watching && watching.mode === "direct" && watching.sid === answer.sid && !ctx.again;
  if (same) switched();      // 还是正在放的那一路（又点了同一个台）
  const player = same ? watching.player : playerFor(answer);
  // 重试、跳转用的请求：不再「下一个」，而是回到这一个（UP 主、剧集按第几个；投屏的按视频和第几集）。
  const again = answer.index ? { channel: answer.channel, episode: answer.index }
    : answer.video && (answer.channel === "cast" || answer.part) ? Object.assign({ video: answer.video }, answer.part ? { part: answer.part } : {})
    : { channel: answer.channel };
  watching = { mode: "direct", device: deviceId(), sid: answer.sid, key: "", player, busy: false, channel: answer.channel,
               group: ctx.group || groupOf(answer.channel), name: answer.name, title: answer.title, kind: answer.kind,
               index: answer.index || 0, total: answer.total || 0, series: !!answer.series, again,
               start: answer.start || 0, duration: answer.duration || 0, seekable: !!answer.seekable, seeking: null,
               parts: answer.parts || null, part: answer.part || answer.index || 0, video: answer.video || null,
               quality: answer.quality || null,
               routes: answer.routes || 1, route: answer.route || 0, lines: answer.lines || [] };
  miniWho = "pc";
  $("watchFollow").checked = false;
  const parts = answer.parts;
  const what = parts && parts.items.length > 1 && parts.kind !== "up"
    ? partWord(parts.current) + " / " + parts.items.length + (answer.title ? " · " + answer.title : "")
    : answer.title && answer.title !== answer.name ? answer.title : "";
  const name = answer.item_title || answer.name || answer.channel;
  watching.label = "这台电脑 · " + name + (what ? " · " + what : "");
  if (answer.item_title) watching.name = answer.item_title;
  hideUpNext();
  if (!ctx.seek) closeParts();
  else if (!$("pcParts").hidden) openParts();
  // 接着上次看的地方：说一声，给一个「从头播放」。
  $("pcResume").hidden = !(answer.resume > 0 && !ctx.seek);
  if (answer.resume > 0) $("pcResumeText").textContent = "接着上次看到的 " + clock(answer.resume);
  if (!$("pcResume").hidden) setTimeout(() => { if (watching && watching.sid === answer.sid) $("pcResume").hidden = true; }, 9000);
  renderWatchTitle();
  render();
  renderPc();
  pcActive();
  if (!same) player.start();
  setTimeout(() => loadHome(true), 4000);
}

function stopWatch() {
  if (opening) {
    // 还在等中转回答时关掉：取消。回答到了也不放（playOnPc 收到后会让中转停掉那一路）。
    opening = null;
    if (!watching) call("POST", "api/watch/stop", { viewer }).catch(() => {});
  }
  if (watching) {
    const body = pcTracked() ? pcBody() : { viewer };
    watching.player.stop();
    // 直接看的那一路只给这个页面用：马上停，让出名额（跟随的那一路全家共用，没人看 20 秒后自己停）。
    if (watching.mode === "direct" || watching.own) call("POST", "api/watch/stop", body).catch(() => {});
  }
  clearTimeout(seekTimer);
  clearTimeout(warmTimer);
  switched();
  hideUpNext();
  closeParts();
  closeMenu();
  if (document.fullscreenElement === $("pcBox")) document.exitFullscreen().catch(() => {});
  watching = null;
  miniWho = "tv";
  histAfterPlay();
  watchSay("");
  $("pcResume").hidden = true;
  $("pcwin").hidden = true;
  $("pcwin").classList.remove("big");
  if (data) render();
  setTimeout(() => loadHome(true), 1500);
}

// 上一台 / 下一台：在点台时的那一组里挨着换。
function stepChannel(delta) {
  if (!watching || watching.mode !== "direct" || watching.busy || !data) return;
  const g = data.groups.find((x) => x.name === watching.group && x.channels.some((c) => c.id === watching.channel))
    || data.groups.find((x) => x.channels.some((c) => c.id === watching.channel));
  if (!g) return;
  const list = g.channels, at = list.findIndex((c) => c.id === watching.channel);
  const next = list[(at + delta + list.length) % list.length];
  if (watching.kind === "live" && !isRelay(next.id)) switching(next.name);
  startDirect({ channel: next.id }, { group: g.name });
}

// 播放器标题：直播台有节目单时是「CCTV5 · 运动一起赢」，下面一行是时间和接下来的节目。
function watchEpg() {
  const live = watching && watching.channel && !isRelay(watching.channel) && watching.kind !== "video";
  return live ? epgNow(watching.channel) : { now: null, next: null };
}
function watchHeadline() {
  if (!watching) return "";
  const p = watchEpg().now;
  return p ? (watching.name || watching.channel) + " · " + p.title : watching.title || watching.name || "在电脑上看";
}
function renderWatchTitle() {
  if (!watching) return;
  const e = watchEpg();
  $("watchWhat").textContent = watchHeadline();
  $("watchSub").textContent = e.now
    ? [watching.mode === "direct" ? "这台电脑" : (watching.label || ""), hm(e.now.start) + "–" + hm(e.now.end),
       e.next ? "接下来 " + hm(e.next.start) + " " + e.next.title : ""].filter(Boolean).join(" · ")
    : (watching.label || "");
  $("watchWhat").title = $("watchWhat").textContent;
}

function renderWatch() {
  const can = !!data.watch;
  const online = tvOnline();
  // 小电视在线时才有「在电脑上看它正在播的」。
  $("watchOpen").hidden = !can || !online;
  if (!can) { if (watching) stopWatch(); return; }
  const following = watching && watching.mode === "follow";
  $("watchOpen").disabled = !!following;
  $("watchOpen").textContent = following ? "正在电脑上看" : watching ? "改成跟着小电视看" : "在电脑上看这个";
  if (!watching) return;
  const direct = watching.mode === "direct";
  $("watchPrevCh").hidden = $("watchNextCh").hidden = !direct || watching.channel === "cast";
  renderLine();
  const video = direct && watching.kind === "video" && /^(up|sr)\d\d$/.test(watching.channel);
  $("watchFollowBox").hidden = !online;
  const d = data.now.device;
  // 只在跟着小电视看时说一句（两边不同步）；直接在电脑上看不用解释。
  $("watchNote").textContent = direct ? ""
    : "和小电视各放各的，电脑上会晚几秒" + (watching.seekable ? "；拖进度只动电脑" : "") + (d && d.audio_only === 1 ? "；小电视现在「只听」" : "");
  $("watchNote").hidden = direct;
  // 跟随：小电视换了台（或下一个视频）、电脑版换了设备，就重新要一次地址；还是同一个片源时不打断。
  if (!direct && $("watchFollow").checked && online && !watching.busy &&
      (watchKey(data.now) !== watching.key || watching.device !== deviceId())) startWatch(true);
  renderPc();
}
$("watchOpen").onclick = () => { closeSheets(true); startWatch(false); };
$("watchClose").onclick = stopWatch;
$("watchPrevCh").onclick = () => stepChannel(-1);
$("watchNextCh").onclick = () => stepChannel(1);
$("watchFollow").onchange = () => { if ($("watchFollow").checked && watching && watching.mode === "direct") startWatch(false); };
// 电脑上看的小窗：宽屏（.pcdock，见 console.css）时页面右边让出小窗的位置，不挡内容；窄一点的窗口里小窗
// 可以按住标题栏拖开（拖过就不再让位置，放大、关掉以后回到右下角）。
function pcDock() {
  const w = $("pcwin");
  $("app").classList.toggle("pcdock", !w.hidden && !w.classList.contains("big") && !w.dataset.moved);
}
new MutationObserver(() => {
  const w = $("pcwin");
  if ((w.hidden || w.classList.contains("big")) && w.dataset.moved) { delete w.dataset.moved; w.style.left = w.style.top = w.style.right = w.style.bottom = ""; }
  pcDock();
}).observe($("pcwin"), { attributes: true, attributeFilter: ["hidden", "class"] });
qs("#pcwin .pchead").addEventListener("pointerdown", (event) => {
  const w = $("pcwin");
  if (event.button !== 0 || w.classList.contains("big") || isM() || event.target.closest("button, a, input")) return;
  const box = w.getBoundingClientRect(), area = w.offsetParent.getBoundingClientRect();
  const dx = event.clientX - box.left, dy = event.clientY - box.top;
  let moved = false;
  const move = (e) => {
    if (!moved && Math.abs(e.clientX - event.clientX) + Math.abs(e.clientY - event.clientY) < 6) return;
    moved = true;
    const x = Math.min(Math.max(0, e.clientX - area.left - dx), area.width - box.width);
    const y = Math.min(Math.max(0, e.clientY - area.top - dy), area.height - box.height);
    Object.assign(w.style, { left: x + "px", top: y + "px", right: "auto", bottom: "auto" });
    if (!w.dataset.moved) { w.dataset.moved = "1"; pcDock(); }
  };
  const up = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", up); };
  addEventListener("pointermove", move);
  addEventListener("pointerup", up);
});
$("watchBig").onclick = () => {
  const big = $("pcwin").classList.toggle("big");
  $("watchBig").innerHTML = ic(big ? "shrink" : "expand", 16);
  $("watchBig").setAttribute("aria-label", big ? "缩小" : "放大");
  $("watchBig").title = big ? "缩小" : "放大";
  renderPc();
};

// ======================================================================= 直播：分组 + 频道卡片 / 节目表

// 直播页上面的分段：频道（卡片）| 节目表（网格；手机上是每台的现在和接下来）。每个浏览器记住选的哪个。
let liveView = lsGet("rootv.liveView", "grid") === "guide" ? "guide" : "grid";
let guideSpan = GUIDE.SPANS.includes(Number(lsGet("rootv.guideSpan", ""))) ? Number(lsGet("rootv.guideSpan", "")) : GUIDE.SPAN;
function setLiveView(view) {
  liveView = view === "guide" ? "guide" : "grid";
  lsSet("rootv.liveView", liveView);
  guideScroll = null;       // 打开节目表时从「现在」开始看
  closeGuidePop(true);
  renderLive();
}
for (const b of qsa("#liveSeg [data-lv]")) b.onclick = () => setLiveView(b.dataset.lv);
for (const b of qsa("#guideSpan [data-span]")) b.onclick = () => {
  guideSpan = Number(b.dataset.span);
  lsSet("rootv.guideSpan", String(guideSpan));
  renderGuide(true);
};
$("guideNow").onclick = () => guideToNow(true);

function renderLive() {
  if (route !== "live" || !data) return;
  renderGroups();
  for (const b of qsa("#liveSeg [data-lv]")) b.setAttribute("aria-pressed", String(b.dataset.lv === liveView));
  $("liveGrid").hidden = liveView !== "grid";
  $("liveGuide").hidden = liveView !== "guide";
  if (liveView === "grid") renderChannels(); else renderGuide();
  renderLiveHint();
}
function renderLiveHint() {
  const to = currentTarget();
  const tap = !to ? "还没有小电视。" + (data.watch ? "" : "绑定以后点台就能换台。")
    : to === PC ? "点一下，在这台电脑上看（不动小电视）。" : "点一下，「" + targetName(to) + "」就换到这个台。";
  $("tapHint").textContent = tap;
  $("liveHint").textContent = data.epg ? "节目信息来自节目单（EPG）" : "";
  $("guideHint").textContent = (liveView === "guide" && !isM() ? "方向键在节目之间移动，回车播放或看详情。" : "")
    + "点台名或正在播的节目就播放：" + (to === PC ? "在这台电脑上看。" : to ? "「" + targetName(to) + "」换到这个台。" : "先绑定小电视。")
    + (data.now.online && data.now.channel ? "左边有色条的是「" + deviceName() + "」正在播的台。" : "");
}
// 小电视没在播时，默认选第一个有直播台的组（不是只有一张投屏卡片的「我的投屏」）。
function defaultGroup() {
  const names = data.groups.map((g) => g.name);
  if (data.now.group && names.includes(data.now.group) && data.now.online) return data.now.group;
  const live = data.groups.find((g) => g.channels.some((c) => !isRelay(c.id)));
  return (live || data.groups[0] || {}).name || "";
}
let groupsSig = "";
function renderGroups() {
  const names = data.groups.map((g) => g.name);
  if (!names.includes(group)) group = defaultGroup();
  const box = $("groups");
  const sig = JSON.stringify([group, data.groups.map((g) => [g.name, g.channels.length])]);
  if (sig === groupsSig && box.childElementCount) return;
  groupsSig = sig;
  const fk = focusKey();
  box.replaceChildren();
  for (const g of data.groups) {
    const b = el("button", "", g.name);
    b.setAttribute("aria-pressed", String(g.name === group));
    b.appendChild(el("em", "", String(g.channels.length)));
    b.dataset.k = "grp-" + g.name;
    b.onclick = () => { group = g.name; closeGuidePop(true); renderLive(); loadEpg(); };
    box.appendChild(b);
  }
  restoreFocus(fk);
}
const isRelay = (id) => /^(up|sr)\d\d$/.test(id) || id === "cast";
// 点一个台：播到「播放到」选的地方（小电视在播的这个台、又是对着它，就打开遥控）。
function playChannel(c) {
  if (!c) return;
  const air = !pending && c.id === data.now.channel && data.now.online;
  if (air && currentTarget() === deviceId()) { openRemote(); return; }
  play({ kind: isRelay(c.id) ? "channel" : "live", channel: c.id, title: c.name }, null, c.name);
}
function channelById(id) {
  for (const g of data.groups) { const c = g.channels.find((x) => x.id === id); if (c) return c; }
  return null;
}
const logoImg = (id) => `<img src="api/art/logo/${encodeURIComponent(id)}" alt="" loading="lazy" decoding="async">`;
// 没有节目单时（UP 主、剧集、投屏台）在节目表里写的一句话。
function relayLine(c, sites) {
  if (c.id === "cast") return "投屏：手机或电脑投过来的视频";
  if (/^up/.test(c.id)) return (sites[c.id] ? siteTag(sites[c.id]) + " · " : "") + "UP 主的视频，一个接一个播";
  return (sites[c.id] ? siteTag(sites[c.id]) + " · " : "") + "剧集，按顺序接着上次播";
}
function relaySites() {
  const sites = {};
  for (const c of data.creators) sites[c.id] = c.site;
  for (const s of data.series || []) sites[s.id] = s.site;
  return sites;
}
let channelsSig = "";
function renderChannels() {
  if (route !== "live" || liveView !== "grid" || !data) return;
  prepareGroupSoon();
  const grid = $("channels");
  const current = data.groups.find((g) => g.name === group);
  const shown = current ? current.channels.map((c) => epgNow(c.id)) : [];
  const sig = JSON.stringify([group, current, data.now.channel, data.now.online, pending, watching && watching.channel, adjusting,
                              Math.floor(Date.now() / 60000), shown, deviceId()]);
  if (sig === channelsSig && grid.childElementCount) return;
  channelsSig = sig;
  const fk = focusKey();
  grid.replaceChildren();
  if (!current) return;
  const sites = relaySites();
  const movable = !["我的投屏", "UP 主"].includes(group);
  const nowS = Date.now() / 1000;
  current.channels.forEach((c, i) => {
    const air = !pending && c.id === data.now.channel && data.now.online;
    const going = pending && pending.kind === "switch" && c.id === pending.channel;
    const pc = watching && watching.mode === "direct" && watching.channel === c.id;
    const card = el("button", "card" + (air ? " air" : "") + (going ? " going" : "") + (pc ? " pcair" : ""));
    card.dataset.k = "ch-" + c.id;
    const e = shown[i];
    const relay = isRelay(c.id);
    const badge = air ? deviceName() + "在播" : pc ? "电脑上在放" : relay ? (c.id === "cast" ? "投屏" : sites[c.id] ? siteTag(sites[c.id]) : "") : "直播";
    const logo = relay ? "" : `<span class="logo-big">${badgeSVG(c.name)}${logoImg(c.id)}</span>`;
    const pct = progressOf(e.now, nowS);
    const line = going ? "切换中…" : e.now ? epgText(e.now, hm) : relay ? relayLine(c, sites) : "";
    card.innerHTML = `<span class="art">${art(c.name, "w", { title: relay ? c.name : "" })}${logo}${badge ? `<span class="badge ${relay && !air && !pc ? "" : "live"}">${esc(badge)}</span>` : ""}`
      + `<span class="badge r">${pad(c.n)}</span>${e.now ? `<span class="pbar"><i style="width:${(pct * 100).toFixed(1)}%"></i></span>` : ""}</span>`
      + `<span class="t">${esc(c.name)}</span>`
      + (e.now && !going ? `<span class="s onair"><span class="et">正在播：${esc(e.now.title)}</span><span class="tm">&nbsp;· ${hm(e.now.start)}–${hm(e.now.end)}</span></span>`
        : `<span class="s">${esc(line) || "&nbsp;"}</span>`)
      + `<span class="s2">${e.next ? "接下来 " + hm(e.next.start) + " " + esc(e.next.title) : "&nbsp;"}</span>`;
    card.setAttribute("aria-label", "播放 " + c.name + (e.now ? "，正在播 " + e.now.title + "，" + hm(e.now.start) + " 到 " + hm(e.now.end) : ""));
    if (adjusting && movable) {
      const select = el("select", "field");
      select.style.marginTop = "8px";
      for (const name of data.group_names) {
        const option = el("option", "", name);
        option.value = name;
        option.selected = name === group;
        select.appendChild(option);
      }
      select.onclick = (event) => event.stopPropagation();
      select.onchange = async () => {
        try {
          const answer = await call("POST", "api/groups", { channel: c.id, group: select.value });
          if (!answer.ok) toast(answer.error, true); else toast("已移到「" + select.value + "」");
        } catch (error) { if (error instanceof SignedOut) signedOut(); }
        refresh();
      };
      const wrap = el("div");
      wrap.append(card, select);
      card.onclick = null;
      grid.appendChild(wrap);
      return;
    }
    // 这台电脑上正在看直播、点台也是在电脑上放：鼠标停在直播台上就先转起来（预热）。
    if (!relay) warmOnHover(card, () => (currentTarget() === PC ? c.id : null));
    card.onclick = () => { if (!going) playChannel(c); };
    grid.appendChild(card);
  });
  restoreFocus(fk);
}
$("adjust").onclick = () => {
  adjusting = !adjusting;
  $("adjust").textContent = adjusting ? "完成" : "调整分组";
  $("adjust").classList.toggle("on", adjusting);
  if (data) renderChannels();
};

// ---------- 节目表：行是这一组的台，横着是时间（现在前 1 小时到后几个小时），竖线是现在 ----------
let guideData = null;     // {key, at, from, to, channels: {台 id: [节目…]}}
let guideBusy = "";
// 节目表要的台：这一组的台（直播台问节目单；UP 主、剧集、投屏台也列出来，写一句它播什么）。
function guideChannels() {
  const g = data.groups.find((x) => x.name === group);
  return g ? g.channels.slice(0, GUIDE.ROWS) : [];
}
async function loadGuide(force) {
  if (route !== "live" || liveView !== "guide" || !data || !data.epg) return;
  const ids = guideChannels().map((c) => c.id).filter((id) => !isRelay(id));
  const w = guideWindow(Date.now() / 1000, guideSpan, epgWin);
  const key = [group, w.from, w.to, ids.join(",")].join("|");
  if (!ids.length || guideBusy === key) return;
  if (!force && guideData && guideData.key === key && Date.now() - guideData.at < 300000) return;
  guideBusy = key;
  const chunks = [];
  for (let i = 0; i < ids.length; i += GUIDE.CHUNK) chunks.push(ids.slice(i, i + GUIDE.CHUNK));
  let answers;
  try {
    answers = await Promise.all(chunks.map((c) => call("GET", "api/epg?channels=" + c.map(encodeURIComponent).join(",")
      + "&from=" + w.from + "&to=" + w.to + "&max=" + GUIDE.MAX)));
  } catch (error) { guideBusy = ""; if (error instanceof SignedOut) signedOut(); return; }
  guideBusy = "";
  if (answers.some((a) => !a || !a.ok)) return;
  const channels = {};
  for (const a of answers) {
    if (a.window) epgWin = a.window;
    for (const [id, e] of Object.entries(a.channels || {})) {
      channels[id] = e.programmes || [];
      epg[id] = { now: e.now, next: e.next };
    }
  }
  for (const id of ids) { epgAsked.add(id); if (!channels[id]) delete epg[id]; }
  guideData = { key, at: Date.now(), from: w.from, to: w.to, channels };
  renderGuide(true);
}
function dayLabel(at) {
  const d = new Date(at * 1000), t = new Date();
  const same = (x, y) => x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
  const tomorrow = new Date(t.getFullYear(), t.getMonth(), t.getDate() + 1);
  const word = same(d, t) ? "今天" : same(d, tomorrow) ? "明天" : "";
  return (word ? word + " · " : "") + (d.getMonth() + 1) + "月" + d.getDate() + "日 周" + "日一二三四五六"[d.getDay()];
}
let guideSig = "", guideScroll = null;
function renderGuide(force) {
  if (route !== "live" || liveView !== "guide" || !data) return;
  const box = $("guide");
  $("guideTools").hidden = !data.epg;
  if (!data.epg) {
    guideSig = "";
    box.innerHTML = `<div class="empty"><div class="eic">${ic("guide", 40)}</div><b>还没有节目单</b><p>${me && me.role === "admin"
      ? "在「管理 → 频道表 → 节目单」里选一个来源就有了。"
      : "请管理员在「管理 → 频道表 → 节目单」里打开。"}</p>${me && me.role === "admin" ? `<a class="btn sm" href="#/admin/channels">去打开</a>` : ""}</div>`;
    return;
  }
  const nowS = Date.now() / 1000;
  const m = isM();
  const chans = guideChannels();
  const w = guideWindow(nowS, guideSpan, epgWin);
  const have = guideData && guideData.from === w.from && guideData.to === w.to && guideData.key.startsWith(group + "|") ? guideData : null;
  for (const b of qsa("#guideSpan [data-span]")) b.setAttribute("aria-pressed", String(Number(b.dataset.span) === guideSpan));
  const sig = JSON.stringify([group, m, w, have && have.at, Math.floor(nowS / 60), data.now.channel, data.now.online,
                              watching && watching.channel, pending && pending.channel, chans.map((c) => [c.id, c.name, epgNow(c.id)])]);
  // 详情弹着时不重画（它指着的那个节目块会没了）；关掉以后下一次刷新再画。
  if (box.childElementCount && (!$("gpop").hidden || (!force && sig === guideSig))) { loadGuide(); return; }
  guideSig = sig;
  const fk = focusKey();
  const sc = qs(".gscroll", box);
  const keep = sc ? { x: sc.scrollLeft, y: sc.scrollTop } : null;
  const sites = relaySites();
  const listOf = (c) => (have && have.channels[c.id]) || (() => { const e = epg[c.id]; return e ? [e.now, e.next].filter(Boolean) : []; })();
  const onTv = data.now.online ? data.now.channel : "";
  if (!chans.length) box.innerHTML = `<div class="empty"><p>这一组没有台。</p></div>`;
  else if (m) box.innerHTML = guideListHTML(chans, listOf, nowS, sites, onTv);
  else box.innerHTML = guideGridHTML(chans, listOf, w, nowS, sites, onTv, !have);
  const sc2 = qs(".gscroll", box);
  if (sc2) {
    // 同一组重画：停在原来看的时间（半点一过时间段往后挪了，滚动位置跟着挪）；换了组、第一次画：回到现在。
    if (keep && guideScroll && guideScroll.group === group) {
      sc2.scrollLeft = keep.x - (w.from - guideScroll.from) / 60 * GUIDE.PPM;
      sc2.scrollTop = keep.y;
    } else guideToNow(false);
    guideScroll = { group, from: w.from };
    pinGuide();
    sc2.addEventListener("scroll", () => requestAnimationFrame(pinGuide), { passive: true });
  }
  restoreFocus(fk);
  loadGuide();
}
function guideGridHTML(chans, listOf, w, nowS, sites, onTv, loading) {
  const ppm = GUIDE.PPM, W = (w.to - w.from) / 60 * ppm;
  const ticks = guideTicks(w.from, w.to, GUIDE.STEP_S).map((t) => {
    const label = hm(t) === "00:00" ? dayLabel(t).replace(/^.* · /, "").replace(/ 周.$/, "") : hm(t);
    return `<span style="left:${((t - w.from) / 60 * ppm).toFixed(1)}px">${esc(label)}</span>`;
  }).join("");
  const rows = chans.map((c) => {
    const relay = isRelay(c.id), list = relay ? [] : listOf(c);
    const blocks = guideBlocks(list, w.from, w.to, nowS, ppm);
    const cur = nowNext(list, nowS).now;
    const head = `<button class="gch${c.id === onTv ? " cur" : ""}" data-gplay="${esc(c.id)}" data-k="gch-${esc(c.id)}" aria-label="播放 ${esc(c.name)}${cur ? "，正在播 " + esc(cur.title) : ""}">`
      + `<span class="no">${pad(c.n)}</span><span class="lg">${relay ? avatarSVG(c.name, 34) : badgeSVG(c.name) + logoImg(c.id)}</span>`
      + `<span class="nm">${esc(c.name)}${cur ? `<small>${esc(cur.title)}</small>` : ""}</span></button>`;
    let cells;
    if (blocks.length) {
      cells = blocks.map((b) => `<button class="gp ${b.state}" style="left:${(b.left + 2).toFixed(1)}px;width:${Math.max(4, b.width - 4).toFixed(1)}px" data-gp="${esc(c.id)}:${b.p.start}" data-k="gp-${esc(c.id)}-${b.p.start}" data-left="${b.left.toFixed(1)}" data-w="${b.width.toFixed(1)}"`
        + ` aria-label="${esc(c.name)} ${hm(b.p.start)} 到 ${hm(b.p.end)} ${esc(b.p.title)}${b.state === "now" ? "，正在播，回车播放" : b.state === "past" ? "，已经播完" : ""}">`
        + `<b>${esc(b.p.title)}</b><small>${hm(b.p.start)}–${hm(b.p.end)}</small>${b.state === "now" ? `<span class="gpb" style="width:${(b.pct * 100).toFixed(1)}%"></span>` : ""}</button>`).join("");
    } else {
      const text = relay ? relayLine(c, sites) : loading && epgAsked.has(c.id) === false ? "正在读节目单…" : "没有节目信息 · 点这里看这个台";
      cells = `<button class="gp none" style="left:2px;width:${(W - 4).toFixed(1)}px" data-gplay="${esc(c.id)}" data-k="gp-${esc(c.id)}-none" data-left="0" data-w="${W.toFixed(1)}" aria-label="播放 ${esc(c.name)}"><b>${esc(text)}</b></button>`;
    }
    return `<div class="grow"><div class="gcell">${head}</div><div class="gprogs" style="width:${W.toFixed(1)}px">${cells}</div></div>`;
  }).join("");
  const nowX = GUIDE.CHW + (nowS - w.from) / 60 * ppm;
  return `<div class="gscroll" style="--chw:${GUIDE.CHW}px"><div class="ginner" style="width:${(GUIDE.CHW + W).toFixed(0)}px">`
    + `<div class="gruler"><div class="gcorner">${esc(dayLabel(nowS))}</div><div class="gticks" style="width:${W.toFixed(0)}px">${ticks}`
    + `<b class="gnowtag" style="left:${(nowX - GUIDE.CHW).toFixed(1)}px" aria-hidden="true">${hm(nowS)}</b></div></div>`
    + rows + `<div class="gnow" style="left:${nowX.toFixed(1)}px" aria-hidden="true"></div></div></div>`;
}
// 手机：每个台一行，现在播的（进度）和接下来的；点一行就播这个台。
function guideListHTML(chans, listOf, nowS, sites, onTv) {
  return `<div class="glist">` + chans.map((c) => {
    const relay = isRelay(c.id), nn = relay ? { now: null, next: null } : nowNext(listOf(c), nowS);
    const p = nn.now, n = nn.next;
    const body = p ? `<span class="gn">${esc(p.title)}</span><span class="gtm"><span>${hm(p.start)}–${hm(p.end)}</span><span class="trk"><i style="width:${(progressOf(p, nowS) * 100).toFixed(1)}%"></i></span></span>`
      : `<span class="gn dim">${esc(relay ? relayLine(c, sites) : "没有节目信息")}</span>`;
    return `<button class="gli${c.id === onTv ? " cur" : ""}" data-gplay="${esc(c.id)}" data-k="gli-${esc(c.id)}" aria-label="播放 ${esc(c.name)}${p ? "，正在播 " + esc(p.title) : ""}">`
      + `<span class="lg">${relay ? avatarSVG(c.name, 40) : badgeSVG(c.name) + logoImg(c.id)}</span>`
      + `<span class="gtx"><span class="gname"><span class="no">${pad(c.n)}</span>${esc(c.name)}${c.id === onTv ? `<em>${esc(deviceName())}在播</em>` : ""}</span>${body}`
      + (n ? `<span class="gx">接下来 ${hm(n.start)} ${esc(n.title)}</span>` : "") + `</span></button>`;
  }).join("") + `</div>`;
}
// 横向滚动：被左边切掉一截的节目把标题挪到看得见的地方。
function pinGuide() {
  const sc = qs("#guide .gscroll");
  if (!sc) return;
  const x = sc.scrollLeft;
  for (const b of qsa(".gp", sc)) b.style.paddingLeft = pinPad(Number(b.dataset.left), Number(b.dataset.w), x) + "px";
}
// 「回到现在」：把现在那条线挪到左边台名旁边一点的地方。
function guideToNow(smooth) {
  const sc = qs("#guide .gscroll");
  if (!sc) return;
  const w = guideWindow(Date.now() / 1000, guideSpan, epgWin);
  const x = Math.max(0, (Date.now() / 1000 - w.from) / 60 * GUIDE.PPM - 150);
  sc.scrollTo({ left: x, behavior: smooth && !RM.matches ? "smooth" : "auto" });
}
// 点节目表：台名、正在播的节目 → 播；过去和以后的节目 → 弹出详情。
$("guide").addEventListener("click", (event) => {
  const t = event.target.closest("[data-gplay], [data-gp]");
  if (!t || !data) return;
  if (t.dataset.gplay) { playChannel(channelById(t.dataset.gplay)); return; }
  const [id, start] = t.dataset.gp.split(":");
  const c = channelById(id), list = (guideData && guideData.channels[id]) || [];
  const e = epg[id] || {};
  const p = list.concat([e.now, e.next]).find((x) => x && String(x.start) === start);
  if (!c || !p) return;
  const nowS = Date.now() / 1000;
  if (p.start <= nowS && nowS < p.end) { playChannel(c); return; }
  openGuidePop(t, c, p);
});
let popFrom = null;
function openGuidePop(anchor, c, p) {
  const pop = $("gpop"), nowS = Date.now() / 1000, cur = epgNow(c.id).now;
  const past = p.end <= nowS, mins = Math.round((p.end - p.start) / 60), wait = Math.max(1, Math.round((p.start - nowS) / 60));
  const waitText = wait >= 60 ? Math.floor(wait / 60) + " 小时" + (wait % 60 ? " " + (wait % 60) + " 分钟" : "") : wait + " 分钟";
  pop.innerHTML = `<div class="gpc">${pad(c.n)} · ${esc(c.name)}</div><h4 id="gpopT">${esc(p.title)}</h4>`
    + `<div class="gpt">${dayLabel(p.start).replace(/ 周.$/, "")} ${hm(p.start)}–${hm(p.end)} · ${mins} 分钟</div>`
    + `<p>${past ? "已经播完，暂时不能回看。" : hm(p.start) + " 开始，还有 " + waitText + "。到时候再来点它，或者现在先看这个台正在播的。"}</p>`
    + `<div class="actions"><button class="btn pri sm" data-popplay>${ic("play", 16)}看这个台现在播的${cur ? `<small>${esc(cur.title)}</small>` : ""}</button><button class="btn sm" data-popclose>关闭</button></div>`;
  popFrom = anchor;
  pop.hidden = false;
  const r = anchor.getBoundingClientRect(), pw = Math.min(340, innerWidth - 24);
  pop.style.width = pw + "px";
  const ph = pop.offsetHeight, below = r.bottom + 8 + ph < innerHeight - 90;
  pop.style.left = Math.max(12, Math.min(innerWidth - pw - 12, r.left + Math.min(r.width, 300) / 2 - pw / 2)) + "px";
  pop.style.top = Math.max(12, below ? r.bottom + 8 : r.top - 8 - ph) + "px";
  pop.querySelector("[data-popplay]").onclick = () => { closeGuidePop(true); playChannel(c); };
  pop.querySelector("[data-popclose]").onclick = () => closeGuidePop(true);
  pop.querySelector("[data-popplay]").focus({ preventScroll: true });
}
// back：把焦点还给打开它的那个节目块（Esc、关闭按钮）；点了别处、滚动了就不还。
function closeGuidePop(back) {
  const pop = $("gpop");
  if (pop.hidden) return;
  pop.hidden = true;
  if (back && popFrom && document.contains(popFrom)) popFrom.focus({ preventScroll: true });
  popFrom = null;
}
document.addEventListener("pointerdown", (event) => {
  if (!$("gpop").hidden && !event.target.closest("#gpop, [data-gp]")) closeGuidePop(false);
});
// 用户滚动了（滚轮、触摸）：详情不跟着走，关掉。
document.addEventListener("wheel", (event) => { if (!$("gpop").hidden && !event.target.closest("#gpop")) closeGuidePop(false); }, { passive: true });

// ======================================================================= 选集（抽屉）

let eps = null, epPending = 0, epLoading = false;

async function openEpisodes(id) {
  eps = null; epPending = 0;
  const sr = ((data && data.series) || []).find((x) => x.id === id);
  $("epId").textContent = id.toUpperCase();
  $("epName").textContent = sr ? sr.name : "";
  $("epSum").textContent = "正在取集数…";
  $("epList").replaceChildren();
  $("epActs").replaceChildren();
  openSheet($("drawer"));
  await loadEpisodes(id, true);
}
$("epClose").onclick = () => closeSheets();

async function loadEpisodes(id, scroll) {
  epLoading = true;
  let answer;
  const id2 = deviceId();
  try { answer = await call("GET", "api/series/" + id + "/episodes" + (id2 ? "?device=" + encodeURIComponent(id2) : "")); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  finally { epLoading = false; }
  if ($("drawer").hidden) return;
  if (!answer.ok) { toast(answer.error, true); closeSheets(); return; }
  eps = answer;
  renderEpisodes(scroll);
}

function renderEpisodes(scroll) {
  const list = $("epList");
  const mark = eps.playing || eps.next;
  const frag = document.createDocumentFragment();
  for (const ep of eps.episodes) {
    const row = el("button", "ep" + (ep.state === "watched" && ep.n !== mark ? " seen" : "") + (ep.n === mark ? " cur" : "") + (ep.n === epPending ? " pending" : ""));
    row.dataset.n = String(ep.n);
    const label = ep.n === epPending ? "切换中…" : ep.n === eps.playing ? "播放中"
      : ep.state === "current" ? (ep.position ? "看到 " + clock(ep.position) : "接着播")
      : ep.state === "watched" ? "看过" : ep.duration ? clock(ep.duration) : "";
    row.append(el("span", "n", pad(ep.n)), el("span", "t", ep.title || "第 " + ep.n + " 集"), el("span", "s", label));
    frag.appendChild(row);
  }
  list.replaceChildren(frag);
  $("epName").textContent = eps.name;
  const to = currentTarget();
  $("epSum").textContent = "共 " + eps.total + " 集 · " + (eps.playing ? "正在播第 " + eps.playing + " 集" : "下次从第 " + eps.next + " 集播")
    + (to === PC ? " · 点一集，在电脑上看这一集（不动小电视的进度）" : to ? " · 点一集，「" + targetName(to) + "」就从这一集播" : "");
  const acts = $("epActs");
  if (!acts.childElementCount) {
    const n = eps.playing || eps.next || 1;
    acts.innerHTML = splitHTML("sr:" + eps.id, "播放第 " + n + " 集");
    ITEMS["sr:" + eps.id] = { title: eps.name + " 第 " + n + " 集", subtitle: eps.name, play: { kind: "episode", channel: eps.id, episode: n } };
  }
  if (scroll) { const cur = list.querySelector(".ep.cur"); if (cur) cur.scrollIntoView({ block: "center" }); }
}
$("epList").onclick = async (event) => {
  const row = event.target.closest(".ep");
  if (!row || !eps || epPending) return;
  const n = Number(row.dataset.n);
  const id = eps.id, name = eps.name;
  const item = { kind: "episode", channel: id, episode: n, title: name + " 第 " + n + " 集" };
  const to = currentTarget();
  if (to && to !== PC && n === eps.playing && to === deviceId()) { toast("正在播这一集"); return; }
  if (to === PC || !to || !remember || !targetOnline(to)) { closeSheets(true); play(item, null, name + " 第 " + n + " 集"); return; }
  epPending = n;
  renderEpisodes(false);
  try { await play(item, to, name + " 第 " + n + " 集"); }
  finally { epPending = 0; if (eps) await loadEpisodes(id, false); }
};
function followEpisodes() {
  if (!eps || epLoading || epPending || $("drawer").hidden) return;
  const sr = (data.series || []).find((x) => x.id === eps.id);
  const playing = data.now.online && data.now.channel === eps.id ? data.now.episode : 0;
  if ((sr && sr.episode !== eps.next) || playing !== eps.playing) loadEpisodes(eps.id, false);
}
$("openEps").onclick = () => { if (data && data.now.total) openEpisodes(data.now.channel); };

// ======================================================================= 订阅：剧集和 UP 主

function removeButton(kind, item) {
  return button("删除", "linkbtn red", async (event) => {
    event.stopPropagation();
    if (!confirm("删除「" + item.name + "」这个台？" + (kind === "series" ? "看到哪一集也会忘掉。" : ""))) return;
    try {
      const answer = await call("DELETE", "api/" + kind + "/" + item.id);
      if (!answer.ok) toast(answer.error, true);
    } catch (error) { if (error instanceof SignedOut) signedOut(); }
    refresh();
    loadHome(true);
  });
}
// UP 主：中转只轮播最新的 20 个投稿，「未看」只数这些；count 是 UP 主一共有多少个（不知道时为 0）。
function upMeta(c) {
  const count = Math.max(c.count || 0, c.total);
  return siteTag(c.site) + " · 共 " + count + " 个视频 · " + (count > c.total ? "最近 " + c.total + " 个里" : "") + "未看 " + c.left;
}
function onAir(id) { return data.now && data.now.online && data.now.channel === id; }
function playButton(item) {
  const b = button("播放", "linkbtn play", (event) => { event.stopPropagation(); play({ kind: "channel", channel: item.id, title: item.name }, null, item.name); });
  b.title = "播放到「" + targetName(currentTarget()) + "」";
  return b;
}
function pickButton(item) {
  const b = button("播放到…", "linkbtn", (event) => { event.stopPropagation(); openTarget({ kind: "channel", channel: item.id, title: item.name }, "", item.name); });
  return b;
}
function listRow(cls, title, sub, acts) {
  const row = el("div", "li" + (cls ? " " + cls : ""));
  const tx = el("span", "tx");
  tx.appendChild(el("b", "", title));
  if (sub) tx.appendChild(el("small", "", sub));
  const box = el("span", "acts");
  box.append(...acts);
  row.append(tx, box);
  return row;
}
function emptyRow(text) {
  const row = el("div", "li");
  row.appendChild(el("span", "empty-t", text));
  return row;
}
let subsSig = "";
function renderSubs() {
  if (!me || isGuest()) return;
  const sig = JSON.stringify([data.creators, data.series, data.room, data.now.channel, data.now.online, currentTarget(), deviceName()]);
  if (sig === subsSig) return;
  subsSig = sig;
  $("subsHint").textContent = "「播放」播到「播放到」选的地方（" + targetName(currentTarget()) + "）。";
  const ups = $("creatorRows");
  ups.replaceChildren();
  $("upCount").textContent = data.creators.length + " / 12 个 UP 主";
  $("upCount2").textContent = data.creators.length + " / 12";
  if (!data.creators.length) ups.appendChild(emptyRow("还没有添加 UP 主：在下面粘贴主页链接，或者去搜索里找"));
  for (const c of data.creators) {
    ups.appendChild(listRow(onAir(c.id) ? "air" : "", c.name, upMeta(c) + (onAir(c.id) ? " · " + deviceName() + "正在播" : ""),
                            [playButton(c), pickButton(c), removeButton("creators", c)]));
  }
  const rows = $("seriesRows");
  rows.replaceChildren();
  const room = data.room || {};
  $("seriesCount").textContent = (data.series || []).length + " 部" + (room.fits === false ? " · 频道列表已满" : "");
  if (!(data.series || []).length) rows.appendChild(emptyRow("还没有添加剧集"));
  for (const s of data.series || []) {
    const row = listRow("tap" + (onAir(s.id) ? " air" : ""), s.name,
                        siteTag(s.site) + " · 第 " + s.episode + " / " + s.total + " 集" + (onAir(s.id) ? " · " + deviceName() + "正在播" : ""),
                        [playButton(s), button("选集 ›", "linkbtn", (event) => { event.stopPropagation(); openEpisodes(s.id); }), removeButton("series", s)]);
    row.onclick = () => openEpisodes(s.id);
    rows.appendChild(row);
  }
  markSubscribed();
}
// 剧集海报格子：封面来自 /api/home 的 series_updates（B 站给过的海报），没有就用占位图。
let seriesSig = "";
function renderSeriesGrid() {
  if (!data) return;
  const grid = $("seriesGrid");
  const arts = {};
  for (const s of (homeData && homeData.series_updates) || []) arts[s.series.id] = s;
  const list = data.series || [];
  const sig = JSON.stringify([list, Object.keys(arts).map((k) => [k, arts[k].art.url, arts[k].series.done]), data.now.channel, data.now.online]);
  if (sig === seriesSig && grid.childElementCount) return;
  seriesSig = sig;
  const fk = focusKey();
  if (!list.length) {
    grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="eic">${ic("series", 40)}</div><b>还没有剧</b><p>番剧、多集视频、合集、播放列表都行：每部剧是一个台，按顺序播，接着上次的地方。</p><a class="btn pri sm" href="#/search">去搜索里找</a></div>`;
    return;
  }
  grid.innerHTML = list.map((s) => {
    const h = arts[s.id];
    const done = h && h.series.done;
    const badge = onAir(s.id) ? '<span class="badge live">正在播</span>' : done ? '<span class="badge">看完了</span>' : s.episode > 1 ? `<span class="badge new">第 ${s.episode} 集</span>` : "";
    const pct = s.total ? (s.episode - 1) / s.total : 0;
    return `<button class="card p" data-series="${esc(s.id)}" data-k="sr-${esc(s.id)}" aria-label="${esc(s.name)}，选集">
      <span class="art">${art(s.name, "p", { title: s.name, kick: siteTag(s.site) })}${h ? imgTag(h.art.url) : ""}${badge}${pct > 0 ? `<span class="pbar"><i style="width:${(pct * 100).toFixed(1)}%"></i></span>` : ""}</span>
      <span class="t">${esc(s.name)}</span><span class="s">${s.episode > 1 ? "下次第 " + s.episode + " 集 · " : ""}共 ${s.total} 集</span></button>`;
  }).join("");
  restoreFocus(fk);
}
function renderUpLatest() {
  const box = $("upLatest");
  const list = (homeData && homeData.creator_updates) || [];
  box.innerHTML = list.length ? shelf("最新视频", "还没看的，新的在前", list.map((x) => cardVideo(x)).join("")) : "";
}

$("addCreator").onclick = () => busy($("addCreator"), async () => {
  toast("正在添加…");
  const answer = await call("POST", "api/creators", { url: $("creatorLink").value });
  if (answer.ok) { $("creatorLink").value = ""; toast("已添加「" + answer.creator.name + "」"); refresh(); loadHome(true); }
  else toast(answer.error, true);
});
$("addSeries").onclick = () => busy($("addSeries"), async () => {
  toast("正在取集数…");
  const answer = await call("POST", "api/series", { url: $("seriesLink").value, name: $("seriesName").value });
  if (answer.ok) {
    $("seriesLink").value = ""; $("seriesName").value = "";
    toast("已添加「" + answer.series.name + "」，共 " + answer.series.total + " 集"); refresh(); loadHome(true);
  } else toast(answer.error, true);
});

// ======================================================================= 搜索：B 站搜索（/api/bili/search，封面经 /api/bili/thumb）、
// YouTube 搜索（/api/yt/search，server/relay/ytsearch.py）+ 粘贴链接即播

// pgc：「视频」第一页顺带的正版影视、番剧（bilibili.py 的 pgc_top），放在视频前面。
const bs = { kind: "video", q: "", page: 0, pages: 0, items: [], pgc: [], run: 0, timer: 0, retried: false };
const BS_HELLO = "输入关键词，或粘贴视频链接直接播放";
const BS_WHAT = { video: "的视频", user: "的 UP 主（有投稿的）", bangumi: "的番剧", ft: "的影视剧" };
// 在哪儿搜（bili / yt），每个浏览器记住；YouTube 的结果另放，切回来还在。
let searchSrc = lsGet("console.search.src", "bili") === "yt" ? "yt" : "bili";
const YT_KINDS = ["video", "channel", "playlist"];
const yts = { kind: YT_KINDS.includes(lsGet("console.yt.kind", "")) ? lsGet("console.yt.kind", "") : "video",
              q: "", items: [], tries: 0, ticker: 0, error: null };
const YT_WAIT_MS = 30000;   // 服务器最多等节点 25 秒，这里多留几秒
const LINKY = /(https?:\/\/|bilibili\.com|b23\.tv|youtube\.com|youtu\.be|^BV[0-9A-Za-z]{10}$)/i;
function bsSay(text, kind) {
  $("bsState").textContent = text;
  $("bsState").className = "bsstate" + (kind ? " " + kind : "");
}
function bigNumber(n) {
  if (n >= 1e8) return (n / 1e8).toFixed(1).replace(/\.0$/, "") + " 亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, "") + " 万";
  return String(n);
}
function day(at) {
  if (!at) return "";
  const d = new Date(at * 1000);
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}
// 粘贴了链接：第一条结果是「播放这个链接」（投屏，或者投到电脑），也可以直接订阅、加为剧集。
function renderLinkHit() {
  const text = $("bsQuery").value.trim();
  const box = $("linkHit");
  if (!LINKY.test(text)) { box.hidden = true; box.replaceChildren(); return false; }
  const url = /^BV[0-9A-Za-z]{10}$/.test(text) ? "https://www.bilibili.com/video/" + text : text;
  box.hidden = false;
  const item = { kind: "cast", url, title: "粘贴的链接", subtitle: url };
  ITEMS["link"] = { title: "这个链接", subtitle: url, play: item };
  box.innerHTML = `<div class="linkhit"><span class="ico">${ic("link", 22)}</span><span class="tx"><b>播放这个链接</b><small>${esc(url)}</small></span>${splitHTML("link", "播放")}</div>`;
  if (!isGuest()) {
    const more = el("div", "inline");
    more.style.marginTop = "-8px";
    more.append(addButton("订阅这个 UP 主", "link", url, async () => {
      toast("正在添加…");
      const answer = await call("POST", "api/creators", { url });
      if (!answer.ok) { toast(answer.error, true); return; }
      toast("已订阅「" + answer.creator.name + "」"); refresh(); loadHome(true);
    }), addButton("加为剧集", "link", url, async () => {
      toast("正在取集数…");
      const answer = await call("POST", "api/series", { url, name: "" });
      if (!answer.ok) { toast(answer.error, true); return; }
      toast("已添加「" + answer.series.name + "」，共 " + answer.series.total + " 集"); refresh(); loadHome(true);
    }));
    box.appendChild(more);
  }
  bsSay("粘贴的是链接：点「播放」放这个视频；也可以订阅它的 UP 主，或者把多集视频加为剧集。");
  return true;
}
async function bsSearch(fresh) {
  if (renderLinkHit()) return;
  if (isGuest()) { bsSay("访客只能粘贴视频链接播放。"); return; }
  if (searchSrc === "yt") return ytSearch(false);
  const q = $("bsQuery").value.trim().replace(/\s+/g, " ");
  if (!q) { bsSay(BS_HELLO); return; }
  if (q.length > 40) { bsSay("关键词太长了（最多 40 个字）", "bad"); return; }
  clearTimeout(bs.timer);
  const run = ++bs.run, kind = bs.kind, page = fresh ? 1 : bs.page + 1;
  if (fresh) bs.retried = false;
  bsSay(fresh ? "正在搜「" + q + "」…" : "正在取第 " + page + " 页…", "busy");
  $("bsMore").disabled = true;
  let answer;
  try {
    answer = await call("GET", "api/bili/search?" + new URLSearchParams({ q, kind, page }));
  } catch (error) {
    if (error instanceof SignedOut) { signedOut(); return; }
    if (run === bs.run) { bsSay("连不上小电视服务器", "bad"); $("bsMore").disabled = false; }
    return;
  }
  if (run !== bs.run) return;           // 已经有更新的搜索了
  $("bsMore").disabled = false;
  if (!answer.ok) {
    if (answer.retry && !bs.retried) {  // 搜得太快：等一下再试一次
      bs.retried = true;
      bs.timer = setTimeout(() => bsSearch(fresh), answer.retry * 1000 + 200);
      return;
    }
    bsSay(answer.error, "bad");
    return;
  }
  bs.q = q; bs.page = answer.page; bs.pages = answer.pages;
  bs.items = fresh ? answer.items : bs.items.concat(answer.items.filter((x) => !bs.items.some((y) => y.id === x.id)));
  if (fresh) bs.pgc = answer.pgc || [];
  if (!bs.items.length && !bs.pgc.length) bsSay("没有搜到「" + q + "」" + BS_WHAT[kind]);
  else bsSay(answer.total ? bigNumber(answer.total) + " 个结果" : "");
  renderSearch();
}
$("bsQuery").addEventListener("input", () => {
  clearTimeout(bs.timer);
  if (renderLinkHit()) return;
  const q = $("bsQuery").value.trim();
  // 停手 0.9 秒再搜，不逐字去问 B 站；YouTube 每次都要跑一次 yt-dlp，停手 1.5 秒。
  if (searchSrc === "yt") { if (q.length >= 2 && q !== yts.q && !isGuest() && ytReady()) bs.timer = setTimeout(() => bsSearch(true), 1500); }
  else if (q.length >= 2 && q !== bs.q && !isGuest()) bs.timer = setTimeout(() => bsSearch(true), 900);
});
$("bsQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") bsSearch(true); });
$("bsMore").onclick = () => bsSearch(false);
function bsKind(kind) {
  if (bs.kind === kind) return;
  bs.kind = kind;
  for (const x of qsa("#bsKinds button")) x.setAttribute("aria-pressed", String(x.dataset.kind === kind));
  bs.items = []; bs.pgc = []; bs.page = 0; bs.q = "";
  renderSearch();
  if ($("bsQuery").value.trim()) bsSearch(true);
  else bsSay(BS_HELLO);
}
for (const b of qsa("#bsKinds button")) b.onclick = () => bsKind(b.dataset.kind);

// ---------- YouTube 搜索 ----------

const YT_HELLO = "输入关键词按回车（每次要几秒），或粘贴 YouTube 链接直接播放";
// YouTube 能不能搜：off（没开）、cloudoff（服务器没配）、login（cookies 过期）、ok。
function ytAvail() {
  if (!data) return "ok";
  if (data.youtube_off) return "off";
  if (!data.youtube) return "cloudoff";
  return data.youtube.state === "login" ? "login" : "ok";
}
function ytReady() { const a = ytAvail(); return a === "ok" || a === "login"; }
const YT_GATES = {
  off: ["YouTube 还没开启", "YouTube 还没开启：去「设置 → YouTube」开启本机模式并填代理。开好以后就能在这里搜 YouTube 视频、频道和播放列表。", true],
  cloudoff: ["这台服务器没有 YouTube", "这台服务器还没有配置 YouTube，现在只能搜 B 站。", false],
  login: ["需要导入 cookies", "YouTube 要求登录才肯回答：去「设置 → YouTube」导入 cookies.txt（用小号导出）。", true],
  tools: ["YouTube 组件还没装好", "去「设置 → YouTube」安装 yt-dlp 等组件，装好就能搜。", true],
};
// 顶上的说明：没开、没配、要 cookies、组件没装好时代替结果列表；有按钮去设置（管理员才看得到那一页）。
function renderYtGate() {
  const box = $("ytGate");
  const why = searchSrc !== "yt" || isGuest() ? "" : (yts.error && YT_GATES[yts.error] ? yts.error : ytAvail() === "ok" ? "" : ytAvail());
  const sig = why + (me ? me.role : "");
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.hidden = !why;
  if (!why) { box.replaceChildren(); return; }
  if ((why === "off" || why === "cloudoff") && $("bsState").textContent === YT_HELLO) bsSay("");   // 搜不了：不说「按回车搜」
  const [title, text, settings] = YT_GATES[why];
  const admin = me && me.role === "admin";
  box.innerHTML = `<div class="empty ytgate"><div class="eic">${ic("search", 40)}</div><b>${esc(title)}</b><p>${esc(text)}${settings && !admin ? "（这一页要管理员打开）" : ""}</p>`
    + (settings && admin ? `<a class="btn pri sm" href="#/settings/youtube">去「设置 → YouTube」</a>` : "") + `</div>`;
}
// 来源切换、种类按钮、输入框的提示跟着「在哪儿搜」走（render() 每次刷新都会调，不变就不动）。
function renderSearchSource() {
  const yt = searchSrc === "yt";
  for (const x of qsa("#srcSwitch button")) x.setAttribute("aria-pressed", String(x.dataset.src === searchSrc));
  for (const x of qsa("#ytKinds button")) x.setAttribute("aria-pressed", String(x.dataset.kind === yts.kind));
  $("bsKinds").hidden = yt;
  $("ytKinds").hidden = !yt;
  $("bsQuery").placeholder = yt ? "搜 YouTube 视频、频道、播放列表，或粘贴视频链接" : "搜 B 站视频、影视剧、番剧、UP 主，或粘贴视频链接";
  if (yt) $("castYt").hidden = true;    // 没开 YouTube 的说明由 ytGate 写
  renderYtGate();
}
function ytHello() {
  if ($("linkHit").hidden === false) return;
  const a = ytAvail();
  bsSay(a === "ok" || a === "login" ? YT_HELLO : "");
}
function setSource(src) {
  if (src === searchSrc) return;
  searchSrc = src;
  lsSet("console.search.src", src);
  clearTimeout(bs.timer);
  bs.run++;                              // 切走了：还在路上的那个搜索回来也不画
  yts.error = null;
  if (route === "search" && data) $("castYt").hidden = !data.youtube_off;
  renderSearchSource();
  renderSearch();
  const q = $("bsQuery").value.trim();
  if (renderLinkHit()) return;
  if (src === "yt") {
    if (q && q !== yts.q && ytReady()) bsSearch(true);
    else if (yts.items.length) bsSay("YouTube 上「" + yts.q + "」的 " + yts.items.length + " 个结果");
    else ytHello();
  } else if (q && q !== bs.q) bsSearch(true);
  else bsSay(bs.items.length ? "" : BS_HELLO);
}
for (const b of qsa("#srcSwitch button")) b.onclick = () => setSource(b.dataset.src);
for (const b of qsa("#ytKinds button")) {
  b.onclick = () => {
    if (yts.kind === b.dataset.kind) return;
    yts.kind = b.dataset.kind;
    lsSet("console.yt.kind", yts.kind);
    yts.items = []; yts.q = ""; yts.error = null;
    renderSearchSource();
    renderSearch();
    if ($("bsQuery").value.trim()) bsSearch(true);
    else ytHello();
  };
}
renderSearchSource();
if (searchSrc === "yt") bsSay(YT_HELLO);
function ytStopTicker() { clearInterval(yts.ticker); yts.ticker = 0; }
async function ytSearch(retry) {
  if (!retry) yts.tries = 0;
  const q = $("bsQuery").value.trim().replace(/\s+/g, " ");
  const kind = yts.kind;
  if (!q) { ytHello(); return; }
  if (q.length > 60) { bsSay("关键词太长了（最多 60 个字）", "bad"); return; }
  const avail = ytAvail();
  if (avail === "off" || avail === "cloudoff") { renderYtGate(); bsSay(""); return; }
  clearTimeout(bs.timer);
  ytStopTicker();
  const run = ++bs.run;
  const what = { video: "视频", channel: "频道", playlist: "播放列表" }[kind];
  const started = Date.now();
  const progress = () => {
    const s = Math.round((Date.now() - started) / 1000);
    bsSay("正在 YouTube 上搜「" + q + "」的" + what + "…" + (s >= 2 ? " " + s + " 秒" : "")
          + (s >= 8 ? "（yt-dlp 经代理要一会儿，最多等 25 秒）" : ""), "busy");
  };
  progress();
  yts.ticker = setInterval(() => { if (run === bs.run) progress(); else ytStopTicker(); }, 1000);
  $("bsMore").hidden = true;
  let answer;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), YT_WAIT_MS);
  try {
    const response = await fetch("api/yt/search?" + new URLSearchParams({ q, kind }),
                                 { credentials: "same-origin", signal: abort.signal });
    if (response.status === 404) throw new SignedOut("登录已失效");
    answer = await response.json();
  } catch (error) {
    if (run !== bs.run) return;
    ytStopTicker();
    if (error instanceof SignedOut) { signedOut(); return; }
    bsSay(error && error.name === "AbortError" ? "YouTube 搜索超时了：代理慢或连不上，过一会儿再试" : "连不上小电视服务器", "bad");
    return;
  } finally { clearTimeout(timer); }
  if (run !== bs.run) return;           // 已经有更新的搜索了，或切回了 B 站
  ytStopTicker();
  if (!answer.ok) {
    if (answer.retry && yts.tries < 3) {   // 节点正忙、别的搜索在跑、搜得太快：等一下再试，最多三次
      yts.tries++;
      bsSay(answer.error + "（" + Math.ceil(answer.retry) + " 秒后自动再试）", "busy");
      bs.timer = setTimeout(() => { if (run === bs.run && !renderLinkHit()) ytSearch(true); }, answer.retry * 1000 + 300);
      return;
    }
    yts.tries = 0;
    yts.error = YT_GATES[answer.reason] ? answer.reason : null;
    yts.items = []; yts.q = "";
    renderSearch();
    renderYtGate();
    bsSay(yts.error ? "" : answer.error, "bad");
    return;
  }
  yts.tries = 0;
  if (yts.error) { yts.error = null; renderYtGate(); }
  yts.q = q; yts.items = answer.items;
  if (!yts.items.length) bsSay("YouTube 上没有搜到「" + q + "」的" + what + (kind === "video" ? "（直播、预告和 Shorts 不列出）" : ""));
  else bsSay("YouTube 上「" + q + "」的 " + yts.items.length + " 个" + what + "：" + (kind === "video" ? "点「播放」放到小电视或这台电脑"
             : kind === "channel" ? "「订阅频道」后它就是一个台，新视频自动排上" : "「加为剧集」后按列表顺序播，记住看到哪一集"));
  renderSearch();
}
// 大约多久以前（YouTube 搜索结果只说「3 年前」，节点换算成的时刻只是大约）。
function ytAgo(at) {
  if (!at) return "";
  const days = (Date.now() / 1000 - at) / 86400;
  if (days < 1) return "今天";
  if (days < 30) return Math.round(days) + " 天前";
  if (days < 365) return Math.round(days / 30) + " 个月前";
  return Math.round(days / 365) + " 年前";
}
function subscribeChannel(item) {
  const link = item.kind === "channel" ? item.link : item.channel_link;
  const name = item.kind === "channel" ? item.name : item.channel;
  return addButton("订阅频道", "ytchannel", link, async () => {
    toast("正在订阅" + (name ? "「" + name + "」" : "") + "…（YouTube 要取投稿列表，十几秒）", false, 8000);
    const answer = await call("POST", "api/creators", { url: link });
    if (!answer.ok) { toast(answer.error, true); return; }
    toast("已订阅「" + answer.creator.name + "」，在频道里是一个新台");
    await refresh();
    loadHome(true);
  });
}
function addPlaylist(item) {
  return addButton("加为剧集", "ytseries", item.id, async () => {
    toast("正在取播放列表…（YouTube 要十几秒）", false, 8000);
    const answer = await call("POST", "api/series", { url: item.link, name: "" });
    if (!answer.ok) { toast(answer.error, true); return; }
    toast("已添加「" + answer.series.name + "」，共 " + answer.series.total + " 集");
    await refresh();
    loadHome(true);
  });
}
function renderYtHits(box) {
  for (const item of yts.items) {
    const row = el("div", "hit");
    const body = el("div", "body");
    const acts = el("div", "acts");
    if (item.kind === "video") {
      const cover = thumb(item, "v");
      cover.appendChild(el("span", "dur", clock(item.duration)));
      const meta = el("div", "m");
      meta.append(el("b", "", item.channel || "—"));
      const more = [item.views ? "播放 " + bigNumber(item.views) : "", ytAgo(item.date)].filter(Boolean);
      if (more.length) meta.append(" · " + more.join(" · "));
      body.append(el("div", "t", item.title), meta);
      const go = el("button", "go", "播放");
      go.onclick = () => pressed(go, play({ kind: "cast", url: item.link, title: item.title }, null, item.title));
      const yid = ytIdOfItem({ url: item.link });
      if (yid) row.dataset.ytid = yid;      // 鼠标停住、焦点到「播放」时先取好地址（ytPrefetch）
      acts.appendChild(go);
      if (item.channel_link) acts.appendChild(subscribeChannel(item));
      row.append(cover, body, acts);
    } else if (item.kind === "channel") {
      const meta = [item.fans ? "订阅 " + bigNumber(item.fans) : "", item.videos ? "视频 " + bigNumber(item.videos) + " 个" : ""].filter(Boolean);
      body.append(el("div", "t", item.name), el("div", "m", meta.join(" · ") || "YouTube 频道"));
      acts.appendChild(subscribeChannel(item));
      row.append(thumb(item, "f", "face"), body, acts);
    } else {
      const cover = thumb(item, "v");
      if (item.count) cover.appendChild(el("span", "dur", item.count + " 个视频"));
      body.append(el("div", "t", item.title), el("div", "m", item.channel ? "" : "播放列表"));
      if (item.channel) body.lastChild.append(el("b", "", item.channel), " · 播放列表");
      acts.appendChild(addPlaylist(item));
      row.append(cover, body, acts);
    }
    box.appendChild(row);
  }
}

function thumb(item, size, cls) {
  const box = el("div", "cover" + (cls ? " " + cls : ""));
  box.innerHTML = cls === "face" ? avatarSVG(item.name || item.title || "?", 72) : art(item.title || item.name || "?", cls === "poster" ? "p" : "w");
  const svg = box.firstChild;
  if (svg) { svg.style.position = "absolute"; svg.style.inset = "0"; svg.style.width = "100%"; svg.style.height = "100%"; }
  if (item.cover) {
    const img = el("img");
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    img.src = "api/bili/thumb?" + new URLSearchParams({ s: size, u: item.cover });
    box.appendChild(img);
  }
  return box;
}
function subscribed(kind, source) {
  if (!data) return false;
  const site = kind === "ytchannel" || kind === "ytseries" ? "youtube" : "bilibili";
  return kind === "user" || kind === "ytchannel" ? (data.creators || []).some((c) => c.site === site && c.source === source)
    : kind === "series" || kind === "ytseries" ? (data.series || []).some((s) => s.site === site && s.source === source) : false;
}
function addButton(text, kind, source, work) {
  const b = el("button", "ghost", text);
  b.dataset.kind = kind; b.dataset.source = source; b.dataset.text = text;
  b.onclick = async () => {
    b.disabled = true;
    try { await work(); }
    catch (error) { if (error instanceof SignedOut) signedOut(); else toast(error.message || "网络不通", true); }
    finally { b.disabled = false; markSubscribed(); }
  };
  return b;
}
function subscribeUp(mid, name) {
  return addButton("订阅 UP 主", "user", mid, async () => {
    toast("正在订阅" + (name ? "「" + name + "」" : "") + "…");
    const answer = await call("POST", "api/creators", { url: "https://space.bilibili.com/" + mid });
    if (!answer.ok) { toast(answer.error, true); return; }
    toast("已订阅「" + answer.creator.name + "」，在频道里是一个新台");
    await refresh();
    loadHome(true);
  });
}
function addAsSeries(item) {
  // 剧集的来源：番剧是 ss<季 id>，多集视频是 BV 号（和订阅列表里的 source 一样，好标「已添加」）。
  return addButton("加为剧集", "series", item.id, async () => {
    toast("正在取集数…");
    const answer = await call("POST", "api/series", { url: item.link, name: "" });
    if (!answer.ok) { toast(answer.error, true); return; }
    toast("已添加「" + answer.series.name + "」，共 " + answer.series.total + " 集");
    await refresh();
    loadHome(true);
  });
}
// 已经订阅过的：按钮变灰，写「已订阅」「已添加」（每次刷新订阅列表后都对一遍）。
function markSubscribed() {
  for (const b of qsa("#bsHits button[data-kind]")) {
    const done = subscribed(b.dataset.kind, b.dataset.source);
    b.classList.toggle("done", done);
    b.textContent = done ? (b.dataset.kind === "user" || b.dataset.kind === "ytchannel" ? "已订阅" : "已添加") : b.dataset.text;
    if (done) b.disabled = true;
    else if (b.classList.contains("was")) b.disabled = false;
    b.classList.toggle("was", done);
  }
}
function renderSearch() {
  const box = $("bsHits");
  box.replaceChildren();
  if (searchSrc === "yt") {
    renderYtHits(box);
    $("bsMore").hidden = true;          // YouTube 一次给 20 个，不翻页
    markSubscribed();
    ytPrefetchView();
    return;
  }
  if (bs.kind === "video" && bs.pgc.length) {
    const head = el("div", "bshead");
    head.append(el("b", "", "正版影视 · 番剧"));
    head.append(button("更多影视剧 ›", "linkbtn", () => bsKind("ft")), button("更多番剧 ›", "linkbtn", () => bsKind("bangumi")));
    box.appendChild(head);
    for (const item of bs.pgc) box.appendChild(pgcHit(item));
    if (bs.items.length) { const v = el("div", "bshead"); v.append(el("b", "", "视频")); box.appendChild(v); }
  }
  for (const item of bs.items) {
    const row = el("div", "hit");
    const body = el("div", "body");
    const acts = el("div", "acts");
    if (item.kind === "video") {
      const cover = thumb(item, "v");
      if (item.duration) cover.appendChild(el("span", "dur", clock(item.duration)));
      const meta = el("div", "m");
      meta.append(el("b", "", item.author || "—"));
      meta.append(" · 播放 " + bigNumber(item.play) + (item.pubdate ? " · " + day(item.pubdate) : ""));
      body.append(el("div", "t", item.title), meta, el("div", "d", item.desc || ""));
      if (/\/video\//.test(item.link || "")) {
        const go = el("button", "go", "播放");
        go.onclick = () => pressed(go, play({ kind: "cast", url: item.link, title: item.title }, null, item.title));
        acts.appendChild(go);
      }
      if (item.mid) acts.appendChild(subscribeUp(item.mid, item.author));
      if (item.series) acts.appendChild(addAsSeries(item));
      row.append(cover, body, acts);
    } else if (item.kind === "user") {
      body.append(el("div", "t", item.name), el("div", "m", "粉丝 " + bigNumber(item.fans) + " · 视频 " + bigNumber(item.videos) + " 个"),
                  el("div", "d", item.desc || ""));
      acts.appendChild(subscribeUp(item.id, item.name));
      row.append(thumb(item, "f", "face"), body, acts);
    } else {
      box.appendChild(pgcHit(item));
      continue;
    }
    box.appendChild(row);
  }
  $("bsMore").hidden = !bs.items.length || bs.page >= bs.pages;
  markSubscribed();
}
// 正版的影视、番剧（一整季，ss<季 id>），排得和 B 站的「影视」搜索页一样：海报、类型、出演、简介、评分、选集。
// 「立即观看」按番剧链接放（web.py 的 _pc_resolve：一季的各集，有选集）；点一集按 /bangumi/play/ep<集 id> 放；
// 看过的（中转按你的 B 站观看记录标的 item.watched）是「继续看第 N 集」，那一集标出来。小电视上要「加为剧集」。
// 要不要大会员、地区限制在放的时候由中转说。
const PGC_EPS_SHOWN = 12;
function pgcHit(item) {
  const row = el("div", "hit pgc");
  const body = el("div", "body");
  const poster = item.cover ? "api/bili/thumb?" + new URLSearchParams({ s: "p", u: item.cover }) : "";
  const playEp = (ep, n, node) => pressed(node, playOnPc({ kind: "cast", url: "https://www.bilibili.com/bangumi/play/ep" + ep,
                                                           title: item.title + " 第" + n + "集" }, item.title, poster));
  const t = el("div", "t");
  if (item.badge) t.appendChild(el("span", "kind", item.badge));
  t.append(item.title);
  if (item.org_title) t.title = item.org_title;
  const meta = el("div", "m", [item.styles, item.date || (item.year ? item.year + " 年" : ""), item.areas,
                               item.show || (item.eps ? "共 " + item.eps + " 集" : "")].filter(Boolean).join(" · "));
  for (const tag of item.tags || []) meta.append(" ", el("span", "tag" + (/会员|付费/.test(tag) ? " vip" : ""), tag));
  body.append(t, meta);
  if (item.cast) body.appendChild(el("div", "c", "出演：" + item.cast));
  if (item.desc) body.appendChild(el("div", "d", item.desc));
  if (item.score) {
    const sc = el("div", "score");
    if (item.votes) sc.append(bigNumber(item.votes) + " 人评分 ");
    sc.appendChild(el("b", "", item.score.toFixed(1) + " 分"));
    body.appendChild(sc);
  }
  const acts = el("div", "pacts");
  const w = item.watched;
  if (canPc()) {
    const go = el("button", "go", w && w.ep ? "继续看第 " + w.n + " 集" : "立即观看");
    go.onclick = () => w && w.ep ? playEp(w.ep, w.n, go)
      : pressed(go, playOnPc({ kind: "cast", url: item.link, title: item.title }, item.title, poster));
    acts.appendChild(go);
  }
  acts.appendChild(addAsSeries(item));
  body.appendChild(acts);
  const eps = canPc() ? item.episodes || [] : [];
  if (eps.length > 1) {
    const box = el("div", "peps");
    const draw = (all) => {
      box.replaceChildren();
      for (const e of all ? eps : eps.slice(0, PGC_EPS_SHOWN)) {
        const b = el("button", "pep" + (w && w.ep === e.ep ? " seen" : ""), e.n);
        b.title = item.title + " 第" + e.n + "集" + (e.vip ? "（会员）" : "");
        if (e.vip) b.appendChild(el("i", "", "会员"));
        b.onclick = () => playEp(e.ep, e.n, b);
        box.appendChild(b);
      }
      if (!all && eps.length > PGC_EPS_SHOWN) box.appendChild(button("全部 " + eps.length + " 集", "pep more", () => draw(true)));
    };
    draw(false);
    body.appendChild(box);
  }
  row.append(thumb(item, "p", "poster"), body);
  return row;
}

// ======================================================================= 放过的（投屏历史）

let historySig = "";
function renderHistory() {
  if (!me || isGuest()) return;
  const sig = JSON.stringify(data.history);
  if (sig === historySig) return;
  historySig = sig;
  const rows = $("historyRows");
  rows.replaceChildren();
  if (!data.history.length) rows.appendChild(emptyRow("还没有投过视频：在「搜索」里粘贴视频链接"));
  for (const h of data.history) {
    const item = { kind: "history", site: h.site, id: h.id, title: h.title || h.id };
    rows.appendChild(listRow("", h.title || h.id, siteTag(h.site) + " · " + when(h.at), [
      button("播放", "linkbtn play", (event) => pressed(event.currentTarget, play(item, null, h.title || h.id))),
      button("播放到…", "linkbtn", () => openTarget(item, "", h.title || h.id)),
      button("删除", "linkbtn red", async () => {
        try {
          const answer = await call("DELETE", "api/history/" + h.site + "/" + encodeURIComponent(h.id));
          if (!answer.ok) toast(answer.error, true);
        } catch (error) { if (error instanceof SignedOut) signedOut(); }
        refresh();
      })]));
  }
}

// ======================================================================= 设备

let devicesCache = [];
async function loadDevices() {
  if (isGuest()) return;
  let answer;
  try { answer = await call("GET", "api/console/devices"); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  devicesCache = answer.devices;
  renderDevices();
}
function renderDevices() {
  const box = $("devCards");
  const list = devicesCache;
  $("devCount").textContent = list.length ? list.length + " 台小电视" : "";
  box.replaceChildren();
  const where = (d) => {
    const g = data && data.groups.flatMap((x) => x.channels.map((c) => [x.name, c])).find(([, c]) => c.id === d.channel);
    return g ? [pad(g[1].n), g[0]] : ["▶", ""];
  };
  for (const d of list) {
    const card = el("div", "dev" + (d.online ? "" : " off"));
    const [n, g] = d.online ? where(d) : ["--", ""];
    card.innerHTML = `<header>${ic("tv")}<b>${esc(d.name)}</b><small><span class="dot ${d.online ? "" : "off"}"></span>${d.online ? "在线" : "离线" + (d.last_seen ? " · " + ago(d.last_seen) + "在线" : "")}</small></header>
      <div class="model">${esc(d.model_name + (d.fw ? " · " + d.fw : "") + (d.legacy ? " · 旧固件" : ""))} · ${d.mine ? "我的" : "家人共用"}${d.mine && d.guests ? " · 访客 " + d.guests + " 个" : ""}</div>
      <div class="tvbox"><div class="tvscr ${d.online ? "" : "dark"}"><span class="g">${esc(g)}</span><div class="n">${esc(n)}</div><div class="x">${esc(d.online ? d.channel_name || d.channel || "—" : "离线")}</div><div class="y">${esc(d.online ? d.title || "" : "")}</div></div></div>`;
    const row = el("div", "row");
    const remote = button("遥控", "btn pri sm", () => openRemote(d.id));
    remote.disabled = !d.online;
    const def = button(currentTarget() === d.id ? "已是默认播放位置" : "设为默认播放位置", "btn sm", () => { setTarget(d.id); renderDevices(); });
    def.disabled = !d.online || currentTarget() === d.id;
    row.append(remote, def);
    if (d.online && data && data.watch) row.append(button("在电脑上看", "btn sm ghost-t", () => { pick(d.id); setTimeout(() => startWatch(false), 600); }));
    card.appendChild(row);
    if (d.mine) {
      const more = el("div", "more");
      more.append(button("改名", "linkbtn", () => renameDevice(d)), button("Wi-Fi", "linkbtn", () => openWifi(d)),
                  button("访客码", "linkbtn", () => guestCode(d)),
                  ...(d.legacy ? [] : [button("重新配对", "linkbtn", () => repairDevice(d)), button("解绑", "linkbtn red", () => unbindDevice(d))]));
      card.appendChild(more);
    }
    box.appendChild(card);
  }
  if (data && data.watch) {
    const pc = el("div", "dev");
    pc.innerHTML = `<header>${ic("pc")}<b>这台电脑</b><small><span class="dot"></span>就是这里</small></header><div class="model">在电脑上看 · 不需要小电视</div>
      <div class="tvbox">${watching ? "窗口里正在放 " + esc(watching.title || watching.name || "") : "点任何节目的「播放到 → 这台电脑」"}</div>`;
    const row = el("div", "row");
    const def = button(currentTarget() === PC ? "已是默认播放位置" : "设为默认播放位置", "btn sm", () => { setTarget(PC); renderDevices(); });
    def.disabled = currentTarget() === PC;
    row.appendChild(def);
    pc.appendChild(row);
    box.appendChild(pc);
  }
  const bind = el("div", "dev add");
  bind.innerHTML = `${ic("plus", 36)}<b>绑定新的小电视</b><p>新的小电视连上网以后，屏幕上会显示绑定码；也可以用手机扫它状态页上的二维码绑定。</p>`;
  const form = el("div", "inline");
  const input = el("input");
  input.type = "text"; input.id = "claim"; input.placeholder = "小电视屏幕上的 20 位绑定码"; input.autocomplete = "off"; input.spellcheck = false;
  const go = el("button", "go", "绑定");
  go.id = "bind";
  go.onclick = bindDevice;
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") bindDevice(); });
  form.append(input, go);
  bind.appendChild(form);
  box.appendChild(bind);
  claimField();
}

async function renameDevice(d) {
  const value = prompt("新名字（最多 20 个字）", d.name);
  if (!value) return;
  try {
    const answer = await call("POST", "api/devices/" + d.id, { name: value });
    if (!answer.ok) toast(answer.error, true); else toast("已改名为「" + answer.name + "」");
  } catch (error) { if (error instanceof SignedOut) signedOut(); }
  loadDevices(); refresh(); loadTargets();
}
async function unbindDevice(d) {
  if (!confirm("解绑「" + d.name + "」？解绑后它会显示新的绑定码，谁绑定就归谁；你的订阅不受影响。")) return;
  try {
    const answer = await call("POST", "api/devices/" + d.id + "/unbind", {});
    if (!answer.ok) toast(answer.error, true); else toast("已解绑");
  } catch (error) { if (error instanceof SignedOut) signedOut(); }
  loadDevices(); refresh(); loadTargets();
}
async function repairDevice(d) {
  if (!confirm("小电视刷过机（清空了存储）、连不上了才需要：10 分钟内让它重新连上网络，它的新密钥会被记住。继续？")) return;
  try {
    const answer = await call("POST", "api/devices/" + d.id + "/repair", {});
    toast(answer.ok ? answer.note : answer.error, !answer.ok);
  } catch (error) { if (error instanceof SignedOut) signedOut(); }
}
async function qrSvg(text) {
  const answer = await call("POST", "api/qr", { text });
  return answer.ok ? answer.svg : "";
}
async function guestCode(d) {
  if (!d.guest_link) { toast("这台还没有访客码", true); return; }
  const box = el("div", "qrbox");
  try { box.innerHTML = await qrSvg(d.guest_link); } catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  const rotate = button("换一个码", "ghost", async () => {
    if (!confirm("换码后，扫旧码进来的访客都要重新扫；你和家人不受影响。确定？")) return;
    try {
      const answer = await call("POST", "api/code", { device: d.id });
      if (!answer.ok) { toast(answer.error, true); return; }
      toast("已换新码，小电视换台后显示新的二维码");
      closeOverlays();
      loadDevices();
    } catch (error) { if (error instanceof SignedOut) signedOut(); }
  });
  const row = el("div", "row");
  row.append(rotate, button("关闭", "go", closeOverlays));
  modal("「" + d.name + "」的访客码", box,
        el("p", "", "客人用手机相机或微信扫这个码，就能遥控这一台（换台、音量、投屏），看不到你的订阅，也碰不到别的设备。小电视状态页上的二维码是同一个。"),
        el("div", "link", d.guest_link), row);
}

// ---------- 设备的 Wi-Fi（主人；接口和手机同一套：GET/POST api/wifi，见 server/relay/wifi.py） ----------
// 密码加密后经电视服务发给设备，只有那台设备解得开；这里也拿不到已保存的密码。
let wifiDev = null;     // { id, name, data, wait }
let wifiTimer = 0;

async function openWifi(d) {
  wifiDev = { id: d.id, name: d.name, data: null, wait: null };
  const ssid = el("input"); ssid.type = "text"; ssid.id = "wifiSsid"; ssid.placeholder = "Wi-Fi 名字"; ssid.maxLength = 32;
  ssid.autocomplete = "off"; ssid.spellcheck = false;
  const pass = el("input"); pass.type = "password"; pass.id = "wifiPass"; pass.placeholder = "密码（没有就留空）";
  pass.autocomplete = "new-password"; pass.maxLength = 64;
  const add = button("保存到小电视", "go", () => {
    if (!ssid.value.trim()) { toast("先填 Wi-Fi 名字", true); return; }
    if (pass.value && pass.value.length < 8) { toast("Wi-Fi 密码至少 8 位；没有密码就留空", true); return; }
    const body = { op: "add", ssid: ssid.value, password: pass.value };
    pass.value = "";
    wifiChange(body, "保存「" + ssid.value + "」");
  });
  add.id = "wifiAdd";
  const form = el("div", "wifiform");
  form.append(ssid, pass, add);
  const rows = el("div", "list");
  rows.id = "wifiRows";
  rows.style.margin = "8px 0 0";
  const state = el("div", "wifistate");
  state.id = "wifiState";
  const row = el("div", "row");
  row.append(button("关闭", "ghost", () => { closeWifi(); closeOverlays(); }));
  modal("「" + d.name + "」的 Wi-Fi",
        el("p", "", "小电视按这个顺序找能连上的 Wi-Fi，信号太弱的排到最后。新加的排在最前面；最多存 10 个。"),
        rows, state, form,
        el("p", "", "「立即切换」会让小电视断开几秒；连不上时它会自己回到原来的 Wi-Fi。"), row);
  $("modal").classList.add("wide");
  await loadWifi();
}

function closeWifi() {
  wifiDev = null;
  clearTimeout(wifiTimer);
  $("modal").classList.remove("wide");
}

async function loadWifi() {
  if (!wifiDev) return;
  clearTimeout(wifiTimer);
  let answer;
  try { answer = await call("GET", "api/wifi?device=" + encodeURIComponent(wifiDev.id)); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  if (!wifiDev || !answer.ok) return;
  wifiDev.data = answer;
  renderWifi();
  if (wifiDev && wifiDev.wait) wifiTimer = setTimeout(loadWifi, 1000);
}

function wifiSay(text, kind) {
  const box = $("wifiState");
  if (!box) return;
  box.textContent = text;
  box.className = "wifistate" + (kind ? " " + kind : "");
}

function renderWifi() {
  const d = wifiDev.data, wait = wifiDev.wait;
  const rows = $("wifiRows");
  if (!rows) return;
  rows.replaceChildren();
  $("wifiAdd").disabled = !d.supported || !!wait;
  if (!d.supported) { rows.appendChild(emptyRow("这台小电视的固件还不支持在这里管理 Wi-Fi，升级后再来")); return; }
  if (d.key_changed) wifiSay("小电视的钥匙变了（刷过机？）：先在设备里点「重新配对」，再让它重新连一次网。", "bad");
  const names = d.networks || [];
  if (!names.length) rows.appendChild(emptyRow("还没有保存的 Wi-Fi"));
  names.forEach((name, i) => {
    const using = d.online && name === d.current;
    const up = button("上移", "linkbtn", () => wifiChange({ op: "up", i, ssid: name }, "「" + name + "」上移"));
    up.disabled = i === 0 || !!wait;
    const use = button("立即切换", "linkbtn", () => {
      if (!confirm("让小电视马上换到「" + name + "」？它会断开几秒；连不上时会自己回到原来的 Wi-Fi。")) return;
      wifiChange({ op: "switch", i, ssid: name }, "切换到「" + name + "」");
    });
    use.disabled = using || !!wait;
    const remove = button("删除", "linkbtn red", () => {
      const last = names.length === 1 ? "这是最后一个，删掉后小电视下次开机会进入配网模式。" : "";
      if (!confirm("从小电视上删掉「" + name + "」？" + last)) return;
      wifiChange({ op: "remove", i, ssid: name }, "删除「" + name + "」");
    });
    remove.disabled = !!wait;
    const row = listRow("wifirow", pad(i + 1) + "  " + name, using ? "正在用" : "", [up, use, remove]);
    rows.appendChild(row);
  });
  followWifi();
}

function followWifi() {
  const d = wifiDev.data, wait = wifiDev.wait;
  if (!wait) return;
  const p = d.pending;
  if (!p || p.seq !== wait.seq) return;
  if (p.state === "sent") { wifiSay(wait.label + "：正在发给小电视…", "busy"); return; }
  if (p.state !== "saved") { wifiSay(wait.label + "：" + p.error, "bad"); wifiDev.wait = null; renderWifi(); return; }
  if (wait.op !== "switch") { wifiSay(wait.label + "：已保存", "ok"); wifiDev.wait = null; renderWifi(); return; }
  const elapsed = (Date.now() - wait.sentAt) / 1000;
  if (d.online && d.current === wait.ssid) {
    wifiSay("已切换到「" + wait.ssid + "」", "ok");
  } else if (d.online && elapsed > 8 && d.current && d.reported_at * 1000 > wait.sentAt) {
    wifiSay("没能连上「" + wait.ssid + "」（密码不对或不在范围内），小电视回到了「" + d.current + "」", "bad");
  } else if (elapsed > 90) {
    wifiSay("小电视还没重新上线。过一会儿刷新看看；连不上时它会自己回到能用的 Wi-Fi。", "bad");
  } else {
    wifiSay("已保存，小电视正在切换到「" + wait.ssid + "」…", "busy");
    return;
  }
  wifiDev.wait = null;
  renderWifi();
}

async function wifiChange(body, label) {
  if (!wifiDev) return;
  let answer;
  try { answer = await call("POST", "api/wifi", Object.assign({ device: wifiDev.id }, body)); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  if (!answer.ok) { wifiSay(label + "：" + answer.error, "bad"); return; }
  if (body.op === "add" && $("wifiSsid")) $("wifiSsid").value = "";
  wifiDev.wait = { seq: answer.seq, op: body.op, ssid: body.ssid, label, sentAt: Date.now() };
  wifiSay(label + "：正在发给小电视…", "busy");
  await loadWifi();
}

// 绑定码：云端 20 位（A–Z、2–7），自建 / 电脑客户端 6 位数字（/api/state 的 claim_digits）。两种都认，服务器说了算。
const CLAIM_CODE = /^(?:[0-9]{6}|[A-Z2-7]{20})$/;
function claimField() {
  const input = $("claim");
  if (!input || !data || !data.claim_digits || input.dataset.digits) return;
  input.dataset.digits = "1";
  input.placeholder = "小电视屏幕上的 6 位数字";
  input.inputMode = "numeric";
  input.setAttribute("autocapitalize", "off");
  input.setAttribute("enterkeyhint", "go");
}
function claimFrom(text) {
  const claim = String(text || "").replace(/[\s-]/g, "").toUpperCase();
  if (!claim) return { error: "请先输入绑定码" };
  if (!CLAIM_CODE.test(claim)) {
    return { error: data && data.claim_digits ? "绑定码是小电视屏幕上的 6 位数字" : "绑定码是小电视屏幕上的 20 位字母和数字" };
  }
  return { claim };
}
function bindDevice() {
  busy($("bind"), async () => {
    const { claim, error } = claimFrom($("claim").value);
    if (error) { toast(error, true); return; }
    const answer = await call("POST", "api/bind", { claim });
    if (!answer.ok) { toast(answer.error, true); return; }
    $("claim").value = "";
    toast("已绑定「" + answer.device.name + "」，小电视几秒后换成你的频道");
    loadDevices(); refresh(); loadTargets();
  });
}

// ======================================================================= 设置：登录的地方

async function loadSessions() {
  let answer;
  try { answer = await call("GET", "api/sessions"); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  const rows = $("sessionRows");
  rows.replaceChildren();
  const kinds = { phone: "手机", pc: "电脑", guest: "访客" };
  for (const s of answer.sessions) {
    const notes = [];
    if (s.kind === "guest") notes.push(s.device_name || s.device_id);
    if (s.current) notes.push("这台电脑");
    if (s.via === "recover") notes.push("设备恢复登录");
    else if (s.via === "admin") notes.push("管理员给的链接");
    else if (s.via === "local") notes.push("电脑上的" + BRAND_ZH + "程序");
    const sub = [s.browser, s.ip_prefix || "", "最近 " + when(s.last_seen), "登录 " + when(s.created_at)].filter(Boolean).join(" · ");
    const acts = s.current ? [] : [button("退出", "linkbtn red", async () => {
      try {
        const result = await call("POST", "api/sessions/revoke", { id: s.id });
        if (!result.ok) toast(result.error, true);
      } catch (error) { if (error instanceof SignedOut) signedOut(); }
      loadSessions();
    })];
    const row = listRow(s.current ? "air" : "", (kinds[s.kind] || s.kind) + (notes.length ? " · " + notes.join(" · ") : ""), sub, acts);
    rows.appendChild(row);
  }
  if (!answer.sessions.length) rows.appendChild(emptyRow("没有别的登录"));
}
$("logoutOthers").onclick = () => busy($("logoutOthers"), async () => {
  if (!confirm("退出你在别的手机和电脑上的登录？它们要重新登录才能用。")) return;
  const answer = await call("POST", "api/sessions/revoke", { others: true });
  toast(answer.ok ? "已退出 " + answer.count + " 个登录" : answer.error, !answer.ok);
  loadSessions();
});

// ---------- 设置 → B 站账号（家庭服务器和电脑客户端；接口见 server/relay/bililogin.py） ----------
// 每个人扫码登录自己的 B 站账号。二维码 3 分钟过期；页面每 2.5 秒问一次结果，过期、连续出错或离开设置页就停。

let bili = null, biliQr = null, biliRun = 0, biliLoading = false;
function biliOn() { return !!(data && data.bili && me && me.role !== "guest"); }
function renderBiliGate() {
  renderYtImportGate();
  $("biliBlock").hidden = !biliOn();
  // 有「我的关注」（自动同步）时不用一个个导入了。
  $("biliImportRow").hidden = !biliOn() || feedsOn();
  if (biliOn() && route === "settings" && !bili && !biliQr) loadBili();
}
async function loadBili(fresh) {
  if (!biliOn()) { $("biliBlock").hidden = true; return; }
  $("biliBlock").hidden = false;
  if (biliQr) { renderBili(); return; }
  if (biliLoading && !fresh) return;
  biliLoading = true;
  let answer;
  try { answer = await call("GET", "api/bili/account" + (fresh ? "?fresh=1" : "")); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  finally { biliLoading = false; }
  bili = answer;
  renderBili();
}
function biliFace(face, cls) {
  return thumb({ cover: face || "", name: bili && bili.name || "B" }, "f", cls);
}
function renderBili() {
  const box = $("biliBox");
  box.replaceChildren();
  const acts = el("div", "biliacts");
  if (biliQr) {
    $("biliSum").textContent = "扫码登录";
    const wrap = el("div", "biliqr");
    const qrb = el("div", "qrbox");
    qrb.innerHTML = biliQr.svg;
    if (biliQr.state === "expired" || biliQr.stopped) {
      const over = el("div", "over");
      over.append(el("span", "", biliQr.state === "expired" ? "二维码过期" : biliQr.text),
                  button(biliQr.state === "expired" ? "换一个" : "再试一次", "go", () => biliStart()));
      qrb.appendChild(over);
    }
    const side = el("div");
    const ol = el("ol");
    ol.append(el("li", "", "打开手机上的哔哩哔哩 App"), el("li", "", "用 App 里的「扫一扫」扫左边的码"), el("li", "", "在手机上点「确认登录」"));
    const cls = biliQr.state === "scanned" ? " ok" : (biliQr.state === "error" || biliQr.state === "expired") ? " bad" : "";
    side.append(el("p", "bilistate" + cls, biliQr.text), ol, button("取消", "ghost", () => { biliCancel(); renderBili(); }));
    wrap.append(qrb, side);
    box.appendChild(wrap);
    return;
  }
  if (!bili) { box.appendChild(el("p", "hint", "正在读取…")); return; }
  if (!bili.login) {
    $("biliSum").textContent = "没有登录";
    box.appendChild(el("p", "", "没有登录：B 站按游客给清晰度（最高 720P），会员剧集只能试看、放不了。"));
    acts.appendChild(button("扫码登录", "go", () => biliStart()));
  } else {
    $("biliSum").textContent = bili.invalid ? "登录已失效" : bili.vip ? (bili.vip_label || "大会员") : "已登录";
    const who = el("div", "biliwho");
    const text = el("div");
    text.appendChild(el("b", "", bili.name || "（还没取到昵称）"));
    const meta = [bili.vip ? (bili.vip_label || "大会员") : "普通账号"];
    if (bili.level) meta.push("Lv" + bili.level);
    meta.push("电脑上看最高 " + (bili.vip ? "1080P+" : "1080P") + "，小电视 720P");
    text.appendChild(el("span", bili.vip ? "vip" : "", meta.join(" · ")));
    who.append(biliFace(bili.face), text);
    box.appendChild(who);
    if (bili.invalid) {
      box.appendChild(el("p", "bilistate bad", "登录已失效，请重新扫码"));
      acts.append(button("重新扫码", "go", () => biliStart()), button("删除登录信息", "ghost", biliLogout));
    } else {
      acts.append(button("刷新", "ghost", (event) => busy(event.target, () => loadBili(true))),
                  button("退出登录", "ghost", biliLogout));
    }
  }
  box.appendChild(acts);
  if (bili.error) box.appendChild(el("p", "bilistate bad", bili.error));
}
async function biliStart() {
  const run = ++biliRun;
  let answer;
  try { answer = await call("POST", "api/bili/login/start", {}); }
  catch (error) { if (error instanceof SignedOut) signedOut(); else toast("网络不通", true); return; }
  if (run !== biliRun) return;
  if (!answer.ok) { toast(answer.error, true); return; }
  biliQr = { svg: answer.svg, state: answer.state, text: answer.text, stopped: false };
  renderBili();
  biliPoll(run);
}
function biliCancel() {
  biliRun++;
  biliQr = null;
}
async function biliPoll(run) {
  let errors = 0;
  while (run === biliRun && biliQr) {
    await sleep(2500);
    if (run !== biliRun || !biliQr) return;
    if (route !== "settings" || $("app").hidden) { biliCancel(); return; }
    let answer;
    try { answer = await call("POST", "api/bili/login/poll", {}); }
    catch (error) {
      if (error instanceof SignedOut) { signedOut(); return; }
      answer = { state: "error", text: "网络不通" };
    }
    if (run !== biliRun || !biliQr) return;
    if (answer.state === "done") {
      biliQr = null;
      bili = answer;
      renderBili();
      toast("B 站登录成功" + (answer.name ? "：" + answer.name : ""));
      refresh();
      resetFeeds();
      return;
    }
    biliQr.state = answer.state;
    biliQr.text = answer.text || "";
    if (answer.state === "error") errors++; else errors = 0;
    // 连着三次出错就停下（不一直问），等人点「再试一次」。
    if (answer.state === "expired" || errors >= 3) { biliQr.stopped = true; renderBili(); return; }
    renderBili();
  }
}
async function biliLogout() {
  if (!confirm("退出 B 站登录？这台电脑上存的登录信息会马上删掉，之后按游客的清晰度放。")) return;
  try { bili = await call("POST", "api/bili/logout", {}); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  toast("已退出 B 站登录");
  renderBili();
  refresh();
  resetFeeds();
}

// ---------- UP 主 → 从 B 站导入关注的 UP 主（GET api/bili/followings，POST api/bili/followings/import） ----------

let follow = null;    // { items, picked: Set, q, max, count, total, note }
function followLeft() { return follow ? Math.max(0, follow.max - follow.count) : 0; }
$("biliImport").onclick = openFollow;
async function openFollow() {
  const b = data && data.bili;
  if (!b || !b.login || b.invalid) {
    toast(b && b.invalid ? "B 站登录已失效：先在「设置 → B 站账号」重新扫码" : "先在「设置 → B 站账号」扫码登录 B 站", true);
    location.hash = "#/settings/bili";
    return;
  }
  follow = { items: null, picked: new Set(), q: "", max: 12, count: 0, total: 0, note: "" };
  const q = el("input");
  q.type = "search"; q.placeholder = "在关注里找（名字或签名）"; q.maxLength = 40; q.autocomplete = "off"; q.spellcheck = false;
  q.oninput = () => { follow.q = q.value.trim().toLowerCase(); renderFollow(); };
  const tools = el("div", "followtools");
  tools.append(q, button("全选", "ghost", followAll), button("全不选", "ghost", () => { follow.picked.clear(); renderFollow(); }));
  const sum = el("p", "followsum", "正在取你关注的 UP 主…");
  sum.id = "followSum";
  const list = el("div", "follows");
  list.id = "followList";
  const go2 = button("导入选中的", "go", followImport);
  go2.id = "followGo"; go2.disabled = true;
  const row = el("div", "row");
  row.append(button("关闭", "ghost", closeOverlays), go2);
  modal("从 B 站导入关注的 UP 主", tools, sum, list,
        el("p", "", "最多 12 个 UP 主：每个 UP 主是小电视上的一个台，台名要放进小电视的设置包（大小有限），装不下更多。想换就先删掉几个。每个 UP 主从最新的投稿开始轮播；没有投稿的不会加。"),
        row);
  $("modal").classList.add("bfollow");
  let answer;
  try { answer = await call("GET", "api/bili/followings"); }
  catch (error) { if (error instanceof SignedOut) signedOut(); else sum.textContent = "网络不通"; return; }
  if (!follow) return;
  if (!answer.ok) {
    sum.textContent = answer.error || "取不到关注列表";
    if (answer.login === false) refresh();
    return;
  }
  Object.assign(follow, { items: answer.items, max: answer.max, count: answer.count, total: answer.total });
  renderFollow();
}
function followShown() {
  const q = follow.q;
  return follow.items.filter((f) => !q || f.name.toLowerCase().includes(q) || (f.sign || "").toLowerCase().includes(q));
}
function renderFollow() {
  if (!follow || !follow.items) return;
  const list = $("followList");
  list.replaceChildren();
  const shown = followShown();
  if (!follow.items.length) list.appendChild(el("div", "empty-t", "你还没有关注任何 UP 主"));
  else if (!shown.length) list.appendChild(el("div", "empty-t", "没有找到"));
  for (const f of shown) {
    const row = el("label", "frow" + (f.subscribed ? " had" : ""));
    const box = el("input");
    box.type = "checkbox";
    box.checked = f.subscribed || follow.picked.has(f.mid);
    box.disabled = f.subscribed;
    box.onchange = () => {
      if (box.checked && follow.picked.size >= followLeft()) {
        box.checked = false;
        toast(followLeft() ? "最多还能加 " + followLeft() + " 个 UP 主" : "UP 主已经满 12 个了，先删掉几个", true);
        return;
      }
      if (box.checked) follow.picked.add(f.mid); else follow.picked.delete(f.mid);
      followSum();
    };
    const name = el("div", "n", f.name);
    if (f.sign) name.appendChild(el("small", "", f.sign));
    row.append(box, thumb({ cover: f.face || "", name: f.name }, "f"), name, el("span", "tag2", f.subscribed ? "已订阅" : ""));
    list.appendChild(row);
  }
  followSum();
}
function followSum() {
  const had = follow.items.filter((f) => f.subscribed).length;
  $("followSum").textContent = "关注了 " + follow.total + " 个（其中 " + had + " 个已订阅）· UP 主 " + follow.count + " / " + follow.max +
    " · 选了 " + follow.picked.size + " 个，还能加 " + Math.max(0, followLeft() - follow.picked.size) + " 个" + (follow.note ? " · " + follow.note : "");
  $("followGo").disabled = !follow.picked.size;
}
function followAll() {
  if (!follow || !follow.items) return;
  let cut = false;
  for (const f of followShown()) {
    if (f.subscribed || follow.picked.has(f.mid)) continue;
    if (follow.picked.size >= followLeft()) { cut = true; break; }
    follow.picked.add(f.mid);
  }
  if (cut) toast("只选了前 " + followLeft() + " 个：UP 主最多 " + follow.max + " 个", true);
  renderFollow();
}
async function followImport() {
  const btn = $("followGo");
  const mids = Array.from(follow.picked);
  await busy(btn, async () => {
    toast("正在导入 " + mids.length + " 个 UP 主（每个要取一次投稿列表）…");
    const answer = await call("POST", "api/bili/followings/import", { mids });
    if (!follow) return;
    if (!answer.ok) { toast(answer.error, true); return; }
    const added = new Set(answer.added.map((a) => a.mid));
    for (const f of follow.items) if (added.has(f.mid)) f.subscribed = true;
    follow.count += answer.added.length;
    follow.picked.clear();
    const why = answer.skipped.map((x) => x.name + "（" + x.reason + "）");
    follow.note = why.length ? "没加：" + why.slice(0, 4).join("、") + (why.length > 4 ? " 等 " + why.length + " 个" : "") : "";
    toast(answer.added.length ? "导入了 " + answer.added.length + " 个 UP 主" + (why.length ? "，" + why.length + " 个没加" : "")
          : "一个也没加：" + (why[0] || ""), !answer.added.length);
    renderFollow();
    refresh();
    loadHome(true);
  });
}

// ---------- 设置 → YouTube（家庭服务器和电脑客户端的管理员；接口见 server/relay/ytsettings.py） ----------

let yt = null, ytEdited = false, ytPoll = 0;
const YT_MODES = { off: "关闭", local: "本机", node: "自己的节点" };

async function loadYouTube() {
  if (!me || me.role !== "admin") return;
  let answer;
  try { answer = await call("GET", "api/youtube/settings"); }
  catch (error) { $("ytLocal").hidden = true; return; }   // 云端没有这个接口（404）
  if (!answer.ok) return;
  yt = answer;
  renderYouTube();
}

function ytMode() {
  const picked = qs("input[name=ytMode]:checked");
  return picked ? picked.value : "off";
}

function renderYouTube() {
  if (!yt) return;
  $("ytLocal").hidden = false;
  $("ytBlock").hidden = true;
  if (!ytEdited) {
    for (const r of qsa("input[name=ytMode]")) r.checked = r.value === yt.mode;
    $("ytProxy").value = yt.proxy || "";
  }
  $("ytConfigError").hidden = !yt.config_error;
  $("ytConfigError").textContent = yt.config_error ? "config.toml 有错：" + yt.config_error : "";
  const health = { ok: "正常", login: "要求登录（导入或更新 cookies）", error: "上次出错了", unknown: "还没用过", unreachable: "没有响应" }[yt.health] || "";
  $("ytRun").textContent = "现在：" + (YT_MODES[yt.running] || yt.running) + (yt.running !== "off" && health ? " · " + health : "")
    + (yt.running !== yt.mode ? "（正在换成「" + (YT_MODES[yt.mode] || yt.mode) + "」）" : "");
  const mode = ytMode();
  $("ytProxyBox").hidden = mode !== "local";
  $("ytParts").hidden = mode !== "local";
  $("ytCookiesBox").hidden = mode !== "local";
  const c = yt.components;
  let line;
  if (!c.supported) line = "这台电脑的系统没有官方的 deno，本机模式用不了；可以用「自己的节点」。";
  else if (c.state === "working") line = "正在下载 " + (c.what || "") + (c.total ? "：" + Math.floor(c.done / c.total * 100) + "%（" + (c.done / 1e6).toFixed(1) + " / " + (c.total / 1e6).toFixed(1) + " MB）" : c.done ? "：" + (c.done / 1e6).toFixed(1) + " MB" : "…");
  else if (c.ready) line = "已安装：yt-dlp " + c.ytdlp + "，deno " + c.deno + (c.update ? "。有新版 yt-dlp " + c.latest + "，点「更新 yt-dlp」" : c.latest ? "（已是最新）" : "");
  else line = "还没安装" + (yt.mode === "local" ? "：点「安装」（经上面的代理下载）" : "");
  if (c.state === "failed" && c.error) line += "。上次失败：" + c.error;
  $("ytPartsState").textContent = line;
  $("ytPartsState").style.color = c.state === "failed" ? "var(--bad)" : "";
  $("ytInstall").hidden = !c.supported || c.ready;
  $("ytUpdate").hidden = !c.ready;
  $("ytCheck").hidden = !c.ready;
  for (const b of [$("ytInstall"), $("ytUpdate"), $("ytCheck")]) b.disabled = c.state === "working";
  const k = yt.cookies;
  $("ytCookiesState").textContent = k.present ? "已导入（" + k.count + " 条，YouTube 域名，" + when(k.updated) + "）" : "没有导入";
  $("ytCookiesDelete").hidden = !k.present;
  if (mode === "local" && route === "settings" && !ytBrowsers && !ytBrowserLoading) loadBrowsers();
  renderYtImportGate();
  clearTimeout(ytPoll);
  if (c.state === "working" || yt.running !== yt.mode) ytPoll = setTimeout(() => { if (route === "settings") loadYouTube(); }, 1500);
}
for (const r of qsa("input[name=ytMode]")) r.onchange = () => { ytEdited = true; renderYouTube(); };
$("ytProxy").oninput = () => { ytEdited = true; };

$("ytDetect").onclick = () => busy($("ytDetect"), async () => {
  const answer = await call("POST", "api/youtube/detect", {});
  if (answer.proxy) { $("ytProxy").value = answer.proxy; ytEdited = true; }
  $("ytDetected").textContent = answer.message || answer.error || "";
});

$("ytSave").onclick = () => busy($("ytSave"), async () => {
  const answer = await call("POST", "api/youtube/settings", { mode: ytMode(), proxy: $("ytProxy").value.trim() });
  if (!answer.ok) { toast(answer.error, true); return; }
  ytEdited = false;
  $("ytTestResult").textContent = "";
  toast(!answer.changed ? "没有改动" : answer.restart ? "已保存，正在重启 YouTube 和遥控服务（几秒钟）" : "已保存，几秒后生效");
  if (answer.changed) await ytAfterSave();
});

// 保存后服务会重启几秒：一直问到能回答为止（最多 30 秒）。
async function ytAfterSave() {
  for (let i = 0; i < 20; i++) {
    await sleep(1500);
    try {
      const answer = await call("GET", "api/youtube/settings");
      if (answer.ok) { yt = answer; renderYouTube(); if (answer.running === answer.mode) { refresh(); return; } }
    } catch (error) { if (error instanceof SignedOut) { signedOut(); return; } }
  }
}

$("ytTest").onclick = () => busy($("ytTest"), async () => {
  $("ytTestResult").style.color = "";
  $("ytTestResult").textContent = ytEdited ? "改动还没保存：先点「保存」，测试的是正在用的设置。" : "正在测试（解析一个公开的测试视频，最多 1 分钟）…";
  if (ytEdited) return;
  const answer = await call("POST", "api/youtube/test", {});
  $("ytTestResult").style.color = answer.ok ? "var(--ok)" : "var(--bad)";
  $("ytTestResult").textContent = answer.message || answer.error || "";
  loadYouTube();
});

async function ytParts(action, btn) {
  await busy(btn, async () => {
    const answer = await call("POST", "api/youtube/components", { action, mirror: $("ytMirror").value.trim() });
    if (!answer.ok) { toast(answer.error, true); return; }
    if (action === "check") toast(answer.update ? "有新版 yt-dlp：" + answer.latest : "yt-dlp " + answer.current + " 已是最新");
    if (answer.components && yt) { yt.components = answer.components; renderYouTube(); }
    loadYouTube();
  });
}
$("ytInstall").onclick = () => ytParts("install", $("ytInstall"));
$("ytUpdate").onclick = () => ytParts("update", $("ytUpdate"));
$("ytCheck").onclick = () => ytParts("check", $("ytCheck"));

// cookies.txt：只把 youtube.com、google.com 的行发给服务器（别的网站的 cookies 不离开这台电脑）。
$("ytCookiesPick").onclick = () => $("ytCookiesFile").click();
$("ytCookiesFile").onchange = () => {
  const file = $("ytCookiesFile").files[0];
  $("ytCookiesFile").value = "";
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) { toast("文件太大了", true); return; }
  const reader = new FileReader();
  reader.onload = () => busy($("ytCookiesPick"), async () => {
    const keep = /(^|\.)(youtube|google)\.com$/i;
    const lines = String(reader.result).split(/\r?\n/).filter((line) => {
      const domain = line.replace(/^#HttpOnly_/, "").split("\t")[0];
      return (line.startsWith("#HttpOnly_") || !line.startsWith("#")) && line.split("\t").length === 7 && keep.test(domain.replace(/^\./, ""));
    });
    if (!lines.length) { toast("文件里没有 youtube.com 的 cookies（要 Netscape 格式的 cookies.txt）", true); return; }
    const text = "# Netscape HTTP Cookie File\n" + lines.join("\n") + "\n";
    if (text.length > 48 * 1024) { toast("YouTube 的 cookies 太多了：只导出 youtube.com 再试", true); return; }
    const answer = await call("POST", "api/youtube/cookies", { text });
    if (!answer.ok) { toast(answer.error, true); return; }
    toast("已导入（" + answer.cookies.count + " 条，YouTube 域名）");
    if (yt) { yt.cookies = answer.cookies; renderYouTube(); }
  });
  reader.onerror = () => toast("读不了这个文件", true);
  reader.readAsText(file);
};
$("ytCookiesDelete").onclick = () => busy($("ytCookiesDelete"), async () => {
  if (!confirm("删除导入的 YouTube cookies？")) return;
  const answer = await call("POST", "api/youtube/cookies/delete", {});
  if (answer.ok && yt) { yt.cookies = answer.cookies; renderYouTube(); toast("已删除"); }
});

// ---------- 设置 → YouTube → 从浏览器读取（只在这台电脑上打开时有；接口见 server/relay/ytaccount.py） ----------
// 服务器只对「在运行它的这台电脑上直接打开」的电脑版回答（经隧道、局域网打开时 404），所以 404 就整块藏起来。
// 读取在服务器的后台做（macOS 的钥匙串窗口要等人点）：页面每 1.5 秒问一次结果。

let ytBrowsers = null, ytBrowserLoading = false, ytBrowserPick = "", ytBrowserPoll = 0;
const YT_BROWSER_NOTES = {
  mac: "macOS：读 Chrome、Edge、Brave 时会弹出钥匙串窗口，询问能否使用「Chrome Safe Storage」（Edge、Brave 是它们自己的名字）：输入这台 Mac 的登录密码，点「允许」。读 Safari 要先在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里打开「小鼠电视」（RooTV），再退出、重新打开小鼠电视。",
  windows: "Windows：先完全退出要读的浏览器（开着时 cookies 文件被锁住）。新版 Chrome、Edge 的 cookies 只有浏览器自己能解开，读不了时换 Firefox，或用「导入 cookies.txt」。",
  linux: "Linux：Chrome、Edge、Brave 的 cookies 用系统钥匙环加密，可能要求解锁钥匙环。",
};
async function loadBrowsers() {
  if (!me || me.role !== "admin") return;
  ytBrowserLoading = true;
  let answer;
  try { answer = await call("GET", "api/youtube/browsers"); }
  catch (error) { $("ytBrowserBox").hidden = true; ytBrowsers = { hidden: true }; return; }  // 不是在这台电脑上打开的（404）
  finally { ytBrowserLoading = false; }
  if (!answer.ok) return;
  ytBrowsers = answer;
  renderBrowsers();
}
function renderBrowsers() {
  const a = ytBrowsers;
  if (!a || a.hidden) { $("ytBrowserBox").hidden = true; return; }
  $("ytBrowserBox").hidden = false;
  const box = $("ytBrowsers");
  box.replaceChildren();
  if (!a.browsers.some((b) => b.id === ytBrowserPick)) ytBrowserPick = a.browsers.length ? a.browsers[0].id : "";
  for (const b of a.browsers) {
    const btn = button(b.name, "", () => { ytBrowserPick = b.id; renderBrowsers(); });
    btn.setAttribute("aria-pressed", String(b.id === ytBrowserPick));
    box.appendChild(btn);
  }
  const chosen = a.browsers.find((b) => b.id === ytBrowserPick);
  const sel = $("ytProfile");
  const keep = sel.dataset.browser === ytBrowserPick ? sel.value : "";
  sel.replaceChildren();
  for (const p of (chosen && chosen.profiles) || []) {
    const o = el("option", "", p.name + (p.account ? " · " + p.account : ""));
    o.value = p.id;
    sel.appendChild(o);
  }
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
  sel.dataset.browser = ytBrowserPick;
  sel.hidden = !chosen || !chosen.profiles.length;
  const working = a.job && a.job.state === "working";
  $("ytBrowserRead").disabled = !chosen || working || !a.ytdlp;
  $("ytBrowserRead").textContent = chosen ? "读取 " + chosen.name + " 里的 YouTube 登录" : "读取";
  let note = YT_BROWSER_NOTES[a.system] || "";
  if (!a.browsers.length) note = "这台电脑上没有找到 Chrome、Edge、Firefox、Safari 或 Brave。" ;
  else if (!a.ytdlp) note = "读取要用到 YouTube 组件里的 yt-dlp：先在上面点「安装」。" + note;
  $("ytBrowserNote").textContent = note + " 只存 youtube.com 和 google.com 的 cookies，别的网站的不会留下。";
  const j = a.job || {}, st = $("ytBrowserState");
  st.className = j.state === "failed" ? "bad" : j.state === "done" ? "ok" : "";
  const what = (j.browser_name || "") + (j.profile_name ? "「" + j.profile_name + "」" : "");
  if (j.state === "working") st.textContent = "正在读取 " + what + "…" + (a.system === "mac" && j.browser !== "safari" && j.browser !== "firefox" ? "弹出钥匙串窗口时点「允许」。" : "");
  else if (j.state === "done") st.textContent = "已读取（" + j.count + " 条，YouTube 域名）· " + (j.account ? "账号：" + j.account : "浏览器配置：" + (what || "—")) + " · " + when(j.at);
  else if (j.state === "failed") st.textContent = j.message || "读取失败";
  else st.textContent = "";
  clearTimeout(ytBrowserPoll);
  if (working) ytBrowserPoll = setTimeout(pollBrowsers, 1500);
}
async function pollBrowsers() {
  if (route !== "settings") return;
  const before = ytBrowsers && ytBrowsers.job ? ytBrowsers.job.state : "";
  await loadBrowsers();
  if (before === "working" && ytBrowsers && ytBrowsers.job && ytBrowsers.job.state !== "working") {
    if (ytBrowsers.job.state === "done") toast("已读取 YouTube 登录（" + ytBrowsers.job.count + " 条）");
    loadYouTube();
  }
}
$("ytBrowserRead").onclick = () => busy($("ytBrowserRead"), async () => {
  const chosen = ytBrowsers && ytBrowsers.browsers.find((b) => b.id === ytBrowserPick);
  if (!chosen) return;
  const profile = chosen.profiles.length ? $("ytProfile").value : "";
  const p = chosen.profiles.find((x) => x.id === profile);
  const who = chosen.name + (p ? "「" + p.name + "」" + (p.account ? "（" + p.account + "）" : "") : "");
  if (!confirm("从 " + who + " 读取 YouTube 登录？\n\n会读取这个浏览器里当前登录的账号，通常是你的主账号。建议专门建一个只登录小号的浏览器配置，再选它。\n\n只保存 youtube.com 和 google.com 的 cookies，替换现在的 cookies。")) return;
  const answer = await call("POST", "api/youtube/browsers/read", { browser: chosen.id, profile });
  if (!answer.ok) { toast(answer.error, true); return; }
  ytBrowsers.job = answer.job;
  renderBrowsers();
});

// ---------- UP 主 → 从 YouTube 导入订阅的频道（管理员、本机模式、有 cookies；接口见 server/relay/ytaccount.py） ----------
// 订阅列表要服务器跑一次 yt-dlp（几秒到一两分钟），导入时每个频道再跑一次：都在后台做，页面每 2 秒问一次。

let ysub = null, ysubPoll = 0;   // { items, picked: Set, q, max, count, total, note, importing }
function ytImportOn() { return !!(data && data.youtube && data.youtube.kind === "local" && me && me.role === "admin"); }
function renderYtImportGate() {
  const on = ytImportOn() && !feedsOn();
  $("ytImportRow").hidden = !on;
  if (!on) return;
  if (!yt && route === "up" && !ytImportAsked) { ytImportAsked = true; loadYouTube(); }
  const cookies = !!(yt && yt.cookies && yt.cookies.present);
  $("ytImport").disabled = !cookies;
  $("ytImportWhy").textContent = !yt ? "" : cookies ? "" : "先在「设置 → YouTube」导入 cookies 或从浏览器读取登录";
}
let ytImportAsked = false;
function ysubLeft() { return ysub ? Math.max(0, ysub.max - ysub.count) : 0; }
$("ytImport").onclick = openYsub;
function ytFans(n) { return n >= 10000 ? (n / 10000).toFixed(n >= 100000 ? 0 : 1) + " 万订阅" : n ? n + " 订阅" : ""; }
async function openYsub() {
  ysub = { items: null, picked: new Set(), q: "", max: 12, count: 0, total: 0, note: "", importing: false };
  const q = el("input");
  q.type = "search"; q.placeholder = "在订阅里找（频道名或 @）"; q.maxLength = 40; q.autocomplete = "off"; q.spellcheck = false;
  q.oninput = () => { if (ysub) { ysub.q = q.value.trim().toLowerCase(); renderYsub(); } };
  const tools = el("div", "followtools");
  tools.append(q, button("全选", "ghost", ysubAll), button("全不选", "ghost", () => { if (ysub) { ysub.picked.clear(); renderYsub(); } }));
  const sum = el("p", "followsum", "正在取你在 YouTube 订阅的频道（要跑一次 yt-dlp，订阅多时要一两分钟）…");
  sum.id = "ysubSum";
  const list = el("div", "follows");
  list.id = "ysubList";
  const go2 = button("导入选中的", "go", ysubImport);
  go2.id = "ysubGo"; go2.disabled = true;
  const row = el("div", "row");
  row.append(button("关闭", "ghost", closeOverlays), go2);
  modal("从 YouTube 导入订阅的频道", tools, sum, list,
        el("p", "", "订阅列表来自「设置 → YouTube」里的 cookies 那个账号。最多 12 个 UP 主（YouTube 频道也算）：每个是小电视上的一个台，台名要放进小电视的设置包（大小有限）。每个频道从最新的视频开始轮播；没有视频的不会加。导入时每个频道要取一次视频列表（十几秒）。"),
        row);
  $("modal").classList.add("bfollow");
  await ysubLoad(false);
}
async function ysubLoad(fresh) {
  const mine = ysub;
  for (let i = 0; i < 150 && ysub === mine; i++) {
    let answer;
    try { answer = await call("GET", "api/youtube/subscriptions" + (fresh && !i ? "?fresh=1" : "")); }
    catch (error) { if (error instanceof SignedOut) signedOut(); else if ($("ysubSum")) $("ysubSum").textContent = "网络不通"; return; }
    if (ysub !== mine) return;
    if (!answer.ok) { $("ysubSum").textContent = answer.error || "取不到订阅列表"; if (answer.login === false) refresh(); return; }
    if (answer.state === "ready") {
      Object.assign(ysub, { items: answer.items, max: answer.max, count: answer.count, total: answer.total });
      renderYsub();
      return;
    }
    await sleep(2000);
  }
}
function ysubShown() {
  const q = ysub.q;
  return ysub.items.filter((f) => !q || f.name.toLowerCase().includes(q) || (f.handle || "").toLowerCase().includes(q));
}
function renderYsub() {
  if (!ysub || !ysub.items) return;
  const list = $("ysubList");
  list.replaceChildren();
  const shown = ysubShown();
  if (!ysub.items.length) list.appendChild(el("div", "empty-t", "这个账号没有订阅任何频道"));
  else if (!shown.length) list.appendChild(el("div", "empty-t", "没有找到"));
  for (const f of shown.slice(0, 500)) {
    const row = el("label", "frow" + (f.subscribed ? " had" : ""));
    const box = el("input");
    box.type = "checkbox";
    box.checked = f.subscribed || ysub.picked.has(f.id);
    box.disabled = f.subscribed || ysub.importing;
    box.onchange = () => {
      if (box.checked && ysub.picked.size >= ysubLeft()) {
        box.checked = false;
        toast(ysubLeft() ? "最多还能加 " + ysubLeft() + " 个 UP 主" : "UP 主已经满 12 个了，先删掉几个", true);
        return;
      }
      if (box.checked) ysub.picked.add(f.id); else ysub.picked.delete(f.id);
      ysubSum();
    };
    const name = el("div", "n", f.name);
    const sub = [f.handle, ytFans(f.fans)].filter(Boolean).join(" · ");
    if (sub) name.appendChild(el("small", "", sub));
    row.append(box, thumb({ cover: "", name: f.name }, "f"), name, el("span", "tag2", f.subscribed ? "已订阅" : ""));
    list.appendChild(row);
  }
  ysubSum();
}
function ysubSum() {
  const had = ysub.items.filter((f) => f.subscribed).length;
  $("ysubSum").textContent = "订阅了 " + ysub.total + " 个频道（其中 " + had + " 个已订阅）· UP 主 " + ysub.count + " / " + ysub.max +
    " · 选了 " + ysub.picked.size + " 个，还能加 " + Math.max(0, ysubLeft() - ysub.picked.size) + " 个" + (ysub.note ? " · " + ysub.note : "");
  $("ysubGo").disabled = !ysub.picked.size || ysub.importing;
}
function ysubAll() {
  if (!ysub || !ysub.items || ysub.importing) return;
  let cut = false;
  for (const f of ysubShown()) {
    if (f.subscribed || ysub.picked.has(f.id)) continue;
    if (ysub.picked.size >= ysubLeft()) { cut = true; break; }
    ysub.picked.add(f.id);
  }
  if (cut) toast("只选了前 " + ysubLeft() + " 个：UP 主最多 " + ysub.max + " 个", true);
  renderYsub();
}
async function ysubImport() {
  if (!ysub || ysub.importing) return;
  const ids = Array.from(ysub.picked);
  let answer;
  try { answer = await call("POST", "api/youtube/subscriptions/import", { ids }); }
  catch (error) { if (error instanceof SignedOut) signedOut(); else toast("网络不通", true); return; }
  if (!answer.ok) { toast(answer.error, true); return; }
  ysub.importing = true;
  renderYsub();
  ysubProgress(answer.job);
}
function ysubProgress(job) {
  if (!ysub) return;
  if (job.state === "working") {
    ysub.note = "正在导入 " + (job.done + 1 > job.total ? job.total : job.done + 1) + " / " + job.total + (job.current ? "：" + job.current : "") + "…";
    ysubSum();
    clearTimeout(ysubPoll);
    ysubPoll = setTimeout(async () => {
      let next;
      try { next = await call("GET", "api/youtube/subscriptions/import"); }
      catch (error) { if (error instanceof SignedOut) signedOut(); return; }
      if (next.ok) ysubProgress(next.job);
    }, 2000);
    return;
  }
  const added = new Set(job.added.map((a) => a.id));
  for (const f of ysub.items) if (added.has(f.id)) f.subscribed = true;
  ysub.count += job.added.length;
  ysub.picked.clear();
  ysub.importing = false;
  const why = job.skipped.map((x) => x.name + "（" + x.reason + "）");
  ysub.note = why.length ? "没加：" + why.slice(0, 4).join("、") + (why.length > 4 ? " 等 " + why.length + " 个" : "") : "";
  toast(job.error ? job.error : job.added.length ? "导入了 " + job.added.length + " 个频道" + (why.length ? "，" + why.length + " 个没加" : "")
        : "一个也没加：" + (why[0] || ""), !job.added.length || !!job.error);
  renderYsub();
  refresh();
  loadHome(true);
}

// ---------- 让手机登录：一次性的手机登录链接（5 分钟、只能用一次），手机用相机扫码在 Safari 里打开。 ----------
let phoneRun = 0;
async function phoneLink() {
  let answer;
  try { answer = await call("POST", "api/phone-link", {}); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  if (!answer.ok) { toast(answer.error, true); return; }
  const run = ++phoneRun;
  const box = el("div", "qrbox");
  box.innerHTML = answer.svg;   // 服务器画的二维码，只有数字和固定颜色
  const state = el("p", "", "5 分钟内有效，只能用一次。");
  const how = [el("p", "", "用手机相机扫这个二维码，在打开的 Safari（或手机浏览器）里点「在这台手机上登录」，再「添加到主屏幕」。"),
               el("p", "", "扫不了的话，在手机浏览器里打开 " + answer.typed + "，输入："), el("div", "pcode", answer.code)];
  const finish = (text, live) => {
    for (const node of [box, ...how]) node.hidden = true;
    box.replaceChildren();
    state.className = live ? "live" : "";
    state.textContent = text;
  };
  const row = el("div", "row");
  row.append(button("关闭", "go", closeOverlays));
  modal("让手机登录", box, ...how, state, el("p", "", "只给你自己的手机用：有这个码的人能以你的身份登录。"), row);
  const until = Date.now() + answer.ttl * 1000;
  while (run === phoneRun && !$("modal").hidden) {
    await sleep(2000);
    if (run !== phoneRun || $("modal").hidden) return;
    let status;
    try { status = await call("GET", "api/phone-link/status?poll=" + encodeURIComponent(answer.poll)); }
    catch (error) { if (error instanceof SignedOut) { signedOut(); return; } continue; }
    if (status.state === "used") {
      finish("手机已经登录了（" + status.phone + (status.network ? "，" + status.network : "") + "）。可以关掉这个窗口。", true);
      if (route === "settings") loadSessions();
      return;
    }
    if (status.state === "expired" || Date.now() > until) {
      finish("过期了。需要的话再点一次「让手机登录」。", false);
      return;
    }
  }
}
$("phoneLink1").onclick = phoneLink;
$("phoneLink2").onclick = phoneLink;

// ======================================================================= 自己账号的几排、我的关注
// 接口见 server/relay/accountfeeds.py、follows.py：家庭服务器和电脑客户端才有（/api/state 的 feeds），云端没有，
// 这里也就什么都不显示。每一排单独去问（GET api/feeds/row），不挡首页；YouTube 要十几秒，先显示「正在加载」。

let feedsInfo = null, feedsAt = 0;   // GET api/feeds：有哪几排、能不能用、设置
const feedRows = {};                 // 排 id → GET api/feeds/row 的回答
let feedRowsAt = 0, feedTimer = 0, feedTries = 0;
const FEED_SUB = { updates: "关注的 UP 主新发的，新的在前", bili_history: "接着上次看到的地方", bili_rec: "B 站首页给你的推荐",
                   yt_history: "YouTube 上看过的", yt_rec: "YouTube 首页给你的推荐" };
// 登录、退出 B 站以后：几排和关注列表都重新问（服务器那边已经开始同步关注）。
function resetFeeds() {
  for (const id of Object.keys(feedRows)) delete feedRows[id];
  feedsInfo = null; feedsAt = 0; feedRowsAt = 0; fw = null;
  setTimeout(() => { loadFeeds(true); if (route === "up") loadFollows(); }, 600);
}
function feedsOn() { return !!(data && data.feeds && me && !isGuest()); }
async function loadFeeds(force) {
  if (!feedsOn()) return;
  if (!force && feedsInfo && Date.now() - feedsAt < 60000) { loadFeedRows(false); return; }
  feedsAt = Date.now();
  let answer;
  try { answer = await call("GET", "api/feeds"); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  if (!answer || !answer.ok) return;
  const changed = JSON.stringify(feedsInfo) !== JSON.stringify(answer);
  feedsInfo = answer;
  applyFeedSettings();
  if (changed && route === "home") renderHome();
  loadFeedRows(force);
}
async function loadFeedRows(force) {
  if (!feedsInfo || (!force && Date.now() - feedRowsAt < 60000)) return;
  feedRowsAt = Date.now();
  await Promise.all(feedsInfo.rows.filter((r) => r.state === "ready").map((r) => loadFeedRow(r.id)));
  feedTries = 0;
  scheduleFeeds();
}
async function loadFeedRow(id, fresh) {
  let answer;
  try { answer = await call("GET", "api/feeds/row?id=" + encodeURIComponent(id) + (fresh ? "&fresh=1" : "")); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  if (!answer || !answer.ok) return;
  const before = JSON.stringify(feedRows[id] || null);
  feedRows[id] = answer;
  if (before !== JSON.stringify(answer) && route === "home") renderHome();
}
const feedWaiting = (id) => feedRows[id] && (feedRows[id].loading || feedRows[id].state === "loading");
// 还在加载的（YouTube）：每 3 秒再问一次，最多两分钟。离开首页就停。
function scheduleFeeds() {
  clearTimeout(feedTimer);
  const ids = Object.keys(feedRows).filter(feedWaiting);
  if (!ids.length || ++feedTries > 40) return;
  feedTimer = setTimeout(async () => {
    if (route !== "home" || document.hidden) return;
    await Promise.all(ids.map((id) => loadFeedRow(id)));
    scheduleFeeds();
  }, 3000);
}
function applyFeedSettings() {
  const s = (feedsInfo && feedsInfo.settings) || (fw && fw.settings) || {};
  $("writebackSw").setAttribute("aria-checked", String(!!s.writeback));
  if (s.writeback != null && hist.writeback != null) { hist.writeback = !!s.writeback; $("histSync").setAttribute("aria-checked", String(hist.writeback)); }
  $("autoTvSw").setAttribute("aria-checked", String(!!s.autotv));
  $("writebackRow").hidden = !feedsOn() || !(data.bili && data.bili.login && !data.bili.invalid);
}
async function setFeedSetting(key, sw) {
  const on = sw.getAttribute("aria-checked") !== "true";
  let answer;
  try { answer = await call("POST", "api/feeds/settings", { [key]: on }); }
  catch (error) { if (error instanceof SignedOut) signedOut(); else toast("网络不通", true); return; }
  if (!answer.ok) { toast(answer.error, true); return; }
  if (feedsInfo) feedsInfo.settings = answer.settings;
  if (fw) fw.settings = answer.settings;
  if (hist.writeback != null) hist.writeback = !!answer.settings.writeback;
  applyFeedSettings();
  $("histSync").setAttribute("aria-checked", String(!!answer.settings.writeback));
  toast(key === "writeback" ? (on ? "已打开：在这里看的 B 站视频会记进你的 B 站历史" : "已关掉同步到 B 站历史")
        : (on ? "已打开：以后同步到新关注，会自动放到小电视（放满为止）" : "已关掉自动放到小电视"));
}
$("writebackSw").onclick = (event) => setFeedSetting("writeback", event.currentTarget);
$("autoTvSw").onclick = (event) => setFeedSetting("autotv", event.currentTarget);

function cardFeed(x) {
  const id = reg(x);
  const cr = x.creator || {};
  const face = cr.face ? `<img src="${esc(cr.face)}" alt="" loading="lazy">` : avatarSVG(cr.name || x.subtitle || "?", 20);
  const p = x.progress;
  const pct = p && p.duration ? (p.done ? 1 : p.position / p.duration) : 0;
  let badges = x.duration ? `<span class="badge r b num">${clock(x.duration)}</span>` : "";
  if (p && p.done) badges += '<span class="badge">看完了</span>';
  else if (x.reason) badges += `<span class="badge">${esc(x.reason)}</span>`;
  if (x.pc_only) badges += '<span class="badge r">电脑上看</span>';
  const meta = [cr.name || x.subtitle || ""];
  if (x.id.startsWith("updates:")) meta.push(siteTag(x.source));
  if (p) meta.push(p.done ? "" : p.position ? "看到 " + clock(p.position) : "", x.watched_at ? ago(x.watched_at) : "");
  else if (x.pubdate) meta.push(ago(x.pubdate));
  const resume = x.play && x.play.start ? "，从 " + clock(x.play.start) + " 接着看" : "";
  return `<button class="card" data-play="${esc(id)}" data-k="f-${esc(id)}" aria-label="播放 ${esc(x.title)}${resume}"><span class="art">${itemArt(x, "w")}${badges}${pbar(pct)}</span>
    <span class="t">${esc(x.title)}</span><span class="who">${face}<span class="s">${esc(meta.filter(Boolean).join(" · "))}</span></span></button>`;
}
const skelCards = (n) => Array.from({ length: n || 4 }, () => '<span class="card skel" aria-hidden="true"><span class="art"></span><span class="t"></span><span class="s"></span></span>').join("");
function noteCard(icon, title, text, href) {
  const tag = href ? `a class="card soon note" href="${href}"` : 'div class="card soon note"';
  return `<${tag}><span class="art"><span>${ic(icon, 30)}${esc(title)}</span></span><span class="t">${esc(title)}</span><span class="s2 wrap">${esc(text)}</span></${href ? "a" : "div"}>`;
}
// 首页上自己账号的几排（在「直播 · 正在播」后面）。没登录：每个网站一张小卡片，点了去设置。
function feedShelves() {
  if (!feedsOn() || !feedsInfo) return "";
  let o = "";
  const asked = {};
  const login = (need, message) => {
    if (asked[need]) return "";
    asked[need] = true;
    return need === "bili"
      ? shelf("你的 B 站", "", noteCard("up", "登录 B 站", message || "登录 B 站后，这里会显示你的推荐和观看记录", "#/settings/bili"))
      : shelf("你的 YouTube", "", noteCard("search", "导入 YouTube cookies", message || "导入 YouTube cookies 后，这里会显示你的推荐", "#/settings/youtube"));
  };
  for (const r of feedsInfo.rows) {
    if (r.state === "hidden") continue;
    if (r.state === "login") { o += login(r.need, r.message); continue; }
    const a = feedRows[r.id];
    if (!a || (!a.items.length && a.state === "loading")) { o += shelf(r.title, "正在加载…", skelCards(4)); continue; }
    if (a.state === "hidden") continue;
    if (a.state === "login") { o += login(a.need || (r.id.startsWith("yt") ? "youtube" : "bili"), a.message); continue; }
    if (!a.items.length) {
      o += shelf(r.title, "", a.state === "blocked" || a.state === "error" ? noteCard("warn", "暂时取不到", a.message)
                 : noteCard("info", r.id.endsWith("history") ? "还没有观看记录" : "暂时没有内容",
                            r.id === "updates" ? "关注的 UP 主发了新视频会出现在这里" : "过一会儿再来看看"));
      continue;
    }
    const sub = a.loading ? "正在更新…" : a.message ? a.message : FEED_SUB[r.id] || "";
    o += shelf(r.title, sub, a.items.map(cardFeed).join(""), r.id === "updates" ? "#/up" : "", r.id === "updates" ? "我的关注" : "");
  }
  return o;
}

// ---------- UP 主 → 我的关注（GET api/follows；POST api/follows/sync、api/follows/tv） ----------

let fw = null, fwTimer = 0, fwShow = 60, fwQuery = "";
let fwFilter = lsGet("rootv.fwFilter", "all");
async function loadFollows(fresh) {
  if (!feedsOn()) { $("followsBox").hidden = true; return; }
  let answer;
  try { answer = await call("GET", "api/follows" + (fresh ? "?fresh=1" : "")); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  if (!answer || !answer.ok) return;
  fw = answer;
  renderFollows();
  pollFollows();
}
function pollFollows() {
  clearTimeout(fwTimer);
  if (fw && fw.syncing) fwTimer = setTimeout(() => { if (route === "up" && !document.hidden) loadFollows(); }, 2500);
}
const fwKinds = [["all", "全部"], ["bilibili", "B 站"], ["youtube", "YouTube"], ["tv", "在小电视上"], ["gone", "已取关"]];
function fwMatch(f, kind) {
  return kind === "all" || (kind === "tv" ? !!f.tv : kind === "gone" ? f.unfollowed : f.site === kind && !f.unfollowed);
}
function fwSorted() {
  // 最近有新视频的在前；没记下新视频的按关注的先后。
  return fw.items.map((f, i) => [f, i]).sort((a, b) => ((b[0].latest && b[0].latest.at) || 0) - ((a[0].latest && a[0].latest.at) || 0) || a[1] - b[1]).map((x) => x[0]);
}
function renderFollows() {
  const on = feedsOn();
  $("followsBox").hidden = !on;
  if (!on) return;
  if (!fw) { $("fwList").innerHTML = '<p class="hint">正在读取你关注的 UP 主…</p>'; return; }
  const fk = focusKey();
  const s = fw.sites || {}, count = (site) => fw.items.filter((f) => f.site === site && !f.unfollowed).length;
  const parts = [];
  if (s.bilibili && s.bilibili.state !== "off") parts.push("B 站 " + count("bilibili"));
  if (s.youtube && !["hidden", "off"].includes(s.youtube.state)) parts.push("YouTube " + count("youtube"));
  parts.push(fw.syncing ? "正在同步…" : fw.synced_at ? "上次同步 " + ago(fw.synced_at) : "还没同步过");
  $("fwSum").textContent = parts.join(" · ");
  $("fwSync").disabled = !!fw.syncing;
  const notes = [];
  if (fw.message) notes.push(fw.message + "（「设置 → B 站账号」或「设置 → YouTube」）");
  if (s.bilibili && s.bilibili.state === "login") notes.push("B 站：" + (s.bilibili.error || "登录已失效，请重新扫码"));
  else if (s.bilibili && s.bilibili.state === "error") notes.push("B 站这次没同步上：" + s.bilibili.error + "（先用上次的列表）");
  if (s.youtube && s.youtube.state === "login") notes.push("YouTube：" + s.youtube.error);
  else if (s.youtube && s.youtube.state === "error") notes.push("YouTube 这次没同步上：" + s.youtube.error);
  else if (s.youtube && s.youtube.state === "off" && s.youtube.error && s.bilibili && s.bilibili.state !== "off") notes.push("YouTube 订阅：" + s.youtube.error);
  if (!fw.message) notes.push("自动同步：登录时一次，之后每 6 小时一次；新关注的会出现，取关的会消失。小电视上取关了的 UP 主台不会悄悄删掉，会标「已取关」。");
  $("fwNote").textContent = notes.join(" ");
  const tv = fw.tv || {};
  $("fwCap").textContent = "小电视内存小，最多 " + (tv.max || 12) + " 个 UP 主台（现在 " + (tv.count || 0) + " / " + (tv.max || 12) + "）" +
    (tv.room && tv.room.fits === false ? "；频道列表已经满了" : "") + "；放满了要先关掉一个，关掉的随时可以再放";
  $("autoTvSw").setAttribute("aria-checked", String(!!(fw.settings && fw.settings.autotv)));
  const kinds = fwKinds.filter(([k]) => k === "all" || fw.items.some((f) => fwMatch(f, k)));
  if (!kinds.some(([k]) => k === fwFilter)) fwFilter = "all";
  $("fwChips").innerHTML = kinds.map(([k, t]) => `<button data-fwk="${k}" aria-pressed="${k === fwFilter}">${t} <em>${fw.items.filter((f) => fwMatch(f, k)).length}</em></button>`).join("");
  const q = fwQuery.toLowerCase();
  const list = fwSorted().filter((f) => fwMatch(f, fwFilter) && (!q || f.name.toLowerCase().includes(q) || (f.sign || "").toLowerCase().includes(q)));
  if (!fw.items.length) {
    $("fwList").innerHTML = `<div class="empty" style="grid-column:1/-1;padding:40px 24px"><div class="eic">${ic("up", 36)}</div><b>${fw.syncing ? "正在同步你关注的 UP 主…" : "还没有关注的 UP 主"}</b><p>${esc(fw.message || "在 B 站关注的 UP 主（和 YouTube 订阅的频道）会自动列在这里，不用一个个添加。")}</p>${fw.message ? '<a class="btn pri sm" href="#/settings/bili">去登录</a>' : ""}</div>`;
  } else if (!list.length) {
    $("fwList").innerHTML = '<p class="hint" style="grid-column:1/-1">没有找到</p>';
  } else {
    $("fwList").innerHTML = list.slice(0, fwShow).map(fwTile).join("");
  }
  $("fwMore").hidden = list.length <= fwShow;
  $("fwMore").textContent = "显示更多（还有 " + Math.max(0, list.length - fwShow) + " 个）";
  restoreFocus(fk);
}
function fwTile(f) {
  const face = `<span class="fwface">${avatarSVG(f.name || "?", 48)}${f.face ? `<img src="${esc(f.face)}" alt="" loading="lazy">` : ""}</span>`;
  const latest = f.latest && f.latest.title ? "最新：" + f.latest.title + (f.latest.at ? " · " + ago(f.latest.at) : "") : f.sign || (f.handle || "");
  const tag = f.unfollowed ? '<span class="fwtag gone">已取关</span>' : `<span class="fwtag ${f.site === "youtube" ? "yt" : ""}">${siteTag(f.site)}</span>`;
  return `<div class="fw${f.tv ? " on" : ""}${f.unfollowed ? " gone" : ""}"><button class="fwmain" data-fw="${esc(f.key)}" data-k="fw-${esc(f.key)}" aria-label="看「${esc(f.name)}」的投稿">${face}<span class="fwtx"><b>${esc(f.name)}</b><small>${tag}${esc(latest)}</small></span></button>
    <span class="fwtv"><small>${f.tv ? "在小电视上" : "放到小电视"}</small><button class="sw" role="switch" aria-checked="${!!f.tv}" data-fwtv="${esc(f.key)}" data-k="fwtv-${esc(f.key)}" aria-label="放到小电视：${esc(f.name)}"></button></span></div>`;
}
async function setFollowTv(key, on) {
  const f = fw && fw.items.find((x) => x.key === key);
  if (!f) return;
  if (!on && !confirm("把「" + f.name + "」从小电视上拿下来？这个台看到哪里会忘掉；以后可以再放。")) return;
  let answer;
  try { answer = await call("POST", "api/follows/tv", { key, on }); }
  catch (error) { if (error instanceof SignedOut) signedOut(); else toast("网络不通", true); return; }
  if (!answer.ok) { toast(answer.error, true); return; }
  toast(on ? "「" + f.name + "」放到小电视上了" : "「" + f.name + "」从小电视上拿下来了");
  await loadFollows();
  refresh();
  loadHome(true);
  if (cs && cs.key === key) renderCreatorActs();
}
$("fwList").addEventListener("click", (event) => {
  const t = event.target.closest("button");
  if (!t) return;
  if (t.dataset.fwtv) { setFollowTv(t.dataset.fwtv, t.getAttribute("aria-checked") !== "true"); return; }
  if (t.dataset.fw) openCreator(t.dataset.fw);
});
$("fwChips").addEventListener("click", (event) => {
  const t = event.target.closest("[data-fwk]");
  if (!t) return;
  fwFilter = t.dataset.fwk;
  lsSet("rootv.fwFilter", fwFilter);
  fwShow = 60;
  renderFollows();
});
$("fwQ").addEventListener("input", () => { fwQuery = $("fwQ").value.trim(); fwShow = 60; renderFollows(); });
$("fwMore").onclick = () => { fwShow += 120; renderFollows(); };
$("fwSync").onclick = () => busy($("fwSync"), async () => {
  const answer = await call("POST", "api/follows/sync", {});
  if (!answer.ok) { toast(answer.error, true); return; }
  fw = answer;
  toast(answer.note || "正在同步你关注的 UP 主…");
  renderFollows();
  pollFollows();
});

// 一个关注的 UP 主：最新的投稿，点一个播放（播放到记住的地方），上面是「放到小电视」。
let cs = null;
function renderCreatorActs() {
  const f = fw && cs && fw.items.find((x) => x.key === cs.key);
  if (!f) return;
  const b = button(f.tv ? "从小电视上拿下来" : "放到小电视", f.tv ? "ghost" : "go", () => setFollowTv(f.key, !f.tv));
  $("csActs").replaceChildren(b);
  $("csSite").textContent = siteTag(f.site) + (f.unfollowed ? " · 已取关" : "") + (f.tv ? " · 小电视上的台" : "");
}
function openCreator(key) {
  const f = fw && fw.items.find((x) => x.key === key);
  if (!f) return;
  cs = { key, run: (cs ? cs.run : 0) + 1 };
  $("csName").textContent = f.name;
  $("csSum").textContent = "正在取最新的投稿…";
  $("csList").innerHTML = '<div class="wgrid">' + skelCards(4) + "</div>";
  renderCreatorActs();
  openSheet($("csheet"));
  pollCreator(cs.run, 0);
}
async function pollCreator(run, n) {
  let answer;
  try { answer = await call("GET", "api/follows/videos?key=" + encodeURIComponent(cs.key)); }
  catch (error) { if (error instanceof SignedOut) signedOut(); else if (cs && cs.run === run) $("csSum").textContent = "网络不通"; return; }
  if (!cs || cs.run !== run) return;
  if (!answer.ok) { $("csSum").textContent = answer.error; $("csList").replaceChildren(); return; }
  if (answer.items.length) {
    const to = currentTarget();
    $("csSum").textContent = "最新 " + answer.items.length + " 个投稿 · 点一下播放" + (to ? (to === PC ? "（在这台电脑上）" : "到「" + targetName(to) + "」") : "");
    $("csList").innerHTML = '<div class="wgrid">' + answer.items.map(cardFeed).join("") + "</div>";
  } else if (!answer.loading) {
    $("csSum").textContent = answer.error || "还没有投稿";
    $("csList").replaceChildren();
  }
  if (answer.loading && n < 60) setTimeout(() => pollCreator(run, n + 1), 2000);
}

// ======================================================================= 片库：历史、本地片库
// 「历史」是自己 B 站的观看记录（GET api/feeds/history，accountfeeds.py 的 history_page）：每次打开都要新的
// （fresh=1；服务器至少隔 30 秒才真去问 B 站），「加载更多」按 B 站给的游标往下翻。每一行是首页那样的节目
// （data-play），点了按「播放到」接着看：视频是那一 P，剧集、番剧是那一集，都从看到的地方起。
const LIB_TABS = [["history", "历史", "hist"], ["local", "本地片库", "lib"]];
const HIST_KINDS = [["all", "全部"], ["video", "视频"], ["pgc", "剧集·番剧"], ["yt", "YouTube"]];
const hist = { items: [], more: null, state: "", message: "", busy: false, at: 0, writeback: null, kind: "all", ytAsked: false };
let libTab = "", libNavHTML = "", histTimer = 0;
function libNav() {
  if (!me) return;
  const want = route === "lib" ? routeParts()[1] || "" : "";
  const cur = LIB_TABS.some(([id]) => id === want) ? want : "history";
  const html = LIB_TABS.map(([id, t, i]) => `<a href="#/lib/${id}" data-k="lib-${id}"${id === cur ? ' aria-current="page"' : ""}>${ic(i, 16)}${t}</a>`).join("");
  if (html !== libNavHTML) {
    const k = focusKey();
    $("libNav").innerHTML = libNavHTML = html;
    restoreFocus(k);
  }
  for (const pane of qsa("#app .lpane")) pane.hidden = pane.dataset.pane !== cur;
  libTab = cur;
  if (cur === "history") { renderHist(); loadHist(false); }
}
// YouTube 观看记录（管理员、YouTube 开着）：就是首页那一排（feedRows.yt_history），在这里多一个标签。
const histYt = () => !!(feedsInfo && feedsInfo.rows.some((r) => r.id === "yt_history" && r.state !== "hidden"));
async function loadHist(more, again) {
  if (!data) return;               // /api/state 还没到：到了以后 render() 再叫
  if (!feedsOn()) { hist.state = "off"; renderHist(); return; }
  if (hist.busy || (!more && !again && Date.now() - hist.at < 5000)) return;
  hist.busy = true;
  renderHistMore();
  const c = hist.more;
  const q = more && c ? "?max=" + c.max + "&view_at=" + c.view_at + (c.business ? "&business=" + encodeURIComponent(c.business) : "") : "?fresh=1";
  let answer;
  try { answer = await call("GET", "api/feeds/history" + q); }
  catch (error) {
    hist.busy = false;
    if (error instanceof SignedOut) signedOut(); else { toast("网络不通", true); renderHist(); }
    return;
  }
  hist.busy = false;
  if (!answer.ok) { toast(answer.error, true); renderHist(); return; }
  if (!more) hist.at = Date.now();
  hist.state = answer.state;
  hist.message = answer.message || "";
  hist.writeback = answer.writeback;
  if (more) {
    const have = new Set(hist.items.map((x) => x.id));
    hist.items = hist.items.concat(answer.items.filter((x) => !have.has(x.id)));
  } else if (answer.items.length || answer.state !== "loading") hist.items = answer.items;
  hist.more = answer.more;
  // 首页那一排正好在取（后台线程）：等一下再问。
  clearTimeout(histTimer);
  if (answer.state === "loading") histTimer = setTimeout(() => { if (route === "lib" && libTab === "history") loadHist(false, true); }, 1500);
  renderHist();
}
// 在这台电脑上看完、关掉窗口以后：中转已经报给 B 站（开着同步时），过一会儿重新取一次。
function histAfterPlay() {
  if (route !== "lib" || libTab !== "history") return;
  clearTimeout(histTimer);
  histTimer = setTimeout(() => loadHist(false, true), 3000);
}
function histGroup(at) {
  const t = new Date(), start = new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime() / 1000;
  return at >= start ? "今天" : at >= start - 86400 ? "昨天" : "更早";
}
function histRow(x, group) {
  const id = reg(x);
  const h = x.hist || { head: x.title, ep: x.subtitle || "" };
  const p = x.progress || {};
  const pct = p.duration ? (p.done ? 1 : p.position / p.duration) : 0;
  const at = x.watched_at || 0, d = new Date(at * 1000);
  const time = !at ? "" : group !== "更早" ? hm(at)
    : (d.getFullYear() === new Date().getFullYear() ? "" : d.getFullYear() + "年") + (d.getMonth() + 1) + "月" + d.getDate() + "日";
  const how = p.done ? "看完了" : p.position ? "看到 " + clock(p.position) + (p.duration ? " / " + clock(p.duration) : "") : "";
  const resume = x.play && x.play.start ? "，从 " + clock(x.play.start) + " 接着看" : "";
  return `<button class="hrow" data-play="${esc(id)}" data-k="h-${esc(id)}" aria-label="播放 ${esc(x.title)}${resume}"><span class="art">${itemArt(x, "w")}${pbar(pct)}</span>
    <span class="htx"><b>${esc(h.head)}</b>${h.ep ? `<small>${esc(h.ep)}</small>` : ""}<small class="hm">${esc([how, time].filter(Boolean).join(" · "))}</small></span></button>`;
}
function renderHistMore() {
  const btn = $("histMore");
  btn.hidden = hist.kind === "yt" || !hist.more || hist.state === "login";
  btn.disabled = hist.busy;
  btn.textContent = hist.busy && hist.items.length ? "正在加载…" : "加载更多";
}
function renderHist() {
  if (route !== "lib" || libTab !== "history") return;
  const kinds = HIST_KINDS.filter(([id]) => id !== "yt" || histYt());
  if (!kinds.some(([id]) => id === hist.kind)) hist.kind = "all";
  const login = hist.state === "login" || hist.state === "off";
  $("histChips").hidden = login;
  $("histChips").innerHTML = kinds.map(([id, t]) => `<button data-hk="${id}" aria-pressed="${id === hist.kind}">${t}</button>`).join("");
  $("histSyncRow").hidden = login || hist.writeback == null;
  $("histSync").setAttribute("aria-checked", String(!!hist.writeback));
  renderHistMore();
  const box = $("histBox");
  if (hist.state === "off") { box.innerHTML = '<div class="empty sm"><b>这里没有观看记录</b><p>电脑客户端和家庭服务器才有</p></div>'; return; }
  if (hist.state === "login") {
    box.innerHTML = `<div class="empty sm"><b>登录 B 站后，这里是你的观看记录</b><a class="btn pri sm" href="#/settings/bili">去登录</a></div>`;
    return;
  }
  let items = hist.items, note = hist.message;
  if (hist.kind === "yt") {
    const row = feedRows.yt_history;
    items = row ? row.items : [];
    note = row ? row.message : "";
    // 还没问过就问一次；还在取（yt-dlp 要十几秒）每 3 秒再问。
    if ((!row && !hist.ytAsked) || (row && feedWaiting("yt_history"))) {
      hist.ytAsked = true;
      clearTimeout(histTimer);
      histTimer = setTimeout(async () => { if (route === "lib" && hist.kind === "yt") { await loadFeedRow("yt_history"); renderHist(); } }, row ? 3000 : 0);
    }
  } else if (hist.kind !== "all") items = items.filter((x) => (x.hist && x.hist.type) === hist.kind);
  const k = focusKey();
  if (!items.length) {
    const waiting = hist.kind === "yt" ? !feedRows.yt_history || feedWaiting("yt_history")
      : hist.busy || hist.state === "loading" || !hist.at;
    box.innerHTML = `<p class="hint">${esc(waiting ? "正在加载…" : note || (hist.more ? "这一页里没有，点「加载更多」" : "还没有观看记录"))}</p>`;
    restoreFocus(k);
    return;
  }
  let o = note ? `<p class="hint">${esc(note)}</p>` : "", last = null;
  for (const x of items) {
    const g = histGroup(x.watched_at);
    if (g !== last) { o += (last === null ? "" : "</div>") + `<h3 class="hgrp">${g}</h3><div class="hlist">`; last = g; }
    o += histRow(x, g);
  }
  box.innerHTML = o + "</div>";
  restoreFocus(k);
}
$("histChips").addEventListener("click", (event) => {
  const b = event.target.closest("[data-hk]");
  if (!b || b.dataset.hk === hist.kind) return;
  hist.kind = b.dataset.hk;
  renderHist();
});
$("histMore").onclick = () => loadHist(true);
$("histSync").onclick = (event) => setFeedSetting("writeback", event.currentTarget);

// ======================================================================= 首页（/api/home）

// 页面上的节目：data-play / data-pick 指到这里（id → {title, subtitle, play}）。
const ITEMS = {};
function reg(item) { ITEMS[item.id] = item; return item.id; }
const pbar = (p) => p > 0 ? `<span class="pbar"><i style="width:${(Math.min(1, p) * 100).toFixed(1)}%"></i></span>` : "";
function splitHTML(id, label, small) {
  const to = currentTarget();
  return `<span class="split"><button class="btn pri" data-play="${esc(id)}">${ic("play", 17)}${esc(label)}${small ? ` <small>${esc(small)}</small>` : ""}</button><button class="btn pri caret" data-pick="${esc(id)}" aria-label="选择播放到哪里（现在是 ${esc(to ? targetName(to) : "还没选")}）">${ic("down", 17)}</button></span>`;
}
function itemArt(x, ratio, opt) {
  const r = ratio || (x.art && x.art.ratio === "poster" ? "p" : "w");
  return art(x.title || "?", r, opt || {}) + imgTag(x.art && x.art.url);
}
function left(p) {
  if (!p || !p.duration) return "";
  const m = Math.max(1, Math.round((p.duration - p.position) / 60));
  return "还剩 " + m + " 分钟";
}
function cardContinue(x) {
  const id = reg(x);
  const p = x.progress || {};
  return `<button class="card" data-play="${esc(id)}" data-k="c-${esc(id)}" aria-label="继续播放 ${esc(x.title)}"><span class="art">${itemArt(x, "w")}${pbar(p.duration ? p.position / p.duration : 0)}</span>
    <span class="t">${esc(x.title)}</span><span class="s">${esc([x.subtitle, left(p), p.device].filter(Boolean).join(" · "))}</span></button>`;
}
function cardLive(x) {
  const id = reg(x);
  const c = x.channel || {};
  const l = x.live;
  const now = Date.now() / 1000;
  const pct = l && l.start && l.end ? (now - l.start) / (l.end - l.start) : 0;
  const logo = `<span class="logo-on ${c.logo ? "img" : ""}">${badgeSVG(c.name || x.title)}${c.logo ? `<img src="${esc(c.logo)}" alt="" loading="lazy" style="position:absolute;inset:4px;margin:auto">` : ""}</span>`;
  return `<button class="card" data-play="${esc(id)}" data-k="l-${esc(id)}" aria-label="播放 ${esc(c.name || x.title)}${l && l.title ? "，正在播 " + esc(l.title) : ""}"><span class="art">${art(x.title, "w", { motif: 2 })}<span class="badge live">直播</span>${x.on_tv ? `<span class="badge r">${esc(deviceName())}在播</span>` : ""}${logo}${pbar(pct)}</span>
    <span class="t">${esc(l && l.title ? l.title : c.name || x.title)}</span><span class="s">${esc(c.name || "")}${l && l.start ? " · " + hm(l.start) + "–" + hm(l.end) : ""}</span>${l && l.next_title ? `<span class="s2">接下来 ${hm(l.next_start)} ${esc(l.next_title)}</span>` : ""}</button>`;
}
function cardVideo(x) {
  const id = reg(x);
  const cr = x.creator || {};
  const face = cr.face ? `<img src="${esc(cr.face)}" alt="" loading="lazy">` : avatarSVG(cr.name || "?", 20);
  return `<button class="card" data-play="${esc(id)}" data-k="v-${esc(id)}" aria-label="播放 ${esc(x.title)}"><span class="art">${itemArt(x, "w")}${x.duration ? `<span class="badge r b num">${clock(x.duration)}</span>` : ""}${x.new ? '<span class="badge new">新</span>' : ""}</span>
    <span class="t">${esc(x.title)}</span><span class="who">${face}<span class="s">${esc(cr.name || x.subtitle || "")}${x.pubdate ? " · " + ago(x.pubdate) : ""}</span></span></button>`;
}
function cardSeries(x) {
  const id = reg(x);
  const s = x.series || {};
  const badge = s.done ? '<span class="badge">看完了</span>' : s.fresh ? '<span class="badge new">新加</span>' : s.watched ? `<span class="badge new">第 ${s.episode} 集</span>` : "";
  return `<button class="card p" data-series="${esc(s.id)}" data-k="s-${esc(id)}" aria-label="${esc(x.title)}，选集"><span class="art">${art(x.title, "p", { title: x.title, kick: siteTag(x.source) })}${imgTag(x.art && x.art.url)}${badge}${s.watched && !s.done && s.total ? pbar(s.watched / s.total) : ""}</span>
    <span class="t">${esc(x.title)}</span><span class="s">${esc(x.subtitle || "")}</span></button>`;
}
const shelf = (title, sub, inner, more, moreT) => `<section aria-label="${esc(title)}"><div class="h2"><h2>${esc(title)}</h2>${sub ? `<small>${esc(sub)}</small>` : ""}${more ? `<a class="more" href="${more}">${esc(moreT || "全部")} ›</a>` : ""}</div><div class="shelf">${inner}</div></section>`;
const banner = (kind, t, p, btn, href) => `<div class="banner ${kind}" role="note"><span class="ico">${ic(kind === "warn" ? "warn" : "info", 22)}</span><div><b>${esc(t)}</b><p>${esc(p)}</p></div>${btn ? `<a class="btn sm" href="${href}">${esc(btn)}</a>` : ""}</div>`;

function heroHTML(list) {
  if (!list.length) return "";
  heroIndex = heroIndex % list.length;
  const h = list[heroIndex];
  const id = reg(h);
  const live = h.kind === "live";
  const c = h.channel || {};
  const title = live ? (h.live && h.live.title ? h.live.title : c.name || h.title) : h.title;
  const meta = [];
  if (live) {
    meta.push(c.name || "");
    if (h.live && h.live.start) meta.push(hm(h.live.start) + "–" + hm(h.live.end), "还有 " + Math.max(1, Math.round((h.live.end - Date.now() / 1000) / 60)) + " 分钟");
  } else if (h.kind === "series") {
    meta.push(siteTag(h.source), h.subtitle || "");
  } else {
    meta.push((h.creator && h.creator.name) || h.subtitle || "", siteTag(h.source));
    if (h.duration) meta.push(clock(h.duration));
  }
  const syn = live ? (h.live && h.live.next_title ? "接下来 " + hm(h.live.next_start) + " 《" + h.live.next_title + "》" : "")
    : h.kind === "series" ? "每部剧是一个台，按顺序播，换台回来接着上次的地方。" : "";
  const L = [...title].length;
  const second = h.kind === "series" ? `<button class="btn" data-series="${esc(h.series.id)}">选集</button>`
    : live ? `<a class="btn" href="#/live">全部直播</a>` : `<a class="btn" href="#/up">UP 主</a>`;
  const bg = (h.art && h.art.url ? imgTag(h.art.url) : "") || "";
  return `<section class="hero dk" aria-label="推荐"><div class="bg">${art(title, "w", { motif: live ? 2 : h.kind === "series" ? 1 : 0 })}${bg}</div><div class="shade"></div>
    <div class="copy"><div class="kicker">${live ? '<span class="badge live">直播</span>' : ""}${esc(h.kicker || "")}</div>
      <div class="tart${L > 12 ? " xs" : L > 6 ? " sm" : ""}" style="--h:${hsh(title) % 360}" role="heading" aria-level="2">${esc(title)}</div>
      <div class="meta">${meta.filter(Boolean).map(esc).join('<span aria-hidden="true">·</span>')}</div>${syn ? `<p class="syn">${esc(syn)}</p>` : ""}
      <div class="actions">${splitHTML(id, h.button || "播放", currentTarget() ? (currentTarget() === PC ? "在电脑上" : "到" + targetName(currentTarget())) : "")}${second}</div></div>
    ${list.length > 1 ? `<div class="hnav"><button class="iconbtn" data-hero="-1" aria-label="上一个">${ic("back", 18)}</button>${list.map((_, i) => `<button class="dotb" data-hero-i="${i}" aria-label="第 ${i + 1} 个" ${i === heroIndex ? 'aria-current="true"' : ""}></button>`).join("")}<button class="iconbtn" data-hero="1" aria-label="下一个">${ic("fwd", 18)}</button></div>` : ""}</section>`;
}

function renderHome() {
  const box = $("homeBody");
  if (!homeData) { box.innerHTML = `<div class="ph"><h1 class="lt">首页</h1></div><p class="hint">正在准备…</p>`; return; }
  const fk = focusKey();
  const scrolls = qsa("#homeBody .shelf").map((e) => e.scrollLeft);
  const H = homeData.hero || [];
  let o = heroHTML(H);
  o += H.length ? '<div class="after">' : `<header class="ph"><h1 class="lt">首页</h1></header><div>`;
  if (data && !hasTv() && !isGuest()) {
    o += data.watch ? banner("info", "没有小电视也能用" + BRAND_ZH, "点任何节目都会在这台电脑上播放。以后买了小电视，输入屏幕上的绑定码就能「播放到」它。", "绑定小电视", "#/devices")
      : banner("info", "还没有小电视", "绑定以后，点任何节目都能播到它上面；订阅、剧集现在就可以先加。", "绑定小电视", "#/devices");
  }
  if (data && data.youtube && data.youtube.state === "unreachable") o += banner("warn", "YouTube 暂时不可用", $("noticeText").textContent || "", "", "");
  if (!isGuest()) o += `<div class="chips qchips" aria-label="分类"><a href="#/series">${ic("series", 16)}剧集与番剧</a><a href="#/up">${ic("up", 16)}UP 主</a><a href="#/lib">${ic("lib", 16)}片库</a><a href="#/live">${ic("live", 16)}直播</a></div>`;
  if (homeData.continue.length) o += shelf("继续观看", "在哪台上看的都算；电脑上的进度另记", homeData.continue.map(cardContinue).join(""));
  if (homeData.live_now.length) o += shelf("直播 · 正在播", homeData.epg ? "节目信息来自节目单（EPG）" : "", homeData.live_now.map(cardLive).join(""), "#/live", "全部直播");
  o += feedShelves();
  if (!homeData.guest) {
    const none = !homeData.creator_updates.length && !homeData.series_updates.length && data && !data.creators.length && !(data.series || []).length;
    if (none) {
      o += `<div class="h2"><h2>开始收集你爱看的</h2></div><div class="onb">
        <a href="#/search"><b>${ic("up")}订阅一个 UP 主</b><p>B 站或 YouTube 的 UP 主，每个都变成一个台，新视频自动排上。</p></a>
        <a href="#/series"><b>${ic("series")}加一部剧</b><p>番剧、多集视频、合集、播放列表都行，按顺序播，接着上次的地方。</p></a>
        <a href="#/lib/local"><b>${ic("lib")}本地片库（即将推出）</b><p>电脑或 NAS 上的电影、剧集、动画和家庭视频。</p></a></div>`;
    } else {
      if (homeData.creator_updates.length) o += shelf("UP 主更新", homeData.creator_updates.length + " 个没看的视频", homeData.creator_updates.map(cardVideo).join(""), "#/up");
      if (homeData.series_updates.length) o += shelf("剧集追更", "", homeData.series_updates.map(cardSeries).join(""), "#/series");
    }
    o += shelf("我的片库", "", `<a class="card soon" href="#/lib/local"><span class="art"><span>${ic("lib", 30)}本地片库即将推出</span></span><span class="t">本地片库</span><span class="s">电脑或 NAS 上的视频，以后在这里</span></a>`);
  }
  box.innerHTML = o + "</div>";
  qsa("#homeBody .shelf").forEach((e, i) => { if (scrolls[i] !== undefined) e.scrollLeft = scrolls[i]; });
  restoreFocus(fk);
  ytPrefetchView();
}

// ---- YouTube 预取（server/relay/ytvod.py）：看得见的前几个、鼠标停住或焦点到的那个，先让中转取好地址 ----
// 只在本机模式（data.youtube.kind === "local"）、这台电脑能看时发；中转说 off 就不再发。中转那边一次一个、
// 不耽误真正的播放；同一个视频 10 分钟内不再问。
const YT_PREFETCH = { FIRST: 3, HOVER_MS: 250, AGAIN_MS: 600000 };
let ytPrefetchOff = false, ytHoverTimer = 0;
const ytAsked = {};
function ytPrefetchOk() {
  return !ytPrefetchOff && VOD_OK && !!(data && data.watch && data.youtube && data.youtube.kind === "local") && !!me && !isGuest();
}
// 首页的卡片（ITEMS）是哪个 YouTube 视频：节目里写着，或者封面是 i.ytimg.com/vi/<id>/。
function ytIdOfCard(x) {
  if (!x) return "";
  const id = x.play ? ytIdOfItem(x.play) : "";
  if (id) return id;
  let url = (x.art && x.art.url) || "";
  try { url = decodeURIComponent(url); } catch (error) { /* 原样 */ }
  const m = url.match(/i\.ytimg\.com\/vi\/([A-Za-z0-9_-]{11})\//);
  return m ? m[1] : "";
}
function ytIdAt(node) {
  const card = node && node.closest && node.closest("[data-play], [data-ytid]");
  if (!card) return "";
  return card.dataset.ytid || ytIdOfCard(ITEMS[card.dataset.play]);
}
function ytPrefetch(ids) {
  if (!ytPrefetchOk()) return;
  const now = Date.now();
  const want = [...new Set(ids)].filter((id) => id && !(now - (ytAsked[id] || 0) < YT_PREFETCH.AGAIN_MS)).slice(0, 6);
  if (!want.length) return;
  for (const id of want) ytAsked[id] = now;
  call("POST", "api/yt/prefetch", { ids: want, q: ytQualityPref() })
    .then((answer) => { if (answer && answer.off) ytPrefetchOff = true; }).catch(() => {});
}
function ytPrefetchVisible(root) {
  if (!ytPrefetchOk() || !root) return;
  const h = window.innerHeight, w = window.innerWidth, ids = [];
  for (const node of root.querySelectorAll("[data-play], [data-ytid]")) {
    const r = node.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= h || r.right <= 0 || r.left >= w || !r.width) continue;
    const id = ytIdAt(node);
    if (id && !ids.includes(id)) ids.push(id);
    if (ids.length >= YT_PREFETCH.FIRST) break;
  }
  ytPrefetch(ids);
}
// 页面画好、滚动停下以后（横排的卡片也算）再看一眼哪些看得见。
let ytViewTimer = 0;
function ytPrefetchView() {
  clearTimeout(ytViewTimer);
  ytViewTimer = setTimeout(() => ytPrefetchVisible($("view")), 400);
}
addEventListener("scroll", () => { if (ytPrefetchOk()) ytPrefetchView(); }, { capture: true, passive: true });
function ytPrefetchSoon(event) {
  const id = ytIdAt(event.target);
  if (!id) return;
  clearTimeout(ytHoverTimer);
  ytHoverTimer = setTimeout(() => ytPrefetch([id]), YT_PREFETCH.HOVER_MS);
}
document.addEventListener("mouseover", ytPrefetchSoon);
document.addEventListener("focusin", ytPrefetchSoon);

// ======================================================================= 点击（委托）

document.addEventListener("click", (event) => {
  const t = event.target.closest("button, a");
  if (!t) return;
  const ds = t.dataset;
  if (ds.close !== undefined) { closeSheets(); return; }
  if (ds.play) {
    event.preventDefault();
    const x = ITEMS[ds.play];
    if (t.closest("#csheet")) closeSheets(true);
    if (x) pressed(t, play(x.play, null, x.title, x.art && x.art.url));
    return;
  }
  if (ds.pick) { const x = ITEMS[ds.pick]; if (x) openTarget(x.play, "", x.title, x.art && x.art.url); return; }
  if (ds.series) { openEpisodes(ds.series); return; }
  if (ds.sheet === "target") { openTarget(null); return; }
  if (ds.tg) {
    if (t.getAttribute("aria-disabled") === "true") { toast(targetName(ds.tg) + "离线了，看看电源和 Wi-Fi"); return; }
    if (pendingItem) {
      const item = pendingItem, label = pendingLabel, poster = pendingPoster;
      if (remember) setTarget(ds.tg, true);
      closeSheets(true);
      play(item, ds.tg, label, poster);
    } else { setTarget(ds.tg); closeSheets(); if (route === "home") renderHome(); }
    return;
  }
  if (ds.remember !== undefined) { setRemember(!remember); return; }
  if (ds.rdev) { pick(ds.rdev); return; }
  if (ds.hero) { const n = (homeData && homeData.hero.length) || 1; heroIndex = (heroIndex + Number(ds.hero) + n) % n; renderHome(); return; }
  if (ds.heroI !== undefined) { heroIndex = Number(ds.heroI); renderHome(); return; }
});

// ======================================================================= 键盘：方向键移动焦点、回车、Esc 返回、/ 搜索

const FOC = 'a[href], button:not([disabled]), input:not([type=hidden]):not([disabled]), select, textarea, [tabindex="0"]';
const vis = (node) => node.offsetParent !== null || getComputedStyle(node).position === "fixed";
function overlayRoot() {
  if (!$("modal").hidden) return $("modal");
  if (!$("gpop").hidden) return $("gpop");
  for (const id of SHEETS) if (!$(id).hidden) return $(id);
  return null;
}
// 重画时记住焦点在哪（同一个 data-k / data-play / href …），画完放回去，像电视上那样按完回车焦点不丢。
const FKEYS = ["data-k", "data-mk", "data-play", "data-series", "data-hero-i", "data-tg", "href"];
function focusKey() {
  const a = document.activeElement;
  if (!a || a === document.body) return null;
  const zone = a.closest("#view, #top, #mini, #tabs");
  if (!zone) return null;
  for (const at of FKEYS) if (a.hasAttribute(at)) {
    const sel = `[${at}="${CSS.escape(a.getAttribute(at))}"]`;
    return { z: "#" + zone.id, sel, i: qsa(sel, zone).indexOf(a) };
  }
  return null;
}
function restoreFocus(k) {
  if (!k) return;
  const list = qsa(`${k.z} ${k.sel}`);
  const node = list[k.i] || list[0];
  if (node && node !== document.activeElement) node.focus({ preventScroll: true });
}
function goBack() {
  const bar = isM() ? "#tabs" : "#top .ttabs", a = document.activeElement;
  if (!(a && a.closest && a.closest(bar))) {
    const t = qs(`${bar} a[aria-current=page]`) || qs(`${bar} a`);
    if (t) { t.focus(); return; }
  }
  if (route !== "home") location.hash = "#/home";
}
document.addEventListener("keydown", (event) => {
  if ($("app").hidden) return;
  const tag = (event.target.tagName || "").toLowerCase(), typing = ["input", "select", "textarea"].includes(tag) && event.target.type !== "range" && event.target.type !== "checkbox" && event.target.type !== "radio";
  // Esc = 遥控器上的「返回」：关面板 → 退出输入框 → 焦点回到标签栏 → 回首页
  if (event.key === "Escape") {
    if (!$("gpop").hidden) { event.preventDefault(); closeGuidePop(true); return; }
    if (anyOverlay()) { event.preventDefault(); if (wifiDev) closeWifi(); closeSheets(); return; }
    if (typing) { event.target.blur(); return; }
    if (!$("pcwin").hidden && $("pcwin").classList.contains("big")) { $("watchBig").click(); return; }
    event.preventDefault();
    goBack();
    return;
  }
  if (typing) return;
  if (event.target.type === "range" && (event.key === "ArrowLeft" || event.key === "ArrowRight")) return;   // 滑块只吃左右键
  if (event.key === "/" && !anyOverlay()) {
    event.preventDefault();
    if (route !== "search") location.hash = "#/search";
    setTimeout(() => $("bsQuery").focus(), 0);
    return;
  }
  if (event.key === "Tab" && (anyOverlay() || !$("gpop").hidden)) {   // 面板里 Tab 不跑出去
    const f = qsa(FOC, overlayRoot()).filter(vis);
    if (!f.length) return;
    const i = f.indexOf(document.activeElement);
    if (event.shiftKey && i <= 0) { event.preventDefault(); f[f.length - 1].focus(); }
    else if (!event.shiftKey && i === f.length - 1) { event.preventDefault(); f[0].focus(); }
    return;
  }
  if (event.key.startsWith("Arrow") && !event.altKey && !event.metaKey && !event.ctrlKey) { if (spatial(event.key)) event.preventDefault(); }
});
// 方向键：在屏幕上找那个方向最近的可聚焦元素（电视遥控器式的焦点引擎）。
function spatial(key) {
  const root = overlayRoot() || $("app"), cur = document.activeElement;
  const all = qsa(FOC, root).filter(vis);
  if (!cur || cur === document.body || !root.contains(cur)) {
    const f = all.find((n) => n.closest("#view")) || all[0];
    if (f) { f.focus({ preventScroll: true }); keepVisible(f); return true; }
    return false;
  }
  const r = cur.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2, h = key === "ArrowLeft" || key === "ArrowRight", sg = key === "ArrowRight" || key === "ArrowDown" ? 1 : -1;
  // 先在同一块里找（内容区、顶栏、播放器、播放条、底栏、面板各是一块），找不到再跨块：走到边上才进标签栏。
  const zone = (n) => n.closest("#view, #top, #pcwin, #mini, #tabs, .sheet, #modal"), z = zone(cur);
  // 同一排货架里左右移动只在这一排里找，排尾不会跳到别的排。
  const row = h && cur.closest(".shelf, .chips, .rdevs, .seg, .segc, .petgrid, .grow");
  return (row && pick2(qsa(FOC, row).filter(vis))) || (!row && pick2(all.filter((n) => zone(n) === z))) || (!row && pick2(all)) || false;
  function pick2(list) {
    let best = null, bs2 = Infinity;
    for (const n of list) {
      if (n === cur || n.contains(cur)) continue;
      const b = n.getBoundingClientRect();
      if (!b.width || !b.height) continue;
      const bc = h ? b.left + b.width / 2 : b.top + b.height / 2, rc = h ? cx : cy;
      if ((bc - rc) * sg <= 1) continue;                       // 中心必须往那个方向走了
      const gap = Math.max(0, h ? (sg > 0 ? b.left - r.right : r.left - b.right) : (sg > 0 ? b.top - r.bottom : r.top - b.bottom));
      const overlap = h ? Math.min(b.bottom, r.bottom) - Math.max(b.top, r.top) : Math.min(b.right, r.right) - Math.max(b.left, r.left);
      const cross = overlap > 0 ? 0 : (h ? Math.abs(b.top + b.height / 2 - cy) : Math.abs(b.left + b.width / 2 - cx));
      const sc = gap + Math.abs(bc - rc) * .1 + cross * 2.5;
      if (sc < bs2) { bs2 = sc; best = n; }
    }
    if (best) { best.focus({ preventScroll: true }); keepVisible(best); return true; }
    return false;
  }
}
// 货架横向滚到焦点卡片完整可见，页面纵向滚到这一排不被顶栏和播放条挡住（scroll-margin 定的边距）。
function keepVisible(node) {
  if (node.closest("#top, #tabs, #mini")) return;
  node.scrollIntoView({ block: "nearest", inline: "nearest", behavior: RM.matches ? "auto" : "smooth" });
}
// 鼠标：卡片跟着指针轻微倾斜、反光点跟着走；指到一张卡片时把键盘焦点也挪过来，免得同时有两张浮起。
let tiltEl = null, pmEv = null, pmRaf = 0, lastXY = "";
const resetTilt = (a) => ["--rx", "--ry", "--mx", "--my"].forEach((p) => a.style.removeProperty(p));
function tilt() {
  pmRaf = 0;
  const e = pmEv;
  const a = e.target.closest ? e.target.closest(".card:not(:disabled) .art") : null;
  if (tiltEl && tiltEl !== a) resetTilt(tiltEl);
  tiltEl = a;
  if (!a || RM.matches) return;
  const r = a.getBoundingClientRect(), x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  a.style.setProperty("--rx", ((.5 - y) * 8).toFixed(2) + "deg"); a.style.setProperty("--ry", ((x - .5) * 10).toFixed(2) + "deg");
  a.style.setProperty("--mx", (x * 100).toFixed(1) + "%"); a.style.setProperty("--my", (y * 100).toFixed(1) + "%");
}
// 只认真的移动：页面重画后浏览器会对停着不动的指针补发 pointerover，那不能把键盘焦点抢走。
document.addEventListener("pointermove", (e) => {
  if (e.pointerType !== "mouse") return;
  const xy = e.clientX + "," + e.clientY;
  if (xy === lastXY) return;
  lastXY = xy;
  pmEv = e;
  if (!pmRaf) pmRaf = requestAnimationFrame(tilt);
  const c = e.target.closest && e.target.closest("#view .card:not(:disabled)"), a = document.activeElement;
  if (c && a && a !== c && a.closest && a.closest("#view") && a.matches(":focus-visible")) c.focus({ preventScroll: true });
}, { passive: true });
document.addEventListener("pointerleave", () => { if (tiltEl) { resetTilt(tiltEl); tiltEl = null; } });

// ======================================================================= 开始

hydrateIcons(document);
applyLayout(true);
boot();
