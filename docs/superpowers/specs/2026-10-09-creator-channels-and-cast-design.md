<p align="right">
  <a href="2026-10-09-creator-channels-and-cast-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Creator channels and casting from a phone — design

Status: approved 2026-10-09 (approach A). Follows
[`2026-10-09-paged-setup-and-help-design.md`](2026-10-09-paged-setup-and-help-design.md).

## Goal

Besides live TV, the Xingzhi device can now:

1. **Creator channels.** One channel per followed Bilibili or YouTube creator.
   It plays the creator's newest unwatched video, then the next, without user
   action.
2. **Casting.** A phone web page takes a Bilibili or YouTube video link; the
   device switches to a "My cast" channel on its own and plays it, then goes back
   to the channel it was on.

## Decisions

| Question | Decision |
| --- | --- |
| What a creator channel plays | newest unwatched of the latest 20 uploads; when all 20 are watched, start again from the newest |
| Channel layout | one channel per creator, named after the creator; "My cast" and the creators come first in the list |
| How a link is sent | a phone web page; the device switches channel by itself |
| Access to the page | a private link with a random key (`?k=…`), saved as a phone bookmark |
| Sites | Bilibili and YouTube |
| Where YouTube is fetched | on the user's overseas server "cstone", never on the Aliyun server |

No proxy client runs on the Aliyun server: Aliyun's mainland terms forbid it and
the server also hosts the user's website.

## Architecture

```
phone ──HTTPS──▶ nginx (iewww.com/TV/cast/) ──▶ relay (Aliyun, 127.0.0.1:8099)
                                                  │  Bilibili: web API + ffmpeg, direct
                                                  │  YouTube:  HTTPS ──▶ relay (cstone, YouTube only)
                                                  ▼
TV server (Aliyun, :8098) ── ffmpeg ── http://127.0.0.1:8099/ch/<id>
      │
      └── TCP ──▶ Xingzhi device
```

- **Relay**, a new Python service (`server/relay/`), turns "the next video of
  creator X" or "the cast video" into an endless-looking MPEG-TS stream over
  HTTP. The TV server treats a relay URL like any other live source.
- **The same relay code** runs on cstone in YouTube-only mode. It serves a single
  endpoint (`/yt/<video id>`) that streams a small transcode of that video. The
  Aliyun relay forwards YouTube work to it.
- **TV server changes:** reload the channel tables when they change, and push a
  new SWITCH packet to the device.
- **Firmware change:** handle SWITCH by switching channel as if the user picked
  it in the list.

## Relay (Aliyun)

### Streams

`GET /ch/<id>` is the only endpoint the TV server uses. It listens on loopback,
so it needs no key.

- `cast`: plays the cast queue's current video; returns 404 when the queue is
  empty.
- `upNN`: picks the creator's next video:
  1. refresh the upload list when it is older than 30 minutes (Bilibili rate-limits
     list requests); keep the newest 20;
  2. if a video has a saved position, resume it from position − 10 s;
  3. otherwise take the newest video not marked watched;
  4. if all 20 are watched, clear their marks and take the newest.
- Bilibili is read through its web API, not yt-dlp: Bilibili answers 412 to the
  Aliyun address for video pages and for the signed (`wbi`) list API (see
  Spike findings). Calls, all with a browser User-Agent, `Referer:
  https://www.bilibili.com/` and a `buvid3` cookie from
  `/x/frontend/finger/spi`:
  - creator name: `/x/web-interface/card?mid=`;
  - upload list: `/x/space/arc/list?mid=&ps=20&pn=1` (newest first);
  - video `cid`: `/x/web-interface/view?bvid=`;
  - media: `/x/player/playurl?bvid=&cid=&qn=16&fnval=1&platform=html5` gives one
    360p MP4 with H.264 and AAC, about 400 kbit/s.
  The MP4 is remuxed with `ffmpeg -re -ss <offset> -i <url> -c copy -f mpegts` —
  no transcoding on the 2-core server. Media URLs expire, so they are fetched per
  request and never stored. YouTube: bytes come from cstone, already small.
