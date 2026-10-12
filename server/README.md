<p align="right">
  <a href="README.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Local audio/video prototype server

> **This fork's `server/` is canonical** (decision of 2026-10-12, Q7 in
> [`2026-10-12-multi-tenant-design.md`](../docs/superpowers/specs/2026-10-12-multi-tenant-design.md)):
> the cloud host runs this code, and server changes land here. Upstream's media
> server moved to [ai-passport-tv-server](https://github.com/cjpjxjx/ai-passport-tv-server)
> at commit `208357b`; this directory has diverged from it since (relay, multi-device,
> multi-user). Multi-user operation: [`docs/development/tenancy-phase1-runbook.md`](../docs/development/tenancy-phase1-runbook.md).


This is an opt-in, single-device **LAN player for live channels, video files and
synthetic media**. It is not an Internet proxy or a production authentication
service. Python 3.11+ and its standard library serve the media; ffmpeg is needed
to prepare files and to transcode the `live` subcommand, so that host needs it
installed. `numpy` is an **optional** pip dependency: with it, a live frame that does not fit its
byte target gives detail up perceptually (`server/perceptual.py`); without it, or with
`TV_PERCEPTUAL=0`, the server steps through the colour ladder as before. No device access
or global configuration changes are required. Start commands below from the repository root.

Channel addresses live in a fixed allowlist in `server/live.py`. They come from a
community IPTV playlist, so their availability and rights status are not verified;
they are intended for a private LAN test only. The device picks a channel by name
in its handshake, and the server falls back to `DEFAULT_CHANNEL` for anything it
does not recognise.

## Import a video file

```sh
python3 -m server.tv_server import-video --input /path/to/video.mp4 \
  --media-dir server/.local/my-video --seconds 60 --start 0
```

This accepts a local file, not a remote URL. Choose 1..600 seconds; the output keeps
display aspect ratio (including sample aspect ratio), fitting 16:12 (4:3) with
centered black borders at 160x120/12 FPS, plus 16 kHz mono PCM. No crop or stretch;
even YUV420 rounding may make opposite borders differ by up to two pixels. Silent
sources receive a silence track. JPEG payloads remain bounded at 24 KiB. Imported
media is read from disk per packet, never loaded as a complete movie into RAM.
Use the resulting directory with the same `run --media-dir` command below.

## Play a live channel

The shortest path, which starts everything and prints the address to type on the
device:

```sh
./run.sh                 # macOS and Linux
run.bat                  # Windows
```

It starts the media server and the channel page together, and restarts the media
server when the channel list is saved. Needs Python 3.9+ and ffmpeg; if either is
missing it says so and how to install it, rather than failing obscurely.

> **Windows is untested.** `run.bat` and the Windows branches in
> `tools/launch.py` are written from documented command behaviour and have never
> been run on a Windows machine -- the project has only been developed on macOS.
> Treat them as untried.

The individual commands, when you want to run one without the other:

```sh
python3 -m server.tv_server live --channel ch000      # media server, port 8096
python3 tools/channel_config.py                       # channel page, port 8097
```

`--bind` may be omitted, and usually should be: the server works out the
network address itself and prints both it and this computer's `.local` name. The
name is the better one to enter on the device, because it survives the router
handing out a different address. Supplying `--bind` by hand is still supported
for a machine with several interfaces.

```sh
python3 -m server.tv_server live --channel ch000 \
  --bind 192.168.1.20 --port 8096 --token-file <owner-only file>
```

Each device connection starts its own ffmpeg process and its own time origin, so
switching channels is just the device reconnecting with a different name. A
failed transcode ends that one session and the service keeps listening.

The channel table is read once, at start-up. `run.sh` and `run.bat` watch
`channels.txt` and restart the server when it changes; running the command above
directly means restarting it yourself to pick up an edit.

Old 320x240 JPEGs/manifests must be regenerated in a new directory. The device
requires 160x120 input and uses x2 nearest-neighbor output to 320x240; this does
not establish any device FPS improvement. Protocol timing and PCM are unchanged.

## Prepare once, copy, then run

```sh
python3 -m server.tv_server prepare --media-dir server/.local/media
# Optional: --ffmpeg /absolute/path/to/ffmpeg
```

The destination must not already exist. It contains a portable `manifest.json`,
`audio.s16le` (320,000 bytes) and `frame-000.jpg` through `frame-119.jpg`. Move/copy
the whole directory unchanged to the runtime host. Generated media and local
configuration under `server/.local/` are ignored by Git. A preparation failure
must be investigated; use a new destination for a retry, not a partly generated
set. `run` validates every frame before binding a socket.

The ten-second source has a three-digit frame counter (000–119), moving box,
one-frame white flash at each integer second, and a matching 50 ms, 1 kHz beep.
The beep amplitude is 0.08 (-22 dBFS); start with low device volume. PCM and video
both start at t=0. RGB frames are generated using the standard library (no font
package), then ffmpeg encodes baseline 160×120 JPEG 4:2:0 and 16 kHz mono s16le.
Oversized JPEGs retry at lower quality (q=10,18,25,31 after q=5); still-oversized
frames fail preparation. Frames are **never truncated**. This simple pattern
is not a worst-case JPEG decode/throughput benchmark.

Create the pairing file without placing its value in an argument or shell log:

```sh
python3 - <<'PY'
import os
import secrets
from pathlib import Path
folder = Path('server/.local')
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(folder / 'token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write(secrets.token_hex(32) + '\n')
PY
python3 -m server.tv_server run --media-dir server/.local/media \
  --bind 127.0.0.1 --port 8096 --token-file server/.local/token \
  --duration-seconds 1800
```

Provision the same token to the device through the separately managed private
configuration. Do not print or commit it. Alternatively, supply `TV_PAIRING_TOKEN`
through a secret-aware environment launcher; do not also use `--token-file`.
Tokens are 16–128 printable ASCII characters; use random tokens, not passwords.
Token files must be regular, owned by the process user, exactly mode 0400 or 0600,
and not symlinks. Run under an unprivileged account. Token comparison uses
`hmac.compare_digest`; authentication failures disclose no token or input JSON.

For the device, replace `127.0.0.1` with an **explicit local RFC1918 IPv4 address**
assigned to the server. Wildcard/public binds, hostnames, IPv6 and public peers
are rejected; port defaults to 8096. There is no TLS: use a trusted isolated LAN,
restrict access with the host's existing firewall policy, and never port-forward
this service. RFC1918 filtering alone does not protect against forwarded traffic.
The server accepts one authenticated stream and immediately closes extra clients
while streaming. A slow first HELLO has a 250 ms total deadline.

## Wire contract

TCP packets use the 24-byte network-order `!4sBBHIIII` header:

| Field | Value |
| --- | --- |
| magic, version, flags | `FAV1`, 1, 0 |
| type | HELLO=1, CONFIG=2, PCM=3, JPEG=4, END=5, ERROR=6, PALETTE=7, SWITCH=8, STATUS=9, VOICE_CTL=10, VOICE=11, VOICE_RESULT=12 |
| session | uint32; initial HELLO=0, server chooses a fresh nonzero value |
| seq | uint32; HELLO=0, CONFIG=0, then one global increasing server sequence |
| pts_ms | uint32 presentation timestamp on the shared media timeline |
| payload_length | uint32, validated before reading a payload |

The client first sends HELLO (session=seq=pts=0):

```json
{"version":1,"token":"<private pairing token>","channel":"cctv1"}
```

`channel` is optional and names the channel to play; omitting it, or naming an
unknown channel, selects the server default.

CONFIG contains `width=160`, `height=120`, `fps=12`, `sample_rate=16000`,
`channels=1`, `sample_bits=16`, `audio_chunk_ms=20`, `video_max_bytes=24576`,
`session`, and advisory `duration_ms=10000`, `start_delay_ms=200`,
`audio_lead_ms=200`, `video_lead_ms=50`. The live path also sends `channel` and a
`channel_list` array of `{id,name}` entries for the device to step through.
Control JSON is at most 1024 bytes.

`channels` is the audio channel count and must remain a number; `channel_list` is
a separate key. Do not merge them: sending the list as `channels` fails the
device's CONFIG validation and ends every session immediately.
PCM is exactly 640 bytes (320 samples, little endian) every 20 ms. JPEG payloads
are at most 24 KiB. END has no payload. The client may send END with the active
session or close the connection to stop; no inbound media/control commands are
allowed after HELLO. Invalid versions, flags, types, lengths, sessions or JSON
terminate the connection without searching for another magic. ERROR is reserved
and framing-supported; this server closes on errors rather than risking an ERROR
appended after a partially sent packet.

### Episode information

To firmware whose HELLO carries `"info": 1`, and for relay channels only, the
live path adds `playing` (`{title, episode, total}`) to CONFIG and sends
`SWITCH {"loading": {...}}` when the source is about to change and
`SWITCH {"loading": 0}` when the reopened source has prebuffered
(`server/episode_info.py`, `AVServer.info_*`, `reconnect.hold`). The episode
comes from the relay: `GET /info/<channel>` on its stream port (the stream URL
with `/ch/` replaced by `/info/`), asked for at most 0.5 s before CONFIG and in
a background thread whenever the source is reopened, and its `loading` events,
written before next, previous or an episode pick cuts the stream. A relay that
does not answer means no `playing` and no `loading`; playback is unaffected.
Device side: "Episode information" in
[`docs/local-tv-prototype.md`](../docs/local-tv-prototype.md).

### Playback progress

To firmware whose HELLO carries `"progress": 1`, and for relay channels only,
the live path sends `SWITCH {"progress": {"dur", "pos", "at"}}` anchors (ms):
the video's length and where it is at a given audio PTS of the session; the
device moves the bar on with its own sound clock. `server/progress.py` keeps
one session's anchor: when a source starts feeding (session start with
`at` 0, or `reconnect.hold` returning a new source with `at` = the next audio
PTS) it asks the relay's `/info/<channel>` in a background thread for
`duration` and `offset` (retrying up to three times a second apart until the
stream is active); when a hold begins (stall or pause) it resends the anchor
with `stop` = the PTS after the last real sound; when the same source recovers
it moves the anchor on by the silence. A video without a length sends
`{"progress": 0}` if a bar was up. Nothing per second; no per-frame work. The
v2 sender does not send these. Device side: "Playback progress" in
[`docs/local-tv-prototype.md`](../docs/local-tv-prototype.md).

