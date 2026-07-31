"""Permanent pane capture plus a bounded activity tail for tmux ``pipe-pane``.

Each process owns one pane capture. Pane bytes are split into bounded plaintext
spools and every completed spool is atomically archived with ``zstd -3``.
Separately, all panes in a session append to one small lock-protected activity
tail used by ``session_logs.idle_seconds``.

The permanent side is lossless: rollover starts a new segment instead of
discarding old bytes, and plaintext is removed only after the compressed frame
has passed ``zstd -t``, decompressed to the exact source bytes, and been
published.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO


_COPY_CHUNK = 64 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z",
    )


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"refusing non-directory log path: {path}")
    path.chmod(0o700)


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish owner-only JSON without exposing a partial sidecar."""
    _ensure_private_dir(path.parent)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _diagnose(path: Path | None, message: str) -> None:
    if path is None:
        return
    try:
        _ensure_private_dir(path.parent)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(f"{_utc_now()} {message}\n")
    except OSError:
        pass


def append_chunk(
    path: Path,
    chunk: bytes,
    *,
    max_bytes: int,
    keep_bytes: int,
) -> None:
    """Append ``chunk`` while keeping the disposable activity tail bounded."""
    if not chunk:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as log:
        fcntl.flock(log.fileno(), fcntl.LOCK_EX)
        try:
            size = os.fstat(log.fileno()).st_size
            if size + len(chunk) <= max_bytes:
                _write_all(log, chunk)
                return

            # The activity file is not history. Retain only its newest bytes;
            # the independent capture spool preserves the complete stream.
            existing_bytes = max(0, keep_bytes - len(chunk))
            prefix = b""
            if existing_bytes:
                log.seek(max(0, size - existing_bytes))
                prefix = log.read(existing_bytes)
            tail = (prefix + chunk)[-keep_bytes:]
            log.seek(0)
            log.truncate(0)
            _write_all(log, tail)
        finally:
            fcntl.flock(log.fileno(), fcntl.LOCK_UN)


def copy_bounded(
    source: BinaryIO,
    path: Path,
    *,
    max_bytes: int,
    keep_bytes: int,
) -> None:
    """Compatibility helper used by tests and activity-only callers."""
    read_chunk = getattr(source, "read1", source.read)
    while True:
        chunk = read_chunk(_COPY_CHUNK)
        if not chunk:
            return
        append_chunk(
            path,
            chunk,
            max_bytes=max_bytes,
            keep_bytes=keep_bytes,
        )


def _sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_COPY_CHUNK)
            if not chunk:
                return digest.digest()
            digest.update(chunk)


def _write_all(stream: BinaryIO, data: bytes | memoryview) -> None:
    """Write every byte or fail instead of accepting a short regular-file write."""
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = stream.write(view[offset:])
        if written is None or written <= 0:
            raise OSError("short write while saving session log")
        offset += written


