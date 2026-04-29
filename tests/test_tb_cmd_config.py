"""CLI tests for ``tb config``."""

import argparse
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config as cfg  # noqa: E402
from lib.errors import UsageError  # noqa: E402
from lib.tb_cmds import config_cmd  # noqa: E402


class _IsolatedStateMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(cfg, "STATE_DIR", base),
            mock.patch.object(cfg, "DASHBOARD_CONFIG_FILE", base / "dashboard-config.json"),
            mock.patch.object(cfg, "PID_DIR", base / "pids"),
            mock.patch.object(cfg, "LOG_DIR", base / "logs"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()


class ConfigCommandTests(_IsolatedStateMixin, unittest.TestCase):

    def _args(self, **kwargs) -> argparse.Namespace:
        base = {"json": False, "quiet": False, "no_header": False}
        base.update(kwargs)
        return argparse.Namespace(**base)

    def test_show_json_includes_agent_max_steps(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = config_cmd.cmd_config_show(self._args(json=True))
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn('"agent_max_steps": 20', text)

    def test_get_plain_prints_value(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = config_cmd.cmd_config_get(self._args(key="agent_max_steps"))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "20")

    def test_set_persists_normalized_value(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = config_cmd.cmd_config_set(self._args(key="agent_max_steps", value="250"))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "agent_max_steps=250")
        self.assertIn('"agent_max_steps": 250', cfg.DASHBOARD_CONFIG_FILE.read_text())

    def test_reset_restores_defaults(self):
        config_cmd.cmd_config_set(self._args(key="agent_max_steps", value="250", quiet=True))
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = config_cmd.cmd_config_reset(self._args())
        self.assertEqual(rc, 0)
        self.assertIn("reset", out.getvalue())
        self.assertIn('"agent_max_steps": 20', cfg.DASHBOARD_CONFIG_FILE.read_text())

    def test_unknown_key_raises_usage_error(self):
        with self.assertRaises(UsageError):
            config_cmd.cmd_config_get(self._args(key="bogus"))


if __name__ == "__main__":
    unittest.main()
