import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config  # noqa: E402


class EnsureDirsTests(unittest.TestCase):

    def _run_in_tmp_home(self, fn):
        with tempfile.TemporaryDirectory() as home:
            state = Path(home) / ".tmux-browse"
            patches = {
                "STATE_DIR": state,
                "AGENT_LOG_DIR": state / "agent-logs",
                "AGENT_CONVERSATIONS_DIR": state / "agent-conversations",
                "PID_DIR": state / "pids",
                "LOG_DIR": state / "logs",
            }
            with mock.patch.multiple(config, **patches):
                fn(state)

    def test_creates_state_dir(self):
        def check(state):
            config.ensure_dirs()
            self.assertTrue(state.is_dir())
            for sub in ("agent-logs", "agent-conversations", "pids", "logs"):
                self.assertTrue((state / sub).is_dir())
        self._run_in_tmp_home(check)

    def test_state_dir_is_owner_only(self):
        def check(state):
            config.ensure_dirs()
            mode = stat.S_IMODE(state.stat().st_mode)
            self.assertEqual(mode, 0o700)
        self._run_in_tmp_home(check)

    def test_tightens_preexisting_loose_dir(self):
        def check(state):
            state.mkdir(parents=True)
            state.chmod(0o755)
            config.ensure_dirs()
            mode = stat.S_IMODE(state.stat().st_mode)
            self.assertEqual(mode, 0o700)
        self._run_in_tmp_home(check)

    def test_idempotent(self):
        def check(state):
            config.ensure_dirs()
            config.ensure_dirs()  # second call must not raise
            self.assertTrue(state.is_dir())
        self._run_in_tmp_home(check)


class TtydExecutableTests(unittest.TestCase):

    def test_prefers_local_bin_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            binpath = Path(d) / "ttyd"
            binpath.write_text("#!/bin/sh\n")
            with mock.patch.object(config, "TTYD_BIN", binpath):
                self.assertEqual(config.ttyd_executable(), str(binpath))

    def test_falls_back_to_path_name(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "nope" / "ttyd"
            with mock.patch.object(config, "TTYD_BIN", missing):
                self.assertEqual(config.ttyd_executable(), "ttyd")


if __name__ == "__main__":
    unittest.main()
