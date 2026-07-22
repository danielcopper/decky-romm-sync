"""Tests for the SQLite migration runner — schema creation + version advancement."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import get_args

import pytest

from adapters.sqlite_migrations import MIGRATIONS_DIR, _discover_migrations, apply_migrations
from domain.sync_run import SyncRunStatus

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
# + 003_unique_shortcut_app_id + 004_add_selected_disc
# + 005_unconfirm_legacy_slot_confirmations + 006_native_play_sessions
# + 007_add_last_played + 008_add_version_metadata
# + 009_add_last_session_start_monotonic + 010_add_sibling_group_key_index
# + 011_rekey_sibling_group_key + 012_add_platform_sync_state
# + 013_add_interrupted_sync_run_status + 014_add_paused_sync_run_status
# + 015_add_applied_launch_options + 016_add_cover_source
# + 017_add_last_sync_server_hash + 018_rename_rom_save_states
# + 019_add_collection_sync_state + 020_add_fetch_generation
# + 021_add_rom_fs_size + 022_rename_collection_kind_user_to_standard).
_SHIPPED_VERSION = 22

# Tables after every shipped migration: the v1 set plus 006's play-session outbox,
# 012's per-platform completion stamp, and 019's per-collection completion stamp,
# with 018 renaming the save-sync scalar table rom_save_states -> rom_save_sync_states.
_SHIPPED_TABLES = (_V1_TABLES - {"rom_save_states"}) | {
    "rom_save_sync_states",
    "rom_playtime_sessions",
    "platform_sync_state",
    "collection_sync_state",
}


class TestEmptyDatabase:
    """Empty DB (user_version 0) -> the full shipped schema is applied."""

    def test_applies_real_schema(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert _user_version(db_path) == _SHIPPED_VERSION
        # 002/004 ALTER roms, 003/010 add indexes; 006 adds the play-session
        # outbox table and 012 the per-platform completion stamp — the two tables
        # added past v1.
        assert _tables(db_path) == _SHIPPED_TABLES

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
        assert _tables(db_path) == _SHIPPED_TABLES

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


_VERSION_METADATA_COLUMNS = {
    "sibling_group_key",
    "regions",
    "languages",
    "revision",
    "tags",
    "is_main_sibling",
}


class Test008VersionMetadata:
    """008 adds the sibling-group key + version dimensions to roms only, defaulting
    a pre-existing row (backfilled by the next sync)."""

    def test_adds_version_columns_to_roms_only(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert _columns(db_path, "roms") >= _VERSION_METADATA_COLUMNS
        # rom_installs (and every other table) is untouched by 008.
        assert not (_VERSION_METADATA_COLUMNS & _columns(db_path, "rom_installs"))

    def test_existing_row_gets_defaults_across_the_migration(self, tmp_path: Path):
        # A row created at v7 (before 008) survives and reads back the column
        # defaults: NULL group key, empty JSON arrays, blank revision, 0 flag.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 7)))
        assert _user_version(db_path) == 7
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 5000)
        finally:
            conn.close()

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT sibling_group_key, regions, languages, revision, tags, is_main_sibling "
                "FROM roms WHERE rom_id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert row["sibling_group_key"] is None
        assert row["regions"] == "[]"
        assert row["languages"] == "[]"
        assert row["revision"] == ""
        assert row["tags"] == "[]"
        assert row["is_main_sibling"] == 0


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


def _insert_save_state(conn: sqlite3.Connection, rom_id: int, active_slot: str | None, slot_confirmed: int) -> None:
    """Insert a minimal ``rom_save_states`` row directly (bypassing the adapter)."""
    conn.execute(
        "INSERT INTO rom_save_states (rom_id, active_slot, slot_confirmed) VALUES (?, ?, ?)",
        (rom_id, active_slot, slot_confirmed),
    )


class Test005UnconfirmLegacySlotConfirmations:
    """005 — un-confirms legacy (active_slot NULL) confirmations; never deletes rows (#1276)."""

    def test_flips_only_legacy_confirmed_rows(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")
        # Apply through 004, then seed the three relevant shapes before 005 runs.
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 4)))
        assert _user_version(db_path) == 4

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            for rid in (1, 2, 3):
                _insert_rom(conn, rid, rid)
            # 1: legacy + confirmed  -> must flip to 0
            _insert_save_state(conn, 1, None, 1)
            # 2: named + confirmed   -> must stay 1
            _insert_save_state(conn, 2, "default", 1)
            # 3: legacy + unconfirmed -> already 0, must stay 0
            _insert_save_state(conn, 3, None, 0)
            # A per-file baseline on the legacy row — must survive untouched.
            conn.execute(
                "INSERT INTO rom_save_files (rom_id, filename, last_sync_hash) VALUES (1, 'pokemon.srm', 'abc123')"
            )
        finally:
            conn.close()

        final_version = apply_migrations(db_path)
        assert final_version == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            # The full apply includes 018, which renamed the seeded rom_save_states
            # table to rom_save_sync_states — read the confirmations back from it.
            confirmations = dict(
                conn.execute("SELECT rom_id, slot_confirmed FROM rom_save_sync_states ORDER BY rom_id").fetchall()
            )
            file_rows = conn.execute("SELECT filename, last_sync_hash FROM rom_save_files WHERE rom_id = 1").fetchall()
        finally:
            conn.close()

        # Only the legacy-confirmed row (1) flips; named (2) and already-unconfirmed (3) are untouched.
        assert confirmations == {1: 0, 2: 1, 3: 0}
        # The per-file baseline is preserved — 005 only flips slot_confirmed, never deletes.
        assert file_rows == [("pokemon.srm", "abc123")]


