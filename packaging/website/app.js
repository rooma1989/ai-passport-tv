// 小鼠电视官网的脚本（packaging/website/）。页面只是外壳，内容从 JSON 来：
//   data/site.json        功能、小电视、上手、问答（packaging/website/content.json）
//   data/changelog.json   更新日志（tools/build_site.py 从 docs/CHANGELOG.zh_CN.md 生成）
//   ../rootv/latest/rootv-update.json   最新版本和各平台的安装包（tools/publish_update.py 发的签名清单）
// 取 JSON 都带 ?t=<每 5 分钟变一次的数>，七牛 CDN 缓存了旧的也最多 5 分钟；发布时还会刷新 CDN。
"use strict";

// ---- 官网的纯函数（tests/website_test.mjs 单独取出来测）
const GITHUB = "https://github.com/rooma1989/ai-passport-tv/releases";
const MANIFEST = "../rootv/latest/rootv-update.json";
const PLATFORMS = [
  ["macos-arm64", "Mac（Apple 芯片：M1、M2、M3、M4…）", "DMG"],
  ["macos-x86_64", "Mac（Intel 芯片）", "DMG"],
  ["windows-x64", "Windows 10 / 11（64 位）安装包", "安装包"],
  ["windows-x64-portable", "Windows 免安装版", "zip"],
  ["linux-x86_64", "Linux（x86_64，未验证）", "tar.gz"],
];
function bust(now) { return Math.floor(now / 300000); }
function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
// 更新日志里的 **粗体** 和 `代码`：先转义，再换成标签（不认别的写法，链接只显示文字）。
function inline(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+?)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
}
// 这台电脑大概是哪个平台（只决定先显示哪个按钮，别的照样列着）。
function guess(ua, arch) {
  ua = String(ua || "");
  if (/Windows/i.test(ua)) return "windows-x64";
  if (/Macintosh|Mac OS X/i.test(ua)) return arch === "x86" ? "macos-x86_64" : "macos-arm64";
  if (/Linux/i.test(ua) && !/Android/i.test(ua)) return "linux-x86_64";
  return "";
}
function size(bytes) {
  return bytes >= 1048576 ? (bytes / 1048576).toFixed(0) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
}
// 清单 → 下载行：[{key, label, kind, name, size, sha256, mirror, github, mine}]，自己的平台排第一；精简版的不列。
function rows(manifest, mine) {
  const files = (manifest && manifest.files) || {};
  const out = [];
  for (const [key, label, kind] of PLATFORMS) {
    const f = files[key];
    if (!f || !/^[A-Za-z0-9._-]+$/.test(f.name || "")) continue;
    out.push({ key, label, kind, name: f.name, size: f.size, sha256: f.sha256,
               mirror: "../rootv/" + encodeURIComponent(manifest.version) + "/" + encodeURIComponent(f.name),
               github: GITHUB + "/download/v" + encodeURIComponent(manifest.version) + "/" + encodeURIComponent(f.name),
               mine: key === mine });
  }
  out.sort((a, b) => (b.mine ? 1 : 0) - (a.mine ? 1 : 0));
  return out;
}
// ---- 官网的纯函数到此

const $ = (id) => document.getElementById(id);
async function getJSON(url) {
  const answer = await fetch(url + (url.includes("?") ? "&" : "?") + "t=" + bust(Date.now()), { cache: "no-store" });
  if (!answer.ok) throw new Error("HTTP " + answer.status);
  return answer.json();
}
async function arch() {
  try {
    const ua = navigator.userAgentData;
    if (ua && ua.getHighEntropyValues) return (await ua.getHighEntropyValues(["architecture"])).architecture || "";
  } catch (_) { /* 不支持：按 Apple 芯片 */ }
  return "";
}

