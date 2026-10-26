/* 小鼠电视 RooTV 电脑版（媒体中心界面，阶段 M0–M2）。
   设计：docs/superpowers/specs/2026-10-24-media-center-ui-design.md；原型：docs/design/media-center/index.html。
   - 外框：顶部标签栏（窗口窄于 700px 时换成底部五个标签）、页面路由（#/home、#/live …）、正在播放栏、
     电视遥控器式的方向键焦点（spatial）、深色默认 + 浅色（记在 localStorage）。
   - 「播放到」：每个播放按钮播到记住的地方（某台小电视或这台电脑），旁边的 ▾ 换地方；POST /api/play。
   - 遥控面板：右边的抽屉（手机上是「遥控」标签）：屏幕、换台、暂停、跳转、音量、亮度、只听、定时关、宠物。
   - 首页：GET /api/home 拼好的几排。
   原来电脑版的各项功能（订阅管理、B 站搜索、投屏、设备、Wi-Fi、访客码、绑定、登录列表、B 站账号、YouTube、
   邀请、让手机登录、恢复登录提示）都搬进来了，逻辑基本照旧（console-classic.html 是原来的那一页）。 */
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
  remote: '<rect x="7" y="2" width="10" height="20" rx="4"/><circle cx="12" cy="8" r="2"/><path d="M10 14h4M10 17h4"/>',
  set: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
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
                 devices: "设备与遥控", settings: "设置", remote: "遥控", me: "我的" };
const GUEST_ROUTES = ["home", "live", "search", "settings", "remote", "me"];
const TOPT = [["home", "首页"], ["live", "直播"], ["series", "剧集"], ["up", "UP 主"], ["lib", "片库"], ["search", "搜索", "search"]];
const TABS = [["home", "首页", "home"], ["live", "直播", "live"], ["search", "搜索", "search"], ["remote", "遥控", "remote"], ["me", "我的", "set"]];

