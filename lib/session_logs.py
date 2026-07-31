"""Permanent per-pane logging and content-hash idle detection.

tmux ``pipe-pane`` starts one :mod:`session_log_writer` process per pane. The
writer keeps a small shared activity tail for idle detection and archives the
complete pane stream as ordered ``zstd -3`` segments below
``~/.gpu_terminal/tmux-cli/logs``.

Completed archives are user data: this module never expires or deletes them.
Only disposable activity tails, writer markers, and successfully archived
plaintext spools are removed automatically.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from . import config
from .session_log_writer import (
    _archive_matches_source,
    _atomic_json,
    finalize_segment,
)


ROOT_DIR = config.TMUX_CLI_HOME
LIVE_DIR = config.SESSION_LOG_LIVE_DIR
ARCHIVE_DIR = config.SESSION_LOG_ARCHIVE_DIR
METADATA_DIR = config.SESSION_LOG_METADATA_DIR
RUNTIME_DIR = config.SESSION_LOG_RUNTIME_DIR
ACTIVITY_DIR = config.SESSION_LOG_ACTIVITY_DIR
LOCK_DIR = config.SESSION_LOG_LOCK_DIR
DIAGNOSTIC_PATH = RUNTIME_DIR / "logging-errors.log"
REAPER_LOCK = LOCK_DIR / "log-reaper.lock"
LEGACY_LOG_DIR = config.STATE_DIR / "session-logs"

# Backward-compatible name for consumers/tests that treated the idle tail as
# the whole session-log store.
LOG_DIR = ACTIVITY_DIR


class PaneCaptureInfo(TypedDict):
    pane_id: str
    tmux_session_id: str
    window_index: str
    pane_index: str
    cwd: str
    command: str


_PANE_FORMAT = (
    "#{pane_id}\t#{session_id}\t#{window_index}\t#{pane_index}"
    "\t#{pane_current_path}\t#{pane_current_command}"
)
_LIVE_NAME_RE = re.compile(r"^([0-9a-f]{32})--([0-9]{6})\.log$")
_ARCHIVE_NAME_RE = re.compile(
    r"--([0-9a-f]{32})--([0-9]{6})\.log\.zst$",
)


def _positive_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


# The activity tail retains the existing 10 MiB / 8 MiB behavior, but it is no
# longer the permanent record. Full capture rolls into lossless 8 MiB segments.
_ACTIVITY_MAX_BYTES = _positive_env(
    "TB_SESSION_LOG_MAX_BYTES", 10 * 1024 * 1024,
)
_ACTIVITY_KEEP_BYTES = min(8 * 1024 * 1024, _ACTIVITY_MAX_BYTES)
_SEGMENT_BYTES = _positive_env(
    "TB_SESSION_LOG_SEGMENT_BYTES", 8 * 1024 * 1024,
)

_TAIL_BYTES = 8192
_ENSURE_THROTTLE_SEC = 10
_ORPHAN_GRACE_SEC = 60
_last_ensure_ts = 0
_hash_state: dict[str, tuple[str, int]] = {}


def _safe(name: str) -> str:
    """Return a reversible, collision-free path component."""
    return urllib.parse.quote(name, safe="-_")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _filename_time(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S.") + f"{value.microsecond:06d}Z"


def log_path(session: str) -> Path:
    """Path of the disposable activity tail for ``session``."""
    return ACTIVITY_DIR / f"{_safe(session)}.log"


def _writer_marker(session: str) -> Path:
    return RUNTIME_DIR / f".{_safe(session)}.permanent-writer-v2"


def _capture_lock(capture_id: str) -> Path:
    return LOCK_DIR / f"{capture_id}.lock"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"refusing non-directory log path: {path}")
    path.chmod(0o700)


def _ensure_dir() -> None:
    for path in (
        ROOT_DIR,
        LIVE_DIR,
        ARCHIVE_DIR,
        METADATA_DIR,
        RUNTIME_DIR,
        ACTIVITY_DIR,
        LOCK_DIR,
    ):
        _ensure_private_dir(path)


def _list_panes(session: str) -> list[PaneCaptureInfo]:
    """Return capture metadata for every pane in ``session``."""
    result = subprocess.run(
        ["tmux", "list-panes", "-s", "-t", f"={session}", "-F", _PANE_FORMAT],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return []
    panes: list[PaneCaptureInfo] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        pane_id, session_id, window_index, pane_index, cwd, command = parts
        if not pane_id:
            continue
        panes.append({
            "pane_id": pane_id,
            "tmux_session_id": session_id,
            "window_index": window_index,
            "pane_index": pane_index,
            "cwd": cwd,
            "command": command,
        })
    return panes


def _list_sessions() -> list[str]:
    # Defer to sessions.list_sessions() so ttyd_wrap's grouped viewer sessions
    # remain deduplicated.
    from . import sessions
    return [session["name"] for session in sessions.list_sessions()]


def _all_tmux_sessions() -> set[str] | None:
    """Return every raw tmux session name, or ``None`` if unsure.

    Unlike ``_list_sessions()``, this deliberately includes tmux-browse viewer
    groups. Legacy migration must never mistake a hidden-but-live writer for an
    orphan. A confirmed absent server is safely an empty set; an unavailable or
    ambiguous tmux response blocks legacy mutation.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        }
    stderr = (result.stderr or "").lower()
    no_server = ("no server", "error connecting", "no such file",
                 "failed to connect")
    if any(marker in stderr for marker in no_server):
        return set()
    return None


