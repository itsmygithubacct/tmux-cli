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


if __name__ == "__main__":
    unittest.main()
