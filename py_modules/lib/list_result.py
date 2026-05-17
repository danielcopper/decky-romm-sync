"""Typed-subtype union for list-returning calls that can fail.

Anything that fetches a collection from a remote (RomM, SteamGridDB, …) and
must distinguish "the server answered and the list is empty" from "the call
failed and we have no information" returns a :data:`ListResult` instead of
a bare ``list``. ``ListResult[T]`` is the union ``OkListResult[T] |
FailedListResult``; consumers narrow via ``isinstance`` or ``match``::

    if isinstance(result, FailedListResult):
        log.warning("fetch failed: %s", result.error)
        return
    for item in result.items:
        ...

Both branches narrow cleanly under basedpyright basic mode — the success
branch (``else``) infers ``OkListResult[T]`` and exposes ``items`` without
an extra assertion. Do not introduce a ``TypeGuard``-based predicate
(e.g. ``is_ok``): ``TypeGuard`` only narrows the positive branch, leaving
the ``else`` untyped under basic mode.

Lives in ``lib/`` rather than ``models/`` because it is a cross-cutting
control-flow primitive: services, adapters, and domain logic may all
construct or consume it, and it has no place in the persisted-data layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeAlias, TypeVar

T = TypeVar("T")


class ErrorCode(StrEnum):
    """Coarse failure categories for list-returning calls.

    Kept deliberately small — consumers route on these codes (retry vs.
    surface auth prompt vs. show "unknown error"), so each addition is a
    new branch downstream. Free-form detail goes in
    :attr:`FailedListResult.error_message`, not here.
    """

    SERVER_UNREACHABLE = "server_unreachable"
    AUTH_FAILED = "auth_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OkListResult(Generic[T]):
    """Success branch of :data:`ListResult` — wraps the fetched list.

    ``items`` may be empty (the server answered, nothing matched) — that is
    still a successful call, distinct from the failure branch.
    """

    items: list[T]


@dataclass(frozen=True)
class FailedListResult:
    """Failure branch of :data:`ListResult` — carries the routing code.

    ``error_message`` is free-form detail for logs and UI; routing logic
    must branch on :attr:`error` (the :class:`ErrorCode`) instead.
    """

    error: ErrorCode
    error_message: str | None = None


ListResult: TypeAlias = "OkListResult[T] | FailedListResult"
