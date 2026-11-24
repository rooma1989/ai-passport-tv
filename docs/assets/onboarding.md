<p align="right">
  <a href="onboarding.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# RooTV Onboarding (fork-only)

> For a teammate who develops this fork with Claude Code. Written 2026-11-24, at v0.3.15-beta.
> [`AGENTS.md`](../../AGENTS.md) at the repository root is the only rule set. This page says where to start and lists pitfalls already hit; where the two disagree, `AGENTS.md` wins.

## 1. What the project is

RooTV (formerly Xiaozhi TV) turns a small ESP32 device into a household mini TV:

| Part | Job | Code |
| --- | --- | --- |
| TV firmware | Shows the picture, plays sound, buttons switch channels | `main/` (C, ESP-IDF 5.5.3) |
| PC client | Runs on an always-on computer: fetches content, transcodes, streams it to the TV over Wi-Fi; tray icon plus a browser console | `server/` (Python, standard library only), `desktop/` (tray) |
| Phone remote / PC console | Browser pages: channels, Bilibili, YouTube, cloud-drive library, casting, admin | `server/relay/assets/` (HTML/JS/CSS) |

Two devices are supported: **Xingzhi Cube 1.54** (ESP32-S3) and **FoloToy AI Passport** (ESP32-C3).

Video stays on the home network. The project ships no channels, stream addresses, accounts or cookies.

## 2. Repositories

- This fork: `git@github.com:rooma1989/ai-passport-tv.git` (remote `fork`, branch `main`).
- Upstream: `https://github.com/zpdeng1020-coder/ai-passport-tv` (remote `origin`).
- On the owner's machine other folders sit next to this checkout (experiments, backups, side projects). Day-to-day work happens only in this repository.

## 3. What to have Claude read first

1. [`CLAUDE.md`](../../CLAUDE.md) → [`AGENTS.md`](../../AGENTS.md): build commands, commit rules, safety baseline.
2. [`architecture-index.md`](architecture-index.md): which file owns which concern. Read it before a new feature instead of reading the whole tree.
3. The task's own documents from the routing table in `AGENTS.md` (firmware: `docs/development/ai-guide.md`; release: `docs/development/ci/`; commits: `docs/contribution/commit-and-pr.md`).

Do not have Claude read the large files whole; grep them: `main/av_player.c` (about 3600 lines), `server/relay/library.py` (the library, 4000+ lines), `server/relay/assets/console.js`.

## 4. Layout

```
main/                 firmware (av_player.c is the whole playback session; av_protocol.h is the wire format)
components/bsp/       board support (pins, display, audio)
server/
  tv_server.py        media server that streams to the TV
  live.py, hlsopen.py live channels
  relay/              console backend: Bilibili, YouTube, library, cloud drives, admin API...
    library.py        My library: scanning, title recognition, playback
    clouddrive.py     cloud-drive component (OpenList): Quark, Baidu, UC
    admin.py          /api/admin/* endpoints
    assets/           console and phone front end
  selfhost/           the PC client's core (version, updates)
desktop/              tray program, the tray's half of updates
packaging/desktop/    builds the macOS / Windows / Linux installers
tools/                build, validation and release scripts
tests/                host tests (Python unittest, some .mjs and C tests)
docs/                 documentation; every page is an English .md plus a Chinese .zh_CN.md
```

## 5. Everyday commands

```bash
# Python tests only (fast; for server/ changes)
python3 -m unittest discover -s tests -p 'test_*.py'

# One file / one test
python3 -m unittest tests.test_relay_library
python3 -m unittest tests.test_relay_library -k change_during

# Repository checks + host tests (run before every commit; CI's Static checks runs this)
./tools/validate.sh --static

# Firmware build + image verification (needs an activated ESP-IDF 5.5.3)
./tools/validate.sh --firmware

# Complete gate
./tools/validate.sh
```

The owner's Mac has no `pytest` (use `python3 -m unittest`) and no `timeout` command.

