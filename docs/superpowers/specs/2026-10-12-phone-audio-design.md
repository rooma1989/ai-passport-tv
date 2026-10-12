<p align="right">
  <a href="2026-10-12-phone-audio-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Listening on the phone — design

Status: design, plus a server-side prototype (`server/listen.py`,
`tools/listen_proto/`) that runs on a computer against a synthetic programme.
Nothing is wired into the media server, the relay or the firmware yet, and nothing
has been tried on an iPhone. Date: 2026-10-12.

## Goal

The phone remote plays the sound of whatever the chosen device is showing, so the
viewer can use Bluetooth headphones or a speaker paired with the iPhone. The device
keeps showing the picture with its own speaker muted. The ESP32-S3 (Xingzhi) and
ESP32-C3 (Passport) have BLE only — no Classic Bluetooth, so no A2DP — which is why
the phone has to be the bridge.

**Requirement from the user (2026-10-12):** the sound must keep playing with the
iPhone locked or the page in the background. A sound that is a few seconds late is
fine, and the device's picture may be delayed to match.

## Decisions in brief

1. **Transport: an `<audio>` element playing an HLS audio stream** (AAC, 16 kHz
   mono, 1.024 s packed-audio segments). On iOS an audio-only media element is not
   suspended when the screen locks; Web Audio is, unless extra conditions hold (§1).
2. **The phone stream is the master timeline; the device plays the same schedule
   H seconds later** (H = 5 s by default). The delay lives on the server, in the
   channel queue the media server already keeps — the device can hold under a
   second of sound and about two pictures, so it cannot be the place (§2).
3. **Each end aligns itself to the server clock.** Every segment carries
   `EXT-X-PROGRAM-DATE-TIME` = the wall-clock moment the device plays it. The phone
   reads where it is (`getStartDate() + currentTime`), compares with a synced clock
   and its Bluetooth latency, and seeks. The server keeps the device on the same
   schedule by adding or skipping 40 ms chunks — inaudible, because the device
   speaker is muted.
4. **Expected sync:** within about ±60–80 ms with the page in front, the firmware
   reporting its play position and the Bluetooth latency known. Without the
   position report, about ±250 ms. With the phone locked, add slow drift of up to
   ~0.1 s per hour, corrected the next time the page is opened (§3).
5. **Cost:** about 5 kB/s per listener (≈1.4 % of the 3 Mbit/s host), about 1–3 %
   CPU for the encoder and the tap, and a few MB more queue memory per device (§4).
6. **Firmware:** a session-scoped `mute` (the stored volume is kept) and a play
   position in `STATUS`. Both are small (§5).

## 1. Transport and player on iOS

Research summary (sources at the end). "Verified" means read in WebKit's source,
commit history, MDN compatibility data or the bug tracker. "Inferred" needs an iPhone
to confirm.

| Option | Locked / background | Latency | Sync handle | Verdict |
| --- | --- | --- | --- | --- |
| **`<audio>` + HLS** (native) | **Keeps playing.** Verified: on iOS only video and Web Audio are restricted in the background, not an audio-only element. | segment + hold-back: measured 3–5 s with our 1 s segments | `getStartDate()` + PROGRAM-DATE-TIME gives the wall time of the playing position; `currentTime` seeks are precise | **Chosen** |
| Web Audio fed over WebSocket/fetch | Interrupted by default (`"interrupted"` state). Verified: allowed in the background when `navigator.audioSession.type = "playback"` (WebKit commit 275558@main, 2024-03; reliable from about iOS 17.5). Open bugs in home-screen web apps (291892, 276016). Whether the WebSocket and JS keep running in the background is not established. | 0.2–1 s | sample-accurate scheduling; `outputLatency` (iOS 18.4+) includes the Bluetooth route | Phase 3 experiment only |
| Progressive MP3/AAC over HTTP | Keeps playing | 3–6 s, chosen by Safari | none (no PDT) | No |
| Managed Media Source (iOS 17.1+) | Keeps playing (audio element) | ~0.3–1 s, ours to control | full control | Possible later; much more page code |
| WebRTC | Uncertain; reports of suspension on lock; `jitterBufferTarget` only from iOS 27 | 0.1–0.5 s | weak | No |

