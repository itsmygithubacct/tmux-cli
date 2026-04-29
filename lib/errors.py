"""Typed errors that map 1-to-1 to CLI exit codes."""

from __future__ import annotations


class TBError(Exception):
    code: str = "EUNKNOWN"
    exit_code: int = 1

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message or self.code


class UsageError(TBError):
    code = "EUSAGE"
    exit_code = 2


class SessionNotFound(TBError):
    code = "ENOENT"
    exit_code = 3


class SessionExists(TBError):
    code = "EEXIST"
    exit_code = 4


class Timeout(TBError):
    code = "ETIMEDOUT"
    exit_code = 5


class NoTmuxServer(TBError):
    code = "ENOSERVER"
    exit_code = 6


class TmuxFailed(TBError):
    code = "ETMUX"
    exit_code = 7


class StateError(TBError):
    """Unwritable state dir, corrupt registry, or other on-disk trouble."""
    code = "ESTATE"
    exit_code = 8


class AuthError(TBError):
    """Unauthenticated request when the dashboard has auth enabled."""
    code = "EAUTH"
    exit_code = 9
