"""Dashboard config verbs: ``tb config [show|get|set|reset]``."""

from __future__ import annotations

import argparse

import hashlib
import hmac
import sys

from .. import config, dashboard_config, output
from ..errors import UsageError


def _check_config_lock() -> None:
    """Abort if the config is locked and no valid password is provided."""
    if not config.CONFIG_LOCK_FILE.exists():
        return
    stored = config.CONFIG_LOCK_FILE.read_text(encoding="utf-8").strip()
    if not stored:
        return
    pw = input("Config is locked. Enter password: ").strip()
    attempt = hashlib.sha256(pw.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(stored, attempt):
        raise UsageError("wrong config password")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _valid_keys() -> set[str]:
    return set(dashboard_config.DEFAULTS)


def _sorted_rows(cfg: dict[str, object]) -> list[dict[str, object]]:
    return [{"key": key, "value": cfg[key]} for key in sorted(cfg)]


def _require_key(key: str) -> str:
    name = (key or "").strip()
    if not name:
        raise UsageError("config key must be non-empty")
    if name not in _valid_keys():
        raise UsageError(f"unknown config key: {name}")
    return name


def cmd_config_show(args: argparse.Namespace) -> int:
    cfg = dashboard_config.load()
    if args.json:
        output.emit_json({"path": str(dashboard_config.config.DASHBOARD_CONFIG_FILE), "config": cfg})
    elif not args.quiet:
        output.emit_table(
            _sorted_rows(cfg),
            [("key", "KEY"), ("value", "VALUE")],
            no_header=args.no_header,
        )
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    key = _require_key(args.key)
    cfg = dashboard_config.load()
    value = cfg[key]
    if args.json:
        output.emit_json({"key": key, "value": value})
    elif not args.quiet:
        output.emit_plain(str(value))
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    _check_config_lock()
    key = _require_key(args.key)
    current = dashboard_config.load()
    current[key] = args.value
    saved = dashboard_config.save(current)
    value = saved[key]
    if args.json:
        output.emit_json({
            "path": str(dashboard_config.config.DASHBOARD_CONFIG_FILE),
            "key": key,
            "value": value,
            "config": saved,
        })
    elif not args.quiet:
        print(f"{key}={value}")
    return 0


def cmd_config_reset(args: argparse.Namespace) -> int:
    _check_config_lock()
    saved = dashboard_config.save({})
    if args.json:
        output.emit_json({"path": str(dashboard_config.config.DASHBOARD_CONFIG_FILE), "config": saved})
    elif not args.quiet:
        print(f"reset {dashboard_config.config.DASHBOARD_CONFIG_FILE}")
    return 0


def register(sub, common) -> None:
    p = sub.add_parser(
        "config",
        help="show and edit dashboard-config.json",
        parents=[common],
    )
    csub = p.add_subparsers(dest="_configverb")

    p_show = csub.add_parser("show", help="print the current dashboard config", parents=[common])
    p_show.set_defaults(func=cmd_config_show)

    p_get = csub.add_parser("get", help="print one dashboard config value", parents=[common])
    p_get.add_argument("key")
    p_get.set_defaults(func=cmd_config_get)

    p_set = csub.add_parser("set", help="set one dashboard config value", parents=[common])
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_set.set_defaults(func=cmd_config_set)

    p_reset = csub.add_parser("reset", help="write built-in dashboard config defaults", parents=[common])
    p_reset.set_defaults(func=cmd_config_reset)

    p.set_defaults(func=cmd_config_show)
