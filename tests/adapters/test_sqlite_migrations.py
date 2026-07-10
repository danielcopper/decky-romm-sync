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
# + 003_unique_shortcut_app_id + 004_add_selected_disc
# + 005_unconfirm_legacy_slot_confirmations + 006_native_play_sessions
# + 007_add_last_played + 008_add_version_metadata
# + 009_add_last_session_start_monotonic + 010_add_sibling_group_key_index
# + 011_rekey_sibling_group_key).
_SHIPPED_VERSION = 11

# Tables after every shipped migration: the v1 set plus 006's play-session outbox.
_SHIPPED_TABLES = _V1_TABLES | {"rom_playtime_sessions"}


class TestEmptyDatabase:
    """Empty DB (user_version 0) -> the full shipped schema is applied."""

    def test_applies_real_schema(self, tmp_path: Path):
        db_path = str(tmp_path / "romm_sync.db")

        final_version = apply_migrations(db_path)

        assert final_version == _SHIPPED_VERSION
        assert _user_version(db_path) == _SHIPPED_VERSION
        # 002/004 ALTER roms, 003 adds an index; 006 adds the play-session outbox
        # table — the only table added past v1.
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
            confirmations = dict(
                conn.execute("SELECT rom_id, slot_confirmed FROM rom_save_states ORDER BY rom_id").fetchall()
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


def test_shipped_migrations_dir_resolves_to_real_schema():
    """The default MIGRATIONS_DIR points at the shipped 001_initial.sql."""
    assert (Path(MIGRATIONS_DIR) / "001_initial.sql").is_file()
