"""Registration shape returned by an extension's ``register()`` entry
point, plus helpers for merging multiple registrations into the core's
live surface.

A registration is a plain dataclass describing everything an extension
contributes: HTTP routes, CLI verbs, template-slot HTML, static JS
paths, and startup/shutdown callbacks. The core merges those at server
start; the merged tables feed the ``ThreadingHTTPServer`` and the
template renderer.

Collisions raise at registration time (fail-closed): two extensions
trying to bind the same HTTP route, CLI verb, or template slot is an
operator-visible error, not a silent last-one-wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# HTTP handlers in tmux-browse are bound methods on ``server.Handler``
# with signatures ``(self, parsed_url)`` for GET and
# ``(self, parsed_url, body)`` for POST. Extensions hand back plain
# callables with the same shape.
HandlerGet = Callable[[Any, Any], None]
HandlerPost = Callable[[Any, Any, dict], None]


@dataclass
class Registration:
    """What an extension contributes to core at load time."""

    name: str
    get_routes: dict[str, HandlerGet] = field(default_factory=dict)
    post_routes: dict[str, HandlerPost] = field(default_factory=dict)
    cli_verbs: dict[str, Callable] = field(default_factory=dict)
    ui_blocks: dict[str, str] = field(default_factory=dict)
    static_js: list[Path] = field(default_factory=list)
    startup: list[Callable[[Any], None]] = field(default_factory=list)
    shutdown: list[Callable[[], None]] = field(default_factory=list)


class RegistryConflict(ValueError):
    """Raised when two extensions try to claim the same route, verb, or
    slot. Surfaces with the names of both extensions in the message."""


@dataclass
class MergedRegistry:
    """Accumulator for the union of every loaded extension's registration."""

    get_routes: dict[str, HandlerGet] = field(default_factory=dict)
    post_routes: dict[str, HandlerPost] = field(default_factory=dict)
    cli_verbs: dict[str, Callable] = field(default_factory=dict)
    ui_blocks: dict[str, str] = field(default_factory=dict)
    static_js: list[Path] = field(default_factory=list)
    startup: list[tuple[str, Callable[[Any], None]]] = field(default_factory=list)
    shutdown: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    # Name of the extension that claimed each key — used for clear
    # conflict errors.
    _provenance: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, reg: Registration, *,
            core_get_routes: set[str] | None = None,
            core_post_routes: set[str] | None = None,
            core_cli_verbs: set[str] | None = None) -> None:
        """Merge one registration. Raises :class:`RegistryConflict` on
        any collision with core or with an already-merged extension.

        ``core_*`` sets name the keys that already exist in core. When
        an extension tries to claim one, the conflict lists ``"core"``
        as the other owner so the error is unambiguous.
        """
        core_get = core_get_routes or set()
        core_post = core_post_routes or set()
        core_cli = core_cli_verbs or set()

        for path, handler in reg.get_routes.items():
            if path in core_get:
                raise RegistryConflict(
                    f"extension {reg.name!r} tries to claim GET {path!r}, "
                    f"which is a core route")
            self._claim("GET", path, reg.name)
            self.get_routes[path] = handler
        for path, handler in reg.post_routes.items():
            if path in core_post:
                raise RegistryConflict(
                    f"extension {reg.name!r} tries to claim POST {path!r}, "
                    f"which is a core route")
            self._claim("POST", path, reg.name)
            self.post_routes[path] = handler
        for verb, fn in reg.cli_verbs.items():
            if verb in core_cli:
                raise RegistryConflict(
                    f"extension {reg.name!r} tries to claim CLI verb "
                    f"{verb!r}, which is a core verb")
            self._claim("CLI", verb, reg.name)
            self.cli_verbs[verb] = fn
        for slot, html in reg.ui_blocks.items():
            self._claim("SLOT", slot, reg.name)
            self.ui_blocks[slot] = html
        self.static_js.extend(reg.static_js)
        self.startup.extend((reg.name, fn) for fn in reg.startup)
        self.shutdown.extend((reg.name, fn) for fn in reg.shutdown)

    def _claim(self, kind: str, key: str, owner: str) -> None:
        existing = self._provenance.get((kind, key))
        if existing is not None and existing != owner:
            raise RegistryConflict(
                f"extensions {existing!r} and {owner!r} both claim "
                f"{kind} {key!r}")
        self._provenance[(kind, key)] = owner


def parse_ui_blocks(path: Path) -> dict[str, str]:
    """Parse an ``ui_blocks.html`` file into ``{slot: html}``.

    The file is plain HTML with ``<!-- {{slot_name}} -->`` markers
    delimiting blocks. Text before the first marker is ignored.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RegistryConflict(f"cannot read ui_blocks from {path}: {e}")
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("<!-- {{") and stripped.endswith("}} -->"):
            if current is not None:
                out[current] = "".join(buf).strip() + "\n"
            current = stripped[len("<!-- {{"):-len("}} -->")].strip()
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "".join(buf).strip() + "\n"
    return out
