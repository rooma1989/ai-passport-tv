"""The PC client's tray process (「小鼠电视」 RooTV in the notification area / menu bar).

Design: docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md,
sections 5.1-5.4, 5.10-5.13 (phases P2a/P2b). The tray never touches the core's
files or children directly: it starts the core, then talks to it only through
the local control API (`<data>/run/control.json`, 127.0.0.1 + token), so the
Tauri shell of option B can replace this package without core changes.

Nothing in this package imports `server`; the core is a separate program.
"""

__version__ = "0.3.0-beta"

# Renamed from 小智电视 / Xiaozhi TV after 0.2.5-beta (spec 2026-10-24-media-center-ui-design,
# section 15). The old names stay below only to find and replace what the old
# version left behind: its data folder (tools/desktop_folders.py) and its login item.
APP_NAME = "小鼠电视"
APP_NAME_EN = "RooTV"
# File-system and registry names: ASCII, the same on every OS (section 5.5).
APP_ID = "RooTV"
# macOS bundle identifier and LaunchAgent label. Reverse-DNS under the project's
# GitHub organisation rather than a domain the project does not own.
BUNDLE_ID = "io.github.ai-passport-tv.RooTV"

LEGACY_APP_NAME = "小智电视"
LEGACY_APP_ID = "XiaozhiTV"
LEGACY_BUNDLE_ID = "io.github.ai-passport-tv.XiaozhiTV"
