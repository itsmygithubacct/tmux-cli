"""Session log hashing for content-based idle detection."""

import os
import sys
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import session_log_writer, session_logs  # noqa: E402


class _IsolatedLogDir:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            session_logs, "LOG_DIR", Path(self._tmp.name))
        self._patch.start()
        session_logs._hash_state.clear()
        session_logs._last_ensure_ts = 0

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class LogPathSanitizationTests(_IsolatedLogDir, unittest.TestCase):

    def test_plain_name_passes_through_unchanged(self):
        # Alphanumeric names keep their old path — existing logs aren't orphaned.
        self.assertEqual(session_logs.log_path("work").name, "work.log")

    def test_path_significant_chars_are_encoded(self):
        # A name with '/' must not escape LOG_DIR or imply a missing subdir;
        # it stays a single basename directly under LOG_DIR.
        p = session_logs.log_path("foo/bar")
        self.assertEqual(p.parent, session_logs.LOG_DIR)
        self.assertNotIn("/", p.name[: -len(".log")])
        # Distinct names never collide on basename.
        self.assertNotEqual(session_logs.log_path("foo/bar"),
                            session_logs.log_path("foo_bar"))


class ActivityTsTests(_IsolatedLogDir, unittest.TestCase):

    def test_returns_none_when_no_log(self):
        self.assertIsNone(session_logs.activity_ts("ghost"))

    def test_first_observation_anchors_to_now(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        ts = session_logs.activity_ts("work", now=1000)
        self.assertEqual(ts, 1000)

    def test_stable_hash_preserves_timestamp(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        first = session_logs.activity_ts("work", now=1000)
        # Content unchanged; activity_ts must remain anchored to 1000
        # even though "now" moved forward.
        again = session_logs.activity_ts("work", now=1050)
        self.assertEqual(first, 1000)
        self.assertEqual(again, 1000)

    def test_hash_change_bumps_timestamp(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        session_logs.activity_ts("work", now=1000)
        path.write_bytes(b"hello world")
        bumped = session_logs.activity_ts("work", now=1060)
        self.assertEqual(bumped, 1060)

    def test_idle_seconds_computes_age(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        session_logs.activity_ts("work", now=1000)
        self.assertEqual(session_logs.idle_seconds("work", now=1030), 30)

    def test_idle_seconds_none_when_no_log(self):
        self.assertIsNone(session_logs.idle_seconds("ghost", now=1000))

    def test_forget_clears_cache(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        session_logs.activity_ts("work", now=1000)
        self.assertIn("work", session_logs._hash_state)
        session_logs.forget("work")
        self.assertNotIn("work", session_logs._hash_state)

    def test_tail_hashing_ignores_ancient_prefix(self):
        """Edits deep inside a large log that don't touch the tail should
        be detected only when the tail actually changes. We verify the
        simpler property: a trailing append changes the hash."""
        path = session_logs.log_path("big")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"a" * (session_logs._TAIL_BYTES * 2))
        session_logs.activity_ts("big", now=1000)
        with path.open("ab") as f:
            f.write(b"Z")
        bumped = session_logs.activity_ts("big", now=1010)
        self.assertEqual(bumped, 1010)


class EnsureLoggingTests(_IsolatedLogDir, unittest.TestCase):

    def test_ensure_logging_calls_pipe_pane_for_each_pane(self):
        with mock.patch("lib.session_logs.subprocess.run") as run:
            run.side_effect = [
                # list-panes
                mock.Mock(returncode=0, stdout="%0\n%1\n"),
                # pipe-pane x2
                mock.Mock(returncode=0, stdout=""),
                mock.Mock(returncode=0, stdout=""),
            ]
            session_logs.ensure_logging("work")
        # First call lists panes, next two invoke pipe-pane
        self.assertEqual(run.call_count, 3)
        pane_calls = run.call_args_list[1:]
        for call in pane_calls:
            argv = call.args[0]
            self.assertEqual(argv[:2], ["tmux", "pipe-pane"])
            self.assertNotIn("-o", argv)
            self.assertIn("session_log_writer.py", argv[-1])
            self.assertIn("--max-bytes", argv[-1])

    def test_subsequent_ensure_keeps_an_existing_pipe(self):
        session_logs._ensure_dir()
        session_logs._writer_marker("work").touch()
        with mock.patch("lib.session_logs.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout="%0\n"),
                mock.Mock(returncode=0, stdout=""),
            ]
            session_logs.ensure_logging("work")
        self.assertEqual(
            run.call_args_list[1].args[0][:3],
            ["tmux", "pipe-pane", "-o"],
        )

    def test_ensure_logging_all_throttles(self):
        import time as _t
        with mock.patch("lib.session_logs.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            session_logs.ensure_logging_all(force=True)
            call_count_after_first = run.call_count
            # Immediate second call should be throttled (no-op)
            session_logs.ensure_logging_all()
        self.assertEqual(run.call_count, call_count_after_first)

    def test_prune_removes_only_old_orphan_state(self):
        session_logs._ensure_dir()
        active_log = session_logs.log_path("active")
        old_log = session_logs.log_path("old")
        fresh_log = session_logs.log_path("fresh")
        old_marker = session_logs._writer_marker("old")
        for path in (active_log, old_log, fresh_log, old_marker):
            path.write_bytes(b"x")
        old_time = 1000 - session_logs._ORPHAN_GRACE_SEC - 1
        os.utime(old_log, (old_time, old_time))
        os.utime(old_marker, (old_time, old_time))

        removed = session_logs.prune(["active"], now=1000)

        self.assertEqual(removed, 2)
        self.assertTrue(active_log.exists())
        self.assertTrue(fresh_log.exists())
        self.assertFalse(old_log.exists())
        self.assertFalse(old_marker.exists())

    def test_remove_clears_files_and_hash_state(self):
        session_logs._ensure_dir()
        session_logs.log_path("work").write_bytes(b"output")
        session_logs._writer_marker("work").touch()
        session_logs._hash_state["work"] = ("hash", 1)

        self.assertEqual(session_logs.remove("work"), 2)
        self.assertFalse(session_logs.log_path("work").exists())
        self.assertFalse(session_logs._writer_marker("work").exists())
        self.assertNotIn("work", session_logs._hash_state)


class BoundedLogWriterTests(unittest.TestCase):

    def test_rotation_keeps_the_newest_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "work.log"
            path.write_bytes(b"01234567")
            session_log_writer.append_chunk(
                path, b"89AB", max_bytes=10, keep_bytes=6,
            )
            self.assertEqual(path.read_bytes(), b"6789AB")

    def test_stream_never_exceeds_the_ceiling(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "work.log"
            payload = b"abcdefghijklmnopqrstuvwxyz"
            session_log_writer.copy_bounded(
                BytesIO(payload), path, max_bytes=10, keep_bytes=8,
            )
            self.assertLessEqual(path.stat().st_size, 10)
            self.assertEqual(path.read_bytes(), payload[-8:])

    def test_live_pipe_writes_available_bytes_without_waiting_for_eof(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "work.log"
            read_fd, write_fd = os.pipe()
            errors: list[BaseException] = []

            def copy_pipe():
                try:
                    with os.fdopen(read_fd, "rb") as source:
                        session_log_writer.copy_bounded(
                            source, path, max_bytes=1024, keep_bytes=768,
                        )
                except BaseException as exc:  # surfaced in the test thread
                    errors.append(exc)

            thread = threading.Thread(target=copy_pipe)
            thread.start()
            try:
                os.write(write_fd, b"small interactive update\n")
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    if path.exists() and path.stat().st_size:
                        break
                    time.sleep(0.01)
                self.assertEqual(path.read_bytes(), b"small interactive update\n")
            finally:
                os.close(write_fd)
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
