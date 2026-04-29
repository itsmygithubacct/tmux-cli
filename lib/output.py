"""Output helpers — auto-detect TTY, emit tables or JSON with a stable schema.

JSON envelope on success:   ``{"ok": true, "data": <payload>}``
JSON envelope on failure:   ``{"ok": false, "error": str, "code": str, "exit": int}``
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .errors import TBError


def is_tty(stream=None) -> bool:
    s = stream or sys.stdout
    try:
        return s.isatty()
    except Exception:
        return False


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TB_COLOR") == "always":
        return True
    if os.environ.get("TB_COLOR") == "never":
        return False
    return is_tty()


class _C:
    RESET = "\x1b[0m"
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    BLUE = "\x1b[34m"
    CYAN = "\x1b[36m"


def color(s: str, c: str) -> str:
    if not use_color():
        return s
    return f"{c}{s}{_C.RESET}"


def emit_json(data: Any, *, stream=None) -> None:
    s = stream or sys.stdout
    s.write(json.dumps({"ok": True, "data": data}) + "\n")


def emit_error_json(err: TBError, *, stream=None) -> None:
    s = stream or sys.stderr
    s.write(json.dumps({
        "ok": False,
        "error": err.message,
        "code": err.code,
        "exit": err.exit_code,
    }) + "\n")


def emit_table(rows: list[dict], columns: list[tuple[str, str]],
               *, no_header: bool = False, stream=None,
               empty_message: str = "(none)") -> None:
    """Render ``rows`` as an aligned text table.

    ``columns`` is a list of (key, header) pairs controlling column order
    and titles. Missing values render as ``-``. When ``rows`` is empty we
    emit ``empty_message`` so callers get feedback instead of silence.
    """
    s = stream or sys.stdout
    if not rows:
        if empty_message is not None:
            s.write(color(empty_message, _C.DIM) + "\n")
        return
    keys = [k for k, _ in columns]
    headers = [h for _, h in columns]
    widths = [len(h) for h in headers]
    str_rows: list[list[str]] = []
    for r in rows:
        cells = []
        for i, k in enumerate(keys):
            v = r.get(k, "-")
            if v is None:
                v = "-"
            cells.append(str(v))
            widths[i] = max(widths[i], len(cells[-1]))
        str_rows.append(cells)

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    if not no_header:
        s.write(color(line(headers), _C.BOLD) + "\n")
    for cells in str_rows:
        s.write(line(cells) + "\n")


def emit_plain(text: str, *, stream=None) -> None:
    s = stream or sys.stdout
    s.write(text)
    if text and not text.endswith("\n"):
        s.write("\n")
