English | [简体中文](local-tv-prototype.zh_CN.md)

# Standalone local audio/video prototype

This optional boot entry plays live or pre-generated 160x120 baseline YUV420 JPEG and
16 kHz mono IMA ADPCM audio (decoded to s16le PCM on the device) over one authenticated LAN TCP connection. The server's
`live` subcommand transcodes a channel in real time; `run` replays prepared media.
The channel list and the current channel are always chosen by the server, and the
allowlisted addresses are unverified third-party relays. The original
menu, demos and pixel theme remain unchanged. `CONFIG_AV_RAW_PROTOTYPE` defaults
to disabled; raw mode does not start LVGL or BLE. This is not an Internet URL
proxy or a production player. TCP/token pairing is not encryption; use a trusted
LAN only.

## Private configuration and builds

Create **only locally**, with mode `0600`, the ignored `main/av_private_config.h`.
Define `AV_WIFI_SSID`, `AV_WIFI_PASSWORD`, `AV_SERVER_IPV4` (an IPv4 literal),
`AV_SERVER_PORT` (integer, normally 8096), and `AV_PAIRING_TOKEN`. Do not paste
values into tracked files, logs or commands. No private header means compilation
still succeeds and raw boot reports missing configuration without networking.
The application never erases NVS and configures Wi-Fi storage as RAM.

Activate ESP-IDF 5.5.3, then run from the repository:

```bash
./tools/validate.sh --static
./tools/validate.sh --firmware   # original demo, public/no private header
./tools/validate.sh --prototype  # RAW overlay, public/no private header

# Actual local device image; contains secrets, do NOT publish build-av files.
idf.py -B build-av -D SDKCONFIG=build-av/sdkconfig \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.av-prototype' \
  -D AV_PUBLIC_BUILD=OFF build
idf.py -B build-av merge-bin -o build-av/FoloToy-AI-Passport-full.bin
python3 tools/verify_firmware.py build-av
```

The public validation modes explicitly set `AV_PUBLIC_BUILD=ON` so they never
include a developer's private header. Public raw firmware is a compile/layout
check and reports missing configuration at boot. `--prototype` writes its public
image to `build/FoloToy-AI-Passport-prototype-public.bin`, not the demo image.
Private `build-av` retains the segmented bootloader, partition table and app for
an authorized operator. Flash is never performed by these commands. Preserve the
8 MB/3 MB-app layout and `cardid@0x356000`; take two identical complete backups
before any provisioned-device write. Never erase the device or publish private
merged images. Restore demo mode with a separate default build, not by deleting
the original UI.

## Wire contract

The fixed 24-byte network-order header is Python `!4sBBHIIII`: `FAV1`, version 1,
type, zero flags, session, global sequence, PTS milliseconds, payload length.
HELLO (1) has header session/seq/PTS zero and JSON `{token,version:1}`, plus an
optional `channel` naming the channel to play (omit or send an unknown id to get
the server default). CONFIG (2) has a nonzero session, seq/PTS zero, and matching
JSON session. Required values: width 160, height 120, fps 12, sample_rate 16000,
channels 1, sample_bits 16, audio_codec "ima_adpcm", audio_chunk_ms 40,
video_max_bytes 24576. `sample_bits` is the decoded sound; `audio_codec` says what
travels, and a CONFIG without it (a server still sending PCM) is refused with the
field named in the log. Optional
`start_delay_ms` must be 200; additional server timing fields are tolerated.

`channels` is the audio channel count and must stay a number. The selectable
channel list travels in the separate `channel_list` array of `{id,name}` objects,
which the device steps through on UP/DOWN clicks. A server that omits it leaves
the device on its current channel. Keep the two keys distinct: sending the list
as `channels` fails the device's CONFIG validation and ends every session. The startup target is CONFIG
receipt +200 ms, but audio reset/prebuffer can delay it (see estimated clock).

AUDIO (3) is exactly 324 bytes per 40 ms: one IMA ADPCM block of 640 samples
(16 kHz mono), against the 1280 bytes the same sound takes as PCM. The block is a
four-byte header followed by 320 bytes of samples:

```text
[s16 predictor, big-endian][u8 step index 0..88][u8 zero][320 bytes: 640 x 4-bit codes]
```

