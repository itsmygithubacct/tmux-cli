"""Permanent zstd pane capture and content-based idle detection."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import session_log_writer, session_logs  # noqa: E402


class _IsolatedLogTree:
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
        self._patch = mock.patch.multiple(session_logs, **values)
        self._patch.start()
        session_logs._hash_state.clear()
        session_logs._last_ensure_ts = 0

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()


class LogPathSanitizationTests(_IsolatedLogTree, unittest.TestCase):

    def test_plain_name_passes_through_unchanged(self):
        self.assertEqual(session_logs.log_path("work").name, "work.log")

    def test_path_significant_chars_are_encoded(self):
        path = session_logs.log_path("foo/bar")
        self.assertEqual(path.parent, session_logs.ACTIVITY_DIR)
        self.assertNotIn("/", path.name[:-len(".log")])
        self.assertNotEqual(
            session_logs.log_path("foo/bar"),
            session_logs.log_path("foo_bar"),
        )


class ActivityTsTests(_IsolatedLogTree, unittest.TestCase):

    def test_returns_none_when_no_log(self):
        self.assertIsNone(session_logs.activity_ts("ghost"))

    def test_first_observation_anchors_to_now(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        self.assertEqual(session_logs.activity_ts("work", now=1000), 1000)

    def test_stable_hash_preserves_timestamp(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        first = session_logs.activity_ts("work", now=1000)
        again = session_logs.activity_ts("work", now=1050)
        self.assertEqual(first, 1000)
        self.assertEqual(again, 1000)

    def test_hash_change_bumps_timestamp(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"hello")
        session_logs.activity_ts("work", now=1000)
        path.write_bytes(b"hello world")
        self.assertEqual(session_logs.activity_ts("work", now=1060), 1060)

    def test_idle_seconds_and_forget(self):
        path = session_logs.log_path("work")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        session_logs.activity_ts("work", now=1000)
        self.assertEqual(session_logs.idle_seconds("work", now=1030), 30)
        session_logs.forget("work")
        self.assertNotIn("work", session_logs._hash_state)

    def test_tail_hashing_detects_trailing_append(self):
        path = session_logs.log_path("big")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"a" * (session_logs._TAIL_BYTES * 2))
        session_logs.activity_ts("big", now=1000)
        with path.open("ab") as stream:
            stream.write(b"Z")
        self.assertEqual(session_logs.activity_ts("big", now=1010), 1010)


class EnsureLoggingTests(_IsolatedLogTree, unittest.TestCase):

    _PANES = (
        "%0\t$0\t0\t0\t/tmp/project\tbash\n"
        "%1\t$0\t0\t1\t/tmp/project\tpython\n"
    )

    def test_ensure_logging_calls_permanent_writer_for_each_pane(self):
        with mock.patch("lib.session_logs.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout=self._PANES),
                mock.Mock(returncode=0, stdout=""),
                mock.Mock(returncode=0, stdout=""),
            ]
            session_logs.ensure_logging("work")
        self.assertEqual(run.call_count, 3)
        commands = []
        for call in run.call_args_list[1:]:
            argv = call.args[0]
            self.assertEqual(argv[:2], ["tmux", "pipe-pane"])
            self.assertNotIn("-o", argv)
            self.assertIn("session_log_writer.py", argv[-1])
            self.assertIn("--segment-bytes", argv[-1])
            self.assertIn("--archive-prefix", argv[-1])
            commands.append(argv[-1])
        # Each pane receives a unique random capture ID.
        self.assertNotEqual(commands[0], commands[1])
        self.assertTrue(session_logs._writer_marker("work").is_file())

    def test_subsequent_ensure_keeps_existing_pipe(self):
        session_logs._ensure_dir()
        session_logs._writer_marker("work").touch()
        with mock.patch("lib.session_logs.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(
                    returncode=0,
                    stdout="%0\t$0\t0\t0\t/tmp\tbash\n",
                ),
                mock.Mock(returncode=0, stdout=""),
            ]
            session_logs.ensure_logging("work")
        self.assertEqual(
            run.call_args_list[1].args[0][:3],
            ["tmux", "pipe-pane", "-o"],
        )

    def test_ensure_logging_all_throttles(self):
        with mock.patch.object(
            session_logs, "_list_sessions", return_value=[],
        ) as listed, mock.patch.object(session_logs, "prune"):
            session_logs.ensure_logging_all(force=True)
            session_logs.ensure_logging_all()
        listed.assert_called_once()

    def test_raw_session_enumeration_keeps_hidden_viewer_names(self):
        completed = mock.Mock(
            returncode=0,
            stdout="work\nwork-v123-abcd\n",
            stderr="",
        )
        with mock.patch(
            "lib.session_logs.subprocess.run", return_value=completed,
        ):
            self.assertEqual(
                session_logs._all_tmux_sessions(),
                {"work", "work-v123-abcd"},
            )

    def test_prune_removes_only_disposable_old_orphan_state(self):
        session_logs._ensure_dir()
        active_log = session_logs.log_path("active")
        old_log = session_logs.log_path("old")
        fresh_log = session_logs.log_path("fresh")
        old_marker = session_logs._writer_marker("old")
        for path in (active_log, old_log, fresh_log, old_marker):
            path.write_bytes(b"x")
        now = time.time()
        old_time = now - session_logs._ORPHAN_GRACE_SEC - 1
        os.utime(old_log, (old_time, old_time))
        os.utime(old_marker, (old_time, old_time))

        with mock.patch.object(session_logs, "recover") as recover:
            removed = session_logs.prune(["active"], now=now)

        self.assertEqual(removed, 2)
        self.assertTrue(active_log.exists())
        self.assertTrue(fresh_log.exists())
        self.assertFalse(old_log.exists())
        self.assertFalse(old_marker.exists())
        recover.assert_called_once_with(
            None,
            now=now,
            migrate_legacy=False,
            legacy_limit=None,
        )

    def test_discard_runtime_never_removes_archive(self):
        session_logs._ensure_dir()
        session_logs.log_path("work").write_bytes(b"activity")
        session_logs._writer_marker("work").touch()
        archive = session_logs.ARCHIVE_DIR / "keep.log.zst"
        archive.write_bytes(b"permanent")
        session_logs._hash_state["work"] = ("hash", 1)

        self.assertEqual(session_logs.discard_runtime("work"), 2)
        self.assertTrue(archive.exists())
        self.assertNotIn("work", session_logs._hash_state)


class BoundedActivityWriterTests(unittest.TestCase):

    def test_rotation_keeps_the_newest_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "work.log"
            path.write_bytes(b"01234567")
            session_log_writer.append_chunk(
                path, b"89AB", max_bytes=10, keep_bytes=6,
            )
            self.assertEqual(path.read_bytes(), b"6789AB")

    def test_live_pipe_writes_available_bytes_without_waiting_for_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "work.log"
            read_fd, write_fd = os.pipe()
            errors = []

            def copy_pipe():
                try:
                    with os.fdopen(read_fd, "rb") as source:
                        session_log_writer.copy_bounded(
                            source, path, max_bytes=1024, keep_bytes=768,
                        )
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=copy_pipe)
            thread.start()
            try:
                os.write(write_fd, b"small interactive update\n")
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    if path.exists() and path.stat().st_size:
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    path.read_bytes(), b"small interactive update\n",
                )
            finally:
                os.close(write_fd)
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_write_all_retries_short_writes(self):
        class ShortWriter:
            def __init__(self):
                self.value = bytearray()

            def write(self, data):
                amount = min(2, len(data))
                self.value.extend(data[:amount])
                return amount

        stream = ShortWriter()
        session_log_writer._write_all(stream, b"abcdef")
        self.assertEqual(stream.value, b"abcdef")


@unittest.skipUnless(shutil.which("zstd"), "zstd is required")
class PermanentCaptureWriterTests(_IsolatedLogTree, unittest.TestCase):

    CAPTURE = "0123456789abcdef0123456789abcdef"
    PREFIX = (
        "20260729T071530.123456Z--work--p1--"
        "0123456789abcdef0123456789abcdef"
    )

    def _writer(self, payload, *, segment_bytes=10):
        archive = session_logs.ARCHIVE_DIR / "2026" / "07" / "29"
        return session_log_writer.PermanentCaptureWriter(
            source=BytesIO(payload),
            activity_path=session_logs.log_path("work"),
            live_dir=session_logs.LIVE_DIR,
            archive_dir=archive,
            metadata_dir=session_logs.METADATA_DIR,
            lock_dir=session_logs.LOCK_DIR,
            diagnostic_path=session_logs.DIAGNOSTIC_PATH,
            capture_id=self.CAPTURE,
            archive_prefix=self.PREFIX,
            session_name="work",
            tmux_session_id="$0",
            pane_id="%1",
            window_index="0",
            pane_index="1",
            initial_cwd="/tmp/project",
            initial_command="bash",
            started_utc="2026-07-29T07:15:30.123456Z",
            activity_max_bytes=12,
            activity_keep_bytes=10,
            segment_bytes=segment_bytes,
        )

    def test_multisegment_capture_round_trips_without_loss(self):
        payload = b"abcdefghijklmnopqrstuvwxyz"
        self._writer(payload).run()

        archives = sorted(session_logs.ARCHIVE_DIR.rglob("*.log.zst"))
        self.assertEqual(len(archives), 3)
        decoded = b"".join(
            subprocess.run(
                ["zstd", "-dcq", "--", str(path)],
                check=True,
                capture_output=True,
            ).stdout
            for path in archives
        )
        self.assertEqual(decoded, payload)
        self.assertEqual(session_logs.log_path("work").read_bytes(), payload[-10:])
        self.assertEqual(list(session_logs.LIVE_DIR.glob("*.log")), [])
        self.assertFalse(session_logs._capture_lock(self.CAPTURE).exists())

        metadata = json.loads(
            (session_logs.METADATA_DIR / f"{self.CAPTURE}.json").read_text(),
        )
        self.assertEqual(metadata["compression"], "zstd-3")
        self.assertEqual(metadata["state"], "closed")
        self.assertEqual(len(metadata["segments"]), 3)
        for path in archives:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            subprocess.run(
                ["zstd", "-tq", "--", str(path)],
                check=True,
            )

    def test_list_resolve_and_verify_capture(self):
        self._writer(b"hello terminal\n", segment_bytes=1024).run()
        rows = session_logs.list_captures(session="work")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["capture_id"], self.CAPTURE)
        self.assertEqual(
            session_logs.resolve_capture(self.CAPTURE[:12]), self.CAPTURE,
        )
        self.assertTrue(session_logs.verify_capture(self.CAPTURE)["ok"])

    def test_compression_failure_preserves_plaintext(self):
        source = self.root / "source.log"
        archive = self.root / "archive" / "source.log.zst"
        source.write_bytes(b"do not lose me")
        failed = subprocess.CompletedProcess(
            ["zstd"], 1, b"", b"simulated failure",
        )
        with mock.patch(
            "lib.session_log_writer.subprocess.run", return_value=failed,
        ):
            ok = session_log_writer.finalize_segment(source, archive)
        self.assertFalse(ok)
        self.assertEqual(source.read_bytes(), b"do not lose me")
        self.assertFalse(archive.exists())

    def test_finalizer_invokes_exact_zstd_level_3_command(self):
        source = self.root / "source.log"
        archive = self.root / "archive" / "source.log.zst"
        source.write_bytes(b"level three")
        real_run = subprocess.run
        with mock.patch(
            "lib.session_log_writer.subprocess.run",
            side_effect=real_run,
        ) as run:
            self.assertTrue(
                session_log_writer.finalize_segment(source, archive),
            )
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[:5], ["zstd", "-3", "-q", "-f", "-o"])
        self.assertIn("--", command)

    def test_finalizer_refuses_source_and_destination_symlinks(self):
        real_source = self.root / "real-source.log"
        real_source.write_bytes(b"source bytes")
        source_link = self.root / "source.log"
        source_link.symlink_to(real_source)
        archive = self.root / "archive" / "source.log.zst"
        self.assertFalse(
            session_log_writer.finalize_segment(source_link, archive),
        )
        self.assertEqual(real_source.read_bytes(), b"source bytes")
        self.assertFalse(archive.exists())

        source = self.root / "second-source.log"
        source.write_bytes(b"more source bytes")
        archive.parent.mkdir(parents=True)
        victim = self.root / "victim"
        victim.write_bytes(b"do not replace")
        archive.symlink_to(victim)
        self.assertFalse(
            session_log_writer.finalize_segment(source, archive),
        )
        self.assertEqual(source.read_bytes(), b"more source bytes")
        self.assertEqual(victim.read_bytes(), b"do not replace")

    def test_false_success_without_archive_bytes_preserves_plaintext(self):
        source = self.root / "source.log"
        archive = self.root / "archive" / "source.log.zst"
        source.write_bytes(b"mocked commands must not erase this")
        succeeded_without_output = subprocess.CompletedProcess(
            ["zstd"], 0, b"", b"",
        )
        with mock.patch(
            "lib.session_log_writer.subprocess.run",
            return_value=succeeded_without_output,
        ):
            ok = session_log_writer.finalize_segment(source, archive)
        self.assertFalse(ok)
        self.assertEqual(
            source.read_bytes(), b"mocked commands must not erase this",
        )
        self.assertFalse(archive.exists())

    def test_empty_recovered_segment_is_a_valid_frame(self):
        source = self.root / "source.log"
        archive = self.root / "archive" / "source.log.zst"
        source.touch()

        self.assertTrue(
            session_log_writer.finalize_segment(source, archive),
        )
        self.assertFalse(source.exists())
        self.assertGreater(archive.stat().st_size, 0)
        decoded = subprocess.run(
            ["zstd", "-dcq", "--", str(archive)],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(decoded, b"")

    def test_legacy_mode_retains_even_an_empty_source(self):
        source = self.root / "source.log"
        archive = self.root / "archive" / "source.log.zst"
        source.touch()

        self.assertTrue(
            session_log_writer.finalize_segment(
                source,
                archive,
                remove_source=False,
            ),
        )
        self.assertTrue(source.exists())
        subprocess.run(
            ["zstd", "-tq", "--", str(archive)],
            check=True,
        )

    def test_nonempty_but_invalid_mock_archive_preserves_plaintext(self):
        source = self.root / "source.log"
        archive = self.root / "archive" / "source.log.zst"
        source.write_bytes(b"round-trip validation protects this")

        def false_success(argv, **_kwargs):
            if "-o" in argv:
                output = Path(argv[argv.index("-o") + 1])
                output.write_bytes(b"not actually a zstd frame")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch(
            "lib.session_log_writer.subprocess.run",
            side_effect=false_success,
        ):
            ok = session_log_writer.finalize_segment(source, archive)
        self.assertFalse(ok)
        self.assertEqual(
            source.read_bytes(), b"round-trip validation protects this",
        )
        self.assertFalse(archive.exists())

    def test_recovery_compresses_abandoned_spool(self):
        session_logs._ensure_dir()
        capture = "fedcba9876543210fedcba9876543210"
        source = session_logs.LIVE_DIR / f"{capture}--000000.log"
        source.write_bytes(b"abandoned but intact")
        os.utime(source, (1, 1))

        result = session_logs.recover(
            active_sessions=[],
            now=1000,
            migrate_legacy=False,
        )

        self.assertEqual(result["archived"], 1)
        self.assertFalse(source.exists())
        paths = session_logs.capture_paths(capture)
        self.assertEqual(len(paths), 1)
        decoded = subprocess.run(
            ["zstd", "-dcq", "--", str(paths[0][1])],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(decoded, b"abandoned but intact")

    def test_recovery_without_legacy_does_not_enumerate_tmux_sessions(self):
        with mock.patch.object(session_logs, "_all_tmux_sessions") as listed:
            session_logs.recover(
                active_sessions=None,
                now=1000,
                migrate_legacy=False,
            )
        listed.assert_not_called()

    def test_recovery_never_touches_locked_live_capture(self):
        session_logs._ensure_dir()
        capture = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        source = session_logs.LIVE_DIR / f"{capture}--000000.log"
        source.write_bytes(b"still live")
        os.utime(source, (1, 1))
        lock_path = session_logs._capture_lock(capture)
        lock_path.touch()
        lock_fd = os.open(lock_path, os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            result = session_logs.recover(
                active_sessions=[],
                now=1000,
                migrate_legacy=False,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertEqual(result["archived"], 0)
        self.assertEqual(result["pending"], 1)
        self.assertTrue(source.exists())

    def test_legacy_log_is_copied_only_after_valid_archive(self):
        session_logs._ensure_dir()
        session_logs.LEGACY_LOG_DIR.mkdir()
        source = session_logs.LEGACY_LOG_DIR / "old-work.log"
        source.write_bytes(b"legacy transcript")
        os.utime(source, (1, 1))

        result = session_logs.recover(
            active_sessions=[],
            now=1000,
            migrate_legacy=True,
        )

        self.assertEqual(result["legacy_archived"], 1)
        self.assertEqual(source.read_bytes(), b"legacy transcript")
        archives = list(session_logs.ARCHIVE_DIR.rglob("*.log.zst"))
        self.assertEqual(len(archives), 1)
        decoded = subprocess.run(
            ["zstd", "-dcq", "--", str(archives[0])],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(decoded, b"legacy transcript")

        repeated = session_logs.recover(
            active_sessions=[],
            now=1000,
            migrate_legacy=True,
        )
        self.assertEqual(repeated["legacy_archived"], 0)
        self.assertEqual(source.read_bytes(), b"legacy transcript")

    def test_uncertain_tmux_enumeration_blocks_legacy_mutation(self):
        session_logs._ensure_dir()
        session_logs.LEGACY_LOG_DIR.mkdir()
        source = session_logs.LEGACY_LOG_DIR / "possibly-live.log"
        source.write_bytes(b"leave this alone")
        os.utime(source, (1, 1))

        with mock.patch.object(
            session_logs, "_all_tmux_sessions", return_value=None,
        ):
            result = session_logs.recover(
                active_sessions=None,
                now=1000,
                migrate_legacy=True,
            )

        self.assertEqual(result["legacy_archived"], 0)
        self.assertTrue(source.exists())

    def test_reader_prefers_plaintext_over_corrupt_duplicate_archive(self):
        session_logs._ensure_dir()
        capture = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        source = session_logs.LIVE_DIR / f"{capture}--000000.log"
        source.write_bytes(b"intact plaintext")
        archive_dir = session_logs.ARCHIVE_DIR / "2026" / "07" / "29"
        archive_dir.mkdir(parents=True)
        archive = archive_dir / (
            f"20260729T000000.000000Z--work--p1--{capture}"
            "--000000.log.zst"
        )
        archive.write_bytes(b"corrupt")

        paths = session_logs.capture_paths(capture)

        self.assertEqual(paths, [(0, source, False)])
        verified = session_logs.verify_capture(capture)
        self.assertFalse(verified["ok"])
        self.assertEqual(verified["corrupt"], [str(archive)])

    def test_verify_reports_missing_initial_and_metadata_segments(self):
        session_logs._ensure_dir()
        capture = "cccccccccccccccccccccccccccccccc"
        source = session_logs.LIVE_DIR / f"{capture}--000002.log"
        source.write_bytes(b"third segment")
        metadata = {
            "capture_id": capture,
            "segments": [
                {"sequence": 0},
                {"sequence": 1},
                {"sequence": 2},
            ],
        }
        (session_logs.METADATA_DIR / f"{capture}.json").write_text(
            json.dumps(metadata),
        )

        verified = session_logs.verify_capture(capture)

        self.assertFalse(verified["ok"])
        self.assertEqual(verified["missing_sequences"], [0, 1])


if __name__ == "__main__":
    unittest.main()
