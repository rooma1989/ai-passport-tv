<p align="right">
  <a href="2026-10-24-media-center-ui-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Home media center UI — design

Status: **design accepted 2026-10-24; phases M0, M1 and M2 implemented on branch `rootv-ui` for
v0.3.0-beta (2026-10-25); the M3 programme guide on branch `rootv-live` (2026-10-26, Lineup still
open)**, see section 16 for what was built and where it differs. First version on
branch `media-center-design` (based on `v0.2.4-beta`); the owner accepted every
recommendation in section 13. **Revised the same day** on branch
`media-center-appletv` (based on main `136571d`): a visual language closer to a
TV box's system UI (section 10), a top tab bar instead of the sidebar on the
desktop (section 2), and the product name RooTV with three app icon candidates
(section 15). The clickable prototype is
[`docs/design/media-center/index.html`](../../design/media-center/index.html):
one self-contained file, no network requests (its Content-Security-Policy
forbids them), fake data, placeholder art drawn in SVG. Open it in any browser;
the dashed "prototype" box in the corner switches between the desktop and the
phone layout, between dark and light, between demo states (normal, no TV, no
subscriptions and an empty library, Bilibili unreachable) and between the three
app icon candidates.

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
- A calm look in the manner of a TV box's system UI (section 10): dark by
  default, large rounded artwork, focus that lifts the card, translucent
  materials. Where the UI depicts the tiny TV's own screen it keeps that
  screen's amber digits.

### Non-goals

- No change to the device firmware, the wire protocol or the tiny TV's own
  on-screen menu.
- No new content sources beyond the planned local library and the optional
  Jellyfin/Emby/Plex connection.
- No light theme in the first release: it is designed and in the prototype, and
  ships in M9 (Q10).
- No recommendation engine: the home page is assembled from the household's own
  subscriptions, history and EPG.

## 2. Information architecture

Desktop: a **top tab bar** with six tabs in a centred translucent capsule (Home,
Live, Series, Creators, Library, Search), the wordmark on the left, and on the
right the Play-to chip, **Devices & remote** and **Settings** as icons, and the
user. The bar is transparent over a hero and turns into a blurred material once
the page scrolls. Lineup is no longer a top-level entry: it is the second
segment of Live ("Guide | TV lineup"). A now-playing bar floats at the bottom.
Phone: five bottom tabs.

| Entry | Contents | Replaces today |
| --- | --- | --- |
| **Home** | Hero (up to three items), rows: Continue watching, Live now, Creator updates, New episodes, My library | the "Remote" page as landing page |
| **Live** | Selected programme on top (channel, title, progress, next, Play / Watch on this computer), group chips, programme guide grid with time ruler and now-line | the channel grid on the "Remote" page |
| **Lineup** (second segment of Live) | The channel list each tiny TV cycles through: per TV, groups, order, show/hide, size budget (channels and bytes), live sources and presets, EPG status | "Adjust groups" link, admin channel table presets |
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

**Detail pages** (reached from posters and search): a full-bleed backdrop with
title art (the logo image when the metadata has one, otherwise the title set
large), meta line (year, kind, source, schedule), synopsis, "last watched on
<device> at <position>" with a progress line, actions (Continue episode N at
position, Play to..., From episode 1, Following), episodes as a shelf of 16:9
cards with watched/current/not-yet-released states (not-yet-released cards are
disabled and skipped by focus), related row. Library movies show file facts (resolution, codecs,
subtitles, size, folder) and the note that playback on a tiny TV is transcoded
by the PC.

## 3. The now-playing bar

Always visible at the bottom (above the tabs on the phone). It follows the
**session the user last started or controlled**: artwork, title, "<device> ·
<programme or position>", a progress line, previous / play-pause / next and a
remote button for TVs. On the desktop it floats as a blurred capsule (at most
960 px wide) over the content; the Play-to chip stays in the top bar instead of
being repeated here. Clicking the artwork
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

Desktop: a 440 px sheet floating at the right of the current page. Phone: the Remote tab
is the panel as a page; from elsewhere it is a bottom sheet. Changes apply
immediately with a short toast, as today. Every control has a 44 px (desktop)
or 52–58 px (phone) hit target.

