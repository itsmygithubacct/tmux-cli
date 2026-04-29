"""CLI driver for extension management.

Exposes the same install / update / disable / uninstall operations
the HTTP endpoints call, so the Makefile targets and ad-hoc shell
users go through one code path. No duplicate logic.

Not gated by the dashboard's config lock — shell access to this
repo already implies write access to the files it would edit, so
adding an HTTP-level secret here would be cargo-culting.

Usage::

    python3 -m lib.extensions install <name>
    python3 -m lib.extensions update <name>
    python3 -m lib.extensions enable <name>
    python3 -m lib.extensions disable <name>
    python3 -m lib.extensions uninstall <name> [--remove-state]
    python3 -m lib.extensions list

Exits 0 on success, non-zero on failure. Stderr carries the ``stage``
tag so CI logs are grep-friendly.
"""

from __future__ import annotations

import argparse
import sys

from . import (
    CATALOG,
    InstallError,
    UpdateError,
    disable,
    enable,
    install,
    status,
    uninstall,
    update,
)


def _cmd_list(_args: argparse.Namespace) -> int:
    for row in status():
        # Name | enabled | installed | version
        flag = "E" if row["enabled"] else "-"
        inst = "I" if row["installed"] else "-"
        print(f"  {flag}{inst}  {row['name']:<20}  {row.get('version') or '-'}")
    if not CATALOG:
        print("(no known extensions in catalog)")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    try:
        result = install(args.name)
    except InstallError as e:
        sys.stderr.write(f"install failed [{e.stage}]: {e.msg}\n")
        return 1
    enable(args.name)
    print(f"installed {result.name} {result.version} via {result.via}")
    print("restart the dashboard to activate")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    try:
        result = update(args.name)
    except UpdateError as e:
        sys.stderr.write(f"update failed [{e.stage}]: {e.msg}\n")
        return 1
    if result.changed:
        print(f"updated {result.name} {result.from_version} → "
              f"{result.to_version} via {result.via}")
        print("restart the dashboard to activate")
    else:
        print(f"{result.name} already at {result.to_version}")
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    enable(args.name)
    print(f"enabled {args.name} — restart the dashboard to activate")
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    disable(args.name)
    print(f"disabled {args.name} — restart the dashboard to deactivate")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    summary = uninstall(args.name, remove_state=args.remove_state)
    print(f"uninstalled {args.name} via {summary['via']}")
    if args.remove_state:
        removed = summary.get("state_removed") or []
        if removed:
            print(f"removed {len(removed)} state entries:")
            for rel in removed:
                print(f"  - {rel}")
        else:
            print("no state paths removed (manifest had none or they were absent)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m lib.extensions",
        description="Manage tmux-browse extensions from the shell.",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="<verb>")

    sub_list = sub.add_parser("list", help="list known + installed extensions")
    sub_list.set_defaults(func=_cmd_list)

    for verb, fn, help_ in [
        ("install", _cmd_install, "clone (or submodule-init) an extension and enable it"),
        ("update", _cmd_update, "advance an installed extension to its pinned ref"),
        ("enable", _cmd_enable, "flip the enabled flag on (restart required)"),
        ("disable", _cmd_disable, "flip the enabled flag off (keeps code)"),
    ]:
        s = sub.add_parser(verb, help=help_)
        s.add_argument("name", help="extension name from the catalog")
        s.set_defaults(func=fn)

    sub_un = sub.add_parser(
        "uninstall", help="remove an extension's code (and optionally its state)")
    sub_un.add_argument("name", help="extension name")
    sub_un.add_argument(
        "--remove-state", action="store_true",
        help=("also delete the state paths the extension declared in its "
              "manifest (agents.json, logs, etc). Irreversible."),
    )
    sub_un.set_defaults(func=_cmd_uninstall)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
