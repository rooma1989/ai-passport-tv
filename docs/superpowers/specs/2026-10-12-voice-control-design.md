<p align="right">
  <a href="2026-10-12-voice-control-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Voice control — design

Status: proposal, 2026-10-12. Nothing in this document is built. The mock-ups
were drawn on the host by a prototype (`tools/render_ui/voice_mock.c`) that
uses the firmware's own drawing code and font. Nothing has been run on a
device, and no figure below marked *estimate* has been measured.

## Goal

Hold OK on the little TV, say what you want, let go. The device does it:

- switch to a channel, a creator or a series (*Hunan Weishi*, *zhongyang wu tao* for CCTV-5, *Xiong Chumo di san ji* for episode 3 of a series).

Utterances are written in pinyin or translated in this file, because the
repository keeps the English documents free of Chinese characters; the
Simplified Chinese version quotes them as they are said and shown.
- change the volume, the backlight or audio-only mode; pause, resume, seek,
  next and previous episode;
- open the status page, or enter Wi-Fi setup.

The user asked for OK to be the trigger, and for the channel list and voice to
share **one panel** ("2-in-1"). The phone remote and the keys keep working
exactly as they do now.

## 1. Gestures and the 2-in-1 panel

### What the keys do today (both boards, from `process_key()`)

| While playing | Passport | Xingzhi |
| --- | --- | --- |
| OK click | channel list | channel list |
| OK double-click | status page | audio-only on/off |
| OK long press (1.5 s, `CONFIG_BUTTON_LONG_PRESS_TIME_MS=1500`) | backlight page | backlight page (**the only backlight gesture**) |
| ▲/▼ click / long / double | volume / channel in group / backlight step | volume / channel in group / status page |
| **Inside the list:** OK click / OK long | confirm / back a level, then close | same |

### Proposal

| While playing | New behaviour |
| --- | --- |
| OK click | Opens the list as today; the bottom bar now reads "hold OK to talk, double-click OK to go back" (mock 1). |
| **OK held ≥ 0.45 s** | **Push-to-talk:** the panel opens in its listening state (mock 2). Releasing the key ends the utterance. |
| OK double-click | Unchanged on both boards. |
| OK long press (1.5 s) | Not reachable any more: the hold has already started talking. Passport: no loss, because ▲▼ double-click still steps the backlight. **Xingzhi: the backlight page moves to a triple-click of OK** (mock 11), and the voice commands "brighter" and "brightness 3" also work. |

| Inside the panel | New behaviour |
| --- | --- |
| ▲▼ click | Move, as today. |
| OK click | Confirm, as today: open a group, switch channel, or pick a voice result. |
| **OK held** | **Talk again.** The result replaces the rows. |
| **OK double-click** | **Back a level**, then close at the top. This replaces "long press = back"; today a double-click does nothing in the list. |
| ▲ click while listening or recognising | Cancel. |

Every other page keeps its gestures: the status page, the pet picker, the
backlight page and setup. They still act on the 1.5 s long press, which the
BSP keeps reporting (see below). Talking is only possible from playback and
from the panel.

**Voice off means today's keys, bit for bit.** Voice is off when the firmware
is built with `CONFIG_AV_VOICE=n`, or when the server's CONFIG does not offer
voice (an older server, or one without speech). In that case the OK hold is
never acted on, and the 1.5 s long press still opens the backlight page on
both boards.

### Why 0.45 s, and why nothing is lost

The button component reports a click only 180 ms after release
(`CONFIG_BUTTON_SHORT_PRESS_TIME_MS`). That wait is how it can tell a single
click from a double, so a click already costs press time plus 180 ms. A
threshold of 0.45 s keeps deliberate clicks, which are typically under 0.25 s
(*estimate*), clear of holds.

Speech that starts before 0.45 s is still captured. **Capture starts at
BUTTON_PRESS_DOWN**, not at the hold: the device always records from the moment
OK goes down. A click throws those few hundred milliseconds away; a hold keeps
them. This has the same effect as Muse's continuous 120 ms pre-roll
(`muse_voice.c`), without keeping the microphone running all the time.

### BSP changes for the keys

