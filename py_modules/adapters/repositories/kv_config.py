"""SQLite adapter for the ``kv_config`` key-value table.

No domain aggregate — a flat string-keyed, string-valued surface. Callers own
JSON encoding/decoding; values are always stored as TEXT.
"""

from __future__ import annotations

from adapters.repositories._base import BaseRepository


class SqliteKvConfigRepository(BaseRepository):
    """The miscellaneous singleton-scalar key-value surface."""

    def get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv_config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else None

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO kv_config (key, value) VALUES (?, ?)",
            (key, value),
        )

    def delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM kv_config WHERE key = ?", (key,))
