import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.targeting import Target, parse  # noqa: E402


class ParseTests(unittest.TestCase):

    def test_session_only(self):
        t = parse("work")
        self.assertEqual(t, Target(session="work"))
        self.assertEqual(t.as_tmux_target(), "work:")

    def test_session_window(self):
        t = parse("work:2")
        self.assertEqual(t, Target(session="work", window="2"))
        self.assertEqual(t.as_tmux_target(), "work:2")

    def test_session_window_pane(self):
        t = parse("work:2.1")
        self.assertEqual(t, Target(session="work", window="2", pane="1"))
        self.assertEqual(t.as_tmux_target(), "work:2.1")

    def test_empty_window_suffix_yields_session_form(self):
        # "work:" is equivalent to "work" — bare session, active pane.
        t = parse("work:")
        self.assertEqual(t.window, None)
        self.assertEqual(t.pane, None)
        self.assertEqual(t.as_tmux_target(), "work:")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse("")

    def test_missing_session_raises(self):
        with self.assertRaises(ValueError):
            parse(":2")

    def test_str_uses_as_tmux_target(self):
        self.assertEqual(str(Target(session="w")), "w:")
        self.assertEqual(str(Target(session="w", window="1", pane="0")), "w:1.0")

    def test_named_window(self):
        t = parse("work:main")
        self.assertEqual(t, Target(session="work", window="main"))
        self.assertEqual(t.as_tmux_target(), "work:main")

    def test_session_names_with_dashes_and_underscores(self):
        t = parse("bot-sessions_v2:0")
        self.assertEqual(t.session, "bot-sessions_v2")
        self.assertEqual(t.window, "0")


class FrozenDataclassTests(unittest.TestCase):

    def test_target_is_frozen(self):
        t = Target(session="w")
        with self.assertRaises(Exception):  # dataclasses.FrozenInstanceError
            t.session = "other"  # type: ignore[misc]

    def test_targets_compare_by_value(self):
        self.assertEqual(Target("a"), Target("a"))
        self.assertEqual(Target("a", "1"), Target("a", "1"))
        self.assertNotEqual(Target("a"), Target("b"))


if __name__ == "__main__":
    unittest.main()
