"""Submodule init/update helpers used by the Config-pane install UI.

Covers parsing ``.gitmodules`` and the ``subprocess.run`` shape that
the real ``git submodule`` invocation uses. The actual ``git submodule
update`` call is mocked — exercising the binary on every test would
turn the suite into a network-dependent integration test.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config as cfg  # noqa: E402
from lib.extensions import submodule as sm  # noqa: E402


class IsSubmodulePathTests(unittest.TestCase):

    def _set_project(self, project_dir: Path) -> None:
        self._patch = mock.patch.object(cfg, "PROJECT_DIR", project_dir)
        self._patch.start()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._set_project(Path(self._tmp.name))

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_no_gitmodules_returns_false(self):
        self.assertFalse(sm.is_submodule_path("agent"))

    def test_recognises_declared_submodule(self):
        (Path(self._tmp.name) / ".gitmodules").write_text(textwrap.dedent("""\
            [submodule "extensions/agent"]
            \tpath = extensions/agent
            \turl = https://github.com/itsmygithubacct/tmux-browse-agent.git
            \tbranch = main
            """))
        self.assertTrue(sm.is_submodule_path("agent"))

    def test_ignores_unrelated_submodule(self):
        (Path(self._tmp.name) / ".gitmodules").write_text(textwrap.dedent("""\
            [submodule "docs/book"]
            \tpath = docs/book
            \turl = https://example.com/book.git
            """))
        self.assertFalse(sm.is_submodule_path("agent"))


class SubmoduleInitTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            cfg, "PROJECT_DIR", Path(self._tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_success_returns_ok_with_empty_stderr(self):
        result = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=result) as run:
            ok, output = sm.submodule_init("agent")
        self.assertTrue(ok)
        self.assertEqual(output, "")
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            ["git", "submodule", "update", "--init", "--",
             "extensions/agent"])
        self.assertEqual(kwargs["cwd"], Path(self._tmp.name))
        self.assertEqual(kwargs["timeout"], 120.0)

    def test_non_zero_return_surfaces_stderr(self):
        result = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="",
            stderr="fatal: no submodule mapping found\n")
        with mock.patch("subprocess.run", return_value=result):
            ok, output = sm.submodule_init("agent")
        self.assertFalse(ok)
        self.assertIn("no submodule mapping", output)

    def test_timeout_returns_human_readable_message(self):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120.0),
        ):
            ok, output = sm.submodule_init("agent")
        self.assertFalse(ok)
        self.assertIn("timed out", output)

    def test_missing_git_binary_is_handled(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            ok, output = sm.submodule_init("agent")
        self.assertFalse(ok)
        self.assertIn("git not on PATH", output)


class SubmoduleUpdateRemoteTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            cfg, "PROJECT_DIR", Path(self._tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_success_uses_remote_flag(self):
        result = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=result) as run:
            ok, _ = sm.submodule_update_remote("agent")
        self.assertTrue(ok)
        args, _ = run.call_args
        self.assertEqual(
            args[0],
            ["git", "submodule", "update", "--remote", "--",
             "extensions/agent"])


if __name__ == "__main__":
    unittest.main()
