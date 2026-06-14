"""``tb watch`` poll loop — must not busy-spin on transient capture failure."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.tb_cmds import observe  # noqa: E402


class WatchLoopTests(unittest.TestCase):

    def test_transient_capture_failure_sleeps_each_iteration(self):
        # capture_target keeps failing while the session is still alive — the
        # exact condition (loaded/slow tmux) under which the old bottom-of-loop
        # sleep was skipped by `continue`, spinning the loop at 100% CPU.
        state = {"captures": 0, "sleeps": 0}

        def fake_capture(_t, lines=100, ansi=False):
            state["captures"] += 1
            if state["captures"] >= 3:
                # Break the otherwise-infinite loop; cmd_watch returns 0 on KI.
                raise KeyboardInterrupt
            return (False, "boom")

        def fake_sleep(_secs):
            state["sleeps"] += 1

        args = argparse.Namespace(
            target="work", lines=100, interval=0.01, json=False, quiet=False)
        with mock.patch.object(observe.sessions, "exists", return_value=True), \
             mock.patch.object(observe.sessions, "capture_target",
                               side_effect=fake_capture), \
             mock.patch.object(observe.time, "sleep", side_effect=fake_sleep):
            rc = observe.cmd_watch(args)

        self.assertEqual(rc, 0)
        # Every capture attempt was preceded by an interval sleep. With the
        # bug (sleep only at the bottom, skipped by `continue`) this would be 0.
        self.assertEqual(state["sleeps"], state["captures"])
        self.assertGreaterEqual(state["sleeps"], 2)


    def test_emits_on_change_and_dedupes_identical_captures(self):
        # First capture seeds state (no emit); an identical capture emits
        # nothing; a changed capture emits one event. This is the behaviour
        # the old hash() compare guarded — now a direct content compare.
        captures = ["same\n", "same\n", "one\ntwo\n"]

        def fake_capture(_t, lines=100, ansi=False):
            if not captures:
                raise KeyboardInterrupt
            return (True, captures.pop(0))

        args = argparse.Namespace(
            target="work", lines=100, interval=0.01, json=True, quiet=False)
        buf = io.StringIO()
        with mock.patch.object(observe.sessions, "exists", return_value=True), \
             mock.patch.object(observe.sessions, "capture_target",
                               side_effect=fake_capture), \
             mock.patch.object(observe.time, "sleep"), \
             contextlib.redirect_stdout(buf):
            rc = observe.cmd_watch(args)

        self.assertEqual(rc, 0)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, lines)  # only the change emitted
        self.assertEqual(json.loads(lines[0])["last_line"], "two")


if __name__ == "__main__":
    unittest.main()
