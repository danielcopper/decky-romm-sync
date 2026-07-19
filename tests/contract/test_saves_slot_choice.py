"""Contract tests for the slot-choice mutations — ``confirm_slot_choice`` and
``switch_slot`` — where they reject the retired legacy slot.

``confirm_slot_choice`` is driven frontend-shaped per ``src/api/backend.ts``:
positional ``(rom_id, chosen_slot, migrate, migrate_from_slot)`` with the TS arg
types (``string | null`` for the slot, ``boolean`` for migrate, ``string | null``
for the source). These pin the explicit-contract fix: ``migrate`` is a real bool
(no ``"__no_migration__"`` sentinel string) and the default call runs no
migration. Both callables share one rule: the slot-less legacy bucket is no
longer a confirmable / switchable target (#1276), so an empty / ``None`` slot
name returns the canonical ``invalid_slot_name`` failure and never mutates state.
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
        state = uow.rom_save_sync_states.get(42)
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
        state = uow.rom_save_sync_states.get(42)
    # Nothing confirmed — no rom_save_sync_states row written.
    assert state is None
    # No migration delete fired.
    assert not any(c[0] == "delete_server_saves" for c in harness.romm.call_log)


# ── switch_slot ───────────────────────────────────────────────────────────


async def test_switch_slot_empty_rejected(harness):
    """switch_slot("") is rejected — the legacy bucket is not a switch target (#1276).

    Driven frontend-shaped per ``src/api/backend.ts`` (``switchSlot`` is
    ``callable<[number, string], …>``). The callable returns the canonical
    ``invalid_slot_name`` failure and never switches the ROM into legacy mode.
    """
    enable_save_sync(harness)
    seed_rom(harness, 42)

    result = await harness.plugin.switch_slot(42, "")

    assert result["success"] is False
    assert result["reason"] == "invalid_slot_name"
    assert isinstance(result["message"], str)
    # No state written — the ROM is not switched into legacy mode.
    with harness.uow_factory() as uow:
        state = uow.rom_save_sync_states.get(42)
    assert state is None
