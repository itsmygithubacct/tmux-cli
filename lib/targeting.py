"""Parse and format tmux targets: ``session[:window[.pane]]``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    session: str
    window: str | None = None
    pane: str | None = None

    def as_tmux_target(self) -> str:
        """Return the string form tmux accepts (``session:window.pane``)."""
        if self.window is None:
            # ``session:`` constrains to that session's active pane and avoids
            # tmux's name-prefix ambiguity that a bare name would allow.
            return f"{self.session}:"
        if self.pane is None:
            return f"{self.session}:{self.window}"
        return f"{self.session}:{self.window}.{self.pane}"

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
