"""tmux-browse — dashboard that exposes every local tmux session as a ttyd pane.

Single source of truth for the project version; ``tb.py`` and
``tmux_browse.py`` both import ``__version__`` from here so the two
surfaces can never drift.
"""

from __future__ import annotations

__version__ = "0.7.6.0"
__all__ = ["__version__"]