- `bsp_btn_ev_t` gains three events:
  - `BSP_BTN_HOLD`: OK held for `BSP_OK_HOLD_MS`, 450 by default.
  - `BSP_BTN_RELEASE`: `BUTTON_LONG_PRESS_UP`, the end of a hold.
  - `BSP_BTN_TRIPLE`: `BUTTON_MULTIPLE_CLICK` with `clicks=3`.
- OK's `button_config_t.long_press_time` becomes 450. The legacy
  `BSP_BTN_LONG` is still reported at 1.5 s, either from a second
  `LONG_PRESS_START` callback registered with `press_time=1500` (the
  component supports several long-press times per key) or from the BSP
  comparing `iot_button_get_pressed_time()`.
- Arrows are unchanged, still 1.5 s.
- `on_key()` forwards the new events. Both boards use the same component: GPIO
  keys on the Xingzhi, the ADC ladder on the Passport.
- **Dependency:** the `passport-keys` branch (`BSP_KEYS_ABOVE_VIDEO`, session
  owner at priority 6) must be merged first. Without it, a key on the C3 can
  wait until a busy scene calms down, and push-to-talk would feel broken.

### Panel states (mock-ups)

- Xingzhi, 240×240: ![Xingzhi mock-ups](img/2026-10-12-voice-xingzhi.png)
- Passport, 320×240, laid out in the middle 240 columns like every other page:
  ![Passport mock-ups](img/2026-10-12-voice-passport.png)

Regenerate the mock-ups with `tools/render_ui/voice_mock.sh [--passport] out.png`.
The prototype is a separate program from `render_ui.sh`, and the firmware's
screens are unchanged.

| # | State | What is on screen |
| --- | --- | --- |
| 1 | List at rest (OK click) | Today's list. The bottom bar hint replaces the frame-rate and network-speed line (those stay on the status page and in the log). |
| 2 | Listening | Top bar shows a red dot and "listening". A microphone and live loudness bars come from the device's own level meter, not from the server. There are three examples of what to say. Ten cells along the bottom show the 10 s limit. Bottom bar: "release OK to finish, ▲ to cancel". |
| 3 | Recognising | How long was recorded, then "recognising". |
| 4 | One clear match | The words heard, in corner brackets, then "switching". The matched channel is shown as a selected row. It is carried out at once; 0.8 s later the usual channel banner takes over. |
| 5 | Several matches | Up to 6 candidates, picked with ▲▼ and OK. The last row, "none of these, open the channel list", goes to the normal list. |
| 6 | Series and episode | The episode rows, from the relay's episode list (branch `series-episodes`). |
| 7 | Not understood | The words heard, "didn't catch that", and examples. |
| 8 | Device setting (volume, backlight) | The panel closes; the usual level box appears, with the words heard in the top bar for 2 s. |
| 9 | Setup | Asks for confirmation first, because entering setup ends playback. |
| 10 | No session or no server support | Says so, and how to reach setup with the keys. |
| 11 | Backlight page | Unchanged. Xingzhi opens it with an OK triple-click; Passport keeps ▲▼ double-click. |
| 12 | Server error or timeout | "nothing recognised", with a retry hint. |

Layout:

- The panel header takes rows 36-87.
- A dotted rule sits at y=90.
- Up to four 24 px result rows start at y=96.
- Everything uses existing primitives: `ui_draw_panel`, `ui_draw_notched_rect`,
  `ui_overlay_key_line`, `ui_draw_cells`.
- The one new sprite is a 16×16 microphone, `UI_ICON_MIC`, added to
  `xiaozhi_tv_ui_assets.h`.
- Text is the existing 16 px GB2312 font. SenseVoice outputs simplified
  Chinese, and the corner brackets, the dot and the ellipsis are all in GB2312.

## 2. Audio path per board