## Voice control

Hold OK on the device, speak, let go: the device streams the utterance on its TV
socket, the media server has it recognised by muse-asr (SenseVoice) on the same
host, works out what was meant and answers. Design and the user's decisions:
[`docs/superpowers/specs/2026-10-12-voice-control-design.md`](../docs/superpowers/specs/2026-10-12-voice-control-design.md).
The code is in `server/voice/` (`session.py` packets and worker, `asr.py` the
bridge, `intent.py` + `match.py` + `numbers.py` + `pinyin_table.py` the rules,
`relay_client.py` the relay calls) and `server/relay/voice.py`.

### Packets

| Kind | Direction | Payload |
| --- | --- | --- |
| `VOICE_CTL = 10` | device to server | JSON, at most `CONTROL_MAX`: `{"op":"begin","id":n,"rate":16000,"codec":"adpcm"}`, `{"op":"end","id":n}`, `{"op":"cancel","id":n}`, `{"op":"pick","id":n,"ref":"..."}` |
| `VOICE = 11` | device to server | exactly one 324-byte IMA ADPCM block (the downstream audio layout, 40 ms); `pts` = ms since begin; at most 250 per utterance (10 s), more are dropped |
| `VOICE_RESULT = 12` | server to device | JSON, at most 2048 bytes (`VOICE_RESULT_MAX`), ASCII with `\u` escapes like every control packet |