class Test006NativePlaySessions:
    """006 — drops rom_playtime.note_id, adds the rom_playtime_sessions outbox (#1219)."""

    def test_drops_note_id_adds_outbox_preserves_totals(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")
        # Apply through 005, then seed a populated rom_playtime row (with note_id)
        # before 006 runs.
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 5)))
        assert _user_version(db_path) == 5
        assert "note_id" in _columns(db_path, "rom_playtime")

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 1)
            conn.execute(
                "INSERT INTO rom_playtime (rom_id, total_seconds, session_count, note_id) VALUES (1, 3600, 2, 7)"
            )
        finally:
            conn.close()

        final_version = apply_migrations(db_path)
        assert final_version == _SHIPPED_VERSION

        # note_id gone; the outbox table exists carrying the attempts column.
        assert "note_id" not in _columns(db_path, "rom_playtime")
        assert "rom_playtime_sessions" in _tables(db_path)
        assert "attempts" in _columns(db_path, "rom_playtime_sessions")

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT total_seconds, session_count FROM rom_playtime WHERE rom_id = 1").fetchone()
        finally:
            conn.close()
        # Totals survive the column drop untouched.
        assert row == (3600, 2)

    def test_outbox_cascades_on_parent_rom_delete(self, tmp_path: Path):
        """Deleting the parent roms row cascades away its rom_playtime_sessions rows (#1219)."""
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)
        assert _user_version(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 1)
            conn.execute("INSERT INTO rom_playtime (rom_id, total_seconds, session_count) VALUES (1, 60, 1)")
            conn.execute(
                "INSERT INTO rom_playtime_sessions (rom_id, start_time, device_id, end_time, duration_ms, attempts) "
                "VALUES (1, 's1', 'dev-1', 'e1', 60000, 0)"
            )
            before = conn.execute("SELECT COUNT(*) FROM rom_playtime_sessions").fetchone()[0]
            # Delete the FK parent with cascades enabled.
            conn.execute("DELETE FROM roms WHERE rom_id = 1")
            after = conn.execute("SELECT COUNT(*) FROM rom_playtime_sessions").fetchone()[0]
        finally:
            conn.close()

        assert before == 1
        assert after == 0


class Test007AddLastPlayed:
    """007 — adds rom_playtime.last_played, preserving existing scalars (#903)."""

    def test_adds_last_played_column_and_stamps_version(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")
        # Absent through 006, present after the full apply.
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 6)))
        assert _user_version(db_path) == 6
        assert "last_played" not in _columns(db_path, "rom_playtime")

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "last_played" in _columns(db_path, "rom_playtime")

    def test_preserves_existing_playtime_scalars(self, tmp_path: Path):
        """The additive column leaves total_seconds / session_count untouched (NULL last_played)."""
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 6)))

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 1)
            conn.execute("INSERT INTO rom_playtime (rom_id, total_seconds, session_count) VALUES (1, 3600, 5)")
        finally:
            conn.close()

        assert apply_migrations(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT total_seconds, session_count, last_played FROM rom_playtime WHERE rom_id = 1"
            ).fetchone()
        finally:
            conn.close()
        # Pre-existing scalars survive; the new column defaults to NULL.
        assert row == (3600, 5, None)


