"""Session enumeration — primarily the group-dedup logic that hides
ttyd_wrap.sh's per-viewer grouped sessions from the dashboard and CLI."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import sessions  # noqa: E402
from lib.errors import UsageError  # noqa: E402


class ValidateNameTests(unittest.TestCase):

    def test_accepts_ordinary_names(self):
        for ok in ("work", "bot-sessions_v2", "worker", "a1"):
            sessions._validate_name(ok)  # must not raise

    def test_rejects_empty_and_whitespace_and_delimiters(self):
        for bad in ("", "a b", "a\tb", "a\nb", "a:b", "a.b"):
            with self.assertRaises(UsageError):
                sessions._validate_name(bad)

    def test_rejects_leading_dash_and_equals(self):
        # leading '-' → tmux flag injection; leading '=' → exact-match clash
        for bad in ("-rf", "--help", "=work"):
            with self.assertRaises(UsageError):
                sessions._validate_name(bad)

    def test_allows_dash_and_equals_mid_name(self):
        sessions._validate_name("a-b")
        sessions._validate_name("a=b")


def _tmux_row(name, group, windows=1, attached=0, created=1000, activity=1100):
    """Build one tab-separated row matching _SESSION_FORMAT."""
    return f"{name}\t{windows}\t{attached}\t{created}\t{activity}\t{group}"


class ListSessionsDedupTests(unittest.TestCase):

    def _run(self, rows):
        mock_proc = mock.Mock(returncode=0, stdout="\n".join(rows) + "\n")
        with mock.patch("lib.sessions.subprocess.run", return_value=mock_proc):
            return sessions.list_sessions()

    def test_ungrouped_sessions_pass_through(self):
        rows = [_tmux_row("work", ""), _tmux_row("notes", "")]
        out = self._run(rows)
        self.assertEqual([r["name"] for r in out], ["notes", "work"])

    def test_primary_wins_over_viewers(self):
        rows = [
            _tmux_row("worker", "worker"),
            _tmux_row("worker-v1-1", "worker"),
            _tmux_row("worker-v1-2", "worker"),
        ]
        out = self._run(rows)
        names = [r["name"] for r in out]
        self.assertEqual(names, ["worker"])

    def test_orphan_group_is_dropped_entirely(self):
        # No primary present — viewers are dropped from the listing so the
        # weirdly-named `<base>-v<pid>-<rand>` sessions that ttyd_wrap.sh
        # leaves behind when the base dies don't pollute the session list.
        # The parallel fix in ttyd_wrap.sh's watcher actively kills them;
        # this is the defensive filter for the transient window.
        rows = [
            _tmux_row("music-v1-1", "music"),
            _tmux_row("music-v2-2", "music"),
        ]
        out = self._run(rows)
        self.assertEqual([r["name"] for r in out], [])

    def test_mixed_groups_and_ungrouped(self):
        rows = [
            _tmux_row("scratch", ""),
            _tmux_row("worker", "worker"),
            _tmux_row("worker-v1-1", "worker"),
            _tmux_row("orphan-v1-1", "orphan"),
        ]
        out = self._run(rows)
        names = sorted(r["name"] for r in out)
        # scratch (ungrouped) and worker (primary) survive; the
        # orphan viewer is dropped, the worker viewer is dropped.
        self.assertEqual(names, ["scratch", "worker"])

    def test_primary_wins_regardless_of_ordering(self):
        # Viewer comes first in tmux output, primary second — primary still wins.
        rows = [
            _tmux_row("worker-v1-1", "worker"),
            _tmux_row("worker", "worker"),
        ]
        out = self._run(rows)
        self.assertEqual([r["name"] for r in out], ["worker"])

    def test_attached_and_activity_fields_preserved(self):
        rows = [_tmux_row("work", "", attached=3, activity=2500)]
        out = self._run(rows)
        self.assertEqual(out[0]["attached"], 3)
        self.assertEqual(out[0]["activity"], 2500)


class ListPanesTests(unittest.TestCase):

    def test_includes_stable_pane_id_alongside_human_target_indices(self):
        row = (
            "work\t2\tlogs\t1\t%9\tpython\t1234\t/tmp/work"
            "\t120\t40\t1\n"
        )
        completed = subprocess.CompletedProcess(
            ["tmux", "list-panes"], 0, row, "",
        )
        with mock.patch(
            "lib.sessions.subprocess.run", return_value=completed,
        ) as run:
            panes = sessions.list_panes()

        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0]["pane_id"], "%9")
        self.assertEqual(panes[0]["window"], "2")
        self.assertEqual(panes[0]["pane"], "1")
        self.assertTrue(panes[0]["active"])
        self.assertIn("#{pane_id}", run.call_args.args[0][-1])


class PasteBufferTests(unittest.TestCase):

    def test_each_paste_uses_a_distinct_named_buffer(self):
        ok = subprocess.CompletedProcess(["tmux"], 0, "", "")
        with mock.patch.object(sessions, "exists", return_value=True), \
             mock.patch.object(sessions.secrets, "token_hex",
                               side_effect=["a" * 16, "b" * 16]), \
             mock.patch.object(sessions.subprocess, "run", return_value=ok) as run:
            self.assertTrue(sessions.paste_buffer(
                sessions.Target(session="a"), "first")[0])
            self.assertTrue(sessions.paste_buffer(
                sessions.Target(session="b"), "second")[0])

        commands = [call.args[0] for call in run.call_args_list]
        first_buffer = commands[0][3]
        second_buffer = commands[2][3]
        self.assertNotEqual(first_buffer, second_buffer)
        self.assertEqual(commands[1][4], first_buffer)
        self.assertEqual(commands[3][4], second_buffer)

    def test_failed_paste_deletes_its_private_buffer(self):
        loaded = subprocess.CompletedProcess(["tmux"], 0, "", "")
        failed = subprocess.CompletedProcess(
            ["tmux"], 1, "", "no target pane")
        deleted = subprocess.CompletedProcess(["tmux"], 0, "", "")
        with mock.patch.object(sessions, "exists", return_value=True), \
             mock.patch.object(sessions.secrets, "token_hex",
                               return_value="c" * 16), \
             mock.patch.object(
                 sessions.subprocess, "run",
                 side_effect=[loaded, failed, deleted],
             ) as run:
            ok, error = sessions.paste_buffer(
                sessions.Target(session="work"), "payload")

        self.assertFalse(ok)
        self.assertIn("no target pane", error)
        self.assertEqual(
            run.call_args_list[2].args[0][:3],
            ["tmux", "delete-buffer", "-b"],
        )


class SelectTargetTests(unittest.TestCase):

    def test_session_only_needs_no_selection_command(self):
        with mock.patch.object(sessions, "exists", return_value=True), \
             mock.patch.object(sessions.subprocess, "run") as run:
            self.assertEqual(
                sessions.select_target(sessions.Target(session="work")),
                (True, ""),
            )
        run.assert_not_called()

    def test_window_selection_uses_exact_target(self):
        result = subprocess.CompletedProcess(["tmux"], 0, "", "")
        target = sessions.Target(session="work", window="2")
        with mock.patch.object(sessions, "exists", return_value=True), \
             mock.patch.object(sessions.subprocess, "run",
                               return_value=result) as run:
            self.assertEqual(sessions.select_target(target), (True, ""))
        self.assertEqual(
            run.call_args.args[0],
            ["tmux", "select-window", "-t", "=work:2"],
        )

    def test_pane_selection_uses_exact_target(self):
        result = subprocess.CompletedProcess(["tmux"], 0, "", "")
        target = sessions.Target(session="work", window="2", pane="1")
        with mock.patch.object(sessions, "exists", return_value=True), \
             mock.patch.object(sessions.subprocess, "run",
                               return_value=result) as run:
            self.assertEqual(sessions.select_target(target), (True, ""))
        self.assertEqual(
            run.call_args.args[0],
            ["tmux", "select-pane", "-t", "=work:2.1"],
        )

    def test_invalid_pane_error_is_returned(self):
        result = subprocess.CompletedProcess(
            ["tmux"], 1, "", "can't find pane: 9")
        target = sessions.Target(session="work", window="9", pane="9")
        with mock.patch.object(sessions, "exists", return_value=True), \
             mock.patch.object(sessions.subprocess, "run",
                               return_value=result):
            ok, error = sessions.select_target(target)
        self.assertFalse(ok)
        self.assertIn("can't find pane", error)


class TimeoutHandlingTests(unittest.TestCase):

    def test_exists_returns_false_on_timeout(self):
        with mock.patch(
            "lib.sessions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=5),
        ):
            self.assertFalse(sessions.exists("demo"))

    def test_kill_returns_error_on_timeout(self):
        with mock.patch(
            "lib.sessions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=5),
        ):
            ok, err = sessions.kill("demo")
        self.assertFalse(ok)
        self.assertIn("timed out", err)

    def test_pane_current_command_returns_none_on_timeout(self):
        with mock.patch(
            "lib.sessions.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=5),
        ):
            cmd = sessions.pane_current_command(sessions.Target(session="demo"))
        self.assertIsNone(cmd)

    def test_capture_target_returns_error_on_timeout(self):
        target = sessions.Target(session="demo")
        with mock.patch.object(sessions, "exists", return_value=True), \
             mock.patch(
                 "lib.sessions.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=10),
             ):
            ok, err = sessions.capture_target(target)
        self.assertFalse(ok)
        self.assertIn("timed out", err)

    def test_capture_target_returns_tmux_error(self):
        target = sessions.Target(session="demo")
        failed = subprocess.CompletedProcess(
            ["tmux", "capture-pane"], 1, "", "bad target",
        )
        with mock.patch.object(sessions, "exists", return_value=True), \
             mock.patch("lib.sessions.subprocess.run", return_value=failed):
            ok, err = sessions.capture_target(target)
        self.assertFalse(ok)
        self.assertEqual(err, "bad target")


class SessionLogLifecycleTests(unittest.TestCase):

    def test_successful_kill_removes_session_log_state(self):
        completed = subprocess.CompletedProcess(["tmux", "kill-session"], 0, "", "")
        with mock.patch("lib.sessions.subprocess.run", return_value=completed), \
             mock.patch("lib.session_logs.remove") as remove:
            ok, err = sessions.kill("work")
        self.assertTrue(ok, err)
        remove.assert_called_once_with("work")

    def test_successful_rename_rewires_logging_and_removes_old_state(self):
        completed = subprocess.CompletedProcess(["tmux", "rename-session"], 0, "", "")
        with mock.patch.object(sessions, "exists", side_effect=[True, False]), \
             mock.patch("lib.sessions.subprocess.run", return_value=completed), \
             mock.patch("lib.session_logs.remove") as remove, \
             mock.patch("lib.session_logs.ensure_logging") as ensure:
            ok, err = sessions.rename("old", "new")
        self.assertTrue(ok, err)
        self.assertEqual(remove.call_args_list, [mock.call("new"), mock.call("old")])
        ensure.assert_called_once_with("new")


if __name__ == "__main__":
    unittest.main()
