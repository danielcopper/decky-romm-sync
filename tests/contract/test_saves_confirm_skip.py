"""Contract tests for the post-upload confirm-download skip (#1458).

Driven frontend-shaped through the real ``Plugin`` / ``bootstrap`` harness. A
normal automatic upload leaves this device ``is_current`` via ``add_save``'s own
DeviceSaveSync upsert, so the sync engine skips the redundant
``POST /saves/{id}/downloaded`` ack. The one path that still needs the ack is
``add_save``'s content-dedup early-return (a byte-identical POST that returns the
matching save before the upsert, reporting ``is_current=false``).

Both scenarios stay on the legacy ``compute_sync_action`` matrix path
(``active_slot`` set, ``slot_confirmed`` unset) so the POST is actually
dispatched, matching the ``test_saves_upload_409`` fixtures.
"""

from __future__ import annotations

import hashlib
import os

from domain.rom_save_state import RomSaveState

from ._seed import enable_save_sync, seed_install, seed_save_state, seed_server_save


def _write_local_save(harness, *, system: str, content: bytes, filename: str) -> str:
    """Write a real local save under the resolved saves dir; return its path."""
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), system)
    os.makedirs(saves_dir, exist_ok=True)
    path = os.path.join(saves_dir, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


async def test_normal_upload_skips_confirm_download(harness):
    """A fresh upload into an empty slot: add_save upserts our sync row, so the
    engine skips the confirm round-trip yet we still read back is_current."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, system="gba", content=b"first save", filename="game.srm")
    seed_save_state(harness, 42, RomSaveState(active_slot="default", system="gba"))

    result = await harness.plugin.sync_rom_saves(42)

    assert result["success"] is True
    assert result["synced"] == 1
    assert result["conflicts"] == []
    # Exactly one POST, overwrite=false, and NO confirm round-trip after it.
    upload_calls = [c for c in harness.romm.call_log if c[0] == "upload_save"]
    assert len(upload_calls) == 1
    assert upload_calls[0][2]["overwrite"] is False
    assert not any(c[0] == "confirm_download" for c in harness.romm.call_log)
    # ...yet the device is genuinely current on the new save (add_save recorded
    # the DeviceSaveSync row itself, so skipping the ack lost nothing).
    listed = harness.romm.list_saves(42, device_id="device-1")
    our_sync = next(ds for ds in listed[0]["device_syncs"] if ds["device_id"] == "device-1")
    assert our_sync["is_current"] is True


async def test_dedup_response_still_confirms_download(harness):
    """A byte-identical re-upload hits add_save's content-dedup early-return: the
    response reports is_current=false, so the confirm ack stays load-bearing."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, system="gba", content=b"reverted to older content", filename="game.srm")

    # Head we are current on (branch 4 → Upload) and an older sibling version we
    # never synced, both in the slot. The local edit diverged from the baseline,
    # so the matrix POSTs — and the POST dedups against the older version.
    seed_server_save(
        harness, save_id=500, rom_id=42, slot="default", file_name="game.srm", updated_at="2026-03-02T00:00:00Z"
    )
    harness.romm.stage_device_sync(500, "device-1", "2026-03-02T00:00:00Z")
    seed_server_save(
        harness, save_id=400, rom_id=42, slot="default", file_name="game.srm", updated_at="2026-03-01T00:00:00Z"
    )

    state = RomSaveState(active_slot="default", system="gba")
    state.adopt_baseline("game.srm", tracked_save_id=500, last_sync_hash=hashlib.md5(b"head content").hexdigest())
    seed_save_state(harness, 42, state)

    harness.romm.arm_add_save_dedup(400)

    result = await harness.plugin.sync_rom_saves(42)

    assert result["success"] is True
    assert result["synced"] == 1
    assert result["conflicts"] == []
    upload_calls = [c for c in harness.romm.call_log if c[0] == "upload_save"]
    assert len(upload_calls) == 1
    # The dedup response was NOT provably current, so the confirm ack fired.
    confirm_calls = [c for c in harness.romm.call_log if c[0] == "confirm_download"]
    assert confirm_calls == [("confirm_download", (400, "device-1"), {})]
