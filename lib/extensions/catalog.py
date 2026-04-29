"""Well-known extensions the core dashboard knows how to install.

Deliberately small — this is not a plugin marketplace. Each entry
describes an extension well enough for the Config pane to show it
with a human-readable label and a **Download and enable** button, and
for :func:`lib.extensions.install` to fetch it with ``git clone`` or
via an existing submodule.

The install path works against arbitrary git URLs too (the endpoint
accepts a ``name`` that resolves through this catalog for now; a future
release may accept a raw URL). The catalog is purely advisory.
"""

from __future__ import annotations

from typing import TypedDict


class CatalogEntry(TypedDict):
    name: str
    label: str
    description: str
    repo: str
    pinned_ref: str
    submodule_path: str


KNOWN: dict[str, CatalogEntry] = {
    "agent": {
        "name": "agent",
        "label": "Agents module",
        "description": (
            "LLM agents over tmux sessions. Adds multi-provider "
            "agent CRUD, persistent REPL, conductor rule engine, "
            "cycle / work modes, knowledge base, and an extensible "
            "tool registry. Under active development."
        ),
        "repo": "https://github.com/itsmygithubacct/tmux-browse-agent.git",
        # Tags are preferred over branch names so the install path
        # doesn't move under us between today and next week.
        "pinned_ref": "v0.7.3-agent",
        "submodule_path": "extensions/agent",
    },
    "sandbox": {
        "name": "sandbox",
        "label": "Docker sandbox",
        "description": (
            "Docker-based execution sandbox. Library-only — other "
            "extensions ``import sandbox``; nothing in it is visible "
            "to the dashboard until the agent extension (or another) "
            "uses it. Recommended when any agent is configured with "
            "``sandbox: docker``."
        ),
        "repo": "https://github.com/itsmygithubacct/tmux-browse-sandbox.git",
        "pinned_ref": "v0.7.2-sandbox",
        "submodule_path": "extensions/sandbox",
    },
    "qr": {
        "name": "qr",
        "label": "QR config share",
        "description": (
            "Share view config — layout, hidden list, hot buttons, "
            "idle alerts, mobile keys — between devices by scanning "
            "a QR code. Adds Show QR / Read QR buttons to Config and "
            "a ``/api/qr`` endpoint."
        ),
        "repo": "https://github.com/itsmygithubacct/tmux-browse-qr.git",
        "pinned_ref": "v0.7.2-qr",
        "submodule_path": "extensions/qr",
    },
}
