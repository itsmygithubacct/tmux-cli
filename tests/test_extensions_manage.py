"""Update + uninstall paths from :mod:`lib.extensions`.

``subprocess.run`` is mocked; a small fake manifest is written by
the side effect so the real ``Manifest`` machinery exercises the
validation step.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config as cfg  # noqa: E402
from lib import extensions  # noqa: E402


def _manifest(version: str, *, min_core: str = "0.7.1",
              state_paths: list[str] | None = None) -> str:
    payload = {
        "name": "agent",
        "version": version,
        "module": "agent",
        "min_tmux_browse": min_core,
        "routes_entry": "server.routes:register",
    }
    if state_paths is not None:
        payload["state_paths"] = state_paths
    return json.dumps(payload)


class _IsolatedExt:

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._state = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._project = root
        self._ext_root = root / "extensions"
        self._ext_root.mkdir()
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

    def _install_fake(self, version: str, *, submodule: bool = False,
                      state_paths: list[str] | None = None) -> Path:
        target = self._ext_root / "agent"
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(
            _manifest(version, state_paths=state_paths))
        if submodule:
            (self._project / ".gitmodules").write_text(textwrap.dedent("""\
                [submodule "extensions/agent"]
                \tpath = extensions/agent
                \turl = https://example.com/agent.git
                \tbranch = main
                """))
        return target


class UpdateTests(_IsolatedExt, unittest.TestCase):

    def test_missing_install_raises_missing_stage(self):
        with self.assertRaises(extensions.UpdateError) as ctx:
            extensions.update("agent", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "missing")

    def test_unknown_name_raises_unknown_stage(self):
        with self.assertRaises(extensions.UpdateError) as ctx:
            extensions.update("nope", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "unknown")

    def test_clone_path_fetches_then_checks_out_ref(self):
        path = self._install_fake("0.7.1")
        calls: list[list[str]] = []

        def run(args, **_kwargs):
            calls.append(list(args))
            if args[:2] == ["git", "fetch"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ["git", "checkout"]:
                # Simulate the checkout by bumping the installed manifest.
                (path / "manifest.json").write_text(_manifest("0.7.2"))
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(f"unexpected call: {args!r}")

        with mock.patch("subprocess.run", side_effect=run):
            result = extensions.update("agent", core_version="0.7.2")
        self.assertTrue(result.changed)
        self.assertEqual(result.from_version, "0.7.1")
        self.assertEqual(result.to_version, "0.7.2")
        self.assertEqual(result.via, "clone")
        # First call is fetch, second is checkout.
        self.assertEqual(calls[0][:2], ["git", "fetch"])
        self.assertEqual(calls[1][:2], ["git", "checkout"])

    def test_fetch_failure_raises_fetch_stage(self):
        self._install_fake("0.7.1")

        def run(args, **_kwargs):
            if args[:2] == ["git", "fetch"]:
                return subprocess.CompletedProcess(
                    args, 128, "", "fatal: unable to access")
            raise AssertionError("fetch should have short-circuited")

        with mock.patch("subprocess.run", side_effect=run):
            with self.assertRaises(extensions.UpdateError) as ctx:
                extensions.update("agent", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "fetch")

    def test_submodule_path_calls_update_remote(self):
        self._install_fake("0.7.1", submodule=True)
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["git"], 0, "", ""),
        ) as run:
            result = extensions.update("agent", core_version="0.7.1")
        self.assertEqual(result.via, "submodule")
        args, _ = run.call_args
        self.assertEqual(args[0][:4],
                         ["git", "submodule", "update", "--remote"])

    def test_unchanged_version_reports_changed_false(self):
        path = self._install_fake("0.7.1")

        def run(args, **_kwargs):
            # Leave the manifest at 0.7.1 — fetch + checkout succeed but
            # no new version landed.
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("subprocess.run", side_effect=run):
            result = extensions.update("agent", core_version="0.7.1")
        self.assertFalse(result.changed)
        self.assertEqual(result.from_version, "0.7.1")
        self.assertEqual(result.to_version, "0.7.1")

    def test_too_new_manifest_raises_validate_stage(self):
        path = self._install_fake("0.7.1")

        def run(args, **_kwargs):
            if args[:2] == ["git", "checkout"]:
                (path / "manifest.json").write_text(
                    _manifest("99.0.0", min_core="99.0.0"))
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch("subprocess.run", side_effect=run):
            with self.assertRaises(extensions.UpdateError) as ctx:
                extensions.update("agent", core_version="0.7.1")
        self.assertEqual(ctx.exception.stage, "validate")


class UninstallTests(_IsolatedExt, unittest.TestCase):

    def test_clone_path_removes_directory_keeps_state(self):
        target = self._install_fake(
            "0.7.1",
            state_paths=["agents.json", "agent-logs/"])
        # Stand up the state files that should NOT be touched.
        state = Path(self._state.name)
        (state / "agents.json").write_text("{}")
        (state / "agent-logs").mkdir()
        (state / "agent-logs" / "gpt.jsonl").write_text("{}\n")
        # Pre-seed enabled=true so we can verify the flip.
        extensions.enable("agent")
        result = extensions.uninstall("agent")
        self.assertFalse(target.exists())
        self.assertEqual(result["via"], "clone")
        self.assertEqual(result["state_removed"], [])
        # extensions.json entry flipped off + uninstall timestamp recorded.
        entry = extensions._read_enabled()["agent"]
        self.assertFalse(entry["enabled"])
        self.assertIn("uninstalled_ts", entry)
        # State files untouched.
        self.assertTrue((state / "agents.json").is_file())
        self.assertTrue((state / "agent-logs" / "gpt.jsonl").is_file())

    def test_clone_path_remove_state_deletes_declared_paths(self):
        self._install_fake(
            "0.7.1",
            state_paths=["agents.json", "agent-logs/"])
        state = Path(self._state.name)
        (state / "agents.json").write_text("{}")
        (state / "agent-logs").mkdir()
        (state / "agent-logs" / "gpt.jsonl").write_text("{}\n")
        result = extensions.uninstall("agent", remove_state=True)
        self.assertCountEqual(
            result["state_removed"], ["agents.json", "agent-logs/"])
        self.assertFalse((state / "agents.json").exists())
        self.assertFalse((state / "agent-logs").exists())

    def test_remove_state_reports_missing_paths(self):
        self._install_fake(
            "0.7.1",
            state_paths=["agents.json", "absent.json"])
        (Path(self._state.name) / "agents.json").write_text("{}")
        result = extensions.uninstall("agent", remove_state=True)
        self.assertEqual(result["state_removed"], ["agents.json"])
        self.assertEqual(result["state_missing"], ["absent.json"])

    def test_submodule_path_calls_deinit_not_rmtree(self):
        self._install_fake("0.7.1", submodule=True)
        target = self._ext_root / "agent"
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["git"], 0, "", ""),
        ) as run:
            result = extensions.uninstall("agent")
        self.assertEqual(result["via"], "submodule")
        args, _ = run.call_args
        self.assertEqual(args[0][:5],
                         ["git", "submodule", "deinit", "-f", "--"])
        # The submodule tree is not our responsibility to rm — the
        # operator may want `git submodule update --init` to bring it
        # back later.
        self.assertTrue(target.exists())

    def test_uninstall_is_idempotent_on_missing_tree(self):
        # Never installed at all — uninstall should still mark the
        # entry disabled without raising.
        result = extensions.uninstall("agent")
        self.assertFalse(result["state_removed"])
        entry = extensions._read_enabled()["agent"]
        self.assertFalse(entry["enabled"])


class CLIDriverTests(_IsolatedExt, unittest.TestCase):
    """``python3 -m lib.extensions ...`` entrypoint goes through the
    same functions as the HTTP handlers."""

    def test_list_prints_rows(self):
        from lib.extensions import __main__ as driver
        self._install_fake("0.7.1")
        with mock.patch("sys.stdout") as out:
            rc = driver.main(["list"])
        self.assertEqual(rc, 0)
        # At least one row should have been written.
        self.assertTrue(out.write.called)

    def test_install_failure_returns_nonzero(self):
        from lib.extensions import __main__ as driver

        def run(args, **_kwargs):
            return subprocess.CompletedProcess(
                args, 128, "", "fatal: could not resolve host")

        with mock.patch("subprocess.run", side_effect=run):
            rc = driver.main(["install", "agent"])
        self.assertEqual(rc, 1)

    def test_uninstall_with_remove_state_flag(self):
        from lib.extensions import __main__ as driver
        self._install_fake("0.7.1", state_paths=["agents.json"])
        (Path(self._state.name) / "agents.json").write_text("{}")
        rc = driver.main(["uninstall", "agent", "--remove-state"])
        self.assertEqual(rc, 0)
        self.assertFalse((Path(self._state.name) / "agents.json").exists())


if __name__ == "__main__":
    unittest.main()
