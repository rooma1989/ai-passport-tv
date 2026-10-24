"""The PC client's tray process (「小智电视」 in the notification area / menu bar).

Design: docs/superpowers/specs/2026-10-19-pc-client-architecture-design.md,
sections 5.1-5.4, 5.10-5.13 (phases P2a/P2b). The tray never touches the core's
files or children directly: it starts the core, then talks to it only through
the local control API (`<data>/run/control.json`, 127.0.0.1 + token), so the
Tauri shell of option B can replace this package without core changes.

Nothing in this package imports `server`; the core is a separate program.
"""

__version__ = "0.2.4-beta"

APP_NAME = "小智电视"
# File-system and registry names: ASCII, the same on every OS (section 5.5).
APP_ID = "XiaozhiTV"
# macOS bundle identifier and LaunchAgent label. Reverse-DNS under the project's
# GitHub organisation rather than a domain the project does not own.
BUNDLE_ID = "io.github.ai-passport-tv.XiaozhiTV"
