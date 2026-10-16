<p align="right">
  <a href="CI-build-and-release.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Automated Build and Release

`.github/workflows/build-firmware.yml` builds and publishes firmware for tags and supports manual dispatch. Ordinary branch pushes do not trigger it. Keep this page synchronized with the workflow.

The workflow has three jobs:

- **build**, one per board (a matrix: `ai-passport` with `./tools/validate.sh --prototype` on ESP32-C3, `xingzhi-cube-154` with `./tools/validate.sh --xingzhi` on ESP32-S3), each in the ESP-IDF 5.5.3 container with its own ccache. Both are **public builds** (`AV_PUBLIC_BUILD=ON`): `validate.sh` verifies the merged image's layout (`tools/verify_firmware.py`: bootloader at `0x0`, partition table at `0x8000`, application at `0x10000`, 8 MB flash, the protected `cardid` region) and that it is a public build (`tools/firmware_manifest.py check-public`: `AV_PUBLIC_BUILD` reached the compiler, so no private header, token or help URL; no default server or site in the sdkconfig). The version is `git describe --tags` of a full checkout, passed as `AV_FW_VERSION` (→ `PROJECT_VER`); the device reports it in HELLO `fw`. `tools/firmware_manifest.py make` then re-reads the image (chip id, this project's application at `0x10000`, that version, the Passport's end before `0x356000`), copies it under its release name and writes `manifest-<board>.json`.
- **package** merges the two into `manifest.json`, writes `SHA256SUMS` (each image re-hashed against its manifest; builds of different versions are refused), checks the result the way the flasher will, and assembles `xiaozhi-tv-flasher.zip`: the web flasher (`packaging/flash/`) with `firmware/` beside it, ready to serve as `/flash/`. It runs for manual dispatches too, so the whole set can be inspected before tagging.
- **release** (tags only, `contents: write`, the release concurrency group shared with the server workflow) checks that every file is there and publishes them.

All Actions are pinned to full commit SHAs. Build and package jobs have `contents: read`.

## Release files

| File | What it is | Flash |
| --- | --- | --- |
| `FoloToy-AI-Passport-tv.bin` | AI Passport (ESP32-C3), merged image | write at `0x0`, **never erase** (factory identity at `0x356000`) |
| `Xingzhi-Cube-154-tv-public.bin` | Xingzhi Cube 1.54 (ESP32-S3), merged image | erase once when replacing the Xiaozhi AI firmware, then write at `0x0` |
| `manifest.json` | both boards: board, chip, version, offset, size, SHA-256, erase rule, write limit, protected ranges | read by the web flasher |
| `manifest-<board>.json` | the same for one board | |
| `SHA256SUMS` | checksums of the images and manifests | the flasher checks it too |
| `xiaozhi-tv-flasher.zip` | `flash/`: page, esptool-js, `firmware/` | unzip under any HTTPS site |

## Browser flashing

The web flasher is `packaging/flash/` (static HTML and JavaScript, esptool-js 0.6.1 vendored with its licence). It fetches `firmware/manifest.json`, `firmware/SHA256SUMS` and the images from its own origin: GitHub's release downloads send no CORS header, so a page cannot fetch them from the release (see `docs/flash/index.html`). Serve it with the firmware beside it:

