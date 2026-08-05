"""Contract test for migration 005 — un-confirming a legacy slot:null ROM (#1276).

Runs the real migration runner against the harness's real SQLite database. A
pre-#1276 ROM confirmed into the legacy no-slot mode (``active_slot=None,
slot_confirmed=1``) is seeded directly (``confirm_slot`` now rejects that shape),
the runner applies 005, and the row's confirmation is flipped back so the
first-sync wizard reappears — while the per-file baselines and the slots
read-model survive untouched (005 only flips ``slot_confirmed``, never deletes).
"""

from __future__ import annotations

import sqlite3

from adapters.sqlite_migrations import apply_migrations
from domain.rom_save_sync_state import FileSyncState, RomSaveSyncState

from ._seed import enable_save_sync, seed_save_state


def _db_path(harness) -> str:
    """The real SQLite database the harness's services read/write."""
    return str(harness.tmp_path / "runtime" / "romm_sync.db")


def _rewind_to_v4(db_path: str) -> None:
    """Rewind the bootstrapped DB to the true pre-005 state (version + schema).

    Bootstrap stamps the DB at the latest version with the full schema. To replay
    the real 005 upgrade path the runner must see a genuine v4 database: the
    version stamp AND the pre-006/007/008/009/010/012/015/016/017/018/019/020/021/023 schema
    (006's play-session outbox table absent and ``note_id`` present, 007's
    ``last_played`` column absent, 008's version-metadata columns absent, 009's
    ``last_session_start_monotonic`` column absent, 010's sibling_group_key index
    absent, 012's platform_sync_state table absent, 015's
    ``applied_launch_options`` column absent, 016's ``cover_source`` column
    absent, 017's ``last_sync_server_hash`` column absent, 018's save-sync
    scalar table under its pre-rename name ``rom_save_states``, 019's
    collection_sync_state table absent, 020's fetch-generation columns
    absent, 021's ``fs_size_bytes`` column absent, and 023's ``launchable``
    column absent) so the sequential 005→…→023 re-run applies cleanly.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("DROP TABLE IF EXISTS rom_playtime_sessions")
        conn.execute("ALTER TABLE rom_playtime ADD COLUMN note_id INTEGER")
        conn.execute("ALTER TABLE rom_playtime DROP COLUMN last_played")
        # Reverse 009 so its ADD COLUMN re-applies instead of duplicating.
        conn.execute("ALTER TABLE rom_playtime DROP COLUMN last_session_start_monotonic")
        # Reverse 010's index before dropping its column (SQLite rejects dropping
        # a column an index still references), so 010 re-creates it cleanly.
        conn.execute("DROP INDEX IF EXISTS idx_roms_sibling_group_key")
        # Reverse 012 so its CREATE TABLE re-applies instead of erroring.
        conn.execute("DROP TABLE IF EXISTS platform_sync_state")
        # Reverse 019 so its CREATE TABLE re-applies instead of erroring.
        conn.execute("DROP TABLE IF EXISTS collection_sync_state")
        # Reverse 008 so its ADD COLUMNs re-apply instead of duplicating.
        for column in ("sibling_group_key", "regions", "languages", "revision", "tags", "is_main_sibling"):
            conn.execute(f"ALTER TABLE roms DROP COLUMN {column}")
        # Reverse 015 so its ADD COLUMN re-applies instead of duplicating.
        conn.execute("ALTER TABLE roms DROP COLUMN applied_launch_options")
        # Reverse 016 so its ADD COLUMN re-applies instead of duplicating.
        conn.execute("ALTER TABLE roms DROP COLUMN cover_source")
        # Reverse 020 so its ADD COLUMN re-applies instead of duplicating. Its
        # platform_sync_state column needs no reversal — 012's whole table is
        # dropped above, so 020 re-adds the column to the re-created table.
        conn.execute("ALTER TABLE roms DROP COLUMN last_fetch_id")
        # Reverse 021 so its ADD COLUMN re-applies instead of duplicating.
        conn.execute("ALTER TABLE roms DROP COLUMN fs_size_bytes")
        # Reverse 023 so its ADD COLUMN re-applies instead of duplicating.
        conn.execute("ALTER TABLE rom_installs DROP COLUMN launchable")
        # Reverse 017 so its ADD COLUMN re-applies instead of duplicating.
        conn.execute("ALTER TABLE rom_save_files DROP COLUMN last_sync_server_hash")
        # Reverse 018 so 005's `UPDATE rom_save_states` finds the table under its
        # pre-rename name and 018 re-applies the rename cleanly.
        conn.execute("ALTER TABLE rom_save_sync_states RENAME TO rom_save_states")
        conn.execute("PRAGMA user_version = 4")
    finally:
        conn.close()


async def test_migration_005_reconfirms_legacy_rom(harness):
    enable_save_sync(harness)

    # A pre-#1276 legacy-confirmed ROM: active_slot=None + slot_confirmed=True,
    # with a per-file baseline and a slots read-model entry that must survive.
    legacy_slots = {"": {"source": "server", "count": 1, "latest_updated_at": "2026-01-01T00:00:00Z"}}
    legacy_state = RomSaveSyncState(
        system="gba",
        slot_confirmed=True,
        active_slot=None,
        slots=dict(legacy_slots),
        files={"pokemon.srm": FileSyncState(tracked_save_id=100, last_sync_hash="abc123")},
    )
    seed_save_state(harness, 42, legacy_state)

    # Precondition: the ROM reads as configured before the migration runs.
    before = await harness.plugin.is_save_tracking_configured(42)
    assert before["configured"] is True

    # Bootstrap already stamped the DB at the latest version, so rewind to just
    # before 005 to exercise the real migration path (005, then 006, then 007).
    db_path = _db_path(harness)
    _rewind_to_v4(db_path)
    apply_migrations(db_path)

    # The legacy confirmation is flipped back; files + slots are untouched.
    with harness.uow_factory() as uow:
        state = uow.rom_save_sync_states.get(42)
    assert state is not None
    assert state.slot_confirmed is False
    assert state.active_slot is None
    assert state.slots == legacy_slots
    assert state.files["pokemon.srm"].last_sync_hash == "abc123"

    # And the wizard reappears — the callable now reports it unconfigured.
    after = await harness.plugin.is_save_tracking_configured(42)
    assert after["configured"] is False
    assert after["active_slot"] is None
