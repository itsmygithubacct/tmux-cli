"""CLI verb for the ``ext_hello`` fixture."""


def _dispatch(argv):
    return 0


def register_verb():
    return {"hello": _dispatch}