Other findings that shape the page:

- **Media Session** (iOS 15+): metadata, play/pause and seek handlers, lock-screen
  Now Playing for an `<audio>` element. Verified.
- **Bluetooth latency is not exposed to a media element.** AVFoundation lines up a
  media element's own video with its own audio; it cannot know about a picture on
  another device. The page can read it from a short-lived `AudioContext`:
  `outputLatency` (iOS 18.4+) is the audio unit latency plus AVAudioSession's
  `outputLatency`, so it includes the Bluetooth route (verified in WebKit source).
  It reads 0 when the context is not playing and is clamped under fingerprinting
  protection. Measured AirPods latencies run from 126 ms (Pro 2) to 274 ms (1st
  gen), and the system's reported value can be 50 ms or more off in the first
  minutes. The page offers the automatic reading, presets and a slider.
- `navigator.audioSession.type = "playback"` (Safari 16.4+) also makes the sound
  ignore the ring/silent switch. The page sets it; it does no harm to `<audio>`.
- Starting playback needs a tap. Interruptions such as a phone call or Siri pause
  the element; resuming from the lock screen is a user action.

## 2. Architecture: the phone stream leads, the device follows H later

```
  source ─ffmpeg─▶ LiveChannel queue (content order; deque, up to 16 s)
                      │                       │
         peek ahead   │                       │  pop at the device's pace (today's sender)
   (ChannelTap, no    ▼                       ▼
     dequeue)   Ledger: one entry per 40 ms   _pace_live ── asks Ledger.for_device(play_at)
                 = chunk X, or silence           • on schedule  → send chunk X
                      │                          • device early → send filler silence (device only)
                      ▼                          • device late  → skip entries
               ffmpeg AAC ─▶ 1.024 s segments            │
               PDT = when the device plays it             ▼
                      │                           device (speaker muted) plays entry k
                      ▼                           at published(k) + H
               nginx /tv/listen/dN/ ─▶ iPhone <audio>, seeks so that
                                       PDT(playing now) = server now + Bluetooth delay
```

- **Ledger** (`server/listen.Ledger`): every 40 ms of real time, one entry is
  appended at the end of the phone stream: the next content chunk from the channel
  queue, or silence if the source has nothing ready. Entry *k* becomes part of the
  stream at `published(k) = start + k·40 ms`; the device must play it at
  `published(k) + H`.
- **The device follows the ledger.** When the sender is about to send a chunk that
  the device will play at `play_at`, it calls `for_device(play_at)`. If the next
  entry's target is more than 25 ms later, the device is early: send a filler
  chunk of silence (it shifts the device back by 40 ms and never reaches the
  phone). If the target is more than 25 ms earlier, the device is late: skip
  entries. Otherwise send that entry, popping the channel queue up to its chunk.
  The tolerance is just over half a chunk, so the device settles within ±20 ms
  and does not oscillate. This is host-tested at four phases
  (`test_a_device_at_any_phase_settles_without_oscillating`).
- **The picture needs no new logic.** `LiveChannel.pop_video` already pairs each
  frame with the audio chunk at the head of the queue. The ledger keeps that head
  in content order, so pictures follow. While the device is being filled with
  silence, no picture is sent and the device shows a notice (sound handed to the phone),
  the way `reconnect.hold` does today.
