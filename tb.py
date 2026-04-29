#!/usr/bin/env python3
"""tb — a tmux CLI for humans and LLMs.

Read, write, and manage tmux sessions with predictable output, stable exit
codes, and an optional ``--json`` envelope on every subcommand.

Exit codes:
    0  success
    1  generic error
    2  usage error            (EUSAGE)
    3  session not found      (ENOENT)
    4  session already exists (EEXIST)
    5  timed out              (ETIMEDOUT)
    6  tmux server not running(ENOSERVER)
    7  tmux command failed    (ETMUX)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from lib import __version__, output, sessions
from lib.errors import NoTmuxServer, TBError
from lib.tb_cmds import register_all


VERSION = __version__


def _verbs_needing_server(verb: str) -> bool:
    """Return True when the verb should short-circuit if no tmux server exists.

    ``snapshot``, ``ls`` and ``exists`` are deliberately allowed through —
    they have meaningful zero-session behaviour (empty list / exit 3) and
    humans + agents rely on it.
    """
    skip = {"snapshot", "ls", "exists", "new", "agent", "config"}  # `new` bootstraps a server
    return verb not in skip


def _build_common_parent() -> argparse.ArgumentParser:
    """Flags shared by every subcommand so they work in any position."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        help="emit JSON: {ok, data} on success, "
                             "{ok:false, error, code, exit} on failure")
    common.add_argument("--quiet", "-q", action="store_true",
                        help="suppress non-error output on success")
    common.add_argument("--no-header", action="store_true",
                        help="for table-output verbs, skip the header row")
    return common


def main(argv: list[str] | None = None) -> int:
    common = _build_common_parent()
    p = argparse.ArgumentParser(
        prog="tb",
        description="tmux CLI for humans and LLMs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    p.add_argument("--version", action="version", version=f"tb {VERSION}")
    sub = p.add_subparsers(dest="_verb", required=True, metavar="<verb>")
    register_all(sub, common)

    args = p.parse_args(argv)

    try:
        if _verbs_needing_server(args._verb) and not sessions.server_running():
            raise NoTmuxServer("no tmux server is running")
        return args.func(args) or 0
    except TBError as e:
        if args.json:
            output.emit_error_json(e)
        else:
            sys.stderr.write(f"tb: {e.message}\n")
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