## 6. Layouts

| | Desktop (>= 700 px) | Phone (< 700 px) |
| --- | --- | --- |
| Navigation | top tab bar: centred capsule with six tabs; wordmark left; Play-to chip, Devices and Settings icons and the user right; transparent over heroes, blurred material once scrolled | bottom tab bar (5), blurred material, 54 px targets |
| Page title | large title, 40 px, under the bar (none on Home and detail pages) | large title, 34 px; when it scrolls away a 17 px title appears in the navigation bar |
| Rows | horizontal shelves bleeding to the window edge, 300 px landscape cards, 176 px posters, 24 px gaps | 248 px / 118 px, 14 px gaps, scroll snap |
| Hero | full-bleed, 74 % of the window height (500 to 740 px), fades into the page; the first row overlaps its lower edge | 560 px, text and full-width buttons at the bottom |
| Guide | 210 px channel column (number, logo, name), 5 px per minute | 64 px column (logo only), 3.2 px per minute, preview image hidden |
| Detail | full-bleed backdrop with title art, then the episode shelf and a related shelf | same, 600 px backdrop; buttons wrap to full width |
| Sheets | 440 px sheet floating at the right, 12 px from the edges, 26 px radius | bottom sheet up to 92 % high with a grabber, 24 px top radius |
| Now-playing bar | floating capsule, at most 960 px wide, 72 px high | 62 px capsule floating above the tab bar; hidden on the Remote tab |

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
title in heavy sans), used while loading, when blocked, and when no art exists, so
the grid never collapses.

## 9. Accessibility

- **Keyboard and remote-style navigation on the PC.** Arrow keys move focus to
  the nearest focusable element in that direction, first within the same region
  (content, top bar, now-playing bar, sheet; the tab bar on the phone) and only
  then across. Left and right inside a row stay in that row: the end of a row
  does not jump to another one. Enter activates. Esc is the remote's Back: it
  closes a sheet, leaves a text field, goes back from a detail page, otherwise
  moves focus to the tab bar, and from the tab bar returns Home. Backspace also
  goes back from a detail page; `/` opens Search. Sliders keep left/right,
  up/down leave them. Rows scroll smoothly to keep the focused card fully
  visible, and the page scrolls so the row clears the top bar and the
  now-playing bar. After Play re-renders the page, focus stays on the same
  item. This is what a TV remote or a keyboard on a living-room PC needs. The
  prototype implements all of it.
- **Focus** is always visible (section 10): cards lift, scale and catch a
  highlight; buttons, chips and tabs take the focus colour; everything else
  gets a 2 px ring. Sheets trap Tab and return focus to their opener.
- **Contrast**: the text tokens give at least 4.5:1 for small text on their
  backgrounds in both themes (`--label-3` about 5:1); `--label-4` is for
  decoration only. Text on artwork sits on a dark gradient and is always white.
- **Hit targets**: at least 44 px on desktop controls (buttons 46 px, tabs
  40 px inside a 50 px capsule); phone buttons 50 px, pad keys 60 px, list rows
  60 px, tab bar items 54 px.
- **Semantics**: navigation landmarks, `aria-current` on the active entry,
  pressed state on chips and segment buttons, `role="switch"` on switches,
  the picker as a `radiogroup`, `role="dialog"` with `aria-modal` on sheets,
  toasts in an `aria-live` region, every image-only button labelled
  ("Play <title>", "Remote <device>").
- **Motion**: no autoplaying video, no auto-rotating hero, nothing plays sound
  until the user presses Play; `prefers-reduced-motion` turns off transitions,
  the card tilt and the sheen.

## 10. Visual language

Revised 2026-10-24. The warm-red serif look of the first version is replaced by
a style closer to a TV box's system UI; the owner's reference is the Apple TV
app and tvOS. It evokes that style and copies none of it: no Apple logo, no
"TV+" marks, no Apple icons or assets; every icon is a simple inline SVG drawn
for this project.

