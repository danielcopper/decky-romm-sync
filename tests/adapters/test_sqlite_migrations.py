"""Tests for the SQLite migration runner — schema creation + version advancement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from adapters.sqlite_migrations import MIGRATIONS_DIR, apply_migrations

# The 13 tables the shipped v1 schema (001_initial.sql) declares.
_V1_TABLES = {
    "roms",
    "rom_installs",
    "rom_metadata",
    "rom_playtime",
    "rom_save_states",
    "rom_save_files",
    "downloaded_bios",
    "firmware_cache",
    "sync_runs",
    "kv_config",
}


def _user_version(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _tables(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def _set_user_version(db_path: str, version: int) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
    finally:
        conn.close()


class TestEmptyDatabase:
    """Empty DB (user_version 0) -> the full shipped schema is applied."""

    def test_applies_real_v1_schema(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        final_version = apply_migrations(db_path)

        # Highest NNN in the shipped migrations dir is 001 -> version 1.
        assert final_version == 1
        assert _user_version(db_path) == 1
        assert _tables(db_path) == _V1_TABLES

    def test_creates_missing_parent_directory(self, tmp_path: Path):
        # The runtime dir may not exist yet on first run.
        db_path = str(tmp_path / "nested" / "dir" / "romm_sync.db")

        apply_migrations(db_path)

        assert Path(db_path).exists()
        assert _user_version(db_path) == 1

    def test_idempotent_second_run_is_noop(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        # A re-run finds nothing pending and must not re-execute 001 (which
        # would fail on duplicate-table creation if it were re-applied).
        final_version = apply_migrations(db_path)

        assert final_version == 1
        assert _tables(db_path) == _V1_TABLES


class TestPartiallyMigratedDatabase:
    """A DB already at version N -> only migrations > N are applied."""

    def test_only_pending_migration_applies(self, tmp_path: Path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        # 001 would create t1; 002 creates t2. The DB is preset to version 1,
        # so the runner must skip 001 entirely and apply only 002.
        (migrations_dir / "001_first.sql").write_text("CREATE TABLE t1 (x INTEGER);")
        (migrations_dir / "002_second.sql").write_text("CREATE TABLE t2 (y INTEGER);")

        db_path = str(tmp_path / "romm_sync.db")
        _set_user_version(db_path, 1)

        final_version = apply_migrations(db_path, str(migrations_dir))

        assert final_version == 2
        assert _user_version(db_path) == 2
        tables = _tables(db_path)
        assert "t2" in tables  # 002 applied
        assert "t1" not in tables  # 001 skipped, not re-run

    def test_numeric_ordering_not_lexical(self, tmp_path: Path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        # Unpadded names: lexical sort would place "10" before "2"; the runner
        # parses the integer prefix and applies 2 then 10.
        (migrations_dir / "2_two.sql").write_text("CREATE TABLE t_two (x INTEGER);")
        (migrations_dir / "10_ten.sql").write_text("CREATE TABLE t_ten (x INTEGER);")

        db_path = str(tmp_path / "romm_sync.db")

        final_version = apply_migrations(db_path, str(migrations_dir))

        assert final_version == 10
        assert {"t_two", "t_ten"} <= _tables(db_path)

    def test_empty_migrations_dir_returns_zero(self, tmp_path: Path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()  # no .sql files

        db_path = str(tmp_path / "romm_sync.db")

        final_version = apply_migrations(db_path, str(migrations_dir))

        assert final_version == 0
        assert _tables(db_path) == set()

    def test_ignores_non_migration_files(self, tmp_path: Path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_real.sql").write_text("CREATE TABLE kept (x INTEGER);")
        (migrations_dir / "README.md").write_text("# notes")
        (migrations_dir / "notes.txt").write_text("ignore me")
        (migrations_dir / "002_draft.sql.bak").write_text("CREATE TABLE nope (x INTEGER);")

        db_path = str(tmp_path / "romm_sync.db")

        final_version = apply_migrations(db_path, str(migrations_dir))

        assert final_version == 1
        tables = _tables(db_path)
        assert "kept" in tables
        assert "nope" not in tables


class TestAtomicRollback:
    """A failing migration rolls back fully and leaves the version untouched."""

    def test_broken_migration_raises_and_rolls_back(self, tmp_path: Path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        # First statement is valid, second is a syntax error. The whole
        # migration must roll back: no `good` table, version stays 0.
        (migrations_dir / "001_broken.sql").write_text(
            "CREATE TABLE good (x INTEGER);\nCREATE TABLE bad (this is not valid sql);"
        )

        db_path = str(tmp_path / "romm_sync.db")

        with pytest.raises(sqlite3.Error):
            apply_migrations(db_path, str(migrations_dir))

        assert _user_version(db_path) == 0
        assert "good" not in _tables(db_path)

    def test_failure_preserves_prior_applied_version(self, tmp_path: Path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "001_ok.sql").write_text("CREATE TABLE ok (x INTEGER);")
        (migrations_dir / "002_broken.sql").write_text("CREATE TABLE oops (this is not valid);")

        db_path = str(tmp_path / "romm_sync.db")

        with pytest.raises(sqlite3.Error):
            apply_migrations(db_path, str(migrations_dir))

        # 001 committed before 002 failed: version pinned at 1, 002 rolled back.
        assert _user_version(db_path) == 1
        tables = _tables(db_path)
        assert "ok" in tables
        assert "oops" not in tables


class TestUnreadableSource:
    """An unreadable migration source surfaces the OS error without corrupting state."""

    def test_unreadable_migration_propagates_oserror(self, tmp_path: Path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        # A directory named like a migration is matched by discovery but cannot
        # be opened — open() raises IsADirectoryError (an OSError) on every OS
        # and for every user, so this deterministically exercises the read path.
        (migrations_dir / "001_unreadable.sql").mkdir()

        db_path = str(tmp_path / "romm_sync.db")

        with pytest.raises(OSError):
            apply_migrations(db_path, str(migrations_dir))

        assert _user_version(db_path) == 0


def test_shipped_migrations_dir_resolves_to_real_schema():
    """The default MIGRATIONS_DIR points at the shipped 001_initial.sql."""
    assert (Path(MIGRATIONS_DIR) / "001_initial.sql").is_file()
