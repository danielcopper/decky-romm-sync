"""Aggregate-root decorator. Marker for the AST field-assignment check and
the single canonical way to declare a Cosmic Python aggregate root in this
codebase. Value Objects (immutable members of an aggregate) use plain
``@dataclass(frozen=True, slots=True)`` and do not belong here.
"""

from __future__ import annotations

from dataclasses import Field, dataclass, field
from typing import TypeVar, dataclass_transform

T = TypeVar("T", bound=type)


@dataclass_transform(field_specifiers=(Field, field))
def cosmic_aggregate(cls: T) -> T:
    """Declare ``cls`` as a Cosmic Python aggregate root.

    Applies ``@dataclass(slots=True)`` so the class gets ``__init__``,
    ``__repr__``, ``__eq__``, and ``__slots__`` for free. The decorated
    class is also recognised by the AST field-assignment check that
    rejects ``aggregate.field = value`` outside aggregate methods —
    mutation must go through verb-named methods on the root.

    Use this decorator alone — do not stack ``@dataclass`` on top.
    """
    return dataclass(slots=True)(cls)