| | Passport (ESP32-C3) | Xingzhi (ESP32-S3) |
| --- | --- | --- |
| Microphone | Through ES8311. The codec is already opened full duplex (`ESP_CODEC_DEV_WORK_MODE_BOTH`, `no_dac_ref=true`, input gain 30 dB), and I2S0's RX channel already runs at the TV's 16 kHz/16-bit. Capture is `bsp_audio_read()` on the existing channel: **no codec reopen and no glitch in the TV sound**. | Separate I2S1 RX, WS 4 / SCK 5 / DIN 6 (named in `xingzhi_cube_154.h`, unused today). The format follows Xiaozhi's board config: standard mode, 32-bit slot, left, 16 kHz, scaled to 16-bit. **Not yet verified on this board.** |
| BSP API | New `bsp_mic_start(hz)`, `bsp_mic_read(int16_t *, n, timeout)`, `bsp_mic_stop()` in both board folders. The Passport version reads the left slot of the existing RX channel. The Xingzhi version creates I2S1 on first start (DMA 6×240 frames) and disables it on stop. | (same) |
| Processing | 80 Hz one-pole high-pass, as in Muse; a level meter for the bars (peak per 40 ms); IMA ADPCM encode. | (same) |
| Encoding | **IMA ADPCM, 16 kHz**, in the same 640-sample block the TV already uses downstream (`AV_AUDIO_BYTES`=324 B per 40 ms). The upstream rate is 324 + 24 B header per 40 ms ≈ **8.7 KB/s**, a quarter of Muse's raw PCM (32 KB/s). Muse found raw 16 kHz PCM too heavy for the C3's heap over a 180 ms relay (commit `78f8f25`). 8 kHz is not used, because SenseVoice is trained at 16 kHz. | (same) |
| Streaming | **Streamed while speaking**, one packet per 40 ms block, over the existing TV socket (§6). The server therefore has the whole utterance about 50 ms after release (*estimate*). | (same) |
| Limit | 10 s, then the device ends the utterance by itself. Muse allows 15 s; commands are short. | (same) |

### The TV while talking

- **TV sound is muted on the device from OK press-down.**
  - There is no echo cancellation on either board, and the speaker sits a few
    centimetres from the microphone. SenseVoice would transcribe the
    programme.
  - The `audio_task` multiplies by 0 (the Xingzhi already scales in software;
    the Passport does the same rather than writing ES8311 registers from
    another task).
  - If the press turns out to be a click, the sound returns when the click is
    reported: a gap of about 0.3 s each time the list opens (*estimate*). If
    that is noticeable, duck by -20 dB until the hold instead of muting.
  - About 90 ms of sound already in the I2S DMA still plays out.