- Both upstream kinds are accepted in every phase after CONFIG, wherever STATUS
  is (`AVServer._take_upstream()`, called from `_device_left()` and
  `_wait_until()`, so also while waiting for the first media, reconnecting or
  paused). Any other upstream kind still ends the session.
- On `end` a worker thread decodes the ADPCM, asks muse-asr, decides and calls
  the relay if needed. The send loop never waits for it.
- Results are queued and sent by the live loop (`voice_flush()`) and by
  `reconnect.hold()` while reconnecting or paused, each with the session's next
  `seq` and `pts` 0, like a SWITCH. During the first prebuffer they wait.
- A result answers the `id` it carries. `cancel`, a newer `begin` and a new
  session make an unfinished result for an older `id` disappear.
- CONFIG carries `"voice":{"max_s":10}` only when `TV_ASR_SOCKET` is set. HELLO's
  `"voice":1` is not needed by the server.

A result:

```json
{"id":7,"state":"final","intent":"channel","heard":"...","reply":"...",
 "do":{"channel":"ch017"},"label":"...","lead":"25"}
```

- `state`: `final`, or `error` (recogniser down or timed out, wrong audio
  format, voice off) with only `reply`. `partial` is not sent in phase 1.
- `do` holds exactly one key that `AV_SWITCH` already understands (`channel`,
  `volume` 0-100, `brightness` as the device's step index with 0 the brightest,
  `audio_only` 0/1) or one of the new `page` (`"status"`, `"close"`) and `setup`
  (1). A clear single match carries `do` and runs at once.
