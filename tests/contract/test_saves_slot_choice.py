"""Contract tests for the legacy-bucket refusals — ``confirm_slot_choice``,
``switch_slot``, and ``delete_slot`` — where they reject the retired,
read-only legacy slot.

``confirm_slot_choice`` is driven frontend-shaped per ``src/api/backend.ts``:
positional ``(rom_id, chosen_slot, migrate, migrate_from_slot)`` with the TS arg
types (``string | null`` for the slot, ``boolean`` for migrate, ``string | null``
for the source). These pin the explicit-contract fix: ``migrate`` is a real bool
(no ``"__no_migration__"`` sentinel string) and the default call runs no
migration. All three callables share one rule: the slot-less legacy bucket is no
longer a confirmable / switchable target (#1276) and is read-only — never
deletable — from the plugin (#1478), so an empty / ``None`` slot name returns the
canonical ``invalid_slot_name`` failure and never mutates state or hits the wire.
"""

from __future__ import annotations

import os

from lib.errors import RommConnectionError
from lib.list_result import ErrorCode

from ._seed import enable_save_sync, seed_install, seed_rom, seed_server_save


def _write_local_save(harness, *, system: str = "gba", filename: str = "game.srm", content: bytes = b"x") -> str:
    """Materialize a local save file under the harness saves tree."""
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), system)
    os.makedirs(saves_dir, exist_ok=True)
    path = os.path.join(saves_dir, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


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


async def test_confirm_legacy_migration_content_based_timestamped_filename(harness):
    """The #1498 repro: a timestamped legacy save + a byte-identical local save.

    The old filename-equality migration left the ``default`` slot empty because
    the web-player timestamped legacy filename never matched the canonical local
    name. The content-based migration copies the legacy save's *content* into the
    slot under the canonical name and confirms it; the legacy source is never
    deleted (#1478/#1498).
    """
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, filename="game.srm", content=b"progress")
    seed_server_save(
        harness,
        save_id=700,
        rom_id=42,
        slot=None,
        file_name="game [2026-07-19 13-41-44-611].srm",
    )
    harness.romm.set_server_save_content(700, b"progress")  # byte-identical to local

    result = await harness.plugin.confirm_slot_choice(42, "default", True, None)

    assert result["success"] is True
    assert result["needs_conflict_resolution"] is False
    assert result["migrated"] == 1
    assert result["failed"] == 0
    # Copied into the named slot — never as a slot:null upload.
    upload_calls = [c for c in harness.romm.call_log if c[0] == "upload_save"]
    assert len(upload_calls) == 1
    assert upload_calls[0][2].get("slot") == "default"
    # The legacy source stays in the read-only bucket.
    assert not any(c[0] == "delete_server_saves" for c in harness.romm.call_log)
    assert 700 in harness.romm.saves
    with harness.uow_factory() as uow:
        state = uow.rom_save_sync_states.get(42)
    assert state is not None
    assert state.slot_confirmed is True
    assert state.active_slot == "default"


async def test_confirm_legacy_migration_differing_local_needs_resolution(harness):
    """A differing local save holds the migration: needs_conflict_resolution, nothing confirmed.

    The response carries the ``local_conflict`` reason and a conflict entry (both
    sides' shape) so the wizard can ask; nothing is uploaded/deleted and the slot
    is not confirmed (#1498).
    """
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, filename="game.srm", content=b"my-local-progress")
    seed_server_save(
        harness,
        save_id=701,
        rom_id=42,
        slot=None,
        file_name="game [2026-07-19 13-41-44-611].srm",
    )
    harness.romm.set_server_save_content(701, b"server-progress")

    result = await harness.plugin.confirm_slot_choice(42, "default", True, None)

    assert result["success"] is False
    assert result["needs_conflict_resolution"] is True
    assert result["reason"] == "local_conflict"
    assert isinstance(result["message"], str)
    conflicts = result["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["filename"] == "game.srm"
    assert conflicts[0]["server_save_id"] == 701
    # Nothing migrated, nothing deleted, slot not confirmed.
    assert not any(c[0] == "upload_save" for c in harness.romm.call_log)
    assert not any(c[0] == "delete_server_saves" for c in harness.romm.call_log)
    with harness.uow_factory() as uow:
        state = uow.rom_save_sync_states.get(42)
    assert state is None


async def test_confirm_legacy_migration_use_server_resolves_conflict(harness):
    """ "Use the server save" resolves a differing-local conflict: quarantine local, copy server in."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    local_path = _write_local_save(harness, filename="game.srm", content=b"my-local-progress")
    seed_server_save(
        harness,
        save_id=702,
        rom_id=42,
        slot=None,
        file_name="game [2026-07-19 13-41-44-611].srm",
    )
    harness.romm.set_server_save_content(702, b"server-progress")

    result = await harness.plugin.confirm_slot_choice(42, "default", True, None, True)

    assert result["success"] is True
    assert result["migrated"] == 1
    # The local file now holds the server content; the original is backed up.
    with open(local_path, "rb") as fh:
        assert fh.read() == b"server-progress"
    backup_dir = os.path.join(os.path.dirname(local_path), ".romm-backup")
    backups = [n for n in os.listdir(backup_dir) if n.startswith("game")]
    assert len(backups) == 1
    upload_calls = [c for c in harness.romm.call_log if c[0] == "upload_save"]
    assert len(upload_calls) == 1
    assert upload_calls[0][2].get("slot") == "default"
    with harness.uow_factory() as uow:
        state = uow.rom_save_sync_states.get(42)
    assert state is not None
    assert state.slot_confirmed is True


async def test_confirm_legacy_migration_server_unreachable_holds_wizard(harness):
    """A transient server failure before the apply phase returns the canonical failure and confirms nothing.

    The wholesale (pre-apply) failure must NOT silently confirm-and-close — the
    wizard stays open on the message so the user can retry Track (#1498 review).
    """
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, filename="game.srm", content=b"progress")
    seed_server_save(harness, save_id=703, rom_id=42, slot=None, file_name="game [ts].srm")
    harness.romm.list_saves_side_effect = RommConnectionError("offline")

    result = await harness.plugin.confirm_slot_choice(42, "default", True, None)

    assert result["success"] is False
    assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
    assert result["needs_conflict_resolution"] is False
    assert isinstance(result["message"], str)
    # Nothing confirmed, nothing uploaded — no half-state.
    with harness.uow_factory() as uow:
        state = uow.rom_save_sync_states.get(42)
    assert state is None
    assert not any(c[0] == "upload_save" for c in harness.romm.call_log)


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


# ── delete_slot ───────────────────────────────────────────────────────────


async def test_delete_slot_legacy_rejected(harness):
    """delete_slot("") is refused — the legacy bucket is read-only (#1478).

    Driven frontend-shaped per ``src/api/backend.ts`` (``deleteSlot`` is
    ``callable<[number, string], …>``). The callable returns the canonical
    ``invalid_slot_name`` failure before any server I/O, so the game's
    web-player bucket can never be torn down from the plugin.
    """
    enable_save_sync(harness)
    seed_rom(harness, 42)

    result = await harness.plugin.delete_slot(42, "")

    assert result["success"] is False
    assert result["reason"] == "invalid_slot_name"
    assert isinstance(result["message"], str)
    # No wire traffic — not even a read to inspect the bucket.
    assert not any(c[0] == "delete_server_saves" for c in harness.romm.call_log)
    assert not any(c[0] == "list_saves" for c in harness.romm.call_log)