**Colour tokens.** Dark is the default; the light variant redefines the same
names. Areas over artwork (hero, detail backdrop, the top bar while it floats
over them) are pinned to the dark set with a `.dk` class, so text on images is
always white in both themes.

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `--bg`, `--bg-2`, `--bg-3` | #0b0b0d, #1a1a1d, #26262b | #f2f2f6, #ffffff, #e4e4ea | page; grouped lists and panels; art placeholders |
| `--fill`, `--fill-2`, `--fill-3` | white at 8, 14, 24 % | black at 5, 8, 15 % | buttons, chips, guide blocks, tracks |
| `--label`, `--label-2`, `--label-3` | #f5f5f7, 68 %, 55 % | #111113, 80 %, 66 % | text hierarchy; `--label-4` (about 30 %) is decoration only |
| `--sep` | white at 10 % | black at 10 % | hairlines |
| `--mat`, `--mat-thick` | rgba(40,40,46,.52), rgba(26,26,30,.80) | white at 62 %, rgba(248,248,251,.84) | materials, always with the blur |
| `--focus-bg`, `--focus-fg` | #f5f5f7, #0b0b0d | #111113, #ffffff | focused controls |
| `--brand` | #ff6b3d, gradient to #ffb05a | same | active phone tab, guide now-line, "new" badges, switches, the primary button's focus ring |
| `--live`, `--ok` | #ff4b3e, #34c76a | same | "live" badge; online dot |

The tiny TV's own screen (device cards, the remote panel's readout) keeps the
firmware's amber monospace digits in both themes, because it depicts that
screen.

**Materials.** `backdrop-filter: saturate(180%) blur(28px)` over `--mat` (tab
capsule, Play-to chip, badges on artwork) or `--mat-thick` (top bar after
scrolling, now-playing bar, sheets, toasts, phone tab bar). Without
backdrop-filter the thick colours are opaque enough to stay legible.

**Shape.** Radii 6 px (badges), 10 px (small buttons, guide blocks), 12 px
(buttons, posters), 14 px (landscape cards), 16 to 20 px (lists, panels, search
field, guide), 22 to 26 px (now-playing bar, sheets), fully round for chips and
tabs. Posters 2:3, everything else 16:9. Shadows: cards `0 8px 22px` at 38 %
(12 % in light), focused cards `0 30px 56px` at 62 % (26 % in light). In light
the hero does not fade into the white page: it ends on a darkened lower edge and
the first row starts below it.

**Motion.** Focus and hover 220 ms `cubic-bezier(.2,.8,.2,1)`; colour changes
140 ms; fades 260 ms; sheets 380 ms `cubic-bezier(.32,.72,0,1)`, from the right
on the desktop and from the bottom on the phone; the sheen on a newly focused
card runs once in 900 ms. `prefers-reduced-motion` turns all of it off.

**Focus.** A focused or hovered card lifts 6 px, scales to 1.07, takes the
deeper shadow and a soft highlight; under a mouse it also tilts up to about
4 degrees towards the pointer and the highlight follows the pointer; keyboard
focus plays one sheen sweep instead. The labels under the card move down 5 px.
Buttons, chips, tabs and segments take the focus colour (white on dark) and
scale to 1.05 to 1.07; guide blocks take the focus colour and scale only 1.04;
list rows get a fill and a 2 px inner ring. On the phone cards scale to 1.04.
Hovering a card while keyboard focus is in the content moves the focus there,
so two cards are never raised at once.

**Typography.** System fonts only, no web fonts:
`-apple-system, "SF Pro Display", "PingFang SC", "Helvetica Neue", sans-serif`,
tabular figures for numbers and times. Scale: title art 66 px / 800 (52 px for
long titles, 40 px on the phone); large title 40 px / 700 (34 px phone); live
and detail headline 32 px / 700; row and sheet titles 22 px / 700 (20 px
phone); body 15 px; secondary 13 to 14 px; captions 12 px; phone tab labels
10.5 px. Title art shows the logo image when metadata provides one; otherwise
the title is set large with a light gradient tinted from the artwork's hue.