function showApp() {
  ticketRun++;
  $("login").hidden = true;
  $("app").hidden = false;
  for (const b of qsa(".noguest")) b.hidden = isGuest();
  $("toAdmin").hidden = me.role !== "admin";     // 总后台（admin.html）：服务端也只给管理员
  $("inviteBlock").hidden = $("inviteList").hidden = me.role !== "admin";
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

function parseRoute() {
  const h = decodeURIComponent(location.hash.slice(1)) || "/home";
  return (h.split("?")[0].split("/").filter(Boolean)[0]) || "home";
}

function showRoute() {
  if (!me) return;
  let name = parseRoute();
  if (!TITLES[name]) name = "home";
  if (isGuest() && !GUEST_ROUTES.includes(name)) name = "home";
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
  if (name === "devices") loadDevices();
  if (name === "settings") { if (!isGuest()) loadSessions(); loadYouTube(); loadBili(); if (me.role === "admin") loadInvites(); renderAbout(); }
  if (name !== "settings") biliCancel();
  if (name === "me") renderMe();
  if (name === "up") renderUpLatest();
  if (name === "up" || name === "series") loadHome();
  if (data) render();
}
addEventListener("hashchange", () => { closeSheets(true); showRoute(); });

function tabOf(name) { return name; }
function renderTop() {
  const cur = tabOf(route);
  const cr = (id) => (cur === id ? 'aria-current="page"' : "");
  const tabs = TOPT.filter(([id]) => !isGuest() || GUEST_ROUTES.includes(id));
  const initial = me ? [...me.name][0] || "?" : "?";
  const role = me ? (me.role === "admin" ? "管理员" : me.role === "guest" ? "访客 · 只能遥控一台" : "家人") : "";
  $("top").innerHTML = `<div class="tl"><a class="wml" href="#/home" aria-label="${BRAND} ${BRAND_ZH} 首页">${iconSVG(30, true)}${wordmarkSVG(21, false)}</a></div>
    <nav class="ttabs" aria-label="主导航">${tabs.map(([id, t, i]) => `<a href="#/${id}" ${cr(id)}>${i ? ic(i, 17) : ""}${t}</a>`).join("")}</nav>
    <div class="tr">${chipHTML()}${isGuest() ? "" : `<a class="iconbtn" href="#/devices" aria-label="设备与遥控" title="设备与遥控" ${cr("devices")}>${ic("tv")}</a>`}<a class="iconbtn" href="#/settings" aria-label="设置" title="设置" ${cr("settings")}>${ic("set")}</a><a class="me" href="#/settings" title="${esc((me ? me.name : "") + " · " + role)}" aria-label="当前用户：${esc(me ? me.name : "")}，${role}">${esc(initial)}</a></div>
    <span class="ntitle" aria-hidden="true">${esc(TITLES[route])}</span>`;
  $("top").classList.toggle("home", route === "home");
}
function renderTabs() {
  const cur = ["series", "up", "lib", "devices", "settings"].includes(route) ? "me" : route;
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
                ["devices", "设备", "tv"], ["settings", "设置", "set"]].filter(([id]) => !isGuest() || GUEST_ROUTES.includes(id));
  $("meList").innerHTML = rows.map(([id, t, i]) => `<a class="li" href="#/${id}"><span class="ico">${ic(i, 18)}</span><span class="tx">${t}</span><em>›</em></a>`).join("")
    + `<div class="li"><span class="me">${esc(me ? [...me.name][0] : "?")}</span><span class="tx">${esc(me ? me.name : "")}<small>${me && me.role === "admin" ? "管理员" : isGuest() ? "访客" : "家人"}</small></span></div>`;
}
function renderAbout() {
  $("aboutText").innerHTML = `${esc(BRAND_ZH)} 电脑版<small>${esc(BRAND)} · ${data && data.watch ? "这台电脑就是家里的电视服务器" : "在浏览器里管理和遥控你的小电视"}</small>`;
}

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
      if (route === "home" && ticks % 6 === 0) loadHome(true);
    }
    schedule(pending || listenPending ? 1000 : 5000);
  }, delay);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("app").hidden) { refresh(); loadTargets(); } });

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
// 服务器打开了节目单（/api/state 的 epg，server/relay/epg.py）才问 /api/epg：每个直播台现在和下一个节目。
let epg = {}, epgAt = 0;
async function loadEpg() {
  if (!data || !data.epg) { epg = {}; return; }
  if (Date.now() - epgAt < 60000) return;
  epgAt = Date.now();
  try { epg = (await call("GET", "api/epg")).channels || {}; } catch (error) { return; }
  if (data) { renderChannels(); renderWatchTitle(); }
}
function epgNow(id) {
  const e = data && data.epg ? epg[id] : null;
  const now = Date.now() / 1000;
  const p = e && [e.now, e.next].find((x) => x && x.start <= now && now < x.end);
  const n = e && [e.now, e.next].find((x) => x && x.start >= now);
  return { now: p || null, next: n || null };
}
// 「正在播：新闻联播 19:00–19:30」；没有节目单的台为空。
function epgLine(id) {
  const p = epgNow(id).now;
  return p ? "正在播：" + p.title + " " + hm(p.start) + "–" + hm(p.end) : "";
}

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
  if (route === "live") { renderGroups(); renderChannels(); renderLiveHint(); }
  if (route === "series") renderSeriesGrid();
  renderSubs();
  renderHistory();
  followEpisodes();
  renderBiliGate();
  if (route === "settings") renderAbout();
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