The header is the decoder state *before the first sample*, not the first sample, so
all 640 samples are coded and every block decodes without the one before it. A
continuous encoder therefore writes, as each block's header, the predictor and
index it holds when that block starts. Two codes share a byte with the **earlier
sample in the low nibble**. Each code is the standard IMA one -- bit 3 the sign,
bits 0-2 the magnitude; step table of 89 entries from 7 to 32767; index change
-1,-1,-1,-1,2,4,6,8 by magnitude; `diff = step>>3` plus `step`, `step>>1`,
`step>>2` for magnitude bits 2, 1, 0; predictor and index both clamped. A step
index above 88 or a non-zero fourth header byte ends the session. Because the
nibble order and header differ from a WAV IMA block (which stores sample 0 in the
header and so holds 641 samples in 324 bytes), a stock `adpcm_ima_wav` stream is
not this format; `audioop.lin2adpcm` packs the high nibble first, so swap the
nibbles of each byte to use it as a reference.

VIDEO (4) is a complete baseline YUV420 JPEG,
1..24576 bytes; the ROM validates actual JPEG geometry/subsampling. END (5) has
zero payload. ERROR (6) and other control JSON are at most 1024 bytes. Bad magic,
version/type/flags, sizes, session, sequence, config, JPEG or truncated TCP input
terminates the session; there is no magic-search recovery. Before recursive JSON
parsing, a linear string/escape-aware scan limits nesting depth to four. A pre-CONFIG ERROR is
a terminal handshake failure, not accepted media. Remote JSON is never logged.
Sequences increase globally, but media PTS is checked independently: audio starts
at zero and advances by 40; video strictly increases and may skip frames. Video
PTS can be lower than an immediately preceding audio PTS. Loops must maintain
continuous PTS/sequence or start a new session; `duration_ms` describes server
material, not a firmware-enforced session timeout.

### Voice (push-to-talk), kinds 10-12

Firmware built with `CONFIG_AV_VOICE` adds `"voice":1` to HELLO. Voice is turned
on only by a CONFIG carrying `"voice":{"max_s":N}` (N >= 1; the device caps an
utterance at min(N, 10) s); a CONFIG without it turns it off, and the keys are
then exactly what they were. The authority for the numbers is
`main/av_protocol.h` (`AV_VOICE_CTL`, `AV_VOICE`, `AV_VOICE_RESULT`,
`AV_VOICE_RESULT_MAX`, `AV_VOICE_BLOCKS_MAX`); `server/protocol.py` mirrors them
and a host test compares the two.

| Kind | Direction | Header | Payload |
| --- | --- | --- | --- |
| VOICE_CTL (10) | device to server | session = the session's, seq 0, PTS 0 (like STATUS) | JSON: `{"op":"begin","id":n,"rate":16000,"codec":"adpcm"}`, `{"op":"end","id":n}`, `{"op":"cancel","id":n}`, `{"op":"pick","id":n,"ref":"..."}` |
| VOICE (11) | device to server | seq 0, PTS = 40 x the block's index from the utterance's first block | exactly 324 bytes: one IMA ADPCM block in the AUDIO layout above, 640 samples of 16 kHz microphone audio; at most 250 per utterance |
| VOICE_RESULT (12) | server to device | the session's next seq, PTS 0, flags 0 (like SWITCH) | JSON, 1..2048 bytes |

- `id` counts utterances from 1 per boot. "begin" goes when OK has been held for
  `CONFIG_AV_VOICE_HOLD_MS` (450 ms), but capture starts at OK press-down, so the
  first blocks after "begin" are the pre-roll recorded before it (block 0 is the
  press). The encoder runs continuously over the utterance and every block
  carries its own state, so a block lost to a full uplink ring (`dropped=` in the
  log; the PTS then skips) does not disturb the next.
- "end" follows the last block, about 120 ms after OK is let go. "cancel" (▲
  while listening or recognising, or a new press that abandons an unfinished
  utterance) drops whatever is unsent; a "begin" that never left the device is
  simply forgotten. "pick" answers a candidate that carries `ref`; the server
  answers it with a result for the same `id`.
- Only the receive task writes the socket. It sends these between its own
  reads: at the top of each packet, while it waits for an audio slot, and, while
  an utterance is in flight, in 20 ms slices before a header read, so a quiet
  stream (paused, audio only) does not hold the uplink up. A failed or partial
  write ends the session, like any other.
- A result is read into the control buffer, parsed into a fixed structure
  (no transcript is logged) and handed to the session owner. One whose `id` is
  not the utterance being waited for (a cancel, a newer one) is dropped.
- Fields used: `state` (`final`, `error`; `partial` would update the words while
  recognising, not sent by the server in phase 1), `heard`, `reply`, `intent`
  (`none` is "not understood": the low earcon), `label` and `lead` (the row shown
  for a single match), `do` with one of `channel`, `volume`, `brightness` (step
  index), `audio_only`, `pet`, `page` (`status`, `close`; the device also accepts
  `list`, `brightness`, `pets`) or `setup`, and `choices` (up to 6
  `{label, lead, do | ref}`). A clear match is carried out at once through the
  same paths as AV_SWITCH; no `do` and no `choices` shows `reply` (the relay
  acted) unless `intent` is `none`. "None of these, open the channel list" is
  added after the candidates only when they are channels.

