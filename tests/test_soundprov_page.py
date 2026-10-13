"""The sound-provisioning page (/tv/wifi) and the device's decoder agree.

The phone page's encoder is JavaScript inside server/relay/assets/wifi.html;
the decoder is main/av_soundprov.c. Nothing but these tests connects them:

- the page's encoder, run under node, produces the same symbols as the C
  mirror in tests/soundprov_ref.c (both pinned to the golden hashes below,
  which tests/test_av_soundprov.c asserts for the C side);
- audio the page's own render() makes, written to a WAV file at the rates an
  iPhone uses, is decoded by the device's decoder through the simulated
  speaker/room/microphone of tests/test_av_soundprov.c;
- the page has no way to send anything over the network.

The node and compiler cases skip themselves when node or a C compiler is
missing; the page checks always run.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "server" / "relay" / "assets" / "wifi.html"
NODE = shutil.which("node")
CC = os.environ.get("CC") or shutil.which("cc")

# (ssid, password, server, symbols per repeat, FNV-1a of the bytes after the
# preamble). The same table is in tests/test_av_soundprov.c.
GOLDEN = [
    ("MyWiFi", "12345678", "", 45, 0xB3400EC1),
    ("小米路由器_5G", "pässwd 密码!", "", 74, 0x47655DC8),
    ("ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "0123456789abcdef" * 4, "example.org:8097", 191, 0x1CD967C4),
    ("Guest", "", "", 32, 0xD9B5C29A),
]

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [src, job] = [fs.readFileSync(process.argv[2], "utf8"), JSON.parse(process.argv[3])];
const box = { TextEncoder, Math };
vm.runInNewContext(src + "\nthis.SoundProv = SoundProv;", box);
const SP = box.SoundProv;
function fnv(bytes) {
  let h = 0x811C9DC5;
  for (const b of bytes) h = Math.imul(h ^ b, 0x01000193) >>> 0;
  return h;
}
if (job.kind === "golden") {
  const out = job.vectors.map(([s, p, v]) => {
    const sym = SP.symbols(s, p, v);
    return [sym.a.length, fnv(sym.bytes), sym.a.slice(0, 6), sym.b.slice(0, 6)];
  });
  const errors = job.bad.map(([s, p, v]) => { try { SP.symbols(s, p, v); return null; } catch (e) { return e.field; } });
  console.log(JSON.stringify({ out, errors }));
} else {
  // One WAV: the lead-in silence and `repeats` copies of a repeat, as the page loops them.
  const sym = SP.symbols(job.ssid, job.pass, job.server);
  const once = SP.render(sym, job.rate);
  const lead = Math.round(SP.LEAD_S * job.rate);
  const n = lead + once.length * job.repeats;
  const buf = Buffer.alloc(44 + 2 * n);
  buf.write("RIFF", 0); buf.writeUInt32LE(36 + 2 * n, 4); buf.write("WAVE", 8);
  buf.write("fmt ", 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(job.rate, 24); buf.writeUInt32LE(job.rate * 2, 28); buf.writeUInt16LE(2, 32);
  buf.writeUInt16LE(16, 34); buf.write("data", 36); buf.writeUInt32LE(2 * n, 40);
  for (let r = 0; r < job.repeats; r++)
    for (let i = 0; i < once.length; i++)
      buf.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(once[i] * 32767))), 44 + 2 * (lead + r * once.length + i));
  fs.writeFileSync(job.out, buf);
  console.log(JSON.stringify({ samples: n, seconds: sym.a.length * SP.SYMBOL_S }));
}
"""


# The nearby-network list in the setup screen's code (main/av_wifi_hint.h):
# the same list and fragment as GOLDEN in tests/test_av_wifi_hint.c.
NEARBY_GOLDEN = ("1CL6NILR98PKKTPDGHVJR3CV8MUNUF55HSMCQH927ELIN6T0",
                 [("MyWiFi", 4, False), ("小米路由器", 3, False), ("Guest", 2, True)])

