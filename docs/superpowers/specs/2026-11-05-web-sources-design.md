<p align="right">
  <a href="2026-11-05-web-sources-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Video from other websites — design

Status: approved by the owner section by section, 2026-11-05. Part A (any website through yt-dlp) is designed here; part B (Tencent Video, Youku, iQIYI, Mango TV with a signed-in account) gets its own design after a DRM test.

## 1. Goal and boundary

The owner wants more sources than Bilibili, YouTube and the library: paste any video page and play it on the PC or the small TV, subscribe to a creator or a playlist on any site, and search sites that can be searched.

- Only ordinary video pages that yt-dlp has a **dedicated extractor** for. yt-dlp's `generic` extractor (any page with a video tag) is refused.
- **No resource-site aggregation.** "Apple CMS" collection APIs (`/api.php/provide/vod`, the kind LibreTV aggregates) carry pirated films and are not supported or shipped, now or later.
- **No DRM circumvention.** A video yt-dlp reports as DRM-protected is shown as such and never retried.
- Cookies belong to the server and are imported by the admin per site (cookies.txt). The owner's browser is never read.
- Family accounts only; guests do not see websites (same rule as the library). Self-host and PC client only, local YouTube mode only; the lite edition hides the page.

Approach: extend the existing local yt-dlp node (`server/relay/ytnode.py`) rather than calling yt-dlp from the relay or embedding it. The node already owns installation and auto-update, the resolve cache, play-first scheduling, the TV's small TS stream and the PC's format list.

## 2. Node: URL-based routes (`ytnode.py`, local mode only)

| Route | Answer |
|---|---|
| `GET /web/info?url=` | One video: title, duration, thumbnail, uploader, site (`extractor_key`). A creator page or playlist answers `kind: "list"` with its title. |
| `GET /web/list?url=&n=&start=` | Newest N videos of a creator page or playlist, with paging. |
| `GET /web/search?site=&q=&n=` | Only sites with a yt-dlp search: Bilibili (`bilisearch`), Dailymotion (`dmsearch`), niconico (`nicosearch`), SoundCloud (`scsearch`). |
| `GET /web/formats?url=` | For the PC: every format with its HTTP headers, like `/yt/formats`. |
| `GET /web/stream?url=&ss=` | For the TV: the same small MPEG-TS as `/yt/stream`. |

- Per-site settings by host: cookies at `web-cookies/<host>.txt` and a proxy choice (direct, the default, or the YouTube proxy).
- URLs: http/https only; loopback, private, link-local and `file:` targets are refused; logs carry the host only, never the full URL or a media address.
- The resolve cache, play-first scheduling and yt-dlp auto-update are shared with YouTube.

## 3. Relay (`server/relay/websites.py`)

- A `"web"` entry in `Relay.clients` with the YouTube client's interface (`video`, `open`, `formats`, `creator`, `series`, `search`). Video ids are the normalised page URL with `site="web"`, so casting, resume, Continue watching and channels work unchanged.
- Subscribing to a creator page makes an up-channel (`kind="up"`); a playlist makes a series channel. Both count against the existing channel cap and the TV's settings pack.
- Home: one row per subscribed creator, refreshed every 2 hours; the aggregate search also searches those titles.
- PC playback by format: single H.264 + AAC MP4 → relayed to `VodPlayer` (speed and seeking); DASH with segment indexes → `DashPlayer`; anything else (HLS, FLV, HEVC…) → ffmpeg remux to `WatchPlayer`; TV → `/web/stream`.
- Cookies (admin, Settings → Websites): uploaded per site, mode 600, never returned or logged; the page shows only present / count / import time.

## 4. Pages and the TV

- Top bar **Websites** (`data-route="web"`):
  - a paste box (a video → card with *Play on this computer* / *Send to the small TV*; a list → its videos with *Subscribe*, then *Add to the TV*);
  - search (site picker: *My subscriptions* searches fetched titles, or a searchable site; infinite scroll);
  - *My subscriptions* (site tag, last update, Refresh, Add to / remove from the TV, Unsubscribe).
- Home rows after the YouTube rows, can be switched off; a *Websites* section in aggregate search.
- Settings → Websites (admin): per-site cookies state and proxy, import, delete, Test (resolves a given URL with that site's cookies).
- TV firmware unchanged: website channels are ordinary up / series channels streamed from `/web/stream`.

## 5. Errors and testing

| Case | What the page says |
|---|---|
| Sign-in needed / cookies expired | This site needs a sign-in: import cookies in Settings → Websites |
| DRM | This video is DRM-protected and cannot be played (no retry, no workaround) |
| Unsupported or `generic` only | This address is not supported |
| Region lock, members only | Said as it is |
| Extractor broken | yt-dlp auto-update is triggered, "the parser is updating, try again later" |
| A subscription refresh fails | The last list is kept; the row says the last update failed |

Tests: node URL checks, host → settings, arguments; relay with a `FakeWeb` client (channels, rows, player choice, error classes); `tests/console_web_test.mjs` for the page's pure functions; a live pass in the owner's Chrome test tab (muted) with public free videos (CCTV, Dailymotion, Bilibili). Real-TV playback is left to the owner.

## 6. Delivery

| Step | Content |
|---|---|
| A1 | Node `/web/*`; paste a URL, play on the PC, send to the TV; Settings → Websites cookies |
| A2 | Subscriptions → home rows, TV channels, background refresh |
| A3 | Per-site search, *My subscriptions* search, the aggregate-search section |
| B | Tencent Video, Youku and others: separate design after a DRM test |
