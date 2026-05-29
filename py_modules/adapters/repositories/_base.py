"""Shared base for the SQLite repository adapters.

Every concrete ``SqliteXxxRepository`` holds the unit-of-work's open
``sqlite3.Connection`` and reconstructs/serialises domain aggregates against it.
The row→bool, object→JSON, and JSON→object conversions the STRICT schema forces
(no native BOOL, JSON arrays/objects stored as TEXT) live here so every adapter
maps the same way.

This package imports only ``sqlite3``, ``json``, ``domain.*``, and stdlib —
never ``services`` — so the ``adapters ↛ services`` import-linter contract stays
green. The repositories structurally satisfy the repository Protocols without
importing them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3


class BaseRepository:
    """Holds the unit-of-work connection plus the row/JSON/bool helpers."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @staticmethod
    def _to_bool(value: int) -> bool:
        """Map a STRICT 0/1 INTEGER column to a Python bool."""
        return bool(value)

    @staticmethod
    def _json(obj: Any) -> str:
        """Serialise *obj* to a JSON TEXT column value."""
        return json.dumps(obj)

    @staticmethod
    def _json_or_none(value: str | None) -> Any | None:
        """Decode a nullable JSON TEXT column — ``None`` stays ``None``.

        Preserves the NULL-vs-``'[]'`` distinction the schema relies on: a SQL
        NULL round-trips to Python ``None``, never to ``[]``.
        """
        if value is None:
            return None
        return json.loads(value)
