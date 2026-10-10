<p align="right">
  <a href="2026-10-10-grouped-channels-and-remote-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Grouped channels and the phone remote — design

Status: approved 2026-10-10. Follows
[`2026-10-09-creator-channels-and-cast-design.md`](2026-10-09-creator-channels-and-cast-design.md).

## Goal

1. **Device:** live TV, creator channels and the cast are no longer one list of
   about 95 entries. The channel list is two levels (group, then channel), and a
   long press on the arrows changes channel within the current group only.
2. **Phone:** the page becomes a remote that is quick to open (a short address,
   signed in once, on the home screen, or scanned from the device), with three
   tabs: remote, cast with a replayable history, and creators.

## Decisions

| Question | Decision |
| --- | --- |
| Device list | two levels: groups first, then the group's channels; long-press OK goes back |
| Groups | My cast, Creators, CCTV, Satellite, HK/Macau/Taiwan, International, Music, Cartoons & films, Other; empty groups hidden |
| Live channel grouping | automatic by name, overridable from the phone |
| Opening the page | short address `iewww.com/tv`, sign in once (180 days), add to home screen, QR code on the device |
| Page layout | bottom tab bar: Remote, Cast, Creators |
| Visual style | warm ink background, vermilion accents, Songti (serif) titles and channel names, amber monospace channel-number display; no rounded cards, blue buttons or emoji |
| History | last 50 casts, stored on the server, shared by every phone |

## Groups

- `server/channel_groups.py` (used by the TV server and the relay): the fixed
  group list and `classify(id, name) -> group`:
  - `cast` → My cast; `upNN` → Creators;
  - name starts with `CCTV` → CCTV;
  - name contains the word for "satellite TV" (except the Hong Kong satellite
    station), or a local station's 4K channel (name ending in 4K) → Satellite;
  - names of Hong Kong, Macau and Taiwan stations (Phoenix, TVBS, EBC, CTS, FTV,
    SET, CTV, CTi, TTV, Bright Smart, Hong Kong) → HK/Macau/Taiwan;
  - Latin-only names, and CGTN/CNA/NHK/BBC/RT names → International;
  - names with the words for "selection" or "music" → Music;
  - a fixed list of cartoon and film names (Tom and Jerry, Doraemon, SpongeBob,
    Dragon Ball, Cooking Master Boy, Stephen Chow, Let the Bullets Fly, and the
    word for "film") → Cartoons & films;
  - anything else → Other.
  The exact Chinese keywords live in the module's rule table.
- Overrides: `channel-groups.json` (`{"id": "group"}`), written only by the relay
  (the phone page), read by the TV server (`TV_CHANNEL_GROUPS_FILE`) with the same
  change-then-reload rule as the channel tables. Unknown group names and a broken
  file are ignored with a log line. `channels.txt` keeps its format, so
  `tools/channel_config.py` keeps working.
- CONFIG carries the group names once and one index per channel:
  `"groups": ["<My cast>", …]` (the Chinese group names) and `{"id", "name", "g": n}` in `channel_list`. That
  adds about 600 bytes to a 4.1 KB list; a name per channel would pass the 7 KB
  `TV_CONTROL_MAX`.

## Device

- `ui_menu`: a group level in front of the channel level. OK opens the group
  list on the group of the playing channel; OK on a group opens its channels on
  the playing one (or the first); a long press on OK goes back one level and
  closes from the group level. Pure logic, host-tested.
- Long press on the arrows while playing: next/previous channel within the
  playing channel's group, wrapping.
- No `groups` in CONFIG (older server): one group holding every channel, and the
  menu skips the group level, which is today's behaviour.
- Status page: when CONFIG carries `remote_url`, its QR code replaces the help
  code; otherwise the help code stays.

## Remote control

- The relay gains a third event action, `switch` with a channel
  (`{"seq", "action": "switch", "channel"}`). `SwitchWatcher` answers it with
  `("switch", channel)` unless the device is already there.
