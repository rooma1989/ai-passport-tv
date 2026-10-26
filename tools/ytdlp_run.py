"""Running yt-dlp with the core's own Python: `rootv-core __ytdlp <yt-dlp> [yt-dlp options …]`.

The local YouTube node used to run the single-file yt-dlp build (yt-dlp_macos, yt-dlp.exe,
yt-dlp_linux). Those are PyInstaller onefile programs: every run unpacks a whole Python into a
new temporary folder before doing anything, and on macOS each new folder of unsigned binaries is
assessed again by the system. Measured on a Mac behind a TUN proxy, `yt-dlp_macos --version`
took about 37 seconds every time, and a search 38.5 s, so the console's 25 s search timed out.

yt-dlp also publishes a pure-Python zipapp (the asset called just `yt-dlp`, 3 MB). The core
already carries a Python, so it runs that: the zipapp (or the folder it was unpacked into, which
keeps compiled bytecode between runs, see server/selfhost/ytcomponents.py) goes on sys.path and
`yt_dlp.main` runs in this process. Exit codes, standard output and standard error are yt-dlp's
own. From a checkout the node runs `python <zipapp>` instead (server/relay/ytnode.py,
ytdlp_command), and this module is not involved.

    rootv-core __ytdlp <zipapp|folder> …     yt-dlp itself
    rootv-core __ytdlp --check <zipapp|folder>
                                              the build check: imports every standard-library
                                              module yt-dlp's source imports, and reads the
                                              JavaScript solver yt-dlp ships as package data
    rootv-core __ytdlp --compile <folder>     compile an unpacked yt-dlp once, after installing

PyInstaller bundles only the standard library our own code imports, and yt-dlp imports more
(optparse, sqlite3, http.cookiejar, xml.etree, ctypes, …). STDLIB_PACKAGES and STDLIB_MODULES are
added to the core's hidden imports (packaging/desktop/rootv-core.spec); `--check` is how the CI
finds out when a new yt-dlp needs one more (tools/smoke_test_ytdlp.py).

Standard library only, and nothing of ours but tools/console.py: this is started for every yt-dlp
call, so it skips everything the core's other commands set up (no parent watch either -- the node
starts yt-dlp with no pipe on standard input, and its own timeout ends it).
"""

from __future__ import annotations

import os
import sys

COMMAND = "__ytdlp"

# Standard-library packages yt-dlp's source imports from (all their submodules go into the
# core: they are small, and a new yt-dlp that starts using another submodule should not need
# a new build), and single modules. Both measured from yt-dlp 2026.10.21's sources; --check
# measures again for whatever version it is given.
STDLIB_PACKAGES = ("asyncio", "concurrent", "ctypes", "email", "encodings", "html", "http", "importlib",
                   "json", "logging", "sqlite3", "urllib", "xml", "zipfile", "zoneinfo")
STDLIB_MODULES = (
    "__future__", "abc", "argparse", "ast", "atexit", "base64", "binascii", "bisect", "bz2", "calendar",
    "codecs", "collections", "compileall", "contextlib", "contextvars", "copy", "csv", "dataclasses",
    "datetime", "decimal", "difflib", "enum", "errno", "fileinput", "fnmatch", "fractions", "functools",
    "getpass", "gettext", "glob", "gzip", "hashlib", "heapq", "hmac", "inspect", "io", "ipaddress",
    "itertools", "locale", "lzma", "math", "mimetypes", "netrc", "numbers", "operator", "optparse",
    "os", "pathlib", "pkgutil", "platform", "pprint", "py_compile", "queue", "quopri", "random", "re",
    "secrets", "selectors", "shlex", "shutil", "signal", "socket", "ssl", "stat", "string", "struct",
    "subprocess", "sys", "sysconfig", "tarfile", "tempfile", "textwrap", "threading", "time", "token",
    "tokenize", "traceback", "types", "typing", "unicodedata", "uuid", "warnings", "weakref",
    "zipimport", "zlib",
)
# Only on one family of systems; --check skips the other family's.
WINDOWS_ONLY = ("msvcrt", "winreg", "_winapi", "nt", "winsound", "ctypes.wintypes")
POSIX_ONLY = ("fcntl", "pty", "termios", "tty", "grp", "pwd", "resource", "posix", "curses", "readline")


def _use(target: str) -> None:
    if not (os.path.isfile(target) or os.path.isfile(os.path.join(target, "yt_dlp", "__init__.py"))):
        print(f"yt-dlp：找不到 {target}", file=sys.stderr)
        raise SystemExit(2)
    sys.path.insert(0, os.path.abspath(target))
    # A frozen Python writes no bytecode by default. An unpacked yt-dlp is compiled once when it
    # is installed; anything that was not (a newer Python after an update of the app) is written
    # on first use rather than compiled again on every run.
    sys.dont_write_bytecode = False


