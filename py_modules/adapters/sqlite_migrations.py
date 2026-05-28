"""SQLite schema migration runner — applies numbered DDL to the plugin database.

Anything that creates the SQLite database or advances its schema at startup
belongs here. The runner discovers ``NNN_*.sql`` files under
``py_modules/db/migrations/``, applies the ones newer than the database's
recorded version, and stamps the new version into ``PRAGMA user_version``.

stdlib ``sqlite3`` only — no third-party migration tooling. The
Repository/Unit-of-Work layer that reads the database, and the full
per-connection runtime PRAGMA set, live elsewhere (#783); this module owns
only schema creation and version advancement.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3

# Leading-integer prefix on a ``.sql`` migration file: 001_initial.sql -> 1.
_MIGRATION_NAME = re.compile(r"^(\d+)_.+\.sql$")

# Migrations ship alongside this adapter at py_modules/db/migrations/.
MIGRATIONS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir, "db", "migrations"))

_module_logger = logging.getLogger(__name__)


def _discover_migrations(migrations_dir: str) -> list[tuple[int, str]]:
    """Return ``(version, path)`` for every ``NNN_*.sql`` file, sorted ascending."""
    discovered: list[tuple[int, str]] = []
    for name in os.listdir(migrations_dir):
        match = _MIGRATION_NAME.match(name)
        if match is None:
            continue
        discovered.append((int(match.group(1)), os.path.join(migrations_dir, name)))
    discovered.sort(key=lambda item: item[0])
    return discovered


def apply_migrations(
    db_path: str,
    migrations_dir: str = MIGRATIONS_DIR,
    *,
    logger: logging.Logger | None = None,
) -> int:
    """Apply every pending migration to the SQLite database at ``db_path``.

    Discovers ``NNN_*.sql`` files under ``migrations_dir``, orders them by
    the leading integer, and applies only those whose number is greater than
    the database's current ``PRAGMA user_version``. Each migration runs in
    its own ``BEGIN`` / DDL / version-bump / ``COMMIT`` transaction: on
    failure the transaction is rolled back (leaving the database at the last
    successfully-applied version) and the error re-raised. The database file
    and any missing parent directory are created on first run.

    Migration files must contain transaction-safe DDL only and must NOT carry
    their own ``BEGIN`` / ``COMMIT`` — the runner supplies the transaction.

    Parameters
    ----------
    db_path:
        Absolute path to the SQLite database file.
    migrations_dir:
        Directory holding the ``NNN_*.sql`` files. Defaults to the migrations
        shipped beside this adapter.
    logger:
        Logger for per-migration progress. Defaults to this module's logger.

    Returns
    -------
    int
        The ``user_version`` after all pending migrations have been applied
        (unchanged when there is nothing pending).

    Raises
    ------
    sqlite3.Error
        If a migration fails; its transaction is rolled back before re-raising.
    """
    log = logger or _module_logger
    migrations = _discover_migrations(migrations_dir)

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # isolation_level=None -> autocommit, so this module drives the
    # transaction boundaries explicitly. Each migration's BEGIN/COMMIT lives
    # inside the script handed to executescript(): executescript() commits any
    # pending transaction *before* it runs, so a BEGIN issued separately would
    # be committed away and break per-migration atomicity.
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        # journal_mode=WAL is persistent (recorded in the DB file); foreign_keys
        # is per-connection but kept ON so CASCADE-bearing DDL behaves here as it
        # will at runtime. Both must be set outside any transaction.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for version, path in migrations:
            if version <= current_version:
                continue
            log.info("Applying SQLite migration %s", os.path.basename(path))
            with open(path, encoding="utf-8") as migration_file:
                ddl = migration_file.read()
            script = f"BEGIN;\n{ddl}\nPRAGMA user_version = {version};\nCOMMIT;"
            try:
                conn.executescript(script)
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            current_version = version
        return current_version
    finally:
        conn.close()
