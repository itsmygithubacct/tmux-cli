"""Read-only verbs: ls, show, capture, tail, exists."""

from __future__ import annotations

import argparse
import time

from .. import output, sessions
from ..errors import SessionNotFound, TmuxFailed
from ._common import parse_target, require_target


def cmd_ls(args: argparse.Namespace) -> int:
    rows = sessions.list_sessions()
    if args.attached:
        rows = [r for r in rows if r["attached"] > 0]
    if args.running or args.running_within is not None:
        threshold = args.running_within if args.running_within is not None else 30
        now = int(time.time())
        rows = [r for r in rows if now - r["activity"] < threshold]

    if args.json:
        output.emit_json(rows)
        return 0
    if not rows:
        if not args.quiet:
            print("(no tmux sessions)")
        return 0

    now = int(time.time())
    enriched = [
        {
            "name": r["name"],
            "win": r["windows"],
            "att": r["attached"],
            "idle": _fmt_age(now - r["activity"]),
            "created": _fmt_age(now - r["created"]) + " ago",
        }
        for r in rows
    ]
    output.emit_table(enriched, [
        ("name", "SESSION"),
        ("win", "WIN"),
        ("att", "ATT"),
        ("idle", "IDLE"),
        ("created", "CREATED"),
    ], no_header=args.no_header)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    panes = [p for p in sessions.list_panes() if p["session"] == t.session]
    session_info = next(
        (s for s in sessions.list_sessions() if s["name"] == t.session), None,
    )
    payload = {
        "session": session_info,
        "panes": panes,
    }
    if args.json:
        output.emit_json(payload)
        return 0
    s = session_info or {"name": t.session, "windows": len(panes), "attached": 0}
    print(f"{s['name']}   {s.get('windows', '?')} windows, "
          f"{s.get('attached', 0)} attached")
    output.emit_table(panes, [
        ("window", "W"),
        ("pane", "P"),
        ("window_name", "WINDOW-NAME"),
        ("command", "CMD"),
        ("pid", "PID"),
        ("cwd", "CWD"),
        ("active", "ACTIVE"),
    ], no_header=args.no_header)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    ok, content = sessions.capture_target(t, lines=args.lines, ansi=args.ansi)
    if not ok:
        raise TmuxFailed(content)
    if args.json:
        output.emit_json({"target": str(t), "lines": args.lines, "content": content})
        return 0
    output.emit_plain(content)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    ok, content = sessions.capture_target(t, lines=args.lines)
    if not ok:
        raise TmuxFailed(content)
    # Non-follow --json: full envelope. Follow mode is streaming and remains
    # plain — documented in docs/tb.md.
    if args.json and not args.follow:
        output.emit_json({"target": str(t), "lines": args.lines, "content": content})
        return 0
    output.emit_plain(content)
    if not args.follow:
        return 0
    # Poll mode. tmux doesn't expose a reliable pane-output stream without
    # pipe-pane side-effects, so we re-capture and print only the new suffix.
    last = content.rstrip("\n")
    try:
        while True:
            time.sleep(args.interval)
            if not sessions.exists(t.session):
                print("[session gone]")
                return 3
            ok, content = sessions.capture_target(t, lines=args.lines)
            if not ok:
                continue
            cur = content.rstrip("\n")
            if cur != last:
                if cur.startswith(last):
                    new = cur[len(last):].lstrip("\n")
                    if new:
                        output.emit_plain(new)
                else:
                    # Pane redrew / scrolled far — print a divider and replay.
                    print("--- pane redrew ---")
                    output.emit_plain(cur)
                last = cur
    except KeyboardInterrupt:
        return 0


def cmd_exists(args: argparse.Namespace) -> int:
    """Dual-mode contract:

    - Plain:  silent; exit 0 if the session exists, else 3. Sh-script-friendly.
    - --json: always exit 0; answer is in ``data.exists`` (bool). An LLM
      never needs both an exit code AND an envelope that can disagree.
    """
    # parse_target (not require_target) — "missing" is a valid answer here.
    t = parse_target(args.target)
    present = sessions.exists(t.session)
    if args.json:
        output.emit_json({"target": str(t), "exists": present})
        return 0
    if present:
        return 0
    raise SessionNotFound(f"no such session: {t.session}")


def _fmt_age(secs: int) -> str:
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def register(sub, common) -> None:
    p = sub.add_parser("ls", help="list tmux sessions", parents=[common])
    p.add_argument("--running", action="store_true",
                   help="only sessions with activity in the last 30s")
    p.add_argument("--running-within", type=int, metavar="SEC",
                   help="only sessions with activity in the last SEC seconds "
                        "(overrides --running)")
    p.add_argument("--attached", action="store_true",
                   help="only sessions with at least one attached client")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("show", help="show session detail (windows, panes)",
                       parents=[common])
    p.add_argument("target")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("capture", help="dump pane scrollback as plain text",
                       parents=[common])
    p.add_argument("target")
    p.add_argument("-n", "--lines", type=int, default=2000,
                   help="history lines to include (default: 2000)")
    p.add_argument("--ansi", action="store_true",
                   help="preserve ANSI escapes (tmux -e)")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("tail", help="print pane output; -f to follow",
                       parents=[common])
    p.add_argument("target")
    p.add_argument("-n", "--lines", type=int, default=200)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--interval", type=float, default=0.5,
                   help="follow poll interval in seconds (default: 0.5)")
    p.set_defaults(func=cmd_tail)

    p = sub.add_parser("exists", help="exit 0 if the session exists, else 3",
                       parents=[common])
    p.add_argument("target")
    p.set_defaults(func=cmd_exists)
