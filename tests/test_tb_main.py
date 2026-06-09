"""Tests for ``tb.py`` — the CLI entrypoint: argument wiring, the global-flag
contract, the per-verb server gate, and main()'s error/exit-code handling.

These cover the review fixes:
  * --json/--quiet/--no-header honoured before AND after the verb
  * --version on the shared parent (works in either position)
  * argparse usage errors flow through the TBError -> exit-code/JSON path
  * the ``needs_server`` server gate (SSOT, replacing the hardcoded skip set),
    including ``web url``/``web stop`` no longer demanding a server
  * a missing ``func`` (misconfigured extension) becomes a clean usage error
  * BrokenPipe is swallowed (no traceback) when a reader closes early
  * any non-TBError is caught (EUNKNOWN/exit 1) instead of leaking a traceback
  * stdout/stderr are hardened against UnicodeEncodeError
  * sessions.server_running() treats a socket-absent message as "not running"
"""

import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tb  # noqa: E402
from lib import sessions  # noqa: E402
from lib.errors import SessionNotFound, UsageError  # noqa: E402
from lib.tb_cmds import bulk, lifecycle, read, web  # noqa: E402


def _ns(parser, argv):
    """parse_args + normalize, as main() does before dispatch."""
    args = parser.parse_args(argv)
    tb._normalize_global_flags(args)
    return args


class GlobalFlagPositionTests(unittest.TestCase):
    """--json/--quiet/--no-header must resolve identically before/after verb."""

    def setUp(self):
        self.p = tb._build_parser()

    def test_json_before_verb(self):
        self.assertTrue(_ns(self.p, ["--json", "ls"]).json)

    def test_json_after_verb(self):
        self.assertTrue(_ns(self.p, ["ls", "--json"]).json)

    def test_json_absent_defaults_false(self):
        self.assertFalse(_ns(self.p, ["ls"]).json)

    def test_json_both_positions(self):
        self.assertTrue(_ns(self.p, ["--json", "ls", "--json"]).json)

    def test_quiet_and_no_header_before_verb(self):
        a = _ns(self.p, ["--quiet", "--no-header", "ls"])
        self.assertTrue(a.quiet)
        self.assertTrue(a.no_header)

    def test_quiet_after_verb(self):
        self.assertTrue(_ns(self.p, ["ls", "-q"]).quiet)

    def test_flags_on_nested_subcommand_either_side(self):
        self.assertTrue(_ns(self.p, ["--json", "config", "get", "auto_refresh"]).json)
        self.assertTrue(_ns(self.p, ["config", "get", "auto_refresh", "--json"]).json)