- `choices` (at most 6) replaces `do` when several things fit:
  `{"label","lead","do"}`, or `{"label","lead","ref"}` when choosing needs the
  server (`ep:<series>:<n>`, an episode of one of two series, through the relay;
  `ls:<site>.<kind>.<group>:<offset>`, the next page of a list or one group of
  the overview; `s<op>:<search>:<arg>`, a search result, its next page, the
  question or a creator's action, see below); the device then sends `pick` with that `ref` and the server
  answers with a new result for the same `id`. Only the refs of the latest
  result are accepted. Wi-Fi setup always comes as two choices (enter setup /
  cancel), because it ends playback.
- `intent`, `label` and `lead` are additions to the design's format: the kind of
  decision (`none` is "not understood", for the low earcon), and the row the
  panel shows for a single match (name, and the number the device lists it under).
- No `do` and no `choices`: show `reply` (the relay already acted: pause, seek,
  next episode; or nothing could be done, such as "a live channel cannot pause").

### What it understands (rules, no LLM)

Channel names with fillers and verbs stripped; aliases generated from names
(CCTV-n as "zhongyang n tao", "yangshi n tai", "n tao", CCTV-5+ as "wu jia",
the CCTV themes such as "yangshi tiyu"; satellite channels without "weishi";
singers on the music channels); toneless-pinyin fuzzy matching for homophones
(score = the larger of the character LCS ratio and the pinyin syllable
edit-distance ratio; numbers must agree); "di N tai"; groups as candidates;
next/previous channel within the group; series plus "di N ji" or "the latest
episode" through the relay's episode picker; next/previous episode, pause,
resume, seek and restart through the relay (on live channels the reply says it
cannot); volume up/down/value/mute/unmute; brightness up/down/value; audio
only on/off; the status page; Wi-Fi setup with confirmation; cancel; help.
Auto-execute at score >= 0.85 and a lead of 0.15 (an exact alias always
qualifies), or a single candidate >= 0.65; otherwise up to 6 candidates >= 0.5;
otherwise "not understood". `tests/test_voice_intent.py` holds the phrasings.

**Listing a category** comes before the series and channel matching, so a
question is never fuzzy-matched on its words. The sentence is cut into known
words (longest first): a site (YouTube, "you guan", "you tu", "B zhan", bilibili,
"bi li bi li"), a kind ("UP zhu" / "bo zhu"; "ju" / "ju ji" / "dian shi ju"), a
group ("yang shi", "wei shi", "gang tai", "guo ji", "yin yue", "dong hua",
"dian ying", "qi ta" ...), a plain noun ("pin dao", "tai", "jie mu", "ke yi kan
de") and question or filler words ("na xie", "shen me", "you mei you", "lie chu",
"xian shi", "gao su wo", "ding yue le" ...). If every character is covered and
there is a question word, it is a list question: "wo you na xie YouTube pin dao
gao su wo", "B zhan de UP zhu you na xie", "yang shi you na xie tai". A word that
is not known ("you mei you Hunan wei shi") makes it a channel request again, as
do two different groups ("yang shi dian ying" is CCTV-6). Saying only the
category ("wei shi", "YouTube pin dao", "B zhan UP zhu") also lists it, unless it
is exactly a channel's name or alias ("dian shi ju" stays CCTV-8). The answer
is `intent` `list` (`choices` for a bare category) with a reply that counts
them and the channels as choices (`do`); more than six are paged five at a time
with a last "next page, N more" row (an `ls:` ref). A site and nothing else
("wo you na xie YouTube pin dao", "B zhan you na xie pin dao") lists that site's
creators only; its series need "ju" / "ju ji" ("YouTube you na xie ju ji"). With no category
("dou you shen me tai", "you shen me ke yi kan de") the groups are listed, each
row opening that group's list. An empty category is `none` with a clear reply
("no YouTube series yet"); a site asked for while the relay did not report
sites says the relay did not answer. The relay catalogue
(`GET /api/voice/catalog`) carries `site` (`bilibili` / `youtube`) for every
creator and series.

A named series that does not exist ("Hu lu wa di 3 ji"), or a play verb
followed by a name that matches nothing ("wo yao kan Hu lu wa"), replies "could
not find" with that name, still with `intent` `none` for the low earcon; the
second case adds "you can say 'sou suo Hu lu wa'".

### Seeking

On creator, series and cast channels (live channels keep "a live channel cannot
..."): restart ("cong tou bo", "hui dao kai tou", "cong di yi miao kai shi",
"chong xin kan zhe ji"); a time with combined units ("tiao dao 3 fen 20 miao",
"tiao dao yi ge xiao shi ling wu fen san shi miao", "12 fen ban", "ban xiao shi",
"di 90 miao", "12:30" or "12 dian 30" as the recogniser writes it, "cong 5 fen
zhong kai shi bo", "bo dao 10 fen zhong de di fang", "dao 3 fen 20"); a share of
the video ("tiao dao yi ban" / "zhong jian", "san fen zhi yi", "si fen zhi san",
"bai fen zhi liu shi" / "60%", "tiao dao zui hou" = 30 s before the end); and
relative jumps with combined units ("kuai jin 1 fen 30 miao", "hou tui ban fen
zhong", "wang hui dao 20 miao"). A share, or a time said without "tiao dao" /
"dao" / "cong", is not a seek ("yi ban" alone, "di san ji" stays an episode).
The parser normalises the transcript itself (`intent.seek_text`), because the
shared normalisation drops the colon in "12:30" and the percent sign. The relay
works out shares from the playing video's own duration (`POST /api/seek` takes
`fraction` and `from_end` as well as `delta` and `position`), so it is never
stale, and answers "bu zhi dao zhe ge shi pin you duo chang" when the duration
is unknown; the reply then says where it went ("tiao dao 12 fen 22 miao"). A
spoken time past the end (`within: true`, sent with every voice `position`) is
refused with the video's length ("zhe ge shi pin zhi you 23 fen 40 miao")
instead of skipping to the next video, as the phone's progress bar does.

### Searching videos and creators

Voice only (the phone page has no search box). A search is recognised only
when the sentence starts with a search verb ("sou", "sou suo", "sou yi xia",
"bang wo sou"; "zhao" only together with "video" or "UP zhu") or ends in "...
de shi pin" ("wo xiang kan ... de shi pin", "fang yi ge ... de shi pin"), so
"wo xiang kan Hunan wei shi", "wo yao kan Quan ye cha di 3 ji", "kan ti yu",
list questions and playback or volume commands keep their meaning. It is
checked right after the status page, before volume and playback, because the
words searched for may contain "da sheng" and the like.

- **Site**: Bilibili by default; YouTube when the sentence says YouTube / "you
  guan" / "you tu" ("you guan sou lofi", "zai YouTube shang zhao lofi de shi
  pin"). The site is remembered across the question below.
- **Kind**: videos ("sou shi pin X", "sou suo X shi pin", "zhao X de shi pin",
  "wo xiang kan X de shi pin") or creators ("sou UP zhu X", "sou bo zhu X",
  "zhao X zhe ge UP zhu", "sou pin dao X", "sou you guan pin dao X", "YouTube
  shang zhao X pin dao"). When it is unclear ("sou suo Li Zi Qi", "sou yi xia
  Lao Fan Qie", "bang wo sou Luo Xiang") the server does not guess: the result
  is `intent` `ask`, reply "search videos or UP zhu?" and two choices ("sou shi
  pin" / "sou UP zhu", refs `sq:<n>:v` / `sq:<n>:u`). It stays on screen like
  any list; the next utterance within 30 s may also answer it ("shi pin", "UP
  zhu", "bo zhu", "YouTube de shi pin", "di yi ge"). Anything else drops the
  question.
- **Results** are choices, five per page plus "next page, N more" (`sp:<n>:<offset>`),
  numbered from the top in `lead` (`01`...`20`). A video row is the title, then
  " · duration" and " · uploader" when they fit the row (22 columns); a creator
  row is the name and " · 2084 wan fen" (followers) or the video count. Labels
  drop characters the device's font cannot draw (emoji) and are cut to 47 bytes.
  Choosing a video (`sv:<n>:<i>`) casts it through the relay: the device switches
  to "my cast", plays it and goes back to its channel at the end, exactly like a
  phone cast. Choosing a creator (`su:<n>:<i>`) offers "subscribe as a channel"
  (`sf:`) and "play the newest video" (`sl:`). While results are on screen,
  "xia yi ye" pages and "di er ge" / "bo fang di san ge" picks by voice (the
  number in the row's lead, or the row on the page); "ding yue" / "bo fang zui
  xin" answer the creator's two choices. Without results on screen "di er ge"
  keeps meaning channel 2. Only refs of the latest search are accepted.
- **Filtering**: Bilibili drops paid (`is_pay`, `badgepay`), charge-only
  (`is_charge_video`), bangumi and course pages, live rooms and entries without
  a duration; creators without uploads are dropped. YouTube drops live,
  upcoming and just-ended streams, Shorts, deleted or private entries and
  entries without a duration. Members-only videos cannot be told from the
  search data; they fail like a phone cast does.
- **Latency**: the device waits 8 s for a result. The media server gives the
  relay what is left of 7 s after recognition (`RESULT_BUDGET_S`) and the relay
  asks the site for 0.7 s less, so a slow site answers "B zhan / YouTube sou
  suo tai man le, shao hou zai shi" in time. Bilibili search is one unsigned
  request (`/x/web-interface/search/type`, `search_type=video` or `bili_user`,
  with the buvid3 cookie; no wbi signature needed), about 1 s. YouTube goes to
  the node's `/yt/search` (yt-dlp, one at a time): the node waits at most 3 s for
  a running yt-dlp and answers busy otherwise, and it finishes a search even
  when the relay gave up, so saying it again is quick. Results are cached for 5
  minutes on the relay and 10 minutes on the node.
- **Subscribing** adds the creator like the phone's "add UP zhu" (same 12-creator
  limit, and only when the device's channel list still fits CONFIG), to the
  device owner's subscriptions; it is the one piece of account data a device
  guest may change. "Play the newest" casts the newest upload (Bilibili's
  search already carries it). Both may need the site for longer than a voice
  result can wait (listing a YouTube channel takes 10-20 s); the relay then
  answers "pending" and finishes in the background: the new channel appears in
  the device's list, the cast switches the device as usual.

Relay endpoints (always with `device`, served as that device's guest in
multi-user mode): `GET /api/voice/search?device=&site=bilibili|youtube&kind=video|up&q=&wait=`,
`POST /api/voice/cast {device, site, id, title, duration}`,
`POST /api/voice/creator {device, site, id, name, action: follow|latest, wait}`.
The node endpoint is `GET /yt/search?q=&n=&kind=video|channel`.

Relay actions always carry `device`, so they act on the device that spoke, not
on the one the phone is driving (`Relay.target()`). Which device that is depends
on the relay's mode:

| Relay mode | `device` the media server sends | Where it comes from |
| --- | --- | --- |
| Single-user (no `--db`; media server without `TV_DB`) | the relay's device id (`d0` = `--device-name`, `d1`... = each `--extra-device` in order) or its name (e.g. `Passport`) | `TV_RELAY_DEVICE`, fixed per unit |
| Multi-user (`--db`; media server with `TV_DB`) | the device id the media server admitted for this session: `legacy-xingzhi` / `legacy-passport` for old firmware or a not-yet-adopted new one, the 12-hex MAC once adopted or bound | the HELLO handshake (`_admit()` then `VoiceService.bind_device()`); `TV_RELAY_DEVICE` is ignored, with one log line saying so |

In multi-user mode the relay key is the admin, who controls only the admin's
own devices, yet a media unit may serve anyone's device. So a request with the
key (`X-Key`) that names a device on a single-device endpoint (`switch`, `seek`,
`next`, `previous`, `pause`, `resume`, `device`, `audio`, `pet`,
`series/<id>/play`, `series/<id>/episodes`, `voice/catalog`, `voice/search`,
`voice/cast`, `voice/creator`) is served as that device's **guest**: that one
device, its owner's series and progress, no account data except a voice
subscription to the owner's creators. Names are not accepted there (users choose them, so they are not unique),
and an unknown or unbound id answers 404. Phones (cookie sessions) keep their
own rules. An unbound device gets neither `voice` in CONFIG nor a relay client.
An utterance finishes with the relay client of the session it started in.

### Enabling it (operator)

1. muse-asr (repository `muse-gadget-sdk`, `esp32/tools/muse_asr`) with
   `MUSE_ASR_SOCKET=/run/muse-asr/asr.sock`, `MUSE_ASR_SOCKET_GROUP=tvserver`,
   `RuntimeDirectory=muse-asr`, `RuntimeDirectoryMode=0755` and
   `SupplementaryGroups=tvserver` (its service template has them). The socket
   is mode 0660, owner muse-asr, group tvserver: that permission is the only
   credential, no key is shared. Check with
   `sudo -u tvserver curl --unix-socket /run/muse-asr/asr.sock http://x/health`.
2. Each media server unit:

   | Variable | Value |
   | --- | --- |
   | `TV_ASR_SOCKET` | `/run/muse-asr/asr.sock`; unset means voice is off and CONFIG does not offer it |
   | `TV_RELAY_KEY_FILE` | the relay's key file (same as `tv-relay.service`'s `--key-file`) |
   | `TV_RELAY_DEVICE` | single-user relay only: this unit's device in the relay, `d0` / the `--device-name`, or the name or id of its `--extra-device` (e.g. `Passport` or `d1`). Ignored when `TV_DB` is set; keep it in the base unit anyway, so removing the multi-user drop-in brings it back |
   | `TV_RELAY_MANAGE_URL` | optional, default `http://127.0.0.1:8100`; only loopback is accepted, so the key never leaves the host |

   Without the relay variables (`TV_RELAY_KEY_FILE`, and in single-user mode
   `TV_RELAY_DEVICE`), channels, volume, brightness, pages and setup still
   work; series episodes, pause, seek and next/previous reply that the TV is
   not connected to the relay. The unit's user must be in the `tvserver` group
   (the socket) and able to read the key file.
3. The relay needs this version (it accepts `device` on its POST endpoints and
   serves `GET /api/voice/catalog` and the search endpoints); searching YouTube
   also needs this version of the YouTube node (`server/relay/ytnode.py`, for
   `/yt/search`); with an older node YouTube search answers that YouTube did not
   respond, and Bilibili search works. Restart order: muse-asr, tv-relay, then the
   media servers. Multi-user mode adds nothing for voice: the drop-ins in
   `packaging/systemd/tenancy/` leave these variables alone
   ([runbook](../docs/development/tenancy-phase1-runbook.md#voice-control-in-both-modes)).

Measure the recogniser on the host before promising latency: muse-asr logs
`local ok: ... asr N ms` per request, and the media server logs
`voice id=... intent=... audio_s=... asr_ms=... total_ms=...` per command.

### Privacy and logging

- Audio is never stored. The log has the intent kind, lengths and latency only.
- `TV_VOICE_LOG_TEXT=1` adds the recognised words (`heard='...'`) to the log, for
  tuning the thresholds. It is off by default; turn it off again afterwards.
- `TV_VOICE_WAV_DIR=<dir>` (development only, off by default) saves every
  utterance as a WAV file there, to build a test corpus. Never set it on a
  shared host.

## Scheduling, bounds and shutdown

- Server time origin is CONFIG write completion plus 200 ms. Audio is scheduled
  200 ms before its PTS; video 50 ms before its PTS. Network arrival and actual
  device playback timing are not guaranteed by send scheduling.
- **Wire PTS is not globally sorted across types.** Events merge by send deadline,
  preserving strictly increasing PTS within PCM and within JPEG, and globally
  increasing sequence numbers. Sorting both types by PTS would put video in front
  of audio that needs earlier prefetch, causing head-of-line delay. For example,
  audio PTS=140 precedes video PTS=0. The receiver must track PTS per stream.
- At the ten-second loop boundary indices wrap, but PTS continues to 10000 and
  beyond in the same session. Integer timestamps avoid cumulative rounding drift:
  video PTS=`frame_index*1000//12`. Default run duration is 1800 seconds (180 loops),
  configurable from 1 to 86400 seconds. Reconnect creates a new session and origin.
- There is no application send queue and no real-time transcoding. Memory holds
  a fixed ten-second set (at most about 3.3 MiB), plus one bounded outgoing packet.
  Requested kernel send buffer is 32768 bytes; OS rounding/doubling is possible.
- Nonblocking socket reads/writes use **250 ms total packet deadlines**, not a
  fresh timeout for every fragment. Busy sockets and cancellation are polled at
  most every 50 ms between operations. A timed-out partial send closes immediately.
- JPEG more than 83 ms past presentation is dropped before sending. Audio more
  than 100 ms past presentation closes the session instead of catching up an
  unbounded stale backlog. Reconnect/rebuffer is the receiver's responsibility.
- SIGINT/SIGTERM stops the local server without background workers; otherwise a
  finite stream emits END at the end of the timeline and the listener remains
  ready for another client. User-space cancellation is normally within 300 ms;
  scheduler/OS suspension is not a hard real-time guarantee.
- Logs include authenticated session IDs, sent PCM/JPEG counts every ten seconds,
  dropped video totals and close/failure summaries. They never include credentials
  or received payloads. These are **socket submission counts**, not DAC/DMA,
  acoustic playback, or audiovisual synchronization acceptance measurements.

## Host validation

```sh
python3 tests/test_tv_server.py -v
python3 tests/test_video_import.py -v
```

Tests cover exact header bytes, fragmentation/coalescing, EOF, no magic resync,
unknown fields, payload and uint32 limits, slow receive/send deadlines, token
sources/permissions/authentication, session mismatch, busy-client rejection,
stream termination, JPEG metadata, and the full 30-minute **simulated** schedule
(90,000 PCM packets and 21,600 JPEG frames). When ffmpeg is on PATH, the test also
prepares and reloads ten seconds and checks all ten audio marker windows; without
ffmpeg that preparation test is explicitly skipped. A metadata fixture used by
framing tests is not an image-decoder test.

A 30-minute wall-clock device run, DMA/audio timing, RF performance and JPEG ROM
decoding are separate acceptance work. No host test result implies those passed.
See [repository test guidance](../docs/development/engineering/build-and-test.md).

## Scope notes

The required unequal prefetch leads are retained; only the interpretation of
cross-type PTS ordering is made explicit. Closing rather than replying ERROR on
invalid input avoids framing ambiguity. Extra CONFIG fields are advisory.
Documentation is kept beside this server because this implementation's edit
scope is `server/` and `tests/test_tv_server.py`; documentation-index/changelog
integration belongs to the coordinating change. No firmware or deployment
configuration is changed by this module.
