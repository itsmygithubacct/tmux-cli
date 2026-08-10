"""Dashboard UI config file helpers."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import tempfile
from typing import Any

from . import config


IDLE_SOUND_CHOICES: tuple[str, ...] = (
    "beep", "chime", "knock", "bell", "blip", "ding",
)

# How the dashboard receives session-list updates.
#   "sse"  — long-lived /api/sessions/stream Server-Sent Events
#            connection; updates land within ~1s of server-side
#            changes. Default in modern browsers.
#   "poll" — periodic /api/sessions GETs at refresh_seconds cadence.
#            Fallback for environments where SSE is blocked (some
#            corporate proxies) or when EventSource isn't available.
REFRESH_STRATEGY_CHOICES: tuple[str, ...] = ("sse", "poll")


DEFAULTS: dict[str, Any] = {
    "auto_refresh": False,
    "refresh_seconds": 5,
    "refresh_strategy": "sse",
    "hot_loop_idle_seconds": 5,
    "agent_max_steps": 20,
    "global_daily_token_budget": 0,
    "launch_on_expand": True,
    "default_ttyd_height_vh": 70,
    "default_ttyd_min_height_px": 200,
    # Approximate xterm.js cell size used by the dashboard's "fit tmux
    # to iframe" buttons. Defaults match ttyd's default 15px monospace
    # font on a 1× DPI display (~7.7 px wide × 17 px tall, rounded down
    # to slightly under-fill rather than over-flow). Operators on
    # different fonts / browser zoom can tune.
    "ttyd_cell_width_px": 7.7,
    "ttyd_cell_height_px": 17,
    "day_mode": False,
    "idle_sound": "bell",
    "show_topbar": True,
    "show_topbar_title": True,
    "show_topbar_count": True,
    "show_topbar_new_session": True,
    "show_topbar_raw_ttyd": False,
    "show_topbar_refresh": False,
    "show_topbar_restart": False,
    "show_topbar_os_restart": True,
    "show_launch_codex": False,
    "show_launch_codex_yolo": False,
    "show_launch_kimi": False,
    "show_launch_kimi_yolo": False,
    "show_launch_monitor": False,
    "show_launch_top": False,
    "launch_cwd": "",
    "launch_ask_name": True,
    "launch_open_tab": False,
    "show_topbar_status": False,
    "show_summary_row": True,
    "show_pane_snapshot": True,
    "show_summary_name": True,
    "show_summary_arrow": True,
    "furl_side_by_side": True,
    "resize_row_together": True,
    "show_body_actions": False,
    "show_footer": True,
    "show_inline_messages": True,
    "show_attached_badge": False,
    "show_window_badge": False,
    "show_port_badge": False,
    "show_idle_text": True,
    "show_idle_alert_button": False,
    "show_wc_idle_icon": True,
    "show_wc_scroll_icon": True,
    "show_summary_open": False,
    "show_summary_log": False,
    "show_wc_log_icon": True,
    "show_summary_scroll": False,
    "show_summary_split": False,
    "show_summary_hide": False,
    "show_wc_hide_icon": True,
    "show_summary_reorder": False,
    "show_summary_move": False,
    "show_wc_move_icon": True,
    "show_agents_pane": False,
    "show_wc_close": True,
    "show_wc_maximize": True,
    "show_wc_minimize": False,
    # Iframe size step buttons. ±W / ±H sit in the window-chrome row
    # left of the maximize square. The plus side ships on by default
    # (the common operation: "this pane is too small"); the minus
    # side ships off so the chrome stays compact for operators who
    # rarely need to shrink a pane.
    "show_wc_step_plus_w": True,
    "show_wc_step_plus_h": True,
    "show_wc_step_minus_w": False,
    "show_wc_step_minus_h": False,
    "show_body_launch": True,
    "show_body_stop": True,
    "show_body_kill": True,
    "show_body_send_bar": False,
    "show_body_phone_keys": False,
    "show_body_hot_buttons": True,
    "show_hot_loop_toggles": True,
}


_BOOL_KEYS = {
    key for key, value in DEFAULTS.items() if isinstance(value, bool)
}
_INT_RANGES = {
    "refresh_seconds": (1, 300),
    "hot_loop_idle_seconds": (1, 3600),
    "agent_max_steps": (1, 1000),
    "default_ttyd_height_vh": (20, 95),
    "default_ttyd_min_height_px": (120, 900),
    "global_daily_token_budget": (0, 100_000_000),
}

_FLOAT_RANGES = {
    "ttyd_cell_width_px": (4.0, 20.0),
    "ttyd_cell_height_px": (8.0, 40.0),
}


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _coerce_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _coerce_choice(value: Any, default: str, choices: tuple[str, ...]) -> str:
    if isinstance(value, str) and value in choices:
        return value
    return default


def normalize(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULTS)
    for key in _BOOL_KEYS:
        out[key] = _coerce_bool(raw.get(key), DEFAULTS[key])
    for key, (lo, hi) in _INT_RANGES.items():
        out[key] = _coerce_int(raw.get(key), DEFAULTS[key], lo, hi)
    for key, (lo, hi) in _FLOAT_RANGES.items():
        out[key] = _coerce_float(raw.get(key), DEFAULTS[key], lo, hi)
    out["idle_sound"] = _coerce_choice(raw.get("idle_sound"), DEFAULTS["idle_sound"], IDLE_SOUND_CHOICES)
    out["refresh_strategy"] = _coerce_choice(raw.get("refresh_strategy"), DEFAULTS["refresh_strategy"], REFRESH_STRATEGY_CHOICES)
    out["launch_cwd"] = str(raw.get("launch_cwd") or "").strip()
    return out


def _load_unlocked() -> dict[str, Any]:
    """Read the last complete config snapshot without taking the write lock."""
    try:
        raw = json.loads(config.DASHBOARD_CONFIG_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return dict(DEFAULTS)
    return normalize(raw)


@contextmanager
def _write_lock():
    """Serialize writers on a stable inode beside the replaced data file."""
    config.ensure_dirs()
    lock_path = config.DASHBOARD_CONFIG_FILE.with_name(
        config.DASHBOARD_CONFIG_FILE.name + ".lock",
    )
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _save_unlocked(normalized: dict[str, Any]) -> None:
    """Durably replace the config while the caller holds ``_write_lock``."""
    payload = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{config.DASHBOARD_CONFIG_FILE.name}.",
        suffix=".tmp",
        dir=str(config.DASHBOARD_CONFIG_FILE.parent),
    )
    tmp = config.DASHBOARD_CONFIG_FILE.with_name(os.path.basename(raw_tmp))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, config.DASHBOARD_CONFIG_FILE)
        directory_fd = os.open(
            config.DASHBOARD_CONFIG_FILE.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def load() -> dict[str, Any]:
    # Writers publish with os.replace(), so readers see either the old or the
    # new complete snapshot and do not need to contend on the write lock.
    config.ensure_dirs()
    return _load_unlocked()


def save(raw: Any) -> dict[str, Any]:
    normalized = normalize(raw)
    with _write_lock():
        _save_unlocked(normalized)
    return normalized


def update_values(values: dict[str, Any]) -> dict[str, Any]:
    """Apply one key batch without losing a concurrent independent update."""
    with _write_lock():
        current = _load_unlocked()
        current.update(values)
        normalized = normalize(current)
        _save_unlocked(normalized)
        return normalized


def set_value(key: str, value: Any) -> dict[str, Any]:
    """Set one key without losing a concurrent independent key update."""
    return update_values({key: value})