- **Static site** (`<static-site>/flash/`, HTTPS, needed for Web Serial): unzip `xiaozhi-tv-flasher.zip` at the site root, or run `python3 tools/firmware_manifest.py bundle <folder with the release files> --out <site>/flash`.
- **Household server**: serve the same folder at `/flash/`; Web Serial works on `http://localhost:8080/flash/` on the machine the device is plugged into. The self-hosting package ships the page from `packaging/flash/` and the release's firmware files in its `firmware/`, and sets `TV_FIRMWARE_MANIFEST` (that `manifest.json`) and `TV_FLASHER_URL=/flash/` for the relay so the admin Devices page can say which devices run an older version.
- **Manual**: `esptool.py --chip esp32c3 write_flash 0x0 FoloToy-AI-Passport-tv.bin` (never `erase_flash` on a Passport); for the Xingzhi see [its board document](../../boards/xingzhi-cube-154.md#flash).

The older single-board page `docs/flash/index.html` (GitHub Pages, Passport only, firmware committed under `docs/firmware/`) is unchanged.

## Release title

When this repository publishes firmware for several different applications from
the same source tree, a bare version number does not tell a user which app a
release is for. Give each tag a name that carries the version and the app, and
make sure the release title shows both.

- **Tag naming convention**: name tags as `v<version>-<app-name>` in
  lowercase-kebab-case, e.g. `v0.1.0-voice-keychain`, `v1.0.0-pocket-pomodoro`.
  The `<app-name>` is the application this release builds (see the
  `plays/<username>/<app-name>/` archive naming). A tag that only says a version
  is ambiguous when several apps share the tree.
- **After the release is published, confirm the release title.** The workflow
  sets the title to the tag name, so a correctly-named tag already reads
  `v0.1.0-voice-keychain`. If the tag did not include the app, or the title is
  not obvious at a glance, edit the release (GitHub: `Edit release`; GitLab:
  edit the tag) so the title is `<version> <app-name>`, e.g. `v0.1.0 Voice
  Keychain`. One quick scan of the release list should distinguish which app a
  release is for.
- **Keep title and tag consistent.** Use `<version>-<app-name>` so the app name
  is visible in both the tag list and the release list. Do not rely on a
  human-readable body alone to carry the app name.

## Release notes

A tag-triggered release succeeds only when the merged firmware and its release
notes travel together. After the release is published, write release notes that
explain the build to a user who may not have read the repository. Cover three
things:

- **What's new**: the features, behaviors, or fixes this release adds or
  changes compared with the previous one. Keep it user-facing, not a commit log.
- **How to build**: how to produce and verify the merged firmware
  (`./tools/validate.sh --firmware` or `idf.py build`), and the artifact file to
  flash (`FoloToy-AI-Passport-full.bin` flashed from `0x0`).
- **How to use**: how to flash the build (the browser flasher above) and the key
  interactions or hardware requirements of the release.

Write the release notes in English (and a Simplified Chinese version where the
project is bilingual) and link them from the GitHub/GitLab release. Keep them
consistent with `docs/CHANGELOG.md` for user-visible behavior.

## Building the server (`.github/workflows/build-server.yml`)

The server is a second, independently distributed program, built by its own workflow. This page is kept in step with that one as well.

- **Triggers**: tag pushes, pull requests touching `server/`, `tools/`, `packaging/`, `tests/` or `channels.txt`, and manual dispatch. Ordinary branch pushes do not trigger it.
- **Three-platform matrix**: Linux x86_64, macOS arm64 and Windows x86_64, each built on its own runner. PyInstaller cannot cross-build, so this is a matrix rather than one job with flags. `fail-fast: false`, because one platform failing is a fact about that platform and cancelling the others hides whether the same change broke them too.
- **Smoke test immediately after each build**: the artifact is started on the spot and checked for four things -- `--help` returns successfully, starting in an empty directory brings up both services, the media server's port accepts a connection, and a channel table appears beside the program.
  This step is not optional. Every defect found in this feature was of the form "builds cleanly, dies on start": PyInstaller ships what its static analysis can see, so a module reached only by name at run time is silently left out, and a pipeline that reported only "build succeeded" would have published every one of them.
- **Artifacts**: `build-server/tv-server-<system>-<arch>[.exe]`, published to the release for a tag and not committed, like the firmware.
- **A shared release concurrency group with the firmware workflow**: a release is updated by reading it and writing it back, so two workflows writing the same tag's release at once lose files. Both use the same `concurrency` group to queue, and `cancel-in-progress` stays `false` -- cancelling a release job halfway is how a published release ends up missing an asset.

## Related documents

- Firmware publishing to the community: [publish-to-community.md](../release/publish-to-community.md)
- Post-release follow-up: [project-completion.md](../release/project-completion.md)