- **Why the delay is on the server.** Device buffers (from `main/av_player.c`):
  `PCM_QUEUE` = 24 chunks = 960 ms, with flow control at 20 chunks (800 ms); the
  picture ring is `AV_VIDEO_RING_BYTES` = 16 KB with 32 items, i.e. two or three
  frames; and a frame more than 100 ms behind the audio clock is dropped. The
  device cannot hold seconds. The media server's `LiveChannel` queue holds
  `PCM_QUEUE_CHUNKS` = 400 chunks (16 s) and `TV_VIDEO_QUEUE_S` = 16 s.
- **One fixed H rather than a measured D.** H is a per-device setting (default
  5 s), not a value negotiated with each phone. The phone absorbs its own latency
  and the slider by seeking. The device never moves when someone drags the
  slider, several phones can listen to one device, and the device's schedule does
  not depend on a phone that may be locked and silent. H must exceed the phone's
  "segment appears → audible" latency. Measured on macOS AVPlayer (§7): it played
  without stalling at H = 3 s on loopback. 5 s leaves ~2 s for the mobile network
  and Bluetooth. The page shows its distance to the live edge, so H can be tuned
  from real use.
- **Cushion.** `ChannelTap` starts reading only once the queue holds a cushion
  beyond the tap. Live HLS sources arrive in bursts of several seconds, so they
  need about 3 s. Relay sources (Bilibili, YouTube, cast) are paced by `-re`, so
  about 1 s is enough. If the tap runs dry, it publishes silence and the device
  later plays the same silence, so both stay aligned. After 0.5 s dry, the tap
  waits for the cushion again.
- **The stream never stops while someone listens.** Between device sessions
  (channel switch, reconnect), the ledger keeps appending silence in real time, so
  the phone's player never stalls. A locked phone does not need the page to wake up
  to stay aligned.

### What happens on each event

| Event | Phone | Device |
| --- | --- | --- |
| Start listening mid-programme | Plays from the first segments (3 s of silence prefill, then programme) | Muted. The picture freezes for about H + cushion − queue depth (typically 3–5 s) while filler moves it back, then resumes H behind the phone. |
| Channel switch (phone or device button) | Old channel continues for what is already published, then silence, then the new channel after prebuffer + cushion (about 5–7 s) | Leaves at once. Its connecting screen lasts H longer than today (about 9–10 s in total). |
| Pause (relay channels) | The tap stops taking programme at once and publishes silence | Keeps going for the H seconds the phone already heard, then shows the pause band when it reaches the pause marker in the ledger (a marker entry, phase 2) |
| Resume | Programme resumes after the relay restarts the source + cushion | H later, same content |
| Seek / next / next episode (relay restarts the source) | Silence, then new content | The old `LiveChannel` is closed by `reconnect.hold`, so up to the last H seconds before the cut are not shown (the viewer asked to skip anyway). Then the new content, H after the phone. |
| Source breaks without being asked (rare) | Heard to the break, then silence | Same as above: up to H seconds of picture lost. Keeping the closed channel until the ledger drains is possible but not worth it now. |
| Phone paused or closed / locked + paused | Safari stops fetching the playlist | After 20 s without a playlist request, listening ends: device unmuted, ledger dropped. The device keeps its H of extra lag until the next session. |
| Page stopped with the button | `POST …/bye` | Same, at once |

Relay channels differ from live channels in three ways. They are smooth (small
cushion). They can pause, seek and skip (table above). And the relay's progress
estimate assumes a fixed lag of `PLAYBACK_LAG_S = 4.0` s between relay output and
the device screen, which must grow by H while listening (§6).

## 3. Synchronisation and expected numbers

- **Clock sync (phone ↔ server).** NTP-style over the same HTTPS origin: eight
  `GET /tv/listen/time` requests; keep the one with the shortest round trip;
  offset = server time − midpoint. The error is at most half the asymmetry of that
  round trip, typically ±10–25 ms on a mobile link. Repeated every minute and on
  every `visibilitychange` to visible.
