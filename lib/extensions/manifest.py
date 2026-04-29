"""Extension manifest: parse + validate ``manifest.json`` from an
``extensions/<name>/`` directory.

The manifest is the entire declared surface of an extension — which
entry-point dotted paths to import, which template slots it fills,
which directories it stores state in. Core reads this once at server
start to decide what to load; the extension's own code doesn't have
to agree on anything else with core beyond this file.

Failure mode is fail-closed: any ambiguity (unknown keys, missing
fields, wrong types, core-version too old) raises ``ManifestError``
before the extension's Python code runs. Callers surface the error
message to the operator; core keeps running without the extension.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class ManifestError(ValueError):
    """Raised on any shape/validation problem in an extension manifest."""


# The manifest format is intentionally tiny. Adding a field is
# compatible; renaming or removing one is not, so the schema version
# below is bumped on breaking changes. Extensions may declare their
# own minimum manifest_version when that matters.
MANIFEST_VERSION = 1


# Top-level keys accepted by the manifest. Anything else raises. This is
# strict on purpose — an extension that encodes state in an unknown key
# is signalling a version mismatch we want to surface.
_ALLOWED_KEYS = frozenset({
    "name",
    "version",
    "module",
    "min_tmux_browse",
    "routes_entry",
    "cli_entry",
    "ui_blocks_path",
    "static_dir",
    "startup_entry",
    "state_paths",
    "manifest_version",
})

_REQUIRED_KEYS = frozenset({
    "name",
    "version",
    "module",
    "min_tmux_browse",
})


@dataclass(frozen=True)
class Manifest:
    """Parsed manifest.

    ``entry`` fields are dotted paths relative to the extension root
    (``sys.path`` has the extension dir prepended at load time so
    ``server.routes:register`` resolves to ``server/routes.py``'s
    ``register`` function).
    """

    name: str
    version: str
    module: str
    min_tmux_browse: str
    routes_entry: str | None = None
    cli_entry: str | None = None
    ui_blocks_path: str | None = None
    static_dir: str | None = None
    startup_entry: str | None = None
    state_paths: tuple[str, ...] = field(default_factory=tuple)
    manifest_version: int = MANIFEST_VERSION
    # Not part of the serialized manifest — populated by load() so
    # callers know where the file lived.
    source_dir: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Parse a manifest file. Raises :class:`ManifestError` on any
        shape problem."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ManifestError(f"cannot read {path}: {e}") from e
        if not isinstance(raw, dict):
            raise ManifestError(f"{path}: manifest must be a JSON object")
        unknown = set(raw) - _ALLOWED_KEYS
        if unknown:
            raise ManifestError(
                f"{path}: unknown manifest keys: {sorted(unknown)}")
        missing = _REQUIRED_KEYS - set(raw)
        if missing:
            raise ManifestError(
                f"{path}: missing required keys: {sorted(missing)}")
        try:
            return cls(
                name=_req_str(raw, "name"),
                version=_req_str(raw, "version"),
                module=_req_str(raw, "module"),
                min_tmux_browse=_req_str(raw, "min_tmux_browse"),
                routes_entry=_opt_str(raw, "routes_entry"),
                cli_entry=_opt_str(raw, "cli_entry"),
                ui_blocks_path=_opt_str(raw, "ui_blocks_path"),
                static_dir=_opt_str(raw, "static_dir"),
                startup_entry=_opt_str(raw, "startup_entry"),
                state_paths=tuple(_opt_list_of_str(raw, "state_paths")),
                manifest_version=int(raw.get("manifest_version", MANIFEST_VERSION)),
                source_dir=path.parent.resolve(),
            )
        except (TypeError, ValueError) as e:
            raise ManifestError(f"{path}: {e}") from e

    def validate(self, *, core_version: str) -> None:
        """Fail-closed protocol + version checks."""
        if self.manifest_version > MANIFEST_VERSION:
            raise ManifestError(
                f"extension {self.name!r} requires manifest version "
                f"{self.manifest_version}, core supports up to "
                f"{MANIFEST_VERSION}")
        if _version_tuple(self.min_tmux_browse) > _version_tuple(core_version):
            raise ManifestError(
                f"extension {self.name!r} requires tmux-browse >= "
                f"{self.min_tmux_browse}; this install is {core_version}")
        # Library-only extensions (no entry points, just a Python
        # package other extensions import) are allowed. The loader
        # still prepends the extension dir to ``sys.path`` at load
        # time, which is what makes ``import sandbox`` etc. work.


# --- helpers -----------------------------------------------------------

def _req_str(raw: dict, key: str) -> str:
    v = raw[key]
    if not isinstance(v, str) or not v.strip():
        raise ManifestError(f"{key!r} must be a non-empty string")
    return v.strip()


def _opt_str(raw: dict, key: str) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ManifestError(f"{key!r} must be a non-empty string or omitted")
    return v.strip()


def _opt_list_of_str(raw: dict, key: str) -> list[str]:
    v = raw.get(key, [])
    if not isinstance(v, list):
        raise ManifestError(f"{key!r} must be a list of strings")
    out: list[str] = []
    for entry in v:
        if not isinstance(entry, str) or not entry.strip():
            raise ManifestError(f"{key!r} entries must be non-empty strings")
        out.append(entry.strip())
    return out


def _version_tuple(raw: str) -> tuple[int, ...]:
    """Parse ``1.2.3`` or ``0.7.0.4`` into a comparable tuple."""
    parts = []
    for chunk in raw.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            raise ManifestError(f"bad version string: {raw!r}")
        parts.append(int(digits))
    return tuple(parts)
