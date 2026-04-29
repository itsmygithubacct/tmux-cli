"""Resilience of pidfile detection and self-heal of orphaned ttyds.

Covers two behaviors added to ``lib/ttyd.py``:

* ``_pid_alive`` / ``_pid_is_ttyd`` distinguish "definitely dead" from
  "can't tell" — a transient /proc read or EPERM no longer triggers
  pidfile deletion of a live ttyd.
* ``_reconcile_pidfile`` restores a missing pidfile when the session's
  port is listening AND a live ttyd owns the matching wrapper argv,
  so a single bad poll doesn't permanently orphan the session from
  the dashboard's view.
"""

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _StateDirMixin:
    """Patch all state-dir paths to a temp directory so tests don't
    touch ``~/.tmux-browse/``."""

    def setUp(self):
        from lib import config as cfg
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(cfg, "STATE_DIR", d),
            mock.patch.object(cfg, "PID_DIR", d / "pids"),
            mock.patch.object(cfg, "LOG_DIR", d / "logs"),
            mock.patch.object(cfg, "PORTS_FILE", d / "ports.json"),
        ]
        for p in self._patches:
            p.start()
        (d / "pids").mkdir()
        self._dir = d

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class PidIsTtydTests(_StateDirMixin, unittest.TestCase):

    def test_returns_true_for_proc_named_ttyd(self):
        from lib import ttyd
        m = mock.mock_open(read_data="ttyd\n")
        with mock.patch("builtins.open", m):
            self.assertTrue(ttyd._pid_is_ttyd(1234))

    def test_returns_false_for_other_proc_name(self):
        from lib import ttyd
        m = mock.mock_open(read_data="bash\n")
        with mock.patch("builtins.open", m):
            self.assertFalse(ttyd._pid_is_ttyd(1234))

    def test_filenotfound_means_dead(self):
        from lib import ttyd
        with mock.patch("builtins.open", side_effect=FileNotFoundError()):
            self.assertFalse(ttyd._pid_is_ttyd(1234))

    def test_transient_oserror_is_treated_as_alive(self):
        # Permission / EIO / EBUSY etc. on a still-living /proc entry
        # must NOT trigger pidfile deletion. Permissive here, strict
        # only on the unambiguous "no such file" signal above.
        from lib import ttyd
        with mock.patch("builtins.open",
                        side_effect=PermissionError(errno.EACCES, "denied")):
            self.assertTrue(ttyd._pid_is_ttyd(1234))


class PidAliveTests(_StateDirMixin, unittest.TestCase):

    def test_dead_pid_returns_false(self):
        from lib import ttyd
        with mock.patch("os.kill", side_effect=ProcessLookupError()):
            self.assertFalse(ttyd._pid_alive(999999999))

    def test_eperm_treated_as_alive(self):
        from lib import ttyd
        # EPERM means the process exists but we lack signal permission
        # (different uid). Treat as alive — same-uid is the common case
        # for our own ttyds, but we mustn't unlink a live pidfile if
        # the kernel ever returns EPERM transiently.
        kill_err = PermissionError(errno.EPERM, "operation not permitted")
        with mock.patch("os.kill", side_effect=kill_err), \
             mock.patch.object(ttyd, "_pid_is_ttyd", return_value=True):
            self.assertTrue(ttyd._pid_alive(1234))

    def test_alive_and_named_ttyd(self):
        from lib import ttyd
        with mock.patch("os.kill", return_value=None), \
             mock.patch.object(ttyd, "_pid_is_ttyd", return_value=True):
            self.assertTrue(ttyd._pid_alive(1234))

    def test_alive_but_recycled_pid_not_ttyd(self):
        from lib import ttyd
        with mock.patch("os.kill", return_value=None), \
             mock.patch.object(ttyd, "_pid_is_ttyd", return_value=False):
            self.assertFalse(ttyd._pid_alive(1234))


class ReconcilePidfileTests(_StateDirMixin, unittest.TestCase):

    def test_returns_none_when_no_port_assignment(self):
        from lib import ttyd
        # No ports.assign call, so ports.get("work") is None.
        self.assertIsNone(ttyd._reconcile_pidfile("work"))

    def test_returns_none_when_port_not_listening(self):
        from lib import ttyd, ports
        ports.assign("work")
        # _port_listening probes 127.0.0.1:port — return False to
        # simulate "no ttyd bound there anymore".
        with mock.patch.object(ttyd, "_port_listening", return_value=False):
            self.assertIsNone(ttyd._reconcile_pidfile("work"))

    def test_returns_none_when_no_matching_proc(self):
        from lib import ttyd, ports
        ports.assign("work")
        with mock.patch.object(ttyd, "_port_listening", return_value=True), \
             mock.patch.object(ttyd, "_scan_ttyd_for_session", return_value=None):
            self.assertIsNone(ttyd._reconcile_pidfile("work"))
        # No pidfile should have been created.
        self.assertFalse((self._dir / "pids" / "work.pid").exists())

    def test_writes_pidfile_and_scheme_when_match_found(self):
        from lib import ttyd, ports
        ports.assign("work")
        with mock.patch.object(ttyd, "_port_listening", return_value=True), \
             mock.patch.object(ttyd, "_scan_ttyd_for_session", return_value=4242), \
             mock.patch.object(ttyd, "_scheme_from_argv", return_value="https"):
            self.assertEqual(ttyd._reconcile_pidfile("work"), 4242)
        self.assertEqual((self._dir / "pids" / "work.pid").read_text(), "4242\n")
        self.assertEqual((self._dir / "pids" / "work.scheme").read_text(), "https\n")