// 播放一个节目（/api/home 的 play，或者页面自己拼的 {kind, channel, …}）：播到 to，不给就是记住的地方。
async function play(item, to, label) {
  if (!item || !me) return null;
  if (!to) {
    to = currentTarget();
    if (!to) { openTarget(item, "", label); return null; }
    if (!remember) { openTarget(item, "", label); return null; }
    if (!targetOnline(to)) { openTarget(item, targetName(to) + "没在线，换一个地方", label); return null; }
  }
  if (remember && to !== targetPref) setTarget(to, true);
  if (to === PC) return playOnPc(item, label);
  let answer;
  try { answer = await call("POST", "api/play", { target: to, item }); }
  catch (error) { if (error instanceof SignedOut) signedOut(); else toast("网络不通", true); return null; }
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
async function playOnPc(item, label) {
  if (watching) watching.busy = true;
  const body = { target: PC, item, viewer };
  if (item.channel === "cast" && deviceId()) body.device = deviceId();
  let answer;
  try { answer = await call("POST", "api/play", body); }
  catch (error) {
    if (watching) watching.busy = false;
    if (error instanceof SignedOut) signedOut(); else toast("网络不通", true);
    return null;
  }
  if (!answer.ok) {
    if (watching) watching.busy = false;
    toast(answer.error, true);
    return answer;
  }
  miniWho = "pc";
  openDirect(answer, { group: groupOf(answer.channel) });
  if (label) toast("在这台电脑上播放「" + label + "」");
  return answer;
}

// 「播放到」面板：每台小电视（在线、在播什么）+ 这台电脑；item 给了就是「选一个地方马上播」。
let pendingItem = null, pendingLabel = "";
function openTarget(item, message, label) {
  pendingItem = item || null;
  pendingLabel = label || "";
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
const SHEETS = ["sheet", "rsheet", "drawer"];
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
      <div class="mtx"><b>${esc(watching.title || watching.name || "在电脑上看")}</b><span><span class="dot"></span>这台电脑 · ${esc(watching.mode === "follow" ? "跟着" + deviceName() : watching.name || "窗口里播放")}</span></div>
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
    const v = $("watchVideo");
    pct = v.duration && isFinite(v.duration) ? v.currentTime / v.duration : 0;
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
  else if (k === "pcpp") { const v = $("watchVideo"); if (v.paused) v.play().catch(() => {}); else v.pause(); }
  else if (k === "pcprev") stepChannel(-1);
  else if (k === "pcnext") stepChannel(1);
  else if (k === "pcclose") stopWatch();
});

// ======================================================================= 在电脑上看（和原来一样，播放器挪到浮动窗口里）
// 自建 / 电脑客户端才有（/api/state 的 watch）：中转把片源转成 HLS（fMP4 段），这里用浏览器自带的
// MediaSource 一段段接上放（server/relay/watch.py）。两种看法：
// - 跟随（mode "follow"）：看选中的小电视正在播的台，小电视换台这里也跟着换。
// - 直接看（mode "direct"）：「播放到 → 这台电脑」，不动小电视（web.py 的 watch_direct）。
//   UP 主和剧集从电脑上次看到的地方起（没有就从小电视下次会播的地方起），电脑上的进度另记，不标看过。

let watching = null;      // {mode, device, sid, key, player, busy, channel, group, name, title, kind, index, total, series, again}
// 这个页面的编号：同一个页面换台只占一路（每个用户最多两路）；每次打开页面换一个。
const viewer = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, "0")).join("");

function watchKey(now) { return [now.channel, now.title, now.episode].join("|"); }

function watchSay(text, bad, retry) {
  const box = $("watchMsg");
  box.className = "watchmsg" + (bad ? " bad" : "");
  box.replaceChildren();
  if (!text) return;
  box.appendChild(el("div", "", text));
  if (retry) box.appendChild(button("重试", "ghost", retryWatch));
}
function retryWatch() {
  if (watching && watching.mode === "direct") startDirect(watching.again, { group: watching.group, again: true });
  else startWatch(false, true);
}

