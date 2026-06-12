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
:data:`ListResult` union above) and that can fail, the canonical failure
shape is::

    {"success": False, "reason": ErrorCode | str, "message": str, **extras}

Three required fields:

* ``success: False`` — binary success discriminant.
* ``reason`` — coarse routing slug. For server-reachability failures use
  :data:`ErrorCode.SERVER_UNREACHABLE`; the rest of the small Lean enum
  (auth/not-found/unsupported/unknown plus the frontend-routed
  ``version_error`` / ``stale_conflict`` / ``stale_preview``) covers the
  categories a consumer branches on. For bespoke static guards
  (``sync_disabled``, ``not_installed``, ``active_slot``,
  ``config_error``, ``blocked_by_migration``, …) a plain string literal is
  fine — the ``ErrorCode | str`` union allows it.
* ``message: str`` — human-readable detail for logs and UI. Carries the
  exception text on transport-layer failures. Two transport failures may
  share one ``reason`` slug but carry distinct messages (e.g. a 403
  Cloudflare bot-fight block vs. wrong credentials both map to
  ``AUTH_FAILED`` but explain different remedies).

The legacy ``error_code`` key and a second ``error`` key are **forbidden**.
``scripts/check_failure_shape.py --check`` enforces this: every
``success: False`` return in ``py_modules/services/`` must carry ``reason``
and ``message`` and must not carry ``error`` or ``error_code``.

Optional payload-shape extras (``slot``, ``saves``, ``active_slot``, …) may
appear alongside on a per-callable basis when the frontend needs to render
fallback UI on failure.

Two carve-outs (also recognised by the gate):

* **Discriminated-status unions** (e.g. ``versions.rollback_to_version``,
  ``versions.list_file_versions``) keep the ``status: "ok" | "..."``
  discriminant — a dict with a ``status`` key and no ``success`` key; the
  ``server_unreachable`` branch still carries ``message: str`` instead of
  the legacy ``error: str``.
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
    """Coarse failure categories for the canonical ``reason`` slug.

    Kept deliberately small (Lean) — consumers route on these codes (retry
    vs. surface auth prompt vs. show "unknown error"), so each addition is a
    new branch downstream. Free-form detail goes in the failure dict's
    ``message`` (or :attr:`FailedListResult.error_message`), not here.
    Bespoke non-server-reachability failures stay plain-string ``reason``
    values (``config_error``, ``sync_disabled``, ``not_installed``,
    ``active_slot``, …) — the ``reason: ErrorCode | str`` union allows it.

    The transport categories collapse several exception types onto one slug:
    ``SERVER_UNREACHABLE`` folds connection/timeout/SSL/5xx/generic-API
    failures; ``AUTH_FAILED`` folds 401 and 403 (same slug, distinct
    ``message`` so a Cloudflare bot-fight 403 stays distinguishable from
    wrong credentials). ``VERSION_ERROR`` / ``STALE_CONFLICT`` /
    ``STALE_PREVIEW`` stay distinct because the frontend routes on them.
    """

    SERVER_UNREACHABLE = "server_unreachable"
    AUTH_FAILED = "auth_failed"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    VERSION_ERROR = "version_error"
    STALE_CONFLICT = "stale_conflict"
    STALE_PREVIEW = "stale_preview"


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
