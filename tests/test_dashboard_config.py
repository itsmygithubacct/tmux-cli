"""Dashboard config normalization + save/load round-trip."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config as cfg  # noqa: E402
from lib import dashboard_config as dc  # noqa: E402


class NormalizeTests(unittest.TestCase):

    def test_empty_input_yields_defaults(self):
        out = dc.normalize({})
        self.assertEqual(out, dc.DEFAULTS)

    def test_auto_refresh_default_is_disabled(self):
        self.assertFalse(dc.DEFAULTS["auto_refresh"])

    def test_agent_max_steps_default_is_20(self):
        self.assertEqual(dc.DEFAULTS["agent_max_steps"], 20)

    def test_non_dict_input_yields_defaults(self):
        self.assertEqual(dc.normalize("nope"), dc.DEFAULTS)
        self.assertEqual(dc.normalize(None), dc.DEFAULTS)
        self.assertEqual(dc.normalize([1, 2, 3]), dc.DEFAULTS)

    def test_bool_coerces_from_strings(self):
        for s in ("true", "yes", "on", "1", "TRUE"):
            self.assertTrue(dc.normalize({"auto_refresh": s})["auto_refresh"], s)
        for s in ("false", "no", "off", "0", "False"):
            self.assertFalse(dc.normalize({"auto_refresh": s})["auto_refresh"], s)

    def test_bool_invalid_falls_back_to_default(self):
        out = dc.normalize({"auto_refresh": "maybe"})
        self.assertEqual(out["auto_refresh"], dc.DEFAULTS["auto_refresh"])

    def test_int_clamps_to_range(self):
        # refresh_seconds range is (1, 300)
        self.assertEqual(dc.normalize({"refresh_seconds": 0})["refresh_seconds"], 1)
        self.assertEqual(dc.normalize({"refresh_seconds": 99999})["refresh_seconds"], 300)
        self.assertEqual(dc.normalize({"refresh_seconds": 42})["refresh_seconds"], 42)
        self.assertEqual(dc.normalize({"agent_max_steps": 0})["agent_max_steps"], 1)
        self.assertEqual(dc.normalize({"agent_max_steps": 2000})["agent_max_steps"], 1000)
        self.assertEqual(dc.normalize({"agent_max_steps": 250})["agent_max_steps"], 250)

    def test_int_invalid_falls_back_to_default(self):
        out = dc.normalize({"refresh_seconds": "abc"})
        self.assertEqual(out["refresh_seconds"], dc.DEFAULTS["refresh_seconds"])

    def test_unknown_keys_dropped(self):
        out = dc.normalize({"mystery_flag": True, "auto_refresh": False})
        self.assertNotIn("mystery_flag", out)
        self.assertFalse(out["auto_refresh"])


class SaveLoadRoundTripTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patches = [
            mock.patch.object(cfg, "STATE_DIR", Path(self._tmp.name)),
            mock.patch.object(cfg, "DASHBOARD_CONFIG_FILE",
                              Path(self._tmp.name) / "dashboard-config.json"),
            mock.patch.object(cfg, "PID_DIR", Path(self._tmp.name) / "pids"),
            mock.patch.object(cfg, "LOG_DIR", Path(self._tmp.name) / "logs"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_round_trip_preserves_values(self):
        saved = dc.save({"auto_refresh": False, "refresh_seconds": 10})
        loaded = dc.load()
        self.assertEqual(saved, loaded)
        self.assertEqual(loaded["auto_refresh"], False)
        self.assertEqual(loaded["refresh_seconds"], 10)
        self.assertEqual(cfg.DASHBOARD_CONFIG_FILE.stat().st_mode & 0o777, 0o600)

    def test_load_without_file_returns_defaults(self):
        self.assertEqual(dc.load(), dc.DEFAULTS)

    def test_load_ignores_malformed_file(self):
        cfg.DASHBOARD_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        cfg.DASHBOARD_CONFIG_FILE.write_text("{ not json")
        # Fall back silently — broken config must never crash the dashboard.
        self.assertEqual(dc.load(), dc.DEFAULTS)

    def test_replace_failure_keeps_the_previous_complete_config(self):
        dc.save({"refresh_seconds": 10})
        with mock.patch.object(dc.os, "replace", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                dc.save({"refresh_seconds": 20})
        self.assertEqual(dc.load()["refresh_seconds"], 10)
        self.assertEqual(
            list(cfg.DASHBOARD_CONFIG_FILE.parent.glob(
                ".dashboard-config.json.*.tmp",
            )),
            [],
        )

    def test_update_values_applies_one_normalized_batch(self):
        saved = dc.update_values({
            "auto_refresh": "true",
            "refresh_seconds": "12",
        })
        self.assertTrue(saved["auto_refresh"])
        self.assertEqual(saved["refresh_seconds"], 12)
        self.assertEqual(saved, dc.load())

    def test_parallel_independent_key_updates_are_all_retained(self):
        root = Path(__file__).resolve().parent.parent
        keys = (
            "auto_refresh",
            "day_mode",
            "show_launch_codex",
            "show_launch_kimi",
            "show_launch_monitor",
            "show_launch_top",
            "show_topbar_refresh",
            "show_body_send_bar",
        )
        environment = dict(os.environ, HOME=self._tmp.name)
        programs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys; from pathlib import Path; "
                    "from lib import config, dashboard_config; "
                    "path = Path(sys.argv[1]); "
                    "config.STATE_DIR = path.parent; "
                    "config.DASHBOARD_CONFIG_FILE = path; "
                    "dashboard_config.set_value(sys.argv[2], 'true')",
                    str(cfg.DASHBOARD_CONFIG_FILE),
                    key,
                ],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for key in keys
        ]
        failures = []
        for process in programs:
            stdout, stderr = process.communicate(timeout=20)
            if process.returncode:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual(failures, [])
        state = json.loads(cfg.DASHBOARD_CONFIG_FILE.read_text())
        self.assertTrue(all(state[key] is True for key in keys))


if __name__ == "__main__":
    unittest.main()
