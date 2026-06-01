"""Lifecycle verbs: new, kill, rename, attach, range."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys

from .. import output, sessions
from ..errors import (
    SessionExists,
    SessionNotFound,
    Timeout,
    TmuxFailed,
    UsageError,
)
from ..targeting import Target
from ._common import parse_target, require_target


def _auto_name() -> str:
    return "tb_" + secrets.token_hex(3)


def _derive_base(command: str) -> str:
    """Base session name from a command — first token, sanitized.

    ``codex --yolo`` → ``codex``; ``./run-it.sh -x`` → ``run-it``. Strips to
    the chars tmux session names tolerate (no whitespace, ':' or '.'), so the
    result is always a valid prefix for ``<base>_<n>``.
    """
    token = command.split()[0] if command.split() else ""
    token = os.path.basename(token)            # drop any leading path
    token = re.sub(r"[^A-Za-z0-9_-]", "", token)
    return token


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


def cmd_range(args: argparse.Namespace) -> int:
    if args.count < 1:
        raise UsageError("count must be a positive integer")
    base = args.name or _derive_base(args.command)
    if not base:
        raise UsageError(
            "could not derive a session name from the command; "
            "pass --name to set one explicitly",
        )
    start = args.start
    names = [f"{base}_{i}" for i in range(start, start + args.count)]

    # Pre-check so we don't half-create a batch and leave the user guessing.
    clashes = [n for n in names if sessions.exists(n)]
    if clashes:
        raise SessionExists(
            "these sessions already exist: " + ", ".join(clashes),
        )

    created: list[str] = []

    def _so_far() -> str:
        # Mid-batch failures leave the already-created sessions in place
        # (we don't roll back work that may already be running). Name them
        # so the operator can clean up instead of guessing — the pre-check
        # above only covers the all-clash case.
        return f" (created so far: {', '.join(created)})" if created else ""

    for name in names:
        ok, err = sessions.new_session(name, cwd=args.cwd,
                                       enable_logging=not args.no_log)
        if not ok:
            raise TmuxFailed(f"creating {name}: {err}{_so_far()}")
        created.append(name)
        if not args.no_run:
            ok, err = sessions.type_line(Target(session=name), args.command)
            if not ok:
                raise TmuxFailed(f"sending command to {name}: {err}{_so_far()}")

    if args.json:
        output.emit_json({
            "base": base,
            "count": args.count,
            "command": None if args.no_run else args.command,
            "created": created,
        })
    elif not args.quiet:
        for name in created:
            print(name)
    return 0


def _range_members(base: str, start: int = 1) -> list[str]:
    """Existing ``<base>_<n>`` sessions, ordered by their numeric suffix.

    Matches the naming ``tb range`` produces. Indices below ``start`` are
    skipped (so you can resume a stagger partway through), and gaps in the
    sequence are tolerated — we order by whatever members actually exist.
    """
    rx = re.compile(rf"^{re.escape(base)}_(\d+)$")
    found: list[tuple[int, str]] = []
    for s in sessions.list_sessions():
        m = rx.match(s["name"])
        if m:
            n = int(m.group(1))
            if n >= start:
                found.append((n, s["name"]))
    found.sort()
    return [name for _, name in found]


def cmd_stagger(args: argparse.Namespace) -> int:
    # Exactly one of: a text line to type, or named key(s) to send.
    if (args.text is None) == (args.key is None):
        raise UsageError(
            "provide either a text line to type or --key <KEY…> (not both)",
        )

    members = _range_members(args.name, args.start)
    if not members:
        raise SessionNotFound(
            f"no sessions matching {args.name}_<n> "
            f"(start index {args.start})",
        )

    desc = f"key {' '.join(args.key)}" if args.key else f"text {args.text!r}"
    results: list[dict] = []
    for i, name in enumerate(members):
        t = Target(session=name)
        if args.key:
            ok, err = sessions.send_keys(t, *args.key)
        else:
            ok, err = sessions.type_line(t, args.text)
        if not ok:
            raise TmuxFailed(f"sending to {name}: {err}")

        # Wait for this pane to settle before poking the next one. The last
        # pane has no successor to gate, so we don't block on it (it may be
        # an interactive process that never goes idle).
        is_last = i == len(members) - 1
        waited = False
        if not is_last:
            if not args.json and not args.quiet:
                sys.stdout.write(f"{name}: sent {desc}; waiting for idle…\n")
                sys.stdout.flush()
            ok, err = sessions.wait_idle(
                t, idle_sec=args.idle, timeout_sec=args.timeout,
            )
            if not ok:
                if "timed out" in err:
                    raise Timeout(f"{name}: {err}")
                raise TmuxFailed(f"waiting on {name}: {err}")
            waited = True
        elif not args.json and not args.quiet:
            sys.stdout.write(f"{name}: sent {desc}\n")
            sys.stdout.flush()
        results.append({"name": name, "waited_idle": waited})

    if args.json:
        output.emit_json({
            "base": args.name,
            "key": list(args.key) if args.key else None,
            "text": args.text,
            "idle_sec": args.idle,
            "sessions": results,
        })
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

    p = sub.add_parser(
        "range",
        help="create N sessions (<base>_1..<base>_N) and run a command in each",
        parents=[common],
    )
    p.add_argument("count", type=int, help="how many sessions to create")
    p.add_argument("command",
                   help="command to run in each session; its first word is "
                        "the default session base name")
    p.add_argument("--name", "-n",
                   help="session base name (default: first word of command)")
    p.add_argument("--start", type=int, default=1,
                   help="first index for the numeric suffix (default: 1)")
    p.add_argument("--cwd", help="starting directory for every session")
    p.add_argument("--no-run", action="store_true",
                   help="create the sessions but don't run the command")
    p.add_argument("--no-log", action="store_true",
                   help="don't enable pipe-pane logging on the new sessions")
    p.set_defaults(func=cmd_range)

    p = sub.add_parser(
        "stagger",
        help="send input to each <base>_N pane in turn, waiting for the "
             "prior pane to go idle before the next",
        parents=[common],
    )
    p.add_argument("text", nargs="?",
                   help="text line to type (Enter appended) into each pane; "
                        "omit and use --key to send named key(s) instead")
    p.add_argument("--key", nargs="+", metavar="KEY",
                   help="named key(s) to send instead of typing text "
                        "(e.g. --key Enter, --key C-c)")
    p.add_argument("--name", "-n", required=True,
                   help="session base name (the <base> in <base>_N)")
    p.add_argument("--idle", type=float, default=2.0,
                   help="seconds of quiet that count as idle (default: 2)")
    p.add_argument("--timeout", type=float, default=0,
                   help="per-pane idle-wait timeout (0 = no timeout)")
    p.add_argument("--start", type=int, default=1,
                   help="lowest <base>_N index to include (default: 1)")
    p.set_defaults(func=cmd_stagger)

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
