"""Load a single extension: resolve its manifest, add its directory to
``sys.path``, import its entry-point modules, and build a
:class:`Registration` describing what it contributes.

Failure mode is per-extension: if any step raises, the caller gets an
:class:`ExtensionLoadError` with the extension name and the failing
stage, and core keeps running without that one extension.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .manifest import Manifest, ManifestError
from .registry import Registration, parse_ui_blocks


class ExtensionLoadError(RuntimeError):
    """Raised when an extension fails to load. ``stage`` is a short tag
    for what went wrong (``manifest``, ``validate``, ``import``,
    ``entry``, ``ui_blocks``, ``static``) — used by the UI to show a
    useful hint next to the verbatim message."""

    def __init__(self, name: str, stage: str, message: str):
        super().__init__(f"extension {name!r} failed at {stage}: {message}")
        self.name = name
        self.stage = stage
        self.message = message


def load_one(path: Path, *, core_version: str) -> Registration:
    """Load the extension rooted at ``path`` and return its Registration.

    ``path`` is the directory that contains ``manifest.json``. On any
    failure raises :class:`ExtensionLoadError`.
    """
    manifest_path = path / "manifest.json"
    try:
        manifest = Manifest.load(manifest_path)
    except ManifestError as e:
        raise ExtensionLoadError(path.name, "manifest", str(e)) from e
    try:
        manifest.validate(core_version=core_version)
    except ManifestError as e:
        raise ExtensionLoadError(manifest.name, "validate", str(e)) from e

    # ``sys.path`` insertion is what makes the extension's own imports
    # resolve. We prepend so the extension's modules shadow anything of
    # the same name in core; that's intentional — extensions are their
    # own namespace.
    ext_root = str(path.resolve())
    if ext_root not in sys.path:
        sys.path.insert(0, ext_root)

    reg = Registration(name=manifest.name)

    if manifest.routes_entry:
        handlers = _call_entry(manifest, "routes_entry", "register")
        if handlers is not None:
            # routes_entry may return either a full Registration (rich
            # form) or a dict of route dicts (light form).
            if isinstance(handlers, Registration):
                reg.get_routes.update(handlers.get_routes)
                reg.post_routes.update(handlers.post_routes)
            elif isinstance(handlers, dict):
                reg.get_routes.update(handlers.get("get_routes") or {})
                reg.post_routes.update(handlers.get("post_routes") or {})
            else:
                raise ExtensionLoadError(
                    manifest.name, "entry",
                    f"routes_entry must return Registration or dict; "
                    f"got {type(handlers).__name__}")

    if manifest.cli_entry:
        verbs = _call_entry(manifest, "cli_entry", "register_verb")
        if isinstance(verbs, dict):
            reg.cli_verbs.update(verbs)
        elif callable(verbs):
            # Convenience: entry returning a single callable is treated
            # as a dispatch dict keyed by the manifest module name.
            reg.cli_verbs[manifest.module] = verbs
        elif verbs is not None:
            raise ExtensionLoadError(
                manifest.name, "entry",
                f"cli_entry must return dict or callable; "
                f"got {type(verbs).__name__}")

    if manifest.ui_blocks_path:
        blocks_path = path / manifest.ui_blocks_path
        try:
            reg.ui_blocks.update(parse_ui_blocks(blocks_path))
        except Exception as e:
            raise ExtensionLoadError(
                manifest.name, "ui_blocks", str(e)) from e

    if manifest.static_dir:
        static_root = path / manifest.static_dir
        if static_root.is_dir():
            reg.static_js.extend(sorted(static_root.glob("*.js")))

    if manifest.startup_entry:
        fns = _call_entry(manifest, "startup_entry", "register")
        if isinstance(fns, dict):
            for fn in fns.get("on_server_start") or []:
                reg.startup.append(fn)
            for fn in fns.get("on_server_stop") or []:
                reg.shutdown.append(fn)
        elif fns is not None:
            raise ExtensionLoadError(
                manifest.name, "entry",
                f"startup_entry must return dict with on_server_start "
                f"and/or on_server_stop lists; got {type(fns).__name__}")

    return reg


def _call_entry(manifest: Manifest, field_name: str, default_fn: str):
    """Resolve a ``module:callable`` spec, import, call, return result.

    ``manifest.routes_entry`` etc. are ``"module.path:func"`` strings.
    If the ``:func`` part is omitted, ``default_fn`` is used.
    """
    spec = getattr(manifest, field_name)
    assert spec is not None  # checked by caller
    if ":" in spec:
        mod_name, fn_name = spec.split(":", 1)
    else:
        mod_name, fn_name = spec, default_fn
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        raise ExtensionLoadError(
            manifest.name, "import",
            f"cannot import {mod_name!r}: {e}") from e
    fn = getattr(mod, fn_name, None)
    if fn is None:
        raise ExtensionLoadError(
            manifest.name, "entry",
            f"{mod_name}.{fn_name} not found")
    try:
        return fn()
    except Exception as e:
        raise ExtensionLoadError(
            manifest.name, "entry",
            f"{mod_name}.{fn_name}() raised: {e}") from e
