"""Two-phase binding helper for forward references during service wiring.

Bootstrap constructs services in an order where some consumers must hold a
read seam for a producer that is built later in the same function. A plain
forward-referencing lambda (``lambda: producer.attr``) closes over a
yet-undefined local — works at call time but produces ``NameError`` with no
explicit trail if the order is ever broken. ``LateBinding`` swaps that
implicit closure for an explicit two-phase contract: callers receive the
binding at construction time, the producer plugs a *reader function* in
afterwards via :meth:`set`, and any read before that raises a clear
``RuntimeError``. Each :meth:`get` re-invokes the reader, so consumers
observe live producer state — not a snapshot taken at bind time — which
matters when the producer rebinds its attribute (not just mutates the
underlying object).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")


class LateBinding(Generic[T]):
    """Typed forward-reference for service wiring."""

    __slots__ = ("_name", "_reader")

    def __init__(self, name: str) -> None:
        """Create an unset binding tagged ``name`` for error reporting."""
        self._name = name
        self._reader: Callable[[], T] | None = None

    def set(self, reader: Callable[[], T]) -> None:
        """Bind a *reader* callable that returns the current value on demand.

        Calling more than once overwrites the reader; intentional.
        """
        self._reader = reader

    def get(self) -> T:
        """Invoke the bound reader and return its value.

        Raises ``RuntimeError`` if :meth:`set` has not been called yet.
        """
        reader = self._reader
        if reader is None:
            raise RuntimeError(f"LateBinding[{self._name}] accessed before set() was called — startup ordering bug")
        return reader()