- **No decoding behind the opaque panel.**
  - The voice panel and the list cover rows 30-210 completely. While it is up,
    the `video_task` releases each stripe from the ring without inflating or
    drawing it.
  - Today the picture is still decoded under the list. That is pure waste,
    and on the C3 it is the part that saturates the CPU: 13-16 s of decode and
    panel work per 10 s in busy scenes at 140-165 kB/s (from the
    `passport-keys` commit).
  - The session, the socket and the receive task carry on. The receive task
    only copies about 110 kB/s into the ring (*estimate* 2-4 % of the C3,
    from the handoff's 20-36 cycles a byte).
- **The session is not torn down.** Voice travels on its socket, a channel
  command ends the session anyway, and for anything else the picture returns
  the moment the panel closes, without a redial.
- **Optional, phase 2:** VOICE begin also tells the media server to hold video
  packets (as for an audio-only session, at most 10 s), which saves the link
  too. Do this only if measurement shows the receive cost matters.

### Memory budget, Passport during playback

Allocated at OK press-down, freed when the panel closes. A viewer who never
talks pays nothing but code size.

| Item | Bytes |
| --- | --- |
| Uplink ring, 24 ADPCM blocks (0.96 s, covers TCP stalls and the press-to-hold pre-roll) | 7 776 |
| Capture chunk (20 ms) + PCM block accumulator | 640 + 1 280 |
| Voice task stack | 3 072 |
| Result: raw JSON (≤ 2 KB) + cJSON tree while parsing (*estimate* 3-4 KB) + up to 6 choices × 112 B | ≈ 6 000 peak, ≈ 700 kept |
| **Total** | **≈ 13 KB while listening, ≈ 19 KB peak while a result is parsed** |

The free heap during playback is about 71 KB (recorded in
`tcp-delta-progress-20260930.md`) to 85 KB (the user's observation). The
number that matters is the *minimum* free heap and the largest block, which
the session log prints (`min_heap`, `largest`) and which must be read during
voice use.

If the C3 turns out tighter than this, the uplink ring can borrow the video
ring's 16 KB while decoding is suspended. That is a fallback, not the plan,
because it couples two owners.

On the Xingzhi, internal RAM is not the constraint; its numbers will still be
logged.

### CPU, Passport

Capture, the high-pass filter and ADPCM encoding come to about 50 cycles per
sample × 16 000 ≈ 0.8 Mcycles/s, about 0.5 % of 160 MHz (*estimate*). The voice
task runs at priority 6:

- above video (5);
- beside the receive task and the session owner;
- below audio (7).

Before the panel appears (the first 0.45 s) the video is still decoding, and
the capture task must not starve during that time. I2S DMA holds 90 ms, which
is the slack.

## 3. Server

### Where speech is recognised

Reuse **muse-asr** (SenseVoice-Small int8 through sherpa-onnx, already running
on the cloud host as `muse-asr`, 127.0.0.1:8102, RSS about 550 MB). Do not
start a second model.

The media server is standard-library only, and muse-asr's protocol needs
`cryptography` (AES-GCM). The proposal is a small addition to muse-asr in the
muse-gadget-sdk fork:

- A **Unix-domain socket listener**, `/run/muse-asr/asr.sock`, mode 0660,
  group `tvserver`.
- It speaks a plain request (`POST /asr`, body PCM16 LE 16 kHz, answer
  `{"text": …}`).
- Authentication is the file's permission, so no key is shared with the TV
  server.
- The existing HTTP+AES path stays as it is for Muse.

The media server decodes the ADPCM (`server/adpcm.py` gains a decoder,
cross-checked against the device encoder) and posts the PCM.

**Latency** is not known. muse-asr logs upload and ASR milliseconds per
request, but no figures were recorded. Measure on the host before promising
anything. The target is ≤ 1.5 s from release to the action starting, channel
redial not included.

**Risk: muse-asr has one lock for ASR and TTS.** A Muse reply being
synthesised (VITS, 1-1.35× real time on the 1-vCPU host) blocks voice
recognition for seconds. Give ASR its own lock or executor in muse-asr.

**Live transcript** (phase 2): SenseVoice is not a streaming model. Partial
text means re-decoding the growing buffer about once a second. That is
affordable for 2-3 s commands only if one decode is well under a second on
the host, so measure first. Phase 1 shows live loudness bars instead and the
words once, after release.

### Intent engine: rules first

This is a new package `server/voice/`, standard library only, pure functions
with host tests.

- `intent.py`: transcript plus catalogue to a decision.
- `match.py`: the fuzzy matcher.
- `numbers.py`: Chinese numerals.
- `pinyin_table.py`: the pinyin data.

**Catalogue.** It is built per request from what the media server already
has:

- the channel table it sends in CONFIG (ids, names, the number the device
  shows, groups);
- from the relay's manage API (loopback, keyed), a short `GET /api/voice/catalog`:
  series names, episode counts, the playing series and episode, creators.

**Normalisation:**

- full-width to half-width, lower case;
- punctuation and filler words removed (*qing, bang wo, wo xiang, wo yao, gei wo, yixia, ba, a, ne*);
- Chinese numerals to digits (*san* → 3, *shi'er* → 12, *yibai* → 100; SenseVoice's ITN
  already gives digits most of the time).

**Channel matching:**

1. **Exact** name or id.
2. **Aliases** generated from names:
   - `CCTV-5` ↔ *zhongyang wu tao* / *yangshi wu tao* / *zhongyang 5 tai* / CCTV5 / *wu tao*;
   - `CCTV-5+` ↔ *wu jia* / 5 *jia*;
   - satellite channels with and without *weishi* (*Hunan* → Hunan Weishi when unique);
   - creators' names, series names.
3. **Number:** *di 12 tai*, *12 tai* or *pindao 12* is the number shown in the list.
4. **Group:** *weishi*, *yangshi*, *donghua* give the group's channels as candidates.
5. **Fuzzy:**
   - The score is the larger of the character LCS ratio and a *toneless pinyin*
     syllable edit-distance ratio, so ASR homophones (a wrong character for *wei* in Hunan Weishi) still
     match.
   - The pinyin table is generated once by `tools/make_pinyin_table.py` (uses
     `pypinyin` at development time only) for the 6 763 GB2312 characters, with
     every reading of polyphones (*chong/zhong*, *chang/zhang*, *le/yue*, *xing/hang*). That is about 60 KB
     of Python data.
   - **Auto-execute** when the top score is ≥ 0.85 and leads the second by
     ≥ 0.15.
   - **Candidates** (up to 6) when the top score is ≥ 0.5.
   - Otherwise **not understood**.
   - Thresholds are tuned on the test corpus.

**Grammar (examples, not exhaustive):**

| Intent | Phrases | Executed by |
| --- | --- | --- |
| Channel | (switch to / open / I want to watch / play)? X (channel)? | device (`request_switch`) |
| Channel step | next channel / previous channel / another channel | device (same as ▲▼ hold) |
| Series + episode | X episode N (*di N ji*); episode N (the playing series); X latest episode | relay `play_episode` (series-episodes branch) + device switch if not already there |
| Episode step | next episode / previous episode / replay this one | relay `next` / `previous` / seek to 0 |
| Pause / resume | pause / stop a moment / continue / carry on | relay (creator and series channels); for a live channel the answer is "a live channel cannot pause" |
| Seek | forward N seconds/minutes, back N, rewind | relay `seek` |
| Volume | louder / quieter (±10, "much louder" ±30), volume N (0-100; 0-10 means ×10), mute / unmute | device |
| Backlight | brighter / dimmer / brightness N | device |
| Audio-only | sound only / picture off / picture on | device (`set_audio_only`) |
| Status | status / how is the signal / how much battery | device opens the status page |
| Setup | set up the network (*peiwang*) / connect Wi-Fi / change network | device, after confirmation (mock 9) |
| Pet | change pet / change to X | device (opens the picker, or picks by name) |
| Cancel / help | never mind / cancel; what can you do / help | device closes; or shows examples |

**Who executes:**

- **The device does what it owns:** channel, volume, backlight, audio-only,
  pages, setup. The result carries a `do` object with exactly the keys
  `AV_SWITCH` already understands (`channel`, `volume`, `brightness`,
  `audio_only`, `pet`), plus new `page` and `setup`. The firmware reuses
  `take_switch()`. There is no one-second relay-event poll in this path.
- **The relay does what it owns:** which episode, pause, seek, next and
  previous. The media server calls the relay's manage API with an explicit
  `device` field.
  - The relay's control methods currently act on `self.target()`, the device
    the phone controls. A voice command must act on the device that spoke.
  - So each control endpoint gains an optional `device`, and the media server
    is told its own id (`TV_RELAY_DEVICE=Passport`, matching
    `--extra-device`).

**LLM fallback** (later, optional): when the rules say "not understood", send
the transcript and a compact catalogue (names only, about 2-4 KB) to an LLM
with a JSON-only answer schema, and validate the answer against the catalogue
before acting. Costs: a second or more of latency, an external service and
sending text off the host. Only the text goes, never audio.

### Replies

- **Phase 1: on-screen text and earcons only.** The earcons are a short rising
  tone when recording ends and a low tone for "not understood". They are
  generated in the `audio_task`, which owns I2S; no assets are needed.
- **Phase 3, TTS:**
  - VITS on the 1-vCPU host runs at 1-1.35× real time, so a fresh sentence
    takes as long to make as to hear.
  - Speak only replies that are not followed by a channel switch (the switch
    shows its own banner).
  - Pre-synthesise the fixed phrases and every channel and series name
    ("switching to Hunan Weishi") into a server-side cache when the channel table changes,
    so a reply costs nothing at request time.
  - The audio is sent as ADPCM blocks the device already decodes.

## 4. Wi-Fi setup by voice — what is honestly possible

Cloud recognition needs the network, and setup is exactly when the network is
missing. Options:

| Option | Works offline | Boards | Cost | Recommendation |
| --- | --- | --- | --- | --- |
| *peiwang* ("set up the network") while online → confirm → setup mode (same as status page → long ▲) | no | both | none beyond phase 1 | **Phase 1.** Useful for moving to a new network. |
| **Spoken guidance on the setup pages**: prerecorded clips ("scan the code on the screen with the phone's camera", "once joined, open the browser", "done, connecting") played as the pages turn | yes | both | about 6 clips × 4 s × 8 KB/s ≈ 200 KB of ADPCM in the app image (the Passport's `ota_1` has about 2.3 MB spare; the factory slot about 1.4 MB). Synthesised once with the existing VITS voices. | **Phase 2.** It addresses the real pain (not knowing what to do), with no microphone involved. |
| Offline command words (ESP-SR MultiNet: *peiwang*, "next channel", "louder", …) | yes | **Xingzhi only** | PSRAM must be enabled (the board has 8 MB octal PSRAM; the TV build keeps it off). Use `CONFIG_SPIRAM_USE_CAPS_ALLOC` so ordinary `malloc` and SPI DMA buffers stay internal, which was the reason for keeping it off. Needs an `srmodels` partition in the ~4.6 MB free after `cardid` (0x35A000-0x800000). The size of the chosen Chinese MultiNet model must be checked against that. MultiNet can load commands at run time, so channel names could be added from CONFIG, within its command limit. | Phase 4, optional, after push-to-talk is proven. |
| Data over sound (a phone page plays SSID and password as tones) | yes | both in principle | A phone page plus a decoder (e.g. ggwave). Unproven with these microphones; the QR flow already works with the system camera. | Not recommended now. |

## 5. Wake word

- **Xingzhi:**
  - Feasible with ESP-SR WakeNet. *Ni hao Xiaozhi*, the board's original Xiaozhi wake
    word, exists as a model; check the esp-sr model list.
  - It needs PSRAM (as above) and an always-on microphone and front end, which
    costs battery and part of one core (to be measured).
  - The hard part is **the TV's own sound**. With no echo cancellation, the
    programme causes misses and false wakes. The firmware has the playback PCM
    in hand, so it could feed it as the AEC reference, but the I2S delay must
    be calibrated.
  - **Phase 4, off by default.** A sensible first step is to listen for the wake
    word only in audio-only mode or while paused, when the speaker is quiet or
    the content is music at low level.
- **Passport:**
  - Push-to-talk only.
  - No PSRAM, no spare CPU while video plays, and ESP-SR's support for small
    chips is limited (newer esp-sr lists a small WakeNet for PSRAM-less chips;
    verify before relying on it).
  - Muse on the same board is push-to-talk too.

## 6. Protocol and modules

### Wire format (FAV1; the device struct comes first, per architecture-index §5)

| Kind | Direction | Payload |
| --- | --- | --- |
| `AV_VOICE_CTL = 10` | device → server | JSON. `{"op":"begin","id":n,"rate":16000,"codec":"adpcm"}`, `{"op":"end","id":n}`, `{"op":"cancel","id":n}`, `{"op":"pick","id":n,"ref":"…"}` (a candidate that needs a relay action) |
| `AV_VOICE = 11` | device → server | One 324 B ADPCM block (the downstream audio layout); `pts` = ms since begin; at most 250 per utterance |
| `AV_VOICE_RESULT = 12` | server → device | JSON ≤ 2 KB. `{"id":n,"state":"final"|"partial"|"error","heard":"…","reply":"…","do":{…}}` or `"choices":[{"label":"CCTV-5 …","lead":"05","do":{"channel":"ch5"}}, … ≤ 6]`; a choice may carry `"ref"` instead of `do` |

- **Capability.**
  - HELLO gains `"voice":1`.
  - CONFIG answers `"voice":{"max_s":10}` only when the media server has an
    ASR socket configured (`TV_ASR_SOCKET`).
  - Old firmware never sends the new kinds.
  - On an old server, OK hold shows mock 10.
- **The media server's loops.** `_device_left()`, `_wait_until()` and the pause
  hold accept only END or STATUS today. They take the new kinds through one
  `_take_upstream()`.
  - Audio is appended to a per-session buffer, capped at 10 s.
  - On `end`, a worker thread does ASR, intent and any relay call, so the send
    loop never blocks on it.
  - The result is queued, and the loop sends it like a SWITCH.
- **Device side.**
  - The receive task owns the socket. It sends `AV_VOICE*` from the voice ring
    between packets, as it already does for STATUS; the voice task never
    touches the socket.
  - A result is copied under a lock with a flag; `pump_keys()` applies it in
    the session owner, as for remote switches.
- **Separate HTTPS connection (Muse's way): rejected for the TV.**
  - A second socket and AES (or TLS: mbedTLS needs tens of KB of heap) on a C3
    with about 70 KB free during video.
  - Its own key in the firmware.
  - The device would have to execute intents itself without the series data.
  - The TV socket is already authenticated (pairing token) and carries every
    other control.

### Firmware modules

| Module | New or changed |
| --- | --- |
| `components/bsp/src/{passport,xingzhi_cube_154}/bsp_mic.c`, `include/bsp_mic.h` | new: start/read/stop; Xingzhi mic pins in its board header |
| `bsp_button.c` (both), `bsp_button.h` | HOLD / RELEASE / TRIPLE; OK long-press time per key |
| `main/av_voice.c/.h` | new: voice task (capture, high-pass, level, ADPCM, ring), allocation at press, teardown |
| `main/av_voice_policy.c/.h` | new, pure, host-tested: IDLE → ARMED (press) → LISTENING (hold) → SENDING → WAITING → RESULT / CHOOSING → IDLE; click discards; 10 s cap; 8 s result timeout; cancel; what each key does in each state |
| `main/av_adpcm.c/.h` | add the encoder; host test round trip and cross-check with `server/adpcm.py` |
| `main/av_protocol.h` / `server/protocol.py` | three kinds, limits; the existing constant cross-check test covers them |
| `main/ui_menu.c/.h` | voice sub-state of `UI_VIEW_MENU` (header, choices, timeouts); double = back |
| `main/ui_overlay.c/.h`, `ui_draw.c`, `xiaozhi_tv_ui_assets.h` | voice header and rows, `UI_ICON_MIC`; the bottom hint |
| `main/av_player.c` | key routing, mute flag in `audio_task`, skip-decode in `video_task` while the panel is opaque, receive-task send/parse, `CONFIG_AV_VOICE` |
| `main/Kconfig.projbuild` | `AV_VOICE` (default y on both boards once verified), `AV_VOICE_HOLD_MS` |

### Server modules

| Module | New or changed |
| --- | --- |
| `server/voice/{intent,match,numbers,pinyin_table,session,asr}.py` | new (see §3) |
| `tools/make_pinyin_table.py` | new, development-time only |
| `server/adpcm.py` | add a decoder |
| `server/tv_server.py` | upstream kinds, worker thread, CONFIG `voice`, `TV_ASR_SOCKET`, `TV_RELAY_DEVICE` |
| `server/relay/web.py` | optional `device` on control endpoints; `GET /api/voice/catalog` |
| muse-gadget-sdk `tools/muse_asr/server.py` | Unix socket listener; ASR not serialised behind TTS |
| `packaging/systemd/*` | socket directory and group; muse-asr's `MemoryMax` unchanged |

**Privacy:** audio is never stored. The log records the intent kind and the
latency, and the transcript only with `TV_VOICE_LOG_TEXT=1`.

## 7. Tests

- **Host, C (`tools/validate.sh --static`):**
  - the voice policy state machine: every key in every state, click versus
    hold at the threshold, cancel, timeouts, a result arriving after cancel;
  - the ADPCM encoder (round trip, and bytes equal to the Python encoder's for
    a fixed vector);
  - `ui_menu` voice states;
  - a check that `voice_mock`'s successor screens render.
- **Host, Python:**
  - `tests/test_voice_intent.py`: a table of about 200 utterances to expected
    decisions, against a fixture channel table and series catalogue.
    Homophones, numbers (*zhongyang wu tao*, *di shi'er ji*, 5 *jia*), aliases, ambiguity,
    "not understood".
  - `tests/test_voice_session.py`: packet sequences, the 10 s cap, cancel, ASR
    timeout and error, with the ASR stubbed.
  - Protocol constant cross-check.
- **Corpus:**
  - A development-only server option saves utterances as WAV (off by
    default).
  - Record about 50 real commands per board.
  - `tools/voice_eval.py` replays them through ASR and intent offline. This
    gives transcript accuracy and intent accuracy per board's microphone.
- **On the device** (each board, from an erased device for the public build
  as AGENTS.md requires):
  - **Timing:** hold-to-listening-UI under 100 ms; first syllable not lost;
    TV muted while holding; click still opens the list with only the short
    sound gap.
  - **Other gestures:** double-click and triple-click still work; Xingzhi
    backlight by triple-click.
  - **Passport performance:** frame rate and key latency during and after
    voice in a busy scene.
  - **Memory:** minimum free heap and largest block during voice, logged;
    20 commands in a row with a stable heap.
  - **Other states:** voice in audio-only, paused and waiting states; an old
    server; old firmware against a new server.
  - **Muse-dual:** double OK on the status page still returns to Muse.

## 8. Phased plan

Effort is working days of agent implementation, with the user's device checks
in between.

| Phase | Scope | Effort |
| --- | --- | --- |
| 0 | Merge `passport-keys`. Measure SenseVoice latency on the host. Bring up each board's microphone with a raw capture dump (Xingzhi's format is unverified). | 1-2 |
| **1** | Push-to-talk on both boards, the 2-in-1 panel, in-band protocol, muse-asr Unix socket, rule intents (channel, group, number, series + episode, volume, backlight, audio-only, pause/resume, seek, next/previous, status, setup), on-screen replies and earcons, Xingzhi backlight on triple-click, tests | 14-17 |
| 2 | Live partial transcript (if ASR is fast enough), spoken setup guidance (prerecorded), server-side video hold while listening (if measured useful), corpus-driven threshold tuning | 5-7 |
| 3 | TTS replies with a per-name cache; optional LLM fallback | 4-6 |
| 4 | Xingzhi: PSRAM on, ESP-SR WakeNet (audio-only or paused first) and MultiNet offline words incl. *peiwang* | 8-12, highest risk |

## 9. Risks

- **Microphones.**
  - The Xingzhi's I2S1 microphone has never been used by this firmware, so its
    format and gain are unverified.
  - The Passport's microphone is proven in Muse at 30 dB gain, but not in a
    room with the TV just muted.
- **Recognition.**
  - Channel names are hard for ASR: digits in CCTV-N, Latin abbreviations,
    homophones.
  - The pinyin matcher and aliases are the mitigation; the corpus measures it.
- **The C3.**
  - Heap fragmentation at result parse while a CONFIG is also live: keep
    results ≤ 2 KB.
  - A key-latency regression if the priorities are wrong.
- **The host.**
  - One vCPU shared by two media servers' ffmpeg, the relay, and SenseVoice;
    a recognition burst may disturb a stream.
  - muse-asr's single lock (§3).
- **Gestures.** The panel's back gesture moves from a long press to a
  double-click, and the Xingzhi's backlight from a long press to a
  triple-click. Users must relearn both; the bottom-bar hints say so.
- **Privacy.** Voice goes to the user's own host, in clear text on the TV
  socket like everything else on it (the pairing token keeps scanners out but
  is not encryption).

## 10. Questions for the user

1. **Xingzhi backlight.** Is a triple-click of OK the right replacement, plus
   the voice command? The alternative is a "brightness" row in the status page.
2. **In the panel, is double-click OK = back acceptable?** The alternative is
   long ▲ = back.
3. **Hold threshold.** Is 0.45 s right?
4. **Clear matches.** Carry them out at once (proposed), or always show a
   0.8 s confirmation that can be cancelled?
5. **TV sound on press.** A ~0.3 s gap each time the list opens with a click:
   acceptable, or duck instead of mute?
6. **Replies.** Are on-screen replies and earcons enough for phase 1, or is
   speech (TTS) wanted early?
7. **Phase 4.** Is it wanted at all (PSRAM, wake word, offline words on the
   Xingzhi)? It changes the Xingzhi's memory configuration.
8. **Logging.** May transcripts be logged on the host while thresholds are
   being tuned?