class Test009AddLastSessionStartMonotonic:
    """009 — adds rom_playtime.last_session_start_monotonic, preserving scalars (#1148)."""

    def test_adds_monotonic_column_and_stamps_version(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")
        # Absent through 008, present after the full apply.
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 8)))
        assert _user_version(db_path) == 8
        assert "last_session_start_monotonic" not in _columns(db_path, "rom_playtime")

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "last_session_start_monotonic" in _columns(db_path, "rom_playtime")

    def test_pre_migration_row_reads_null_monotonic(self, tmp_path: Path):
        """A rom_playtime row created at v8 survives 009 and reads NULL (→ wall fallback)."""
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 8)))

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 1)
            conn.execute("INSERT INTO rom_playtime (rom_id, total_seconds, session_count) VALUES (1, 3600, 5)")
        finally:
            conn.close()

        assert apply_migrations(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT total_seconds, session_count, last_session_start_monotonic FROM rom_playtime WHERE rom_id = 1"
            ).fetchone()
        finally:
            conn.close()
        # Pre-existing scalars survive; the new column defaults to NULL.
        assert row == (3600, 5, None)


class Test010SiblingGroupKeyIndex:
    """010 — non-unique index on roms(sibling_group_key) for the group readers (#1296)."""

    def test_index_exists_after_full_apply(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "idx_roms_sibling_group_key" in _indexes(db_path, "roms")

    def test_index_absent_before_010(self, tmp_path: Path):
        # The index is added by 010 — a DB at v9 does not yet carry it.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 9)))

        assert _user_version(db_path) == 9
        assert "idx_roms_sibling_group_key" not in _indexes(db_path, "roms")

    def test_index_is_non_unique_allows_duplicate_group_keys(self, tmp_path: Path):
        # A sibling group has many rows sharing one key — the index must be
        # non-unique so two rows with the same sibling_group_key coexist.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, None)
            _insert_rom(conn, 2, None)
            conn.execute("UPDATE roms SET sibling_group_key = 'igdb:42:7' WHERE rom_id IN (1, 2)")
            count = conn.execute("SELECT COUNT(*) FROM roms WHERE sibling_group_key = 'igdb:42:7'").fetchone()[0]
        finally:
            conn.close()
        assert count == 2


class Test011RekeySiblingGroupKey:
    """011 — NULLs every sibling_group_key so the next sync re-derives it under the
    component kernel; the needs_backfill gate then forces a full refetch (#1368)."""

    def test_nulls_all_sibling_group_keys(self, tmp_path: Path):
        # Rows carrying old-style keys at v10 have every key NULLed by 011, so the
        # incremental-skip's needs_backfill gate (any NULL key) forces a full
        # refetch + recompute on the next sync.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 10)))
        assert _user_version(db_path) == 10

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 5000)
            _insert_rom(conn, 2, 6000)
            conn.execute("UPDATE roms SET sibling_group_key = 'igdb:100:57' WHERE rom_id IN (1, 2)")
        finally:
            conn.close()

        final_version = apply_migrations(db_path)
        assert final_version == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            keys = [row[0] for row in conn.execute("SELECT sibling_group_key FROM roms ORDER BY rom_id").fetchall()]
        finally:
            conn.close()
        assert keys == [None, None]

    def test_only_nulls_the_key_column_rows_survive(self, tmp_path: Path):
        # 011 is a pure re-key — the rows and their other fields (the binding) are
        # untouched; only sibling_group_key is cleared.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 10)))

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 5000)
            conn.execute("UPDATE roms SET sibling_group_key = 'igdb:100:57', regions = '[\"USA\"]' WHERE rom_id = 1")
        finally:
            conn.close()

        assert apply_migrations(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT rom_id, shortcut_app_id, sibling_group_key, regions FROM roms WHERE rom_id = 1"
            ).fetchone()
        finally:
            conn.close()
        # Binding + version dimensions survive; only the group key is NULLed.
        assert row == (1, 5000, None, '["USA"]')


