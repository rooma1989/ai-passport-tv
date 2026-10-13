<p align="right">
  <a href="2026-10-13-sound-provisioning-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Sound-wave Wi-Fi setup — design

Status: the modem is written and tested on a computer only: the decoder
library (`main/av_soundprov.c`), the phone page (`/tv/wifi`) and a host
simulation of the path between them. Wiring the decoder into the device's setup
mode is separate work. Nothing has been played to a real device yet, and no
iPhone has played the page. Date: 2026-10-13.

## Goal

Setting up Wi-Fi today means: the device raises a hotspot, the phone joins it,
a captive page opens, the user picks a network, types the password, maybe a
server address, and the phone switches back. The new first-time flow is:

1. Open a page on the phone (iPhone Safari, no app): `https://<site>/tv/wifi`,
   or the QR-friendly `https://<site>/TV/W`.
2. Type the Wi-Fi name and password once; the phone remembers them.
3. Hold the phone near the TV in setup mode, tap "Send" (labelled in Chinese). The TV hears it
   and connects.

The password never leaves the phone over the network: the page has no network
code at all, and its Content-Security-Policy (`connect-src 'none'`,
`form-action 'none'`) makes the browser enforce that.

## Why audible tones

The device has a microphone and, in setup mode, nothing else to do with it.
Sound needs no pairing, no app and no permission on the phone, and works when
the phone is on mobile data. The modem follows the idea of
[ggwave](https://github.com/ggerganov/ggwave)'s audible protocols (MIT
licence): several simultaneous tones per symbol, a known start marker, and
Reed–Solomon over GF(256). No ggwave code is used; the framing, timing recovery
and receiver are this project's own, written for a 16 kHz integer-only decoder.

## On the air

Everything below is at the device's 16 kHz sample rate; the phone renders the
same signal at whatever rate its audio runs (48 or 44.1 kHz).

| Item | Value |
| --- | --- |
| Tones | 32: 1250 + 125·k Hz, k = 0..31 (1250–5125 Hz). Phone speakers are weak below ~800 Hz; the device's codec filters above ~7 kHz. |
| Bands | Band A: k = 0..15 (1250–3125 Hz) carries the high nibble; band B: k = 16..31 (3250–5125 Hz) the low nibble. |
| Symbol | 48 ms, two tones at once (one per band) = one byte. Each tone at 0.45 of full scale, 3 ms raised-cosine fade at both ends. |
| Preamble | 6 symbols, A = 3, 12, 0, 9, 15, 6 and B = 10, 1, 13, 4, 7, 14 (all neighbours different). |
| Header | 5 symbols: payload length L, plus 4 Reed–Solomon parity bytes (corrects 2 wrong bytes). |
| Payload codeword | L payload bytes + 2·⌈L/4⌉ (at least 8) Reed–Solomon parity bytes: corrects one wrong symbol in four, or one in two when the decoder knows which (erasures). |
| Whitening | Byte j after the preamble is XORed with `((j + 1) · 0x9E3779B1) >> 24` (32-bit), so a password like `88888888` does not repeat tones, which the timing loop needs. |
| Reed–Solomon | GF(256), polynomial 0x11d, generator roots α^0..α^(n−1), systematic, first byte = highest coefficient. |
| Repeats | The page plays 0.15 s of silence, then the message up to 6 times with 0.3 s of silence after each (or until "Stop" is tapped). |

Payload, version 1 (length-prefixed):

```
0x01 | ssid_len | ssid (1..32 bytes) | pass_len | password (0..64) | server_len | server (0..63) | CRC-32
```

Strings are UTF-8 as typed (Chinese SSIDs work; 11 Chinese characters are 33
bytes and do not fit, the page says so). CRC-32 is the IEEE one (as in zlib),
little endian, over everything before it. The decoder refuses a payload whose
CRC, version or lengths do not agree, an empty SSID, or a NUL inside a field.
The server field is empty in normal use; a device that receives one is meant to
use it as its server address.

Durations (one repeat, without the 0.3 s gap):