- The TV server writes `now-playing.json` (`TV_NOW_PLAYING_FILE`): channel id,
  connected or not, whether the device announced `switch`, and the time, at each
  session start and end. The relay adds the video title for `cast` and `upNN`
  from what it last started on that channel.
- The relay reads the full channel table itself (`--main-channels`, the same
  `channels.txt`) to list live channels on the page.
- Errors shown on the page: device offline, firmware without switch support
  ("switch channels on the device"), unknown channel.

## Phone page

### Address and sign-in

- nginx: `location ^~ /tv/` → relay page port (the page and its API), and
  `location ^~ /TV/R/` → the relay's code endpoint. `/TV/cast/?k=` keeps working
  and redirects. The help page at `/TV/` is unchanged.
- Signing in: `GET /tv/?k=<key>` or `GET /TV/R/<code>` creates a random session,
  stored by the relay with its creation time, and sets
  `tvs=<session>; HttpOnly; Secure; SameSite=Lax; Path=/tv/; Max-Age=15552000`
  (180 days), then redirects to `/tv/`. Every page and API request needs a valid
  session cookie or the key header; otherwise 404 as now.
- The QR code: `HTTPS://IEWWW.COM/TV/R/<CODE>` with `<CODE>` 20 characters of
  base32 (A–Z, 2–7, 100 bits). Upper case only, so it encodes in QR alphanumeric
  mode as a version-2 code (25 modules, error correction L: 43 of 47 characters) that fits the status page. The relay
  writes the URL to `remote-url.txt`; the TV server sends it as `remote_url` in
  CONFIG. It travels in plain text like the pairing token; the page can issue a
  new code, which also ends every session.
- Home screen: a web app manifest (the Chinese name "Xiaozhi TV", standalone, theme `#1c1915`,
  start `/tv/`) and 180×180 and 512×512 PNG icons drawn once by a script.

### Tabs

- **Remote:** an amber channel-number display (`CH 02`, group, title), a row of
  group names, the group's channels with numbers; tap to switch. An "adjust
  groups" toggle shows a group picker on each row.
- **Cast:** paste box and "play on Xiaozhi"; "played before": the last 50
  casts (title, Bilibili/YouTube, date), tap to replay, swipe left to delete.
- **Creators:** add and remove creators, "unwatched n/20"; a link to the help
  page at the bottom; "issue a new QR code" at the very bottom.

### API (all behind the session or key)

| Call | Effect |
| --- | --- |
| `GET /api/state` | now playing, groups with channels, creators, history, cast |
| `POST /api/switch {channel}` | write a `switch` event |
| `POST /api/cast {url}` | as before; also records history |
| `POST /api/history/play {site, id}` | cast a history entry again |
| `DELETE /api/history/<site>/<id>` | remove an entry |
| `POST /api/groups {channel, group}` | set or clear (`null`) an override |
| `POST /api/code` | issue a new QR code, end all sessions |
| creators | unchanged |

### History

- In `relay-state.json`: `history`, newest first, at most 50 entries of
  `{site, id, title, at}`; casting an entry again moves it to the top.

## Error handling

| Case | Behaviour |
| --- | --- |
| Session expired or code reissued | 404; the page says to scan the device's code again |
| Group file broken | ignored, everything classified by name, logged |
| `now-playing.json` missing or older than 30 s while "connected" | shown as offline |
| Two phones at once | both work; the last switch wins |

## Testing

- Host: `classify` against the current 88 names; override load and bad files;
  CONFIG encoding of groups and its size with 12 creators; `ui_menu` two levels,
  back, open on the playing group, long-press wrap within a group, no-group
  fallback; session cookie issue, expiry and reissue; code format and QR version;
  history order, limit and dedupe; `switch` events and offline/no-switch answers;
  the page's script against the API fields.
- Device: two-level menu, long-press within a group, switch from the phone, scan
  the status-page code with WeChat and the camera, add to home screen.

## Delivery in steps

1. Server and page: address and sign-in, home screen, three tabs in the new style,
   history, switching from the phone. No firmware change.
2. Firmware: two-level menu and in-group long press (needs the `groups` field
   from step 1).
3. Firmware: the remote QR code on the status page.