**Rhythm.** Content gutter 56 px on the desktop, 16 px on the phone; 24 px
between cards (14 px phone); rows 30 px apart; space instead of rules and
borders.

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
| M0 Shell (**done**, `rootv-ui`) | Shared tokens (dark, light prepared) and components (cards with focus lift and tilt, shelves, materials, sheets, chips, floating now-playing bar, spatial focus engine with row scrolling and focus restore) as one CSS/JS asset; top tab bar and routes in `console.html`; today's pages mounted under the new entries unchanged | 3 |
| M1 Play to and remote (**done**, `rootv-ui`) | `/api/targets`, `POST /api/play` dispatcher, the picker, the remote panel (moving today's dials, pets and pad), `POST /api/sleep`, remembered target | 3.5 |
| M2 Home (**done**, `rootv-ui`) | `/api/home` aggregator, Home rows, YouTube thumbnails in the proxy, cached M3U logos | 3 |
| M3 Live and lineup (guide **done**, `rootv-live`; selected-programme header and Lineup open) | Guide grid (needs `desktop-epg`), selected-programme header, Lineup as the second segment of Live (moves "adjust groups", presets, size budget) | 3 |
| M4 Series, creators, detail, progress | Poster grid, creator page, detail pages, `/api/progress`, continue-watching rules (Q4) | 4 |
| M5 Search | Aggregated search, paste-a-link result, add/subscribe from results | 2 |
| M6 Phone | `remote.html` rebuilt on the same shell with bottom tabs (or one responsive page, Q1); guest view | 3 |
| M7 Local library | Folder picker, scanner (walk, `.nfo`/poster, filename parsing, low-priority thumbnail extraction), index, detail, playback to the PC and to TVs through the existing ffmpeg paths | 7 |
| M8 Media servers | Jellyfin and Emby connector (same API family); Plex after | 4 (+2 Plex) |
| M9 Polish | Accessibility pass, empty/error states, keyboard tests, light theme checked on every screen, screenshots for the docs and the website | 2.5 |
| R Rename | RooTV: app and bundle names, installers, data-folder migration with tests, autostart entries, docs and website (section 15); the chosen icon as `.icns`, `.ico`, PNGs and tray templates. Firmware boot text not included | 2 |
| **Total** | | **37 (39 with Plex)** |

Order: M0 → M1 → M2 are the visible change and can start now; M3 waits for the
EPG branch; M7 can run in parallel after M1 because it only needs the item
shape and `POST /api/play`. R is independent of the others and can go first or
last; it needs the icon choice (Q12).

Risks: the console is a 140 KB single file and the phone page 104 KB; moving
them to a shared asset is the first real cost (M0). Library scanning and
thumbnail extraction must stay low priority so they never disturb streaming to
the TVs (the PC is the household's TV server). Logo fetching must not become an
open URL fetcher. Bilibili rate limits apply to search aggregation exactly as
today. Blur materials cost GPU time on old integrated graphics; the thick
material colours are chosen to work without the blur, so it can be switched
off per machine.

## 13. Questions for you to decide

The owner accepted every recommendation in Q1 to Q11 on 2026-10-24. Two were
revised by the visual update the same day (Q2, Q10); Q12 and Q13 are new and
open.

**Q1. One responsive page or two pages?** Today the PC console and the phone
page are separate files with duplicated styles. *Recommendation:* one shared
shell and component asset (M0), served as both pages first, merged into one
responsive page in M6 once the phone layout is proven. Guests keep a reduced
view.

**Q2. What "Lineup" means.** The prototype reads it as the **tiny TV's channel
list** (what channel up/down cycles through, per TV, with groups, order,
presets and the size budget), because Live already holds the programme guide.
*Accepted, then revised:* with the top tab bar it is the second segment of Live
("Guide | TV lineup") on the desktop and stays under Me on the phone.

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

**Q10. Light theme.** *Accepted, then revised:* dark stays the default and ships
first; the light variant is now designed (section 10, toggle in the prototype)
and ships in M9.

**Q11. Channel logos.** *Recommendation:* cache at channel-table load time and
serve locally (section 8); never proxy arbitrary logo URLs on demand.

**Q12. Which app icon?** Three candidates (section 15): A mouse inside a retro
TV, B a TV with mouse ears, C a mouse resting on a TV. *Recommendation:* B for
the clearest silhouette at 16 px and in the menu bar; A if a more illustrative
icon matters more than the small sizes.

**Q13. When to rename?** *Recommendation:* as its own phase R after M0 (so the
new shell ships under the new name), with the data folder moved automatically
and the old path still read for one release.

## 14. Prototype notes

- File: `docs/design/media-center/index.html`, about 160 KB, no dependencies,
  no requests. Hash routes: `#/home`, `#/live`, `#/lineup`, `#/series`, `#/up`,
  `#/lib`, `#/search`, `#/devices`, `#/settings`, `#/brand`, `#/detail/<id>`,
  and on the phone `#/remote` and `#/me`. Query parameters for demos:
  `view=phone`, `theme=light`, `logo=a|b|c`, `state=notv|nosubs|biliblocked`,
  `q=<search>`, `sheet=target|remote:<device>`, `chrome=0` (hides the prototype
  panel).
- The product names are the constants `BRAND` and `BRAND_ZH`; the app icons,
  tray icons and wordmark come from one builder in the page (between the
  `BRAND-ICONS` markers), which `brand/export.mjs` also uses to write the
  standalone SVG files.
- The clock is fixed at 20:12 so screenshots are repeatable. All titles,
  creators, programmes and covers are invented placeholders.
- Not prototyped: the add-folder flow, the media server sign-in, family and
  session management (unchanged from today), the in-page video player.
- Screenshots for review are generated into `.shots/` (git-ignored) from the
  file with a headless browser at 1440x900 (desktop) and 390x844 (phone); they
  are not part of the repository.
- Verified: renders in a Chromium-based headless browser at both sizes and in
  both themes with no console errors and no network requests; all routes and
  sheets open; arrow-key navigation reaches the hero, rows, guide blocks and the
  tab bar, and Esc walks back as described in section 9. Not verified: Safari
  and Firefox (backdrop-filter and the focus tilt in particular), screen
  readers, a real touch device, real mouse hover (the tilt was exercised only
  in code review).

## 15. Name and brand

Decided 2026-10-24: the product is **RooTV**. Its Chinese name (four characters
meaning "little mouse TV", written out in the Chinese peer of this document and
in `BRAND_ZH`) is used wherever a Chinese UI needs a product name: window and
tab titles, the About row, the installer, empty states that name the product.
The wordmark always reads "RooTV". The tiny TV hardware keeps its generic name in
the UI. In the prototype the two names live in `BRAND` and `BRAND_ZH` and
nowhere else.

**Wordmark.** Geometric round-cap strokes, no font: "Roo" in the label colour,
"TV" in coral, so the exported file looks the same everywhere. A stacked lock-up
adds the Chinese name beneath; in the exported file that line is live text and
must be converted to outlines before production use.

**App icon: three candidates; B chosen (Q12, decided 2026-10-25).**

- **A, mouse in the TV**: a mouse peeking up inside a cream retro TV with
  antennas; coral background, matching the UI accent.
- **B, TV with mouse ears**: the TV is the mouse's head, two round ears on top
  and the screen as its face; night-blue background. Simplest silhouette.
- **C, mouse on the TV**: a mouse resting its paws on a TV whose screen shows a
  play symbol; warm yellow background.

Each has a 1024 master on a superellipse ("squircle") background, used from
64 px up; a simplified small version for 16 to 32 px (no whiskers, paws,
antenna tips or highlights, larger features); and a single-colour tray
template on a 32 grid, black on transparent, which macOS recolours for light and
dark menu bars (Windows uses the same shape in white or black). All three are
original drawings and use no existing cartoon character or platform mark. The
prototype's Brand page (`#/brand`) shows each at 1024 (scaled), 180, 64, 32 and
16 px, in light and dark menu bars, and in lock-ups with the wordmark and the
Chinese name; "Try in the top bar" swaps it into the page header.

**Files.** In `docs/design/media-center/brand/`: `icon-{a,b,c}.svg`,
`icon-{a,b,c}-small.svg`, `tray-{a,b,c}.svg`, `wordmark-rootv.svg`,
`wordmark-rootv-white.svg`, `wordmark-lockup-zh.svg`,
`wordmark-lockup-zh-white.svg`. They are written by
`node docs/design/media-center/brand/export.mjs` from the builder inside the
prototype, so the page and the files cannot drift. `.icns`, `.ico` and PNG
sizes are produced from icon B by `python3 tools/make_icons.py` (phase R).

**Rename scope** (phase R; timing is Q13). What carries the old name today:

| Area | Today | Change |
| --- | --- | --- |
| Desktop app | macOS `Xiaozhi TV.app` with executable `XiaozhiTV`; tray menu and About | `RooTV.app`; Chinese display name where the system shows localised names |
| Installers and archives | `XiaozhiTV-<ver>-macos-<arch>.dmg`, `XiaozhiTV-<ver>-windows-x64.zip` and the setup built from `packaging/desktop/xiaozhi-tv.iss` (app name, Start menu and desktop shortcuts, uninstaller), `xiaozhi-tv-<ver>-linux-x86_64.tar.gz` | `RooTV-<ver>-...` and `rootv-<ver>-linux-...`; the Windows setup upgrades or removes the old installation cleanly |
| Data folder | `XiaozhiTV` (`xiaozhi-tv` on Linux) from `tools/datadir.py` and `desktop/paths.py`; variables `XIAOZHI_TV_DATA` and `XIAOZHI_TV_CORE`; autostart entries named `XiaozhiTV` (`desktop/autostart.py`, the Windows Run key) | new folder `RooTV` (`rootv` on Linux); on first start move the old folder when the new one does not exist; read the old path and variables for one more release; replace the old autostart entry rather than adding a second |
| Pages and documents | console and phone page titles and icons, README, user documents, screenshots | new name and icon; the CHANGELOG records the rename |
| Website | `packaging/site/` with the download page | name, icon, download file names |
| Tiny TV boot text | product name on the firmware's boot screen and settings page | firmware change, assessed separately; not in phase R's estimate |

## 16. Implementation status (M0 to M3)

Built on branch `rootv-ui` (2026-10-25) for v0.3.0-beta. The console at `/tv/console` is now three
static files in `server/relay/assets/`: `console.html` (markup), `console.css` (tokens, materials,
components, the phone layout) and `console.js` (shell, focus engine, Play to, remote, Home, and the
ported pages). `console.py` stamps a content hash into the stylesheet and script URLs; the page's CSP
adds `style-src 'self'`. The previous console stays at `/tv/console-classic` for this beta, linked
from Settings (the "classic console" row), and links back.

- **M0 shell**: top tab bar (Home, Live, Series, Creators, Library, Search; Devices and Settings as
  icons), hash routes, now-playing bar, sheets, toasts, the spatial focus engine (arrow keys within a
  region and row first, Enter, Esc closes a sheet, leaves a field, goes to the tab bar, then Home; `/`
  opens Search), focus restore after the 5-second refresh, card tilt and sheen. Dark by default; the
  light variant is a switch in Settings, stored in `localStorage` (`rootv.theme`), ahead of M9
  because the owner asked for it. Windows narrower than 700 px get the phone layout (bottom tabs Home,
  Live, Search, Remote, Me) in the same page; `remote.html` is unchanged (M6).
- **Pages not redesigned yet are ported, not rewritten**: Live shows the group chips and channel
  cards (logo or generated badge, EPG now and next, "adjust groups"); the programme guide grid and
  Lineup are M3. Series shows a poster grid plus the management list (episodes drawer, delete,
  add); Creators shows the latest unwatched videos, the list (play, play to, delete), add and
  "import my Bilibili followings". Search is the Bilibili search with a "play this link" first result
  when a link is pasted (casting moved here, M5 adds the aggregated search). Library is the empty
  state ("the library is coming soon"). Devices are cards (screen readout, remote, default target, watch on this
  computer, rename, Wi-Fi, guest code, re-pair, unbind) plus bind and "let a phone sign in".
  Settings holds Play-to memory, appearance, history ("played before"), sessions, the Bilibili
  account, YouTube, invites, the admin link, the classic console and About.
- **M1 Play to**: `GET /api/targets` (each TV with online state, last seen, model, what it plays,
  capabilities; "this computer" only where watching on the PC exists, so never on the cloud relay)
  and `POST /api/play {target, item, viewer}` (`server/relay/home.py`), which dispatches to switch,
  subscription play, episode pick, cast, history replay and `watch_direct`, and answers a `reason`
  (`offline`, `needs_login`, `youtube_off`, `no_device`, `bad`). The remembered target is per browser
  and user (`localStorage`, `rootv.target:<name>`), falling back to the server's auto device, then to
  this computer; an offline target opens the picker with a message. A creator video sent to a TV
  switches to that creator's channel, which plays its own next unwatched video (the answer says so);
  choosing an episode for a TV moves that TV's series position, as picking an episode always did.
- **M1 remote**: the sheet (the Remote tab on the phone) has device pills, the amber readout with a
  seek bar for videos, channel up and down (the channel table in group order, like the TV's own
  keys), pause, back and forward 30 s, next, previous episode, episodes, "watch this on the
  computer", volume, brightness as one button per device step, listen only, the sleep timer (off,
  15, 30, 60, 90 minutes) and the pets (the firmware's 11, not six). `POST /api/sleep {device,
  minutes}` reuses the voice command's timer (`voicecmds.set_timer`: dim, then pause; 0 cancels);
  `/api/state` reports it as `sleep`.
- **M2 Home**: `GET /api/home`, per caller, cached 4 s and dropped after a play. Hero: up to three
  automatic picks (a followed series to continue, the live channel the TV shows or one with EPG now,
  the newest creator video), manual arrows only. Rows: Continue watching, Live now, Creator updates,
  New episodes, My library (placeholder); guests get only Live now. Covers: the Bilibili client keeps
  the cover and face URLs it receives with lists in memory (a restart shows placeholders until the
  next list refresh); YouTube thumbnails go through the same `/api/bili/thumb` proxy, limited to
  `i.ytimg.com/vi/<id>/<fixed names>.jpg` and fetched through the local-mode YouTube proxy.
- **PC progress (Q4)**: positions watched on this computer are kept per user in
  `<data>/pc-progress.json` (`home.PcProgress`), shown in Continue watching as "this computer", used as
  the resume point after a restart, and never mark anything watched or move a TV's position. TV
  positions already saved in the owner's state appear in the same row as "the tiny TV".
- **Logos (Q11)**: `GET /api/art/logo/<channel>` (`server/relay/logos.py`) takes only a channel id;
  the URL comes from `channels.txt.meta.json`. Each logo is fetched once on first request (not at
  table load), only over http(s) to public addresses, at most 256 KB, PNG/JPEG/GIF/WebP by magic
  bytes (no SVG), stored under `<data>/logos/`; failures are retried after 6 hours, and a channel
  without a logo answers a cacheable 404 so the page draws the badge.
- **PC player (after M2)**: the player window has its own controls (play/pause, position and length in
  the video's timeline, a draggable bar with the buffered range, back/forward 10 s, volume and mute,
  fullscreen, next, an episode sheet; Space, Left/Right, F, M, Esc while it has focus, is enlarged or
  fullscreen). Position = the answer's `start` + media time − the first fragment's media time
  (`#EXT-X-RTV-ORIGIN` in the playlist, `watch.Window.origin`); length = the answer's `duration`
  (Bilibili view and pages, YouTube, the subscription lists). A seek inside the buffer moves the media
  element; outside it, the page asks `POST /api/watch` again with `start` and gets a new stream (ffmpeg
  with `-ss`, new id and token) and a new MediaSource; following a TV, a seek gives the page a stream of
  its own and leaves the TV alone. Parts: multi-part videos (`pages`), collections (`ugc_season`),
  bangumi seasons, YouTube playlists (`Bilibili.parts`, `client.series`) and the series and creator
  channels; a cast item is addressed as `{video: {site, id}, part}`, its info cached 30 minutes. PC
  progress is reported every 10 s and on close (`POST /api/watch/progress`, `/api/watch/stop`), kept per
  user, item and part with the parts finished on the PC (`done`), and resumed with a "from the start"
  choice; a finished part moves on to the next, both when it ends (5-second countdown) and when the
  item is opened again.
- **On-demand playback (VOD)**: Bilibili videos the browser can play natively — the single-segment
  H.264 MP4 of the html5 playurl (720p at most) — no longer go through ffmpeg: the page asks with
  `vod: true` (and `quality`), and `web.py`'s `_vod_source` answers `mode: "vod"` with
  `vod/<id>/<token>/media`, the start position and the duration; the item, part and start come from the
  same planners as the HLS path. `server/relay/vod.py` forwards the file with Range passed through
  (206/Content-Range/Content-Length/Accept-Ranges, Referer and User-Agent added), streams it in 64 KB
  chunks without the disk, closes the upstream when the browser drops a request, re-resolves the address
  once on 403/404/410 (and after 90 minutes), allows four requests per stream (the oldest is cut), shares
  `watch.py`'s caps, and reaps a stream after 180 s without requests or progress reports (a paused page
  reports once a minute). `VodPlayer` in `console.js` is a native `<video>` behind the same controls:
  position = `currentTime`, seeks are the browser's, buffered range from the element, speed 0.75–2×
  (remembered; `<` `>`), picture-in-picture (`P`), the start via `#t=` and `loadedmetadata`. A failure
  before the first frame switches to HLS (`vod` left out); a failure mid-play reopens at the same position
  once. A signed-in Bilibili account gets a 720P (VOD, default) / 1080P (HLS) menu, remembered per browser.
  Live channels, YouTube (the node only gives a real-time MPEG-TS stream, no ranged file), multi-segment,
  FLV and HEVC files, the classic console and TVs stay on HLS / the relay streams.