- **Where the phone is.** `audio.getStartDate().getTime() + currentTime·1000` is the
  PDT of the sample being rendered. As a fallback, each segment's ID3 `TXXX pdt`
  shows up as a metadata cue. The phone's audible position is that minus the
  Bluetooth latency.
- **Error and correction.** `error = PDT − bt − serverNow + userOffset`. Positive
  means the phone is ahead, so seek back. The page seeks when |error| > 60 ms, at
  most every 4 s. A seek stalls playback while the new position buffers. On
  AVPlayer it lost a steady 0.35 s, so the page learns that cost from the residual
  and adds it to the next seek (§7: residual −3 to −28 ms after the second seek).
- **The device.** The server knows the device's play time only up to the device's
  queue depth: the sender releases audio up to `AUDIO_LEAD_MS` 320 +
  `AUDIO_MAX_LOOKAHEAD_MS` 280 ms early, and the device plays continuously at its
  I2S rate, slipping later with every underrun. The estimate δ therefore wanders
  between about −230 ms and +200 ms. Underruns are rare since the 320 ms lead, so a
  constant estimate holds most of the time, but not reliably. With a `"pos"` field
  (the device's `estimated_pts()`) in the `STATUS` packet every 2 s while
  listening, the server measures δ to about ±20–30 ms (the Wi-Fi uplink delay),
  and the ledger's filler/skip keeps it there.
- **Locked phone.** No page correction runs. The phone's audio clock against the
  server's drifts at crystal tolerance, ±20–50 ppm, which is 0.07–0.18 s per hour.
  The device keeps to the server clock. The page corrects when it is next opened.

Budget with the page in front:

| Term | Size |
| --- | --- |
| Phone ↔ server clock | ±10–25 ms |
| PDT readout | ~1 ms (AVPlayer: stable to the millisecond) |
| Seek landing after learning | −3 to −28 ms measured (macOS AVPlayer) |
| Bluetooth latency | ±30–60 ms (outputLatency or preset); the slider removes the steady part |
| Device schedule quantum | ±20 ms (40 ms chunks) |
| Device play time δ | ±20–30 ms with `pos` reports; ±200 ms without |
| **Total** | **about ±60–80 ms typical; about ±250 ms without `pos`** |

For reference, ITU-R BT.1359 puts the detectability of lip-sync error at 45 ms
with the sound early and 125 ms with the sound late (acceptability 90/185 ms). A
late sound is better tolerated, so the default offset could bias the phone 20–40 ms
late.

**If the latency cannot be met** (the phone's distance to the live edge falls below
about one segment, or it keeps stalling): the page seeks no further forward than
1.5 s from the seekable end and counts an "edge too close" event. The fix is a larger H for that
device, not a slower picture. A fixed lag is the fallback.

## 4. Where the audio is tapped; authentication; cost

- **Tap.** In the media server, from the same `LiveChannel` queue the device sender
  pops, read ahead without dequeuing (`ChannelTap`). The queue holds the wire format
  (IMA ADPCM, 324 B per 40 ms). The prototype decodes it in Python: measured
  0.17 ms per block, 0.4 % of a core (Apple silicon), and the phone hears exactly
  what the device would. Better: keep the pre-ADPCM PCM in a parallel deque in
  `LiveChannel._process_audio` while someone listens (1280 B per chunk, ~0.5 MB at
  400 chunks), avoiding the decode and the ADPCM noise.
- **Encoding.** One long-running `ffmpeg … -c:a aac -b:a 32k -f adts` per listened
  device. The native AAC encoder is in every ffmpeg build, including the deployed
  btbn 8.1. Measured: the encoder adds 118–141 ms before a frame comes out, and
  uses 0.15–0.18 CPU-seconds per 31 s on Apple silicon (~0.5 %). Expect 1–3 % on
  the cloud vCPU (not measured there). Python CPU for the ledger, tap and HTTP is
  under 1 %.
- **Bandwidth per listener.** Segments 4.1–4.25 kB/s, plus a 12-segment playlist
  (~0.84 kB) about once a second, so about 5 kB/s ≈ 41 kbit/s ≈ 1.4 % of 3 Mbit/s.
  For comparison, a device's picture stream is 110–200 kB/s. nginx gzip on `.m3u8`
  would cut the playlist share.
- **Memory.** The extra queue is H + cushion of programme, held in the existing
  `LiveChannel` queues. Audio is 8.1 kB/s. Pictures are queued raw (one byte per
  pixel): 43.2 kB per frame at 240×180, so 15 fps × 8 s ≈ 5.2 MB for the Xingzhi;
  smaller for the Passport's 160×120. Bursty live sources can push the queue past
  16 s, so while listening raise `TV_PCM_QUEUE_CHUNKS` / `TV_VIDEO_QUEUE_S` to
  about 24 s (≈ 15 MB worst case on the Xingzhi, against `MemoryMax=600M`). The
  segment window is 12 × ~4 kB.
- **Who may listen.** The relay already knows the signed-in phone (cookie `tvs`, or
  `?s=` for the home-screen app). It issues a token,
  `listen.issue_token(secret, "d0")` (`<expiry>.<HMAC-SHA256 truncated>`, 12 h).
  The media server checks it with the same secret. The token is in the URL because
  there is no guarantee AVFoundation sends the cookie when it fetches playlists and
  segments, least of all in a home-screen app that does not share Safari's
  cookies. The playlist repeats the token on every segment URI. nginx has
  `access_log off` for these locations, as for the rest of `/tv/`. The secret is a
  new file readable by the `tvserver` group, e.g.
  `/etc/ai-passport-tv/listen-key`.

## 5. Device side

- **Mute without losing the volume.** Add `AV_SWITCH {"mute": 1|0}` and a
  `"mute": 1` capability in HELLO. Also send `"mute": 1` in CONFIG while listening,
  so a new session starts muted with no audible blip. The device keeps it in RAM
  only (not in `av_store`), and `audio_task` keeps the codec muted
  (`bsp_audio_set_mute(true)`) instead of unmuting on the first block. `s.volume` and
  NVS stay as they are, and the phone's volume slider keeps showing the device's
  value. A reboot or a session without the flag unmutes, so a crash cannot leave a
  device silent for good. The status page could show a headphone mark.
- **Play position.** Add `"pos": estimated_pts()` (ms on the wire timeline) to
  `STATUS`, sent every 2 s while muted for listening. One field, no new packet.
- **Older firmware.** Listening is offered only when HELLO carries `mute`. A
  fallback of "set volume to 0 and restore later" was considered and rejected: the
  device saves the volume in NVS and reports it to the phone, so a lost restore
  leaves the device at 0.
- **What the device sees during filler.** Silence chunks with continuous timestamps.
  Since the speaker is muted, nothing is heard. The picture freezes on its last
  frame and the server sends the notice frame once a second. This keeps it inside
  the 1.5 s "picture stopped" judgement, the 3 s `AUDIO_SILENCE_MAX_MS` and the 3 s
  `AV_READ_DEADLINE_MS`, as `reconnect.hold` does.

## 6. How it would be wired in (not done here)

Changes to `server/relay/web.py` and `server/relay/assets/remote.html` are left
until the concurrent episode-picker work lands.

- **`server/tv_server.py` (`AVServer`).**
  - New env vars: `TV_LISTEN_PORT` (Xingzhi 8198, Passport 8199), `TV_LISTEN_DEVICE`
    (`d0`/`d1`, the relay's device id) and `TV_LISTEN_KEY_FILE`.
  - When set, start a `ThreadingHTTPServer(("127.0.0.1", port), listen.make_handler(...))`.
  - `streams(device, start)` creates `self.listen = ListenStream(self.ffmpeg, lead_s)`
    on the first playlist request and returns it.
  - A pump thread runs every 20 ms. It calls `self.listen.pump(tap)`, where `tap`
    is a `ChannelTap` rebound whenever `self.live_channel` changes. It closes the
    stream when `idle()`.
  - In `_pace_live`'s two audio-send loops, when `self.listen` is active, replace
    `channel.pop_audio()` with the ledger decision:
    - `play_at = origin + audio_pts/1000 + δ`.
    - Filler: send `bytes(AUDIO_BYTES)` and `stamp_base += AUDIO_CHUNK_MS`, with no
      pop and no picture.
    - Entry: pop until the entry's key.
  - Send `{"mute": 1}` / `{"mute": 0}` as listening starts and ends, and put
    `"mute": 1` in CONFIG.
  - Feed `STATUS.pos` into δ (median of the last 5).
  - Write `"listening": true` into `now-playing.json`.
  - Ledger keys become `(channel generation, content_ms)`, because a replaced
    channel's content times restart at 0.
- **`server/reconnect.py`.** While listening, the paused branch enters only when the
  device reaches the ledger's pause marker (phase 2). The filler notice reuses
  `_send_notice`.
- **`server/relay/web.py`.**
  - Add `POST /api/listen` → `{"ok": true, "url": "/tv/listen/<id>/live.m3u8?t=<token>"}`
    for `self.target()`. Refuse it if the device is offline or its HELLO has no
    `mute`.
  - Add `--listen-key-file`.
  - When `now_playing` says `listening`, add H to `PLAYBACK_LAG_S` in the progress
    estimate.
- **`remote.html`.**
  - Add a "listen on the phone" switch in the device panel, with the offset slider and the
    Bluetooth presets / automatic button. Store the settings per device in
    `localStorage`.
  - On tap: set `audioSession.type = "playback"`, POST `/api/listen`, set
    `audio.src`, `play()`, then set the MediaSession metadata (channel name).
  - The sync loop, clock sync, seek learning and `visibilitychange` handling come
    from `tools/listen_proto/index.html`. The page must not switch the device when
    the phone is used as an earphone only.
- **nginx (`packaging/nginx/tv-relay.conf`).** The longer prefix wins over `/tv/`:

  ```nginx
  location ^~ /tv/listen/d0/ { proxy_pass http://127.0.0.1:8198/listen/d0/; proxy_buffering off; access_log off; }
  location ^~ /tv/listen/d1/ { proxy_pass http://127.0.0.1:8199/listen/d1/; proxy_buffering off; access_log off; }
  location = /tv/listen/time { proxy_pass http://127.0.0.1:8198/listen/time; access_log off; }
  ```

- **systemd.** Add the env vars to `tv-xingzhi.service` and to the Passport unit.
  Add `--listen-key-file` to `tv-relay.service`.
- **Firmware.** §5.

## 7. Prototype

What is in the branch:

- `server/listen.py`: `Ledger`, ADTS splitting, the ID3 tag (PRIV transport-stream
  timestamp + TXXX pdt), `HlsWindow` (segments + playlist with PDT and
  `EXT-X-START`), `AacEncoder` (ffmpeg), `ChannelTap`, `ListenStream`, tokens and the
  HTTP handler (`/listen/time`, `/listen/<dev>/live.m3u8`, `/listen/<dev>/<n>.aac`,
  `POST /listen/<dev>/bye`).
- `tests/test_listen.py`: 18 host tests. The encoder test runs when `ffmpeg` is on
  `PATH` or `TV_LISTEN_FFMPEG` points at one; otherwise it is skipped.
- `tools/listen_proto/serve.py`: a synthetic programme and a simulated device, as
  described under "Try it on an iPhone" below. `--bursty` imitates a live HLS
  source; `--measure N` runs headless and prints the numbers below.
- `tools/listen_proto/index.html`: the listener page (Chinese UI). It covers start
  and stop, clock sync, the error readout, automatic alignment, the offset slider,
  Bluetooth presets, automatic Bluetooth measurement, Media Session and resync on
  return. It also has a box that flashes at the device's beep times.
- `tools/listen_proto/avplayer_probe.swift`: plays the stream in macOS AVPlayer
  (Safari's HLS engine), muted. It prints the PDT error and the edge distance and
  aligns by seeking the way the page does.

The simulated device is a sender that follows the ledger and records when it
"plays" each beep. The programme beeps every second, with a higher beep every
five seconds.

Measured on the development Mac (loopback, ffmpeg 4.4 native AAC, macOS AVPlayer):

| Measurement | Result |
| --- | --- |
| Segment payload | 4.10–4.25 kB/s; playlist 841 B |
| Encoder extra delay (PCM in → frame out) | median 118–121 ms, max 141 ms |
| ffmpeg CPU | 0.15–0.18 s per 31 s (~0.5 %) |
| Python CPU (ledger, tap, HTTP, plus the simulation) | 0.7–0.8 % |
| ADPCM → PCM decode in the tap | 0.17 ms per 40 ms block |
| Simulated device vs schedule | settles within the 25 ms tolerance (one run: 8.2 ms constant) |
| Bursty source with a 3 s cushion | no silence inserted after the cushion |
| AVPlayer accepts the 16 kHz packed-audio stream | yes; plays within 0.5 s of `play()`; `currentDate()` follows PDT |
| AVPlayer natural start (with `EXT-X-START`) | phone off by −0.66 to +0.51 s (four runs) |
| First seek | lands 0.35 s late (time lost buffering) |
| Second seek with learned compensation | residual −3 ms (H = 6 s), −5 ms (H = 5 and 4), −28 ms (H = 3); stable for 13–20 s afterwards |
| Stalls at H = 3, 4, 5, 6 s | none during playback on loopback (one run waited once before its first sound) |

In Chromium (the desktop app's browser pane), the stream plays, but
`getStartDate` is missing and there are no metadata cues, so the page cannot
align. Alignment needs Safari.

**Not measured:** an iPhone (Safari or home-screen app), the lock screen, Bluetooth,
the mobile network, the cloud host's CPU, and the real media server and devices.

**Try it on an iPhone:** run
`python3 tools/listen_proto/serve.py --ffmpeg <ffmpeg> --lan` on a Mac on the same
Wi-Fi. Open the printed address in Safari and tap the start button. Listen for
the beep against the flashing box, lock the phone, unlock it, and see whether
the "phone sound vs TV picture" readout returns to near zero.

## 8. Recommendation, plan, effort, risks

**Recommendation:** build the HLS path with the device delayed by a fixed H on the
server, as above. Keep Web Audio with `audioSession` "playback" as a later
experiment for a low-latency mode (H ≈ 1 s), because WebKit's source says it now
survives locking. Two things are not established for it: that JS and the socket
keep running in the background, and how it behaves in home-screen apps (open
bugs). It would reuse the same ledger and device side.

| Phase | Content | Effort |
| --- | --- | --- |
| 0 | Try the prototype on the user's iPhone (Safari and home-screen app, AirPods, locked) and record the numbers | 0.5 day |
| 1 | Media server integration (§6), relay `/api/listen` + token, page switch + sync loop, nginx/systemd, firmware `mute` + `pos` on both boards, host tests, device test | 4–6 days |
| 2 | Pause marker in the ledger, relay progress + H, PCM tap instead of ADPCM decode, headphone mark on the device, H tuning from field data, gzip playlists | 2–3 days |
| 3 (optional) | Low-latency mode: Web Audio + `audioSession` "playback" over WebSocket, H ≈ 1 s, if it holds up when locked | 2–3 days |

Risks:

- **Nothing has run on an iPhone yet.** The open questions are `getStartDate` on
  iOS, the cost of a seek on mobile, and home-screen app behaviour. WebKit bug
  261858 reports `<audio>` lock-screen controls not responding in standalone mode;
  our single live item does not need "next track". Phase 0 exists for this.
- **The TV picture is later.** It is about H (5 s) further behind live while
  listening, and switching channels on the TV takes about 5 s longer.
- **Source cuts.** A seek, next or source failure drops up to H seconds of picture on
  the device.
- **Bluetooth on iOS before 18.4.** Without `outputLatency`, the user must pick a
  preset or use the slider.
- **Locked-phone drift** is about 0.1 s per hour, uncorrected until the page is
  opened again. A stall while locked leaves a lag until then.
- **Memory.** The deeper queues need about +15 MB worst case per device.

**Open questions for the user:**

1. Is a TV picture about 5 s further behind live, and about 5 s slower channel
   switching while listening, acceptable? Or should we try for a smaller H after
   phase 0?
2. May both devices get a firmware update (`mute` and `pos`)? Without `pos`, sync is
   about ±250 ms instead of ±60–80 ms.
3. Pressing pause on the lock screen: should it only stop listening (the TV gets its
   sound back after ~20 s), or pause the programme on relay channels as the
   remote's pause button does?
4. Is the home-screen app the main way in, or Safari? Which iOS version is on the
   iPhone? 18.4+ gives automatic Bluetooth latency.
5. Should several phones be able to listen to one device at once? The design allows
   it at no extra cost.
6. `server/README.md` says the media server moved to the separate
   `ai-passport-tv-server` repository, yet deployment and recent commits use this
   repository's `server/`. Which one should get this work?

## Sources

- WebKit bug 261554 and commit b848143 (Web Audio allowed in the background with `audioSession` "playback"): https://bugs.webkit.org/show_bug.cgi?id=261554 , https://github.com/WebKit/WebKit/commit/b848143a2ad57c80405dc5b30e61c750f5896d72
- WebKit source (main): `Source/WebCore/Modules/webaudio/AudioContext.cpp`, `platform/audio/ios/MediaSessionManagerIOS.mm`, `html/HTMLMediaElement.cpp`, `platform/audio/cocoa/AudioDestinationCocoa.cpp`, `platform/audio/ios/AudioSessionIOS.mm`, `Modules/audiosession/DOMAudioSession.cpp`, `Modules/mediastream/RTCRtpReceiver.idl` — https://github.com/WebKit/WebKit
- Home-screen and interruption bugs: https://bugs.webkit.org/show_bug.cgi?id=291892 , https://bugs.webkit.org/show_bug.cgi?id=276016 , https://bugs.webkit.org/show_bug.cgi?id=261858 , https://bugs.webkit.org/show_bug.cgi?id=283419 , https://bugs.webkit.org/show_bug.cgi?id=239602 , https://bugs.webkit.org/show_bug.cgi?id=236363
- MDN / compatibility data: https://developer.mozilla.org/en-US/docs/Web/API/AudioSession , https://developer.mozilla.org/docs/Web/API/AudioContext/outputLatency , https://github.com/mdn/browser-compat-data
- Output latency: https://web.dev/articles/audio-output-latency
- Safari releases: https://webkit.org/blog/13966/webkit-features-in-safari-16-4/ , https://webkit.org/blog/14735/
- Apple developer forums on background audio and LL-HLS: https://developer.apple.com/forums/thread/679274 , https://developer.apple.com/forums/thread/774239 , https://developers.apple.com/forums/thread/745254
- AirPods latency measurements: https://stephencoyle.net/airpods-pro-2 , https://9to5mac.com/2019/12/22/airpods-latency-test/
- HLS: RFC 8216 (EXT-X-PROGRAM-DATE-TIME, EXT-X-START); Apple "HTTP Live Streaming — packed audio" (ID3 PRIV `com.apple.streaming.transportStreamTimestamp`)
- Lip-sync thresholds: ITU-R BT.1359-1
