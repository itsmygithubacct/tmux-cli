"""Thin wrapper around ``tmux`` for session enumeration, lifecycle, and I/O.

Exposes high-level helpers for the dashboard server AND the ``tb`` CLI. The
older positional-return helpers (``exists``, ``new``, ``kill``, …) retain
their signatures because they're called by ``lib/server.py``; richer
variants sit alongside with clearer return shapes.
"""

from __future__ import annotations

import subprocess
import time
from typing import TypedDict

from .errors import UsageError
from .targeting import Target


# -----------------------------------------------------------------------------
# Data types
# -----------------------------------------------------------------------------

class Session(TypedDict):
    name: str
    windows: int
    attached: int
    created: int  # epoch seconds
    activity: int  # epoch seconds — last activity


class PaneInfo(TypedDict, total=False):
    session: str
    window: str     # window index
    window_name: str
    pane: str       # pane index
    command: str    # #{pane_current_command}
    pid: int
    cwd: str
    width: int
    height: int
    active: bool


_SESSION_FORMAT = (
    "#{session_name}\t#{session_windows}\t#{session_attached}"
    "\t#{session_created}\t#{session_activity}\t#{session_group}"
)
_PANE_FORMAT = (
    "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}"
    "\t#{pane_current_command}\t#{pane_pid}\t#{pane_current_path}"
    "\t#{pane_width}\t#{pane_height}\t#{?pane_active,1,0}"
)


# -----------------------------------------------------------------------------
# Server + session listing
# -----------------------------------------------------------------------------

