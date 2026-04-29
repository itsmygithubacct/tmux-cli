"""Minimal route registration for the ``ext_hello`` fixture."""


def _hello_get(handler, parsed):
    handler._send_json({"ok": True, "from": "ext_hello"})


def register():
    return {
        "get_routes": {"/api/ext-hello": _hello_get},
    }
