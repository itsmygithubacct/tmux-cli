"""Atomic writes, safe names, and keyed lifecycle locks in lib/ttyd.py."""

import gc
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lib.ttyd as ttyd  # noqa: E402
from lib.ttyd import (  # noqa: E402
    _atomic_write, _lifecycle_lock, _safe, _start_lock, _unsafe,
)


class AtomicWriteTests(unittest.TestCase):

    def test_creates_target_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "sub" / "pid"
            _atomic_write(target, "12345\n")
            self.assertEqual(target.read_text(), "12345\n")
            # Unique temp files should not linger.
            self.assertEqual(list(target.parent.glob(".pid.*.tmp")), [])

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

    def test_waiter_keeps_the_same_registered_lock(self):
        name = "inflight-waiter"
        held = _start_lock(name)
        held.acquire()
        ready = threading.Event()
        entered = threading.Event()
        seen: list[threading.Lock] = []

        def waiter():
            lock = _start_lock(name)
            seen.append(lock)
            ready.set()
            with lock:
                entered.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(ready.wait(1))
        self.assertIs(seen[0], held)
        self.assertFalse(entered.is_set())
        held.release()
        thread.join(1)
        self.assertTrue(entered.is_set())

    def test_registry_reclaims_unreferenced_locks(self):
        name = "raw-shell-review-reclaim"
        lock = _start_lock(name)
        self.assertIn(name, ttyd._start_locks)
        del lock
        gc.collect()
        self.assertNotIn(name, ttyd._start_locks)

    def test_registry_stays_bounded_for_unique_raw_shells(self):
        prefix = "raw-shell-review-bounded-"
        for i in range(200):
            lock = _start_lock(f"{prefix}{i}")
            del lock
        gc.collect()
        leftover = [n for n in ttyd._start_locks if n.startswith(prefix)]
        self.assertEqual(leftover, [])

    def test_stop_waits_for_an_inflight_start_lock(self):
        name = "serialized-stop"
        lock = _start_lock(name)
        lock.acquire()
        started = threading.Event()
        read_called = threading.Event()
        result: list[dict] = []

        def read_pid(_name):
            read_called.set()
            return None

        def run_stop():
            started.set()
            result.append(ttyd.stop(name))

        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ttyd, "_read_pid_unlocked", side_effect=read_pid), \
             mock.patch.object(ttyd.config, "STATE_DIR", Path(d)), \
             mock.patch.object(ttyd.config, "ensure_dirs"):
            thread = threading.Thread(target=run_stop)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(read_called.wait(0.05))
            lock.release()
            thread.join(1)

        self.assertTrue(read_called.is_set())
        self.assertTrue(result[0]["already_stopped"])


class LifecycleFileLockTests(unittest.TestCase):

    def test_lifecycle_lock_takes_and_releases_flock(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ttyd.config, "STATE_DIR", Path(d)), \
             mock.patch.object(ttyd.config, "ensure_dirs"), \
             mock.patch.object(ttyd.fcntl, "flock") as flock:
            with _lifecycle_lock("work"):
                pass
        self.assertEqual(flock.call_args_list[0].args[1], ttyd.fcntl.LOCK_EX)
        self.assertEqual(flock.call_args_list[-1].args[1], ttyd.fcntl.LOCK_UN)


class RuntimePolicyTests(unittest.TestCase):

    def test_missing_runtime_sidecar_does_not_match(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ttyd.config, "PID_DIR", Path(d)):
            self.assertFalse(ttyd._runtime_matches("work", "127.0.0.1", None))

    def test_matching_runtime_sidecar_is_accepted(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(ttyd.config, "PID_DIR", Path(d)):
            ttyd._write_runtime("work", "127.0.0.1", None)
            self.assertTrue(ttyd._runtime_matches("work", "127.0.0.1", None))
            self.assertFalse(ttyd._runtime_matches("work", "0.0.0.0", None))

    def test_start_restarts_when_bind_policy_changes(self):
        spawned = {"ok": True, "pid": 456, "port": 7715, "already": False}
        with mock.patch.object(ttyd.config, "ensure_dirs"), \
             mock.patch.object(ttyd, "_lifecycle_lock", return_value=nullcontext()), \
             mock.patch.object(ttyd, "_read_pid_unlocked", return_value=123), \
             mock.patch.object(ttyd.ports, "assign", return_value=7715), \
             mock.patch.object(ttyd, "_runtime_matches", return_value=False), \
             mock.patch.object(
                 ttyd, "_stop_locked", return_value={"ok": True, "pid": 123},
             ) as stop_locked, \
             mock.patch.object(ttyd, "_spawn_ttyd", return_value=spawned) as spawn:
            result = ttyd.start("work", bind_addr="127.0.0.1")
        stop_locked.assert_called_once_with("work", release_port=False)
        spawn.assert_called_once()
        self.assertTrue(result["restarted"])

    def test_start_keeps_process_when_runtime_policy_matches(self):
        with mock.patch.object(ttyd.config, "ensure_dirs"), \
             mock.patch.object(ttyd, "_lifecycle_lock", return_value=nullcontext()), \
             mock.patch.object(ttyd, "_read_pid_unlocked", return_value=123), \
             mock.patch.object(ttyd.ports, "assign", return_value=7715), \
             mock.patch.object(ttyd, "_runtime_matches", return_value=True), \
             mock.patch.object(ttyd, "_stop_locked") as stop_locked, \
             mock.patch.object(ttyd, "_spawn_ttyd") as spawn:
            result = ttyd.start("work", bind_addr="127.0.0.1")
        self.assertTrue(result["already"])
        stop_locked.assert_not_called()
        spawn.assert_not_called()


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
            mock.patch.object(cfg, "ensure_dirs"),
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