// 一路 HLS（fMP4 段）用 MediaSource 放：取 master 里的 CODECS，接上初始化段，再按列表一段段接。
class WatchPlayer {
  constructor(video, base, say, ended) {
    this.video = video; this.base = base; this.say = say;
    this.alive = true; this.next = -1; this.ms = null; this.sb = null; this.started = false;
    this.onWaiting = () => this.kick();
    // 放完了：视频播完，或者直播源断了。跟随小电视时，小电视换到下一个就会自动接上。
    this.onEnded = () => { if (this.alive) ended(); };
    video.addEventListener("waiting", this.onWaiting);
    video.addEventListener("ended", this.onEnded);
  }
  async start() {
    const v = this.video;
    if (!window.MediaSource) {
      // 没有 MediaSource（老 iPhone Safari）：浏览器自己放 HLS。
      if (!v.canPlayType("application/vnd.apple.mpegurl")) { this.say("这个浏览器放不了，换 Chrome、Edge 或 Safari 试试", true); return; }
      v.src = this.base + "master.m3u8";
      v.play().catch(() => this.say("点一下画面开始播放"));
      return;
    }
    this.say("正在连片源…（要几秒钟）");
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
      if (this.alive) this.say(error.message || "放不了", true, true);
    }
  }
  // 取一个文件：503（还在连片源）过一会儿再来；410 是这一路停了，正文是原因（B 站连不上之类）；404 是地址失效了。
  async fetch(name, kind) {
    for (let round = 0; ; round++) {
      if (!this.alive) throw new Error("");
      const response = await fetch(this.base + name, { credentials: "same-origin", cache: "no-store" });
      if (response.ok) return response[kind]();
      if (response.status === 503 && round < 8) { await sleep(1000); continue; }
      if (response.status === 410) throw new Error((await response.text()) || "片源断了");
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
  async loop() {
    while (this.alive) {
      const text = await this.fetch("index.m3u8", "text");
      const first = Number((text.match(/#EXT-X-MEDIA-SEQUENCE:(\d+)/) || [])[1] || 0);
      const count = (text.match(/^\d+\.m4s$/gm) || []).length;
      const ended = text.includes("#EXT-X-ENDLIST");
      // 第一次从倒数第六段起（约 12 秒的余量）：直播源常常几秒才来一大段。落在窗口外就跳到窗口里。
      if (this.next < 0) this.next = Math.max(first, first + count - 6);
      if (this.next < first) this.next = first;
      for (; this.next < first + count && this.alive; this.next++) {
        const chunk = await this.fetch(this.next + ".m4s", "arrayBuffer");
        if (!chunk || !this.alive) continue;
        await this.trim();
        await this.append(chunk);
        this.kick();
      }
      if (ended && this.next >= first + count) {
        if (this.ms.readyState === "open") this.ms.endOfStream();
        return;
      }
      await sleep(1000);
    }
  }
  // 起播、跳过空档、落后太多时追到最新附近。
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
    } else if (end - v.currentTime > 30) v.currentTime = end - 12;
    if (!this.started) {
      this.started = true;
      this.say("");
      v.play().catch(() => this.say("点一下画面开始播放"));
    }
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
    v.pause();
    v.removeAttribute("src");
    v.load();
  }
}
function playerFor(answer) {
  if (watching) watching.player.stop();
  $("pcwin").hidden = false;
  return new WatchPlayer($("watchVideo"), answer.url, watchSay, watchEnded);
}

// 一个视频放完了：直接看 UP 主、剧集时接着放下一个（下一集），像小电视一样；别的停在这里，可以重试。
function watchEnded() {
  if (watching && watching.mode === "direct" && watching.kind === "video" && /^(up|sr)\d\d$/.test(watching.channel)) {
    startDirect({ channel: watching.channel, step: 1 }, { group: watching.group });
    return;
  }
  watchSay(watching && watching.kind === "live" ? "直播断了" : "这一段放完了", false, true);
}

// 跟随小电视：看它正在播的台。
async function startWatch(auto, again) {
  const device = deviceId(), key = watchKey(data.now);
  if (watching) watching.busy = true;
  let answer;
  try { answer = await call("POST", "api/watch", withDevice({ viewer })); }
  catch (error) {
    if (watching) watching.busy = false;
    if (error instanceof SignedOut) signedOut(); else toast("网络不通", true);
    return;
  }
  if (!answer.ok) {
    if (watching && auto) { watching.key = key; watching.busy = false; watchSay(answer.error, true, true); }
    else { if (watching) watching.busy = false; toast(answer.error, true); }
    return;
  }
  if (watching && watching.mode === "follow" && watching.sid === answer.sid && watching.device === device && !again) {
    watching.key = key; watching.busy = false; return;       // 还是同一个片源，接着放
  }
  const player = playerFor(answer);
  miniWho = "pc";
  watching = { mode: "follow", device, sid: answer.sid, key, player, busy: false, channel: answer.channel, kind: answer.kind,
               name: data.now.name || answer.channel, title: answer.title || data.now.name };
  $("watchFollow").checked = true;
  watching.label = deviceName() + " · " + (data.now.name || answer.channel) + (answer.title && answer.title !== data.now.name ? " · " + answer.title : "");
  renderWatchTitle();
  renderWatch();
  renderMini();
  player.start();
}

// 直接看：body 是 {channel[, episode | step]}、{url}（投到电脑）或 {video: {site, id}}（投屏历史）。
async function startDirect(body, ctx) {
  ctx = ctx || {};
  if (watching) watching.busy = true;
  let answer;
  const request = Object.assign({ viewer }, body);
  if (body.channel === "cast" && deviceId()) request.device = deviceId();
  try { answer = await call("POST", "api/watch", request); }
  catch (error) {
    if (watching) watching.busy = false;
    if (error instanceof SignedOut) signedOut(); else toast("网络不通", true);
    return false;
  }
  if (!answer.ok) {
    if (watching) watching.busy = false;
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
  const player = same ? watching.player : playerFor(answer);
  // 重试用的请求：不再「下一个」，而是回到这一个（UP 主、剧集按第几个；投屏的按视频）。
  const again = answer.index ? { channel: answer.channel, episode: answer.index }
    : answer.channel === "cast" && answer.video ? { video: answer.video } : { channel: answer.channel };
  watching = { mode: "direct", device: deviceId(), sid: answer.sid, key: "", player, busy: false, channel: answer.channel,
               group: ctx.group || groupOf(answer.channel), name: answer.name, title: answer.title, kind: answer.kind,
               index: answer.index || 0, total: answer.total || 0, series: !!answer.series, again };
  miniWho = "pc";
  $("watchFollow").checked = false;
  const what = answer.series ? "第 " + answer.index + " / " + answer.total + " 集" + (answer.title ? " · " + answer.title : "")
    : answer.title && answer.title !== answer.name ? answer.title : "";
  watching.label = "这台电脑 · " + (answer.name || answer.channel) + (what ? " · " + what : "");
  renderWatchTitle();
  render();
  if (!same) player.start();
  setTimeout(() => loadHome(true), 4000);
}

function stopWatch() {
  if (watching) {
    watching.player.stop();
    // 直接看的那一路只给这个页面用：马上停，让出名额（跟随的那一路全家共用，没人看 20 秒后自己停）。
    if (watching.mode === "direct") call("POST", "api/watch/stop", { viewer }).catch(() => {});
  }
  watching = null;
  miniWho = "tv";
  watchSay("");
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
  startDirect({ channel: next.id }, { group: g.name });
}

function renderWatchTitle() {
  if (!watching) return;
  const line = epgLine(watching.channel);
  $("watchWhat").textContent = watching.title || watching.name || "在电脑上看";
  $("watchSub").textContent = (watching.label || "") + (line ? " · " + line : "");
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
  const video = direct && watching.kind === "video" && /^(up|sr)\d\d$/.test(watching.channel);
  $("watchNextVid").hidden = !video;
  $("watchNextVid").textContent = watching.series ? "下一集" : "下一个";
  $("watchFollowBox").hidden = !online;
  const d = data.now.device;
  $("watchNote").textContent = direct
    ? "直接在电脑上看，不动小电视。" + (video ? "电脑上看的进度另记（「继续观看」里有），不会把小电视那边标成看过。" : "")
      + "只在开着这个画面时才转，关掉后马上停。"
    : "电脑上的画面和小电视不同步：两边各自从片源取，电脑上通常晚几秒到十几秒。"
      + (d && d.audio_only === 1 ? "小电视现在「只听」，电脑上照样有画面。" : "")
      + "关掉约 20 秒后自动停。";
  // 跟随：小电视换了台（或下一个视频）、电脑版换了设备，就重新要一次地址；还是同一个片源时不打断。
  if (!direct && $("watchFollow").checked && online && !watching.busy &&
      (watchKey(data.now) !== watching.key || watching.device !== deviceId())) startWatch(true);
}
$("watchOpen").onclick = () => { closeSheets(true); startWatch(false); };
$("watchClose").onclick = stopWatch;
$("watchPrevCh").onclick = () => stepChannel(-1);
$("watchNextCh").onclick = () => stepChannel(1);
$("watchNextVid").onclick = () => { if (watching && !watching.busy) startDirect({ channel: watching.channel, step: 1 }, { group: watching.group }); };
$("watchFollow").onchange = () => { if ($("watchFollow").checked && watching && watching.mode === "direct") startWatch(false); };
$("watchBig").onclick = () => {
  const big = $("pcwin").classList.toggle("big");
  $("watchBig").innerHTML = ic(big ? "shrink" : "expand", 16);
  $("watchBig").setAttribute("aria-label", big ? "缩小" : "放大");
  $("watchBig").title = big ? "缩小" : "放大";
};
for (const ev of ["play", "pause", "timeupdate"]) $("watchVideo").addEventListener(ev, () => { if (ev !== "timeupdate") renderMini(); else renderMiniProgress(); });

// ======================================================================= 直播：分组 + 频道卡片

function renderLiveHint() {
  const to = currentTarget();
  $("tapHint").textContent = !to ? "还没有小电视。" + (data.watch ? "" : "绑定以后点台就能换台。")
    : to === PC ? "点一下，在这台电脑上看（不动小电视）。" : "点一下，「" + targetName(to) + "」就换到这个台。";
  $("liveHint").textContent = data.epg ? "节目信息来自节目单（EPG）" : "";
}
function renderGroups() {
  const names = data.groups.map((g) => g.name);
  if (!names.includes(group)) group = data.now.group && names.includes(data.now.group) ? data.now.group : names[0];
  const box = $("groups");
  box.replaceChildren();
  for (const g of data.groups) {
    const b = el("button", "", g.name);
    b.setAttribute("aria-pressed", String(g.name === group));
    b.appendChild(el("em", "", String(g.channels.length)));
    b.dataset.k = "grp-" + g.name;
    b.onclick = () => { group = g.name; renderGroups(); renderChannels(); };
    box.appendChild(b);
  }
}
const isRelay = (id) => /^(up|sr)\d\d$/.test(id) || id === "cast";
let channelsSig = "";
function renderChannels() {
  if (route !== "live" || !data) return;
  const grid = $("channels");
  const current = data.groups.find((g) => g.name === group);
  const sig = JSON.stringify([group, current, data.now.channel, data.now.online, pending, watching && watching.channel, adjusting,
                              Math.floor(Date.now() / 60000), Object.keys(epg).length, deviceId()]);
  if (sig === channelsSig && grid.childElementCount) return;
  channelsSig = sig;
  const fk = focusKey();
  grid.replaceChildren();
  if (!current) return;
  const sites = {};
  for (const c of data.creators) sites[c.id] = c.site;
  for (const s of data.series || []) sites[s.id] = s.site;
  const movable = !["我的投屏", "UP 主"].includes(group);
  for (const c of current.channels) {
    const air = !pending && c.id === data.now.channel && data.now.online;
    const going = pending && pending.kind === "switch" && c.id === pending.channel;
    const pc = watching && watching.mode === "direct" && watching.channel === c.id;
    const card = el("button", "card" + (air ? " air" : "") + (going ? " going" : "") + (pc ? " pcair" : ""));
    card.dataset.k = "ch-" + c.id;
    const e = epgNow(c.id);
    const relay = isRelay(c.id);
    const badge = relay ? (c.id === "cast" ? "投屏" : sites[c.id] ? siteTag(sites[c.id]) : "") : "直播";
    const logo = relay ? "" : `<span class="logo-big">${badgeSVG(c.name)}<img src="api/art/logo/${encodeURIComponent(c.id)}" alt="" loading="lazy" style="position:absolute;inset:0;margin:auto"></span>`;
    let pct = 0;
    if (e.now) pct = (Date.now() / 1000 - e.now.start) / (e.now.end - e.now.start);
    card.innerHTML = `<span class="art">${art(c.name, "w", { title: relay ? c.name : "" })}${logo}${badge ? `<span class="badge ${relay ? "" : "live"}">${esc(badge)}</span>` : ""}`
      + `<span class="badge r">${pad(c.n)}</span>${pct > 0 ? `<span class="pbar"><i style="width:${(Math.min(1, pct) * 100).toFixed(1)}%"></i></span>` : ""}</span>`
      + `<span class="t">${esc(e.now ? e.now.title : c.name)}</span>`
      + `<span class="s">${esc(going ? "切换中…" : pc ? "电脑上在放" : air ? (deviceName() + "在播 · " + c.name) : e.now ? c.name + " · " + hm(e.now.start) + "–" + hm(e.now.end) : c.name)}</span>`
      + (e.next ? `<span class="s2">接下来 ${hm(e.next.start)} ${esc(e.next.title)}</span>` : "");
    card.setAttribute("aria-label", "播放 " + c.name + (e.now ? "，正在播 " + e.now.title : ""));
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
      continue;
    }
    card.onclick = () => {
      if (going) return;
      if (air && currentTarget() === deviceId()) { openRemote(); return; }
      play({ kind: relay ? "channel" : "live", channel: c.id, title: c.name }, null, c.name);
    };
    grid.appendChild(card);
  }
  restoreFocus(fk);
}
$("adjust").onclick = () => {
  adjusting = !adjusting;
  $("adjust").textContent = adjusting ? "完成" : "调整分组";
  $("adjust").classList.toggle("on", adjusting);
  if (data) renderChannels();
};

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

// ======================================================================= 搜索：B 站搜索（/api/bili/search，封面经 /api/bili/thumb）+ 粘贴链接即播

const bs = { kind: "video", q: "", page: 0, pages: 0, items: [], run: 0, timer: 0, retried: false };
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
  const q = $("bsQuery").value.trim().replace(/\s+/g, " ");
  if (!q) { bsSay("输入关键词，停手一会儿就会搜；按回车马上搜。粘贴 B 站或 YouTube 视频链接可以直接播放。"); return; }
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
  if (!bs.items.length) bsSay("没有搜到「" + q + "」" + { video: "的视频", user: "的 UP 主（有投稿的）", bangumi: "的番剧" }[kind]);
  else bsSay((answer.total ? "B 站说有 " + bigNumber(answer.total) + " 个结果，" : "") + "这里列出 " + bs.items.length + " 个");
  renderSearch();
}
$("bsQuery").addEventListener("input", () => {
  clearTimeout(bs.timer);
  if (renderLinkHit()) return;
  const q = $("bsQuery").value.trim();
  if (q.length >= 2 && q !== bs.q && !isGuest()) bs.timer = setTimeout(() => bsSearch(true), 900);   // 停手 0.9 秒再搜，不逐字去问 B 站
});
$("bsQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") bsSearch(true); });
$("bsMore").onclick = () => bsSearch(false);
for (const b of qsa("#bsKinds button")) {
  b.onclick = () => {
    if (bs.kind === b.dataset.kind) return;
    bs.kind = b.dataset.kind;
    for (const x of qsa("#bsKinds button")) x.setAttribute("aria-pressed", String(x === b));
    bs.items = []; bs.page = 0; bs.q = "";
    renderSearch();
    if ($("bsQuery").value.trim()) bsSearch(true);
    else bsSay("输入关键词，停手一会儿就会搜；按回车马上搜。");
  };
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
  return kind === "user" ? (data.creators || []).some((c) => c.site === "bilibili" && c.source === source)
    : kind === "series" ? (data.series || []).some((s) => s.site === "bilibili" && s.source === source) : false;
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
    b.textContent = done ? (b.dataset.kind === "user" ? "已订阅" : "已添加") : b.dataset.text;
    if (done) b.disabled = true;
    else if (b.classList.contains("was")) b.disabled = false;
    b.classList.toggle("was", done);
  }
}
function renderSearch() {
  const box = $("bsHits");
  box.replaceChildren();
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
        go.onclick = () => play({ kind: "cast", url: item.link, title: item.title }, null, item.title);
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
      body.append(el("div", "t", item.title),
                  el("div", "m", [item.badge || "番剧", item.eps ? "共 " + item.eps + " 集" : "", item.styles].filter(Boolean).join(" · ")),
                  el("div", "d", item.desc || ""));
      acts.appendChild(addAsSeries(item));
      row.append(thumb(item, "p", "poster"), body, acts);
    }
    box.appendChild(row);
  }
  $("bsMore").hidden = !bs.items.length || bs.page >= bs.pages;
  markSubscribed();
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
      button("播放", "linkbtn play", () => play(item, null, h.title || h.id)),
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
  $("biliBlock").hidden = !biliOn();
  $("biliImportRow").hidden = !biliOn();
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
}