- While streaming, the relay saves the position (start offset + elapsed wall
  time, since `-re` runs at real time) every 10 s.
- When the video ends naturally it is marked watched and its position cleared,
  and the response ends. The TV server's decoder runs ffmpeg with
  `-reconnect_streamed 1`, so it reopens the same URL by itself and the relay
  serves the next video; the device sees no reconnect notice (measured: one
  88 ms gap).
- The choice of video depends only on the state file, never on how many requests
  came before: each channel start also opens a short extra connection to sample
  the palette. A connection that ends early only saves the position.
- At the end of a cast video the relay pops the queue; if it is now empty it
  writes a "return" event (see below).

### Channel table

The relay writes `relay-channels.txt` (same format as `channels.txt`):
`cast | <"My cast" in Chinese> | http://127.0.0.1:8099/ch/cast` and one `upNN | <name> | …/ch/upNN`
per creator. Names are cut to 20 columns (a Chinese character counts two, so 10 of them or 20 letters) so the CONFIG list stays below
`TV_CONTROL_MAX` (7168 bytes); at most 12 creators.

### Events to the TV server

The relay writes `relay-events.json`, `{"seq": n, "action": "cast" | "return"}`,
atomically (temporary file and rename). `seq` only grows.

### Management page and API

nginx proxies `https://iewww.com/TV/cast/` to the relay. Every request there must
carry the key (`?k=` on the page, the same value in an `X-Key` header for API
calls); a wrong key gets 404. Key file: `/etc/ai-passport-tv/relay-key`, mode
0640 root:tvserver.

| Call | Effect |
| --- | --- |
| `GET /` | the page (Chinese, phone-sized, no external assets) |
| `GET /api/state` | creators with name, site, videos left; cast queue |
| `POST /api/creators {url}` | add a creator from a space/channel link |
| `DELETE /api/creators/<id>` | remove one |
| `POST /api/cast {url}` | replace the cast queue with this video, write a "cast" event |

Links are accepted only from `bilibili.com`, `b23.tv` (short links are resolved
first), `youtube.com` and `youtu.be`. yt-dlp and ffmpeg run with argument lists,
never a shell, and the URL goes after `--`.

### State

`/var/lib/ai-passport-tv/relay-state.json`: creators (id, site, source URL, name,
cached list with fetch time, watched ids, saved positions) and the cast queue.
Written atomically; one lock around every change.

## Relay (cstone, YouTube only)

- `GET /yt/<video id>?k=<key>` → yt-dlp picks a format ≤ 360p, ffmpeg transcodes
  to 320×240-class H.264 ≈ 300 kbit/s + AAC 48 kbit/s MPEG-TS at real time.
  Cross-border traffic is about 150 MB per hour of viewing.
- `GET /yt-list/<channel>?k=` → the newest 20 uploads (id, title) as JSON, so
  YouTube creator lists are also read from cstone.
- HTTPS with a self-signed certificate on a high port; the Aliyun relay trusts
  only that certificate (pinned file), so no domain is needed. The address and
  key live in `/etc/ai-passport-tv/cstone.conf` on Aliyun.
- An optional `cookies.txt` for yt-dlp if YouTube asks to sign in.
- systemd unit with the same sandboxing as `tv-xingzhi.service`.

## TV server changes

- **Two channel files**, `TV_RELAY_CHANNELS_FILE` first, then `TV_CHANNELS_FILE`.
  At each new session the server re-reads them if either file's mtime changed. A
  bad file keeps the previous table and logs a warning.
- **Switching:** during a live session, once a second, the server checks
  `relay-events.json`. On a new `seq`:
  - `cast`: remember the current channel as `previous` (unless it is `cast`) and
    send `SWITCH {"channel": "cast"}`;
  - `return`: send `SWITCH {"channel": previous}` if the device is still on
    `cast`.
  The first `seq` seen after start-up is only recorded, so an old event is not
  replayed. Nothing is sent to a device whose HELLO lacks `"switch": 1`.
- New packet kind `SWITCH = 8` in `server/protocol.py`; JSON payload.

## Firmware changes