def _writer_command(
    session: str,
    pane: PaneCaptureInfo,
    *,
    capture_id: str,
    started: datetime,
) -> str:
    writer = Path(__file__).with_name("session_log_writer.py")
    pane_label = _safe(pane["pane_id"].lstrip("%") or "unknown")
    prefix = (
        f"{_filename_time(started)}--{_safe(session)}--p{pane_label}"
        f"--{capture_id}"
    )
    archive_dir = (
        ARCHIVE_DIR
        / started.strftime("%Y")
        / started.strftime("%m")
        / started.strftime("%d")
    )
    return shlex.join([
        sys.executable,
        str(writer),
        "--activity-path",
        str(log_path(session)),
        "--live-dir",
        str(LIVE_DIR),
        "--archive-dir",
        str(archive_dir),
        "--metadata-dir",
        str(METADATA_DIR),
        "--lock-dir",
        str(LOCK_DIR),
        "--diagnostic-path",
        str(DIAGNOSTIC_PATH),
        "--capture-id",
        capture_id,
        "--archive-prefix",
        prefix,
        "--session-name",
        session,
        "--tmux-session-id",
        pane["tmux_session_id"],
        "--pane-id",
        pane["pane_id"],
        "--window-index",
        pane["window_index"],
        "--pane-index",
        pane["pane_index"],
        "--initial-cwd",
        pane["cwd"],
        "--initial-command",
        pane["command"],
        "--started-utc",
        _utc_iso(started),
        "--activity-max-bytes",
        str(_ACTIVITY_MAX_BYTES),
        "--activity-keep-bytes",
        str(_ACTIVITY_KEEP_BYTES),
        "--segment-bytes",
        str(_SEGMENT_BYTES),
    ])


def ensure_logging(session: str) -> None:
    """Enable permanent capture for every pane in ``session``. Idempotent."""
    _ensure_dir()
    marker = _writer_marker(session)
    migrate_existing_pipe = not marker.exists()
    panes = _list_panes(session)
    migrated = bool(panes)
    for pane in panes:
        command = _writer_command(
            session,
            pane,
            capture_id=uuid.uuid4().hex,
            started=_utc_now(),
        )
        # The first run after this upgrade replaces legacy ``cat`` or bounded
        # writers. Later calls use -o, configuring only panes with no pipe.
        options = [] if migrate_existing_pipe else ["-o"]
        result = subprocess.run(
            ["tmux", "pipe-pane", *options, "-t", pane["pane_id"], command],
            capture_output=True,
            text=True,
            timeout=5,
        )
        migrated = migrated and result.returncode == 0
    if migrate_existing_pipe and migrated:
        marker.touch(mode=0o600)
        marker.chmod(0o600)


def ensure_logging_all(force: bool = False) -> None:
    """Ensure capture is active for every pane of every visible session."""
    global _last_ensure_ts
    now = int(time.time())
    if not force and now - _last_ensure_ts < _ENSURE_THROTTLE_SEC:
        return
    _last_ensure_ts = now
    active = _list_sessions()
    for name in active:
        ensure_logging(name)
    prune(active)


def _read_tail(path: Path, limit: int = _TAIL_BYTES) -> bytes:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > limit:
                stream.seek(size - limit)
            return stream.read()
    except OSError:
        return b""


