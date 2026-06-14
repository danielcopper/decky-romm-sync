"""Tests for ``SqliteUnitOfWork`` — PRAGMA application, BEGIN mode, commit, and rollback."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from adapters.repositories.unit_of_work import SqliteUnitOfWork
from domain.rom import Rom
from domain.rom_install import RomInstall

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _rom(rom_id: int) -> Rom:
    return Rom(
        rom_id=rom_id,
        platform_slug="snes",
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.sfc",
        shortcut_app_id=1000 + rom_id,
        last_synced_at="2026-01-01T00:00:00Z",
    )


class TestPragmas:
    def test_foreign_keys_on(self, uow: SqliteUnitOfWork):
        assert uow._conn is not None
        assert uow._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_synchronous_normal(self, uow: SqliteUnitOfWork):
        assert uow._conn is not None
        # synchronous=NORMAL is enum value 1
        assert uow._conn.execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_busy_timeout_5000(self, uow: SqliteUnitOfWork):
        assert uow._conn is not None
        assert uow._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    def test_journal_mode_wal_persisted_from_runner(self, uow: SqliteUnitOfWork):
        assert uow._conn is not None
        assert uow._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_foreign_keys_enforced_rejects_orphan_child(self, uow: SqliteUnitOfWork):
        # No parent roms row -> the FK CASCADE reference must reject the insert.
        with pytest.raises(sqlite3.IntegrityError):
            uow.rom_installs.save(
                RomInstall(
                    rom_id=12345,
                    file_path="/x",
                    rom_dir=None,
                    platform_slug="snes",
                    system="snes",
                    installed_at="2026-01-01T00:00:00Z",
                )
            )


class TestCommit:
    def test_clean_exit_persists_changes(self, db: str):
        with SqliteUnitOfWork(db) as unit:
            unit.roms.save(_rom(1))

        with SqliteUnitOfWork(db) as fresh:
            assert fresh.roms.get(1) is not None

    def test_commit_flag_via_in_transaction(self, db: str):
        unit = SqliteUnitOfWork(db)
        with unit:
            unit.roms.save(_rom(1))
        # Connection closed on exit.
        assert unit._conn is None


class TestRollback:
    def test_exception_rolls_back_changes(self, db: str):
        class Boom(Exception):
            pass

        with pytest.raises(Boom):  # noqa: SIM117 — the with-body must raise inside the UoW
            with SqliteUnitOfWork(db) as unit:
                unit.roms.save(_rom(1))
                raise Boom

        with SqliteUnitOfWork(db) as fresh:
            assert fresh.roms.get(1) is None
            assert fresh.roms.count() == 0

    def test_exit_without_enter_is_a_noop(self, db: str):
        # __exit__ called on an un-entered unit (no open connection) must not raise.
        unit = SqliteUnitOfWork(db)
        unit.__exit__(None, None, None)
        assert unit._conn is None

    def test_exception_is_re_raised(self, db: str):
        class Boom(Exception):
            pass

        with pytest.raises(Boom):  # noqa: SIM117
            with SqliteUnitOfWork(db) as unit:
                unit.roms.save(_rom(1))
                raise Boom


class TestRepositoryWiring:
    def test_all_nine_repositories_exposed(self, uow: SqliteUnitOfWork):
        for name in (
            "roms",
            "rom_installs",
            "rom_metadata",
            "playtime",
            "rom_save_states",
            "bios_files",
            "firmware_cache",
            "sync_runs",
            "kv_config",
        ):
            assert getattr(uow, name) is not None

    def test_repositories_share_one_connection(self, uow: SqliteUnitOfWork):
        # A write through one repo is visible through another in the same unit
        # (same connection, same open transaction) before commit.
        uow.roms.save(_rom(1))
        uow.rom_installs  # noqa: B018 — touch the peer repo
        assert uow.roms.count() == 1


class _RecordingConnection:
    """Wraps a real ``sqlite3.Connection`` and records every ``execute`` SQL string.

    Delegates all attribute access to the wrapped connection so the UoW behaves
    exactly as it would against a raw connection, while ``executed`` accumulates
    the SQL the UoW issued — letting a test assert the transaction-start
    statement non-vacuously.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.executed: list[str] = []

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        self.executed.append(sql)
        return self._conn.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


class TestBeginImmediate:
    """The UoW must start its transaction with ``BEGIN IMMEDIATE``, not a deferred ``BEGIN``."""

    def test_enter_issues_begin_immediate_not_plain_begin(self, db: str, monkeypatch: pytest.MonkeyPatch):
        recorded: list[str] = []
        real_connect = sqlite3.connect

        def spy_connect(*args: object, **kwargs: object) -> _RecordingConnection:
            conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
            wrapper = _RecordingConnection(conn)
            recorded.append(wrapper)  # type: ignore[arg-type]
            return wrapper

        monkeypatch.setattr(sqlite3, "connect", spy_connect)

        with SqliteUnitOfWork(db) as unit:
            unit.roms.save(_rom(1))

        assert len(recorded) == 1
        statements = recorded[0].executed  # type: ignore[attr-defined]
        # The write lock is taken up front with BEGIN IMMEDIATE ...
        assert "BEGIN IMMEDIATE" in statements
        # ... and a plain deferred BEGIN (the SNAPSHOT-prone start) is NOT issued.
        assert "BEGIN" not in statements


