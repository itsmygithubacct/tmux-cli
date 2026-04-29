"""Thin wrappers over ``git submodule`` for extension install/update.

Post-E2 the agent extension ships as a submodule at
``extensions/agent/``. A fresh ``git clone`` of core leaves that
directory empty; E3's Config-pane **Download and enable** button
calls :func:`submodule_init` to materialise it. The **Update** button
calls :func:`submodule_update_remote` to pull the latest extension
commit on its tracked branch.

These functions shell out to ``git`` and return ``(ok, stderr_or_stdout)``
so the UI can surface what happened. 120-second timeout is generous
for a cold clone over a slow link; happy-path calls finish in
seconds.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import config


__all__ = [
    "is_submodule_path",
    "submodule_init",
    "submodule_update_remote",
]


def _gitmodules_path() -> Path:
    return config.PROJECT_DIR / ".gitmodules"


def is_submodule_path(name: str) -> bool:
    """Return True iff ``extensions/<name>`` is declared in ``.gitmodules``.

    Reads the file as text rather than invoking ``git config -f`` so
    the check works in test fixtures that don't have a git repo
    initialised.
    """
    gm = _gitmodules_path()
    if not gm.is_file():
        return False
    try:
        text = gm.read_text(encoding="utf-8")
    except OSError:
        return False
    return f"path = extensions/{name}" in text


def submodule_init(name: str, *, timeout: float = 120.0) -> tuple[bool, str]:
    """Run ``git submodule update --init -- extensions/<name>``.

    Materialises a submodule directory that a non-recursive clone left
    empty. Returns ``(ok, output)`` where ``output`` is stderr on
    failure (empty string on success). On timeout, returns
    ``(False, "timed out after <N>s")``.
    """
    try:
        r = subprocess.run(
            ["git", "submodule", "update", "--init", "--",
             f"extensions/{name}"],
            cwd=config.PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (False, f"timed out after {timeout:.0f}s")
    except FileNotFoundError:
        return (False, "git not on PATH")
    if r.returncode == 0:
        return (True, "")
    return (False, (r.stderr or r.stdout).strip())


def submodule_update_remote(
    name: str, *, timeout: float = 120.0,
) -> tuple[bool, str]:
    """Run ``git submodule update --remote -- extensions/<name>``.

    Advances the submodule to the tip of its tracked branch (set in
    ``.gitmodules`` via ``submodule.<name>.branch``). The core repo's
    working tree gets a ``M`` entry for the submodule pointer, which
    the operator commits after confirming the update is good.
    """
    try:
        r = subprocess.run(
            ["git", "submodule", "update", "--remote", "--",
             f"extensions/{name}"],
            cwd=config.PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (False, f"timed out after {timeout:.0f}s")
    except FileNotFoundError:
        return (False, "git not on PATH")
    if r.returncode == 0:
        return (True, "")
    return (False, (r.stderr or r.stdout).strip())
