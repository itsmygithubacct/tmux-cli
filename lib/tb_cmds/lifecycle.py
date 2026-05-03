"""Lifecycle verbs: new, kill, rename, attach."""

from __future__ import annotations

import argparse
import os
import secrets
import sys

from .. import output, sessions
from ..errors import SessionExists, SessionNotFound, TmuxFailed, UsageError
from ._common import parse_target, require_target


def _auto_name() -> str:
    return "tb_" + secrets.token_hex(3)


def cmd_new(args: argparse.Namespace) -> int:
    name = args.name
    if args.auto:
        if name:
            raise UsageError("--auto generates a name; don't pass one")
        name = _auto_name()
        while sessions.exists(name):
            name = _auto_name()
    if not name:
        raise UsageError("missing session name (or use --auto)")
    if args.attach and not sys.stdout.isatty():
        raise UsageError("refusing to --attach from non-TTY")
    if sessions.exists(name):
        raise SessionExists(f"session '{name}' already exists")
    ok, err = sessions.new_session(name, cwd=args.cwd, cmd=args.cmd,
                                   enable_logging=not args.no_log)
    if not ok:
        raise TmuxFailed(err)
    if args.json:
        output.emit_json({"name": name})
    else:
        # Print the resolved name so --auto callers can capture it.
        print(name)
    if args.attach:
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"={name}"])
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    # parse_target, not require_target — JSON mode treats "already gone" as
    # success (idempotent), so we need to detect absence ourselves.
    t = parse_target(args.target)
    if not sessions.exists(t.session):
        if args.json:
            output.emit_json({"name": t.session, "already_gone": True})
            return 0
        raise SessionNotFound(f"no such session: {t.session}")
    if sys.stdout.isatty() and not args.force:
        raise UsageError(
            f"kill is destructive; pass -f/--force to proceed",
        )
    ok, err = sessions.kill(t.session)
    if not ok:
        raise TmuxFailed(err)
    if args.json:
        output.emit_json({"name": t.session, "killed": True})
    elif not args.quiet:
        print(f"killed {t.session}")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    if not sessions.exists(args.old):
        raise SessionNotFound(f"no such session: {args.old}")
    if sessions.exists(args.new):
        raise SessionExists(f"session '{args.new}' already exists")
    ok, err = sessions.rename(args.old, args.new)
    if not ok:
        raise TmuxFailed(err)
    if args.json:
        output.emit_json({"from": args.old, "to": args.new})
    elif not args.quiet:
        print(f"renamed {args.old} → {args.new}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    if not sys.stdout.isatty():
        raise UsageError("attach requires an interactive TTY")
    os.execvp("tmux", ["tmux", "attach-session", "-t", f"={t.session}"])
    return 0  # unreachable


def register(sub, common) -> None:
    p = sub.add_parser("new", help="create a new (detached) session",
                       parents=[common])
    p.add_argument("name", nargs="?", help="session name; required unless --auto")
    p.add_argument("--cwd", help="starting directory")
    p.add_argument("--cmd", help="command to run as the session's first pane")
    p.add_argument("--attach", action="store_true",
                   help="attach after creating (requires TTY)")
    p.add_argument("--auto", action="store_true",
                   help="generate a random name and print it")
    p.add_argument("--no-log", action="store_true",
                   help="don't enable pipe-pane logging "
                        "(disables hash-based idle detection for this session)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("kill", help="kill a session (and any child processes)",
                       parents=[common])
    p.add_argument("target")
    p.add_argument("-f", "--force", action="store_true",
                   help="required when stdout is a TTY")
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("rename", help="rename a session", parents=[common])
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("attach", help="attach to a session (requires a TTY)",
                       parents=[common])
    p.add_argument("target")
    p.set_defaults(func=cmd_attach)
