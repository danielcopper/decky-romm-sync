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

Canonical failure response shape for dict-returning callables
=============================================================

For Decky callables that return a plain ``dict`` (rather than the typed
:data:`ListResult` union above) and that can fail because the RomM server
is unreachable, the canonical failure shape is::

    {"success": False, "reason": ErrorCode | str, "message": str, **extras}

Three required fields:

* ``success: False`` — binary success discriminant.
* ``reason`` — coarse routing slug. For server-reachability failures use
  :data:`ErrorCode.SERVER_UNREACHABLE`; for static guards (sync_disabled,
  not_installed, not_found, active_slot, …) a plain string literal is fine.
  Never duplicate ``reason`` into a second ``error`` field.
* ``message: str`` — human-readable detail for logs and UI. Carries the
  exception text on transport-layer failures.

Optional payload-shape extras (``slot``, ``saves``, ``active_slot``, …) may
appear alongside on a per-callable basis when the frontend needs to render
fallback UI on failure.

Two carve-outs:

* **Discriminated-status unions** (e.g. ``versions.rollback_to_version``,
  ``versions.list_file_versions``) keep the ``status: "ok" | "..."``
  discriminant; the ``server_unreachable`` branch still carries
  ``message: str`` instead of the legacy ``error: str``.
* **Partial-success responses** that return a full data payload alongside
  a failure flag (e.g. ``status.get_save_status``'s
  ``server_query_failed: bool``, ``setup.get_save_setup_info``'s
  ``recommended_action: "server_unreachable" | ...``) keep the additive
  flag — the call has half-broken half-working semantics that the binary
  ``success`` boolean would erase.
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
