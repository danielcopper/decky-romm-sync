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
from domain.rom_save_state import FileSyncState, RomSaveState

from ._seed import enable_save_sync, seed_save_state


def _db_path(harness) -> str:
    """The real SQLite database the harness's services read/write."""
    return str(harness.tmp_path / "runtime" / "romm_sync.db")


def _set_user_version(db_path: str, version: int) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
    finally:
        conn.close()


async def test_migration_005_reconfirms_legacy_rom(harness):
    enable_save_sync(harness)

    # A pre-#1276 legacy-confirmed ROM: active_slot=None + slot_confirmed=True,
    # with a per-file baseline and a slots read-model entry that must survive.
    legacy_slots = {"": {"source": "server", "count": 1, "latest_updated_at": "2026-01-01T00:00:00Z"}}
    legacy_state = RomSaveState(
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

    # Bootstrap already stamped the DB at the latest version, so re-run the
    # runner from just before 005 to exercise the real migration path.
    db_path = _db_path(harness)
    _set_user_version(db_path, 4)
    apply_migrations(db_path)

    # The legacy confirmation is flipped back; files + slots are untouched.
    with harness.uow_factory() as uow:
        state = uow.rom_save_states.get(42)
    assert state is not None
    assert state.slot_confirmed is False
    assert state.active_slot is None
    assert state.slots == legacy_slots
    assert state.files["pokemon.srm"].last_sync_hash == "abc123"

    # And the wizard reappears — the callable now reports it unconfigured.
    after = await harness.plugin.is_save_tracking_configured(42)
    assert after["configured"] is False
    assert after["active_slot"] is None