def activity_ts(session: str, now: int | None = None) -> int | None:
    """Epoch second of the last observed output change in ``session``."""
    path = log_path(session)
    if not path.exists():
        return None
    if now is None:
        now = int(time.time())
    digest = hashlib.sha256(_read_tail(path)).hexdigest()
    previous = _hash_state.get(session)
    if previous is None:
        _hash_state[session] = (digest, now)
        return now
    previous_hash, previous_ts = previous
    if digest != previous_hash:
        _hash_state[session] = (digest, now)
        return now
    return previous_ts


def idle_seconds(session: str, now: int | None = None) -> int | None:
    """Seconds since captured output last changed; ``None`` before first log."""
    if now is None:
        now = int(time.time())
    changed = activity_ts(session, now)
    if changed is None:
        return None
    return max(0, now - changed)


def forget(session: str) -> None:
    _hash_state.pop(session, None)


def discard_runtime(session: str) -> int:
    """Remove only disposable idle/marker state for a session."""
    forget(session)
    removed = 0
    for path in (log_path(session), _writer_marker(session)):
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


def remove(session: str) -> int:
    """Compatibility alias for :func:`discard_runtime`.

    The old implementation also deleted session history. Permanent metadata,
    pending capture spools, and completed archives are now deliberately
    untouched.
    """
    return discard_runtime(session)


def _capture_is_locked(capture_id: str) -> bool:
    path = _capture_lock(capture_id)
    try:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _load_metadata(capture_id: str) -> dict[str, Any] | None:
    path = METADATA_DIR / f"{capture_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _fallback_metadata(
    capture_id: str,
    source: Path,
    *,
    origin: str = "recovered",
    session_name: str = "unknown",
    pane_id: str = "unknown",
) -> dict[str, Any]:
    started = datetime.fromtimestamp(source.stat().st_mtime, timezone.utc)
    pane_label = _safe(pane_id.lstrip("%") or "unknown")
    prefix = (
        f"{_filename_time(started)}--{_safe(session_name)}--p{pane_label}"
        f"--{capture_id}"
    )
    archive_dir = (
        ARCHIVE_DIR
        / started.strftime("%Y")
        / started.strftime("%m")
        / started.strftime("%d")
    )
    return {
        "format": 1,
        "capture_id": capture_id,
        "session_name": session_name,
        "tmux_session_id": "",
        "pane_id": pane_id,
        "window_index": "",
        "pane_index": "",
        "started_utc": _utc_iso(started),
        "ended_utc": None,
        "initial_cwd": "",
        "initial_command": "",
        "archive_prefix": prefix,
        "archive_directory": str(archive_dir),
        "compression": "zstd-3",
        "origin": origin,
        "state": "pending",
        "segments": [],
    }


def _safe_archive_target(
    metadata: dict[str, Any],
    capture_id: str,
    sequence: int,
    source: Path,
) -> Path:
    raw_dir = metadata.get("archive_directory")
    raw_prefix = metadata.get("archive_prefix")
    if isinstance(raw_dir, str) and isinstance(raw_prefix, str):
        candidate_dir = Path(raw_dir)
        try:
            candidate_dir.resolve().relative_to(ARCHIVE_DIR.resolve())
        except (OSError, ValueError):
            pass
        else:
            if (
                "/" not in raw_prefix
                and "\x00" not in raw_prefix
                and f"--{capture_id}" in raw_prefix
            ):
                return candidate_dir / (
                    f"{raw_prefix}--{sequence:06d}.log.zst"
                )
    fallback = _fallback_metadata(capture_id, source)
    metadata.update(fallback)
    return Path(fallback["archive_directory"]) / (
        f"{fallback['archive_prefix']}--{sequence:06d}.log.zst"
    )


def _record_recovered_segment(
    metadata: dict[str, Any],
    archive: Path,
    sequence: int,
    source_bytes: int,
) -> None:
    segments = metadata.setdefault("segments", [])
    if not isinstance(segments, list):
        segments = []
        metadata["segments"] = segments
    replacement = {
        "sequence": sequence,
        "bytes": source_bytes,
        "archive": archive.name,
        "pending": None,
    }
    for index, value in enumerate(segments):
        if isinstance(value, dict) and value.get("sequence") == sequence:
            segments[index] = replacement
            break
    else:
        segments.append(replacement)
    if not any(
        path.name.startswith(f"{metadata['capture_id']}--")
        for path in LIVE_DIR.glob("*.log")
    ):
        metadata["state"] = "closed"
        metadata["ended_utc"] = metadata.get("ended_utc") or _utc_iso(_utc_now())
    _atomic_json(
        METADATA_DIR / f"{metadata['capture_id']}.json",
        metadata,
    )