- **M3 Live (guide)**, branch `rootv-live` (2026-10-26): the Live page has a **Channels | Guide** segment
  (remembered per browser, `rootv.liveView`). Channel tiles read the channel name, then "Now: <title> ·
  HH:MM–HH:MM" with a progress bar and "next"; the console asks `/api/epg?channels=` only for the visible
  group plus the channels the TV and this computer are playing, once a minute and again as soon as a cached
  programme has ended. The guide grid follows section 6 (210 px channel column with number, logo and name,
  5 px per minute, sticky ruler and channel column, a now-line with the time in the ruler, past programmes at
  half opacity, the current one with a progress line, titles pinned when a block is cut by the left edge). Its
  range starts an hour before now (rounded to the half hour) and ends 3, 6 (default) or 12 hours ahead,
  remembered as `rootv.guideSpan` and clamped to what the server keeps; it is fetched with
  `/api/epg?channels=&from=&to=&max=` in batches of 60 channels and refetched when the half hour moves or after
  5 minutes. Clicking a channel or the current programme plays it through Play to; a past or future programme
  opens a popover (time, duration, "starts in N minutes" or "finished, catch-up not supported", "watch what this
  channel shows now"). Creator, series and cast channels in a group get one full-width block saying what they
  play; a channel without a guide gets "no programme information" (section 7). The focus engine treats a guide
  row like a shelf (left and right stay in the row); Esc closes the popover and returns focus to the block. Below
  700 px the guide is a list per channel with the current programme, its progress and the next one (no time
  ruler). The player window and the now-playing bar read "CCTV5 · <programme>" with the time and next programme
  underneath. Home's "Live now" row already used now/next from `/api/home` and now also refreshes at a programme
  boundary. `GET /api/epg` gained `from`, `to` (Unix seconds) and `max`: each channel then carries
  `programmes` overlapping the range, clamped to the kept window (reported as `window: {before, after}`), at most
  60 channels and 64 programmes per channel; malformed numbers answer `ok: false`. Titles drop CCTV's trailing
  "-2026-248" (year and issue) or "-2026-10-26" when the guide is read (`epg.tidy_title`). With the TV idle the
  Live page opens on the first group that has live channels instead of the cast group. Not built: the
  selected-programme header and the Lineup segment; "remind me" for future programmes.
- **Not verified**: real playback on the PC in the new player window (the UI check used generated
  data and no network), Safari and Firefox, Windows, screen readers, a real touch device; for the PC
  player, real Bilibili multi-part videos, collections and bangumi, YouTube playlists and fullscreen.