class Test012PlatformSyncState:
    """012 — adds the platform_sync_state completion-stamp table (#1025 / ADR-0023)."""

    def test_table_exists_after_full_apply(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "platform_sync_state" in _tables(db_path)
        # fetch_id is added by 020 (#1504); after a full apply the table carries it.
        assert _columns(db_path, "platform_sync_state") == {
            "platform_slug",
            "completed_at",
            "rom_count",
            "fetch_id",
        }

    def test_table_absent_before_012(self, tmp_path: Path):
        # The table is added by 012 — a DB at v11 does not yet carry it.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 11)))

        assert _user_version(db_path) == 11
        assert "platform_sync_state" not in _tables(db_path)

    def test_platform_slug_is_primary_key_upsert(self, tmp_path: Path):
        # platform_slug is the PK — a second write for the same slug replaces the row.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO platform_sync_state (platform_slug, completed_at, rom_count) "
                "VALUES ('n64', '2026-01-01T00:00:00+00:00', 100)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO platform_sync_state (platform_slug, completed_at, rom_count) "
                "VALUES ('n64', '2026-02-01T00:00:00+00:00', 105)"
            )
            rows = conn.execute("SELECT platform_slug, completed_at, rom_count FROM platform_sync_state").fetchall()
        finally:
            conn.close()
        assert rows == [("n64", "2026-02-01T00:00:00+00:00", 105)]

    def test_survives_full_apply_with_seeded_v11_state(self, tmp_path: Path):
        # A DB seeded at v11 (before 012) upgrades cleanly and gains the table.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 11)))
        assert _user_version(db_path) == 11

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert "platform_sync_state" in _tables(db_path)


def _insert_sync_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    finished_at: str | None = None,
    error: str | None = None,
) -> None:
    """Insert a minimal ``sync_runs`` row directly (bypassing the adapter) for migration tests."""
    conn.execute(
        "INSERT INTO sync_runs (id, started_at, status, platforms_planned, roms_planned, finished_at, error) "
        "VALUES (?, '2026-05-28T10:00:00', ?, 3, 120, ?, ?)",
        (run_id, status, finished_at, error),
    )


