"""Run every installed extension's ``tests/`` from the core runner.

Walks ``extensions/*/tests/`` and loads each ``test_*.py`` as a
uniquely-named module via ``importlib.util.spec_from_file_location``
— that way the ``tests`` package name doesn't collide between
core's ``tests/`` (an implicit namespace package at cwd) and each
extension's ``tests/`` (explicit packages with __init__.py).

Before loading, every extension's root is prepended to ``sys.path``
so an extension that imports another at module-load time (e.g.
agent → sandbox) can resolve the import regardless of discovery
order.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_EXT_ROOT = _REPO / "extensions"


def _installed_extensions() -> list[Path]:
    if not _EXT_ROOT.is_dir():
        return []
    return sorted(
        p for p in _EXT_ROOT.iterdir()
        if p.is_dir() and (p / "manifest.json").is_file()
    )


def _prime_sys_path(exts: list[Path]) -> None:
    for p in (_REPO, *exts):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_tests(_loader: unittest.TestLoader, _tests, _pattern):
    exts = _installed_extensions()
    _prime_sys_path(exts)

    suite = unittest.TestSuite()
    inner = unittest.TestLoader()
    for ext in exts:
        test_dir = ext / "tests"
        if not test_dir.is_dir():
            continue
        for test_file in sorted(test_dir.glob("test_*.py")):
            # Uniquify so two extensions can both ship a
            # ``test_config_lock_agent_endpoints.py`` (if they ever
            # did) without stomping on each other in sys.modules.
            modname = f"_ext_{ext.name}_{test_file.stem}"
            try:
                mod = _load_module(modname, test_file)
            except Exception as e:
                # One bad test file shouldn't nuke discovery for
                # the rest. Report it as a failing test.
                suite.addTest(unittest.TestCase.failureException(
                    f"could not load {test_file}: {e}"))
                continue
            suite.addTests(inner.loadTestsFromModule(mod))
    return suite