class ReadPidReconcilesTests(_StateDirMixin, unittest.TestCase):
    """``read_pid`` must invoke reconciliation when the pidfile is gone."""

    def test_missing_pidfile_with_live_orphan_is_recovered(self):
        from lib import ttyd, ports
        ports.assign("work")
        # Simulate the overnight failure mode: pidfile deleted, but a
        # ttyd process for this session is still alive on the assigned
        # port. read_pid should detect it via reconciliation, recreate
        # the pidfile, and return the PID.
        with mock.patch.object(ttyd, "_port_listening", return_value=True), \
             mock.patch.object(ttyd, "_scan_ttyd_for_session", return_value=4242), \
             mock.patch.object(ttyd, "_scheme_from_argv", return_value="http"):
            self.assertEqual(ttyd.read_pid("work"), 4242)
        self.assertTrue((self._dir / "pids" / "work.pid").exists())

    def test_missing_pidfile_no_orphan_returns_none(self):
        from lib import ttyd, ports
        ports.assign("work")
        with mock.patch.object(ttyd, "_port_listening", return_value=True), \
             mock.patch.object(ttyd, "_scan_ttyd_for_session", return_value=None):
            self.assertIsNone(ttyd.read_pid("work"))


class ScanTtydForSessionTests(_StateDirMixin, unittest.TestCase):
    """``_scan_ttyd_for_session`` walks a /proc-shaped directory, treats
    each numeric-named entry as a candidate, and matches on
    ``comm == "ttyd"`` plus the wrapper path being immediately followed
    by the session name in the cmdline. Tests use a fixture proc_root
    (``proc_root=`` arg) so they don't depend on the real /proc."""

    def _proc(self) -> Path:
        return self._dir / "proc"

    def _make_proc_entry(self, pid: int, comm: str, argv: list[str]):
        d = self._proc() / str(pid)
        d.mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")

    def test_returns_none_when_no_proc_root(self):
        from lib import ttyd
        self.assertIsNone(ttyd._scan_ttyd_for_session("work",
                                                     proc_root=Path("/nonexistent")))

    def test_finds_ttyd_with_matching_session_arg(self):
        from lib import ttyd, config as cfg
        wrap = str(cfg.TTYD_WRAP)
        self._make_proc_entry(
            4242, "ttyd",
            ["/usr/local/bin/ttyd", "-p", "7715", "-W", "bash", wrap, "work"],
        )
        self.assertEqual(
            ttyd._scan_ttyd_for_session("work", proc_root=self._proc()),
            4242,
        )

    def test_skips_non_ttyd_processes(self):
        from lib import ttyd, config as cfg
        wrap = str(cfg.TTYD_WRAP)
        # Same wrapper + session name, but the process isn't ttyd.
        self._make_proc_entry(
            4242, "bash",
            ["/usr/local/bin/ttyd", "-p", "7715", "-W", "bash", wrap, "work"],
        )
        self.assertIsNone(
            ttyd._scan_ttyd_for_session("work", proc_root=self._proc()),
        )

    def test_session_must_immediately_follow_wrapper(self):
        # Defends against name-collision false matches: a session name
        # that happens to appear elsewhere in the argv must not match.
        from lib import ttyd, config as cfg
        wrap = str(cfg.TTYD_WRAP)
        self._make_proc_entry(
            4242, "ttyd",
            ["ttyd", "-p", "7715", "work", "-W", "bash", wrap, "other"],
        )
        self.assertIsNone(
            ttyd._scan_ttyd_for_session("work", proc_root=self._proc()),
        )

    def test_ignores_non_numeric_entries(self):
        from lib import ttyd, config as cfg
        wrap = str(cfg.TTYD_WRAP)
        # Real /proc has /proc/self, /proc/cpuinfo, etc. — non-numeric
        # entries the scanner must skip without ever opening them.
        (self._proc() / "self").mkdir(parents=True)
        (self._proc() / "self" / "comm").write_text("ttyd\n")
        self._make_proc_entry(
            4242, "ttyd",
            ["ttyd", "-W", "bash", wrap, "work"],
        )
        self.assertEqual(
            ttyd._scan_ttyd_for_session("work", proc_root=self._proc()),
            4242,
        )


if __name__ == "__main__":
    unittest.main()
