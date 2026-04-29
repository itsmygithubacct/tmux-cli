"""Continuous per-session logging and content-hash idle detection.

Each tmux session has its output piped to
``~/.tmux-browse/session-logs/<session>.log`` via ``tmux pipe-pane``.
``idle_seconds(session)`` reads the tail of that log, hashes it, and
returns the age of the last content change — which is a more accurate
"idle" signal than tmux's ``session_activity`` field, since the latter
bumps on cursor-position updates and other non-content events.

Logging is idempotent: ``pipe-pane -o`` only opens a new pipe when no
pipe currently exists for that pane, so calling ``ensure_logging``
repeatedly is safe and cheap.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import time
from pathlib import Path

from . import config


LOG_DIR = config.STATE_DIR / "session-logs"

# How many trailing bytes of the log are hashed for change-detection.
# Small enough to stay fast on large logs; large enough that a single
# keystroke's output reliably flips the hash.
_TAIL_BYTES = 8192

# Throttle for ensure_logging_all — enumerating every pane across every
# session costs a subprocess round-trip, and /api/sessions is hot.
_ENSURE_THROTTLE_SEC = 10
_last_ensure_ts = 0

# In-memory cache of the last observed hash and the wall-clock time we
# first saw the current hash. Keyed by session name.
_hash_state: dict[str, tuple[str, int]] = {}


def log_path(session: str) -> Path:
    return LOG_DIR / f"{session}.log"


def _ensure_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _list_panes(session: str) -> list[str]:
    """Return tmux pane ids (``%N``) for every pane in ``session``."""
    r = subprocess.run(
        ["tmux", "list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _list_sessions() -> list[str]:
    # Defer to sessions.list_sessions() so ttyd_wrap's per-viewer grouped
    # sessions are deduped — we don't want empty log files for those.
    from . import sessions
    return [s["name"] for s in sessions.list_sessions()]


def ensure_logging(session: str) -> None:
    """Enable pipe-pane logging for every pane in ``session``. Idempotent."""
    _ensure_dir()
    path = log_path(session)
    quoted = shlex.quote(str(path))
    for pane_id in _list_panes(session):
        # -o: only open a new pipe if no pipe currently exists. This is what
        # makes repeated calls safe; tmux will skip panes that are already
        # being piped.
        subprocess.run(
            ["tmux", "pipe-pane", "-o", "-t", pane_id, f"cat >> {quoted}"],
            capture_output=True, text=True, timeout=5,
        )


def ensure_logging_all(force: bool = False) -> None:
    """Ensure pipe-pane is active for every pane of every session.

    Throttled internally; callers can invoke this on every request. Pass
    ``force=True`` to bypass the throttle (used in tests and on session
    creation).
    """
    global _last_ensure_ts
    now = int(time.time())
    if not force and now - _last_ensure_ts < _ENSURE_THROTTLE_SEC:
        return
    _last_ensure_ts = now
    for name in _list_sessions():
        ensure_logging(name)


def _read_tail(path: Path, limit: int = _TAIL_BYTES) -> bytes:
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    try:
        with path.open("rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read()
    except OSError:
        return b""


def activity_ts(session: str, now: int | None = None) -> int | None:
    """Epoch second of the last content change in ``session``'s log.

    Returns ``None`` if no log file exists yet (log hasn't been created
    because no output has been captured, or logging was never enabled).
    """
    path = log_path(session)
    if not path.exists():
        return None
    if now is None:
        now = int(time.time())
    h = hashlib.sha256(_read_tail(path)).hexdigest()
    prev = _hash_state.get(session)
    if prev is None:
        # First observation — anchor activity to now rather than to an
        # arbitrary past write, so idle starts ticking from when the
        # server first sees the session.
        _hash_state[session] = (h, now)
        return now
    prev_hash, prev_ts = prev
    if h != prev_hash:
        _hash_state[session] = (h, now)
        return now
    return prev_ts


def idle_seconds(session: str, now: int | None = None) -> int | None:
    """Seconds since ``session``'s log content last changed. ``None`` if
    no log exists."""
    if now is None:
        now = int(time.time())
    ts = activity_ts(session, now)
    if ts is None:
        return None
    return max(0, now - ts)


def forget(session: str) -> None:
    """Drop cached hash state — call on rename/kill to avoid stale entries."""
    _hash_state.pop(session, None)
