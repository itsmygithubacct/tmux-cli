"""Bulk / overview verbs: snapshot, describe."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import time

from .. import config, output, ports, sessions, ttyd
from ..errors import UsageError
from ._common import parse_target, require_target


def _dashboard_status() -> dict:
    bind = "127.0.0.1"
    port = config.DASHBOARD_PORT
    try:
        raw = json.loads(config.DASHBOARD_FILE.read_text())
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        pid = raw.get("pid")
        try:
            if pid:
                os.kill(int(pid), 0)
                port = int(raw.get("port", port))
                bind = str(raw.get("bind") or bind)
        except (OSError, ValueError, TypeError):
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        probe_host = "127.0.0.1" if bind in ("0.0.0.0", "", "::") else bind
        listening = s.connect_ex((probe_host, port)) == 0
    return {"listening": listening, "port": port, "bind": bind}


def snapshot_data(*, tmux_only: bool = False) -> dict:
    """Return one coherent overview.

    ``tmux_only`` is the low-latency polling contract for interactive clients.
    It deliberately skips the ports registry, ttyd process probes, dashboard
    state file, and dashboard socket probe; those services are unrelated to a
    tmux session/pane browser and can make a one-second UI refresh needlessly
    expensive or fail because optional dashboard state is unavailable.
    """
    sess = sessions.list_sessions()
    panes = sessions.list_panes()
    # A successful listing already proves the server is reachable. Avoid a
    # third tmux subprocess on the hot polling path; probe explicitly only
    # for the genuinely empty case (including filtered viewer-only groups).
    tmux_server = bool(sess or panes) or sessions.server_running()
    data = {
        "now": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "host": os.uname().nodename,
        "tmux_server": tmux_server,
        "sessions": sess,
        "panes": panes,
    }
    if tmux_only:
        return data

    assignments = ports.all_assignments()
    ttyds = ttyd.status_all()
    data.update({
        "ttyd": {
            "assignments": assignments,
            "running": ttyds,
            "port_range": [config.TTYD_PORT_START, config.TTYD_PORT_END],
        },
        "dashboard": _dashboard_status(),
    })
    return data


def cmd_snapshot(args: argparse.Namespace) -> int:
    capture_lines: int | None = None
    if args.capture is not None:
        capture_lines = args.lines if args.lines is not None else 80
        if capture_lines < 1:
            raise UsageError("--lines must be a positive integer")
    elif args.lines is not None:
        raise UsageError("--lines requires --capture")

    data = snapshot_data(tmux_only=args.tmux_only)
    if args.capture is not None:
        assert capture_lines is not None
        target = parse_target(args.capture)
        ok, content = sessions.capture_target(target, lines=capture_lines)
        data["capture"] = {
            "target": args.capture,
            "lines": capture_lines,
            "content": content if ok else "",
            "error": "" if ok else content,
        }
    if args.json or not args.human:
        output.emit_json(data)
        return 0
    # Human-ish snapshot (mostly useful in terminals for ad-hoc checks).
    print(f"host: {data['host']}   tmux server: {data['tmux_server']}")
    print(f"sessions: {len(data['sessions'])}   panes: {len(data['panes'])}")
    if args.tmux_only:
        return 0
    dash = data["dashboard"]
    print(f"dashboard: port {dash['port']} "
          f"({'listening' if dash['listening'] else 'down'})")
    running = [t for t in data["ttyd"]["running"] if t.get("running")]
    print(f"ttyds: {len(running)} running, "
          f"{len(data['ttyd']['assignments'])} ports assigned")
    return 0


def cmd_describe(args: argparse.Namespace) -> int:
    t = require_target(args.target)
    sess = next((s for s in sessions.list_sessions() if s["name"] == t.session), None)
    panes = [p for p in sessions.list_panes() if p["session"] == t.session]
    port = ports.get(t.session)
    pid = ttyd.read_pid(t.session)

    now = int(time.time())
    idle = now - sess["activity"] if sess else None
    lines: list[str] = []
    hdr = f"Session '{t.session}'"
    if sess:
        parts = [f"{sess['windows']} windows"]
        if sess["attached"]:
            parts.append(f"{sess['attached']} attached")
        if idle is not None:
            parts.append(f"idle {_fmt_age(idle)}")
        hdr += ": " + ", ".join(parts) + "."
    lines.append(hdr)
    for p in panes:
        marker = "*" if p["active"] else " "
        lines.append(
            f"  {marker} {p['window']}.{p['pane']} "
            f"{p['window_name'] or ''}  "
            f"cmd={p['command']}  pid={p['pid']}  cwd={p['cwd']}",
        )
    if port is not None:
        state = f"running (pid {pid})" if pid else "assigned (ttyd not running)"
        lines.append(f"ttyd: port {port}, {state}")
    else:
        lines.append("ttyd: (no port assigned)")

    if args.json:
        output.emit_json({
            "session": sess, "panes": panes,
            "ttyd": {"port": port, "pid": pid, "running": pid is not None},
            "text": "\n".join(lines),
        })
    else:
        output.emit_plain("\n".join(lines))
    return 0


def _fmt_age(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def register(sub, common) -> None:
    p = sub.add_parser(
        "snapshot",
        help="dump full state (sessions, panes, ttyds, ports) as JSON",
        parents=[common],
    )
    p.add_argument("--human", action="store_true",
                   help="terse human summary instead of JSON "
                        "(ignored when --json is also set)")
    p.add_argument(
        "--tmux-only",
        action="store_true",
        help="omit dashboard, ttyd, and port probes for low-latency polling",
    )
    p.add_argument(
        "--capture",
        metavar="TARGET",
        help="also capture one pane in the same polling request",
    )
    p.add_argument(
        "--lines",
        type=int,
        default=None,
        metavar="N",
        help="history lines for --capture (default: 80)",
    )
    # snapshot reports tmux_server:false rather than erroring when no server.
    p.set_defaults(func=cmd_snapshot, needs_server=False)

    p = sub.add_parser(
        "describe",
        help="prose summary of a session (useful for LLM context)",
        parents=[common],
    )
    p.add_argument("target")
    p.set_defaults(func=cmd_describe)