LIST_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [src, cases] = [fs.readFileSync(process.argv[2], "utf8"), JSON.parse(process.argv[3])];
const box = { TextDecoder, Uint8Array, Set };
vm.runInNewContext(src + "\nthis.parseNearby = parseNearby;", box);
console.log(JSON.stringify(cases.map((hash) => box.parseNearby(hash).map((n) => [n.ssid, n.bars, n.open]))));
"""


def nearby_fragment(nets) -> str:
    """An encoder written from the format description, independent of the C one."""
    import base64
    data = b""
    for ssid, bars, is_open in nets:
        raw = ssid.encode()
        data += bytes([(len(raw) - 1) | ((bars - 1) << 5) | (0x80 if is_open else 0)]) + raw
    return "1" + base64.b32hexencode(data).decode().rstrip("=")


def list_source() -> str:
    text = PAGE.read_text(encoding="utf-8")
    match = re.search(r"// BEGIN WIFI LIST\n(.*?)// END WIFI LIST", text, re.S)
    assert match, "list markers missing from wifi.html"
    return match.group(1)


def encoder_source() -> str:
    text = PAGE.read_text(encoding="utf-8")
    match = re.search(r"// BEGIN SOUNDPROV ENCODER\n(.*?)// END SOUNDPROV ENCODER", text, re.S)
    assert match, "encoder markers missing from wifi.html"
    return match.group(1)


class PageTests(unittest.TestCase):
    def test_page_cannot_send_anything(self):
        text = PAGE.read_text(encoding="utf-8")
        for word in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource",
                     "navigator.share", "<img", "<iframe", "<link", "src=", "action="):
            self.assertNotIn(word, text)
        csp = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', text)
        self.assertIsNotNone(csp)
        for rule in ("default-src 'none'", "connect-src 'none'", "form-action 'none'"):
            self.assertIn(rule, csp.group(1))
        self.assertNotRegex(text, r"https?://")

    def test_page_strings_are_chinese(self):
        text = PAGE.read_text(encoding="utf-8")
        for phrase in ("把音量调大，手机靠近电视", "显示密码", "发送", "清除"):
            self.assertIn(phrase, text)

    def test_nearby_list_is_on_the_page(self):
        text = PAGE.read_text(encoding="utf-8")
        for phrase in ("电视附近的 Wi-Fi", "parseNearby(location.hash)", "🔓", 'id="nets"'):
            self.assertIn(phrase, text)
        # The names are put on the page as text, never as markup.
        self.assertNotIn("innerHTML", text)

    def test_python_encoder_matches_the_golden_fragment(self):
        self.assertEqual(nearby_fragment(NEARBY_GOLDEN[1]), NEARBY_GOLDEN[0])

    def test_served_as_a_public_asset(self):
        sys.path.insert(0, str(ROOT))
        from server.relay import web
        kind, body = web.ASSETS["/wifi"]
        self.assertTrue(kind.startswith("text/html"))
        self.assertIn("声波配网".encode(), body)


@unittest.skipUnless(NODE, "node is not installed")
class NearbyListTests(unittest.TestCase):
    def parse(self, hashes):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "list.js"
            src.write_text(list_source(), encoding="utf-8")
            harness = Path(tmp) / "harness.js"
            harness.write_text(LIST_HARNESS, encoding="utf-8")
            done = subprocess.run([NODE, str(harness), str(src), json.dumps(hashes)],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
        return [[tuple(n) for n in parsed] for parsed in json.loads(done.stdout)]

    def test_golden_and_edge_cases(self):
        fragment, nets = NEARBY_GOLDEN
        longest = [("x" * 32, 1, False), ("😀" * 8, 4, True)]
        cases = ["#" + fragment, fragment, "#" + fragment.lower(), "", "#", "#2" + fragment[1:],
                 "#1!!", "#" + fragment[:-6], "#" + nearby_fragment(longest),
                 "#" + nearby_fragment([("A", 1, False), ("A", 3, True)])]
        got = self.parse(cases)
        expected = [nets, nets, nets, [], [], [], [], nets[:2], longest, [("A", 1, False)]]
        self.assertEqual(got, [[tuple(n) for n in e] for e in expected])

    def test_invalid_utf8_record_is_skipped(self):
        import base64
        bad = bytes([1, 0xC4, 0xE3]) + bytes([2]) + b"Net"   # GBK bytes, then "Net"
        fragment = "#1" + base64.b32hexencode(bad).decode().rstrip("=")
        self.assertEqual(self.parse([fragment]), [[("Net", 1, False)]])


@unittest.skipUnless(NODE, "node is not installed")
class EncoderTests(unittest.TestCase):
    def node(self, job: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "enc.js"
            src.write_text(encoder_source(), encoding="utf-8")
            harness = Path(tmp) / "harness.js"
            harness.write_text(HARNESS, encoding="utf-8")
            done = subprocess.run([NODE, str(harness), str(src), json.dumps(job)],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
        return json.loads(done.stdout)

    def test_same_symbols_as_the_c_mirror(self):
        answer = self.node({"kind": "golden", "vectors": [g[:3] for g in GOLDEN],
                            "bad": [["", "x", ""], ["A" * 33, "", ""], ["a", "p" * 65, ""],
                                    ["a", "", "s" * 64], ["一二三四五六七八九十十", "", ""]]})
        for (nsym, digest, pre_a, pre_b), golden in zip(answer["out"], GOLDEN):
            self.assertEqual(nsym, golden[3], golden[0])
            self.assertEqual(digest, golden[4], golden[0])
            self.assertEqual(pre_a, [3, 12, 0, 9, 15, 6])
            self.assertEqual(pre_b, [10, 1, 13, 4, 7, 14])
        # 11 Chinese characters are 33 bytes: too long, like 33 letters.
        self.assertEqual(answer["errors"], [0, 0, 1, 2, 0])

    @unittest.skipUnless(CC, "no C compiler")
    def test_page_audio_decodes_on_the_device_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "test_av_soundprov"
            subprocess.run([CC, "-std=c11", "-O2", f"-I{ROOT / 'main'}",
                            str(ROOT / "tests" / "test_av_soundprov.c"),
                            str(ROOT / "tests" / "soundprov_ref.c"), "-lm", "-o", str(binary)],
                           check=True, capture_output=True, timeout=120)
            cases = [
                (48000, GOLDEN[1][:3], ["--snr", "99"]),
                (44100, GOLDEN[0][:3], ["--snr", "0", "--drift", "0.004"]),
                (48000, ("TP-LINK_5A3F", "jiayou2024!!", ""), ["--snr", "-3", "--drift", "-0.005"]),
            ]
            for rate, (ssid, password, server), channel in cases:
                wav = Path(tmp) / f"page-{rate}.wav"
                self.node({"kind": "wav", "rate": rate, "repeats": 2, "out": str(wav),
                           "ssid": ssid, "pass": password, "server": server})
                done = subprocess.run([str(binary), "--wav", str(wav), *channel, "--seed", "7"],
                                      capture_output=True, text=True, encoding="utf-8", timeout=120)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                got = json.loads(done.stdout)
                self.assertEqual((got["ssid"], got["password"], got["server"]), (ssid, password, server))


if __name__ == "__main__":
    unittest.main()
