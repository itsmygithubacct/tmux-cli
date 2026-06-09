#!/usr/bin/env python3
"""tb — a tmux CLI for humans and LLMs.

Read, write, and manage tmux sessions with predictable output, stable exit
codes, and an optional ``--json`` envelope on every subcommand.

Exit codes:
    0  success
    1  generic / unexpected error (EUNKNOWN)
    2  usage error               (EUSAGE)
    3  session not found         (ENOENT)
    4  session already exists    (EEXIST)
    5  timed out                 (ETIMEDOUT)
    6  tmux server not running   (ENOSERVER)
    7  tmux command failed       (ETMUX)
    8  state corrupt/unwritable  (ESTATE)
    9  dashboard auth failed     (EAUTH)
  130  interrupted (SIGINT / Ctrl-C)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.append(str(_SCRIPT_DIR))

from lib import __version__, output, sessions
from lib.errors import NoTmuxServer, TBError, UsageError
from lib.tb_cmds import register_all


VERSION = __version__

# The global flags live on one parent parser that is a parent of *both* the
# top-level parser and every subparser, so a value can be supplied before or
# after the verb. argparse re-parses subcommands into a fresh namespace and
# copies every attribute back, so an ordinary ``default`` on the subparser
# copy would clobber a value parsed *before* the verb back to its default.
# ``SUPPRESS`` makes an unsupplied flag simply absent from each namespace (so
# it never overwrites the other position); we then fill the missing defaults
# once after parsing. Net effect: --json/--quiet/--no-header work in *both*
# positions.
_GLOBAL_FLAG_DEFAULTS = {"json": False, "quiet": False, "no_header": False}


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose ``error()`` raises instead of calling ``sys.exit``.

    This routes argparse usage errors (bad/missing verb, unknown flag, missing
    positional) through the same ``TBError`` -> exit-code/JSON machinery as
    every other failure, so even ``tb --json bogus`` yields a structured
    ``{ok:false,...}`` envelope. ``add_subparsers`` defaults ``parser_class``
    to ``type(self)``, so every subparser inherits this behaviour for free.
    """

    def error(self, message: str):
        raise UsageError(message)


def _build_common_parent() -> argparse.ArgumentParser:
    """Flags shared by every subcommand so they work in any position."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="emit JSON: {ok, data} on success, "
                             "{ok:false, error, code, exit} on failure")
    common.add_argument("--quiet", "-q", action="store_true",
                        default=argparse.SUPPRESS,
                        help="suppress non-error output on success")
    common.add_argument("--no-header", action="store_true",
                        default=argparse.SUPPRESS,
                        help="for table-output verbs, skip the header row")
    common.add_argument("--version", action="version", version=f"tb {VERSION}",
                        help="print version and exit")
    return common


def _build_parser() -> argparse.ArgumentParser:
    common = _build_common_parent()
    p = _ArgumentParser(
        prog="tb",
        description="tmux CLI for humans and LLMs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    sub = p.add_subparsers(dest="_verb", required=True, metavar="<verb>")
    register_all(sub, common)
    return p


def _normalize_global_flags(args: argparse.Namespace) -> None:
    """Fill any global flag left unset by ``SUPPRESS`` with its default."""
    for name, default in _GLOBAL_FLAG_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)


def _harden_streams() -> None:
    """Degrade instead of crashing on non-encodable output.

    Human-mode output uses a few decorative non-ASCII glyphs; under an
    ``ascii``-locked stdio they would raise ``UnicodeEncodeError``. Matching
    the lenient ``backslashreplace`` that stderr already uses by default keeps
    the tool from dying on, e.g., ``rename a → b`` in a C/ascii environment.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


def _silence_stdout() -> None:
    """Point stdout at /dev/null so a closed pipe can't raise again at exit."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    _harden_streams()
    # Best-effort guess for failures raised *during* parsing (before we know
    # the parsed --json); refined to the authoritative value once parsed.
    wants_json = "--json" in argv

    p = _build_parser()
    try:
        args = p.parse_args(argv)
        _normalize_global_flags(args)
        wants_json = args.json

        func = getattr(args, "func", None)
        if func is None:
            # A subcommand group reached dispatch with no leaf verb and no
            # default handler (e.g. a misconfigured extension parser). Treat
            # it as a usage error rather than letting AttributeError escape.
            raise UsageError("missing subcommand")

        if getattr(args, "needs_server", True) and not sessions.server_running():
            raise NoTmuxServer("no tmux server is running")

        rc = func(args) or 0
        sys.stdout.flush()  # surface a broken pipe here, not at interpreter exit
        return rc
    except TBError as e:
        if wants_json:
            output.emit_error_json(e)
        else:
            sys.stderr.write(f"tb: {e.message}\n")
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # The reader closed the pipe early (e.g. `tb capture work | head`).
        # Redirect stdout so the interpreter's final flush doesn't raise a
        # second BrokenPipeError, then exit quietly.
        _silence_stdout()
        return 0
    except Exception as e:  # noqa: BLE001 — last resort, never leak a traceback
        if os.environ.get("TB_DEBUG"):
            raise
        if wants_json:
            output.emit_error_json(TBError(str(e)))
        else:
            sys.stderr.write(f"tb: unexpected error: {e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
