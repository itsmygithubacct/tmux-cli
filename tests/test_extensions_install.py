"""Real-install path for :func:`lib.extensions.install`.

``subprocess.run`` is mocked so the tests don't touch the network or
the operator's actual git state. A tiny fake manifest is dropped into
the target path as a side effect of the mocked clone, so the
validation step exercises the real ``Manifest.load`` / ``validate``
machinery.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config as cfg  # noqa: E402
from lib import extensions  # noqa: E402


def _write_fake_manifest(path: Path, *, min_core: str = "0.7.1") -> None:
    (path / "manifest.json").write_text(json.dumps({
        "name": "agent",
        "version": "0.7.1",
        "module": "agent",
        "min_tmux_browse": min_core,
        # At least one entry point is required for validate() to pass.
        "routes_entry": "server.routes:register",
    }))


def _clone_side_effect(dest: Path, *, min_core: str = "0.7.1"):
    """``subprocess.run`` side-effect that populates ``dest`` as git would."""
    def run(args, **_kwargs):
        dest.mkdir(parents=True, exist_ok=True)
        _write_fake_manifest(dest, min_core=min_core)
        return subprocess.CompletedProcess(args, returncode=0,
                                           stdout="", stderr="")
    return run


class InstallCloneTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._ext_root = root / "extensions"
        self._enabled = Path(self._state.name) / "extensions.json"
        self._patches = [
            mock.patch.object(cfg, "PROJECT_DIR", root),
            mock.patch.object(cfg, "STATE_DIR", Path(self._state.name)),
            mock.patch.object(extensions, "EXTENSIONS_ROOT", self._ext_root),
            mock.patch.object(extensions, "ENABLED_FILE", self._enabled),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()
        self._state.cleanup()

    def test_fresh_clone_success_returns_install_result(self):
        dest = self._ext_root / "agent"
        with mock.patch(
            "subprocess.run",
            side_effect=_clone_side_effect(dest),
        ):
            result = extensions.install("agent", core_version="0.7.1")
        self.assertEqual(result.name, "agent")
        self.assertEqual(result.version, "0.7.1")
        self.assertEqual(result.via, "clone")
        self.assertTrue((dest / "manifest.json").is_file())

    def test_clone_failure_cleans_up_partial_tree(self):
        dest = self._ext_root / "agent"

        def run(args, **_kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            # git would have written .git/ even before crashing; leave a
            # partial file behind to prove cleanup removes it.
            (dest / "README.md").write_text("partial")
            return subprocess.CompletedProcess(
                args, returncode=128, stdout="",
                stderr="fatal: unable to access repo")

        with mock.patch("subprocess.run", side_effect=run):
            with self.assertRaises(extensions.InstallError) as ctx:
                extensions.install("agent", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "clone")
        self.assertIn("unable to access", ctx.exception.msg)
        self.assertFalse(dest.exists())

    def test_validate_failure_cleans_up(self):
        dest = self._ext_root / "agent"

        def run(args, **_kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "manifest.json").write_text("{not json")
            return subprocess.CompletedProcess(args, returncode=0,
                                               stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=run):
            with self.assertRaises(extensions.InstallError) as ctx:
                extensions.install("agent", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "validate")
        self.assertFalse(dest.exists())

    def test_too_new_extension_raises_validate_error(self):
        dest = self._ext_root / "agent"
        with mock.patch(
            "subprocess.run",
            side_effect=_clone_side_effect(dest, min_core="99.0.0"),
        ):
            with self.assertRaises(extensions.InstallError) as ctx:
                extensions.install("agent", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "validate")
        self.assertFalse(dest.exists())

    def test_install_rejects_non_empty_target(self):
        dest = self._ext_root / "agent"
        dest.mkdir(parents=True)
        (dest / "README.md").write_text("leftover")
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(extensions.InstallError) as ctx:
                extensions.install("agent", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "exists")
        run.assert_not_called()  # No clone attempted when pre-existing tree.

    def test_unknown_name_raises_unknown_stage(self):
        with self.assertRaises(extensions.InstallError) as ctx:
            extensions.install("nope", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "unknown")

    def test_timeout_surfaces_as_clone_stage(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120.0),
        ):
            with self.assertRaises(extensions.InstallError) as ctx:
                extensions.install("agent", core_version="0.7.1",
                                   clone_timeout=120.0)
        self.assertEqual(ctx.exception.stage, "clone")
        self.assertIn("timed out", ctx.exception.msg)


class InstallSubmodulePathTests(unittest.TestCase):
    """When .gitmodules declares extensions/<name>, install takes the
    submodule-init path instead of cloning afresh."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._ext_root = root / "extensions"
        self._ext_root.mkdir()
        # Seed the submodule target with a manifest so validation finds
        # something after the fake submodule_init "succeeds".
        (self._ext_root / "agent").mkdir()
        _write_fake_manifest(self._ext_root / "agent")
        # Register the submodule in .gitmodules.
        (root / ".gitmodules").write_text(
            '[submodule "extensions/agent"]\n'
            '\tpath = extensions/agent\n'
            '\turl = https://example.com/agent.git\n'
            '\tbranch = main\n')
        self._patches = [
            mock.patch.object(cfg, "PROJECT_DIR", root),
            mock.patch.object(cfg, "STATE_DIR", Path(self._state.name)),
            mock.patch.object(extensions, "EXTENSIONS_ROOT", self._ext_root),
            mock.patch.object(
                extensions, "ENABLED_FILE",
                Path(self._state.name) / "extensions.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()
        self._state.cleanup()

    def test_submodule_path_calls_submodule_update_not_clone(self):
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout="", stderr=""),
        ) as run:
            result = extensions.install("agent", core_version="0.7.1")
        self.assertEqual(result.via, "submodule")
        args, _ = run.call_args
        # The first positional arg is the argv list.
        self.assertEqual(args[0][:4],
                         ["git", "submodule", "update", "--init"])


if __name__ == "__main__":
    unittest.main()