def _recover_live(*, now: float) -> dict[str, int]:
    counts = {"archived": 0, "pending": 0, "errors": 0}
    try:
        sources = sorted(LIVE_DIR.glob("*.log"))
    except OSError:
        return counts
    for source in sources:
        match = _LIVE_NAME_RE.match(source.name)
        if not match:
            counts["pending"] += 1
            continue
        capture_id, raw_sequence = match.groups()
        try:
            if now - source.stat().st_mtime < _ORPHAN_GRACE_SEC:
                counts["pending"] += 1
                continue
        except OSError:
            counts["errors"] += 1
            continue
        if _capture_is_locked(capture_id):
            counts["pending"] += 1
            continue
        metadata = _load_metadata(capture_id)
        if metadata is None:
            try:
                metadata = _fallback_metadata(capture_id, source)
            except OSError:
                counts["errors"] += 1
                continue
        # The filename is the trusted identity. Never let edited/corrupt
        # metadata redirect its own sidecar update outside METADATA_DIR.
        metadata["capture_id"] = capture_id
        sequence = int(raw_sequence)
        try:
            source_bytes = source.stat().st_size
        except OSError:
            counts["errors"] += 1
            continue
        archive = _safe_archive_target(
            metadata, capture_id, sequence, source,
        )
        if finalize_segment(
            source,
            archive,
            diagnostic_path=DIAGNOSTIC_PATH,
        ):
            counts["archived"] += 1
            try:
                _record_recovered_segment(
                    metadata, archive, sequence, source_bytes,
                )
            except OSError:
                counts["errors"] += 1
        else:
            counts["pending"] += 1
    return counts


def _legacy_capture_id(path: Path) -> str:
    status = path.stat()
    identity = (
        f"{path.resolve()}:{status.st_dev}:{status.st_ino}:"
        f"{status.st_mtime_ns}:{status.st_size}"
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, identity).hex


def _migrate_legacy(
    active_sessions: set[str],
    *,
    now: float,
    limit: int | None,
) -> dict[str, int]:
    counts = {"legacy_archived": 0, "legacy_pending": 0, "errors": 0}
    try:
        sources = sorted(LEGACY_LOG_DIR.glob("*.log"))
    except OSError:
        return counts
    attempted = 0
    for source in sources:
        if limit is not None and attempted >= limit:
            break
        encoded_session = source.name[:-4]
        session_name = urllib.parse.unquote(encoded_session)
        try:
            if now - source.stat().st_mtime < _ORPHAN_GRACE_SEC:
                counts["legacy_pending"] += 1
                continue
        except OSError:
            counts["errors"] += 1
            continue
        # A v2 marker proves active panes were rewired away from the legacy
        # path. Without it, never touch an active session's open file.
        if (
            session_name in active_sessions
            and not _writer_marker(session_name).exists()
        ):
            counts["legacy_pending"] += 1
            continue
        attempted += 1
        try:
            capture_id = _legacy_capture_id(source)
            metadata = _fallback_metadata(
                capture_id,
                source,
                origin="legacy-tmux-browse",
                session_name=session_name,
                pane_id="legacy",
            )
            metadata["state"] = "pending"
            archive = _safe_archive_target(
                metadata, capture_id, 0, source,
            )
            source_bytes = source.stat().st_size
            metadata_path = METADATA_DIR / f"{capture_id}.json"
            already_imported = archive.exists() and metadata_path.exists()
            _atomic_json(metadata_path, metadata)
            if not finalize_segment(
                source,
                archive,
                diagnostic_path=DIAGNOSTIC_PATH,
                remove_source=False,
            ):
                counts["legacy_pending"] += 1
                continue
            _record_recovered_segment(metadata, archive, 0, source_bytes)
            if not already_imported:
                counts["legacy_archived"] += 1
        except OSError:
            counts["errors"] += 1
    return counts


