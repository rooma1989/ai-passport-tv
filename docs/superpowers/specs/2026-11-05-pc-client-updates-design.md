<p align="right">
  <a href="2026-11-05-pc-client-updates-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# PC client updates — design

Status: approved by the owner section by section, 2026-11-05.

## 1. Goal and decisions

When the owner publishes a new version, every installed PC client (RooTV) learns about it, downloads it in the background, and installs it when someone clicks **Update and restart**.

- Scope: the PC client only. TV firmware is out of scope.
- Sources: a mirror on Qiniu Kodo first, the fork's GitHub Releases second. Both are untrusted transport: only a manifest signed with the owner's ed25519 key is accepted.
- Behaviour: check daily, download in the background, verify, then show "version x.y.z is ready"; one click installs and restarts. Before restarting, the client warns if someone is watching.
- Approach: the core checks and downloads (`server/selfhost/updater.py`); the tray installs (it already owns the lifecycle) through a small swap script.

## 2. The manifest and its signature

`rootv-update.json`, one per release:

```json
{"version": "0.3.9-beta", "date": "2026-11-10", "notes": "…the release's Chinese changelog entries…",
 "files": {"macos-arm64": {"name": "RooTV-0.3.9-beta-macos-arm64.dmg", "size": 123456789, "sha256": "…"},
           "macos-x86_64": {…}, "windows-x64": {…installer…}, "windows-x64-portable": {…zip…},
           "linux-x86_64": {…}}}
```

- `rootv-update.json.sig` beside it: an ed25519 signature (the project's `server/ed25519.py`) made with a dedicated update-signing key at `~/.config/xiaozhi-tv/update-signing.key` (0600), separate from the channel-pack key. The public key is built into the client.
- Accepted only when the signature checks, the version is newer than the running one (`0.4.0` is newer than `0.4.0-beta`) and the date is not older than the last manifest accepted (no downgrade by replaying an old manifest).
- Lite builds take only `<platform>-lite` entries.
- Addresses: Qiniu `https://<qiniu domain>/rootv/latest/rootv-update.json` (files under `/rootv/<version>/`), then GitHub `https://github.com/rooma1989/ai-passport-tv/releases/latest/download/rootv-update.json`. The Qiniu domain is a constant filled in at build time; empty means GitHub only.

## 3. Checking and downloading (core, `server/selfhost/updater.py`)

- First check 10 minutes after start, then every 24 hours; **Check now** in Settings.
- GitHub goes through the YouTube proxy from Settings → YouTube when one is set; Qiniu goes direct.
- Only this platform's file, into `<data>/updates/<version>/`, hashed while downloading, resumable; a wrong SHA-256 deletes and retries (3 times); not started when the disk is short.
- States `latest`, `checking`, `downloading` (with progress), `ready`, `failed` (with a reason), served to the tray (control API `/local/v1/update`) and to the admin (`GET /api/update`, `POST /api/update/check`, `POST /api/update/install`). The console shows a bar for the admin only: "Version x.y.z is ready · what's new · Update and restart".
- Not in a source checkout (`tools/dev_desktop.sh`) and not in Docker self-hosting.

## 4. Installing and rolling back (tray)

1. Watching check: the control API's `watching` and the TVs' state; if someone is watching, ask first (in the console when clicked there).
2. The tray hashes the downloaded file again against the signed manifest.
3. It copies a swap script into `updates/`, starts it detached, stops the core and exits.
   - **macOS:** wait for the old processes → mount the DMG → `ditto` the new RooTV.app beside the old one → rename the old one `.old` → move the new one in → detach → open it. The `.old` copy is deleted once the new app's control API answers; if it does not within 60 s, swap back, open the old app, and the tray says the update failed. If the app's folder is not writable, open the DMG instead.
   - **Windows, installed:** run the installer with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART` (UAC asks once: it installs under Program Files); Inno Setup rolls back on failure and starts the new version.
   - **Windows, portable** (no uninstaller beside the program) and **Linux:** unpack to a temporary folder, rename-swap, start; swap back on failure. Not writable: show where the download is.
4. The new version's first start clears older downloads and logs "updated from x to y".
5. Data, settings and the database live outside the program and are not touched (the database is backed up at start already).

## 5. Publishing (the owner's machine)

- Once: `python3 tools/publish_update.py keygen` makes the update-signing key and prints the public key, which goes into the code. Back it up.
- Each release, after the CI build: `python3 tools/publish_update.py v0.3.9-beta`:
  download `SHA256SUMS-desktop` and the installers from the release and check them; take the notes from `docs/CHANGELOG.zh_CN.md`; write and sign the manifest; upload manifest and signature to the GitHub release (`gh`); upload installers, then signature and manifest last, to Qiniu (`rootv/<version>/`, `rootv/latest/`), with keys from `~/.config/xiaozhi-tv/qiniu.json` (stdlib upload API, no SDK).
- `--dry-run` builds and checks without uploading; `--skip-qiniu` publishes to GitHub only. No staged rollout.

## 6. Testing

- Unit: version order (beta, lite), signature (one changed byte, another key), no downgrade, source fallback, resume, SHA-256 retry, state changes, no checks in a checkout, the publish command against a fake release and a fake Qiniu, the swap scripts' arguments.
- macOS live: a fake "old" app in a scratch folder, a locally made "new" DMG, a local test server as the source — download, update, swap, restart and roll back. The owner's installed app is not touched.
- Not run: the Windows and Linux swap scripts (written from documentation, checked statically — said so in the docs); the real Qiniu upload waits for the owner's Qiniu settings and the next release.
