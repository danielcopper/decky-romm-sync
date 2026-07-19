"""Contract test for RomM's per-device sync-disabled policy stop (#1489).

Driven frontend-shaped through the real ``Plugin`` / ``bootstrap`` harness: when
RomM has save sync disabled for this device, the negotiate 400 surfaces as a
``device_sync_disabled`` policy stop carrying the canonical failure shape, and no
upload is attempted (the run aborts at the negotiate step, before the matrix).
"""

from __future__ import annotations

import os

from domain.rom_save_state import RomSaveState

from ._seed import enable_save_sync, seed_install, seed_save_state


def _write_local_save(harness, *, system: str, content: bytes, filename: str) -> str:
    """Write a real local save under the resolved saves dir; return its path."""
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), system)
    os.makedirs(saves_dir, exist_ok=True)
    path = os.path.join(saves_dir, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


async def test_sync_rom_saves_device_sync_disabled_returns_policy_failure(harness):
    """A confirmed-slot ROM whose device has sync disabled server-side stops with the
    ``device_sync_disabled`` reason and the canonical failure shape — no upload runs."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, system="gba", content=b"local save bytes", filename="game.srm")
    # A confirmed named slot makes the run open a real negotiate session, which the
    # server rejects with the per-device sync-disabled 400.
    seed_save_state(harness, 42, RomSaveState(active_slot="default", slot_confirmed=True, system="gba"))
    harness.romm.negotiate_sync_disabled = True

    result = await harness.plugin.sync_rom_saves(42)

    assert result["success"] is False
    assert result["reason"] == "device_sync_disabled"
    assert isinstance(result["message"], str) and result["message"]
    assert result["synced"] == 0
    # Canonical failure shape — the forbidden legacy keys are absent.
    assert "error" not in result
    assert "error_code" not in result
    # The run aborted at negotiate, before the upload matrix.
    assert not any(c[0] == "upload_save" for c in harness.romm.call_log)
