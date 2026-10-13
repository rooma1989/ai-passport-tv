<p align="right">
  <a href="2026-10-13-wifi-setup-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Easier Wi-Fi setup — design

Status: built on branch `wifi-setup`, 2026-10-13. Host tests and both board
builds pass; nothing has run on a device yet (section 9 lists what to check).
The sound decoder itself is a separate module (branch `sound-prov`, its own
design note `2026-10-13-sound-provisioning-design.md`); this note covers how
setup mode uses it.

## 1. Goal

Setting up the device's Wi-Fi used to mean: switch the phone to the device's
hotspot, open a page, type the network, the password and (on a public build) a
server address, switch the phone back. Three changes make it shorter:

- **A. Sound.** In setup mode the device also listens. A phone page plays the
  network name and password as sound; the device decodes it, joins the network
  and restarts. The phone never leaves its own network.
- **B. Remote management.** While the device is online, its owner can list,
  add, remove, reorder and switch its saved networks from the phone remote and
  the PC console — for example, add grandma's Wi-Fi before taking the device
  there. The password travels end-to-end encrypted.
- **C. No server step.** A build that knows its server hides the server field
  behind a small "Advanced" toggle; a device that cannot reach any saved network at
  boot opens setup by itself.

## 2. Setup mode, as the viewer sees it

```
boot ─┬─ no network saved ───────────────────────────────► setup (as before)
      └─ networks saved ─► none answers for 60 s ─────────► setup, "automatic"
                                   (once per boot, only if never online since boot)
setup:  page 1 sound setup   QR → <SITE>/TV/W, "example.com/TV/W", "hold near the TV, press Send"
        page 2 join the hotspot (hotspot QR)   page 3 open the setup page (http://192.168.4.1)
        page 4 help (help URL builds)          page 5 status
```

- Sound is page 1 only when the decoder is linked in; without it the pages are
  exactly as before (renders compared pixel by pixel).
- The page's code is upper case (`HTTPS://HOST/TV/W`) so it stays in QR
  alphanumeric mode (version 1 for a usual host). The relay serves the page at
  `/tv/wifi` and `/TV/W` (nginx `location = /TV/W`, sound-prov branch).
- The site comes from, in order: the server's last `remote_url` (stored in NVS
  `avsettings/site` whenever CONFIG carries a new one), Kconfig
  `CONFIG_AV_PUBLIC_SITE` (git-ignored sdkconfig), the site of `AV_HELP_URL`
  (private header). With none, the page shows the path without a code.
- While listening the line under the code says "hold near the TV and press
  Send", then "receiving… 60%", "got it, connecting…", "connected, restarting",
  or in vermilion "did not connect, check the password and send again" (six
  seconds, then listening again; the exact Chinese strings are in
  `av_setup_sound_line()`). Hearing a message
  jumps the screen to page 1 from any page.
- A decoded network goes through exactly the web form's path
  (`WifiConfigurationAp::ConnectAndSave`: test-join, save with its channel,
  store the server if one came along), then setup ends and the device restarts.
- An **automatic** setup closes again, returning to playback, when the access
  point's periodic scan sees a saved network and nobody is using setup (no phone
  on the hotspot, no message being received) — a router that was rebooting
  does not leave the device stuck in setup.

### Microphone

Setup already opens the codec at 16 kHz/16 bit/mono for its hiss, which is what
the AI Passport's ES8311 duplex microphone needs (`bsp_mic_start` reads the open
I2S0). The Xingzhi opens its own I2S1 microphone (format still unverified on
that board). A task (`av_sound`, 6 KB stack) reads 20 ms blocks and feeds the
decoder continuously; the decoder's state is one 14.9 KB allocation, freed with
the task when setup ends. Every 2 s the log prints `progress=… peak=…`, which
tells "heard nothing" from "heard but could not decode".

`CONFIG_AV_SOUNDPROV_DUMP` (default off) prints half a second before a message
starts plus `CONFIG_AV_SOUNDPROV_DUMP_S` (10) seconds of microphone PCM as
`SNDDUMP` lines; `tools/soundprov_dump.py` turns a saved console log into WAVs
for `tests/test_av_soundprov --wav`.