def _archive_matches_source(
    source: Path,
    archive: Path,
    *,
    zstd: str,
) -> bool:
    """True when an existing archive expands to exactly ``source``."""
    try:
        source_digest = _sha256_file(source)
    except OSError:
        return False
    archive_digest = hashlib.sha256()
    try:
        proc = subprocess.Popen(
            [zstd, "-dcq", "--", str(archive)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    assert proc.stdout is not None
    try:
        with proc.stdout:
            while True:
                chunk = proc.stdout.read(_COPY_CHUNK)
                if not chunk:
                    break
                archive_digest.update(chunk)
        return proc.wait() == 0 and archive_digest.digest() == source_digest
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        return False


def finalize_segment(
    source: Path,
    archive: Path,
    *,
    zstd: str = "zstd",
    diagnostic_path: Path | None = None,
    remove_source: bool = True,
) -> bool:
    """Atomically turn one closed plaintext segment into a zstd level-3 frame.

    On every failure the source is retained. If a prior crash published the
    final archive but did not unlink its source, matching their decompressed
    hashes completes that interrupted transaction safely. ``remove_source``
    is false for legacy imports, which deliberately leave the old copy in
    place even after the permanent archive has been verified.
    """
    try:
        if source.is_symlink():
            raise OSError(f"refusing symlink source: {source}")
        status = source.stat()
    except OSError as exc:
        _diagnose(diagnostic_path, f"cannot stat pending segment {source}: {exc}")
        return False
    try:
        _ensure_private_dir(archive.parent)
    except OSError as exc:
        _diagnose(diagnostic_path, f"cannot prepare archive directory: {exc}")
        return False

    if archive.is_symlink():
        _diagnose(
            diagnostic_path,
            f"refusing symlink archive destination {archive}",
        )
        return False
    if archive.exists():
        if _archive_matches_source(source, archive, zstd=zstd):
            if not remove_source:
                return True
            try:
                source.unlink()
                return True
            except OSError as exc:
                _diagnose(
                    diagnostic_path,
                    f"archive valid but source could not be removed {source}: {exc}",
                )
                return False
        _diagnose(
            diagnostic_path,
            f"refusing to replace non-matching archive {archive}",
        )
        return False

    try:
        fd, raw_tmp = tempfile.mkstemp(
            prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent,
        )
        os.close(fd)
    except OSError as exc:
        _diagnose(
            diagnostic_path,
            f"cannot stage archive for plaintext segment {source}: {exc}",
        )
        return False
    tmp = Path(raw_tmp)
    try:
        compressed = subprocess.run(
            [zstd, "-3", "-q", "-f", "-o", str(tmp), "--", str(source)],
            capture_output=True,
            timeout=120,
        )
        if compressed.returncode != 0:
            detail = compressed.stderr.decode("utf-8", "replace").strip()
            raise OSError(f"zstd -3 failed: {detail or compressed.returncode}")
        # A successful exit without a frame is impossible for the real zstd
        # CLI, even for an empty input. Check the artifact independently so a
        # faulty wrapper or test double cannot turn "return code 0" into data
        # loss.
        if tmp.stat().st_size == 0:
            raise OSError("zstd -3 produced an empty archive")

        checked = subprocess.run(
            [zstd, "-tq", "--", str(tmp)],
            capture_output=True,
            timeout=120,
        )
        if checked.returncode != 0:
            detail = checked.stderr.decode("utf-8", "replace").strip()
            raise OSError(f"zstd validation failed: {detail or checked.returncode}")
        if not _archive_matches_source(source, tmp, zstd=zstd):
            raise OSError("zstd round-trip did not match the plaintext source")

        tmp.chmod(0o600)
        os.utime(tmp, ns=(status.st_atime_ns, status.st_mtime_ns))
        with tmp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(tmp, archive)
        _fsync_dir(archive.parent)
        if remove_source:
            source.unlink()
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        _diagnose(
            diagnostic_path,
            f"left plaintext segment pending {source}: {exc}",
        )
        return False
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class PermanentCaptureWriter:
    """Own one pane stream and publish its ordered permanent segments."""

    def __init__(
        self,
        *,
        source: BinaryIO,
        activity_path: Path,
        live_dir: Path,
        archive_dir: Path,
        metadata_dir: Path,
        lock_dir: Path,
        diagnostic_path: Path,
        capture_id: str,
        archive_prefix: str,
        session_name: str,
        tmux_session_id: str,
        pane_id: str,
        window_index: str,
        pane_index: str,
        initial_cwd: str,
        initial_command: str,
        started_utc: str,
        activity_max_bytes: int,
        activity_keep_bytes: int,
        segment_bytes: int,
        zstd: str = "zstd",
    ):
        self.source = source
        self.activity_path = activity_path
        self.live_dir = live_dir
        self.archive_dir = archive_dir
        self.metadata_path = metadata_dir / f"{capture_id}.json"
        self.lock_path = lock_dir / f"{capture_id}.lock"
        self.diagnostic_path = diagnostic_path
        self.capture_id = capture_id
        self.archive_prefix = archive_prefix
        self.activity_max_bytes = activity_max_bytes
        self.activity_keep_bytes = activity_keep_bytes
        self.segment_bytes = segment_bytes
        self.zstd = zstd
        self.sequence = 0
        self.spool: BinaryIO | None = None
        self.spool_path: Path | None = None
        self.spool_size = 0
        self._lock_fd: int | None = None
        self.metadata: dict[str, Any] = {
            "format": 1,
            "capture_id": capture_id,
            "session_name": session_name,
            "tmux_session_id": tmux_session_id,
            "pane_id": pane_id,
            "window_index": window_index,
            "pane_index": pane_index,
            "started_utc": started_utc,
            "ended_utc": None,
            "initial_cwd": initial_cwd,
            "initial_command": initial_command,
            "archive_prefix": archive_prefix,
            "archive_directory": str(archive_dir),
            "compression": "zstd-3",
            "state": "live",
            "segments": [],
        }

    def _acquire_capture_lock(self) -> None:
        _ensure_private_dir(self.lock_path.parent)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _open_spool(self) -> None:
        _ensure_private_dir(self.live_dir)
        path = self.live_dir / f"{self.capture_id}--{self.sequence:06d}.log"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        self.spool = os.fdopen(fd, "wb", buffering=0)
        self.spool_path = path
        self.spool_size = 0

    def _record_segment(self, source: Path, archived: bool, size: int) -> None:
        archive_name = (
            f"{self.archive_prefix}--{self.sequence:06d}.log.zst"
        )
        self.metadata["segments"].append({
            "sequence": self.sequence,
            "bytes": size,
            "archive": archive_name if archived else None,
            "pending": None if archived else source.name,
        })
        try:
            _atomic_json(self.metadata_path, self.metadata)
        except OSError as exc:
            _diagnose(
                self.diagnostic_path,
                f"could not update capture metadata {self.metadata_path}: {exc}",
            )

    def _finalize_spool(self) -> None:
        if self.spool is None or self.spool_path is None:
            return
        stream = self.spool
        source = self.spool_path
        size = self.spool_size
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        self.spool = None
        self.spool_path = None
        self.spool_size = 0
        archive = self.archive_dir / (
            f"{self.archive_prefix}--{self.sequence:06d}.log.zst"
        )
        archived = finalize_segment(
            source,
            archive,
            zstd=self.zstd,
            diagnostic_path=self.diagnostic_path,
        )
        self._record_segment(source, archived, size)
        self.sequence += 1

    def run(self) -> None:
        for directory in (
            self.live_dir,
            self.archive_dir,
            self.metadata_path.parent,
            self.lock_path.parent,
            self.activity_path.parent,
            self.diagnostic_path.parent,
        ):
            _ensure_private_dir(directory)
        self._acquire_capture_lock()
        _atomic_json(self.metadata_path, self.metadata)
        read_chunk = getattr(self.source, "read1", self.source.read)
        try:
            while True:
                chunk = read_chunk(_COPY_CHUNK)
                if not chunk:
                    break
                try:
                    append_chunk(
                        self.activity_path,
                        chunk,
                        max_bytes=self.activity_max_bytes,
                        keep_bytes=self.activity_keep_bytes,
                    )
                except OSError as exc:
                    _diagnose(
                        self.diagnostic_path,
                        f"activity tail write failed {self.activity_path}: {exc}",
                    )

                view = memoryview(chunk)
                offset = 0
                while offset < len(view):
                    if self.spool is None:
                        self._open_spool()
                    remaining = self.segment_bytes - self.spool_size
                    amount = min(remaining, len(view) - offset)
                    assert self.spool is not None
                    _write_all(self.spool, view[offset:offset + amount])
                    self.spool_size += amount
                    offset += amount
                    if self.spool_size == self.segment_bytes:
                        self._finalize_spool()
            self._finalize_spool()
            self.metadata["state"] = "closed"
            self.metadata["ended_utc"] = _utc_now()
            _atomic_json(self.metadata_path, self.metadata)
        except BaseException as exc:
            _diagnose(
                self.diagnostic_path,
                f"capture writer failed {self.capture_id}: {exc}",
            )
            raise
        finally:
            if self.spool is not None:
                try:
                    self.spool.flush()
                    os.fsync(self.spool.fileno())
                    self.spool.close()
                except OSError:
                    pass
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._lock_fd)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activity-path", type=Path, required=True)
    parser.add_argument("--live-dir", type=Path, required=True)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--metadata-dir", type=Path, required=True)
    parser.add_argument("--lock-dir", type=Path, required=True)
    parser.add_argument("--diagnostic-path", type=Path, required=True)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--archive-prefix", required=True)
    parser.add_argument("--session-name", required=True)
    parser.add_argument("--tmux-session-id", default="")
    parser.add_argument("--pane-id", required=True)
    parser.add_argument("--window-index", default="")
    parser.add_argument("--pane-index", default="")
    parser.add_argument("--initial-cwd", default="")
    parser.add_argument("--initial-command", default="")
    parser.add_argument("--started-utc", required=True)
    parser.add_argument("--activity-max-bytes", type=int, required=True)
    parser.add_argument("--activity-keep-bytes", type=int, required=True)
    parser.add_argument("--segment-bytes", type=int, required=True)
    parser.add_argument("--zstd", default="zstd")
    args = parser.parse_args(argv)
    if args.activity_max_bytes < 1:
        parser.error("--activity-max-bytes must be positive")
    if (
        args.activity_keep_bytes < 1
        or args.activity_keep_bytes > args.activity_max_bytes
    ):
        parser.error(
            "--activity-keep-bytes must be positive and no larger than "
            "--activity-max-bytes",
        )
    if args.segment_bytes < 1:
        parser.error("--segment-bytes must be positive")

    writer = PermanentCaptureWriter(
        source=sys.stdin.buffer,
        activity_path=args.activity_path,
        live_dir=args.live_dir,
        archive_dir=args.archive_dir,
        metadata_dir=args.metadata_dir,
        lock_dir=args.lock_dir,
        diagnostic_path=args.diagnostic_path,
        capture_id=args.capture_id,
        archive_prefix=args.archive_prefix,
        session_name=args.session_name,
        tmux_session_id=args.tmux_session_id,
        pane_id=args.pane_id,
        window_index=args.window_index,
        pane_index=args.pane_index,
        initial_cwd=args.initial_cwd,
        initial_command=args.initial_command,
        started_utc=args.started_utc,
        activity_max_bytes=args.activity_max_bytes,
        activity_keep_bytes=args.activity_keep_bytes,
        segment_bytes=args.segment_bytes,
        zstd=args.zstd,
    )
    writer.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