class Test013InterruptedSyncRunStatus:
    """013 — widens the sync_runs status CHECK with 'interrupted' via an in-place table rebuild (#1025)."""

    def test_rebuild_preserves_existing_rows(self, tmp_path: Path):
        # Seed a completed + a cancelled run at v12 (before the rebuild), then apply
        # 013 and assert every column of both rows survives the copy unchanged.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 12)))
        assert _user_version(db_path) == 12

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            _insert_sync_run(conn, run_id="run-done", status="completed", finished_at="2026-05-28T10:05:00")
            _insert_sync_run(
                conn,
                run_id="run-cancel",
                status="cancelled",
                finished_at="2026-05-28T11:05:00",
                error="user aborted",
            )
        finally:
            conn.close()

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert _user_version(db_path) == _SHIPPED_VERSION
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id, started_at, status, platforms_planned, roms_planned, finished_at, "
                "platforms_completed, collections_completed, error FROM sync_runs ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [
            (
                "run-cancel",
                "2026-05-28T10:00:00",
                "cancelled",
                3,
                120,
                "2026-05-28T11:05:00",
                None,
                None,
                "user aborted",
            ),
            ("run-done", "2026-05-28T10:00:00", "completed", 3, 120, "2026-05-28T10:05:00", None, None, None),
        ]

    def test_interrupted_status_accepted_after_rebuild(self, tmp_path: Path):
        # The widened CHECK accepts the new terminal status.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)
        assert _user_version(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            _insert_sync_run(
                conn,
                run_id="run-int",
                status="interrupted",
                finished_at="2026-05-28T12:05:00",
                error="external death",
            )
            status = conn.execute("SELECT status FROM sync_runs WHERE id = 'run-int'").fetchone()[0]
        finally:
            conn.close()
        assert status == "interrupted"

    def test_bogus_status_rejected_by_surviving_check(self, tmp_path: Path):
        # The rebuilt table keeps its status CHECK — an unknown status still raises.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_sync_run(conn, run_id="run-bad", status="teleported", finished_at="2026-05-28T13:05:00")
        finally:
            conn.close()

    @pytest.mark.parametrize("status", get_args(SyncRunStatus))
    def test_every_domain_status_accepted_by_check(self, tmp_path: Path, status: str):
        # Bind the domain SyncRunStatus literal to migration 013's CHECK: every value
        # the enum allows must INSERT cleanly. Adding a status to the literal without
        # widening the CHECK then fails CI here (the bogus-status test above guards
        # the reverse direction — a value in the CHECK but not the literal).
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            _insert_sync_run(conn, run_id=f"run-{status}", status=status)
            stored = conn.execute("SELECT status FROM sync_runs WHERE id = ?", (f"run-{status}",)).fetchone()[0]
        finally:
            conn.close()
        assert stored == status


class Test014PausedSyncRunStatus:
    """014 — widens the sync_runs status CHECK with 'paused' via an in-place table rebuild (#1383)."""

    def test_rebuild_from_13_preserves_existing_rows(self, tmp_path: Path):
        # Seed a completed + an interrupted run at v13 (before the 014 rebuild),
        # then apply 014 and assert every column of both rows survives the copy.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 13)))
        assert _user_version(db_path) == 13

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            _insert_sync_run(conn, run_id="run-done", status="completed", finished_at="2026-07-11T10:05:00")
            _insert_sync_run(
                conn,
                run_id="run-int",
                status="interrupted",
                finished_at="2026-07-11T11:05:00",
                error="external death",
            )
        finally:
            conn.close()

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert _user_version(db_path) == _SHIPPED_VERSION
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT id, status, finished_at, error FROM sync_runs ORDER BY id").fetchall()
        finally:
            conn.close()
        assert rows == [
            ("run-done", "completed", "2026-07-11T10:05:00", None),
            ("run-int", "interrupted", "2026-07-11T11:05:00", "external death"),
        ]

    def test_paused_status_accepted_after_rebuild(self, tmp_path: Path):
        # The widened CHECK accepts the new terminal status.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)
        assert _user_version(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            _insert_sync_run(
                conn,
                run_id="run-paused",
                status="paused",
                finished_at="2026-07-11T12:05:00",
                error="Sync paused: Steam's memory is nearly full.",
            )
            status = conn.execute("SELECT status FROM sync_runs WHERE id = 'run-paused'").fetchone()[0]
        finally:
            conn.close()
        assert status == "paused"

    def test_bogus_status_still_rejected(self, tmp_path: Path):
        # The rebuilt table keeps its status CHECK — an unknown status still raises.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_sync_run(conn, run_id="run-bad", status="hibernated", finished_at="2026-07-11T13:05:00")
        finally:
            conn.close()

    def test_paused_absent_before_014(self, tmp_path: Path):
        # At v13 the CHECK does not yet accept 'paused'.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 13)))

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_sync_run(conn, run_id="run-early", status="paused", finished_at="2026-07-11T14:05:00")
        finally:
            conn.close()


class Test015AppliedLaunchOptions:
    """015 — adds the nullable applied_launch_options column to roms only (#1383)."""

    def test_adds_applied_launch_options_to_roms_only(self, tmp_path: Path):
        # 015 ALTERs only roms; rom_installs (and every other table) is untouched.
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "applied_launch_options" in _columns(db_path, "roms")
        assert "applied_launch_options" not in _columns(db_path, "rom_installs")

    def test_existing_row_reads_null_across_the_migration(self, tmp_path: Path):
        # A row seeded before 015 reads NULL for the new column (unknown = never
        # skipped), the "no data invented" contract the delta apply relies on.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 14)))
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO roms (rom_id, platform_slug, name, fs_name, last_synced_at) "
                "VALUES (1, 'snes', 'Game', 'game.sfc', '2026-07-11T10:00:00')"
            )
        finally:
            conn.close()

        assert apply_migrations(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            value = conn.execute("SELECT applied_launch_options FROM roms WHERE rom_id = 1").fetchone()[0]
        finally:
            conn.close()
        assert value is None

    def test_applied_launch_options_absent_before_015(self, tmp_path: Path):
        # At v14 the column does not yet exist.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 14)))
        assert "applied_launch_options" not in _columns(db_path, "roms")


class Test016CoverSource:
    """016 — adds the nullable cover_source fingerprint column to roms only (#1386)."""

    def test_adds_cover_source_to_roms_only(self, tmp_path: Path):
        # 016 ALTERs only roms; rom_installs (and every other table) is untouched.
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "cover_source" in _columns(db_path, "roms")
        assert "cover_source" not in _columns(db_path, "rom_installs")

    def test_existing_row_reads_null_across_the_migration(self, tmp_path: Path):
        # A row seeded before 016 reads NULL for the new column (unknown → the
        # NULL-adopt path), the "no data invented" contract the invalidation
        # pass relies on — a fingerprint is never fabricated by the migration.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 15)))
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO roms (rom_id, platform_slug, name, fs_name, last_synced_at) "
                "VALUES (1, 'snes', 'Game', 'game.sfc', '2026-07-11T10:00:00')"
            )
        finally:
            conn.close()

        assert apply_migrations(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            value = conn.execute("SELECT cover_source FROM roms WHERE rom_id = 1").fetchone()[0]
        finally:
            conn.close()
        assert value is None

    def test_cover_source_absent_before_016(self, tmp_path: Path):
        # At v15 the column does not yet exist.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 15)))
        assert "cover_source" not in _columns(db_path, "roms")


class Test017LastSyncServerHash:
    """017 — adds the nullable last_sync_server_hash column to rom_save_files only (#1468)."""

    def test_adds_last_sync_server_hash_to_rom_save_files_only(self, tmp_path: Path):
        # 017 ALTERs only rom_save_files; the save-sync scalar table (renamed to
        # rom_save_sync_states by 018) and every other table is untouched by 017.
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "last_sync_server_hash" in _columns(db_path, "rom_save_files")
        assert "last_sync_server_hash" not in _columns(db_path, "rom_save_sync_states")

    def test_existing_row_reads_null_across_the_migration(self, tmp_path: Path):
        # A file baseline seeded before 017 reads NULL for the new column (no
        # stored server hash → the identity check's parity fallback), the "no
        # data invented" contract: a server hash is never fabricated by the
        # migration, only stamped by a later real sync.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 16)))
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO roms (rom_id, platform_slug, name, fs_name, last_synced_at) "
                "VALUES (1, 'gba', 'Game', 'game.gba', '2026-07-11T10:00:00')"
            )
            conn.execute("INSERT INTO rom_save_states (rom_id) VALUES (1)")
            conn.execute(
                "INSERT INTO rom_save_files (rom_id, filename, last_sync_hash) VALUES (1, 'game.srm', 'deadbeef')"
            )
        finally:
            conn.close()

        assert apply_migrations(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            value = conn.execute(
                "SELECT last_sync_server_hash FROM rom_save_files WHERE rom_id = 1 AND filename = 'game.srm'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert value is None

    def test_last_sync_server_hash_absent_before_017(self, tmp_path: Path):
        # At v16 the column does not yet exist.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 16)))
        assert "last_sync_server_hash" not in _columns(db_path, "rom_save_files")


class Test018RenameRomSaveStates:
    """018 — renames the save-sync scalar table rom_save_states -> rom_save_sync_states (#1478 terminology)."""

    def test_table_renamed_after_full_apply(self, tmp_path: Path):
        # After the full apply the scalar table carries the new name; the old name
        # is gone. The rom_save_files child is anchored on roms, not renamed.
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        tables = _tables(db_path)
        assert "rom_save_sync_states" in tables
        assert "rom_save_states" not in tables
        assert "rom_save_files" in tables

    def test_table_named_rom_save_states_before_018(self, tmp_path: Path):
        # At v17 the scalar table still carries the pre-rename name.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 17)))

        assert _user_version(db_path) == 17
        tables = _tables(db_path)
        assert "rom_save_states" in tables
        assert "rom_save_sync_states" not in tables

    def test_seeded_row_and_child_baseline_survive_the_rename(self, tmp_path: Path):
        # A scalar row + its rom_save_files child seeded at v17 (pre-rename table
        # name) survive 018 intact and read back from the renamed table — the
        # data-preserving ALTER TABLE ... RENAME TO contract.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 17)))
        assert _user_version(db_path) == 17

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            _insert_rom(conn, 1, 5000)
            # Seeded under the pre-018 table name (its name at v17).
            conn.execute(
                "INSERT INTO rom_save_states (rom_id, active_slot, slot_confirmed, emulator, system) "
                "VALUES (1, 'default', 1, 'retroarch', 'gba')"
            )
            conn.execute(
                "INSERT INTO rom_save_files (rom_id, filename, last_sync_hash) VALUES (1, 'game.srm', 'deadbeef')"
            )
        finally:
            conn.close()

        final_version = apply_migrations(db_path)
        assert final_version == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            state_row = conn.execute(
                "SELECT rom_id, active_slot, slot_confirmed, emulator, system "
                "FROM rom_save_sync_states WHERE rom_id = 1"
            ).fetchone()
            file_row = conn.execute("SELECT filename, last_sync_hash FROM rom_save_files WHERE rom_id = 1").fetchone()
        finally:
            conn.close()
        # Every scalar column survives the rename; the untouched child baseline too.
        assert state_row == (1, "default", 1, "retroarch", "gba")
        assert file_row == ("game.srm", "deadbeef")


class Test019CollectionSyncState:
    """019 — adds the collection_sync_state completion-stamp table (#742 / ADR-0023)."""

    def test_table_exists_after_full_apply(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "collection_sync_state" in _tables(db_path)
        assert _columns(db_path, "collection_sync_state") == {
            "collection_id",
            "collection_kind",
            "updated_at",
            "completed_at",
            "rom_count",
            "member_rom_ids",
        }

    def test_table_absent_before_019(self, tmp_path: Path):
        # The table is added by 019 — a DB at v18 does not yet carry it.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 18)))

        assert _user_version(db_path) == 18
        assert "collection_sync_state" not in _tables(db_path)

    def test_composite_key_is_primary_key_upsert(self, tmp_path: Path):
        # (collection_id, collection_kind) is the PK — a same-key write replaces the
        # row, while the same id under a different kind coexists.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO collection_sync_state "
                "(collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids) "
                "VALUES ('7', 'standard', '2026-01-01T00:00:00', '2026-01-01T00:05:00', 2, '[1, 2]')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO collection_sync_state "
                "(collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids) "
                "VALUES ('7', 'standard', '2026-02-01T00:00:00', '2026-02-01T00:05:00', 3, '[1, 2, 3]')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO collection_sync_state "
                "(collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids) "
                "VALUES ('7', 'smart', '2026-01-01T00:00:00', '2026-01-01T00:05:00', 1, '[9]')"
            )
            rows = conn.execute(
                "SELECT collection_id, collection_kind, rom_count FROM collection_sync_state ORDER BY collection_kind"
            ).fetchall()
        finally:
            conn.close()
        # The standard row was replaced (rom_count 3); the smart row coexists.
        assert rows == [("7", "smart", 1), ("7", "standard", 3)]

    def test_member_rom_ids_rejects_invalid_json(self, tmp_path: Path):
        # The json_valid CHECK guards the JSON-array column.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO collection_sync_state "
                    "(collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids) "
                    "VALUES ('7', 'standard', 'x', 'y', 0, 'not-json')"
                )
        finally:
            conn.close()

    def test_survives_full_apply_with_seeded_v18_state(self, tmp_path: Path):
        # A DB seeded at v18 (before 019) upgrades cleanly and gains the table.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 18)))
        assert _user_version(db_path) == 18

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert "collection_sync_state" in _tables(db_path)


class Test020FetchGeneration:
    """020 — adds the fetch-generation marker columns (#1504)."""

    def test_adds_the_marker_to_roms_and_the_platform_stamp(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "last_fetch_id" in _columns(db_path, "roms")
        assert "fetch_id" in _columns(db_path, "platform_sync_state")

    def test_columns_absent_before_020(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 19)))

        assert _user_version(db_path) == 19
        assert "last_fetch_id" not in _columns(db_path, "roms")
        assert "fetch_id" not in _columns(db_path, "platform_sync_state")

    def test_existing_rows_read_null_across_the_migration(self, tmp_path: Path):
        # A pre-020 row keeps its data and reads NULL for the new marker — the
        # "unknown generation" state the skip falls back to counting every row for.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 19)))
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO roms (rom_id, platform_slug, name, fs_name, last_synced_at) "
                "VALUES (4375, 'dc', 'Game', 'game.gdi', '2026-07-15T20:56:03')"
            )
            conn.execute(
                "INSERT INTO platform_sync_state (platform_slug, completed_at, rom_count) "
                "VALUES ('dc', '2026-07-15T20:56:03', 2)"
            )
        finally:
            conn.close()

        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            rom = conn.execute("SELECT name, last_fetch_id FROM roms WHERE rom_id = 4375").fetchone()
            stamp = conn.execute(
                "SELECT rom_count, fetch_id FROM platform_sync_state WHERE platform_slug = 'dc'"
            ).fetchone()
        finally:
            conn.close()
        assert rom == ("Game", None)
        assert stamp == (2, None)

    def test_marker_round_trips(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO roms (rom_id, platform_slug, name, fs_name, last_synced_at, last_fetch_id) "
                "VALUES (25135, 'dc', 'Game', 'game.gdi', '2026-07-20T06:27:12', 'run-abc')"
            )
            stored = conn.execute("SELECT last_fetch_id FROM roms WHERE rom_id = 25135").fetchone()[0]
        finally:
            conn.close()
        assert stored == "run-abc"


