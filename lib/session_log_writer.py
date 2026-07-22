"""Bounded, lock-safe writer used by tmux ``pipe-pane`` processes."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path
from typing import BinaryIO


def append_chunk(
    path: Path,
    chunk: bytes,
    *,
    max_bytes: int,
    keep_bytes: int,
) -> None:
    """Append ``chunk`` while keeping ``path`` below ``max_bytes``.

    Multiple panes can feed the same session log.  Locking the log inode
    around the size check, tail read, and write prevents their rotations from
    racing with one another.
    """
    if not chunk:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as log:
        fcntl.flock(log.fileno(), fcntl.LOCK_EX)
        try:
            size = os.fstat(log.fileno()).st_size
            if size + len(chunk) <= max_bytes:
                log.write(chunk)
                return

            # Retain the newest bytes from the existing file plus this chunk.
            # Truncating the existing inode keeps all long-lived pipe writers
            # attached to the same file after rotation.
            existing_bytes = max(0, keep_bytes - len(chunk))
            prefix = b""
            if existing_bytes:
                log.seek(max(0, size - existing_bytes))
                prefix = log.read(existing_bytes)
            tail = (prefix + chunk)[-keep_bytes:]
            log.seek(0)
            log.truncate(0)
            log.write(tail)
        finally:
            fcntl.flock(log.fileno(), fcntl.LOCK_UN)


def copy_bounded(
    source: BinaryIO,
    path: Path,
    *,
    max_bytes: int,
    keep_bytes: int,
) -> None:
    # ``sys.stdin.buffer`` is a BufferedReader over tmux's long-lived pipe.
    # BufferedReader.read(n) waits for all n bytes (or EOF), which can delay a
    # normal interactive update indefinitely.  read1() performs at most one
    # raw read and returns whatever is currently available.  BytesIO and
    # other simple BinaryIO implementations do not expose read1(), so retain
    # read() as the test/file fallback.
    read_chunk = getattr(source, "read1", source.read)
    while True:
        chunk = read_chunk(64 * 1024)
        if not chunk:
            return
        append_chunk(
            path,
            chunk,
            max_bytes=max_bytes,
            keep_bytes=keep_bytes,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--keep-bytes", type=int, required=True)
    args = parser.parse_args(argv)
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    if args.keep_bytes < 1 or args.keep_bytes > args.max_bytes:
        parser.error("--keep-bytes must be positive and no larger than --max-bytes")
    copy_bounded(
        sys.stdin.buffer,
        args.path,
        max_bytes=args.max_bytes,
        keep_bytes=args.keep_bytes,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
