"""Parse and format tmux targets: ``session[:window[.pane]]``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    session: str
    window: str | None = None
    pane: str | None = None

    def as_tmux_target(self) -> str:
        """Return the string form tmux accepts (``=session:window.pane``).

        The session name is prefixed with ``=`` so tmux matches it *exactly*
        rather than by name-prefix or fnmatch. Without it, targeting ``web``
        while only ``web2`` exists would silently resolve to ``web2``; the
        lifecycle ops (``exists``/``kill``/``rename``) already anchor with
        ``=`` and the I/O verbs must agree so a command never lands in the
        wrong pane.
        """
        if self.window is None:
            # Trailing ``:`` constrains to the session's active pane.
            return f"={self.session}:"
        if self.pane is None:
            return f"={self.session}:{self.window}"
        return f"={self.session}:{self.window}.{self.pane}"

    def __str__(self) -> str:
        return self.as_tmux_target()


def parse(expr: str) -> Target:
    if not expr:
        raise ValueError("empty target")
    session = expr
    window: str | None = None
    pane: str | None = None
    if ":" in expr:
        session, rest = expr.split(":", 1)
        if rest:
            if "." in rest:
                window, pane = rest.split(".", 1)
            else:
                window = rest
    if not session:
        raise ValueError(f"no session in target: {expr!r}")
    return Target(session=session, window=window or None, pane=pane or None)
