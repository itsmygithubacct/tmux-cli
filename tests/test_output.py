import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import output  # noqa: E402
from lib.errors import SessionNotFound  # noqa: E402


class EmitJsonTests(unittest.TestCase):

    def test_success_envelope(self):
        buf = io.StringIO()
        output.emit_json({"a": 1}, stream=buf)
        data = json.loads(buf.getvalue())
        self.assertEqual(data, {"ok": True, "data": {"a": 1}})

    def test_error_envelope_carries_code_and_exit(self):
        buf = io.StringIO()
        output.emit_error_json(SessionNotFound("no such session: work"), stream=buf)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["ok"], False)
        self.assertEqual(data["error"], "no such session: work")
        self.assertEqual(data["code"], "ENOENT")
        self.assertEqual(data["exit"], 3)


class EmitTableTests(unittest.TestCase):

    def test_empty_rows_renders_empty_message(self):
        buf = io.StringIO()
        output.emit_table([], [("n", "NAME")], empty_message="(nothing)", stream=buf)
        self.assertIn("(nothing)", buf.getvalue())

    def test_none_renders_as_dash(self):
        buf = io.StringIO()
        output.emit_table(
            [{"n": "a", "v": None}, {"n": "b", "v": 2}],
            [("n", "NAME"), ("v", "V")],
            no_header=True, stream=buf,
        )
        out = buf.getvalue().splitlines()
        self.assertEqual(len(out), 2)
        self.assertIn("-", out[0])  # first row's None → "-"

    def test_column_widths_track_longest_cell(self):
        buf = io.StringIO()
        output.emit_table(
            [{"n": "short"}, {"n": "muchlonger"}],
            [("n", "N")],
            no_header=True, stream=buf,
        )
        # Both rows padded to width of "muchlonger" = 10
        self.assertEqual(
            [len(line.rstrip()) for line in buf.getvalue().splitlines()],
            [len("short"), len("muchlonger")],  # rstrip removes padding
        )


class EmitPlainTests(unittest.TestCase):

    def test_adds_trailing_newline_when_missing(self):
        buf = io.StringIO()
        output.emit_plain("hello", stream=buf)
        self.assertEqual(buf.getvalue(), "hello\n")

    def test_preserves_existing_trailing_newline(self):
        buf = io.StringIO()
        output.emit_plain("hello\n", stream=buf)
        self.assertEqual(buf.getvalue(), "hello\n")

    def test_empty_string_emits_nothing(self):
        buf = io.StringIO()
        output.emit_plain("", stream=buf)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
