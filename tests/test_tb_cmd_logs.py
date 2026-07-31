"""CLI coverage for permanent ``tb logs`` commands."""

import base64
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tb  # noqa: E402
from lib import session_log_writer, session_logs  # noqa: E402
from lib.tb_cmds import logs  # noqa: E402


class _IsolatedLogCommands:
    CAPTURE = "0123456789abcdef0123456789abcdef"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        values = {
            "ROOT_DIR": self.root,
            "LIVE_DIR": self.root / "logs" / "live",
            "ARCHIVE_DIR": self.root / "logs" / "archive",
            "METADATA_DIR": self.root / "logs" / "metadata",
            "RUNTIME_DIR": self.root / "runtime",
            "ACTIVITY_DIR": self.root / "runtime" / "activity",
            "LOCK_DIR": self.root / "locks",
            "LEGACY_LOG_DIR": self.root / "legacy",
        }
        values.update({
            "LOG_DIR": values["ACTIVITY_DIR"],
            "DIAGNOSTIC_PATH": values["RUNTIME_DIR"] / "logging-errors.log",
            "REAPER_LOCK": values["LOCK_DIR"] / "log-reaper.lock",
        })
        self._paths = mock.patch.multiple(session_logs, **values)
        self._paths.start()
        session_logs._hash_state.clear()

    def tearDown(self):
        self._paths.stop()
        self._tmp.cleanup()

    def _capture(self, payload=b"first line\nmigration complete\n"):
        archive_dir = session_logs.ARCHIVE_DIR / "2026" / "07" / "29"
        writer = session_log_writer.PermanentCaptureWriter(
            source=BytesIO(payload),
            activity_path=session_logs.log_path("work"),
            live_dir=session_logs.LIVE_DIR,
            archive_dir=archive_dir,
            metadata_dir=session_logs.METADATA_DIR,
            lock_dir=session_logs.LOCK_DIR,
            diagnostic_path=session_logs.DIAGNOSTIC_PATH,
            capture_id=self.CAPTURE,
            archive_prefix=(
                "20260729T071530.123456Z--work--p1--" + self.CAPTURE
            ),
            session_name="work",
            tmux_session_id="$0",
            pane_id="%1",
            window_index="0",
            pane_index="1",
            initial_cwd="/tmp/project",
            initial_command="bash",
            started_utc="2026-07-29T07:15:30.123456Z",
            activity_max_bytes=1024,
            activity_keep_bytes=768,
            segment_bytes=12,
        )
        writer.run()


@unittest.skipUnless(shutil.which("zstd"), "zstd is required")
class LogCommandTests(_IsolatedLogCommands, unittest.TestCase):

    def test_parser_marks_every_logs_verb_serverless(self):
        parser = tb._build_parser()
        for argv in (
            ["logs"],
            ["logs", "list"],
            ["logs", "path"],
            ["logs", "show", self.CAPTURE],
            ["logs", "grep", "text"],
            ["logs", "verify"],
            ["logs", "recover"],
            ["logs", "manual"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(
                    getattr(parser.parse_args(argv), "needs_server", True),
                )

    def test_list_json_reports_capture_and_root(self):
        self._capture()
        args = types.SimpleNamespace(
            session=None,
            pane=None,
            json=True,
            quiet=False,
            no_header=False,
        )
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream):
            logs.cmd_logs_list(args)
        value = json.loads(stream.getvalue())
        self.assertTrue(value["ok"])
        self.assertEqual(value["data"]["path"], str(self.root))
        self.assertEqual(
            value["data"]["captures"][0]["capture_id"], self.CAPTURE,
        )

    def test_show_json_round_trips_arbitrary_bytes(self):
        payload = b"\x00hello\x1b[31mred\xff\n"
        self._capture(payload)
        args = types.SimpleNamespace(
            capture=self.CAPTURE[:12],
            json=True,
            quiet=False,
        )
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream):
            logs.cmd_logs_show(args)
        value = json.loads(stream.getvalue())["data"]
        self.assertEqual(base64.b64decode(value["content"]), payload)

    def test_grep_searches_across_compressed_segments(self):
        self._capture()
        args = types.SimpleNamespace(
            pattern="migration",
            fixed=True,
            ignore_case=True,
            capture=None,
            session="work",
            pane=None,
            json=True,
            quiet=False,
        )
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream):
            rc = logs.cmd_logs_grep(args)
        self.assertEqual(rc, 0)
        matches = json.loads(stream.getvalue())["data"]["matches"]
        self.assertEqual(len(matches), 1)
        self.assertIn("migration complete", matches[0]["text"])

    def test_verify_accepts_valid_frames_and_contiguous_sequence(self):
        self._capture()
        args = types.SimpleNamespace(
            capture=self.CAPTURE[:8],
            json=True,
            quiet=False,
            no_header=False,
        )
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream):
            self.assertEqual(logs.cmd_logs_verify(args), 0)
        result = json.loads(stream.getvalue())["data"]["captures"][0]
        self.assertTrue(result["ok"])

    def test_json_verify_failure_does_not_emit_success_first(self):
        self._capture()
        archive = next(session_logs.ARCHIVE_DIR.rglob("*.log.zst"))
        archive.write_bytes(b"corrupt")
        args = types.SimpleNamespace(
            capture=self.CAPTURE,
            json=True,
            quiet=False,
            no_header=False,
        )
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream):
            with self.assertRaises(logs.StateError):
                logs.cmd_logs_verify(args)
        self.assertEqual(stream.getvalue(), "")

    def test_recover_requests_complete_legacy_import(self):
        result = {
            "archived": 1,
            "pending": 0,
            "legacy_archived": 2,
            "legacy_pending": 0,
            "errors": 0,
            "busy": 0,
        }
        args = types.SimpleNamespace(
            no_legacy=False,
            json=True,
            quiet=False,
        )
        stream = io.StringIO()
        with mock.patch.object(
            logs.session_logs, "recover", return_value=result,
        ) as recover, mock.patch.object(sys, "stdout", stream):
            logs.cmd_logs_recover(args)
        recover.assert_called_once_with(
            migrate_legacy=True,
            legacy_limit=None,
        )


if __name__ == "__main__":
    unittest.main()
