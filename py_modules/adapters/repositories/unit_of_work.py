"""The synchronous SQLite Unit of Work — the transactional boundary for one operation.

Opens one ``sqlite3`` connection per operation, applies the runtime
per-connection PRAGMAs, issues an explicit ``BEGIN``, exposes the nine
repositories over that shared connection, and commits / rolls back on exit. Used
as a synchronous context manager from a service's ``run_in_executor`` worker so
the connection never escapes its worker thread (ADR-0004 — sync ``sqlite3`` over
``aiosqlite``, thread-affinity by connection-per-operation).

``SqliteUnitOfWork`` returns concrete ``SqliteXxxRepository`` instances and never
imports the ``services``-layer Protocols; it structurally satisfies the
``UnitOfWork`` Protocol at the composition-root assignment site (where
basedpyright checks it against ``UnitOfWorkFactory``).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from adapters.repositories.bios_file import SqliteBiosFileRepository
from adapters.repositories.firmware_cache import SqliteFirmwareCacheRepository
from adapters.repositories.kv_config import SqliteKvConfigRepository
from adapters.repositories.playtime import SqlitePlaytimeRepository
from adapters.repositories.rom import SqliteRomRepository
from adapters.repositories.rom_install import SqliteRomInstallRepository
from adapters.repositories.rom_metadata import SqliteRomMetadataRepository
from adapters.repositories.rom_save_state import SqliteRomSaveStateRepository
from adapters.repositories.sync_run import SqliteSyncRunRepository

if TYPE_CHECKING:
    from types import TracebackType


class SqliteUnitOfWork:
    """Atomic transaction boundary over one SQLite connection and the nine repositories.

    A clean ``__exit__`` commits; an exceptional one rolls back and re-raises.
    The connection is opened in ``__enter__`` and closed in ``__exit__`` so it
    lives entirely on the executor thread that drives the ``with`` block.

    The nine repositories are bound in ``__enter__`` (they need the open
    connection). Declaring them here as instance attributes makes the structural
    match against the ``UnitOfWork`` Protocol explicit: the concrete repository
    types each satisfy their repository Protocol, so this class satisfies
    ``UnitOfWork`` without importing it.
    """

    roms: SqliteRomRepository
    rom_installs: SqliteRomInstallRepository
    rom_metadata: SqliteRomMetadataRepository
    playtime: SqlitePlaytimeRepository
    rom_save_states: SqliteRomSaveStateRepository
    bios_files: SqliteBiosFileRepository
    firmware_cache: SqliteFirmwareCacheRepository
    sync_runs: SqliteSyncRunRepository
    kv_config: SqliteKvConfigRepository

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> SqliteUnitOfWork:
        # isolation_level=None -> the UoW drives BEGIN/COMMIT/ROLLBACK itself.
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # journal_mode=WAL is persistent (set by the #781 runner); the rest are
        # per-connection and must be (re-)applied here for every runtime connection.
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("BEGIN")
        self._conn = conn

        self.roms = SqliteRomRepository(conn)
        self.rom_installs = SqliteRomInstallRepository(conn)
        self.rom_metadata = SqliteRomMetadataRepository(conn)
        self.playtime = SqlitePlaytimeRepository(conn)
        self.rom_save_states = SqliteRomSaveStateRepository(conn)
        self.bios_files = SqliteBiosFileRepository(conn)
        self.firmware_cache = SqliteFirmwareCacheRepository(conn)
        self.sync_runs = SqliteSyncRunRepository(conn)
        self.kv_config = SqliteKvConfigRepository(conn)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            if exc_type is None:
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
        finally:
            conn.close()
            self._conn = None
