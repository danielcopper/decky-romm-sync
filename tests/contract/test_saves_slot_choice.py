"""Contract tests for ``confirm_slot_choice`` — the #1004/#1008 wire contract.

Driven frontend-shaped per ``src/api/backend.ts``: positional
``(rom_id, chosen_slot, migrate, migrate_from_slot)`` with the TS arg types
(``string | null`` for the slot, ``boolean`` for migrate, ``string | null``
for the source). These pin the explicit-contract fix: ``migrate`` is a real
bool (no ``"__no_migration__"`` sentinel string), ``chosen_slot=None`` confirms
the legacy slot, and the default call runs no migration.
"""

from __future__ import annotations

from ._seed import enable_save_sync, seed_rom

# ── confirm_slot_choice ───────────────────────────────────────────────────


async def test_confirm_named_slot_no_migration(harness):
    """Named slot, migrate=False: success, slot confirmed, no migration delete fired."""
    enable_save_sync(harness)
    seed_rom(harness, 42)

    result = await harness.plugin.confirm_slot_choice(42, "main", False, None)

    assert result["success"] is True
    assert result["needs_conflict_resolution"] is False
    assert isinstance(result["message"], str)
    # Post-state: the slot is confirmed and active.
    with harness.uow_factory() as uow:
        state = uow.rom_save_states.get(42)
    assert state is not None
    assert state.slot_confirmed is True
    assert state.active_slot == "main"
    # No migration → no upload / no delete on the server edge.
    assert not any(c[0] == "upload_save" for c in harness.romm.call_log)
    assert not any(c[0] == "delete_server_saves" for c in harness.romm.call_log)


async def test_confirm_legacy_slot_none_rejected(harness):
    """chosen_slot=None is rejected — legacy slot:null confirmation is retired (#1276).

    The no-slot mode can no longer be confirmed as a target: the callable returns
    the canonical ``invalid_slot_name`` failure and never persists a confirmed
    legacy state.
    """
    enable_save_sync(harness)
    seed_rom(harness, 42)

    result = await harness.plugin.confirm_slot_choice(42, None, False, None)

    assert result["success"] is False
    assert result["reason"] == "invalid_slot_name"
    assert isinstance(result["message"], str)
    with harness.uow_factory() as uow:
        state = uow.rom_save_states.get(42)
    # Nothing confirmed — no rom_save_states row written.
    assert state is None
    # No migration delete fired.
    assert not any(c[0] == "delete_server_saves" for c in harness.romm.call_log)
