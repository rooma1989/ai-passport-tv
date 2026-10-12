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
  opens the remote as a guest for that device only (no Subscriptions tab).

## 3. Using it

- **Invite family.** On the admin's phone: Subscriptions → My devices → Invite;
  or `python3.11 -m server.tenancy.admin invite --name Mum`. Send the link
  (`https://<site>/tv/i/<code>`, 7 days, single use); opening it and tapping
  Accept signs that phone in as a new user with no devices.
- **Bind a new device.** Flash it with the new firmware (a public build works: no
  token needed). On connecting it is stored as unclaimed and shows a still
  picture "BIND THIS TV" with its 20-character code, and its status page's QR
  code becomes `HTTPS://<SITE>/TV/B/<code>`. The owner scans it with a signed-in
  phone and confirms, or types the code under Subscriptions → My devices. The
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
   `RuntimeDirectoryMode=0755`, `SupplementaryGroups=tvserver`. Check:
   `sudo -u tvserver curl --unix-socket /run/muse-asr/asr.sock http://x/health`.
2. tv-relay: the new code only, no new flag. Its `--device-name` device is `d0`,
   each `--extra-device` is `d1`, `d2`... in order (and also answers to its name).
3. The media units (base units, not drop-ins):

   | Unit | Add |
   | --- | --- |
   | `tv-xingzhi` (template in `packaging/systemd/`) | `TV_ASR_SOCKET=/run/muse-asr/asr.sock`, `TV_RELAY_KEY_FILE=/etc/ai-passport-tv/relay-key`, `TV_RELAY_DEVICE=d0`; `After=` gains `muse-asr.service` |
   | `tv-passport` (hand-made on the host) | the same socket and key file, `TV_RELAY_DEVICE=Passport` (the name in tv-relay's `--extra-device`, or `d1`); `After=muse-asr.service tv-relay.service`; its user must be in the `tvserver` group and able to read the key file |

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
Leave `TV_RELAY_DEVICE` in the base units: a rollback (deleting the drop-ins)
brings single-user mode, and with it that mapping, back as it was.

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

## Left for later phases

PC console and scan-to-sign-in (phase 2, the `login_ticket` table exists
already); hiding and reordering live channels per user and proposals (phase 2;
`user_state` has room for `hidden`/`order`); device members other than the owner
(table exists, no UI); admin web pages and metrics (phase 3); the media gate with
per-device workers, model profiles and admission control (phase 4).

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
