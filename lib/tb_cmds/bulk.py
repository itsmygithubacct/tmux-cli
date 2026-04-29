"""Bulk / overview verbs: snapshot, describe."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import time

from .. import config, output, ports, sessions, ttyd
from ._common import require_target


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


def snapshot_data() -> dict:
    sess = sessions.list_sessions()
    panes = sessions.list_panes()
    assignments = ports.all_assignments()
    ttyds = ttyd.status_all()
    return {
        "now": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "host": os.uname().nodename,
        "tmux_server": sessions.server_running(),
        "sessions": sess,
        "panes": panes,
        "ttyd": {
            "assignments": assignments,
            "running": ttyds,
            "port_range": [config.TTYD_PORT_START, config.TTYD_PORT_END],
        },
        "dashboard": _dashboard_status(),
    }


def cmd_snapshot(args: argparse.Namespace) -> int:
    data = snapshot_data()
    if args.json or not args.human:
        output.emit_json(data)
        return 0
    # Human-ish snapshot (mostly useful in terminals for ad-hoc checks).
    print(f"host: {data['host']}   tmux server: {data['tmux_server']}")
    print(f"sessions: {len(data['sessions'])}   panes: {len(data['panes'])}")
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
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser(
        "describe",
        help="prose summary of a session (useful for LLM context)",
        parents=[common],
    )
    p.add_argument("target")
    p.set_defaults(func=cmd_describe)