def recover(
    active_sessions: list[str] | set[str] | None = None,
    *,
    now: float | None = None,
    migrate_legacy: bool = False,
    legacy_limit: int | None = None,
) -> dict[str, int]:
    """Finalize abandoned spools and optionally import legacy session logs."""
    _ensure_dir()
    if now is None:
        now = time.time()
    legacy_safe = True
    if active_sessions is None and migrate_legacy:
        discovered = _all_tmux_sessions()
        if discovered is None:
            active_sessions = set()
            legacy_safe = False
        else:
            active_sessions = discovered
    else:
        active_sessions = set(active_sessions or ())
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(REAPER_LOCK, flags, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "archived": 0,
                "pending": 0,
                "legacy_archived": 0,
                "legacy_pending": 0,
                "errors": 0,
                "busy": 1,
            }
        counts = _recover_live(now=now)
        result = {
            **counts,
            "legacy_archived": 0,
            "legacy_pending": 0,
            "busy": 0,
        }
        if migrate_legacy and legacy_safe:
            legacy = _migrate_legacy(
                active_sessions,
                now=now,
                limit=legacy_limit,
            )
            result["legacy_archived"] = legacy["legacy_archived"]
            result["legacy_pending"] = legacy["legacy_pending"]
            result["errors"] += legacy["errors"]
        return result
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def prune(
    active_sessions: list[str] | set[str],
    *,
    now: float | None = None,
) -> int:
    """Remove disposable orphan state, then recover permanent capture data."""
    if now is None:
        now = time.time()
    _ensure_dir()
    active_basenames = {_safe(name) for name in active_sessions}
    removed = 0
    for directory, suffix, leading_dot in (
        (ACTIVITY_DIR, ".log", False),
        (RUNTIME_DIR, ".permanent-writer-v2", True),
    ):
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file():
                continue
            encoded: str | None = None
            if not leading_dot and path.name.endswith(suffix):
                encoded = path.name[:-len(suffix)]
            elif (
                leading_dot
                and path.name.startswith(".")
                and path.name.endswith(suffix)
            ):
                encoded = path.name[1:-len(suffix)]
            if not encoded or encoded in active_basenames:
                continue
            try:
                if now - path.stat().st_mtime < _ORPHAN_GRACE_SEC:
                    continue
                path.unlink()
                removed += 1
            except (FileNotFoundError, OSError):
                pass
    for name in list(_hash_state):
        if _safe(name) not in active_basenames:
            _hash_state.pop(name, None)
    # Automatic maintenance handles only new-format spools. Importing old
    # plaintext is an explicit ``tb logs recover`` operation, and even that
    # operation retains each legacy source after verifying its new archive.
    recover(
        None,
        now=now,
        migrate_legacy=False,
        legacy_limit=None,
    )
    return removed


def _archive_index() -> dict[str, list[tuple[int, Path]]]:
    result: dict[str, list[tuple[int, Path]]] = {}
    try:
        paths = ARCHIVE_DIR.rglob("*.log.zst")
        for path in paths:
            match = _ARCHIVE_NAME_RE.search(path.name)
            if not match:
                continue
            capture_id, raw_sequence = match.groups()
            result.setdefault(capture_id, []).append(
                (int(raw_sequence), path),
            )
    except OSError:
        pass
    for values in result.values():
        values.sort(key=lambda item: item[0])
    return result


def _live_index() -> dict[str, list[tuple[int, Path]]]:
    result: dict[str, list[tuple[int, Path]]] = {}
    try:
        for path in LIVE_DIR.glob("*.log"):
            match = _LIVE_NAME_RE.match(path.name)
            if not match:
                continue
            capture_id, raw_sequence = match.groups()
            result.setdefault(capture_id, []).append(
                (int(raw_sequence), path),
            )
    except OSError:
        pass
    for values in result.values():
        values.sort(key=lambda item: item[0])
    return result


