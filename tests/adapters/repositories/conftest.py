"""Fixtures for the SQLite repository-adapter tests.

``db`` builds a fresh on-disk database with the real v1 schema applied via the
migration runner; ``uow`` opens a :class:`SqliteUnitOfWork` on it inside a
``with`` block so each test works against the same connection/transaction shape
production uses. Tests that want to inspect what survives a commit re-open their
own unit on the ``db`` path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from adapters.repositories.unit_of_work import SqliteUnitOfWork
from adapters.sqlite_migrations import MIGRATIONS_DIR, apply_migrations

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def db(tmp_path: Path) -> str:
    """Return the path to a fresh database with the v1 schema applied."""
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    return db_path


@pytest.fixture
def uow(db: str) -> Iterator[SqliteUnitOfWork]:
    """Yield an open :class:`SqliteUnitOfWork` on the ``db`` path."""
    with SqliteUnitOfWork(db) as unit:
        yield unit