async function home() {
  let site;
  try { site = await getJSON("data/site.json"); }
  catch (_) { $("featureList").innerHTML = '<p class="hint bad">内容没取到，刷新一下试试。</p>'; return; }
  $("tagline").textContent = site.tagline || $("tagline").textContent;
  $("intro").textContent = site.intro || "";
  if (site.beta) { $("beta").textContent = site.beta; $("beta").hidden = false; }
  // 前两个大格子，其余的三个一排；小标题轮流用四种渐变色。
  $("featureList").innerHTML = (site.features || []).map((f, i) =>
    `<div class="tile${i < 2 ? " wide" : ""}"><p class="k k${(i % 4) + 1}">${esc(f.kicker || f.title)}</p>
      <h3>${esc(f.headline || f.title)}</h3><p>${esc(f.text)}</p></div>`).join("");
  $("deviceList").innerHTML = (site.devices || []).map((d, i) =>
    `<div class="tile"><p class="k k${i + 2}">小电视</p><h3>${esc(d.name)}</h3><p>${esc(d.text)}</p></div>`).join("");
  $("stepList").innerHTML = (site.steps || []).map((s, i) =>
    `<li><span class="n k${i + 1}">${i + 1}</span><b>${esc(s.title)}</b><span class="t">${esc(s.text)}</span></li>`).join("");
  $("needList").innerHTML = (site.needs || []).map((n) => `<li>${esc(n)}</li>`).join("");
  $("faqList").innerHTML = (site.faq || []).map((f) => `<details><summary>${esc(f.q)}</summary><p>${esc(f.a)}</p></details>`).join("");
  if (site.disclaimer) $("disclaimer").textContent = site.disclaimer;
  try {
    const m = await getJSON(MANIFEST);
    $("latest").textContent = "最新版本 " + m.version + "，" + m.date + " 发布";
  } catch (_) { /* 还没发布：不显示版本 */ }
}

async function download() {
  let manifest;
  try { manifest = await getJSON(MANIFEST); }
  catch (_) {
    $("dlTitle").textContent = "下载列表暂时取不到";
    $("dlList").innerHTML = `<p class="hint">可以到 <a href="${GITHUB}">GitHub 发布页</a> 下载（国内可能慢）。</p>`;
    $("dlNotes").textContent = "—";
    return;
  }
  $("dlTitle").textContent = "最新版本 " + manifest.version + "（" + manifest.date + "）";
  const list = rows(manifest, guess(navigator.userAgent, await arch()));
  $("dlList").innerHTML = list.length ? list.map((r) => `<div class="row${r.mine ? " mine" : ""}">
      <div class="what"><b>${esc(r.label)}</b><span>${esc(r.kind)} · ${esc(size(r.size))}${r.mine ? " · 适合你这台电脑" : ""}</span></div>
      <div class="acts"><a class="alt" href="${esc(r.github)}" rel="noopener">GitHub 备用</a>
        <a class="pill${r.mine ? "" : " ghost sm"}" href="${esc(r.mirror)}" download>下载</a></div>
      <code class="sha">SHA-256 ${esc(r.sha256)}</code></div>`).join("")
    : `<p class="hint">这一版还没有安装包，先到 <a href="${GITHUB}">GitHub 发布页</a> 看看。</p>`;
  $("dlFoot").innerHTML = `下载从国内的镜像走；慢或者下不动时点「GitHub 备用」。所有版本都在 <a href="${GITHUB}">GitHub 发布页</a>。`;
  $("dlNotes").textContent = manifest.notes || "（这一版没有写说明）";
}

async function changelog() {
  let log;
  try { log = await getJSON("data/changelog.json"); }
  catch (_) { $("logList").innerHTML = '<p class="hint bad">更新日志没取到，刷新一下试试。</p>'; return; }
  $("logList").innerHTML = (log.sections || []).map((s) =>
    `<div class="ver-block"><h2>${esc(s.title === "未发布" ? "开发中" : s.title)}${s.title === "未发布" ? "<small>下一版会带上</small>" : ""}</h2>
     <ul>${(s.items || []).map((item) => `<li>${inline(item)}</li>`).join("")}</ul></div>`).join("")
    || '<p class="hint">还没有内容。</p>';
}

const PAGES = { home, download, changelog };
const run = PAGES[document.body.dataset.page];
if (run) run();
