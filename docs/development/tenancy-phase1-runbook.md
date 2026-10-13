<p align="right">
  <a href="tenancy-phase1-runbook.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Multi-user phase 1: deploy, migrate, roll back

Operator runbook for phase 1 of
[`2026-10-12-multi-tenant-design.md`](../superpowers/specs/2026-10-12-multi-tenant-design.md):
accounts, invites, device binding, per-user relay state, on the current host.
`<site>` is the website's address. Nothing here has been run on the cloud host
yet; every step was exercised on a local copy of synthetic state only (see
"Not verified" at the end).

## What changes

| Before | After |
| --- | --- |
| One `relay-state.json`: creators, series, progress, casts, phone sessions, one sign-in code | `tv.db` (SQLite, WAL): users, hashed sessions, invites, devices, per-user state, per-device cast slot, shared video-list cache, audit log |
| Anyone with the key, the device's QR code or a signed-in phone controls every device | Each user sees and controls only their devices; the device's QR code gives a **guest** session for that one device; the key link signs in as the admin |
| `relay-channels.txt`, `channel-groups.json`, `now-playing.json`, `relay-events.json`, `remote-url.txt` shared or per unit | One set per device under `/var/lib/ai-passport-tv/devices/<device id>/` |
| Devices authenticate with one shared pairing token | New firmware sends `device_id` (factory MAC), `device_secret`, `model`, `fw`; old firmware's token is mapped to the legacy device of the port it dials |

What does not change: the media path (two static units, `tv-xingzhi` on 8098 and
`tv-passport` on 8097, one device each at a time), `channels.txt` (the global
live table, read-only to the relay), the wire protocol, and the phone page's
address. The old files are never written to: they are the rollback.

Everything is switched on by configuration, not by new code paths replacing old
ones: the relay without `--db` and the media server without `TV_DB` behave
exactly as before. Both modes live in the same code, so a rollback needs no
older checkout.

## Data model

```
user ──< device_member >── device            (owner | member; admin, user)
 ├── session  (phone 180 d, pc 30 d, guest 30 d; SHA-256 of the token only)
 ├── user_state  {creators[], history[], group_overrides{}}   ← relay-state.json's shape
 └── invite   (SHA-256 of the code, 7 days, single use)
device_state  {cast: {video, position, done}}                 ← relay-state.json's "cast"
source_cache  (site, source, kind) → videos                   shared by every follower
audit_log, meta (schema_version, public_url, legacy_token)
```

Device ids: `legacy-xingzhi` / `legacy-passport` (migrated, old firmware,
`legacy_port` 8098 / 8097) and 12-hex-digit MACs for new firmware. A new device
is stored `unclaimed` with the SHA-256 of its secret (trust on first use) and a
20-character claim code; binding makes it `active` with an owner. `retired` is a
legacy record whose device was adopted.

## 0. Prepare (no downtime)

1. Copy the new `server/` to `/opt/ai-passport-tv/` the way you do today (keep the
   previous copy, for example as `/opt/ai-passport-tv/server.prev`, although the
   rollback below does not need it).
2. Read the Passport unit as it is on the host — it is a hand-made copy and is
   not in the repository:

   ```sh
   systemctl cat tv-passport | grep -E 'TV_|ReadWritePaths|ExecStart'
   ```

   Note its data directory (the migration's `--passport-dir`, normally
   `/var/lib/ai-passport-tv-passport`).
3. Back up (design 9.1, step 1):

   ```sh
   stamp=$(date +%Y%m%d-%H%M%S)
   tar -C / -czf /root/tv-backup-$stamp.tgz var/lib/ai-passport-tv var/lib/ai-passport-tv-passport etc/ai-passport-tv
   find /var/lib/ai-passport-tv /var/lib/ai-passport-tv-passport -maxdepth 1 -type f -exec sha256sum {} + > /root/tv-backup-$stamp.sha256
   ```

## 1. Dry run (no downtime)

Run as the service user, so every file it creates later is owned correctly:

```sh
cd /opt/ai-passport-tv
sudo -u tvserver python3.11 -m server.tenancy.migrate \
    --data-dir /var/lib/ai-passport-tv --passport-dir /var/lib/ai-passport-tv-passport --dry-run
```