### Episode information ("info": 1)

Firmware that draws which episode plays and loads adds `"info":1` to HELLO. Only
such a device gets anything below; older firmware sees exactly what it did.

- CONFIG may carry `"playing":{"title":"...","episode":n,"total":N}` for a relay
  channel (a series, a creator, the cast): what this session opens. `episode`
  and `total` are present only for a series (1-based; `total` only when known).
  The title is UTF-8, at most 60 bytes, cut on a character by the server; the
  device keeps 63 bytes and cuts on a character itself. The opening screen shows
  the title where the list position was and "Opening, episode n/N" under the
  bar; the channel banner shows "Episode n/N · title" in place of
  "group · n/total" (on screen in Chinese, as everywhere). Absent (a live
  channel, an older server) means nothing is known.
- `AV_SWITCH {"loading":{"title","episode","total"}}`: the source is about to
  change inside the session (next, previous episode, an episode picked on the
  phone or by voice, an episode that ended). The device draws a band across
  y=84..155 over the picture -- the "next" mark, "Loading episode n/N", the title --
  and, once the picture has stopped for 1.5 s, the channel's waiting screen with
  "Loading" instead of "Opening". A "next" box (`{"next":1}`) is not drawn while
  it is up.
- `AV_SWITCH {"loading":0}`: the new source is ready and its picture comes next.
  The loading becomes what plays, the channel banner names it, the band is no
  longer drawn over pictures (the new source starts with a whole frame) and the
  waiting screen keeps it until the next picture is drawn.
- Never stuck: the band goes 30 s after the last `loading` without a `0`, 3 s
  after the `0` without a picture (audio-only mode has none), and at the start
  of every session. Both messages are SWITCH packets: parsed at depth 2, no
  timeline, the session's next seq.

The relay knows the episode. Its stream port answers `GET /info/<channel>`
(`/info/<device>/<channel>` with several users) with
`{"channel","title","episode","total"}` for the video the stream at
`/ch/<channel>` plays, or would open now (looked up without contacting the site
or changing state); 404 for anything else. The media server derives the address
from the channel's stream URL, asks at session start (0.5 s at most, before
CONFIG) and again in the background whenever it reopens the source. For the
immediate response, the relay writes a `loading` event (channel, `value` = the
episode or 0, `total`, `text` = the title) before next, previous or an episode
pick cuts the stream; the media server sends it within a second, drops the
`next` hint queued with it, and sends `{"loading":0}` when the reopened source
has prebuffered.

### Playback progress ("progress": 1)

Firmware that draws how far a relay video has played adds `"progress":1` to
HELLO. Only such a device gets the message below; older firmware sees exactly
what it did. All values are milliseconds.

- `AV_SWITCH {"progress":{"dur":D,"pos":P,"at":T}}`: the video is D long and
  is at P when this session's sound clock -- the audio PTS the device is
  playing, `estimated_pts()` -- reads T. The device moves the bar on with its
  own clock (position = P + clock - T, at most D), so nothing is sent per
  second. T is the wire PTS of the first sound block of the source the anchor
  describes: 0 at a session's start, the next audio PTS when a source is
  reopened inside the session (a seek, next, an episode pick, a pause ended, a
  stall).
- The same with `"stop":S`: the source stopped (a stall, a pause) after the
  sound that ends at clock S; the bar stops at P + S - T.
