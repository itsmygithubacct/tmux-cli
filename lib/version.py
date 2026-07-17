"""Single source of truth for the project version.

``lib`` is a PEP 420 namespace package (no ``__init__.py``) so the tmux-browse
dashboard repo can extend the same ``lib`` package with its server-only
modules. The version therefore lives in this dedicated module; ``tb.py`` and
the dashboard both read it from here.
"""

from __future__ import annotations

__version__ = "0.7.9.5"
