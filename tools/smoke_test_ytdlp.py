#!/usr/bin/env python3
"""Check that the core runs yt-dlp's zipapp: `rootv-core __ytdlp` (tools/ytdlp_run.py).

    python3 tools/smoke_test_ytdlp.py --core build-core/dist/rootv-core/rootv-core [--proxy URL]
    python3 tools/smoke_test_ytdlp.py --source

Downloads the pinned zipapp (server/selfhost/ytcomponents.py: version and SHA-256), then, with
the built core (or this checkout's tools/core_entry.py):

1. `__ytdlp <zipapp> --version` prints the pinned version;
2. `__ytdlp --check <zipapp>`: every standard-library module yt-dlp's source imports is in the
   build, yt-dlp imports, its extractors load, the JavaScript solver it ships is readable;
3. unpacks it the way the installer does and `__ytdlp --compile`s the folder; --version and
   --check again from the folder;
4. `--help` exits 0; an unknown option exits 2 (yt-dlp's own exit code comes through);
5. an offline extraction: a local HTML page with a <video> through a file:// URL gives the JSON
   yt-dlp would print for a real page (no YouTube, no network beyond the download).

Each step's time is printed; the summary goes into a GitHub Actions ::notice:: line.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.console import use_utf8  # noqa: E402

use_utf8()

from server.selfhost import ytcomponents  # noqa: E402

PAGE = "<html><head><title>Offline clip</title></head><body><video src=\"{src}\"></video></body></html>"


class Failure(Exception):
    pass


def run(command: list[str], *args: str, expect: int = 0, timeout: float = 180) -> tuple[str, float]:
    started = time.monotonic()
    done = subprocess.run(command + list(args), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    seconds = time.monotonic() - started
    if done.returncode != expect:
        raise Failure(f"`{' '.join(args[:3])} …` exited {done.returncode} (wanted {expect}): "
                      f"{(done.stdout + done.stderr)[-800:]}")
    return done.stdout, seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--core", type=Path, help="the built rootv-core executable")
    where.add_argument("--source", action="store_true", help="this checkout's tools/core_entry.py")
    parser.add_argument("--proxy", default="", help="download the zipapp through this proxy")
    args = parser.parse_args(argv)
    core = [str(args.core.resolve())] if args.core else [sys.executable, str(ROOT / "tools" / "core_entry.py")]
    command = core + ["__ytdlp"]
    version = ytcomponents.YTDLP_VERSION
    times: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="ytdlp smoke 测试 ") as tmp:
            folder = Path(tmp)
            pyz = folder / f"yt-dlp-{version}.pyz"
            url = ytcomponents.release_url(ytcomponents.YTDLP_REPO, version, ytcomponents.YTDLP_ASSET)
            ytcomponents.download(url, pyz, ytcomponents.YTDLP_SHA256, args.proxy)
            print(f"[ytdlp] downloaded {url} ({pyz.stat().st_size} bytes, SHA-256 checked)", flush=True)

            out, seconds = run(command, str(pyz), "--version")
            if out.strip() != version:
                raise Failure(f"--version printed {out.strip()!r}, wanted {version}")
            times.append(f"zipapp --version {seconds:.2f} s")
            out, _ = run(command, "--check", str(pyz))
            report = json.loads(out)
            print(f"[ytdlp] check (zipapp): {report}", flush=True)

            unpacked = folder / f"yt-dlp-{version}"
            ytcomponents.unpack_zipapp(pyz, unpacked)
            _, seconds = run(core, "__ytdlp", "--compile", str(unpacked))
            times.append(f"compile {seconds:.1f} s")
            if not list((unpacked / "yt_dlp" / "__pycache__").glob("*.pyc")):
                raise Failure("--compile wrote no bytecode")
            out, seconds = run(command, str(unpacked), "--version")
            if out.strip() != version:
                raise Failure(f"--version from the folder printed {out.strip()!r}")
            times.append(f"folder --version {seconds:.2f} s")
            out, _ = run(command, "--check", str(unpacked))
            print(f"[ytdlp] check (folder): {json.loads(out)}", flush=True)

            out, _ = run(command, str(unpacked), "--help")
            if "Usage:" not in out:
                raise Failure("--help printed no usage")
            run(command, str(unpacked), "--no-such-option", expect=2)

            clip = folder / "clip.mp4"
            clip.write_bytes(b"\0" * 1024)
            page = folder / "page.html"
            page.write_text(PAGE.format(src=clip.as_uri()), encoding="utf-8")
            for target, label in ((unpacked, "folder"), (pyz, "zipapp")):
                out, seconds = run(command, str(target), "--ignore-config", "--no-warnings", "--enable-file-urls",
                                   "-J", page.as_uri())
                info = json.loads(out)
                if not str(info.get("title", "")).startswith("Offline clip") or not info.get("formats"):
                    raise Failure(f"offline extraction gave {str(info)[:300]}")
                times.append(f"offline extraction ({label}) {seconds:.2f} s")
    except (Failure, ytcomponents.ComponentError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"::error::yt-dlp smoke test: {str(error).splitlines()[0] if str(error) else type(error).__name__}",
              flush=True)
        print(str(error), file=sys.stderr)
        return 1
    print(f"::notice::yt-dlp {version} through the core on {platform.system()} {platform.machine()}: "
          f"{'; '.join(times)}", flush=True)
    print("[ytdlp] all checks passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
