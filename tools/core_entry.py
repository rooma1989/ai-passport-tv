"""The entry point of the PC client's core bundle (packaging/desktop/rootv-core.spec).

The core is one executable, `rootv-core`, that becomes the self-host supervisor or
one of the children it starts, by its first argument (tools/subcommands.py, CORE_COMMANDS;
the dispatch itself is tools/packaged_entry.py, shared with the single-user build).

    rootv-core                       = rootv-core selfhost --desktop
    rootv-core --data D              = rootv-core selfhost --desktop --data D
    rootv-core selfhost --desktop …  what the tray runs (docs/superpowers/specs/
                                          2026-10-19-pc-client-architecture-design.md, 5.13)
    rootv-core __relay …             a child, started by the supervisor
    rootv-core __certs               the build check's certificate question

Anything else is refused rather than run as the single-user server: the core bundle is
not that program, and a mistyped word should not start one.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CODE_ROOT = Path(__file__).resolve().parents[1]
if str(_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT))

from tools.console import use_utf8  # noqa: E402

# Before anything is printed: on Windows the console's code page cannot hold Chinese (tools/console.py).
use_utf8()

from tools import packaged_entry  # noqa: E402
from tools.subcommands import CERTS_COMMAND, CORE_COMMANDS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0].startswith("-"):
        arguments = ["selfhost", "--desktop"] + arguments
    if arguments[0] not in CORE_COMMANDS and arguments[0] != CERTS_COMMAND:
        print(f"rootv-core：不认识「{arguments[0]}」。用法：rootv-core selfhost --desktop [--data <文件夹>]",
              file=sys.stderr)
        return 2
    return packaged_entry.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