class Test021AddRomFsSize:
    """021 — adds the nullable fs_size_bytes column to roms only (#1395)."""

    def test_adds_fs_size_bytes_to_roms_only(self, tmp_path: Path):
        # 021 ALTERs only roms; rom_installs (and every other table) is untouched.
        db_path = str(tmp_path / "romm_sync.db")

        apply_migrations(db_path)

        assert _user_version(db_path) == _SHIPPED_VERSION
        assert "fs_size_bytes" in _columns(db_path, "roms")
        assert "fs_size_bytes" not in _columns(db_path, "rom_installs")

    def test_fs_size_bytes_absent_before_021(self, tmp_path: Path):
        # At v20 the column does not yet exist.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 20)))

        assert _user_version(db_path) == 20
        assert "fs_size_bytes" not in _columns(db_path, "roms")

    def test_existing_row_reads_null_across_the_migration(self, tmp_path: Path):
        # A row seeded before 021 keeps its data and reads NULL for the new column
        # (unknown size), backfilled by the next sync's UPSERT.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 20)))
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO roms (rom_id, platform_slug, name, fs_name, last_synced_at) "
                "VALUES (1, 'snes', 'Game', 'game.sfc', '2026-07-20T10:00:00')"
            )
        finally:
            conn.close()

        assert apply_migrations(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            value = conn.execute("SELECT fs_size_bytes FROM roms WHERE rom_id = 1").fetchone()[0]
        finally:
            conn.close()
        assert value is None

    def test_fs_size_bytes_round_trips(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO roms (rom_id, platform_slug, name, fs_name, last_synced_at, fs_size_bytes) "
                "VALUES (25135, 'dc', 'Game', 'game.gdi', '2026-07-20T06:27:12', 3145728)"
            )
            stored = conn.execute("SELECT fs_size_bytes FROM roms WHERE rom_id = 25135").fetchone()[0]
        finally:
            conn.close()
        assert stored == 3_145_728


class Test022RenameCollectionKindUserToStandard:
    """022 — data migration renaming collection_sync_state.collection_kind 'user' -> 'standard' (#1539)."""

    def test_migrates_user_kind_rows_to_standard(self, tmp_path: Path):
        # Seed a 'user'-kind stamp at v21 (before 022), then apply 022 and assert
        # the kind is rewritten to 'standard' with every other column preserved.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 21)))
        assert _user_version(db_path) == 21

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO collection_sync_state "
                "(collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids) "
                "VALUES ('7', 'user', '2026-01-01T00:00:00', '2026-01-01T00:05:00', 2, '[1, 2]')"
            )
            # A smart-kind stamp must be left untouched by the rename.
            conn.execute(
                "INSERT INTO collection_sync_state "
                "(collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids) "
                "VALUES ('9', 'smart', '2026-01-02T00:00:00', '2026-01-02T00:05:00', 1, '[9]')"
            )
        finally:
            conn.close()

        final_version = apply_migrations(db_path)
        assert final_version == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids "
                "FROM collection_sync_state ORDER BY collection_id"
            ).fetchall()
        finally:
            conn.close()
        # The 'user' stamp is now 'standard' with every column intact; 'smart' is unchanged.
        assert rows == [
            ("7", "standard", "2026-01-01T00:00:00", "2026-01-01T00:05:00", 2, "[1, 2]"),
            ("9", "smart", "2026-01-02T00:00:00", "2026-01-02T00:05:00", 1, "[9]"),
        ]

    def test_absent_before_022(self, tmp_path: Path):
        # At v21 a seeded 'user' stamp is still keyed 'user' — the rename has not run.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path, str(_only_migrations_through(tmp_path, 21)))
        assert _user_version(db_path) == 21

        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO collection_sync_state "
                "(collection_id, collection_kind, updated_at, completed_at, rom_count, member_rom_ids) "
                "VALUES ('7', 'user', '2026-01-01T00:00:00', '2026-01-01T00:05:00', 2, '[1, 2]')"
            )
            kind = conn.execute(
                "SELECT collection_kind FROM collection_sync_state WHERE collection_id = '7'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert kind == "user"

    def test_empty_table_is_noop(self, tmp_path: Path):
        # Fresh install: the stamp table is empty, so the data migration touches nothing.
        db_path = str(tmp_path / "romm_sync.db")
        apply_migrations(db_path)
        assert _user_version(db_path) == _SHIPPED_VERSION

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM collection_sync_state").fetchone()[0]
        finally:
            conn.close()
        assert count == 0


def test_shipped_migrations_dir_resolves_to_real_schema():
    """The default MIGRATIONS_DIR points at the shipped 001_initial.sql."""
    assert (Path(MIGRATIONS_DIR) / "001_initial.sql").is_file()