It builds a database in a temporary directory with the same code as the real run,
prints both sides' counts and deletes it. The tool speaks Chinese; the table has
one row per item -- creators, series, videos in the lists, watched marks,
positions, cast history, phone sessions, cast slot, group overrides, sign-in code
kept, live channels in `channels.txt` -- with the old files' count and the
database's count side by side. Every row must match, the tool must report that
the old files did not change, and it must exit 0 saying the rehearsal wrote
nothing. A row flagged as differing, a report that an old file changed, or a
non-zero exit stops here. Use `--no-passport` if there is no Passport unit.

## 2. Cut over (a few minutes of downtime)

```sh
systemctl stop tv-relay tv-xingzhi tv-passport
cd /opt/ai-passport-tv
sudo -u tvserver python3.11 -m server.tenancy.migrate \
    --data-dir /var/lib/ai-passport-tv --passport-dir /var/lib/ai-passport-tv-passport \
    --public-url https://<site>
```

It refuses a database that already holds data (`--replace` renames it to
`tv.db.bak-<time>` first). It writes `tv.db` and
`devices/legacy-xingzhi/`, `devices/legacy-passport/` with each device's channel
table, groups, QR address, and copies of `now-playing.json` and
`relay-events.json`. The old sign-in code becomes the Xingzhi's remote code, so
its status page and any printed QR code still work; signed-in phones stay signed
in (as the admin).

Install the drop-ins (templates in `packaging/systemd/tenancy/`; replace
`<site>` and check the paths and the Python binary against your base units):

```sh
mkdir -p /etc/systemd/system/tv-relay.service.d /etc/systemd/system/tv-xingzhi.service.d /etc/systemd/system/tv-passport.service.d
cp packaging/systemd/tenancy/tv-relay.conf /etc/systemd/system/tv-relay.service.d/tenancy.conf
cp packaging/systemd/tenancy/tv-media.conf /etc/systemd/system/tv-xingzhi.service.d/tenancy.conf
cp packaging/systemd/tenancy/tv-media.conf /etc/systemd/system/tv-passport.service.d/tenancy.conf
systemctl daemon-reload
```

