"""Verify every error class maps to the documented exit code."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.errors import (  # noqa: E402
    AuthError,
    NoTmuxServer,
    SessionExists,
    SessionNotFound,
    StateError,
    TBError,
    Timeout,
    TmuxFailed,
    UsageError,
)


class ErrorShapeTests(unittest.TestCase):

    CASES = [
        (UsageError,       "EUSAGE",    2),
        (SessionNotFound,  "ENOENT",    3),
        (SessionExists,    "EEXIST",    4),
        (Timeout,          "ETIMEDOUT", 5),
        (NoTmuxServer,     "ENOSERVER", 6),
        (TmuxFailed,       "ETMUX",     7),
        (StateError,       "ESTATE",    8),
        (AuthError,        "EAUTH",     9),
    ]

    def test_each_class_has_expected_code_and_exit(self):
        for cls, code, exit_code in self.CASES:
            with self.subTest(cls=cls.__name__):
                self.assertEqual(cls.code, code)
                self.assertEqual(cls.exit_code, exit_code)

    def test_message_defaults_to_code_when_omitted(self):
        e = SessionNotFound()
        self.assertEqual(e.message, "ENOENT")

    def test_message_preserved_when_given(self):
        e = SessionNotFound("no such session: work")
        self.assertEqual(e.message, "no such session: work")

    def test_base_class_has_unknown_code(self):
        e = TBError()
        self.assertEqual(e.code, "EUNKNOWN")
        self.assertEqual(e.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