- `AV_SWITCH {"progress":0}`: nothing known (the new video's length is not).
- The device forgets the bar at every CONFIG, on `{"loading":{...}}` and on
  `{"next":1}` (another video comes), and moves it at once on `{"seek":n}`
  (to the position + n s, still), keeping that through a stopped anchor until
  a running one arrives or 20 s of clock have passed.
- Drawn in the bottom bar (rows 210..239), not over the picture, so it never
  meets the banner, the level box, the hints, the loading or pause band, and
  delta-coded stripes cannot leave it behind. Under a picture: the played
  share of the panel's width in amber along the bar's top two rows, the rest
  dim; "12:34 / 23:40" (H:MM:SS for an hour or longer) centred below, with the
  channel banner and for 4 s after a key, anything from the phone, a pause or
  resume, a voice result and every new anchor. Audio-only, under the pet: the
  played time, a bar and the total, always. Paused: the line, still, above the
  pause band's hint. The bar is repainted when what it shows changes -- about
  once a second with the times up, when the line grows a pixel otherwise --
  never per frame. A page in the bottom bar (list, status, pets) keeps it.

The relay knows the length and the offset. `GET /info/<channel>` (see above)
also answers `duration` (seconds; absent when unknown) and, while that stream is
being pushed, `offset` (the second it started from). The media server asks in
the background when a source starts feeding (session start, `reconnect.hold`
returning a new source), at most three times a second apart while the relay has
no `offset` yet; it sends the stopped anchor when a hold begins (before the
pause message) and, when the same source recovers, moves the anchor on by the
silence instead of asking (`server/progress.py`). A Bilibili stream starts at
the keyframe before the offset, so the bar can be a few seconds ahead. The v2
sender (`TV_LIVE_ENGINE=v2`) sends no progress.

### Listening on the phone ("listen": 1)

Firmware that can hand its sound to the phone remote adds `"listen":1` to
HELLO. Only such a device gets the messages below, and only from a media server
with `TV_LISTEN_PORT` set; older firmware sees exactly what it did (the server
then offers no listening for it). Design:
[`2026-10-12-phone-audio-design.md`](superpowers/specs/2026-10-12-phone-audio-design.md).

- `AV_SWITCH {"mute":1}`: a phone started listening. The audio task holds the
  codec muted (`bsp_audio_set_mute(true)`) and zeroes each chunk; the stored
  volume and NVS are untouched, and the remote's volume slider keeps showing
  it. `{"mute":0}`: nobody listens any more; the codec opens again.
- CONFIG `"mute":1`: the session starts while someone listens, so it starts
  muted without a moment of sound. A CONFIG without it unmutes; so does a
  reboot (RAM only), so a lost `{"mute":0}` cannot leave a device silent.
- While muted, `AV_STATUS` goes up every 2 s (instead of 30 s) with
  `"pos":P`: `estimated_pts()`, the wire PTS (ms) the device is playing. The
  server compares it with when its sender expected that block to play and
  keeps the median of the last five as the device's offset (δ).
- Nothing else changes on the device. While listening, the sender plays the
  phone's schedule H (5 s) later: it sends silent blocks with continuous PTS
  when the device is early (the picture holds; every second the server sends
  one "SOUND ON PHONE" frame so the device does not paint its waiting screen),
  and drops queued blocks when it is late. The status page shows no extra mark
  (phase 2).

## Media conversion and compatibility

Both `prepare` and `import-video` generate actual baseline 160x120 YUV420 JPEGs;
manifest and CONFIG dimensions match. Existing 320x240 media/configs are rejected:
regenerate into a new directory rather than overwrite old material. Import fits
source display aspect ratio (including sample aspect ratio) into 16:12 (4:3), with
centered black letterbox/pillarbox, no cropping/stretching, and even YUV420 rounding
(up to two pixels of border asymmetry). PCM, 12 fps timestamps, limits and private
network configuration are unchanged. See [server instructions](../server/README.md).

## Ownership, memory and lifecycle

- Socket worker: sole descriptor owner, nonblocking connect/read/write with
  bounded polling/deadlines. Complete ADPCM blocks/JPEG are queued; no consumer reads TCP.
- Audio worker: existing BSP I2S/codec only; 100 ms maximum per write, partial
  submission treated as failure. It decodes each queued ADPCM block to 1280 bytes of
  PCM just before writing it; an empty queue feeds zeroed PCM. The queue holds 24
  blocks (960 ms) of 324 bytes, 7776 bytes against 30720 for the same audio as PCM
  (`PCM_QUEUE` in `main/av_player.c` is the authority for the depth). Startup waits for five chunks. Queue full,
  starvation or feed gaps reset the connection and synchronization origin.
- Video worker: sole raw panel owner, no LVGL callback. Exactly two 24 KiB JPEG
  buffers, two 320x16x2 internal DMA strips (20480 bytes) and 4096-byte decoder
  workspace. A full JPEG pool drops a whole incoming frame without holding audio
  behind decoding. C3 ROM tjpgd still outputs RGB888 (not LEO's LVGL BGR decoder),
  decoding 160x120 at scale=0, followed by x2 nearest-neighbor RGB565 conversion
  to the 320x240 panel. Each 16-source-row MCU row fills both 16-target-row strips;
  source y=112..119 fills only the final target y=224..239 strip. The packer clips
  each MCU to a strip using source-rectangle row stride. Both strips are filled
  before sequential DMA submission/wait; buffers are never reused before completion.
  This retains 20480 bytes of strip RAM, without claiming decode/DMA overlap or
  faster playback. This is a LEO-style source-size change, not a 20 fps replication.
- Worker stacks: receiver 5120, audio 5120, video 4096 bytes; queues, driver DMA,
  Wi-Fi and BSP overhead are additional. The overlay uses 160 MHz and bounded
  Wi-Fi RX/TX pools. Runtime free/minimum/largest heap is authoritative, not a
  build-size estimate. Allocation failure leaves the test stopped.
- Buttons enqueue an eight-event queue of key+gesture pairs without blocking. OK
  toggles stop/restart; UP/DOWN clicks step to the next/previous channel by
  reconnecting with the new id; UP/DOWN long presses adjust volume 10..100% in
  5-point steps, initially 55%. Startup displays raw red/green/blue bars; a
  stopped image remains on screen.
- Stop/error sets cancellation, socket producer closes, consumers finish bounded
  operations and signal final resource access; only then are session queues and
  buffers freed. No worker is forcibly deleted. LCD DMA timeout deliberately
  retains buffers and waits for completion rather than causing use-after-free;
  ten failed 200 ms waits cause an explicit fault-only reboot without freeing
  DMA buffers. The standalone control,
  buttons, Wi-Fi event loop and BSP remain owned for the boot lifetime, while
  per-session allocations are released. There is no return-to-LVGL transition.
- Voice (`main/av_voice.c`, `main/av_voice_policy.c`): nothing is held until OK
  goes down. Then one block of about 9.9 KB (a 24-block uplink ring of 328 bytes,
  the 1280-byte block accumulator, a 640-byte read chunk and a spare block) and a
  task (3072-byte stack on the C3, 4096 on the S3, priority 6) are allocated;
  both go when the capture has stopped and the ring is drained, or at once on a
  click. A result costs about 1.1 KB while it is on screen, plus the cJSON tree
  (a few KB) for the moment it is parsed in the receive task. The TV's sound is
  zeroed in the audio task from press-down until the capture ends; while the
  list or a voice screen covers the picture the video task releases each stripe
  undecoded and repaints the panel on its own.
- Five seconds without receive progress triggers reset; no fixed total session
  timeout is imposed. Normal END drains queued work; reconnect backs off one
  second, clears all state and starts a new handshake. Continuous-PTS server
  material loops do not themselves reset the firmware.

## Timing and measurements: explicitly estimated

Logs use `CLOCK_ESTIMATED`. Submitted sample count means bytes accepted by the
I2S driver, **not DMA-completed samples and not acoustic output**. The monotonic
origin adds an assumed 90 ms (six 240-frame descriptors at 16 kHz) to the first
write; playback position is capped by submitted duration. This budget is not an
observed delay. Startup/reset flushes silence muted before establishing a new
origin. Physical codec/speaker delay, actual DMA fill, sample-clock drift and
network latency remain unmeasured. Video currently waits for estimated PTS before
decoding, so display can lag by decoding/strip transfer time; frames already over
100 ms late are dropped. This is a bounded-latency MVP, not synchronization
acceptance. Measure flash/beep markers before tuning clock offset or decode lead.

Every ten seconds logs report actual interval rendered frames and duration,
drops, queue high-water marks, decode maximum and heap/largest block. Session-end
logs add observed rendered FPS (including startup/drain), submitted samples,
maximum feed gap, minimum heap, and explicit unmeasured DMA/acoustic fields.
Target 12 fps is never reported as achieved frame rate. Logs contain no app-level
SSID/password/token/remote JSON; stock Wi-Fi driver logs may reveal network
identifiers, so device logs must be sanitized before sharing.

## Validation and remaining device checks

`tests/test_av_protocol.c` is pure C: header endian/length/type limits, malformed
headers, reassembly boundaries, sequence/session rejection, independent media
PTS, reconnect reset and RGB stripe geometry/byte order. Full-frame MCU tests
cover x2 duplication, source stride, seams, guard bytes and the final 8-row MCU. Static checks run these
alongside server and actual ffmpeg import/preparation tests when ffmpeg is available. These tests do not emulate ROM JPEG, LCD DMA,
I2S, Wi-Fi, concurrency or acoustic timing.

Report Build, Host tests and Device tests separately. Before hardware acceptance,
check RGB bars/orientation, JPEG rendering speed, mono PCM rate and volume,
flash/beep offset/drift, >30-minute continuous playback and server material loops,
queue/heap bounds, missing/slow server, bad/truncated JPEG, Wi-Fi loss, audio
underrun, repeated OK stop/restart and post-stop DMA ownership. A successful
build/merged-layout validation does not establish any of those physical results.