### Server field (C)

`main/Kconfig.projbuild` adds `CONFIG_AV_DEFAULT_SERVER` (host[:port]) for a
public build; a private build's `AV_SERVER_IPV4`/`AV_SERVER_PORT` count too.
Either makes the setup page get `"server_builtin": true` from
`/advanced/config`: the server field is hidden behind an "Advanced" toggle, not required, and
its note says the address is built in. A stored address still wins over both.
Host names stay out of committed files: these live in the build's own sdkconfig
or the git-ignored private header.

**Port.** An address with no port ("example.com", as typed on either page)
means the server this build is made for: the private header's
`AV_SERVER_PORT`, or in a public build `CONFIG_AV_DEFAULT_SERVER_PORT` (8097 on
the AI Passport, 8098 on the Xingzhi board). Earlier firmware dialled 8096 (the
local `server/tv_server.py` port) for such an address, so a device given a bare
domain timed out against a relay listening elsewhere. An explicit port is kept,
with one migration: `name:8096` on a public DNS name (a dot, not an IPv4
literal, not `.local`) — what that firmware and its page placeholder produced —
also takes the build's port; LAN addresses keep 8096 for the local server.
`av_server_addr_use_build_port()` (tested in `tests/test_av_server_addr.c`)
applies it whenever the address is read, at every session, so a stored value
needs no new setup. The log says `Server address: host:port (stored|built in[,
port from this build])` once and on every change, a failed dial says
`RX connect to host:port failed errno=N`, and a name that does not resolve
`Cannot resolve host: getaddrinfo=N`.

**Defaults on the pages.** The hotspot page's server field starts with what the
device dials now (`"server_default"` in `/advanced/config`) when nothing is
stored, and sends `""` when it is left unchanged, so a built-in address stays
built in. The sound page (`wifi.html`) fills its "Advanced" server field with
the hostname that served it (`location.hostname`, not localhost) and sends it;
clearing the field sends nothing, and an empty server never erases the
device's own.

## 3. Remote management (B), end to end

```
phone/PC ──HTTPS──► relay ── events file ──► media server ──TCP──► device
 POST /api/wifi     seal for the device      SWITCH {"wifi": env}   decrypt, apply
 GET  /api/wifi  ◄── DB device_wifi ◄──────── STATUS {"wifi": ack} ◄─ ack + list
```

1. The device's HELLO (firmware with the feature) carries `"wifimgr": 1`,
   `"wkey"` (its X25519 public key, base64url) and `"wifi_saved"` (display names,
   priority order). The media server's gate stores them in `device_wifi`
   (trust on first use of the key; see section 4).
2. CONFIG carries `"wifi_key"`: the relay's public key (from DB meta
   `wifi_pub`, which the relay writes at start-up).
3. The owner presses add, remove, move up or switch now. `POST /api/wifi` checks: owner (or
   an admin who is a member) — anyone else gets 404 like every other device
   API; firmware supports it; key not changed; no change still waiting; device
   online (otherwise "the TV is offline", nothing queued). It seals
   `{"seq", "op", "ssid", "password"|"i"}` for the device and appends a `wifi`
   event (`text` = envelope, `value` = Unix seconds) to that device's events file.
