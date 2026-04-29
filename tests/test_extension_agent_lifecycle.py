"""End-to-end sanity for the agent extension's load lifecycle.

Proves that the E0 loader wires the agent extension's routes,
UI blocks, static JS, CLI verb, and startup/shutdown hooks to core
when explicitly enabled — and that disabling it removes the entire
agent surface without breaking the rest of the dashboard.

Post-E2, the agent extension is a submodule at ``extensions/agent/``
and is **opt-in**: core never auto-enables it. A fresh
``~/.tmux-browse/`` that has no ``extensions.json`` contributes
nothing from the extension; the operator must click Enable in the
Config pane (or hand-edit the file) for the agent surface to appear.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config as cfg  # noqa: E402
from lib import extensions  # noqa: E402


@unittest.skipUnless(
    (Path(__file__).resolve().parent.parent
     / "extensions" / "agent" / "manifest.json").exists(),
    "agent submodule not checked out (git submodule update --init)",
)
class AgentExtensionLifecycleTests(unittest.TestCase):

    def setUp(self):
        self._state = tempfile.TemporaryDirectory()
        state_path = Path(self._state.name)
        self._enabled_file = state_path / "extensions.json"
        self._patches = [
            mock.patch.object(cfg, "STATE_DIR", state_path),
            mock.patch.object(extensions, "ENABLED_FILE", self._enabled_file),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._state.cleanup()

    def test_fresh_install_has_no_extension_surface(self):
        # Opt-in: with no extensions.json, load_enabled returns empty.
        reg = extensions.load_enabled(core_version_override="0.7.1.3")
        self.assertEqual(reg.get_routes, {})
        self.assertEqual(reg.post_routes, {})
        self.assertEqual(reg.ui_blocks, {})
        self.assertEqual(reg.static_js, [])
        self.assertEqual(reg.cli_verbs, {})
        self.assertFalse(self._enabled_file.exists())

    def test_agent_shows_as_installed_but_not_enabled(self):
        # Status reflects "submodule present on disk, not turned on".
        rows = {r["name"]: r for r in extensions.status()}
        self.assertIn("agent", rows)
        self.assertTrue(rows["agent"]["installed"])
        self.assertFalse(rows["agent"]["enabled"])

    def test_enable_then_load_registers_full_agent_surface(self):
        extensions.enable("agent")
        reg = extensions.load_enabled(core_version_override="0.7.1.3")
        # Routes from server/routes.py
        self.assertIn("/api/agents", reg.get_routes)
        self.assertIn("/api/agents", reg.post_routes)
        self.assertIn("/api/agent-log", reg.get_routes)
        # CLI verb from tb_cmds/agent.py
        self.assertIn("agent", reg.cli_verbs)
        # UI blocks from ui_blocks.html
        self.assertIn("agents_section", reg.ui_blocks)
        self.assertIn("config_agent", reg.ui_blocks)
        # Static JS from static/
        self.assertEqual(
            sorted(p.name for p in reg.static_js),
            ["agents.js", "runs.js", "tasks.js"],
        )
        # Startup + shutdown hooks from startup.py
        self.assertEqual(len(reg.startup), 1)
        self.assertEqual(len(reg.shutdown), 1)

    def test_disable_removes_agent_surface(self):
        extensions.enable("agent")
        extensions.disable("agent")
        reg = extensions.load_enabled(core_version_override="0.7.1.3")
        self.assertEqual(reg.get_routes, {})
        self.assertEqual(reg.post_routes, {})
        self.assertEqual(reg.ui_blocks, {})
        self.assertEqual(reg.static_js, [])
        self.assertEqual(reg.cli_verbs, {})

    def test_disable_leaves_submodule_files_on_disk(self):
        # Uninstall is a separate verb (E4) — disable just flips the bit.
        # Confirms the submodule tree is still readable after disable so
        # re-enabling doesn't require a fresh clone.
        extensions.enable("agent")
        extensions.disable("agent")
        ext_dir = cfg.PROJECT_DIR / "extensions" / "agent"
        self.assertTrue((ext_dir / "manifest.json").is_file())
        self.assertTrue((ext_dir / "agent" / "__init__.py").is_file())


if __name__ == "__main__":
    unittest.main()