def server_running() -> bool:
    """True iff a tmux server socket is reachable.

    ``tmux list-sessions`` exits 1 for *both* "no server socket" and "server
    up, zero sessions" — tmux conflates them because the server exits when
    the last session dies. We distinguish by looking at stderr: only the
    no-server case prints the ``no server running`` banner.
    """
    try:
        r = subprocess.run(
            ["tmux", "list-sessions"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        # Server socket exists but the process isn't responding (memory
        # pressure, stuck client). From the dashboard's perspective the
        # right answer is "no usable server" — same as "not running".
        return False
    if r.returncode == 0:
        return True
    stderr = (r.stderr or "").lower()
    # "no server running on /tmp/tmux-1000/default" → no socket.
    # Anything else (e.g. "no sessions") → server present but empty.
    return "no server" not in stderr


def server_responsive(timeout: float = 2.0) -> bool:
    """Cheap liveness probe — ``tmux display-message`` with a short timeout.

    Distinguishes "tmux is up and answering" from "socket exists but the
    server isn't responding". Used by the dashboard to surface a banner
    when a memory-pressured server has gone unresponsive but the socket
    file is still on disk.
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "ok"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def list_sessions() -> list[Session]:
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", _SESSION_FORMAT],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Treat a hung tmux server like an absent one. Memory-pressured
        # boxes can leave the server unresponsive for tens of seconds;
        # raising would take down /api/sessions and blank the dashboard.
        return []
    if r.returncode != 0:
        return []
    # ttyd_wrap.sh creates a per-viewer grouped session (same session group,
    # different name) so each browser tab can size its own windows. Those
    # viewer sessions shouldn't appear in the dashboard or CLI listing as
    # if they were separate work — collapse each session group to just its
    # primary (the entry whose name equals the group name). Viewer-only
    # groups (primary already dead but a viewer is hanging on because a
    # browser tab is still attached) are dropped from the listing entirely
    # so they stop polluting the session list under their `<base>-v<pid>-<rand>`
    # name; ttyd_wrap.sh also actively kills those viewers when the base
    # disappears, so this is a defensive filter for transient cases.
    raw: list[tuple[Session, str]] = []  # (row, group)
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        name, windows, attached, created, activity, group = parts
        raw.append((
            {
                "name": name,
                "windows": int(windows),
                "attached": int(attached),
                "created": int(created),
                "activity": int(activity),
            },
            group,
        ))
    out: list[Session] = []
    for row, group in raw:
        if not group:
            out.append(row)
        elif row["name"] == group:
            # Primary session keeps its spot. Viewers are dropped below.
            out.append(row)
    out.sort(key=lambda s: s["name"])
    return out


def list_panes() -> list[PaneInfo]:
    """All panes across all sessions — one row per pane."""
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", _PANE_FORMAT],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    out: list[PaneInfo] = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 10:
            continue
        sess, wi, wn, pi, cmd, pid, cwd, w, h, active = parts
        out.append({
            "session": sess,
            "window": wi,
            "window_name": wn,
            "pane": pi,
            "command": cmd,
            "pid": int(pid),
            "cwd": cwd,
            "width": int(w),
            "height": int(h),
            "active": active == "1",
        })
    return out


def exists(session: str) -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", f"={session}"],
        capture_output=True, timeout=5,
    )
    return r.returncode == 0


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------

def _validate_name(name: str) -> None:
    if not name or any(c in name for c in " \t\n:."):
        raise UsageError(
            "session name must be non-empty and contain no whitespace, ':' or '.'",
        )


def kill(session: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["tmux", "kill-session", "-t", f"={session}"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout).strip()


def new_session(name: str, cwd: str | None = None, cmd: str | None = None,
                width: int = 200, height: int = 50) -> tuple[bool, str]:
    """Create a detached tmux session. The single canonical create path."""
    try:
        _validate_name(name)
    except UsageError as e:
        return False, str(e)
    if exists(name):
        return False, f"session '{name}' already exists"
    args = ["tmux", "new-session", "-d", "-s", name,
            "-x", str(width), "-y", str(height)]
    if cwd:
        args += ["-c", cwd]
    if cmd:
        args.append(cmd)
    r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        # Enable continuous log capture for hash-based idle detection.
        try:
            from . import session_logs
            session_logs.ensure_logging(name)
        except Exception:
            pass
        return True, ""
    return False, (r.stderr or r.stdout).strip()


def rename(old: str, new_name: str) -> tuple[bool, str]:
    _validate_name(new_name)
    if not exists(old):
        return False, f"no such session: {old}"
    if exists(new_name):
        return False, f"session '{new_name}' already exists"
    r = subprocess.run(
        ["tmux", "rename-session", "-t", f"={old}", new_name],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout).strip()


# -----------------------------------------------------------------------------
# Capture / read
# -----------------------------------------------------------------------------

def capture_target(target: Target, lines: int = 2000,
                   ansi: bool = False) -> tuple[bool, str]:
    """Single canonical capture. Accepts a Target; works for any pane."""
    if not exists(target.session):
        return False, f"no such session: {target.session}"
    cmd = ["tmux", "capture-pane", "-t", target.as_tmux_target(),
           "-p", "-J", "-S", f"-{lines}"]
    if ansi:
        cmd.append("-e")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, r.stdout


def session_activity(target: Target) -> int | None:
    r = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target.as_tmux_target(),
         "#{session_activity}"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def pane_current_command(target: Target) -> str | None:
    r = subprocess.run(
        ["tmux", "display-message", "-p", "-t", target.as_tmux_target(),
         "#{pane_current_command}"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


# -----------------------------------------------------------------------------
# Write: send-keys family
# -----------------------------------------------------------------------------

def enter_copy_mode(session: str) -> tuple[bool, str]:
    """Invoke the ``C-b [`` binding (put active pane into copy-mode)."""
    if not exists(session):
        return False, f"no such session: {session}"
    r = subprocess.run(
        ["tmux", "copy-mode", "-t", f"{session}:"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def zoom_pane(session: str) -> tuple[bool, str]:
    """Toggle zoom on the active pane (``C-b z`` / ``resize-pane -Z``).

    tmux's pane-zoom feature makes the current pane fill its window; a
    second invocation restores the layout.
    """
    if not exists(session):
        return False, f"no such session: {session}"
    r = subprocess.run(
        ["tmux", "resize-pane", "-t", f"{session}:", "-Z"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def send_literal(target: Target, text: str) -> tuple[bool, str]:
    """Send ``text`` verbatim. Newlines in ``text`` become literal newlines."""
    if not exists(target.session):
        return False, f"no such session: {target.session}"
    r = subprocess.run(
        ["tmux", "send-keys", "-t", target.as_tmux_target(), "-l", "--", text],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def send_keys(target: Target, *keys: str) -> tuple[bool, str]:
    """Send one or more tmux key names (e.g. ``Enter``, ``C-c``, ``F5``)."""
    if not exists(target.session):
        return False, f"no such session: {target.session}"
    r = subprocess.run(
        ["tmux", "send-keys", "-t", target.as_tmux_target(), *keys],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


def type_line(target: Target, text: str) -> tuple[bool, str]:
    """Send ``text`` literally then press Enter."""
    ok, err = send_literal(target, text)
    if not ok:
        return ok, err
    return send_keys(target, "Enter")


def paste_buffer(target: Target, text: str) -> tuple[bool, str]:
    """Use load-buffer + paste-buffer for pasting arbitrary text (survives
    shell auto-indent / bracketed-paste hooks better than send-keys -l)."""
    if not exists(target.session):
        return False, f"no such session: {target.session}"
    # ``load-buffer -`` reads text from stdin into an auto-named buffer.
    r = subprocess.run(
        ["tmux", "load-buffer", "-"],
        input=text, capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    r = subprocess.run(
        ["tmux", "paste-buffer", "-d", "-t", target.as_tmux_target()],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, ""


# -----------------------------------------------------------------------------
# Wait / observe
# -----------------------------------------------------------------------------

def wait_idle(target: Target, idle_sec: float,
              timeout_sec: float = 0) -> tuple[bool, str]:
    """Block until pane has been quiet for ``idle_sec`` seconds.

    Quiet = no change in ``session_activity`` *and* no change in capture hash
    (the activity timer alone skips output that tmux doesn't mark as
    activity). ``timeout_sec`` of 0 means no timeout.
    """
    if not exists(target.session):
        return False, f"no such session: {target.session}"

    deadline = time.time() + timeout_sec if timeout_sec > 0 else None
    last_hash = None
    last_change = time.time()
    poll = 0.2
    while True:
        ok, content = capture_target(target, lines=200)
        if not ok:
            return False, content
        h = hash(content)
        now = time.time()
        if h != last_hash:
            last_hash = h
            last_change = now
        if now - last_change >= idle_sec:
            return True, ""
        if deadline and now >= deadline:
            return False, f"timed out after {timeout_sec}s"
        time.sleep(poll)




# ---------------------------------------------------------------------------
# Pane snapshots (for the dashboard preview tiles)
# ---------------------------------------------------------------------------
#
# Cheap "what's on this pane right now" capture for the index page.
# Trades freshness for cost: ``capture-pane`` per session per request
# would dominate the response time on hosts with many sessions, so
# results are cached for ``_SNAPSHOT_TTL_SEC`` and served stale
# before the cap. The 200 ms per-request budget limits worst-case
# damage when many sessions are stale at once.
#
# Why ``-e`` (ANSI preserved) but no ``-J`` (no joining wrapped lines):
# the dashboard renders a small monospace tile and we want what the
# user actually sees, not a reflowed canonical form. The tile is
# narrow enough that a hard wrap-at-N looks better than the joined
# alternative.

_SNAPSHOT_TTL_SEC = 10
_snapshot_cache: dict[str, tuple[int, str]] = {}


def capture_pane_snapshot(name: str, lines: int = 20) -> str:
    """Best-effort scrollback dump for the dashboard preview.

    Returns the raw ANSI-bearing text (last ``lines`` rows). Empty
    string on any failure — never raises into request flow."""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-e", "-p",
             "-t", name, "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def get_cached_snapshot(name: str, now: int | None = None,
                        lines: int = 20) -> str:
    """Cached accessor — re-captures only if entry is older than TTL."""
    now = now if now is not None else int(time.time())
    cached = _snapshot_cache.get(name)
    if cached and (now - cached[0]) < _SNAPSHOT_TTL_SEC:
        return cached[1]
    text = capture_pane_snapshot(name, lines=lines)
    _snapshot_cache[name] = (now, text)
    return text


def gc_snapshots(active_names: set[str]) -> None:
    """Drop cache entries for sessions that no longer exist."""
    for stale in [n for n in _snapshot_cache if n not in active_names]:
        _snapshot_cache.pop(stale, None)
