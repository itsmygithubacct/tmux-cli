"""Shared paths and defaults for tmux-browse."""

from pathlib import Path

DASHBOARD_PORT = 8096
TTYD_PORT_START = 7700
TTYD_PORT_END = 7799  # inclusive — 100 slots

PROJECT_DIR = Path(__file__).resolve().parent.parent
TTYD_WRAP = PROJECT_DIR / "bin" / "ttyd_wrap.sh"

STATE_DIR = Path.home() / ".tmux-browse"
PORTS_FILE = STATE_DIR / "ports.json"
DASHBOARD_FILE = STATE_DIR / "dashboard.json"
DASHBOARD_CONFIG_FILE = STATE_DIR / "dashboard-config.json"
AGENT_LOG_DIR = STATE_DIR / "agent-logs"
AGENT_CONVERSATIONS_DIR = STATE_DIR / "agent-conversations"
AGENT_WORKFLOWS_FILE = STATE_DIR / "agent-workflows.json"
AGENT_HOOKS_FILE = STATE_DIR / "agent-hooks.json"
AGENT_NOTIFICATIONS_FILE = STATE_DIR / "agent-notifications.jsonl"
AGENT_CONDUCTOR_FILE = STATE_DIR / "agent-conductor.json"
AGENT_CONDUCTOR_LOG = STATE_DIR / "agent-conductor.jsonl"
AGENT_CONTEXT_DIR = STATE_DIR / "agent-contexts"
AGENT_KB_DIR = STATE_DIR / "agent-kb"
CONFIG_LOCK_FILE = STATE_DIR / "config-lock-secret"
PID_DIR = STATE_DIR / "pids"
LOG_DIR = STATE_DIR / "logs"
TTYD_BIN = Path.home() / ".local" / "bin" / "ttyd"


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def ttyd_executable() -> str:
    """Return path to the ttyd binary — prefers ~/.local/bin/ttyd if present."""
    if TTYD_BIN.is_file():
        return str(TTYD_BIN)
    return "ttyd"
