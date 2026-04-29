"""Observe verbs: wait, watch.

``watch --json`` emits newline-delimited JSON (one event per line), **not**
the standard ``{ok, data}`` envelope. Streaming consumers find NDJSON much
easier to parse incrementally; the exception is documented in docs/tb.md.
"""

from __future__ import annotations

import argparse
import json
import time

from .. import output, sessions
from ..errors import Timeout, TmuxFailed
from ._common import require_target


def cmd_wait(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    ok, err = sessions.wait_idle(
        t, idle_sec=args.idle, timeout_sec=args.timeout,
    )
    if not ok:
        if "timed out" in err:
            raise Timeout(err)
        raise TmuxFailed(err)
    if args.json:
        output.emit_json({"idle_sec": args.idle})
    elif not args.quiet:
        print(f"idle for {args.idle}s")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    last_hash = None
    try:
        while True:
            ok, content = sessions.capture_target(t, lines=args.lines)
            if not ok:
                # Session may have gone away.
                if not sessions.exists(t.session):
                    if args.json:
                        # NDJSON: one final event line, then exit 3.
                        print(json.dumps({
                            "t": time.time(), "target": str(t),
                            "event": "gone", "code": "ENOENT",
                        }), flush=True)
                    else:
                        print(f"[{_now_iso()}] session gone")
                    return 3
                continue
            h = hash(content)
            if last_hash is None:
                last_hash = h
            elif h != last_hash:
                last_hash = h
                last_line = content.rstrip("\n").rsplit("\n", 1)[-1]
                if args.json:
                    print(json.dumps({
                        "t": time.time(),
                        "target": str(t),
                        "last_line": last_line,
                    }), flush=True)
                else:
                    print(f"[{_now_iso()}] {last_line}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _now_iso() -> str:
    return time.strftime("%H:%M:%S")


def register(sub, common) -> None:
    p = sub.add_parser("wait",
                       help="block until pane has been idle for N seconds",
                       parents=[common])
    p.add_argument("target")
    p.add_argument("--idle", type=float, default=2.0,
                   help="seconds of silence required (default: 2)")
    p.add_argument("--timeout", type=float, default=0,
                   help="overall timeout (0 = no timeout)")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("watch", help="stream activity events for a session",
                       parents=[common])
    p.add_argument("target")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("-n", "--lines", type=int, default=100)
    p.set_defaults(func=cmd_watch)
