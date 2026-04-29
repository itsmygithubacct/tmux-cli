"""Dashboard-integration verbs: ``tb web start|stop|url``."""

from __future__ import annotations

import argparse
import os

from .. import output, ports, sessions, tls, ttyd
from ..errors import SessionNotFound, TmuxFailed


def _host() -> str:
    return os.environ.get("TB_DASHBOARD_HOST", "localhost")


def cmd_web_start(args: argparse.Namespace) -> int:
    if not sessions.exists(args.session):
        raise SessionNotFound(f"no such session: {args.session}")
    tls_paths = tls.load_tls_paths(cli_cert=args.cert, cli_key=args.key)
    r = ttyd.start(args.session, tls_paths=tls_paths)
    if not r.get("ok"):
        raise TmuxFailed(r.get("error", "ttyd start failed"))
    port = r.get("port")
    scheme = r.get("scheme", "http")
    url = f"{scheme}://{_host()}:{port}/"
    payload = {"session": args.session, "port": port, "url": url,
               "scheme": scheme, "pid": r.get("pid"),
               "already": r.get("already", False)}
    if args.json:
        output.emit_json(payload)
    else:
        already = " (already running)" if r.get("already") else ""
        print(f"{url}{already}")
    return 0


def cmd_web_stop(args: argparse.Namespace) -> int:
    r = ttyd.stop(args.session)
    if not r.get("ok"):
        raise TmuxFailed(r.get("error", "ttyd stop failed"))
    if args.json:
        output.emit_json(r)
    elif not args.quiet:
        if r.get("already_stopped"):
            print("not running")
        else:
            print(f"stopped (pid {r.get('pid')})")
    return 0


def cmd_web_url(args: argparse.Namespace) -> int:
    """Print the ttyd URL for a session. "Not yet assigned" is a state, not
    an error — always exits 0; the absence is conveyed via the payload."""
    port = ports.get(args.session)
    scheme = ttyd.read_scheme(args.session)
    url = f"{scheme}://{_host()}:{port}/" if port is not None else None
    if args.json:
        output.emit_json({"session": args.session, "port": port,
                          "scheme": scheme, "url": url})
    elif url:
        print(url)
    elif not args.quiet:
        print("(no port assigned — run `tb web start` first)")
    return 0


def register(sub, common) -> None:
    p = sub.add_parser("web", help="control the dashboard's ttyd for a session",
                       parents=[common])
    wsub = p.add_subparsers(dest="_webverb", required=True)

    p_start = wsub.add_parser("start", help="start ttyd for a session",
                              parents=[common])
    p_start.add_argument("session")
    p_start.add_argument("--cert", metavar="PATH", default=None,
                         help="TLS cert for ttyd. Also honours $TMUX_BROWSE_CERT.")
    p_start.add_argument("--key", metavar="PATH", default=None,
                         help="TLS key for ttyd. Also honours $TMUX_BROWSE_KEY.")
    p_start.set_defaults(func=cmd_web_start)

    p_stop = wsub.add_parser("stop", help="stop the ttyd for a session",
                             parents=[common])
    p_stop.add_argument("session")
    p_stop.set_defaults(func=cmd_web_stop)

    p_url = wsub.add_parser("url", help="print the ttyd URL (if assigned)",
                            parents=[common])
    p_url.add_argument("session")
    p_url.set_defaults(func=cmd_web_url)