class VersionFlagTests(unittest.TestCase):
    def setUp(self):
        self.p = tb._build_parser()

    def _expect_version_exit(self, argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                self.p.parse_args(argv)
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(tb.VERSION, buf.getvalue())

    def test_version_before_verb(self):
        self._expect_version_exit(["--version"])

    def test_version_after_verb(self):
        # The regression: --version used to live only on the top-level parser.
        self._expect_version_exit(["ls", "--version"])


class ArgparseErrorTests(unittest.TestCase):
    """Usage errors raise UsageError (-> exit 2 / JSON) instead of sys.exit(2)."""

    def setUp(self):
        self.p = tb._build_parser()

    def test_bad_verb_raises_usage_error(self):
        with self.assertRaises(UsageError):
            self.p.parse_args(["bogusverb"])

    def test_missing_verb_raises_usage_error(self):
        with self.assertRaises(UsageError):
            self.p.parse_args([])

    def test_missing_positional_raises_usage_error(self):
        with self.assertRaises(UsageError):
            self.p.parse_args(["kill"])

    def test_missing_nested_subverb_raises_usage_error(self):
        with self.assertRaises(UsageError):
            self.p.parse_args(["web"])

    def test_main_emits_json_envelope_for_bad_verb(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc = tb.main(["--json", "bogusverb"])
        self.assertEqual(rc, 2)
        env = json.loads(buf.getvalue())
        self.assertFalse(env["ok"])
        self.assertEqual(env["code"], "EUSAGE")
        self.assertEqual(env["exit"], 2)


class NeedsServerMappingTests(unittest.TestCase):
    """The per-verb server gate must match the intended skip set exactly."""

    def setUp(self):
        self.p = tb._build_parser()

    def _needs_server(self, argv):
        return getattr(self.p.parse_args(argv), "needs_server", True)

    def test_server_less_verbs(self):
        for argv in (["ls"], ["exists", "x"], ["snapshot"], ["new", "x"],
                     ["range", "2", "bash"], ["config"], ["config", "get", "auto_refresh"],
                     ["web", "url", "s"], ["web", "stop", "s"]):
            with self.subTest(argv=argv):
                self.assertFalse(self._needs_server(argv))

    def test_server_requiring_verbs(self):
        for argv in (["show", "x"], ["capture", "x"], ["tail", "x"], ["kill", "x"],
                     ["rename", "a", "b"], ["wait", "x"], ["describe", "x"],
                     ["web", "start", "s"]):
            with self.subTest(argv=argv):
                self.assertTrue(self._needs_server(argv))


class _StubMixin:
    """Run tb.main() with a verb's handler replaced by a stub, no real tmux."""

    def _run(self, module, attr, argv, *, server, stub=None):
        stub = stub if stub is not None else mock.Mock(return_value=0)
        with mock.patch.object(module, attr, stub), \
             mock.patch.object(tb.sessions, "server_running", return_value=server):
            rc = tb.main(argv)
        return rc, stub


class ServerGateTests(_StubMixin, unittest.TestCase):

    def test_server_less_verb_runs_without_server(self):
        rc, stub = self._run(read, "cmd_ls", ["ls"], server=False)
        self.assertEqual(rc, 0)
        stub.assert_called_once()

    def test_server_requiring_verb_short_circuits(self):
        rc, stub = self._run(read, "cmd_show", ["show", "x"], server=False)
        self.assertEqual(rc, 6)            # ENOSERVER
        stub.assert_not_called()

    def test_server_requiring_verb_runs_when_server_up(self):
        rc, stub = self._run(read, "cmd_show", ["show", "x"], server=True)
        self.assertEqual(rc, 0)
        stub.assert_called_once()

    def test_web_url_runs_without_server(self):
        # The headline web fix: url is a pure registry read.
        rc, stub = self._run(web, "cmd_web_url", ["web", "url", "s"], server=False)
        self.assertEqual(rc, 0)
        stub.assert_called_once()

    def test_web_stop_runs_without_server(self):
        rc, stub = self._run(web, "cmd_web_stop", ["web", "stop", "s"], server=False)
        self.assertEqual(rc, 0)
        stub.assert_called_once()

    def test_web_start_still_needs_server(self):
        rc, stub = self._run(web, "cmd_web_start", ["web", "start", "s"], server=False)
        self.assertEqual(rc, 6)
        stub.assert_not_called()


class ErrorHandlingTests(_StubMixin, unittest.TestCase):

    def test_tberror_plain(self):
        stub = mock.Mock(side_effect=SessionNotFound("no such session: x"))
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc, _ = self._run(read, "cmd_show", ["show", "x"], server=True, stub=stub)
        self.assertEqual(rc, 3)
        self.assertIn("tb: no such session: x", buf.getvalue())

    def test_tberror_json_envelope_with_flag_before_verb(self):
        stub = mock.Mock(side_effect=SessionNotFound("no such session: x"))
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc, _ = self._run(read, "cmd_show", ["--json", "show", "x"],
                              server=True, stub=stub)
        self.assertEqual(rc, 3)
        env = json.loads(buf.getvalue())
        self.assertFalse(env["ok"])
        self.assertEqual(env["code"], "ENOENT")
        self.assertEqual(env["exit"], 3)

    def test_non_tberror_caught_plain(self):
        stub = mock.Mock(side_effect=RuntimeError("boom"))
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc, _ = self._run(read, "cmd_ls", ["ls"], server=True, stub=stub)
        self.assertEqual(rc, 1)
        self.assertIn("unexpected error: boom", buf.getvalue())
        self.assertNotIn("Traceback", buf.getvalue())

    def test_non_tberror_caught_json(self):
        stub = mock.Mock(side_effect=RuntimeError("boom"))
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            rc, _ = self._run(read, "cmd_ls", ["--json", "ls"], server=True, stub=stub)
        self.assertEqual(rc, 1)
        env = json.loads(buf.getvalue())
        self.assertFalse(env["ok"])
        self.assertEqual(env["code"], "EUNKNOWN")
        self.assertEqual(env["exit"], 1)

    def test_tb_debug_reraises(self):
        stub = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.dict(os.environ, {"TB_DEBUG": "1"}):
            with self.assertRaises(RuntimeError):
                self._run(read, "cmd_ls", ["ls"], server=True, stub=stub)

    def test_broken_pipe_swallowed(self):
        stub = mock.Mock(side_effect=BrokenPipeError())
        # Patch _silence_stdout so the test runner's real stdout isn't dup2'd away.
        with mock.patch.object(tb, "_silence_stdout") as silence:
            rc, _ = self._run(read, "cmd_ls", ["ls"], server=True, stub=stub)
        self.assertEqual(rc, 0)
        silence.assert_called_once()

    def test_keyboard_interrupt_returns_130(self):
        stub = mock.Mock(side_effect=KeyboardInterrupt())
        rc, _ = self._run(read, "cmd_ls", ["ls"], server=True, stub=stub)
        self.assertEqual(rc, 130)


class MissingFuncTests(unittest.TestCase):
    """A subparser with no func default (bad extension) -> clean usage error."""

    def test_missing_func_is_usage_error(self):
        def build():
            p = tb._ArgumentParser(prog="tb")
            sub = p.add_subparsers(dest="_verb", required=True)
            sp = sub.add_parser("ext")          # deliberately no set_defaults(func=)
            sp.set_defaults(needs_server=False)
            return p

        buf = io.StringIO()
        with mock.patch.object(tb, "_build_parser", build), \
             mock.patch.object(sys, "stderr", buf):
            rc = tb.main(["ext"])
        self.assertEqual(rc, 2)             # UsageError -> EUSAGE
        self.assertIn("missing subcommand", buf.getvalue())


class StreamHardeningTests(unittest.TestCase):

    def test_reconfigure_called_with_backslashreplace(self):
        fake_out = mock.Mock()
        fake_err = mock.Mock()
        with mock.patch.object(sys, "stdout", fake_out), \
             mock.patch.object(sys, "stderr", fake_err):
            tb._harden_streams()
        fake_out.reconfigure.assert_called_once_with(errors="backslashreplace")
        fake_err.reconfigure.assert_called_once_with(errors="backslashreplace")

    def test_harden_is_safe_when_reconfigure_missing(self):
        # A stream without .reconfigure (e.g. a StringIO) must not raise.
        with mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", io.StringIO()):
            tb._harden_streams()  # should not raise


class ServerRunningMarkerTests(unittest.TestCase):
    """sessions.server_running() must treat socket-absent stderr as not-running."""

    def _run_returns(self, *, returncode, stderr):
        result = types.SimpleNamespace(returncode=returncode, stderr=stderr)
        return mock.patch.object(sessions.subprocess, "run", return_value=result)

    def test_running_when_rc_zero(self):
        with self._run_returns(returncode=0, stderr=""):
            self.assertTrue(sessions.server_running())

    def test_not_running_classic_banner(self):
        with self._run_returns(returncode=1,
                               stderr="no server running on /tmp/tmux-1000/default"):
            self.assertFalse(sessions.server_running())

    def test_not_running_socket_absent(self):
        # The fix: a missing socket prints "error connecting … No such file".
        with self._run_returns(
                returncode=1,
                stderr="error connecting to /tmp/tmux-1000/default (No such file or directory)"):
            self.assertFalse(sessions.server_running())

    def test_not_running_failed_to_connect(self):
        with self._run_returns(returncode=1, stderr="failed to connect to server"):
            self.assertFalse(sessions.server_running())

    def test_running_for_benign_nonzero(self):
        # A non-zero exit with no "no-server" marker means up-but-something-else.
        with self._run_returns(returncode=1, stderr="some unrelated tmux gripe"):
            self.assertTrue(sessions.server_running())

    def test_not_running_when_tmux_absent(self):
        with mock.patch.object(sessions.subprocess, "run",
                               side_effect=FileNotFoundError()):
            self.assertFalse(sessions.server_running())


if __name__ == "__main__":
    unittest.main()
