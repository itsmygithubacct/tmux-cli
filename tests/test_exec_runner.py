"""Pure helpers in exec_runner: _extract, is_shell_pane, _poll_until."""

import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import exec_runner  # noqa: E402
from lib.targeting import Target  # noqa: E402


class IsShellPaneTests(unittest.TestCase):

    def _run(self, cmd: str | None) -> bool:
        with mock.patch.object(exec_runner.sessions, "pane_current_command",
                               return_value=cmd):
            return exec_runner.is_shell_pane(Target(session="w"))

    def test_common_shells_recognized(self):
        for shell in ("bash", "zsh", "fish", "sh", "dash", "ksh"):
            self.assertTrue(self._run(shell), shell)

    def test_case_insensitive(self):
        self.assertTrue(self._run("BASH"))
        self.assertTrue(self._run("Zsh"))

    def test_non_shell_rejected(self):
        self.assertFalse(self._run("vim"))
        self.assertFalse(self._run("python3"))
        self.assertFalse(self._run("nvim"))

    def test_none_is_not_shell(self):
        self.assertFalse(self._run(None))


class ExtractTests(unittest.TestCase):

    def test_returns_content_between_markers(self):
        content = (
            "prompt $ run\n"
            "__TB_abc_START__\n"
            "line one\n"
            "line two\n"
            "__TB_abc_END_0__\n"
        )
        end = re.search(r"^__TB_abc_END_(\d+)__$", content, re.MULTILINE)
        out = exec_runner._extract(content, "__TB_abc_START__", end)
        self.assertEqual(out, "line one\nline two")

    def test_last_start_marker_wins(self):
        # The wrapped command echoes START once; printf emits it again
        content = (
            "user typed __TB_tag_START__; ...\n"  # echoed
            "__TB_tag_START__\n"                  # the real one
            "real output\n"
            "__TB_tag_END_0__\n"
        )
        end = re.search(r"^__TB_tag_END_(\d+)__$", content, re.MULTILINE)
        out = exec_runner._extract(content, "__TB_tag_START__", end)
        self.assertEqual(out, "real output")

    def test_missing_start_marker_returns_preceding_text(self):
        # START marker never fires; END marker does. _extract should return
        # everything preceding the END line (minus trailing newlines).
        content = "some noise\noutput without prelude\n__TB_x_END_2__\n"
        end = re.search(r"^__TB_x_END_(\d+)__$", content, re.MULTILINE)
        self.assertIsNotNone(end)
        out = exec_runner._extract(content, "__TB_x_START__", end)
        self.assertIn("some noise", out)
        self.assertIn("output without prelude", out)


class PollUntilTests(unittest.TestCase):

    def test_returns_hit_immediately(self):
        deadline = time.monotonic() + 5
        hit, val = exec_runner._poll_until(
            lambda: (True, "found"),
            deadline=deadline, interval=0.01,
        )
        self.assertTrue(hit)
        self.assertEqual(val, "found")

    def test_returns_timeout_with_last_value(self):
        seen: list[int] = []

        def never_hits():
            seen.append(1)
            return False, "last"

        # Short deadline so we don't slow the suite
        deadline = time.monotonic() + 0.1
        hit, val = exec_runner._poll_until(
            never_hits, deadline=deadline, interval=0.02,
        )
        self.assertFalse(hit)
        self.assertEqual(val, "last")
        self.assertGreaterEqual(len(seen), 2)  # polled more than once

    def test_hits_after_several_polls(self):
        counter = {"n": 0}

        def hits_on_third():
            counter["n"] += 1
            return (counter["n"] >= 3), counter["n"]

        deadline = time.monotonic() + 5
        hit, val = exec_runner._poll_until(
            hits_on_third, deadline=deadline, interval=0.01,
        )
        self.assertTrue(hit)
        self.assertEqual(val, 3)


class DispatchTests(unittest.TestCase):

    def test_auto_picks_sentinel_for_shell_pane(self):
        with mock.patch.object(exec_runner, "is_shell_pane", return_value=True), \
             mock.patch.object(exec_runner, "exec_sentinel",
                               return_value={"strategy": "sentinel"}) as s, \
             mock.patch.object(exec_runner, "exec_idle") as i:
            out = exec_runner.run(Target(session="w"), "ls", strategy="auto")
        s.assert_called_once()
        i.assert_not_called()
        self.assertEqual(out["strategy"], "sentinel")

    def test_auto_picks_idle_for_non_shell_pane(self):
        with mock.patch.object(exec_runner, "is_shell_pane", return_value=False), \
             mock.patch.object(exec_runner, "exec_idle",
                               return_value={"strategy": "idle"}) as i, \
             mock.patch.object(exec_runner, "exec_sentinel") as s:
            out = exec_runner.run(Target(session="w"), "ls", strategy="auto")
        i.assert_called_once()
        s.assert_not_called()
        self.assertEqual(out["strategy"], "idle")

    def test_unknown_strategy_returns_error_dict(self):
        out = exec_runner.run(Target(session="w"), "ls", strategy="nonesuch")
        self.assertEqual(out["ok"], False)


if __name__ == "__main__":
    unittest.main()
