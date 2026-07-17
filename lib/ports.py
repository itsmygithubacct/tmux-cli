"""Port registry — stable (session name → port) assignments persisted to JSON.

Thread/process-safe via an fcntl file lock on the registry. Ports are never
released implicitly; if a session disappears and comes back under the same
name, it gets the same port. ``release()`` is available for explicit cleanup.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from .errors import StateError


def _empty_registry() -> dict:
    return {"assignments": {}, "next_port": config.TTYD_PORT_START}


def _ensure_state_dir() -> None:
    try:
        config.ensure_dirs()
    except OSError as e:
        raise StateError(
            f"cannot create state dir {config.STATE_DIR}: {e.strerror or e}",
        )


def _recover_corrupt(path: Path, err: Exception) -> dict:
    """Move a corrupt registry aside and log the rescue to stderr."""
    backup = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
    try:
        shutil.copy2(path, backup)
    except OSError:
        backup = None
    sys.stderr.write(
        f"tmux-browse: ports registry at {path} was corrupt ({err}); "
        f"reinitialised"
        + (f", backup kept at {backup}" if backup else "")
        + "\n",
    )
    return _empty_registry()


def _validate_registry(data: object) -> dict:
    """Validate persisted state before any caller indexes into it."""
    if not isinstance(data, dict):
        raise ValueError("registry root must be an object")
    data.setdefault("assignments", {})
    data.setdefault("next_port", config.TTYD_PORT_START)

    assignments = data["assignments"]
    if not isinstance(assignments, dict):
        raise ValueError("assignments must be an object")
    used: set[int] = set()
    for session, port in assignments.items():
        if not isinstance(session, str) or not session:
            raise ValueError("assignment names must be non-empty strings")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError(f"port for {session!r} must be an integer")
        if not config.TTYD_PORT_START <= port <= config.TTYD_PORT_END:
            raise ValueError(f"port for {session!r} is outside the configured range")
        if port in used:
            raise ValueError(f"port {port} is assigned more than once")
        used.add(port)

    next_port = data["next_port"]
    if isinstance(next_port, bool) or not isinstance(next_port, int):
        raise ValueError("next_port must be an integer")
    if not config.TTYD_PORT_START <= next_port <= config.TTYD_PORT_END:
        raise ValueError("next_port is outside the configured range")
    return data


@contextmanager
def _locked_registry():
    """Yield (registry_dict, save_fn). File lock held for the whole block."""
    _ensure_state_dir()
    path: Path = config.PORTS_FILE
    try:
        path.touch(exist_ok=True)
    except OSError as e:
        raise StateError(f"cannot create {path}: {e.strerror or e}")
    try:
        f = open(path, "r+")
    except OSError as e:
        raise StateError(f"cannot open {path}: {e.strerror or e}")
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
        except OSError as e:
            raise StateError(f"cannot lock {path}: {e.strerror or e}")
        try:
            f.seek(0)
            raw = f.read().strip()
            recovered = False
            if raw:
                try:
                    data = _validate_registry(json.loads(raw))
                except (json.JSONDecodeError, ValueError) as e:
                    data = _recover_corrupt(path, e)
                    recovered = True
            else:
                data = _empty_registry()

            def save() -> None:
                # Serialise *before* truncating: a json error (or any
                # exception building the payload) then leaves the existing
                # registry untouched rather than blanked. We rewrite the
                # locked fd in place instead of the usual write-temp-then-
                # rename dance because the fcntl lock is held on this inode —
                # renaming a fresh file over the path would let a concurrent
                # waiter lock a *different* inode and break mutual exclusion.
                payload = json.dumps(data, indent=2, sort_keys=True)
                f.seek(0)
                f.truncate()
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            # Persist recovery even for read-only calls such as get(); otherwise
            # every read would rediscover and back up the same corrupt payload.
            if recovered:
                save()

            yield data, save
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _allocate(data: dict, session: str) -> int:
    assigned = data["assignments"]
    used = set(assigned.values())
    start = config.TTYD_PORT_START
    end = config.TTYD_PORT_END
    # Resume scanning from next_port for locality, then wrap.
    hint = data.get("next_port", start)
    scan = list(range(hint, end + 1)) + list(range(start, hint))
    for port in scan:
        if port not in used:
            assigned[session] = port
            data["next_port"] = port + 1 if port < end else start
            return port
    raise RuntimeError(
        f"No free ports in [{start}, {end}] — "
        f"{len(used)} assigned. Consider running `tmux-browse ports --prune`.",
    )


def assign(session: str) -> int:
    """Return (and persist) the port assigned to ``session``; allocate if new."""
    with _locked_registry() as (data, save):
        existing = data["assignments"].get(session)
        if existing is not None:
            return int(existing)
        port = _allocate(data, session)
        save()
        return port


def get(session: str) -> int | None:
    with _locked_registry() as (data, _save):
        p = data["assignments"].get(session)
        return int(p) if p is not None else None


def all_assignments() -> dict[str, int]:
    with _locked_registry() as (data, _save):
        return dict(data["assignments"])


def release(session: str) -> bool:
    with _locked_registry() as (data, save):
        if session in data["assignments"]:
            del data["assignments"][session]
            save()
            return True
        return False


def prune(active_sessions: set[str]) -> list[str]:
    """Drop assignments for sessions not in ``active_sessions``. Returns dropped names."""
    dropped: list[str] = []
    with _locked_registry() as (data, save):
        for name in list(data["assignments"].keys()):
            if name not in active_sessions:
                dropped.append(name)
                del data["assignments"][name]
        if dropped:
            save()
    return dropped
