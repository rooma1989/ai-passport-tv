<p align="right">
  <a href="developer.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# PC client: tray and installers (developer notes)

Design: [`2026-10-19-pc-client-architecture-design.md`](../superpowers/specs/2026-10-19-pc-client-architecture-design.md),
sections 5.1-5.4 and 5.10-5.13, phase P2 (P2a tray, P2b sleep/autostart/network,
P2c installers). User tutorial: [`README.md`](README.md).

## Code map

| File | What |
| --- | --- |
| `desktop/tray.py` | Entry point (`XiaozhiTV`, `python3 -m desktop`); `App`: starts and watches the core, reads the status every 2 s, first run, every menu action; `PystrayUI` (the real tray), `HeadlessUI` (`--no-tray`); `--smoke-test`, `--tray-check` |
| `desktop/menu.py` | The menu as pure data: headline, problems, icon state, items. Tested without a GUI |
| `desktop/control.py` | Client for the local control API (the P1c contract) and `Status.from_json`: a contract change is a change here only |
| `desktop/core.py` | Starts, restarts and stops the core: a never-written stdin pipe plus `TV_WATCH_PARENT=1`; on Windows a job object (`tools/parentwatch.attach`) and `CREATE_NO_WINDOW`; exit code 3 (another core holds the data folder) is not a crash |
| `desktop/instance.py` | Single instance: an OS file lock on `run/tray.lock`; a second launch opens the console through the control API and exits |
| `desktop/power.py` | Idle-sleep prevention: Windows `SetThreadExecutionState` (one dedicated thread), macOS `IOPMAssertion` (ctypes, `caffeinate -i -w` fallback), Linux `systemd-inhibit`; the `KeepAwake` policy (held while a device watches, released 2 minutes after the last leaves) |
| `desktop/autostart.py` | Start at login: LaunchAgent, HKCU Run (respects Task Manager's switch), XDG autostart |
| `desktop/netcheck.py` | Windows network marked Public; router identity (hashed gateway MAC) and the trusted-network decision (for the core) |
| `desktop/icons.py`, `logs.py`, `diagnostics.py`, `paths.py` | Tray icons (drawn with Pillow), `tray.log`, the tray's own diagnostics zip when the core is down, per-OS paths |
| `desktop/devcore.py` | Stand-in core: only the control API with made-up state, speaking the P1c contract. For the tests and `--stub-core` builds; **never for users** |
| `packaging/desktop/` | `tray.spec`, `stub-core.spec`, `build.py`, `smoke.py`, `xiaozhi-tv.iss` (Inno Setup), `NOTICE.txt`, the Linux `.desktop` file, systemd unit and notes |
| `.github/workflows/build-desktop.yml` | Builds and smoke tests on four platforms; pushes to `desktop-*` branches or on demand |
| `tests/test_desktop_tray.py` | Menu state, status parsing, the control client (against the stand-in core), single instance, autostart files, sleep assertions (mocked), network parsing, the core process, `App` actions |

The tray never imports `server`; the core is another program and the control
API is the only thing between them, so option B (a Tauri shell) replaces only
the tray.

## Running from a checkout

```bash
python3 -m venv build-venv && build-venv/bin/pip install pystray pillow pyinstaller
build-venv/bin/python -m desktop --dev-core                 # stand-in core + real tray
build-venv/bin/python -m desktop --dev-core --no-tray       # no tray
build-venv/bin/python -m desktop --dev-core --tray-check 3  # shows the icon for 3 s, then quits
python3 -m desktop --dev-core --smoke-test --data /tmp/x    # the CI check (no pystray needed)
python3 -m unittest tests.test_desktop_tray
```

Without `--dev-core`, a checkout runs the real core
`python3 -m server.selfhost --desktop --data <folder>` (branch `desktop-core`).
A checkout never registers a login item (only an installed copy does), and
`--smoke-test` and `--tray-check` take no sleep assertion.

## Building

```bash
python3 packaging/desktop/build.py --stub-core                          # CI: stand-in core
python3 packaging/desktop/build.py --core-dir <xiaozhi-tv-core onedir>  # the real core
python3 packaging/desktop/smoke.py [--tray-check]
```

Output in `build-desktop/`: on macOS `Xiaozhi TV.app` and
`XiaozhiTV-<version>-macos-<arm64|x86_64>.dmg`; on Windows the portable zip and
`stage/Xiaozhi TV/` for Inno Setup; on Linux
`xiaozhi-tv-<version>-linux-x86_64.tar.gz`; and `SHA256SUMS`. `--release`
refuses the stand-in core.

Layout: the tray and the core are two PyInstaller onedirs. On macOS the core
goes in `Contents/Resources/core/` (`Contents/MacOS` must hold only code, and a
onedir carries data files); each Mach-O in it is ad-hoc signed first, then the
app. On Windows and Linux the core is `core/` beside the tray. The cost is a
second Python runtime (about 15 MB); PyInstaller's multipackage build can merge
them later.

## The contract with the core (P1c)

What the tray uses: `control.json` (`port`, `token`), `Authorization: Bearer`,
`GET /status` (`state`, `problems`, `lan_url`, `local_url`, `ports.lan`,
`setup_pending`, `devices`, `watching`, `ffmpeg`, `settings`, `logs_dir`),
`console-link` (answers with the setup page on the first run), `phone-qr`
(`url` + `svg`; the tray writes a local page with the QR code), `power`,
`autostart`, `ffmpeg/fetch`, `diagnostics`, `restart`, `stop`; exit code 3.

Decided on the tray's side:

- **The tray takes the sleep assertion** (the contract leaves it to P2b). If
  the core takes it itself later, it reports `"power": {"managed_by": "core"}`
  and the tray releases its own. The status has no "phone listening" count
  (`listening`) yet: the design also keeps the computer awake while a phone
  listens, which needs that field from the core.
- **The tray registers the login item**; `POST /autostart` only lets the core
  record it in `desktop.json`. It is registered once on the first run; after
  that the OS entry is the truth (a switch-off in Task Manager or Login Items
  is not undone).
- **Trusted networks (5.11) are not in the contract yet.** The tray already
  reads a proposed `status.network` (`{"id", "trusted", "profile",
  "lan_open"}`) and calls a proposed `POST /local/v1/network/trust {"id",
  "trusted"}`; `desktop/netcheck.py` has the router identity and
  `lan_decision()` ready for the core. The enforcement (binding
  8080/8097/8098 to 127.0.0.1 away from home) can only be in the core.
- **Windows network marked Public**: the tray checks it itself (PowerShell
  `Get-NetConnectionProfile`, once a minute) and explains it in the menu.

## CI

`build-desktop.yml`: macos-14 (arm64), macos-15-intel, windows-latest,
ubuntu-22.04. On each: the tray's unit tests, the latest released firmware for
the core's flasher page (best effort; no release has the bundle yet), the core
(`tools/build_core.py`, merged from `desktop-core`), the client build, the
smoke test (a data folder with Chinese characters and a space), the tray icon
check (allowed to fail; skipped on Linux); on Windows also compiling the
installer, a silent install, the firewall rule (Private only, for the core
program), a smoke test of the installed copy, and uninstalling with a check
that the rule and the files are gone. Failures are `::error::` annotations
(logs need a login, annotations do not). The artifacts are unsigned test
builds named `desktop-<platform>`, kept 7 days.

## Still to do

- Release packaging: a tag-triggered job that publishes the installers with
  `SHA256SUMS` (and later its Ed25519 signature, P8) and fills the core's
  firmware from the same release.
- Build the PyInstaller bootloader from source in CI (fewer antivirus false
  positives, 5.12); a Chinese installer wizard (a `ChineseSimplified.isl` must
  be shipped when Inno Setup has none); version information in the Windows
  executables.
- Real machines: the tray and menu on Windows 10/11, SmartScreen, Defender; an
  Intel Mac; the macOS 15 Local Network prompt.