def stdlib_imports(target: str) -> set[str]:
    """The standard-library modules imported anywhere in yt-dlp's source (zipapp or folder)."""
    import ast
    import zipfile

    def sources():
        if os.path.isfile(target):
            with zipfile.ZipFile(target) as archive:
                for name in archive.namelist():
                    if name.endswith(".py"):
                        yield name, archive.read(name)
            return
        for folder, _dirs, files in os.walk(target):
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(folder, name)
                    with open(path, "rb") as handle:
                        yield path, handle.read()

    found = set()
    for _name, source in sources():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in sys.stdlib_module_names:
                    found.add(name)
    return found


def check(target: str) -> int:
    """Import every standard-library module yt-dlp imports, yt-dlp itself and its JS solver."""
    import importlib
    import json

    _use(target)
    skip = POSIX_ONLY if os.name == "nt" else WINDOWS_ONLY
    missing = []
    wanted = sorted(stdlib_imports(target))
    for name in wanted:
        if name in skip or name.split(".")[0] in skip:
            continue
        try:
            importlib.import_module(name)
        except ImportError as error:
            missing.append(f"{name} ({error})")
    report = {"modules": len(wanted), "missing": missing}
    try:
        import yt_dlp
        from yt_dlp.version import __version__
        report["version"] = __version__
        import yt_dlp.options     # noqa: F401 —— the option parser (optparse) every run builds
        import yt_dlp.cookies     # noqa: F401 —— sqlite3 and the browsers' decryption
        from yt_dlp.extractor import gen_extractor_classes
        report["extractors"] = len(list(gen_extractor_classes()))
        del yt_dlp
    except Exception as error:      # noqa: BLE001 —— reported, not raised: this is a check
        missing.append(f"yt_dlp ({type(error).__name__}: {error})")
    try:
        import yt_dlp_ejs.yt.solver
        report["ejs"] = len(yt_dlp_ejs.yt.solver.core()) + len(yt_dlp_ejs.yt.solver.lib())
        if not report["ejs"]:
            missing.append("yt_dlp_ejs (empty solver)")
    except Exception as error:      # noqa: BLE001
        missing.append(f"yt_dlp_ejs ({type(error).__name__}: {error})")
    print(json.dumps(report, ensure_ascii=False))
    return 1 if missing else 0


def compile_folder(folder: str) -> int:
    """Compile an unpacked yt-dlp with this Python (the one that will run it)."""
    import compileall
    if not os.path.isfile(os.path.join(folder, "yt_dlp", "__init__.py")):
        print(f"yt-dlp：{folder} 不是解开的 yt-dlp", file=sys.stderr)
        return 2
    return 0 if compileall.compile_dir(folder, quiet=2, workers=1) else 1


def _no_console_windows() -> None:
    """Windows: what yt-dlp starts (deno, ffmpeg) gets CREATE_NO_WINDOW unless it asks otherwise.

    The node starts this process without a console, and a console program started by a process
    that has none opens a window of its own. yt-dlp hides its children's windows itself; this is
    the same net tools/packaged_entry.py puts under the core's other children.
    """
    if os.name != "nt":
        return
    import subprocess

    original = subprocess.Popen.__init__
    if getattr(original, "_no_window", False):
        return

    def init(self, *args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = 0x08000000
        original(self, *args, **kwargs)

    init._no_window = True      # type: ignore[attr-defined]
    subprocess.Popen.__init__ = init    # type: ignore[method-assign]


def main(argv: list[str]) -> int:
    # Both entry points have already done this; repeated so that this module's own Chinese
    # messages can never meet a console that cannot print them (tools/console.py).
    from tools.console import use_utf8
    use_utf8()
    _no_console_windows()
    if len(argv) >= 2 and argv[0] == "--check":
        return check(argv[1])
    if len(argv) >= 2 and argv[0] == "--compile":
        return compile_folder(argv[1])
    if not argv:
        print(f"用法：{COMMAND} <yt-dlp 的 zipapp 或文件夹> [yt-dlp 的参数…]", file=sys.stderr)
        return 2
    _use(argv[0])
    sys.argv = ["yt-dlp", *argv[1:]]
    import yt_dlp
    # yt_dlp.main ends with sys.exit(code): the exit code is yt-dlp's own.
    yt_dlp.main(argv[1:])
    return 0
