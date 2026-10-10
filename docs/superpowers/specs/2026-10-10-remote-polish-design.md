<p align="right">
  <a href="2026-10-10-remote-polish-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# YouTube upkeep and switching feedback — design

Status: approved 2026-10-10. Follows
[`2026-10-10-grouped-channels-and-remote-design.md`](2026-10-10-grouped-channels-and-remote-design.md).
Volume, brightness and playback controls from the phone come next, in their own
design.

## Goal

1. YouTube keeps working without anyone watching it: yt-dlp updates itself, and
   the phone page says when the cookies need replacing.
2. After a tap on a channel, the phone page shows that the device is switching
   instead of still showing the old channel as playing.

## YouTube upkeep

### Weekly yt-dlp update (cstone)

- `packaging/yt-relay/update-ytdlp.sh`, run by `yt-dlp-update.timer` every
  Monday at 04:00: download the latest release beside the current binary, run it
  as `ytrelay` with deno and the cookies on a fixed test video, and replace the
  binary only if that works. Otherwise keep the old one and log why.

### Health

- The node records the outcome of every YouTube resolve: `ok`, `login` (YouTube
  asked to sign in, meaning the cookies expired) or `error`. A background check
  resolves the test video every 6 hours without the cache, so expired cookies
  show even when nothing is being watched.
- `GET /yt/health` (key required) answers `{"state", "at", "version"}`, with
  `state` `unknown` before the first resolve.
- The relay asks at most once a minute and adds `"youtube": {"state"}` to
  `/api/state` when YouTube is configured; an unreachable node is `unreachable`.
- The page shows a vermilion bar above every tab for `login` ("YouTube needs new
  cookies", with the export and upload steps behind a tap) and for `unreachable`
  ("YouTube is unavailable for now").

## Switching feedback

- `now-playing.json` gains `since`: when the current session started. The relay
  returns it as `now.since`, and every switch, cast and replay answer carries
  `at`, the relay's clock at that moment.
- After a successful tap the page keeps a pending switch (target channel, `at`,
  deadline). Until the device reports the target channel with `since >= at - 1`,
  the target row reads "switching…" in amber, the channel display reads
  "switching to …", and the page refreshes every second instead of every five.
- The deadline is 20 s for live channels and 45 s for creators and casts
  (YouTube starts slowly). Past it the page says the switch seems to have failed
  and to tap again.
- A cast or a replay counts as a switch to `cast`; comparing `since` with `at`
  is what tells a new cast apart from the one already playing.

## Testing

- Host: node health transitions (ok, login, error, unknown) and the endpoint's
  key check; the relay's health cache and `unreachable`; `since` written and read;
  `at` in answers; the page's script against the API fields.
- Server: run the update script once by hand; check the timer is scheduled.
- Phone: tap a channel and see "switching…" until the device is there; a cast;
  the YouTube bar by pointing the node at a missing cookies file for a moment.
