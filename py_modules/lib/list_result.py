"""Discriminated result for list-returning calls that can fail.

Anything that fetches a collection from a remote (RomM, SteamGridDB, …) and
must distinguish "the server answered and the list is empty" from "the call
failed and we have no information" returns a :class:`ListResult` instead of
a bare ``list``. The discriminator is ``error``: when it is ``None`` the
call succeeded and ``items`` is the (possibly empty) list; when it is set
the call failed and ``items`` is ``None``. The runtime invariant (exactly
one set) is enforced in :meth:`ListResult.__post_init__`; consumers branch
on ``error is None`` before reading ``items``. basedpyright does not
co-narrow two independent ``Optional`` fields in basic mode, so the
recommended consumer pattern adds one explicit assertion::

    if result.error is None:
        assert result.items is not None
        for item in result.items:
            ...

The assertion is a static-typing concession, not a runtime guard against
the invariant — the dataclass already prevents both fields being ``None``.

Lives in ``lib/`` rather than ``models/`` because it is a cross-cutting
control-flow primitive: services, adapters, and domain logic may all
construct or consume it, and it has no place in the persisted-data layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")


class ErrorCode(StrEnum):
    """Coarse failure categories for list-returning calls.

    Kept deliberately small — consumers route on these codes (retry vs.
    surface auth prompt vs. show "unknown error"), so each addition is a
    new branch downstream. Free-form detail goes in
    :attr:`ListResult.error_message`, not here.
    """

    SERVER_UNREACHABLE = "server_unreachable"
    AUTH_FAILED = "auth_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ListResult(Generic[T]):
    """Success-or-failure envelope for a list fetch.

    Exactly one of ``items`` / ``error`` is set; the invariant is enforced
    in :meth:`__post_init__`. Construct via :meth:`ok` or :meth:`failed`
    rather than the default constructor — direct construction is supported
    for dataclass parity but does not buy you anything over the helpers.
    """

    items: list[T] | None = None
    error: ErrorCode | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """Enforce the exactly-one-of-items-or-error discriminator."""
        items_set = self.items is not None
        error_set = self.error is not None
        if items_set == error_set:
            raise ValueError(
                "ListResult requires exactly one of `items` or `error` to be set "
                f"(items_set={items_set}, error_set={error_set})"
            )
        if not error_set and self.error_message is not None:
            raise ValueError("ListResult.error_message must be None when `error` is None")

    @classmethod
    def ok(cls, items: list[T]) -> ListResult[T]:
        """Build a success result wrapping ``items`` (may be an empty list)."""
        return cls(items=items, error=None, error_message=None)

    @classmethod
    def failed(cls, code: ErrorCode, message: str | None = None) -> ListResult[T]:
        """Build a failure result with the given ``code`` and optional ``message``."""
        return cls(items=None, error=code, error_message=message)
