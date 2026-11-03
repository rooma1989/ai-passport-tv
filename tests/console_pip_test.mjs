// Tests for the PC player's canvas picture-in-picture in server/relay/assets/console.js: the canvas size, where
// the picture goes in it, and how subtitle cues are cleaned, wrapped and laid out (the block between
// "画中画画布的纯函数" and "画中画画布的纯函数到此"; the page is a plain script, so the block is cut out and
// evaluated on its own). Also checks that the page prefers the canvas on Chrome and keeps the fallbacks.
// Run by tests/test_relay_watch.py under Node (18+); also runnable on its own:
//   node tests/console_pip_test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(ROOT, 'server/relay/assets/console.js'), 'utf8');
const begin = source.indexOf('// ---- 画中画画布的纯函数');
const end = source.indexOf('// ---- 画中画画布的纯函数到此');
assert.ok(begin > 0 && end > begin, 'the pure block is marked in console.js');
const { PIP_MAX, pipSize, pipFit, cueText, cueWrap, cueLayout } = new Function(
  source.slice(begin, end) + '\nreturn { PIP_MAX, pipSize, pipFit, cueText, cueWrap, cueLayout };')();

// ---- canvas size: at most 1280x720, the video's aspect, even sides ----
assert.deepEqual(PIP_MAX, { W: 1280, H: 720 });
assert.deepEqual(pipSize(1920, 1080), { w: 1280, h: 720 });
assert.deepEqual(pipSize(3840, 2160), { w: 1280, h: 720 }, '4K is drawn at 720p');
assert.deepEqual(pipSize(1280, 720), { w: 1280, h: 720 });
assert.deepEqual(pipSize(640, 360), { w: 640, h: 360 }, 'never upscaled');
assert.deepEqual(pipSize(1920, 800), { w: 1280, h: 534 }, 'scope: width-bound');
assert.deepEqual(pipSize(1080, 1920), { w: 406, h: 720 }, 'portrait: height-bound');
assert.deepEqual(pipSize(1440, 1080), { w: 960, h: 720 }, '4:3');
assert.deepEqual(pipSize(0, 0), { w: 1280, h: 720 }, 'no metadata yet');
assert.deepEqual(pipSize(NaN, 720), { w: 1280, h: 720 });
for (const [w, h] of [[1919, 1079], [721, 405], [333, 777]]) {
  const s = pipSize(w, h);
  assert.ok(s.w % 2 === 0 && s.h % 2 === 0 && s.w <= 1280 && s.h <= 720, `${w}x${h} -> ${s.w}x${s.h}`);
}

// ---- the picture inside the canvas: contained, centred, black bars on the short side ----
assert.deepEqual(pipFit(1920, 1080, 1280, 720), { x: 0, y: 0, w: 1280, h: 720 });
assert.deepEqual(pipFit(1440, 1080, 1280, 720), { x: 160, y: 0, w: 960, h: 720 }, 'pillarbox');
assert.deepEqual(pipFit(1920, 800, 1280, 720), { x: 0, y: 94, w: 1280, h: 533 }, 'letterbox');
assert.deepEqual(pipFit(0, 0, 1280, 720), { x: 0, y: 0, w: 1280, h: 720 });

// ---- cue text: WebVTT tags out, entities back ----
assert.equal(cueText('<i>你好</i>'), '你好');
assert.equal(cueText('<c.yellow>Tom &amp; Jerry</c> &lt;3'), 'Tom & Jerry <3');
assert.equal(cueText('<v Bob>Hi</v>\n<00:00:01.000>there'), 'Hi\nthere');
assert.equal(cueText('a&nbsp;b'), 'a b');
assert.equal(cueText(''), '');
assert.equal(cueText(undefined), '');

// ---- wrapping (one unit per character in this fake measure) ----
const m = (s) => s.length;
assert.deepEqual(cueWrap('short line', 20, m), ['short line']);
assert.deepEqual(cueWrap('the quick brown fox jumps', 10, m), ['the quick', 'brown fox', 'jumps'], 'by word');
assert.deepEqual(cueWrap('一二三四五六七八九十', 4, m), ['一二三四', '五六七八', '九十'], 'CJK by character');
assert.deepEqual(cueWrap('中文 and English 混排', 8, m), ['中文 and', 'English', '混排']);
assert.deepEqual(cueWrap('first\nsecond', 20, m), ['first', 'second'], 'line breaks kept');
assert.deepEqual(cueWrap('abcdefghijkl', 5, m), ['abcde', 'fghij', 'kl'], 'a word longer than the line');
assert.deepEqual(cueWrap('  spaced   out  ', 20, m), ['spaced out']);
assert.deepEqual(cueWrap('', 20, m), []);
assert.deepEqual(cueWrap('a\n\nb', 20, m), ['a', 'b'], 'empty lines dropped');

// ---- layout: 4.5 % of the height, centred, the last line 5 % above the bottom ----
const lay = cueLayout([300, 500], 1280, 720);
assert.equal(lay.font, 32);
assert.equal(lay.lineH, 42);
assert.equal(lay.boxes.length, 2);
assert.equal(lay.boxes[1].y + lay.boxes[1].h, 720 - 36, 'bottom margin');
assert.equal(lay.boxes[0].y + lay.boxes[0].h, lay.boxes[1].y, 'lines stacked without gaps');
for (const b of lay.boxes) assert.ok(Math.abs(b.x + b.w / 2 - 640) <= 1, 'centred');
assert.equal(lay.boxes[1].w, 500 + 2 * lay.padX, 'padding around the text');
assert.ok(lay.maxW < 1280 * 0.9 && lay.maxW > 1000);
assert.equal(cueLayout([10], 400, 200).font, 12, 'a small canvas keeps a readable size');

// ---- the page: canvas first on Chrome, document PiP as the fallback, Safari keeps its own ----
assert.match(source, /if \(canvasPipOk\(v\)\) \{ openCanvasPip\(v\); return; \}\n  if \(docPipOk\(\)\) \{ openDocPip\(v\); return; \}/);
assert.match(source, /typeof v\.webkitSetPresentationMode !== "function"/, 'Safari does not take the canvas path');
assert.match(source, /canvas\.captureStream\(30\)/);
assert.match(source, /helper\.muted = true/, 'the helper never makes a sound');
assert.match(source, /error\.name === "SecurityError"[\s\S]{0,200}openDocPip\(v\)/, 'a tainted canvas falls back to document PiP');
assert.match(source, /disallowReturnToOpener: true/, 'document PiP keeps no Back to tab');
assert.match(source, /addEventListener\("leavepictureinpicture", pipCleanup\)/, 'closing PiP stops the loop');
assert.match(source, /setActionHandler\(action, fn\)/, 'Media Session play / pause');

console.log('console pip: ok');
