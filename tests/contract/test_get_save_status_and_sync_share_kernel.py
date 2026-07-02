"""Contract test: ``get_save_status`` and ``sync_rom_saves`` share one kernel (ADR-0017).

Phase D collapsed the legacy/negotiate sync fork: every ROM now decides via the
local ``compute_sync_action`` matrix, the same kernel ``get_save_status`` reads.
Before the collapse, a confirmed non-legacy ROM's ``sync_rom_saves`` handed
detection to RomM's ``negotiate`` operation list while ``get_save_status`` used
the matrix — so the two could disagree.

This is the regression guard for the divergence the phase closes. It seeds the
branch-6d adopt case (``domain/sync_action._decide_when_no_entry``): a server save
whose ``content_hash`` is byte-identical to the local file, with NO
``device_syncs`` entry for this device. The matrix verdict is
``Skip(adopt_baseline=True)`` — nothing to transfer, no conflict, adopt the local
hash as the baseline. Under the old fork the ``FakeRommApi.negotiate`` empty plan
would have dispatched nothing (no baseline adopted), diverging from the status
view. Now both derive from the one kernel, so they agree: status reports
``synced`` / no conflict, sync transfers nothing / raises no conflict, and BOTH
adopt the baseline.
"""

from __future__ import annotations

import hashlib
import os

from domain.rom_save_state import RomSaveState

from ._seed import enable_save_sync, seed_install, seed_save_state, seed_server_save


def _write_local_save(harness, *, system: str, content: bytes, filename: str) -> str:
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), system)
    os.makedirs(saves_dir, exist_ok=True)
    path = os.path.join(saves_dir, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def _read_persisted_baseline(harness, rom_id: int, filename: str) -> str | None:
    with harness.uow_factory() as uow:
        state = uow.rom_save_states.get(rom_id)
    assert state is not None
    file_state = state.files.get(filename)
    return file_state.last_sync_hash if file_state else None


async def test_get_save_status_and_sync_share_kernel(harness):
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    content = b"shared bytes"
    _write_local_save(harness, system="gba", content=content, filename="game.srm")
    local_hash = hashlib.md5(content).hexdigest()

    # Confirmed non-legacy slot, no per-file baseline yet (never synced this file).
    seed_save_state(harness, 42, RomSaveState(active_slot="default", slot_confirmed=True, system="gba"))

    # A server save in the slot with content_hash == local_hash and NO device_syncs
    # entry for this device (no ledger row) → branch 6 adopt case.
    entry = seed_server_save(harness, save_id=100, rom_id=42, slot="default", file_name="game.srm")
    entry["content_hash"] = local_hash

    # 1. The status view: the matrix says "synced", no conflict surfaced.
    status = await harness.plugin.get_save_status(42)
    files = {f["filename"]: f for f in status["files"]}
    assert "game.srm" in files
    assert files["game.srm"]["status"] == "synced"
    assert status["conflicts"] == []

    # 2. The sync dispatch derives the same verdict from the same kernel: nothing
    #    transferred, no conflict, no error.
    result = await harness.plugin.sync_rom_saves(42)
    assert result["success"] is True
    assert result["synced"] == 0
    assert result["conflicts"] == []
    assert result["errors"] == []

    # 3. The shared kernel effect: BOTH read and write adopt the local hash as the
    #    baseline. The old negotiate op-dispatch (empty plan) would have left it
    #    unadopted — the divergence this phase closes.
    assert _read_persisted_baseline(harness, 42, "game.srm") == local_hash

    # Neither path fell back to an upload or a download — the byte-identical save is
    # adopted in place, not re-POSTed or re-fetched.
    assert not any(c[0] == "upload_save" for c in harness.romm.call_log)
    assert not any(c[0] == "download_save_content" for c in harness.romm.call_log)