def _wal_connection(db_path: str, *, busy_timeout: int = 5000) -> sqlite3.Connection:
    """Open a raw connection in WAL mode, mirroring the UoW's runtime PRAGMAs.

    ``journal_mode=WAL`` is persistent in the database file once set; the first
    connection to a fresh standalone DB stamps it (production gets it from the
    migration runner). Setting it on every connection is idempotent and keeps
    these standalone-DB tests self-contained.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


class TestBusySnapshotRegression:
    """Deterministic proof of the #1011 SNAPSHOT failure mode and that the fix avoids it.

    Both tests drive two real connections against one WAL database with manual
    lock ordering on a single thread, so there is no timing race.
    """

    def test_deferred_begin_read_then_write_raises_under_concurrent_commit(self, tmp_path: Path):
        # (a) Pin the bug with a *deferred* BEGIN on raw connections (NOT the UoW):
        # conn1 takes a read snapshot, conn2 commits a write, conn1's later write
        # upgrade fails IMMEDIATELY with OperationalError — the SQLITE_BUSY_SNAPSHOT
        # that busy_timeout does not retry.
        db_path = str(tmp_path / "snapshot.db")
        seed = _wal_connection(db_path)
        seed.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
        seed.execute("INSERT INTO t (id, n) VALUES (1, 1)")
        seed.close()

        # A short busy_timeout on conn1 so a (non-snapshot) lock contention would
        # also fail fast rather than hang the test.
        conn1 = _wal_connection(db_path, busy_timeout=100)
        conn2 = _wal_connection(db_path)
        try:
            conn1.execute("BEGIN")  # deferred — no lock taken yet
            conn1.execute("SELECT n FROM t WHERE id = 1").fetchone()  # take the snapshot

            conn2.execute("BEGIN IMMEDIATE")
            conn2.execute("UPDATE t SET n = 2 WHERE id = 1")
            conn2.execute("COMMIT")  # snapshot conn1 holds is now stale

            with pytest.raises(sqlite3.OperationalError):
                # read -> write upgrade against a stale snapshot: SQLITE_BUSY_SNAPSHOT
                conn1.execute("UPDATE t SET n = 3 WHERE id = 1")
        finally:
            conn1.close()
            conn2.close()

    def test_uow_immediate_read_then_write_survives_concurrent_writer(self, tmp_path: Path):
        # (b) Same lock ordering, but conn1 is the real SqliteUnitOfWork. Its
        # BEGIN IMMEDIATE takes the write lock at transaction start, so the
        # contending writer (conn2) cannot commit in between; conn2 instead fails
        # fast with "database is locked" on its own short busy_timeout. The UoW's
        # read-then-write completes and commits cleanly — no SNAPSHOT error.
        db_path = str(tmp_path / "immediate.db")
        apply = _wal_connection(db_path)
        apply.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
        apply.execute("INSERT INTO t (id, n) VALUES (1, 1)")
        apply.close()

        contender = _wal_connection(db_path, busy_timeout=100)
        try:
            with SqliteUnitOfWork(db_path) as unit:
                conn = unit._conn
                assert conn is not None
                # Read first (this is the read-then-write shape that broke with deferred BEGIN).
                row = conn.execute("SELECT n FROM t WHERE id = 1").fetchone()
                assert row[0] == 1

                # A second writer contends now; with the UoW holding the write lock
                # from BEGIN IMMEDIATE, the contender cannot slip a commit in — it
                # fails fast on its short busy_timeout instead.
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute("BEGIN IMMEDIATE")
                    contender.execute("UPDATE t SET n = 99 WHERE id = 1")

                # The UoW's own read-then-write proceeds without a SNAPSHOT error.
                conn.execute("UPDATE t SET n = 2 WHERE id = 1")
            # __exit__ committed cleanly.

            verify = _wal_connection(db_path)
            try:
                assert verify.execute("SELECT n FROM t WHERE id = 1").fetchone()[0] == 2
            finally:
                verify.close()
        finally:
            contender.close()

    def test_two_sequential_immediate_write_uows_serialize(self, db: str):
        # A simpler serialization guard: two sequential write UoWs both succeed
        # and the second sees the first's committed write.
        with SqliteUnitOfWork(db) as first:
            first.roms.save(_rom(1))

        with SqliteUnitOfWork(db) as second:
            assert second.roms.get(1) is not None
            second.roms.save(_rom(2))

        with SqliteUnitOfWork(db) as third:
            assert third.roms.count() == 2
