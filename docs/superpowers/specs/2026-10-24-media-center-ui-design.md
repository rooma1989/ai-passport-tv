<p align="right">
  <a href="2026-10-24-media-center-ui-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Home media center UI — design

Status: **design only, proposed 2026-10-24**, nothing built. Branch
`media-center-design` (based on `v0.2.4-beta`). The clickable prototype is
[`docs/design/media-center/index.html`](../../design/media-center/index.html):
one self-contained file, no network requests (its Content-Security-Policy
forbids them), fake data, placeholder art drawn in SVG. Open it in any browser;
the dashed "prototype" box in the corner switches between the desktop and the
phone layout and between demo states (normal, no TV, no subscriptions and an
empty library, Bilibili unreachable).

Builds on [`2026-10-19-pc-client-architecture-design.md`](2026-10-19-pc-client-architecture-design.md)
(the PC client and its console), [`2026-10-10-grouped-channels-and-remote-design.md`](2026-10-10-grouped-channels-and-remote-design.md)
(channel groups, the device's channel list), [`2026-10-10-remote-polish-design.md`](2026-10-10-remote-polish-design.md)
and [`2026-10-10-audio-only-mode-design.md`](2026-10-10-audio-only-mode-design.md).
It assumes the live-channel EPG work on branch `desktop-epg` lands: per channel a
logo, the current programme (title, start, end) and the next one. Field names
below are placeholders to be reconciled with that branch.

The UI is in Simplified Chinese. This document names screens and controls in
English; the Chinese peer uses the exact labels shown in the prototype. The main
mapping: Home, Live, Lineup, Series & anime, Creators ("UP" channels), Library,
Search, Devices & remote, Settings; **Play to** is the target picker; **Remote**
is the device control panel; **Listen only** is the audio-only mode.

## 1. Why, and what changes

The PC console (`server/relay/assets/console.html`) and the phone page
(`assets/remote.html`) were built as a **remote for a tiny TV**: the landing
page is a screen readout with a channel grid, and content lives in forms
(paste a link, a table of creators, a table of history). The product is now a
household media center: several tiny TVs, playback on the PC itself ("watch on
the computer", with or without a TV), several content sources, several family
members, and next a local media library. The owner wants it to feel like Infuse,
Jellyfin, the Apple TV app or Plex.

The design keeps everything the pages can do today, moves it under a
content-first structure, and introduces three cross-cutting pieces:

1. **Play to**: one target picker for every play button (each TV, plus "this
   computer"), remembered, showing which targets are online.
2. **The remote panel**: per-device controls (volume, brightness, listen only,
   pet, sleep timer, channel up/down, pause, seek) in one sheet that opens from
   anywhere, instead of being the home page.
3. **One media item shape** for live programmes, creator videos, series
   episodes, cast links and library files, so rows, search, history and
   continue-watching render the same way.

### Goals

- Content first: the first screen answers "what can I watch now" (continue
  watching, live now, new from my creators, new episodes, my library).
- Every playable thing has one primary action, "Play" to the remembered target,
  and a secondary "Play to..." that opens the picker.
- Same information architecture on the PC and the phone; different layouts.
- Keyboard and remote-style (arrow key) navigation on the PC; large hit targets
  on the phone.
- Keep the product's identity: dark ink background, warm red accent, amber
  monospace for numbers and times, serif headings.

### Non-goals

- No change to the device firmware, the wire protocol or the tiny TV's own
  on-screen menu.
- No new content sources beyond the planned local library and the optional
  Jellyfin/Emby/Plex connection.
- No light theme in the first release (tokens are prepared for it).
- No recommendation engine: the home page is assembled from the household's own
  subscriptions, history and EPG.

## 2. Information architecture

Desktop: a left sidebar with nine entries; a top bar with search, the Play-to
chip and the user; a now-playing bar along the bottom. Phone: five bottom tabs.

| Entry | Contents | Replaces today |
| --- | --- | --- |
| **Home** | Hero (up to three items), rows: Continue watching, Live now, Creator updates, New episodes, My library | the "Remote" page as landing page |
| **Live** | Selected programme on top (channel, title, progress, next, Play / Watch on this computer), group chips, programme guide grid with time ruler and now-line | the channel grid on the "Remote" page |
| **Lineup** | The channel list each tiny TV cycles through: per TV, groups, order, show/hide, size budget (channels and bytes), live sources and presets, EPG status | "Adjust groups" link, admin channel table presets |
| **Series & anime** | Poster grid with filters (following, anime, drama, documentary, animation, finished); add series | the "Series" table on "Subscriptions" |
| **Creators** | Avatar strip with new-video counts, latest videos grid, subscribe, import from Bilibili | the "Creators" table on "Subscriptions" |
| **Library** | Local folders and media servers: Movies, Series, Animation, Home videos; sources bar | new |
| **Search** | One box over live (channels and EPG), series, library, creators, videos (Bilibili, YouTube); scope chips; add/subscribe from results | the Bilibili search on "Subscriptions"; casting a link stays here too |
| **Devices & remote** | Device cards (screen mock-up, online state, now playing, Remote, Set as default target, Watch on this computer), this computer, bind a new TV, phone sign-in, family and guests | the "Devices" page; "Remote" controls move into the remote panel |
| **Settings** | Playback (Play-to memory), sources (live sources and EPG, Bilibili account, YouTube, library folders, media servers), household (devices, family, sessions), about | "Settings" and "Invites" |

The phone tabs are **Home, Live, Search, Remote, Me**. "Remote" is the remote
panel as a full page (the phone's historical job stays one tap away). "Me"
lists Series & anime, Creators, Library, Lineup, Devices, Settings and
History. Home also carries quick chips to Series, Creators, Library and Lineup.

Casting a link ("paste and play") is not a page any more: pasting a Bilibili or
YouTube link into any search box offers "Play this link" as the first result,
and the Play-to picker applies. History ("played before", up to 50) is a row
under Continue watching on the phone's Me tab and a section in Settings on the
desktop; its entries use the same media item shape.

**Detail pages** (reached from posters and search): backdrop, poster, title,
meta line (year, kind, source, schedule), synopsis, "last watched on <device>
at <position>", actions (Continue episode N at position, Play to..., From
episode 1, Following), episode list with watched/current/not-yet-released
states, related row. Library movies show file facts (resolution, codecs,
subtitles, size, folder) and the note that playback on a tiny TV is transcoded
by the PC.

## 3. The now-playing bar

Always visible at the bottom (above the tabs on the phone). It follows the
**session the user last started or controlled**: artwork, title, "<device> ·
<programme or position>", a progress line, previous / play-pause / next, a
remote button for TVs, and the Play-to chip on desktop. Clicking the artwork
opens what is playing (the guide for live, the detail page for a series or
library item). When this computer is the target, the bar controls the in-page
player window, which keeps today's "follow the TV" option.

Idle state: "Nothing playing" with the Play-to chip.

## 4. Play to

### 4.1 Behaviour

- **Targets**: every TV the user may control (owner, family, or a guest's single
  device) plus **This computer** on the PC console. On the phone, "This phone"
  is not offered in the first release (listening on the phone exists as the
  audio-only page; full phone playback is question Q8).
- Each row shows: icon (TV or computer), name, online dot, model and resolution,
  what it is playing now (or "offline, last seen 3 hours ago"), and a
  **last used** tag on the remembered target.
- **Primary play buttons** play to the remembered target immediately and say
  where in small text ("to Living room"). The caret next to them opens the
  picker for that item; choosing a row plays there.
- **The top-bar chip** opens the same picker without an item: choosing changes
  the default only; nothing that is playing is interrupted.
- **Remember** switch (default on). Off means every play asks first.
- **Offline target**: rows are dimmed and not selectable. If the remembered
  target is offline when the user presses Play, the picker opens with a short
  message instead of failing.
- **No TV bound**: only This computer is listed, with a "bind a TV" link;
  every Play goes to the computer.
- The picker is a right-side drawer on desktop and a bottom sheet on the phone;
  it is a radio group, Esc or the scrim closes it, focus returns to the button
  that opened it.

### 4.2 Content rules the picker applies

| Item | To a TV | To this computer |
| --- | --- | --- |
| Live channel | switch the TV to the channel (today's `/api/switch`) | direct watch of the channel (today's `watch_direct`) |
| Creator video / series episode | play that video on the creator or series channel (today's `/api/subs/play`, episode pick) | direct watch; progress kept separately (Q4) |
| Cast link | cast slot of that TV | cast to the computer |
| Library item | PC transcodes the file to the TV's resolution (new) | PC plays the file in the page (new) |
| YouTube item, YouTube not enabled | row disabled, "enable YouTube in Settings" | same |
| Bilibili member-only episode, not signed in | row disabled with a sign-in link | same |

### 4.3 Where "remembered" lives

Per browser, in `localStorage`, keyed by user: the PC in the study and the
phone in the bedroom naturally aim at different screens. If nothing is stored,
the server's "auto" target (today's `auto` in `/api/state`) is used. See Q3.

## 5. The remote panel

One component, opened by the remote button on a device card, on the
now-playing bar, or the phone's Remote tab. It contains, top to bottom:

1. **Device pills** to switch between TVs (offline ones dimmed; choosing one
   shows "offline, last seen ..., check power and Wi-Fi").
2. **Screen readout** in today's style: big amber channel number, group, channel
   or creator name, programme from EPG with a progress line and "next ...", or
   the video title and position for on-demand items.
3. **Pad**: channel up, pause/continue, channel down; back 30 s, next, forward
   30 s (seek and next disabled for live).
4. **Dials**: volume slider (steps of 10), brightness five steps, listen-only
   switch ("sound only, the screen shows the pet"), sleep timer (off, 15, 30,
   60, 90 minutes, with "N minutes left"; today only the voice command can set
   one, so this needs `POST /api/sleep`).
5. **Pets**: six choices, the current one outlined.
6. **Watch this on the computer** and a link to the TV's lineup.

Desktop: a 430 px right drawer over the current page. Phone: the Remote tab
is the panel as a page; from elsewhere it is a bottom sheet. Changes apply
immediately with a short toast, as today. Every control has a 44 px (desktop)
or 52–58 px (phone) hit target.

## 6. Layouts

| | Desktop (>= 700 px) | Phone (< 700 px) |
| --- | --- | --- |
| Navigation | left sidebar, 224 px | bottom tabs (5) |
| Top bar | page title, search box (`/` focuses), Play-to chip with name, user | page title, compact Play-to chip (short device name) |
| Rows | horizontal shelves, 252 px landscape cards, 150 px posters | shelves bleed to the screen edge, 224 px / 116 px, scroll snap |
| Hero | 360 px, text on the left over a gradient | 420 px, full width, text at the bottom |
| Guide | 200 px channel column (number, logo, name), 5 px per minute | 64 px column (logo only), 3.2 px per minute, preview image hidden |
| Detail | poster 220 px beside title and actions | poster 112 px beside title; synopsis and actions full width |
| Sheets | right drawer, 430 px | bottom sheet, up to 90 % height, grab handle |
| Now-playing bar | full controls, 74 px | art, title, pause, remote; 62 px; hidden on the Remote tab |

The breakpoint is the window width, not the device: a narrow PC window gets the
phone layout. Both layouts are one page and one set of components.

## 7. Empty and error states

| Situation | What the user sees |
| --- | --- |
| No TV bound | Home banner "You can watch without a TV: everything plays on this computer; bind a TV later with the code on its screen"; Play to lists only This computer; Devices shows an empty card with Bind; Lineup and the phone's Remote tab explain they need a TV |
| All TVs offline | Picker rows dimmed; Remote tab "all TVs are offline" with a link to Devices |
| No subscriptions, empty library | Home keeps the hero (live only) and Live now; the other rows become three onboarding cards (subscribe a creator, add a series, add a library folder); Series, Creators and Library each have an empty state with the add action |
| Bilibili unreachable | Warning banner on Home, Series, Creators and Search ("Bilibili cannot be reached: creators and series are not updating; live, YouTube and the library work"); uncached covers become a neutral placeholder "cover unavailable" with the title still under it; search shows only local, live and YouTube results; Settings shows the cause in the Bilibili account row |
| YouTube not enabled | YouTube items disabled with a link to Settings, not hidden |
| No EPG for a channel | the channel shows its name and logo only; no "now/next", no guide blocks (one full-width "no programme information" block) |
| No logo in M3U | generated badge: the channel's short name on a colour derived from its name |
| Library folder missing (NAS not mounted) | source pill turns red "not reachable"; items stay listed but greyed with "the folder is offline" |
| Programme in the past / future in the guide | "Finished, catch-up not supported" / "Starts at 21:00, remind me (later)"; both offer "watch what this channel shows now" |

## 8. Data each row needs

All rows render one **media item**: `id`, `kind` (`live`, `episode`, `video`,
`cast`, `library`), `title`, `subtitle`, `art` (URL plus ratio `poster` or
`wide`), optional `progress` (`position`, `duration`, `device`,
`updated_at`), optional `live` (`channel`, `start`, `end`, `next_title`,
`next_start`), `source` (`m3u`, `bilibili`, `youtube`, `library`,
`jellyfin`...), `detail` (route to a detail page or none), and `playable`
(per-target flags with a reason when disabled).

| Row / screen | Fields | Source | Exists today? |
| --- | --- | --- | --- |
| Continue watching | item, position, duration, device, time | series episode and creator watched sets in `state.py`; positions are **new** (videos, library, PC watching) | partly |
| Live now | channel number, logo, name, group, now title/start/end, next | channel table + EPG (`desktop-epg`); logo from M3U `tvg-logo` | EPG pending |
| Creator updates | video title, duration, published, creator avatar and name, new flag | creator video lists (latest 20), watched set | yes, avatars new |
| New episodes | series poster, kind, latest released, watched up to, schedule | series channels (`sr01...`), Bilibili season info | mostly; posters and schedule new |
| My library | poster, title, year, genre, duration or season count, added time | library index (new) | new |
| Guide | channels x programmes for a window (default now - 1 h to now + 3 h) | EPG | pending |
| Detail (series) | backdrop, poster, synopsis, tags, episodes with titles, durations, watched state | `/api/series/{id}/episodes` + Bilibili/YouTube metadata | partly |
| Devices | name, model, resolution, online, last seen, now playing, volume, brightness, listen only, pet, sleep timer | `/api/state` `devices`, `now`, `room` | yes, except the sleep timer: today it exists only as a voice command (`server/voice/intent.py`, a timed pause in `tv_server.py`) |
| Search | grouped results with the same item shape | local index + `/api/bili/search` + YouTube | partly |

**Artwork.** Pages may only load images from their own origin (the console's
CSP has `img-src 'self'`). All remote art therefore goes through the relay:

- Bilibili covers: the existing proxy `GET /api/bili/thumb?u=...&s=v|f|p`
  (`bilisearch.py`: only `*.hdslb.com` `/bfs/` paths, size limit, memory
  cache, a back-off when the image host is unreachable). Reuse as is.
- YouTube thumbnails: extend the same proxy with an allowlist for
  `i.ytimg.com`, fetched through the proxy setting YouTube already uses.
- Channel logos (`tvg-logo`): arbitrary hosts, so **not** proxied on demand
  (that would be an open fetcher). Fetch them when a channel table or preset is
  loaded, validate type and size, store them on disk, serve
  `GET /api/art/logo/{channel}`; fall back to the generated badge.
- Library posters: from `poster.jpg`/`folder.jpg`/`.nfo` next to the file, else
  a frame grabbed by ffmpeg at low priority; served from `GET /api/library/art/{id}`.
- Creator avatars: Bilibili face URLs are on `hdslb.com`, so the existing proxy
  covers them.

Every art slot has a deterministic placeholder (colour from the title, the
title in serif), used while loading, when blocked, and when no art exists, so
the grid never collapses.

## 9. Accessibility

- **Keyboard and remote-style navigation on the PC.** Arrow keys move focus to
  the nearest focusable element in that direction, first within the same region
  (content, top bar, sidebar, now-playing bar, sheet) and only then across;
  Enter activates; Esc closes a sheet; Backspace goes back from a detail page;
  `/` focuses search. Sliders keep left/right, up/down leave them. Rows scroll
  to keep the focused card visible. This is what a TV remote or a keyboard on a
  living-room PC needs. The prototype implements all of it.
- **Focus** is always visible: a 2 px amber outline; cards also lift and get an
  amber ring. Sheets trap Tab and return focus to their opener.
- **Contrast**: today's `--dim` (#8c8170) and `--faint` (#6e6455) on the ink
  background are below 4.5:1 for small text. The design raises them to #a19580
  and #857a68 and keeps the darker values for decoration only.
- **Hit targets**: at least 44 px on desktop controls; phone buttons 48 px, pad
  keys 58 px, list rows 58 px, bottom tabs 58 px.
- **Semantics**: navigation landmarks, `aria-current` on the active entry,
  pressed state on chips and segment buttons, `role="switch"` on switches,
  the picker as a `radiogroup`, `role="dialog"` with `aria-modal` on sheets,
  toasts in an `aria-live` region, every image-only button labelled
  ("Play <title>", "Remote <device>").
- **Motion**: no autoplaying video, no auto-rotating hero, nothing plays sound
  until the user presses Play; `prefers-reduced-motion` turns off transitions.

## 10. Visual language

Kept from today: `--ink #1c1915`, `--ink-deep`, `--side`, `--rule`,
`--paper`/`--paper-hi`, `--red #d2553f`, `--amber #ffb547`; serif (Songti SC /
Noto Serif SC) for headings, titles and buttons; monospace amber for channel
numbers, times and counts; sans for meta text. Added: `--card`, `--card-hi`,
`--rule-hi`, a 6 px radius on cards and buttons (today's pages are square; posters
need a little softness), red progress bars, amber "new" badges, red "live"
badges. Posters are 2:3, everything else 16:9.

## 11. API additions

Existing endpoints stay; the new ones aggregate them so the page does not make
ten requests to draw Home.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/home` | One call for Home: `hero[]`, `continue[]`, `live_now[]`, `creator_updates[]`, `series_updates[]`, `library_recent[]`, each a media item; per caller; cached a few seconds; guests get only `live_now` |
| `GET /api/guide?from=&to=&group=` | Channels with programmes for a time window (from the EPG work); includes channel number, group, logo URL |
| `GET /api/targets` | Play-to rows: every controllable TV plus the computer, with online, last seen, model, resolution, now playing, capabilities (seek, listen only); or fold into `/api/state.devices` plus a `pc` row |
| `POST /api/play` | `{item, target}` — one entry point that dispatches to today's switch, subs/play, history/play, cast and watch paths; returns the started session or a reason (`offline`, `needs_login`, `youtube_off`) |
| `GET/PUT /api/progress/{item}` | Resume position per user and item, with the device that reported it; written by the TV service and by the PC player |
| `GET /api/search?q=&scope=` | Grouped results over channels and EPG, series, creators, library, plus Bilibili (existing search with its rate limits) and YouTube; a pasted link becomes a "play this link" result |
| `GET /api/art/logo/{channel}`, `/api/library/art/{id}` | Cached logos and posters (section 8); YouTube added to the thumbnail proxy allowlist |
| `GET /api/library`, `GET /api/library/{id}`, `POST /api/library/folders`, `GET /api/library/scan` | Library index, item detail, folder management, scan status |
| `POST /api/connectors/{jellyfin,emby,plex}` | Optional media server link (address, token kept on the PC), mapped into the same item shape |
| `POST /api/sleep` | `{device, minutes}` (0 cancels): the remote panel's sleep timer, reusing the voice command's timed pause; its remaining time is reported in `/api/state` |
| `GET /api/events` (SSE, optional) | Push now-playing, online state and progress changes instead of polling `/api/state` |

## 12. Implementation plan

Estimates are agent-days of focused work including host tests, not calendar
time. Each phase ships on its own; the console keeps working between phases.

| Phase | Scope | Effort |
| --- | --- | --- |
| M0 Shell | Shared tokens and components (cards, shelves, sheets, chips, now-playing bar, spatial navigation) as one CSS/JS asset; new sidebar and routes in `console.html`; today's pages mounted under the new entries unchanged | 2 |
| M1 Play to and remote | `/api/targets`, `POST /api/play` dispatcher, the picker, the remote panel (moving today's dials, pets and pad), `POST /api/sleep`, remembered target | 3.5 |
| M2 Home | `/api/home` aggregator, Home rows, YouTube thumbnails in the proxy, cached M3U logos | 3 |
| M3 Live and lineup | Guide grid (needs `desktop-epg`), selected-programme header, Lineup page (moves "adjust groups", presets, size budget) | 3 |
| M4 Series, creators, detail, progress | Poster grid, creator page, detail pages, `/api/progress`, continue-watching rules (Q4) | 4 |
| M5 Search | Aggregated search, paste-a-link result, add/subscribe from results | 2 |
| M6 Phone | `remote.html` rebuilt on the same shell with bottom tabs (or one responsive page, Q1); guest view | 3 |
| M7 Local library | Folder picker, scanner (walk, `.nfo`/poster, filename parsing, low-priority thumbnail extraction), index, detail, playback to the PC and to TVs through the existing ffmpeg paths | 7 |
| M8 Media servers | Jellyfin and Emby connector (same API family); Plex after | 4 (+2 Plex) |
| M9 Polish | Accessibility pass, empty/error states, keyboard tests, screenshots for the docs and the website | 2 |
| **Total** | | **33.5 (35.5 with Plex)** |

Order: M0 → M1 → M2 are the visible change and can start now; M3 waits for the
EPG branch; M7 can run in parallel after M1 because it only needs the item
shape and `POST /api/play`.

Risks: the console is a 140 KB single file and the phone page 104 KB; moving
them to a shared asset is the first real cost (M0). Library scanning and
thumbnail extraction must stay low priority so they never disturb streaming to
the TVs (the PC is the household's TV server). Logo fetching must not become an
open URL fetcher. Bilibili rate limits apply to search aggregation exactly as
today.

## 13. Questions for you to decide

**Q1. One responsive page or two pages?** Today the PC console and the phone
page are separate files with duplicated styles. *Recommendation:* one shared
shell and component asset (M0), served as both pages first, merged into one
responsive page in M6 once the phone layout is proven. Guests keep a reduced
view.

**Q2. What "Lineup" means.** The prototype reads it as the **tiny TV's channel
list** (what channel up/down cycles through, per TV, with groups, order,
presets and the size budget), because Live already holds the programme guide.
*Recommendation:* keep it as its own entry on desktop and under Me on the
phone. If you meant the EPG itself, Lineup merges into Live as a second tab and
the TV channel list moves into each device's card.

**Q3. How Play to remembers.** Per browser, per account, or always ask?
*Recommendation:* per browser, default on, with the server's auto target as
fallback; one switch in the picker turns it into "always ask".

**Q4. Progress from watching on the PC.** Today PC watching deliberately does
not mark anything watched or move a TV's series position. Continue watching
needs positions. *Recommendation:* record PC positions separately and show
them in Continue watching with the device name; never advance a TV channel's
cursor from the PC unless the user picks "continue on <TV> from here".

**Q5. The hero.** *Recommendation:* up to three items picked automatically
(a new episode of a followed series, a live programme flagged in the EPG or the
user's most watched channel, the newest library item); no autoplay, no
automatic rotation.

**Q6. Library v1 scope.** *Recommendation:* any folder the operating system can
see (a NAS share the user has mounted counts); metadata from local `.nfo` and
poster files plus filename parsing; no online scraping (TMDB keys, privacy,
reachability in mainland China) in v1, optional later.

**Q7. Media servers.** *Recommendation:* Jellyfin and Emby first (open, one API
family), Plex later; read-only browsing and playback, no sync.

**Q8. "This phone" as a target.** Full video on the phone is new load on the PC
(transcoding, bandwidth over the cloud channel when away). *Recommendation:*
not in the first release; keep the existing listen-on-phone page; revisit after
M6.

**Q9. Who sees the library?** *Recommendation:* family members see the whole
library; guests never; a per-folder "only me" later.

**Q10. Light theme.** *Recommendation:* dark only for now; the tokens make a
light theme a later CSS change.

**Q11. Channel logos.** *Recommendation:* cache at channel-table load time and
serve locally (section 8); never proxy arbitrary logo URLs on demand.

## 14. Prototype notes

- File: `docs/design/media-center/index.html`, about 120 KB, no dependencies,
  no requests. Hash routes: `#/home`, `#/live`, `#/lineup`, `#/series`, `#/up`,
  `#/lib`, `#/search`, `#/devices`, `#/settings`, `#/detail/<id>`, and on the
  phone `#/remote` and `#/me`. Query parameters for demos: `view=phone`,
  `state=notv|nosubs|biliblocked`, `q=<search>`, `sheet=target|remote:<device>`.
- The clock is fixed at 20:12 so screenshots are repeatable. All titles,
  creators, programmes and covers are invented placeholders.
- Not prototyped: the add-folder flow, the media server sign-in, family and
  session management (unchanged from today), the in-page video player.
- Screenshots for review are generated into `.shots/` (git-ignored) from the
  file with a headless browser at 1440x900 (desktop) and 390x844 (phone); they
  are not part of the repository.
- Verified: renders in a Chromium-based browser at both sizes, all routes and
  sheets open, arrow-key navigation reaches rows, guide blocks and the sidebar.
  Not verified: Safari and Firefox, screen readers, a real touch device.
