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
    # Move this extension to the front even if it was loaded before and a
    # later extension subsequently took precedence.
    while ext_root in sys.path:
        sys.path.remove(ext_root)
    sys.path.insert(0, ext_root)

    reg = Registration(name=manifest.name)

    if manifest.routes_entry:
        handlers = _call_entry(manifest, path, "routes_entry", "register")
        if handlers is not None:
            # routes_entry may return either a full Registration (rich
            # form) or a dict of route dicts (light form).
            if isinstance(handlers, Registration):
                reg.get_routes.update(handlers.get_routes)
                reg.post_routes.update(handlers.post_routes)
                reg.session_post_processors.extend(handlers.session_post_processors)
            elif isinstance(handlers, dict):
                reg.get_routes.update(handlers.get("get_routes") or {})
                reg.post_routes.update(handlers.get("post_routes") or {})
                reg.session_post_processors.extend(
                    handlers.get("session_post_processors") or [])
            else:
                raise ExtensionLoadError(
                    manifest.name, "entry",
                    f"routes_entry must return Registration or dict; "
                    f"got {type(handlers).__name__}")

    if manifest.cli_entry:
        verbs = _call_entry(manifest, path, "cli_entry", "register_verb")
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
        blocks_path = _contained_path(
            path, manifest.ui_blocks_path, manifest.name, "ui_blocks")
        try:
            reg.ui_blocks.update(parse_ui_blocks(blocks_path))
        except Exception as e:
            raise ExtensionLoadError(
                manifest.name, "ui_blocks", str(e)) from e

    if manifest.static_dir:
        static_root = _contained_path(
            path, manifest.static_dir, manifest.name, "static")
        if static_root.is_dir():
            reg.static_js.extend(sorted(static_root.glob("*.js")))

    if manifest.startup_entry:
        fns = _call_entry(manifest, path, "startup_entry", "register")
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


def _contained_path(base: Path, rel: str, name: str, stage: str) -> Path:
    """Resolve ``base / rel`` and refuse it if it escapes ``base``.

    ``rel`` comes from the manifest (untrusted). A value containing ``..`` —
    or an absolute path, which ``Path.__truediv__`` lets clobber ``base``
    entirely — would otherwise let an extension read UI-block files or glob
    static assets from anywhere on disk. Resolving both sides also defeats a
    symlink inside the extension that points outside its tree.
    """
    base_r = base.resolve()
    candidate = (base / rel).resolve()
    if candidate != base_r and not candidate.is_relative_to(base_r):
        raise ExtensionLoadError(
            name, stage, f"path {rel!r} escapes the extension directory")
    return candidate


def _call_entry(manifest: Manifest, ext_path: Path,
                field_name: str, default_fn: str):
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
    ext_root = ext_path.resolve()
    stale = _evict_stale_entry_modules(mod_name, ext_root)
    before_import = set(sys.modules)
    importlib.invalidate_caches()
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        # A failed extension import must not leave a half-imported package in
        # the process or discard the previously working module cache.
        _restore_entry_modules(mod_name, before_import, stale)
        raise ExtensionLoadError(
            manifest.name, "import",
            f"cannot import {mod_name!r}: {e}") from e
    # The module name is untrusted manifest data and the extension's dir is on
    # sys.path, so ``import_module`` would happily resolve a core module, a
    # sibling extension's module (possibly already cached in sys.modules), or
    # a stdlib name. Confine entry points to the extension's own tree: require
    # a real file living under ext_path. This rejects builtins/namespace
    # packages (no __file__) too — an entry point must be a file we shipped.
    mod_file = getattr(mod, "__file__", None)
    if mod_file is None or not Path(mod_file).resolve().is_relative_to(ext_root):
        _restore_entry_modules(mod_name, before_import, stale)
        raise ExtensionLoadError(
            manifest.name, "import",
            f"entry module {mod_name!r} resolves outside the extension "
            f"directory")
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


def _module_belongs_to(module, ext_root: Path) -> bool:
    """Whether a cached module or namespace package lives in ``ext_root``."""
    mod_file = getattr(module, "__file__", None)
    if mod_file is not None:
        return Path(mod_file).resolve().is_relative_to(ext_root)
    for entry in getattr(module, "__path__", ()):
        if Path(entry).resolve().is_relative_to(ext_root):
            return True
    return False


def _restore_entry_modules(mod_name: str, before_import: set[str],
                           stale: dict[str, object]) -> None:
    """Undo cache changes after a failed or out-of-tree entry import."""
    top_name = mod_name.split(".", 1)[0]
    for name in set(sys.modules) - before_import:
        if name == top_name or name.startswith(top_name + "."):
            sys.modules.pop(name, None)
    sys.modules.update(stale)


def _entry_exists_in_extension(mod_name: str, ext_root: Path) -> bool:
    """Return True only when ``mod_name`` has an importable in-tree file."""
    parts = mod_name.split(".")
    if not parts or any(not part.isidentifier() for part in parts):
        return False
    relative = ext_root.joinpath(*parts)
    candidates = (relative.with_suffix(".py"), relative / "__init__.py")
    return any(
        candidate.is_file()
        and candidate.resolve().is_relative_to(ext_root)
        for candidate in candidates
    )


def _evict_stale_entry_modules(mod_name: str,
                               ext_root: Path) -> dict[str, object]:
    """Remove cached same-named modules from earlier extension roots.

    Entry module names are local to an extension, so two extensions may both
    use a conventional name such as ``startup``. Python's global module cache
    would otherwise return the first extension's module for the second load.
    Eviction is allowed only when this extension actually ships the requested
    module; a manifest that merely names a core, sibling, or stdlib module
    remains fail-closed.
    """
    if not _entry_exists_in_extension(mod_name, ext_root):
        return {}
    top_name = mod_name.split(".", 1)[0]
    stale: dict[str, object] = {}
    for name, module in list(sys.modules.items()):
        if name != top_name and not name.startswith(top_name + "."):
            continue
        if not _module_belongs_to(module, ext_root):
            stale[name] = module
            del sys.modules[name]
    return stale
