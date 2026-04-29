"""``tb`` CLI subcommand registry.

Each module in this package defines ``register(subparsers, common)`` that
adds its verbs to the shared argparse subparser set. ``tb.py`` imports
``register_all`` below and calls it once.

Shared helpers (``parse_target``, ``require_target``) live in ``_common``
and are re-exported here so existing ``from . import parse_target`` imports
inside submodules keep working.
"""

from __future__ import annotations

from . import bulk, config_cmd, lifecycle, observe, read, web, write
from ._common import parse_target, require_target

__all__ = ["parse_target", "require_target", "register_all"]


def register_all(subparsers, common) -> None:
    read.register(subparsers, common)
    write.register(subparsers, common)
    lifecycle.register(subparsers, common)
    observe.register(subparsers, common)
    web.register(subparsers, common)
    bulk.register(subparsers, common)
    config_cmd.register(subparsers, common)
    _register_extension_verbs(subparsers, common)


def _register_extension_verbs(subparsers, common) -> None:
    """Call each enabled extension's ``tb_cmds/<verb>.py:register``.

    Extensions that ship a CLI verb expose it via an argparse
    ``register(sub, common)`` alongside the loader-level
    ``register_verb()``. Core calls the argparse side so the extension's
    verb appears in ``tb --help`` and dispatches like a core verb.
    """
    import importlib
    import sys
    from .. import extensions

    enabled_state = extensions._read_enabled()
    for ext_path in extensions.discover():
        if not enabled_state.get(ext_path.name, {}).get("enabled"):
            continue
        tb_cmds_dir = ext_path / "tb_cmds"
        if not tb_cmds_dir.is_dir():
            continue
        sp = str(ext_path)
        if sp not in sys.path:
            sys.path.insert(0, sp)
        for verb_py in sorted(tb_cmds_dir.glob("*.py")):
            if verb_py.name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"tb_cmds.{verb_py.stem}")
            except Exception:
                continue
            reg = getattr(mod, "register", None)
            if callable(reg):
                try:
                    reg(subparsers, common)
                except Exception:
                    # Don't let one bad extension block the rest of tb.
                    continue