| Message | L | Symbols | Time |
| --- | --- | --- | --- |
| `MyWiFi` / `12345678` | 22 | 45 | 2.16 s |
| 12-byte SSID / 12-byte password (typical) | 32 | 59 | 2.83 s |
| An 18-byte Chinese SSID / a 15-byte password with Chinese | 41 | 74 | 3.55 s |
| 32-byte SSID / 64-byte password / 16-byte server | 120 | 191 | 9.17 s |
| Longest possible (32 / 64 / 63) | 167 | 262 | 12.6 s |

## The receiver (`main/av_soundprov.c`)

1. **Tone bank.** Every 64 samples (a 4 ms "hop") the hop is correlated with
   the 32 tone frequencies using a 256-entry Q14 cosine table. Every tone is a
   whole number of 62.5 Hz bins, so the phase at a hop's first sample is
   `64 · (m · hop mod 4)` and four consecutive hops add up to an exact
   256-point DFT bin (a 16 ms window). Integers only; the C3 has no FPU.
2. **Shares.** For each 16 ms window, each tone's share of its band's energy
   (0..255) goes into a ring of 128 windows (512 ms). Shares make the receiver
   indifferent to volume and to a speaker that is louder in one band, and noise
   between the tone frequencies counts for nothing.
3. **Preamble.** At every hop the six known tone pairs are matched against the
   ring. A symbol is read from two windows in its middle (2 hops = 8 ms of
   guard each side, which also absorbs reverberation), so the match is flat for
   ±2 hops. It needs a score of 28 % of perfect *and* the preamble's tone loudest
   in its band in at least 7 of the 12 places; the second test is what keeps
   speech, music and stretches of payload from starting a reception. The start
   is the middle of the plateau, refined by the edge measurement below.
4. **Timing.** Each symbol's tone is also measured in the windows straddling
   its two edges: if the symbol really started later, more of its tone is in
   the late edge window. After every symbol a quarter of that error is applied
   (only for bands whose neighbours used a different tone). This follows clock
   differences well beyond ±0.5 % and loses of up to 20 ms of capture.
5. **Decoding.** After the header it knows L and the symbol count. At the last
   symbol it tries Reed–Solomon with no erasures and then with ¼, ½, ¾ and all
   but two of the parity's worth of the least certain symbols erased, first on
   this repeat alone, then on the running average of the tone shares of all
   repeats of the same length (a moving average after four). Only a
   CRC-correct, well-formed payload is accepted. If a repeat's header is lost,
   the previous repeat's length is assumed.

Interface (fixed, used by the setup-mode integration):
`av_soundprov_create/destroy/reset`, `av_soundprov_feed(d, pcm, n)` (16 kHz mono
int16, any chunk size; returns 1 once decoded and stays 1 until reset),
`av_soundprov_progress(d)` (0 idle, 1–99 while a message comes in, 100 done) and
`av_soundprov_result(d, &out)`.

## Cost

| | |
| --- | --- |
| RAM | One `malloc` of 14 944 bytes (checked ≤ 16 KB by a static assertion and the host test); stack under 1 KB. Nothing when not created. |
| Flash | 8.4 KB of code and tables (`size` of the ESP32-C3 object). |
| CPU, host | 0.62 ms per second of audio (Apple Silicon, `-O2`, `test_av_soundprov --bench`). |
| CPU, ESP32-C3 estimate | The per-sample inner loop compiles to 21 RV32 instructions per tone-sample (2 multiplies, 3 loads); 32 tones × 16 000 samples ≈ 11 M instructions/s, so about 12 M cycles/s with the per-hop work: roughly 7–8 % of the 160 MHz core. Not measured on a device. |

## Simulated results

`tests/test_av_soundprov.c` renders the page's signal with a C mirror of its
encoder (`tests/soundprov_ref.c`) at 48 kHz and passes it through: a phone
speaker (4th-order high-pass at 700 Hz, low-pass at 6.5 kHz), a windowed-sinc
resampler to 16 kHz that also applies the clock difference, optionally a room
(a reflection 3 ms later and a 0.4 s Schroeder tail) and an uneven speaker
(+6 dB at 2.8 kHz, −10 dB at 4.4 kHz), then noise at a given SNR (over the full
0–8 kHz band, measured against the signal while it plays), a random start offset
of 0.2–1.7 s, 16-bit rounding and random chunk sizes. In every repeat one
payload symbol is sent with other tones and another is not sent at all.

