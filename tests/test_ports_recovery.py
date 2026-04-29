"""Ports registry: allocation, corruption recovery, pruning.

Uses a per-test STATE_DIR override via monkeypatch of lib.config paths so
the real ~/.tmux-browse is never touched.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import config as cfg  # noqa: E402
from lib import ports  # noqa: E402


class _IsolatedStateMixin:
    """Point the registry at a temp dir for the duration of each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(cfg, "STATE_DIR", tmp_path),
            mock.patch.object(cfg, "PORTS_FILE", tmp_path / "ports.json"),
            mock.patch.object(cfg, "PID_DIR", tmp_path / "pids"),
            mock.patch.object(cfg, "LOG_DIR", tmp_path / "logs"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class AssignmentTests(_IsolatedStateMixin, unittest.TestCase):

    def test_assign_returns_stable_port_for_same_session(self):
        p1 = ports.assign("work")
        p2 = ports.assign("work")
        self.assertEqual(p1, p2)

    def test_assign_gives_different_ports_to_different_sessions(self):
        p1 = ports.assign("a")
        p2 = ports.assign("b")
        self.assertNotEqual(p1, p2)

    def test_release_removes_assignment(self):
        ports.assign("a")
        self.assertTrue(ports.release("a"))
        self.assertIsNone(ports.get("a"))
        self.assertFalse(ports.release("a"))  # already gone


class PruneTests(_IsolatedStateMixin, unittest.TestCase):

    def test_prune_drops_only_inactive(self):
        ports.assign("alive")
        ports.assign("stale1")
        ports.assign("stale2")
        dropped = ports.prune({"alive"})
        self.assertEqual(sorted(dropped), ["stale1", "stale2"])
        self.assertIsNotNone(ports.get("alive"))
        self.assertIsNone(ports.get("stale1"))
        self.assertIsNone(ports.get("stale2"))

    def test_prune_noop_when_all_active(self):
        ports.assign("a")
        ports.assign("b")
        dropped = ports.prune({"a", "b"})
        self.assertEqual(dropped, [])


class CorruptRegistryRecoveryTests(_IsolatedStateMixin, unittest.TestCase):

    def test_corrupt_json_is_backed_up_and_replaced(self):
        cfg.PORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        cfg.PORTS_FILE.write_text("{not valid json")
        # Accessing the registry should recover without raising; the first
        # assignment proceeds on a fresh structure.
        port = ports.assign("work")
        self.assertIsInstance(port, int)
        # A corrupt-* backup file should have been written alongside.
        backups = list(cfg.PORTS_FILE.parent.glob("ports.json.corrupt.*"))
        self.assertEqual(len(backups), 1, backups)


class PortsFileIsJSONTests(_IsolatedStateMixin, unittest.TestCase):

    def test_written_file_is_valid_sorted_json(self):
        ports.assign("a")
        ports.assign("b")
        raw = cfg.PORTS_FILE.read_text()
        data = json.loads(raw)
        self.assertIn("assignments", data)
        self.assertIn("next_port", data)
        # sort_keys=True in the save path
        first_keys = list(data["assignments"].keys())
        self.assertEqual(first_keys, sorted(first_keys))


class AllocationBoundariesTests(_IsolatedStateMixin, unittest.TestCase):

    def test_ports_stay_within_configured_range(self):
        for i in range(10):
            p = ports.assign(f"s{i}")
            self.assertGreaterEqual(p, cfg.TTYD_PORT_START)
            self.assertLessEqual(p, cfg.TTYD_PORT_END)

    def test_exhaustion_raises(self):
        # Shrink the range so we can actually exhaust it in a test
        with mock.patch.object(cfg, "TTYD_PORT_START", 7700), \
             mock.patch.object(cfg, "TTYD_PORT_END", 7702):  # 3 slots
            ports.assign("a")
            ports.assign("b")
            ports.assign("c")
            with self.assertRaises(RuntimeError):
                ports.assign("d")

    def test_next_port_advances_between_allocations(self):
        p1 = ports.assign("a")
        p2 = ports.assign("b")
        # next_port should have moved past p1
        self.assertNotEqual(p1, p2)
        raw = json.loads(cfg.PORTS_FILE.read_text())
        self.assertEqual(raw["next_port"], p2 + 1)


if __name__ == "__main__":
    unittest.main()