Update nginx with the new `/TV/B/` location and the `X-Real-IP` header from
[`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf), then
`nginx -t` and reload.

Start and check:

```sh
systemctl start tv-relay tv-xingzhi tv-passport
journalctl -u tv-relay -n 5        # the start line says multi-user, 1 user, 2 devices
python3.11 -m server.tenancy.admin --db /var/lib/ai-passport-tv/tv.db devices
```

- Open the remote from a phone that was signed in before: it shows both devices,
  your creators, series and history. Switch a channel.
- On either device, change channel; the media server logs
  `device=legacy-xingzhi legacy` (or `legacy-passport`).
- Scan the Xingzhi's status-page QR with a phone that is **not** signed in: it
  opens the remote as a guest for that device only (only the Remote and Cast tabs).

## 3. Using it

- **Invite family.** On the admin's phone: Me → Invite family;
  or `python3.11 -m server.tenancy.admin invite --name Mum`. Send the link
  (`https://<site>/tv/i/<code>`, 7 days, single use); opening it and tapping
  Accept signs that phone in as a new user with no devices.
- **Bind a new device.** Flash it with the new firmware (a public build works: no
  token needed). On connecting it is stored as unclaimed and shows a still
  picture "BIND THIS TV" with its 20-character code, and its status page's QR
  code becomes `HTTPS://<SITE>/TV/B/<code>`. The owner scans it with a signed-in
  phone (the remote's scan button) and confirms, or types the code under Me → Bind a new device. The
  device reconnects within about two seconds and plays the owner's channels.
- **A legacy device reflashed with the new firmware** keeps playing as its legacy
  record as long as its build still has the pairing token (it is registered
  meanwhile). Move its state to its real id:

  ```sh
  python3.11 -m server.tenancy.admin devices          # find the new 12-hex id
  python3.11 -m server.tenancy.admin adopt legacy-xingzhi <id>
  ```

  Owner, remote code (the printed QR), cast slot and name move over; the legacy
  record is retired and the token stops working on that port.
- **After an `erase_flash`** the device has a new secret and is refused. Owner:
  `python3.11 -m server.tenancy.admin repair <id>`, or the API
  `POST /api/devices/<id>/repair`; the next connection within 10 minutes pins the
  new secret.
- **Switch the shared token off** once no device has used it for 30 days
  (`admin devices` shows the legacy records' last connection):
  `python3.11 -m server.tenancy.admin legacy-token off`.
- Other commands: `users`, `unbind`, `ban-device`, `unban-device`, `ban-user`,
  `unban-user`, `audit`. The relay picks up database changes within 10 seconds.

## 4. Backups

```sh
sqlite3 /var/lib/ai-passport-tv/tv.db ".backup /var/backups/tv-$(date +%Y%m%d).db"
find /var/backups -name 'tv-*.db' -mtime +14 -delete
```

Nightly (cron or a systemd timer), and always before a migration or an upgrade.
Copy the `.db` file only through `.backup`; a plain copy of a WAL database can
miss recent writes.

## 5. Roll back

The old files were never written to, so a rollback is "turn the configuration
back". Positions and casts after the cut-over are lost unless exported first.

```sh
cd /opt/ai-passport-tv
sudo -u tvserver python3.11 -m server.tenancy.migrate --data-dir /var/lib/ai-passport-tv \
    --export /var/lib/ai-passport-tv/relay-state.export.json     # optional: keep the new progress
systemctl stop tv-relay tv-xingzhi tv-passport
rm /etc/systemd/system/tv-relay.service.d/tenancy.conf \
   /etc/systemd/system/tv-xingzhi.service.d/tenancy.conf \
   /etc/systemd/system/tv-passport.service.d/tenancy.conf
systemctl daemon-reload
# Only if you exported and want the new progress back:
#   cp -p relay-state.json relay-state.json.pre-rollback && cp relay-state.export.json relay-state.json
systemctl start tv-relay tv-xingzhi tv-passport
```

The export carries the admin's creators, series, progress and history and the
Xingzhi's cast slot; sessions and the sign-in code come from the old file, so
phones need not re-scan. Users added after the cut-over, their subscriptions and
bound new devices exist only in `tv.db` — keep it; migrating again later uses
`--replace`. New firmware works against the old server (it ignores the new HELLO
keys) as long as its build has the pairing token; a public build has none and
cannot connect to the old server.

## Voice control in both modes

Push-to-talk ([`server/README.md`](../../server/README.md#voice-control)) works
with both relays. Turn it on in single-user mode first and let it settle; the
cut-over above then changes nothing for voice.

**A. Voice, single-user (before the cut-over).**

1. muse-asr with its Unix socket: `MUSE_ASR_SOCKET=/run/muse-asr/asr.sock`,
   `MUSE_ASR_SOCKET_GROUP=tvserver`, `RuntimeDirectory=muse-asr`,
   `RuntimeDirectoryMode=0755`, `SupplementaryGroups=tvserver` (the drop-in
   [`packaging/systemd/muse-asr.service.d/tv-voice.conf`](../../packaging/systemd/muse-asr.service.d/tv-voice.conf)),
   and on a memory-tight host the warm-up timer
   [`muse-asr-warm.timer`](../../packaging/systemd/muse-asr-warm.timer). Check:
   `sudo -u tvserver curl --unix-socket /run/muse-asr/asr.sock http://x/health`.
2. tv-relay: the new code only, no new flag. Its `--device-name` device is `d0`,
   each `--extra-device` is `d1`, `d2`... in order (and also answers to its name).
3. The media units: one `voice.conf` drop-in each, next to (not inside) `tenancy.conf`:

   | Unit | Drop-in |
   | --- | --- |
   | `tv-xingzhi` | [`tv-xingzhi.service.d/voice.conf`](../../packaging/systemd/tv-xingzhi.service.d/voice.conf): `TV_ASR_SOCKET=/run/muse-asr/asr.sock`, `TV_RELAY_KEY_FILE=/etc/ai-passport-tv/relay-key`, `TV_RELAY_DEVICE=d0`; `After=muse-asr.service tv-relay.service` |
   | `tv-passport` | [`tv-passport.service.d/voice.conf`](../../packaging/systemd/tv-passport.service.d/voice.conf): the same socket and key file, `TV_RELAY_DEVICE=Passport` (the name in tv-relay's `--extra-device`, or `d1`); its user must be in the `tvserver` group and able to read the key file |

   `#Environment=TV_VOICE_LOG_TEXT=1` stays commented out except while tuning.
4. `systemctl daemon-reload`, restart muse-asr, tv-relay, then both media units.
   Each media unit logs `voice: on, relay on, transcripts not logged`. Hold OK
   on a series channel and say "pause": that device pauses, the other does not.

**B. After the cut-over (multi-user).** The drop-ins in
`packaging/systemd/tenancy/` add `TV_DB` and do not touch the voice variables:
nothing to change. With `TV_DB` set, the media server ignores
`TV_RELAY_DEVICE` (it logs `voice: TV_RELAY_DEVICE ignored with a device
database ...` once, then `voice: on, relay per device`) and names, in each relay
call, the device it admitted for that session: `legacy-xingzhi` /
`legacy-passport` for old firmware or a not-yet-adopted new one, the 12-hex MAC
once adopted (`admin adopt`) or bound. The relay serves a keyed request that
names a device on a single-device endpoint as that device's guest, so it reaches
any user's bound device and nothing else; an unbound device gets no voice at all.
Leave `TV_RELAY_DEVICE` in `voice.conf`: a rollback (deleting the `tenancy.conf`
drop-ins) brings single-user mode, and with it that mapping, back as it was.

Check after the cut-over: on each device, speak a channel ("CCTV 5") and, on a
series channel, "pause". The media log shows `device=legacy-...` then
`voice id=... intent=...`; no `relay_catalog=relay status 404`.

## Troubleshooting

| Symptom | Look at |
| --- | --- |
| Relay exits saying there is no administrator yet | The migration has not run against this `--db`. |
| `database is locked` in a log | Something holds a long transaction (an open `sqlite3` shell); close it. |
| Media server logs `device refused: … secret mismatch` | The device was erased or is an impostor: `admin repair <id>` if it is yours. |
| Media server logs `pairing token on port …, which has no legacy device` | That port's legacy device was adopted (retired) or never migrated. |
| A device shows the bind picture although it is bound | It reconnects within 2 s of binding; if not, check `admin devices` and the relay log for a failure to write the device files. |
| Passport unit cannot write `devices/` | Its drop-in's `ReadWritePaths=/var/lib/ai-passport-tv` is missing. |
| Media server logs `voice: relay disabled (PermissionError)` | Its user cannot read `TV_RELAY_KEY_FILE`. |
| Voice says the TV is not connected to the relay | `TV_RELAY_KEY_FILE` unset; or, single-user only, `TV_RELAY_DEVICE` unset. |
| Media log shows `relay_catalog=relay status 404` | Single-user: `TV_RELAY_DEVICE` is not a relay device id or name. Multi-user: the admitted device is not active (unbound, retired, banned). |

## Phase 2: the PC console

The console is part of the multi-user relay; there is no new unit, port or
flag. The single-user relay (no `--db`) answers 404 for every console path:
switch to `--db` first. Deploying it:

1. Copy the new `server/` as in step 0. On its first start the relay adds one
   column to `tv.db` (`login_ticket.poll_sha256`, `ALTER TABLE ... ADD COLUMN`);
   nothing else in the database changes, and older code keeps working against
   the upgraded file. Back up first as in section 4 anyway.
2. nginx: add the `/TV/L/` location from
   [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf), then
   `nginx -t` and reload. `/tv/console` is already covered by `/tv/`.
3. `systemctl restart tv-relay`. The media units need nothing.

Check: open `https://<site>/tv/console` on a computer. It shows a QR code and
a six-character code. On a signed-in phone either scan the QR code (if the
browser that opens has no session, the page says to use the home-screen app
instead) or open the remote, Subscriptions, "Sign in the PC version", type the
code, check the browser and network shown, and confirm. The computer signs in
within a second or two. Its settings page lists the session; "Sign out" ends it.

How it works and its limits:

| | |
| --- | --- |
| Paths | page `/tv/console`; QR landing and phone confirm page `/TV/L/<ticket>` (relay `/l/<ticket>`) and `/tv/login` (typed code, list of signed-in PCs); API `POST /api/login/ticket`, `POST /api/login/poll`, `GET /api/login/info`, `POST /api/login/confirm`, `GET /api/login/status`, `GET /api/sessions`, `POST /api/sessions/revoke`, `POST /api/logout`, `GET /api/console/devices`, `GET /api/invites` (admin), `POST /api/qr`; `GET /api/state?device=<id>` shows one device without changing the phone's choice |
| Ticket | 26-character id (130 bits) only in the QR code, 6-character code (alphabet without I, L, O, 0, 1) on the screen, a separate 256-bit poll secret kept by the page; all three stored as SHA-256 except the short code. Expires in 2 minutes, single use; the PC long-polls up to 25 s per request |
| Who confirms | a signed-in phone or PC session of a user (cookie); not a guest, not the key. The confirm page shows the PC's browser and /24 network and warns when it differs from the phone's |
| Limits | 12 tickets per 10 minutes per network (/24 or /48); 10 wrong codes per 10 minutes per user and per network; at most 100 live tickets and 32 waiting long polls (more answer at once and the page retries) |
| PC session | 30 days, cookie `tvs` with `HttpOnly; Secure; SameSite=Lax; Path=/tv/`, listed under Settings with phones and the guests of your devices; "Sign out other sessions" keeps only the current one |
| Cross-site requests | every POST and DELETE (both relay modes) is refused (404) when the browser says it comes from another site (`Sec-Fetch-Site: cross-site`/`same-site`, or an `Origin` whose host differs from `Host`); requests without those headers (the media server, curl) are unchanged |
| Pages | `Content-Security-Policy` without any external source, `frame-ancestors 'none'`, `X-Frame-Options: DENY`; QR codes are drawn by the relay (`server/relay/qr.py`, a port of the library the firmware uses) |

The admin's key link stays the break-glass sign-in: open `https://<site>/tv/?k=<key>`
on the computer, then `/tv/console`. The console's Invite page (admin only) makes
the same invite links as the phone; the full admin backend is `/tv/admin`
(phase 3, below).

Not in the console yet: per-user hiding and ordering of live channels and
channel proposals; device members other than the owner (the console shows the
device's guest QR code, which is the existing guest link).

### Scanning from the remote, and letting a phone sign in

Two later additions, deployed like the rest of phase 2 (copy `server/` and
`packaging/help/`, restart `tv-relay`):

- **Scan to sign in the PC** from the remote: Me has a button, and the
  scan button at the top right of the Remote tab reads the same code, that
  opens the phone's camera (`/tv/scan.js`, plus the vendored jsQR
  `/tv/jsqr.js` where the browser has no `BarcodeDetector`, both loaded only on
  the tap) and opens `/tv/l/<ticket>` on the site the remote runs on. Tickets
  are relay-wide, so a remote on the main site confirms a QR code showing the
  `tv.` subdomain and the other way round. No nginx change.
- **Let a phone sign in** from a signed-in PC (Settings, or the hint on the
  Remote page) or a signed-in phone (the remote's Me → Let another phone sign
  in, shown full screen on the phone): the page shows `https://<site>/TV/P/<token>` as a QR code
  and an 8-character code; the phone's camera opens the link, the page shows
  whose account and which PC or phone asked, and a tap gives that phone an ordinary member
  phone session of the same user (180 days; then "Add to Home Screen" from the
  remote, which keeps the session in its address). Opening the page does not
  use the link up (link previews only GET it). The QR code uses `--public-url`
  (the `tv.` subdomain), but the page and its API work on either host.
  **nginx:** add the `/TV/P/` location from
  [`packaging/nginx/tv-relay.conf`](../../packaging/nginx/tv-relay.conf), then
  `nginx -t` and reload. `/tv/p`, where the code is typed, is covered by `/tv/`.
  The relay creates the `phone_link` table on its first start.

| | |
| --- | --- |
| Paths | landing `/TV/P/<token>` (relay `/p/<token>`) and `/tv/p`; API `POST /api/phone-link` (PC or phone), `GET /api/phone-link/status?poll=` (the same session), `GET /api/phone-link/info?t=` or `?code=`, `POST /api/phone-link/accept` `{token}` or `{code}` |
| Link | 26-character token (130 bits, upper-case base32) only in the QR code, 8-character code (alphabet without I, L, O, 0, 1; about 40 bits) on the screen, a 256-bit poll secret kept by the page; all three stored as SHA-256. Expires in 5 minutes, single use; a new link replaces the user's unused one; a banned user's link stops working |
| Who creates | a signed-in member's PC or phone session (cookie); not a guest, not the key |
| Limits | 6 links per user and 12 per network per 10 minutes; 10 wrong codes per network and 100 overall per 10 minutes |
| Audit | `phone_link.create` (detail starts with `pc` or `phone`, the asking session) and `login.phone.link` |

## Getting in with nobody signed in (account recovery)

Phones lose their sessions (a new phone, cleared Safari data, the home-screen
icon deleted) and an invite needs an admin. Three ways in, all ending in an
ordinary member phone session (180 days); the device itself is the root of
trust for the first two, because only someone standing at it can see its
screen or press its keys. Deploy: copy `server/` and `packaging/help/`,
restart `tv-relay` and both media servers (`tv-xingzhi`, `tv-passport`); no
nginx change (`/TV/B/` and `/TV/P/` already exist). The relay adds
`phone_link.origin`, `phone_link.device_id` and `session.via` on its first
start (old rows read as `pc` / plain sign-ins). The device side needs the new
firmware; older firmware simply does not offer the gesture.

**1. Self-registration with an unbound device (no invite).** A phone that is
not signed in (or only a guest) opens the device's bind link `/TV/B/<claim>`
(the "BIND THIS TV" still frame's code, or the status page's QR code). The page
offers "Create an account and bind this device" (in Chinese) and asks only for a name (default "me"); one tap
creates a `user`, binds the device to them, gives that phone a member phone
session and lands on the remote with the "Add to Home Screen" hint. A
signed-in phone still gets the old "bind to my account" confirmation. The
claim stays single use (cleared on bind).

**2. Device recovery ("recover sign-in").** On the device's status page:

| Board | Gesture |
| --- | --- |
| Xingzhi | hold ▲ and OK together for 1.5 s, or click OK 5 times quickly |
| AI Passport | click OK 5 times quickly (its three keys share one ADC ladder with ▲ at 0 Ω, so two keys held together read as ▲ alone) |

"Quickly" is the button driver's rule: every press after the first and every
gap shorter than 180 ms; a pause of up to about 0.4 s between groups still
counts (2 + 3 is 5). Because OK clicks on the status page are held back until
the count is known, OK there (audio-only) now acts about 0.6 s after release
instead of 0.2 s, and a double OK (the Passport's back to Muse) the same; an
abandoned attempt (four clicks, a fumbled press) does nothing. ▲ held
(setup), ▼ held (pets) and OK held (close) are unchanged; with the chord, the
second key's own long press or click is swallowed. The gesture is chosen in
`components/bsp/include/boards/*.h` (`BSP_RECOVER_CLICKS`, `BSP_RECOVER_CHORD`).

The device sends `STATUS {"recover": 1}` on its authenticated session; the
media server mints a one-time owner link (`phone_link` with `origin=device`)
and answers `SWITCH {"recover": {"url": "HTTPS://<SITE>/TV/P/<TOKEN>", "ttl": 300}}`.
The device shows the QR code full screen (version 3 at 4 px a module for a
usual site; the server keeps the address within 96 characters, version 4),
"scan with the phone camera / sign in as the owner" (in Chinese) and a countdown; any key closes it. Scanning
with any phone camera opens the same landing page as "Let a phone sign in",
which names the device the recovery came from; the tap signs the phone in as the owner.
The new session is marked `via=recover`: the owner's other sessions show
"a new phone signed in by device recovery" once (console on load, remote on its next refresh),
and the console's session list labels it "device recovery" with "Sign out". An
unbound device shows its bind code instead (way 1). If the server refuses the
screen says why: limited (three an hour), not bound, server too old / no
`--public-url`, no network.

**3. Admin break-glass.** On the host:

```
sudo -u tvserver python3 -m server.tenancy.admin --db /var/lib/ai-passport-tv/tv.db users
sudo -u tvserver python3 -m server.tenancy.admin --db /var/lib/ai-passport-tv/tv.db login-link <user_id>
```

prints `HTTPS://<SITE>/TV/P/<TOKEN>` (5 minutes, single use); send it to the
person, who opens it on the phone and taps the button. The admin backend's Users
page has the same thing as a button (phase 3, below).

| | |
| --- | --- |
| Paths | `GET /api/bind/<claim>` answers `register: true` to a phone that is not signed in; `POST /api/bind/register` `{claim, name}`; the landing page and `/api/phone-link/*` as above, `info` adds `origin` and `device_name`; `/api/login/status` and `/api/state` add `notice`; `/api/sessions` rows carry `via` |
| Protocol | HELLO `"recover": 1`; CONFIG `"recover": 1` (multi-user, bound device, site known); STATUS `{"recover": 1}`; SWITCH `{"recover": {"url", "ttl"}}` or `{"recover": {"err": "limited" \| "unbound" \| "off" \| "fail"}}` |
| Who | registration: anyone holding an unbound device's claim, not a signed-in member; recovery: the media server for a device it authenticated by its secret, only when bound and the owner is active; login-link: shell access to the database |
| Limits | registration 3 accounts per network and 30 overall per hour (counted on success, in relay memory); recovery 3 links per device per hour (counted in the database, survives restarts); a new device link voids its unused one; links 5 minutes, single use; device and admin links have no typed code |
| Audit | `user.signup`, `device.bind`; `phone_link.device`, `login.phone.recover`; `phone_link.admin`, `login.phone.admin` |
| Logs | never the token: the media server logs `recover link issued for user N` or the refusal |

## Phase 3: the admin backend

`https://<site>/tv/admin`, for admins only: a link appears in the PC console
(under your name) and in the phone remote's Me tab (under Invite family). Any
other session, a guest or nobody is sent back to `/tv/console`, and every
`/api/admin/*` call answers 404 to them; the key (`X-Key`, or the
`?k=` sign-in) counts as the admin. POSTs pass the console's same-origin check,
and every change is written to the audit log with the admin and their /24
network.

| Page | What it does |
| --- | --- |
| Overview | devices online/offline, owner, channel and title, session length; bytes sent in the last ~10 s, frames per second and the byte-rate target (from the media server's now-playing file); host CPU, memory and outbound traffic now and as 24-hour curves; the tv_server, relay, ffmpeg, muse-asr and nginx processes; YouTube node state and streams in use (of 2) |
| Users | origin (invite, self-registered, the first accounts); self-registered users are flagged "to review" until you press "got it"; rename; ban/unban; delete (type the name; the user's data is exported first); one-time login link with QR code (5 minutes, single use, the CLI's `login-link`); their sessions (and their devices' guests) with sign-out; invites |
| Devices | every device with status, owner, firmware, last connection and network; reconnect, rename, re-pair, unbind, ban/unban |
| Channels | the global `channels.txt` as a table: rename, regroup, reorder, delete, add; "Check" and "Save" (below) |
| Audit log | newest first, filtered by user, device or action, 50 at a time |
| System | registration and unbound-device policy, limits, session lengths (read-only; they are in the code), the legacy pairing token switch, database and channel-file facts |

What the actions do:

- **Ban a user**: their sessions are deleted at once, the devices they own get
  a `reconnect` event (the media server ends the session) and are refused at
  their next HELLO while the owner is banned (`gate.py`). Data and bindings stay;
  unban restores them, and the user signs in again (recovery, login link,
  invite). You cannot ban or delete yourself or the last admin.
- **Delete a user**: needs the user's name typed. The export (user row,
  `user_state`, devices, sessions without tokens, invites, their audit entries)
  is written to `exports/user-<id>-<time>.json` next to the database (0640) and
  downloaded by the page; then their devices are unbound (new claim code,
  guests signed out), and their sessions, links, state and memberships deleted.
  Their invites stay; an invite they accepted is expired. A user who owns a
  legacy device cannot be deleted (adopt it first).
- **Reconnect / ban / unbind a device**: the relay writes a `reconnect` event to
  the device's `relay-events.json`; firmware that announces `switch` ends the
  session and reconnects within a second or two (older firmware ignores it).
- **Channel table**: ids never change (users' group choices and the device's
  last channel are keyed by id); a new channel gets the next free `chNNN`.
  Save checks names and addresses, every device owner's channel list against
  the CONFIG limits (their creators and series plus this table, with the same
  512-byte spare as adding a creator), and finally runs the media server's own
  `live.parse_channels()` in a child process (`python3 -m server.relay.channeltable --check`;
  a child so the relay does not keep `server.live` and numpy in memory). It
  refuses a table edited elsewhere since the page loaded. Before writing, the
  old file is copied to `channels.txt.bak-<date>-<time>` in the same directory
  (the newest 30 are kept). The media servers re-read the file at the start of
  each session, so **no restart is needed**: each device gets the new list at
  its next channel change or reconnect. A group set here is the channel's
  default for everyone (stored in the database, `meta.channel_groups`); a
  user's own choice on the phone stays on top of it.

Deploy: copy `server/` (relay and media server both changed) and
`packaging/help/` (the help page has a new section), and restart
`tv-relay`, `tv-xingzhi` and `tv-passport`. No nginx change (`/tv/admin` is under
`/tv/`; the page keeps each request under nginx's 8 kB body limit by sending
unchanged channels as their id only). The relay adds the column
`user.reviewed_at` on its first start. The relay user needs write access to the
directory of `--main-channels` (for the backup and the atomic replace) and to
the database's directory (for `exports/`); both are `/var/lib/ai-passport-tv`
in the templates. Without the media-server restart, metrics stay empty and
`reconnect` is ignored, everything else works.

| | |
| --- | --- |
| Paths | page `/tv/admin`; `GET /api/admin/overview[?history=1]`, `users`, `users/<id>/sessions`, `users/<id>/export`, `devices`, `audit?user=&device=&action=&before=`, `system`, `channels`; `POST /api/admin/users/<id>/rename\|ban\|unban\|reviewed\|login-link\|delete\|sessions/revoke`, `invites`, `devices/<id>/unbind\|ban\|unban\|reconnect\|repair\|rename`, `channels` `{version, rows, dry}`, `system/legacy-token` |
| Metrics | now-playing gains `bytes_10s`, `fps` (frames sent per second; the legacy sender only, `TV_LIVE_ENGINE=v2` reports 0) and `target_rate`; the host sampler reads `/proc/stat`, `/proc/meminfo`, `/proc/net/dev` every 5 s and keeps 15 minutes of 5-second points and 24 hours of minute averages in memory (lost on restart); without `/proc` (macOS) it does not run |
| Audit | `user.rename`, `user.reviewed`, `user.ban`/`unban`, `user.export`, `user.delete`, `admin.session.revoke`/`revoke_all`, `phone_link.admin`, `invite.create`, `device.reconnect`, `device.banned`/`active`/`unclaimed`, `device.unbind`, `device.repair`, `device.rename`, `channels.save` (counts and the backup's name), `legacy_token.on`/`off` |

## Left for later phases

Per-user hiding and ordering of live channels and proposals (`user_state` has
room for `hidden`/`order`); device members other than the owner (table exists,
no UI); the media gate with per-device workers, model profiles and admission
control (phase 4), which is also when concurrency caps and per-worker process
figures appear in the admin backend.

## Not verified

- Nothing here has run on the cloud host; the Passport unit's actual paths are
  unknown to the repository.
- The firmware identity (HELLO keys, NVS secret) is build-tested for both boards,
  not device-tested: that `esp_efuse_mac_get_default()` gives the same id under
  the Muse firmware, and that the secret survives switching firmwares on the AI
  Passport, are expected, not measured.
- The bind still frame and the `bound: 0` key were tested against the server
  only; how each firmware draws the picture was not looked at on a device.
- Phone flows were checked in a desktop browser at phone width against a local
  relay with synthetic state, not on an iPhone home-screen app or in WeChat.
- Self-registration, device recovery and `login-link` are covered by
  `tests/test_account_recovery.py` and the gesture by `tests/test_av_recover.c`
  (against a model of the button driver); neither gesture has been pressed on
  a device, the QR codes have not been scanned by a phone camera, and the
  timing (0.4 s between groups, the 180 ms driver window) is unmeasured by hand.
- The PC console was checked in the desktop app's Chromium pane at 1440 and 900
  px wide, against a local relay with synthetic state, fake video sites and a
  fake device; the QR code was decoded by that browser's `BarcodeDetector`, not
  by a phone camera or WeChat. Not tried in Safari, Firefox or Edge, nor behind
  nginx.
- The admin backend (phase 3) was checked in the same pane at 1440 px and at
  phone width (375 px) against a local relay with a temporary database, fake
  devices and a synthetic `/proc` (the host curves on macOS are made up); it has
  not run on the cloud host, so the real `/proc` figures, the process table, the
  channel-check child's time with numpy (about 0.1 s locally without numpy) and
  the file permissions for backups and `exports/` are unverified. The
  `reconnect` event is covered by host tests only, not tried on a device.
