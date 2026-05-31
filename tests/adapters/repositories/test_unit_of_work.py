"""Tests for ``SqliteUnitOfWork`` — PRAGMA application, commit, and rollback."""

from __future__ import annotations

import sqlite3

import pytest

from adapters.repositories.unit_of_work import SqliteUnitOfWork
from domain.rom import Rom
from domain.rom_install import RomInstall


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