def list_captures(
    *,
    session: str | None = None,
    pane: str | None = None,
) -> list[dict[str, Any]]:
    """Return newest-first permanent-capture summaries."""
    _ensure_dir()
    archives = _archive_index()
    live = _live_index()
    capture_ids = set(archives) | set(live)
    try:
        capture_ids.update(path.stem for path in METADATA_DIR.glob("*.json"))
    except OSError:
        pass
    records: list[dict[str, Any]] = []
    for capture_id in capture_ids:
        metadata = _load_metadata(capture_id) or {
            "capture_id": capture_id,
            "session_name": "unknown",
            "pane_id": "unknown",
            "started_utc": "",
            "ended_utc": None,
            "state": "unknown",
        }
        if session is not None and metadata.get("session_name") != session:
            continue
        if pane is not None and metadata.get("pane_id") != pane:
            continue
        archive_paths = [path for _, path in archives.get(capture_id, [])]
        live_paths = [path for _, path in live.get(capture_id, [])]
        size = 0
        for path in archive_paths + live_paths:
            try:
                size += path.stat().st_size
            except OSError:
                pass
        if _capture_is_locked(capture_id):
            state = "live"
        elif live_paths:
            state = "pending"
        else:
            state = str(metadata.get("state") or "closed")
        records.append({
            "capture_id": capture_id,
            "session": metadata.get("session_name", "unknown"),
            "pane": metadata.get("pane_id", "unknown"),
            "started": metadata.get("started_utc", ""),
            "ended": metadata.get("ended_utc"),
            "state": state,
            "segments": len({
                sequence
                for sequence, _ in (
                    archives.get(capture_id, [])
                    + live.get(capture_id, [])
                )
            }),
            "bytes": size,
            "cwd": metadata.get("initial_cwd", ""),
            "command": metadata.get("initial_command", ""),
        })
    records.sort(key=lambda item: str(item.get("started") or ""), reverse=True)
    return records


def resolve_capture(value: str) -> str:
    """Resolve an exact or unambiguous capture-ID prefix."""
    wanted = value.strip().lower()
    if not wanted:
        raise ValueError("capture id must be non-empty")
    matches = [
        record["capture_id"]
        for record in list_captures()
        if record["capture_id"].startswith(wanted)
    ]
    if not matches:
        raise ValueError(f"no such capture: {value}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous capture prefix: {value}")
    return matches[0]


def capture_paths(capture_id: str) -> list[tuple[int, Path, bool]]:
    """Ordered ``(sequence, path, compressed)`` entries for one capture.

    If a crash left both a final archive and its identical plaintext source,
    prefer the archive so a reader never emits that segment twice.
    """
    values: dict[int, tuple[Path, bool]] = {}
    for sequence, path in _live_index().get(capture_id, []):
        values[sequence] = (path, False)
    for sequence, path in _archive_index().get(capture_id, []):
        existing = values.get(sequence)
        if (
            existing is not None
            and not existing[1]
            and not _archive_matches_source(existing[0], path, zstd="zstd")
        ):
            # A readable plaintext duplicate is safer than a corrupt or
            # non-matching archive. Verification still reports the bad frame.
            continue
        values[sequence] = (path, True)
    return [
        (sequence, values[sequence][0], values[sequence][1])
        for sequence in sorted(values)
    ]


def verify_capture(capture_id: str, *, zstd: str = "zstd") -> dict[str, Any]:
    """Check archive frames and sequence continuity for a capture."""
    archives = _archive_index().get(capture_id, [])
    live = _live_index().get(capture_id, [])
    physical_sequences = {
        sequence for sequence, _ in archives + live
    }
    expected_sequences: set[int] = set()
    metadata = _load_metadata(capture_id)
    if metadata is not None:
        raw_segments = metadata.get("segments")
        if isinstance(raw_segments, list):
            for segment in raw_segments:
                if not isinstance(segment, dict):
                    continue
                try:
                    sequence = int(segment.get("sequence"))
                except (TypeError, ValueError):
                    continue
                if 0 <= sequence <= 999999:
                    expected_sequences.add(sequence)
    known_sequences = physical_sequences | expected_sequences
    contiguous = (
        set(range(max(known_sequences) + 1))
        if known_sequences
        else set()
    )
    missing = sorted(
        (expected_sequences | contiguous) - physical_sequences,
    )
    corrupt: list[str] = []
    pending = [str(path) for _, path in live]
    for _, path in archives:
        try:
            checked = subprocess.run(
                [zstd, "-tq", "--", str(path)],
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            corrupt.append(str(path))
            continue
        if checked.returncode != 0:
            corrupt.append(str(path))
    return {
        "capture_id": capture_id,
        "ok": not corrupt and not missing,
        "segments": len(physical_sequences),
        "missing_sequences": missing,
        "corrupt": corrupt,
        "pending_plaintext": pending,
    }