- `AV_SWITCH` (= 8) in `main/av_protocol.h`.
- The receive task copies the channel id (bounded by `AV_CHANNEL_ID_MAX`) into a
  pending slot; the main loop calls the existing `request_switch()` and shows the
  usual channel banner. An unknown id is ignored with a log line.
- `av_header_decode()` and `av_stream_accept()` accept `AV_SWITCH` (no flags,
  length 1..`AV_CONTROL_MAX`, takes its place in the sequence like
  `AV_PALETTE`); host tests cover both.
- Firmware before this change rejects an unknown kind and drops the session, so
  the device announces `"switch": 1` in HELLO and the server sends SWITCH only to
  a device that did. Without it, casting still works by picking "My cast" by
  hand.

## Error handling

| Case | Behaviour |
| --- | --- |
| Video unavailable (deleted, region-locked, members only) | mark watched, log, try the next one (up to 3 per request, then 503) |
| Creator list fetch fails | use the cached list; with none, 503 → device shows the reconnect notice and the dead-channel policy moves on |
| cstone unreachable | YouTube channels and YouTube casts give 503; Bilibili unaffected |
| Bad or unsupported link on the page | error text on the page, nothing changes |
| Device not connected when casting | the queue waits; picking "My cast" plays it |

## Testing

- **Spike first** (before the rest is built) — done for Bilibili, see below. The
  YouTube half (one video fetched on cstone with yt-dlp) is done at the start of
  step 4.
- Host unit tests (`tests/test_relay_*.py`): next-video choice (watched, resume,
  wrap at 20), link validation, channel-table output and name cutting, event
  file `seq`, key check, state file round trip. yt-dlp and ffmpeg are mocked.
- TV server tests: reload on mtime change, bad file keeps the old table, `cast`
  then `return` sends the right SWITCH packets, old event not replayed, no SWITCH
  to a device without the HELLO flag.
- Firmware host tests: `AV_SWITCH` header and sequence acceptance.
- Device: cast from the phone while watching live TV → switch, play, return.

## Spike findings (2026-10-09)

- From the Aliyun address, Bilibili video pages, `x/space/wbi/arc/search` and
  `x/series/recArchivesByKeywords` answer 412, with or without a `buvid3`
  cookie, so yt-dlp's Bilibili extractors fail. `x/space/arc/list`,
  `x/web-interface/card`, `x/web-interface/view` and `x/player/playurl` work.
- `playurl` with `qn=16&fnval=1&platform=html5` returned a single 360p MP4
  (H.264 High 640×360 25 fps, HE-AAC) for all four videos tried, about
  400 kbit/s; resolving took 0.2–0.4 s.
- A throwaway relay remuxing it to HTTP MPEG-TS played on the device with
  picture and sound. The TV server calibrated from the decoder's timestamps and
  held 15 fps and 25 audio packets/s. At each end of a 40 s piece the decoder
  reopened the URL itself; the server logged one 88 ms gap and no reconnect.

## YouTube findings (2026-10-10)

- cstone is Ubuntu 22.04 with one vCPU and 1 GB. yt-dlp 2026.08.19 needs a
  JavaScript runtime for YouTube (deno 2.9.7). Even with it, every player client
  tried (tv, android_vr, mweb, web_safari, tv_simply, web_embedded, ios) was told
  "Sign in to confirm you're not a bot", so the node uses a cookies file from a
  secondary account, exported in a private window.
- With cookies: video info in 18.6 s (0.5 s cached), a channel's uploads in 6.6 s.
  The stream is 320x240 H.264 15 fps plus mono AAC, about 405 kbit/s; load on
  cstone stayed near 0.2 while it ran.
- A cast reached the device about 3 s after the switch (info cached by the cast
  call). The node is reached on its own port with a self-signed certificate
  pinned on Aliyun; the firewall admits only the Aliyun address.

## Delivery in steps

1. Spike.
2. Relay core with Bilibili creator channels; TV server reload.
3. Cast: page, SWITCH in server and firmware.
4. YouTube through cstone (needs SSH key access to cstone, set up like the Aliyun
   server).

Each step is usable on its own and is deployed and tried on the device before the
next.