4. The media server forwards it as `SWITCH {"wifi": {"v":1,"n":…,"c":…}}` only
   to firmware that sent `wifimgr`, and only if the event is under 30 s old
   (also from the paused loop's requeue).
5. The device's receive task hands the envelope to a short-lived worker task
   (6 KB stack) that decrypts, checks (`av_wifi_ops.c`), changes the list
   (SsidManager, NVS) and writes the ack; the receive task sends it as
   `STATUS {"wifi":{"ack":seq,"ok":0|1,"err":code,"saved":[…],"cur":"…"}}`.
   For "switch now" the worker waits until the ack has left, then moves the network
   to the front and restarts the station; if it cannot join, the station goes on
   to the next visible network in order, as at boot.
6. The media server stores the ack; the page polls `GET /api/wifi` every second
   and shows saved, the error, or "no answer from the TV" after 20 s. For a switch it
   waits until the device is back online on the new network (or reports that it
   came back on another one).

Rules the device enforces (`av_wifi_plan`): `seq` strictly greater than the last
applied (NVS `avsettings/wseq`); add needs a 1–32 byte name and an empty or
8–64 character password, and is refused (`full`) when ten are saved unless the
name is already saved (then only its password changes); remove/up/switch name
the network by index **and** reported name, refused (`changed`) if the list
moved meanwhile. Error codes map to Chinese text in `server/tenancy/wifi.py`.

### Priority order

The station used to join the strongest saved network. It now tries visible
saved networks in list order, except that any at or below −80 dBm go after all
usable ones (`components/esp-wifi-connect/wifi_order.c`, host-tested). When the
first network's channel is not yet known (just added or moved up) the scan
covers all channels rather than only the saved ones. SsidManager keeps up to
ten (`kMaxCount`) and is now locked against the remote's worker.

## 4. Encryption

### Threat model

| Party | Can see | Must not get |
| --- | --- | --- |
| Anyone on the path device ↔ media server (plain TCP: Wi-Fi neighbours, ISP, a hotel network) | every HELLO, CONFIG, SWITCH, STATUS | the password; the ability to change the device's networks |
| Same, but active (can inject and rewrite packets) | as above, and can impersonate either end | the password of a message sent after the keys were pinned |
| Someone who reads the events file or the database | envelopes, public keys, SSIDs | the password |
| The relay process / server operator | the password, briefly in memory while sealing | — (trusted: it already controls the device's streams) |

Not in scope: the phone ↔ relay hop (HTTPS via nginx), an attacker controlling
the relay or the media server, and the first contact (both pins are trust on
first use, like `device_secret`).

### Why not a key from `device_secret`

The brief proposed `HKDF(SHA-256(device_secret))`, which both sides can
compute. But the device sends `device_secret` **in clear in every HELLO**
(multi-tenant design 8.1), so anyone who can read the link — exactly the
attacker this is for — can compute that key as well. It would hide nothing.

### Scheme

- **Device key**: X25519 key pair made once on the device (hardware RNG after
  Wi-Fi is up), NVS `avsettings/wkey` (private + public). Only the public half
  leaves, in HELLO `"wkey"`. The server pins it in `device_wifi.pub` at first
  sight; a different key later is not accepted (`key_changed` on the page)
  unless the device is in its re-pair window (owner pressed "re-pair"), which
  re-pins it together with the secret.
- **Relay key**: X25519 key pair in a file next to the DB (`wifi-key`, 0600,
  `--wifi-key` to move it); public half in DB meta `wifi_pub`, sent in CONFIG
  `"wifi_key"`. The device pins it with the server address it dialled
  (`avsettings/wsrv`, `wsrva`); another address re-pins; the same address with
  another key is refused until setup is completed on the device (which clears
  the pin).
- **Message key**: `shared = X25519(own private, peer public)` (both sides get
  the same value; it never crosses the wire). `key = HKDF-SHA256(salt =
  device_pub ‖ relay_pub, ikm = shared, info = "ai-passport-tv wifi v1", 32)`.
  An all-zero shared secret (low-order point) is refused.
- **Message**: AES-256-GCM, 12-byte random nonce, AAD `"avwifi1"`, 16-byte tag.
  Envelope `{"v":1,"n":b64url(nonce),"c":b64url(ciphertext‖tag)}`; plaintext at
  most 320 bytes of JSON with `seq` (relay milliseconds, strictly increasing per
  device). The device rejects a `seq` not larger than the last applied, so a
  captured message cannot be replayed.

What this gives: a passive reader of the TCP link learns nothing about the
password; an active one cannot read or forge messages once both keys are pinned
(static-static Diffie-Hellman authenticates both ends); a database or events
file leak exposes no password (the DB holds only public keys and SSIDs). What it
does not give: forward secrecy (a stolen device or relay private key decrypts
recorded messages to that device), and protection at first contact.

Implementation: device — mbedTLS (Curve25519 group, HMAC-SHA256 for HKDF since
`MBEDTLS_HKDF_C` is off, hardware AES-GCM), `main/av_wifi_crypto.c`; server —
`server/wifi_crypto.py`, standard library only (textbook AES/GCM/X25519; not
constant-time in Python, acceptable because the relay computes only when an
owner presses a button). Both are tested against RFC 7748, RFC 5869 and the GCM
specification's vectors and against one shared envelope
(`tests/test_wifi_crypto.py`, `tests/test_av_wifi_crypto.c`; the C test builds
ESP-IDF's mbedTLS sources on the host).

The SSIDs are not encrypted on their way back (they are broadcast anyway);
non-UTF-8 bytes are shown as `?` so a strange SSID cannot break the JSON.

## 5. Wire summary

| Where | Field | Who | Meaning |
| --- | --- | --- | --- |
| HELLO | `"wifimgr": 1` | firmware | understands SWITCH `"wifi"`, sends STATUS `"wifi"` |
| HELLO | `"wkey"` | firmware | device X25519 public key (43 chars) |
| HELLO | `"wifi_saved"` | firmware | saved networks, display names, priority order |
| CONFIG | `"wifi_key"` | media server, multi-user only, to `wifimgr` firmware | relay X25519 public key |
| SWITCH | `{"wifi": envelope}` | media server | one sealed change |
| STATUS | `{"wifi": {"ack", "ok", "err", "saved", "cur"}}` | firmware | answer; `ack: 0` = could not decrypt |
| events | `{"action":"wifi","text":envelope,"value":unix_s}` | relay | forwarded within 30 s |

No new packet kind: SWITCH and STATUS carry it, gated by the HELLO flag, so
older servers and older firmware are unaffected. The HELLO's existing
`"wifi"."ssid"` diagnostic is now also passed through the display-name filter.

## 6. Storage

- Device NVS `avsettings`: `wkey` (64 B), `wsrv` (32 B) + `wsrva` (address),
  `wseq` (u64), `site` (string). Networks stay in esp-wifi-connect's `wifi`
  namespace, as before.
- Server DB table `device_wifi` (created on open; old databases gain it):
  pinned key, mismatch time, last reported names and current SSID, last seq,
  pending change, last ack. Never a password.

## 7. Memory (firmware)

- Static RAM: a few hundred bytes (key, ack buffer of 900 B, flags). Measured
  totals are in the branch report.
- While a change is applied: a 6 KB worker stack plus about 1.3 KB of heap and
  mbedTLS's short-lived bignums, all freed within about a second. The key pair
  is made once per device on a temporary 6 KB task before the session allocates
  its buffers.
- Setup mode with sound: the decoder's 14.9 KB plus a 6 KB task, freed when
  setup ends; the dump option adds 16 KB.

## 8. Files

Firmware: `main/av_wifi_crypto.*`, `main/av_wifi_ops.*`, `main/av_wifimgr.*`,
`main/av_soundsetup.*`, `main/av_provision.*` (saved-network API, setup helpers),
`main/av_provision_policy.*` (auto setup, pages, site, sound line),
`main/av_store.*`, `main/av_player.c`, `main/ui_overlay.c` (five page squares),
`components/esp-wifi-connect/` (`wifi_order.c`, SsidManager lock and `MoveUp`,
`ConnectAndSave`, `server_builtin`, page toggle). Server: `server/wifi_crypto.py`,
`server/tenancy/wifi.py`, `server/tenancy/db.py`, `server/tenancy/gate.py`,
`server/tv_server.py`, `server/cast_switch.py`, `server/relay/events.py`,
`server/relay/wifi.py`, `server/relay/web.py`, `server/relay/__main__.py`,
`server/relay/assets/remote.html`, `server/relay/assets/console.html`.

## 9. Not verified yet

- Nothing has run on a device: key generation, the worker's stack use (it
  logs its high-water mark), NVS writes during playback, the switch.
- The Xingzhi microphone format; the Passport's codec hiss next to its
  microphone during sound setup.
- Sound decoding in a real room (decoder branch); a full sound setup end to end.
- The automatic setup's exit when a router comes back.