Typical message (12-byte SSID, 12-byte password), 40 trials per cell, % decoded
after 1 / 2 / 3 repeats, white noise:

| SNR | drift −0.5 % | drift 0 | drift +0.5 % |
| --- | --- | --- | --- |
| −15 dB | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| −12 dB | 0 / 0 / 0 | 12 / 37 / 47 | 0 / 0 / 0 |
| −10 dB | 70 / 95 / 100 | 100 / 100 / 100 | 75 / 95 / 97 |
| −8 dB and above | 100 / 100 / 100 | 100 / 100 / 100 | 100 / 100 / 100 |

Room + uneven speaker + pink noise, drift +0.3 %: −12 dB 2 / 55 / 87, −10 dB
72 / 100 / 100, −8 dB and above 100 %. Longest message with a server (191
symbols), drift +0.5 %: −10 dB 80 / 100 / 100, −8 dB and above 100 %. Capture
losing 5, 10, 15 or 20 ms in the middle of every repeat: 100 % after one repeat
(SNR 6 dB); 30 ms: 17 % (half a symbol is gone and the timing slips by a whole
symbol, which no code can repair). A wrong message was never accepted (0 in
1 820 simulated trials), and a minute each of noise, random chords on the modem's
own tones in step with its symbols, and a synthetic voice started no reception
at all.

For scale: a phone at full volume 10–50 cm from the microphone is tens of dB
above a quiet room, so the cliff at about −10 dB (full band; about +8 dB in a
single tone's 62.5 Hz bin) leaves a wide margin. The page's own JavaScript has
also been run (under node) to make WAV files at 48 and 44.1 kHz, which the
decoder reads through the same simulated channel (`tests/test_soundprov_page.py`).

## The page

`server/relay/assets/wifi.html`, served by the relay without sign-in at
`/tv/wifi` (nginx `/tv/` → relay `/wifi`), at `/TV/W` (an nginx exact location,
and the relay answers it directly too), self-contained, no external scripts.
It keeps the SSID, password and server in `localStorage` (`tvwifi.ssid`,
`tvwifi.pass`, `tvwifi.server`) when "remember on this phone" is ticked, and has "show password",
"forget the Wi-Fi remembered on this phone", and an "Advanced" section for the
server address (all labels in Chinese). Sending
creates the WebAudio context inside the tap (iOS requires it), sets
`navigator.audioSession.type = "playback"` where available (iOS 16.4+) so the
silent switch does not mute it, and loops one rendered repeat up to six times
with a progress bar and "turn the volume up, hold the phone near the TV".

## Tests

- `tests/test_av_soundprov.c` (in `./tools/validate.sh --static`): CRC-32 known
  value, Reed–Solomon with random errors and erasures up to the code's limit,
  payload parser refusals, golden symbol hashes, clean loopback at loud and very
  quiet levels with progress and sticky result, no message from noise or
  chords, and six short channel cases (drift ±0.5 %, room, glitch). Modes
  `--sweep`, `--bench`, `--false`, `--golden`, and `--wav FILE [--snr dB]
  [--drift d]` to decode any recording.
- `tests/test_soundprov_page.py`: the page's encoder under node gives the same
  symbols as the C mirror; WAVs it renders decode; the page has no network code
  and a strict CSP; the asset is registered. Node and compiler cases skip
  themselves when those tools are missing.
- `tests/test_relay_web.py`: `/wifi`, `/tv/wifi`, `/TV/W` answer without sign-in.

## Not yet known

- Real speakers and microphones: the AI Passport's ES8311 input gain and the
  Xingzhi's I2S MEMS microphone format are unverified; a wrong format or a
  sample rate that is not really 16 kHz (beyond ±0.5 %) will not decode.
- iPhone behaviour: output level at full volume, whether the first repeat is
  clipped while the audio route starts, and the silent switch on iOS older than
  16.4.
- The integration must feed the decoder continuously: losing more than about
  20 ms of capture in one go costs that repeat.
