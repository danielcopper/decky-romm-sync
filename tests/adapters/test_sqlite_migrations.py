"""Tests for the SQLite migration runner — schema creation + version advancement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from adapters.sqlite_migrations import MIGRATIONS_DIR, _discover_migrations, apply_migrations

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


def _columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row[1] for row in rows}
    finally:
        conn.close()


def _indexes(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
        return {row[1] for row in rows}
    finally:
        conn.close()


def _set_user_version(db_path: str, version: int) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
    finally:
        conn.close()


# Highest NNN in the shipped migrations dir (001_initial + 002_add_emulator_override
# + 003_unique_shortcut_app_id + 004_add_selected_disc).
_SHIPPED_VERSION = 4


class TestEmptyDatabase:
    """Empty DB (user_version 0) -> the full shipped schema is applied."""

    def test_applies_real_schema(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert _user_version(db_path) == _SHIPPED_VERSION
        # 002/004 ALTER roms, 003 adds an index — none adds a table, so the
        # table set is unchanged from v1.
        assert _tables(db_path) == _V1_TABLES

    def test_creates_missing_parent_directory(self, tmp_path: Path):
        # The runtime dir may not exist yet on first run.
        db_path = str(tmp_path / "nested" / "dir" / "romm_sync.db")

        apply_migrations(db_path)

        assert Path(db_path).exists()
        assert _user_version(db_path) == _SHIPPED_VERSION

    def test_idempotent_second_run_is_noop(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        # A re-run finds nothing pending and must not re-execute prior migrations
        # (which would fail on duplicate-table / duplicate-column if re-applied).
        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert _tables(db_path) == _V1_TABLES

    def test_adds_emulator_override_to_roms_only(self, tmp_path: Path):
        # 002 ALTERs only roms; rom_installs (and every other table) is untouched.
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "emulator_override" in _columns(db_path, "roms")
        assert "emulator_override" not in _columns(db_path, "rom_installs")

    def test_adds_selected_disc_to_roms_only(self, tmp_path: Path):
        # 004 ALTERs only roms; rom_installs (and every other table) is untouched.
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "selected_disc" in _columns(db_path, "roms")
        assert "selected_disc" not in _columns(db_path, "rom_installs")


def _insert_rom(conn: sqlite3.Connection, rom_id: int, app_id: int | None) -> None:
    """Insert a minimal ``roms`` row directly (bypassing the adapter) for migration tests."""
    conn.execute(
        "INSERT INTO roms (rom_id, platform_slug, name, fs_name, shortcut_app_id, last_synced_at) "
        "VALUES (?, 'snes', ?, ?, ?, '2026-01-01T00:00:00Z')",
        (rom_id, f"Game {rom_id}", f"game_{rom_id}.sfc", app_id),
    )


class Test003UniqueShortcutAppId:
    """003 — partial unique index on shortcut_app_id + de-dup of pre-existing collisions (#1036)."""

    def test_index_exists_after_full_apply(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) >= 3
        assert "idx_roms_shortcut_app_id" in _indexes(db_path, "roms")

    def test_v2_db_with_collision_dedups_keep_max_and_builds_index(self, tmp_path: Path):
        """A v2 DB holding a duplicate-appId collision de-dups (keep MAX rom_id),
        then the unique index builds cleanly — the upgrade path #1036 fixes."""
        db_path = str(tmp_path / "romm_sync.db")
        # Apply through 002 only, then seed a collision before 003 runs.
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 2)))
        assert _user_version(db_path) == 2

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            # Two bound rows share appId 5000; rom 7 is the higher (newer) id.
            _insert_rom(conn, 3, 5000)
            _insert_rom(conn, 7, 5000)
            # A third, distinct bound appId must survive untouched.
            _insert_rom(conn, 9, 6000)
        finally:
            conn.close()

        # Now apply 003 against the real shipped migrations dir.
        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert "idx_roms_shortcut_app_id" in _indexes(db_path, "roms")
        conn = sqlite3.connect(db_path)
        try:
            bindings = dict(conn.execute("SELECT rom_id, shortcut_app_id FROM roms ORDER BY rom_id").fetchall())
        finally:
            conn.close()
        # keep-MAX: rom 7 keeps 5000, rom 3 is unbound (NULL), rom 9 untouched.
        assert bindings == {3: None, 7: 5000, 9: 6000}

    def test_multiple_null_rows_coexist(self, tmp_path: Path):
        """The partial index allows many unbound (NULL appId) rows — only bound
        appIds are unique."""
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, None)
            _insert_rom(conn, 2, None)
            _insert_rom(conn, 3, None)
            null_count = conn.execute("SELECT COUNT(*) FROM roms WHERE shortcut_app_id IS NULL").fetchone()[0]
        finally:
            conn.close()
        assert null_count == 3

    def test_bound_appid_collision_rejected_by_index(self, tmp_path: Path):
        """Once the index exists, a raw INSERT of a second row sharing a bound
        appId raises IntegrityError — the constraint is real."""
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 5000)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_rom(conn, 2, 5000)
        finally:
            conn.close()


def _only_migrations_through(tmp_path: Path, max_version: int) -> Path:
    """Copy the shipped migrations up to (and including) ``max_version`` into a temp dir.

    Lets a test apply the schema through an earlier version, seed state, then
    apply the remaining shipped migrations against the real dir.
    """
    import shutil

    subset = tmp_path / f"migrations_through_{max_version}"
    subset.mkdir()
    for version, path in _discover_migrations(MIGRATIONS_DIR):
        if version <= max_version:
            shutil.copy(path, subset / Path(path).name)
    return subset


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
