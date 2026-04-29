"""Write verbs: send, type, key, paste, exec."""

from __future__ import annotations

import argparse
import sys

from .. import exec_runner, output, sessions
from ..errors import TmuxFailed, UsageError
from ._common import require_target


def cmd_send(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    text = " ".join(args.text)
    ok, err = sessions.send_literal(t, text)
    if not ok:
        raise TmuxFailed(err)
    if args.json:
        output.emit_json({"sent": len(text)})
    elif not args.quiet:
        print(f"sent {len(text)} bytes")
    return 0


def cmd_type(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    ok, err = sessions.type_line(t, args.text)
    if not ok:
        raise TmuxFailed(err)
    if args.json:
        output.emit_json({"typed": args.text})
    elif not args.quiet:
        print("typed")
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    ok, err = sessions.send_keys(t, *args.keys)
    if not ok:
        raise TmuxFailed(err)
    if args.json:
        output.emit_json({"keys": list(args.keys)})
    elif not args.quiet:
        print(f"sent keys: {' '.join(args.keys)}")
    return 0


def cmd_paste(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    if sys.stdin.isatty():
        raise UsageError(
            "tb paste reads from stdin — pipe content in "
            "(e.g. `cat file | tb paste SESSION`)",
        )
    data = sys.stdin.read()
    if not data:
        if args.json:
            output.emit_json({"pasted": 0})
        return 0
    ok, err = sessions.paste_buffer(t, data)
    if not ok:
        raise TmuxFailed(err)
    if args.json:
        output.emit_json({"pasted": len(data)})
    elif not args.quiet:
        print(f"pasted {len(data)} bytes")
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    if not args.cmd:
        raise UsageError("missing command (after --)")
    command = " ".join(args.cmd)
    result = exec_runner.run(
        t, command,
        strategy=args.strategy,
        timeout_sec=args.timeout,
        idle_sec=args.idle_sec,
        clear=args.clear,
        interrupt_on_timeout=not args.no_interrupt,
    )
    if "error" in result:
        raise TmuxFailed(result["error"])
    if args.json:
        # No nested "ok" — the outer envelope carries that already.
        output.emit_json(result)
        return 0
    # Plain mode: print output, then print a status line to stderr.
    output.emit_plain(result.get("output", ""))
    if not args.quiet:
        rc = result.get("exit_status")
        strategy = result.get("strategy", "?")
        dur = result.get("duration", 0)
        if rc is not None:
            sys.stderr.write(f"[{strategy} · exit {rc} · {dur}s]\n")
        else:
            sys.stderr.write(f"[{strategy} · exit unknown · {dur}s]\n")
    return 0


def register(sub, common) -> None:
    p = sub.add_parser("send", help="send literal keystrokes to a pane (no Enter)",
                       parents=[common])
    p.add_argument("target")
    p.add_argument("text", nargs="+",
                   help="text to send; multiple args are joined with a single "
                        "space — for tabs/newlines/exact bytes, use `tb paste`")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("type", help="send text followed by Enter",
                       parents=[common])
    p.add_argument("target")
    p.add_argument("text", help="line of text; Enter will be appended")
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("key", help="send named key(s): Enter, C-c, Escape, Up…",
                       parents=[common])
    p.add_argument("target")
    p.add_argument("keys", nargs="+")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("paste", help="read stdin and paste into the pane",
                       parents=[common])
    p.add_argument("target")
    p.set_defaults(func=cmd_paste)

    p = sub.add_parser(
        "exec",
        help="run a command in the pane, wait for completion, return output",
        parents=[common],
    )
    p.add_argument("target")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="overall timeout in seconds (default: 30)")
    p.add_argument("--strategy", choices=("auto", "sentinel", "idle"),
                   default="auto",
                   help="sentinel (shell only) or idle (generic)")
    p.add_argument("--idle-sec", type=float, default=2.0,
                   help="idle-strategy: seconds of quiet = done (default: 2)")
    p.add_argument("--clear", action="store_true",
                   help="send C-u C-k first to clear any half-typed line")
    p.add_argument("--no-interrupt", action="store_true",
                   help="on timeout, do NOT send C-c (default: send)")
    p.add_argument("cmd", nargs="+",
                   help="command to run — prefix with -- to stop flag parsing")
    p.set_defaults(func=cmd_exec)
