"""Shared helpers for ``tb`` subcommand modules.

Lives in its own module so ``tb_cmds/__init__.py`` can import submodules at
the top of the file instead of after the helper definitions (avoiding the
forward-declaration smell where submodules imported from their own package's
__init__).
"""

from __future__ import annotations

from .. import sessions, targeting
from ..errors import SessionNotFound, UsageError
from ..targeting import Target


def parse_target(expr: str) -> Target:
    """Parse a target expression, mapping ValueError → UsageError."""
    try:
        return targeting.parse(expr)
    except ValueError as e:
        raise UsageError(str(e))


def require_target(expr: str) -> Target:
    """Parse + validate existence. Standard preamble for target-taking verbs.

    Raises ``UsageError`` for malformed expressions and ``SessionNotFound``
    when the session doesn't exist — both map to stable exit codes.
    """
    t = parse_target(expr)
    if not sessions.exists(t.session):
        raise SessionNotFound(f"no such session: {t.session}")
    return t
