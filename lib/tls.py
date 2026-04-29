"""Optional TLS for the dashboard + spawned ttyds.

**Default: off** (plain HTTP). Opt in via CLI / env:

    tmux-browse serve --cert cert.pem --key key.pem
    TMUX_BROWSE_CERT=cert.pem TMUX_BROWSE_KEY=key.pem tmux-browse serve

BYO cert only — no auto-generation. For a quick LAN self-signed:

    openssl req -x509 -newkey rsa:2048 -nodes -days 365 \\
        -keyout key.pem -out cert.pem -subj "/CN=localhost"

When TLS is on, the same cert/key are passed to every ``ttyd`` spawn via
``--ssl --ssl-cert --ssl-key``. If they weren't, the dashboard's HTTPS
page couldn't embed the ttyd iframes — browsers block ``ws://`` from an
``https://`` origin as mixed content.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path

from .errors import StateError

ENV_CERT = "TMUX_BROWSE_CERT"
ENV_KEY = "TMUX_BROWSE_KEY"


def load_tls_paths(cli_cert: str | None = None,
                   cli_key: str | None = None) -> tuple[Path, Path] | None:
    """Resolve a (cert, key) pair, or None if TLS is disabled.

    Priority: CLI flags > env vars. ``cert`` and ``key`` must either both
    resolve or both be absent; a half-configured pair is an error.
    """
    cert = cli_cert if cli_cert is not None else os.environ.get(ENV_CERT)
    key = cli_key if cli_key is not None else os.environ.get(ENV_KEY)
    cert = (cert or "").strip() or None
    key = (key or "").strip() or None

    if cert is None and key is None:
        return None
    if cert is None or key is None:
        raise StateError(
            f"TLS requires both --cert and --key (or ${ENV_CERT} and ${ENV_KEY}); "
            f"got cert={cert!r} key={key!r}"
        )

    cert_p, key_p = Path(cert).expanduser(), Path(key).expanduser()
    for label, p in (("cert", cert_p), ("key", key_p)):
        if not p.is_file():
            raise StateError(f"TLS {label} file not found: {p}")
        if not os.access(p, os.R_OK):
            raise StateError(f"TLS {label} file not readable: {p}")
    return cert_p, key_p


def build_context(cert: Path, key: Path) -> ssl.SSLContext:
    """Server-side SSLContext loaded from the given cert/key.

    ``PROTOCOL_TLS_SERVER`` picks the highest mutually-supported TLS
    version. We also pin the minimum to TLS 1.2 explicitly — recent
    Pythons default to this, but older builds may allow 1.0/1.1 and
    we'd rather fail closed than inherit a weak default.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Explicit floor: TLS 1.2. TLSv1_3 negotiated when both ends support it.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    except ssl.SSLError as e:
        raise StateError(f"TLS load_cert_chain failed: {e}")
    except OSError as e:
        raise StateError(f"TLS cert/key read failed: {e}")
    return ctx