### Trying a server change in the real app

No need to wait for a package: run the PC client's core from source against the installed RooTV.app's real data.

```bash
tools/dev_desktop.sh start     # quit RooTV.app, back up the database, start the core from source
tools/dev_desktop.sh restart   # after changing code
tools/dev_desktop.sh log       # follow the log
tools/dev_desktop.sh stop      # stop it, then open RooTV.app to return to the packaged build
```

## 6. Runtime data and logs (macOS, for debugging)

| What | Where |
| --- | --- |
| Data folder | `~/Library/Application Support/RooTV/` |
| Library sources / index | `library.json`, `library-index.json` in that folder |
| Database | `tv.db` (SQLite) |
| Logs | `~/Library/Logs/RooTV/core.log` (`core-dev.log` when run from source). The data folder's `logs/` is an old location |
| OpenList cloud-drive component | `127.0.0.1:47444`, WebDAV at `/dav`; its admin password is in `secrets/openlist-admin` |
| Console | `http://localhost:8080` |

Never commit anything from `secrets/`, paste it into a chat, or send it to an outside service.

## 7. Commits and releases

**Commits** (details in [`commit-and-pr.md`](../contribution/commit-and-pr.md)):
- English Conventional Commits: `fix(library): ...`, `feat(console): ...`, subject at most 72 characters.
- User-visible changes go into both `docs/CHANGELOG.md` and `docs/CHANGELOG.zh_CN.md` under "Unreleased".
- **No Chinese prose in English `.md` files**, not even an example file name, or CI's Static checks fail. Chinese goes into the `.zh_CN.md` peer.

**Release** (v0.3.15-beta as the example):
1. Commit `Release 0.3.15-beta`: `__version__` in `desktop/__init__.py`, `VERSION` in `server/selfhost/version.py` (`0.3.15-beta.dev`), and a `## v0.3.15-beta` heading under "Unreleased" in both changelogs.
2. Tag `v0.3.15-beta`; push `main` and the tag to `fork`.
3. The **Build desktop client** workflow builds the macOS (arm64 / x86_64), Windows and Linux installers and publishes the GitHub release (about 15 minutes).
4. `python3 tools/publish_update.py v0.3.15-beta` signs the update manifest and uploads to the Qiniu CDN so installed clients update themselves. The signing key and Qiniu settings live in `~/.config/xiaozhi-tv/` on the owner's machine, never in the repository.

**Pushing, tagging, publishing a release and uploading to the CDN are all publishing**; each needs the owner's agreement first. Commit locally; do not let Claude push on its own.

## 8. Pitfalls already hit (November 2026)

- **Baidu Netdisk will not log in**: on the OpenList token page (api.oplist.org) turn on "Use the parameters provided by OpenList" before fetching the refresh token. Without it the token belongs to another app key, RooTV cannot renew it, and the error says the token is wrong or expired.
- **A video just saved to a drive is missing from the library**: check `core.log` for the library scan's "N videos" line. Before v0.3.15 a drive added during a running scan was skipped; fixed with `Library.scan(again=True)`.
- **Title recognition** is `parse_name()` in `server/relay/library.py`. After changing its rules bump `PARSE_VERSION` so the next scan reparses every item, and compare old and new results over a real `library-index.json` first to catch titles that change by accident.
- **Network tests**: live sources and drive speeds depend heavily on the network. Results taken through an overseas proxy say little about users in mainland China; recheck on a domestic network.
- The macOS installer is unsigned: the first launch needs System Settings → Privacy & Security → Open Anyway.

## 9. Working agreements for Claude

- Reply to the owner in Simplified Chinese; code, commit messages and English documents stay in English.
- Start with `git status --short --branch`; never overwrite or clean unrelated changes.
- Write the test before the fix. On delivery report Build / Host tests / Device tests separately as PASS, FAIL or NOT RUN, and say plainly what was not checked on hardware.
