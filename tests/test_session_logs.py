"""Session log hashing for content-based idle detection."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import session_logs  # noqa: E402


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
            self.assertEqual(argv[:3], ["tmux", "pipe-pane", "-o"])
            self.assertIn("cat >>", argv[-1])

    def test_ensure_logging_all_throttles(self):
        import time as _t
        with mock.patch("lib.session_logs.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            session_logs.ensure_logging_all(force=True)
            call_count_after_first = run.call_count
            # Immediate second call should be throttled (no-op)
            session_logs.ensure_logging_all()
        self.assertEqual(run.call_count, call_count_after_first)


if __name__ == "__main__":
    unittest.main()
