"""Spawn, track, and stop one ttyd process per tmux session.

Each managed ttyd is identified by a PID file at ``$STATE_DIR/pids/<session>.pid``.
A log per session lives at ``$STATE_DIR/logs/<session>.log``.
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from . import config, ports


# Per-session spawn locks. Without this, two concurrent start() calls for
# the same session both pass the read_pid() check and race to Popen ttyd,
# with the loser hitting EADDRINUSE on bind. The outer lock guards the
# dict; the per-session lock serializes start() for that specific session.
_start_locks_mutex = threading.Lock()
_start_locks: dict[str, threading.Lock] = {}

# Cache of (bind_addr → interface-name | None). _ttyd_interface is called
# on every spawn; the underlying `ip addr` / `ifconfig` call is the
# expensive part and the mapping changes rarely (only when the operator
# reconfigures networking). Entries stay for the life of the process.
_iface_cache_lock = threading.Lock()
_iface_cache: dict[str, str | None] = {}


def _start_lock(session: str) -> threading.Lock:
    with _start_locks_mutex:
        lock = _start_locks.get(session)
        if lock is None:
            lock = threading.Lock()
            _start_locks[session] = lock
        return lock


def _atomic_write(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` via tempfile+rename so readers never see a
    half-written file. write_text() truncates and writes — a concurrent
    read can catch zero bytes between truncate and write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def _pidfile(session: str) -> Path:
    return config.PID_DIR / f"{_safe(session)}.pid"


def _schemefile(session: str) -> Path:
    return config.PID_DIR / f"{_safe(session)}.scheme"


def _logfile(session: str) -> Path:
    return config.LOG_DIR / f"{_safe(session)}.log"


def _safe(name: str) -> str:
    """Reversible, collision-free filename for a session name.

    Percent-encodes every non-[A-Za-z0-9_-] byte; two distinct names can
    never produce the same basename (unlike the old "replace-with-_"
    scheme, which collided ``foo bar`` with ``foo_bar``).
    """
    return urllib.parse.quote(name, safe="-_")


def _unsafe(name: str) -> str:
    """Decode a pid/log basename back to the original session name."""
    return urllib.parse.unquote(name)


def _pid_is_ttyd(pid: int) -> bool:
    """On Linux, confirm the process name is actually ``ttyd`` — guards
    against a recycled PID being mistaken for our old ttyd process.

    Returns True for "definitely ttyd" or "indeterminate" (transient
    /proc read failure that isn't ENOENT). Only returns False when we
    can affirmatively show the process is gone or is not a ttyd. Being
    permissive here keeps a transient /proc hiccup from triggering
    pidfile deletion of a live process.
    """
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip() == "ttyd"
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _pid_alive(pid: int) -> bool:
    """Return True iff the PID is alive AND looks like one of our ttyds.

    Distinguishes "definitely dead" (ESRCH / ENOENT) from "can't tell"
    (EPERM, transient OSError). The latter is treated as alive — a
    misclassification here means a stale pidfile lingers one cycle
    longer; the inverse misclassification permanently orphans a live
    ttyd from the dashboard's view.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM (different uid) or other transient — assume alive.
        return True
    if Path("/proc").is_dir() and not _pid_is_ttyd(pid):
        return False
    return True


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _probe_host(bind_addr: str | None) -> str:
    raw = (bind_addr or "").strip()
    if not raw or raw in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    if raw == "localhost":
        return "127.0.0.1"
    try:
        infos = socket.getaddrinfo(raw, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return raw
    for family, _, _, _, sockaddr in infos:
        host = sockaddr[0]
        if family == socket.AF_INET:
            return host
    return infos[0][4][0] if infos else raw


def _port_listening_on(port: int, host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for family, socktype, proto, _, sockaddr in infos:
        with socket.socket(family, socktype, proto) as s:
            s.settimeout(0.3)
            if s.connect_ex(sockaddr) == 0:
                return True
    return False


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def _iface_from_ip_addr(targets: set[str]) -> str | None:
    """Parse Linux `ip -o addr show` to find the interface owning one of
    the IP addresses in ``targets``. Returns None if `ip` isn't available
    or no match is found.
    """
    try:
        output = subprocess.check_output(
            ["ip", "-o", "addr", "show"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    except Exception:
        return None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        ifname = parts[1]
        address = parts[3].split("/", 1)[0]
        if address in targets:
            return ifname
    return None


def _iface_from_ifconfig(targets: set[str]) -> str | None:
    """Parse BSD / macOS `ifconfig -a` to find the interface owning one of
    the IP addresses in ``targets``. Returns None on missing tool or no match.
    """
    try:
        output = subprocess.check_output(
            ["ifconfig", "-a"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    except Exception:
        return None
    current_if: str | None = None
    for line in output.splitlines():
        # New interface stanzas start flush-left: "en0: flags=..."
        if line and not line[0].isspace() and ":" in line:
            current_if = line.split(":", 1)[0].strip() or None
            continue
        if current_if is None:
            continue
        # Indented "inet A.B.C.D ..." or "inet6 ::1 ..." lines.
        stripped = line.strip()
        if stripped.startswith("inet "):
            addr = stripped.split()[1]
            # Strip any %zone-id (e.g. fe80::1%en0)
            addr = addr.split("%", 1)[0]
            if addr in targets:
                return current_if
        elif stripped.startswith("inet6 "):
            addr = stripped.split()[1].split("%", 1)[0]
            if addr in targets:
                return current_if
    return None


def _ttyd_interface(bind_addr: str | None) -> str | None:
    """Map a dashboard bind address to ttyd's `--interface` argument.

    ttyd binds by interface name, not by IP address. For wildcard binds we
    intentionally omit `--interface`; for loopback or a concrete host/IP we
    resolve the owning interface via `ip -o addr show` (Linux) with a
    fallback to `ifconfig -a` (BSD / macOS).

    Results are cached per-bind-address for the process lifetime — the
    underlying routing table rarely changes and spawns would otherwise
    shell out on every API hit.
    """
    raw = (bind_addr or "").strip()
    if not raw or raw in {"0.0.0.0", "::"}:
        return None
    if raw in {"127.0.0.1", "::1", "localhost"}:
        return "lo"

    with _iface_cache_lock:
        if raw in _iface_cache:
            return _iface_cache[raw]

    targets = {raw}
    try:
        infos = socket.getaddrinfo(raw, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        infos = []
    for _, _, _, _, sockaddr in infos:
        host = sockaddr[0]
        if host:
            targets.add(host)

    resolved = _iface_from_ip_addr(targets)
    if resolved is None:
        resolved = _iface_from_ifconfig(targets)

    with _iface_cache_lock:
        _iface_cache[raw] = resolved
    return resolved


def _scan_ttyd_for_session(session: str, proc_root: Path | None = None) -> int | None:
    """Find a live ttyd process whose argv references this session's wrapper.

    Used to re-link an orphan ttyd (pidfile lost but process still alive)
    back to its session, so a transient pidfile loss doesn't leave the
    dashboard reporting the session as down forever. ``proc_root`` is
    a test seam — production always uses ``/proc``.
    """
    proc = proc_root if proc_root is not None else Path("/proc")
    if not proc.is_dir():
        return None
    wrap = str(config.TTYD_WRAP)
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            with open(entry / "comm") as f:
                if f.read().strip() != "ttyd":
                    continue
            with open(entry / "cmdline", "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\x00") if a]
        except OSError:
            continue
        # Expect: ttyd ... -W bash <wrap> <session>. Confirm session is
        # the arg directly after wrap so we don't false-match on a
        # session whose name happens to appear elsewhere in argv.
        try:
            i = argv.index(wrap)
        except ValueError:
            continue
        if i + 1 < len(argv) and argv[i + 1] == session:
            try:
                return int(entry.name)
            except ValueError:
                continue
    return None


def _scheme_from_argv(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\x00")
    except OSError:
        return "http"
    return "https" if b"--ssl" in argv else "http"


def _reconcile_pidfile(session: str) -> int | None:
    """Restore a missing pidfile when a live ttyd matches this session.

    Triggered when ``read_pid`` finds no pidfile but the dashboard still
    has a port assignment for the session. If that port is listening
    AND a ttyd process owns the session's wrapper argv, we rewrite the
    pidfile (and scheme sidecar) so subsequent ``read_pid`` calls hit
    the fast path. Self-heals the "pidfile vanished, ttyd still alive"
    failure mode that otherwise leaves the dashboard's iframe blank
    until the user clicks Launch.
    """
    port = ports.get(session)
    if port is None or not _port_listening(port):
        return None
    pid = _scan_ttyd_for_session(session)
    if pid is None:
        return None
    try:
        _atomic_write(_pidfile(session), f"{pid}\n")
        _atomic_write(_schemefile(session), f"{_scheme_from_argv(pid)}\n")
    except OSError:
        return pid
    return pid


def read_pid(session: str) -> int | None:
    pf = _pidfile(session)
    if not pf.is_file():
        return _reconcile_pidfile(session)
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return None
    if not _pid_alive(pid):
        pf.unlink(missing_ok=True)
        _schemefile(session).unlink(missing_ok=True)
        return None
    return pid


def read_scheme(session: str) -> str:
    """Return ``"https"`` or ``"http"`` for the session's currently running
    ttyd. Defaults to ``"http"`` when no sidecar exists (e.g. started by an
    older version, or never started)."""
    sf = _schemefile(session)
    if not sf.is_file():
        return "http"
    try:
        v = sf.read_text().strip()
    except OSError:
        return "http"
    return "https" if v == "https" else "http"


def is_running(session: str) -> bool:
    port = ports.get(session)
    if port is None:
        return read_pid(session) is not None
    # Prefer the cheap pid check; fall back to port probe if no pidfile.
    if read_pid(session) is not None:
        return True
    return _port_listening(port)


def _spawn_ttyd(name: str, port: int, argv_tail: list[str],
                tls_paths: tuple[Path, Path] | None = None,
                bind_addr: str | None = None) -> dict:
    config.ensure_dirs()
    probe_host = _probe_host(bind_addr)

    if _port_listening_on(port, probe_host):
        return {"ok": True, "port": port, "already": True, "note": "port already in use"}

    ttyd = config.ttyd_executable()
    argv = [ttyd, "-p", str(port)]
    interface = _ttyd_interface(bind_addr)
    if bind_addr and not interface and bind_addr.strip() not in {"", "0.0.0.0", "::"}:
        return {
            "ok": False,
            "error": (
                f"cannot map bind address {bind_addr!r} to a ttyd interface; "
                "use 0.0.0.0, 127.0.0.1, localhost, or an address returned by `ip addr`"
            ),
        }
    if interface:
        argv += ["-i", interface]
    if tls_paths is not None:
        cert, key = tls_paths
        argv += ["--ssl", "--ssl-cert", str(cert), "--ssl-key", str(key)]
    argv += argv_tail

    log = _logfile(name)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab", buffering=0) as lf:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "error": (
                    "ttyd binary not found. Install it with `tmux-browse install-ttyd` "
                    "or place a ttyd executable on $PATH."
                ),
            }
        except Exception as e:
            return {"ok": False, "error": f"spawn failed: {e}"}

    scheme = "https" if tls_paths is not None else "http"
    _atomic_write(_pidfile(name), f"{proc.pid}\n")
    _atomic_write(_schemefile(name), f"{scheme}\n")

    # Give ttyd a moment to bind, then verify.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if _port_listening_on(port, probe_host):
            return {"ok": True, "pid": proc.pid, "port": port, "already": False,
                    "scheme": scheme, "name": name}
        if proc.poll() is not None:
            return {
                "ok": False,
                "error": f"ttyd exited immediately (rc={proc.returncode}); see {log}",
            }
        time.sleep(0.05)
    return {"ok": True, "pid": proc.pid, "port": port, "already": False,
            "scheme": scheme, "note": "spawned but not yet listening", "name": name}


def start(session: str,
          tls_paths: tuple[Path, Path] | None = None,
          bind_addr: str | None = None) -> dict:
    """Ensure a ttyd instance is running for ``session``. Idempotent.

    When ``tls_paths`` is set, ttyd is spawned with ``--ssl --ssl-cert --ssl-key``
    so the dashboard (which is also serving HTTPS) can embed it without
    tripping browser mixed-content blocking on the iframe's ``ws://``.

    Thread-safe for the same session: concurrent callers for the same
    name serialize on a per-session lock, so only one actually spawns ttyd.
    """
    config.ensure_dirs()

    with _start_lock(session):
        existing_pid = read_pid(session)
        port = ports.assign(session)
        if existing_pid is not None:
            return {"ok": True, "pid": existing_pid, "port": port, "already": True,
                    "scheme": read_scheme(session)}

        wrap = str(config.TTYD_WRAP)
        if not Path(wrap).is_file():
            return {"ok": False, "error": f"wrapper missing: {wrap}"}
        return _spawn_ttyd(
            session,
            port,
            ["-W", "bash", wrap, session],
            tls_paths=tls_paths,
            bind_addr=bind_addr,
        )


def start_raw(tls_paths: tuple[Path, Path] | None = None,
              bind_addr: str | None = None) -> dict:
    """Spawn a one-off raw ttyd shell not attached to tmux.

    The name includes 8 hex bytes of randomness so two clicks within the
    same millisecond never collide on pidfile / per-session-lock identity.
    The port is persisted via ``ports.assign`` so the shell shows up in
    the dashboard's session list and survives a page reload — the
    cleanup path on the dashboard side calls ``ports.release`` after
    stopping the ttyd.
    """
    name = f"raw-shell-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    port = ports.assign(name)
    argv_tail = ["-W", "bash", "-lc", 'exec "${SHELL:-bash}" -il']
    return _spawn_ttyd(name, port, argv_tail, tls_paths=tls_paths, bind_addr=bind_addr)


def stop(session: str) -> dict:
    # Raw shells own their port assignment too; release it on stop so a
    # subsequent ``ports.assign`` doesn't trip "already assigned" or have
    # the dashboard show a stale row. Tmux sessions keep their port (the
    # ttyd may be re-spawned later); only raw shells get the release.
    is_raw = session.startswith("raw-shell-")
    pid = read_pid(session)
    if pid is None:
        _pidfile(session).unlink(missing_ok=True)
        _schemefile(session).unlink(missing_ok=True)
        if is_raw:
            ports.release(session)
        return {"ok": True, "already_stopped": True}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        _pidfile(session).unlink(missing_ok=True)
        _schemefile(session).unlink(missing_ok=True)
        if is_raw:
            ports.release(session)
        return {"ok": False, "error": f"kill failed: {e}"}
    # Wait briefly; escalate to SIGKILL if needed.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.05)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _pidfile(session).unlink(missing_ok=True)
    _schemefile(session).unlink(missing_ok=True)
    if is_raw:
        ports.release(session)
    return {"ok": True, "pid": pid}


def stop_all() -> int:
    killed = 0
    for pf in config.PID_DIR.glob("*.pid"):
        session = _unsafe(pf.stem)
        if stop(session).get("ok"):
            killed += 1
    return killed


def gc_orphans() -> dict:
    """Sweep stale state — call at dashboard startup.

    Removes pidfiles whose process is dead and prunes port assignments
    whose tmux session no longer exists. Returns counts for logging.
    Safe to run while other threads call start()/stop() (each operation
    takes its own lock / acquires via read_pid's liveness check).
    """
    stale_pids = 0
    for pf in config.PID_DIR.glob("*.pid"):
        session = _unsafe(pf.stem)
        # read_pid() already unlinks dead PIDs as a side-effect.
        if read_pid(session) is None:
            stale_pids += 1

    # Prune port assignments for sessions that no longer exist in tmux.
    # Lazy import avoids pulling sessions (with its subprocess surface)
    # into a hot-path import cycle for ttyd.
    from . import sessions
    active = {s["name"] for s in sessions.list_sessions()}
    # Raw shells aren't tmux sessions but their port assignment is still
    # live as long as their pidfile exists; keep them in the active set
    # so prune() doesn't strip them.
    for pf in config.PID_DIR.glob("raw-shell-*.pid"):
        name = _unsafe(pf.stem)
        if read_pid(name) is not None:
            active.add(name)
    dropped = ports.prune(active)
    return {"stale_pids_removed": stale_pids, "ports_dropped": len(dropped)}


def status_all() -> list[dict]:
    """Return [{session, port, pid, running}] for every known assignment + pidfile."""
    out: list[dict] = []
    seen: set[str] = set()
    for session, port in ports.all_assignments().items():
        seen.add(session)
        pid = read_pid(session)
        out.append({
            "session": session,
            "port": port,
            "pid": pid,
            "running": pid is not None,
        })
    # Orphan pidfiles without a port assignment (shouldn't happen, but surface it)
    for pf in config.PID_DIR.glob("*.pid"):
        session = _unsafe(pf.stem)
        if session not in seen:
            pid = read_pid(session)
            out.append({
                "session": session,
                "port": None,
                "pid": pid,
                "running": pid is not None,
            })
    out.sort(key=lambda r: r["session"])
    return out
