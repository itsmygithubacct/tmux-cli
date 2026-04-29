"""Atomic write + safe-name round-trip helpers in lib/ttyd.py."""

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.ttyd import _atomic_write, _safe, _start_lock, _unsafe  # noqa: E402


class AtomicWriteTests(unittest.TestCase):

    def test_creates_target_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "sub" / "pid"
            _atomic_write(target, "12345\n")
            self.assertEqual(target.read_text(), "12345\n")
            # tmp file should not linger
            tmp = target.with_name(target.name + ".tmp")
            self.assertFalse(tmp.exists())

    def test_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "pid"
            _atomic_write(target, "old\n")
            _atomic_write(target, "new\n")
            self.assertEqual(target.read_text(), "new\n")


class SafeNameRoundTripTests(unittest.TestCase):

    def test_simple_names_are_unchanged(self):
        self.assertEqual(_safe("work"), "work")
        self.assertEqual(_unsafe("work"), "work")

    def test_whitespace_and_special_chars_encode(self):
        # Must be reversible; no collision between "foo bar" and "foo_bar"
        encoded_a = _safe("foo bar")
        encoded_b = _safe("foo_bar")
        self.assertNotEqual(encoded_a, encoded_b)
        self.assertEqual(_unsafe(encoded_a), "foo bar")
        self.assertEqual(_unsafe(encoded_b), "foo_bar")

    def test_slash_and_dot_are_encoded(self):
        # Basename must not contain path separators
        encoded = _safe("a/b.c")
        self.assertNotIn("/", encoded)
        self.assertEqual(_unsafe(encoded), "a/b.c")


class StartLockTests(unittest.TestCase):

    def test_same_session_returns_same_lock(self):
        a = _start_lock("work")
        b = _start_lock("work")
        self.assertIs(a, b)

    def test_different_sessions_get_independent_locks(self):
        a = _start_lock("work")
        b = _start_lock("other")
        self.assertIsNot(a, b)

    def test_lock_is_usable_from_multiple_threads(self):
        lock = _start_lock("concurrency-test")
        held: list[bool] = []

        def hold():
            with lock:
                held.append(True)

        t1 = threading.Thread(target=hold)
        t2 = threading.Thread(target=hold)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(held), 2)


class ReadPidTests(unittest.TestCase):
    """read_pid() removes pidfiles belonging to dead processes."""

    def setUp(self):
        from lib import config as cfg  # local to keep the mock patches tight
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        from unittest import mock
        self._patches = [
            mock.patch.object(cfg, "STATE_DIR", d),
            mock.patch.object(cfg, "PID_DIR", d / "pids"),
            mock.patch.object(cfg, "LOG_DIR", d / "logs"),
        ]
        for p in self._patches:
            p.start()
        (d / "pids").mkdir()
        self._dir = d

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_missing_pidfile_returns_none(self):
        from lib import ttyd
        self.assertIsNone(ttyd.read_pid("nope"))

    def test_dead_pid_is_cleaned_up(self):
        from lib import ttyd
        pid_path = self._dir / "pids" / "work.pid"
        pid_path.write_text("999999999\n")  # PID that (almost certainly) isn't alive
        self.assertIsNone(ttyd.read_pid("work"))
        self.assertFalse(pid_path.exists(),
                         "read_pid must unlink the pidfile when process is dead")


class ReadSchemeTests(unittest.TestCase):

    def setUp(self):
        from lib import config as cfg
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        from unittest import mock
        self._patches = [
            mock.patch.object(cfg, "STATE_DIR", d),
            mock.patch.object(cfg, "PID_DIR", d / "pids"),
            mock.patch.object(cfg, "LOG_DIR", d / "logs"),
        ]
        for p in self._patches:
            p.start()
        (d / "pids").mkdir()
        self._dir = d

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_defaults_to_http_when_file_missing(self):
        from lib import ttyd
        self.assertEqual(ttyd.read_scheme("nope"), "http")

    def test_reads_https_when_set(self):
        from lib import ttyd
        (self._dir / "pids" / "work.scheme").write_text("https\n")
        self.assertEqual(ttyd.read_scheme("work"), "https")

    def test_unknown_contents_treated_as_http(self):
        from lib import ttyd
        (self._dir / "pids" / "work.scheme").write_text("garbage\n")
        self.assertEqual(ttyd.read_scheme("work"), "http")


if __name__ == "__main__":
    unittest.main()
