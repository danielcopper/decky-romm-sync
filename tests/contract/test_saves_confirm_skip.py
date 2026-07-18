"""Contract tests for the post-upload confirm-download skip (#1458) and the
dedup-to-non-head guard (#1482).

Driven frontend-shaped through the real ``Plugin`` / ``bootstrap`` harness. A
normal automatic upload leaves this device ``is_current`` via ``add_save``'s own
DeviceSaveSync upsert, so the sync engine skips the redundant
``POST /saves/{id}/downloaded`` ack (#1458). When ``add_save``'s content-dedup
early-returns a matching save that is *not* the slot head (an older version while
a newer, different head still leads), the upload never became the head, so the
guard routes the response through the 409 backstop and surfaces a conflict rather
than a false ``synced`` (#1482) — no ack fires on that path. The #1458 ack itself
(fail-open on a not-provably-current dedup response) is exercised at the unit tier
(``TestConfirmUploadSyncDiscriminator``), where the guard is inert.

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


async def test_dedup_to_non_head_surfaces_conflict(harness):
    """A POST that content-dedups to an OLDER save while a newer, different head
    still leads the slot (#1482): no new version is created and the foreign head
    stays authoritative, so recording the dedup response as ``synced`` would be a
    silent no-op cross-device. The guard routes it through the 409 backstop and
    surfaces a conflict on the head instead — no synced count, no confirm ack, DB
    baseline untouched."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, system="gba", content=b"reverted to older content", filename="game.srm")

    # Head we are current on (branch 4 → Upload) and an older sibling version we
    # never synced, both in the slot. The local edit diverged from the baseline,
    # so the matrix POSTs — and the POST dedups against the older, non-head version.
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

    harness.romm.arm_add_save_dedup(400)  # the POST dedups to the OLDER, non-head save

    result = await harness.plugin.sync_rom_saves(42)

    assert result["success"] is True
    # Not a false "synced" — the true divergence surfaced as a conflict on the head.
    assert result["synced"] == 0
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["server_save_id"] == 500
    upload_calls = [c for c in harness.romm.call_log if c[0] == "upload_save"]
    assert len(upload_calls) == 1
    # No confirm ack stamped currency on the non-head response…
    assert not any(c[0] == "confirm_download" for c in harness.romm.call_log)
    # …and the DB baseline was never rewritten onto the dedup response (400).
    with harness.uow_factory() as uow:
        reloaded = uow.rom_save_states.get(42)
    assert reloaded is not None
    assert reloaded.files["game.srm"].tracked_save_id == 500
