"""Permanent session-log verbs: ``tb logs``."""

from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO

from .. import config, output, session_logs
from ..errors import StateError, UsageError


def _capture_id(value: str) -> str:
    try:
        return session_logs.resolve_capture(value)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc


def _human_bytes(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or suffix == "TiB":
            if suffix == "B":
                return f"{int(size)}B"
            return f"{size:.1f}{suffix}"
        size /= 1024
    return f"{value}B"


def _read_segment(path: Path, compressed: bool) -> bytes:
    if not compressed:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StateError(f"cannot read pending log {path}: {exc}") from exc
    try:
        result = subprocess.run(
            ["zstd", "-dcq", "--", str(path)],
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise StateError("zstd is required to read archived logs") from exc
    except subprocess.SubprocessError as exc:
        raise StateError(f"could not decompress {path}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise StateError(
            f"could not decompress {path}: {detail or result.returncode}",
        )
    return result.stdout


def _capture_bytes(capture_id: str) -> bytes:
    entries = session_logs.capture_paths(capture_id)
    if not entries:
        raise StateError(f"capture has no readable segments: {capture_id}")
    return b"".join(
        _read_segment(path, compressed)
        for _, path, compressed in entries
    )


def cmd_logs_list(args: argparse.Namespace) -> int:
    rows = session_logs.list_captures(
        session=args.session,
        pane=args.pane,
    )
    if args.json:
        output.emit_json({
            "path": str(session_logs.ROOT_DIR),
            "captures": rows,
        })
    elif not args.quiet:
        display = [
            {
                **row,
                "capture_id": row["capture_id"][:12],
                "bytes": _human_bytes(int(row["bytes"])),
                "started": str(row["started"]).replace("T", " ")[:19],
            }
            for row in rows
        ]
        output.emit_table(
            display,
            [
                ("started", "STARTED (UTC)"),
                ("session", "SESSION"),
                ("pane", "PANE"),
                ("state", "STATE"),
                ("segments", "PARTS"),
                ("bytes", "SIZE"),
                ("capture_id", "CAPTURE"),
                ("cwd", "CWD"),
            ],
            no_header=args.no_header,
            empty_message="(no permanent logs)",
        )
    return 0


def cmd_logs_path(args: argparse.Namespace) -> int:
    if args.capture is None:
        paths = [str(session_logs.ROOT_DIR)]
        capture_id = None
    else:
        capture_id = _capture_id(args.capture)
        paths = [
            str(path)
            for _, path, _ in session_logs.capture_paths(capture_id)
        ]
    if args.json:
        output.emit_json({"capture_id": capture_id, "paths": paths})
    elif not args.quiet:
        for path in paths:
            print(path)
    return 0


def cmd_logs_show(args: argparse.Namespace) -> int:
    capture_id = _capture_id(args.capture)
    content = _capture_bytes(capture_id)
    if args.json:
        output.emit_json({
            "capture_id": capture_id,
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        })
    elif not args.quiet:
        stream: BinaryIO | Any = getattr(sys.stdout, "buffer", sys.stdout)
        if isinstance(content, bytes) and hasattr(stream, "write"):
            try:
                stream.write(content)
            except TypeError:
                stream.write(content.decode("utf-8", "backslashreplace"))
    return 0


def cmd_logs_grep(args: argparse.Namespace) -> int:
    try:
        expression = (
            re.escape(args.pattern.encode("utf-8"))
            if args.fixed
            else args.pattern.encode("utf-8")
        )
        matcher = re.compile(
            expression,
            re.IGNORECASE if args.ignore_case else 0,
        )
    except re.error as exc:
        raise UsageError(f"invalid regular expression: {exc}") from exc

    if args.capture:
        capture_ids = [_capture_id(args.capture)]
    else:
        capture_ids = [
            row["capture_id"]
            for row in session_logs.list_captures(
                session=args.session,
                pane=args.pane,
            )
        ]
    matches: list[dict[str, Any]] = []
    for capture_id in capture_ids:
        # Search the reconstructed pane stream so a word or line split exactly
        # at a segment boundary still matches.
        content = _capture_bytes(capture_id)
        for line_number, line in enumerate(content.splitlines(), 1):
            if not matcher.search(line):
                continue
            text = line.decode("utf-8", "backslashreplace")
            if len(text) > 1000:
                text = text[:999] + "…"
            matches.append({
                "capture_id": capture_id,
                "line": line_number,
                "path": f"capture:{capture_id}",
                "text": text,
            })
    if args.json:
        output.emit_json({"pattern": args.pattern, "matches": matches})
    elif not args.quiet:
        for match in matches:
            print(
                f"{match['path']}:{match['line']}:{match['text']}",
            )
    return 0 if matches else 1


def cmd_logs_verify(args: argparse.Namespace) -> int:
    if args.capture:
        capture_ids = [_capture_id(args.capture)]
    else:
        capture_ids = [
            row["capture_id"] for row in session_logs.list_captures()
        ]
    results = [
        session_logs.verify_capture(capture_id)
        for capture_id in capture_ids
    ]
    failed = [value["capture_id"] for value in results if not value["ok"]]
    if args.json and failed:
        # Let the top-level JSON error handler emit the single authoritative
        # envelope. Emitting a success payload first would produce two JSON
        # documents for one failed command.
        raise StateError(
            "log verification failed: " + ", ".join(failed),
        )
    if args.json:
        output.emit_json({"captures": results})
    elif not args.quiet:
        rows = [
            {
                "capture_id": value["capture_id"][:12],
                "status": "ok" if value["ok"] else "FAILED",
                "segments": value["segments"],
                "missing": ",".join(map(str, value["missing_sequences"])) or "-",
                "corrupt": len(value["corrupt"]),
                "pending": len(value["pending_plaintext"]),
            }
            for value in results
        ]
        output.emit_table(
            rows,
            [
                ("capture_id", "CAPTURE"),
                ("status", "STATUS"),
                ("segments", "PARTS"),
                ("missing", "MISSING"),
                ("corrupt", "CORRUPT"),
                ("pending", "PENDING"),
            ],
            no_header=args.no_header,
            empty_message="(no permanent logs)",
        )
    if failed:
        raise StateError(
            "log verification failed: " + ", ".join(failed),
        )
    return 0


def cmd_logs_recover(args: argparse.Namespace) -> int:
    result = session_logs.recover(
        migrate_legacy=not args.no_legacy,
        legacy_limit=None,
    )
    if args.json and result["errors"]:
        raise StateError("one or more logs could not be recovered")
    if args.json:
        output.emit_json(result)
    elif not args.quiet:
        print(
            "archived {archived}, pending {pending}, imported legacy "
            "{legacy_archived}, legacy pending {legacy_pending}, errors "
            "{errors}".format(**result),
        )
    if result["errors"]:
        raise StateError("one or more logs could not be recovered")
    return 0


def cmd_logs_manual(args: argparse.Namespace) -> int:
    path = config.SESSION_LOGGING_MANUAL
    if args.json:
        output.emit_json({"path": str(path), "installed": path.is_file()})
    elif not args.quiet:
        print(path)
    return 0


def register(sub, common) -> None:
    parser = sub.add_parser(
        "logs",
        help="inspect permanent zstd pane logs",
        parents=[common],
    )
    nested = parser.add_subparsers(dest="_logsverb")

    list_parser = nested.add_parser(
        "list", aliases=["ls"], help="list pane captures", parents=[common],
    )
    list_parser.add_argument("--session", help="only this session name")
    list_parser.add_argument("--pane", help="only this tmux pane id")
    list_parser.set_defaults(func=cmd_logs_list, needs_server=False)

    path_parser = nested.add_parser(
        "path", help="print the log root or capture paths", parents=[common],
    )
    path_parser.add_argument("capture", nargs="?")
    path_parser.set_defaults(func=cmd_logs_path, needs_server=False)

    show_parser = nested.add_parser(
        "show", aliases=["cat"], help="write one capture to stdout",
        parents=[common],
    )
    show_parser.add_argument("capture")
    show_parser.set_defaults(func=cmd_logs_show, needs_server=False)

    grep_parser = nested.add_parser(
        "grep", help="search decompressed pane logs", parents=[common],
    )
    grep_parser.add_argument("pattern")
    grep_parser.add_argument("--capture", help="only this capture id")
    grep_parser.add_argument("--session", help="only this session name")
    grep_parser.add_argument("--pane", help="only this tmux pane id")
    grep_parser.add_argument(
        "-F", "--fixed", action="store_true", help="literal string search",
    )
    grep_parser.add_argument(
        "-i", "--ignore-case", action="store_true",
        help="case-insensitive search",
    )
    grep_parser.set_defaults(func=cmd_logs_grep, needs_server=False)

    verify_parser = nested.add_parser(
        "verify", help="check zstd frames and segment order", parents=[common],
    )
    verify_parser.add_argument("capture", nargs="?")
    verify_parser.set_defaults(func=cmd_logs_verify, needs_server=False)

    recover_parser = nested.add_parser(
        "recover", help="archive abandoned spools and copy legacy plaintext",
        parents=[common],
    )
    recover_parser.add_argument(
        "--no-legacy", action="store_true",
        help="skip ~/.tmux-browse/session-logs import",
    )
    recover_parser.set_defaults(func=cmd_logs_recover, needs_server=False)

    manual_parser = nested.add_parser(
        "manual", help="print the installed logging-manual path",
        parents=[common],
    )
    manual_parser.set_defaults(func=cmd_logs_manual, needs_server=False)

    # Bare ``tb logs`` behaves like ``tb logs list``.
    parser.set_defaults(
        func=cmd_logs_list,
        needs_server=False,
        session=None,
        pane=None,
    )