// ---------- UP 主 → 从 B 站导入关注的 UP 主（GET api/bili/followings，POST api/bili/followings/import） ----------

let follow = null;    // { items, picked: Set, q, max, count, total, note }
function followLeft() { return follow ? Math.max(0, follow.max - follow.count) : 0; }
$("biliImport").onclick = openFollow;
async function openFollow() {
  const b = data && data.bili;
  if (!b || !b.login || b.invalid) {
    toast(b && b.invalid ? "B 站登录已失效：先在「设置 → B 站账号」重新扫码" : "先在「设置 → B 站账号」扫码登录 B 站", true);
    location.hash = "#/settings";
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
$("castYtGo").onclick = () => { setTimeout(() => { const box = $("ytLocal"); if (!box.hidden) box.scrollIntoView(); }, 300); };

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

// ---------- 邀请（管理员） ----------
async function loadInvites() {
  let answer;
  try { answer = await call("GET", "api/invites"); }
  catch (error) { if (error instanceof SignedOut) signedOut(); return; }
  const rows = $("inviteRows");
  rows.replaceChildren();
  if (!answer.invites.length) rows.appendChild(emptyRow("还没有邀请过"));
  for (const i of answer.invites) {
    const state = i.state === "used" ? "已接受" + (i.used_by_name ? "：" + i.used_by_name : "") : i.state === "expired" ? "过期了" : "等待接受";
    rows.appendChild(listRow(i.state === "open" ? "air" : "", i.name || "（没写名字）", "生成 " + when(i.created_at) + " · 到期 " + when(i.expires_at) + " · " + state, []));
  }
}
$("invite").onclick = () => busy($("invite"), async () => {
  const answer = await call("POST", "api/invites", { name: $("inviteName").value });
  if (!answer.ok) { toast(answer.error, true); return; }
  $("inviteName").value = "";
  const box = el("div", "qrbox");
  box.innerHTML = await qrSvg(answer.link);
  const copy = button("复制链接", "ghost", async () => {
    try { await navigator.clipboard.writeText(answer.link); toast("已复制"); } catch (error) { toast("请手动选中复制", true); }
  });
  const row = el("div", "row");
  row.append(copy, button("关闭", "go", closeOverlays));
  modal("邀请链接", box, el("p", "", "家人用手机扫码或打开这个链接，点「接受邀请」。7 天内有效、只能用一次。"), el("div", "link", answer.link), row);
  loadInvites();
});

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
  if (!homeData.guest) {
    const none = !homeData.creator_updates.length && !homeData.series_updates.length && data && !data.creators.length && !(data.series || []).length;
    if (none) {
      o += `<div class="h2"><h2>开始收集你爱看的</h2></div><div class="onb">
        <a href="#/search"><b>${ic("up")}订阅一个 UP 主</b><p>B 站或 YouTube 的 UP 主，每个都变成一个台，新视频自动排上。</p></a>
        <a href="#/series"><b>${ic("series")}加一部剧</b><p>番剧、多集视频、合集、播放列表都行，按顺序播，接着上次的地方。</p></a>
        <a href="#/lib"><b>${ic("lib")}片库（即将推出）</b><p>电脑或 NAS 上的电影、剧集、动画和家庭视频。</p></a></div>`;
    } else {
      if (homeData.creator_updates.length) o += shelf("UP 主更新", homeData.creator_updates.length + " 个没看的视频", homeData.creator_updates.map(cardVideo).join(""), "#/up");
      if (homeData.series_updates.length) o += shelf("剧集追更", "", homeData.series_updates.map(cardSeries).join(""), "#/series");
    }
    o += shelf("我的片库", "", `<a class="card soon" href="#/lib"><span class="art"><span>${ic("lib", 30)}片库即将推出</span></span><span class="t">片库</span><span class="s">电脑或 NAS 上的视频，以后在这里</span></a>`);
  }
  box.innerHTML = o + "</div>";
  qsa("#homeBody .shelf").forEach((e, i) => { if (scrolls[i] !== undefined) e.scrollLeft = scrolls[i]; });
  restoreFocus(fk);
}

// ======================================================================= 点击（委托）

document.addEventListener("click", (event) => {
  const t = event.target.closest("button, a");
  if (!t) return;
  const ds = t.dataset;
  if (ds.close !== undefined) { closeSheets(); return; }
  if (ds.play) {
    event.preventDefault();
    const x = ITEMS[ds.play];
    if (x) play(x.play, null, x.title);
    return;
  }
  if (ds.pick) { const x = ITEMS[ds.pick]; if (x) openTarget(x.play, "", x.title); return; }
  if (ds.series) { openEpisodes(ds.series); return; }
  if (ds.sheet === "target") { openTarget(null); return; }
  if (ds.tg) {
    if (t.getAttribute("aria-disabled") === "true") { toast(targetName(ds.tg) + "离线了，看看电源和 Wi-Fi"); return; }
    if (pendingItem) {
      const item = pendingItem, label = pendingLabel;
      if (remember) setTarget(ds.tg, true);
      closeSheets(true);
      play(item, ds.tg, label);
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
  if (event.key === "Tab" && anyOverlay()) {   // 面板里 Tab 不跑出去
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
  const row = h && cur.closest(".shelf, .chips, .rdevs, .seg, .petgrid");
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
