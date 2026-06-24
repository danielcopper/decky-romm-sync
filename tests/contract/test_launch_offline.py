"""Contract tests for the offline launch-path callables.

Drives the new launch-gate funnel callables exactly as the frontend does
(``src/api/backend.ts``), asserting only the response SHAPE + behaviour:

* ``check_local_drift(rom_id)`` → ``{"drifted": bool, "rom_id": int}`` — the
  purely-local drift probe the offline path uses to warn the user that an
  out-of-band local change would be overwritten the next time sync succeeds.
* ``probe_reachability()`` → ``{"online": bool}`` — a fresh, version-free
  heartbeat at the launch decision point.
* ``refresh_save_status(rom_id)`` → ``{"success": True}`` — fire-and-forget
  trigger for the background ``save_status_updated`` emit (the F7 fix).

The manifest-parity contract test (``tests/contract/test_callable_manifest.py``)
asserts all three names + arities match the frontend declarations; these tests
cover the runtime shape + behaviour.
"""

from __future__ import annotations

import asyncio
import os

from domain.rom_save_state import RomSaveState

from ._seed import seed_install, seed_save_state


def _write_local_save(harness, *, system: str, content: bytes, filename: str = "game.srm") -> str:
    """Write a real local save file under the resolved saves dir; return its path.

    Mirrors where ``RomInfoService.find_save_files`` looks for a default-sort
    install: ``<saves_path>/<content_dir>/<rom_name><ext>``. ``content_dir`` is
    the ROM's parent folder name (``seed_install`` lays the ROM under
    ``…/roms/<system>/<file>``), so it equals *system* here.
    """
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), system)
    os.makedirs(saves_dir, exist_ok=True)
    path = os.path.join(saves_dir, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


# ── check_local_drift ─────────────────────────────────────────────────────


async def test_check_local_drift_not_installed_shape(harness):
    """Not installed → {"drifted": False, "rom_id"} (no local files to probe)."""
    result = await harness.plugin.check_local_drift(99)

    assert result == {"drifted": False, "rom_id": 99}


async def test_check_local_drift_matching_hash_not_drifted(harness):
    """Local file whose content matches its persisted baseline → drifted False."""
    seed_install(harness, 1, system="gba", file_name="game.gba")
    _write_local_save(harness, system="gba", content=b"save-bytes")
    # MD5 of b"save-bytes".
    import hashlib

    baseline = hashlib.md5(b"save-bytes").hexdigest()
    state = RomSaveState()
    state.adopt_baseline("game.srm", tracked_save_id=10, last_sync_hash=baseline)
    seed_save_state(harness, 1, state, platform_slug="gba")

    result = await harness.plugin.check_local_drift(1)

    assert result == {"drifted": False, "rom_id": 1}


async def test_check_local_drift_changed_content_drifted(harness):
    """Local file whose content diverges from its baseline → drifted True."""
    seed_install(harness, 2, system="gba", file_name="game.gba")
    _write_local_save(harness, system="gba", content=b"new-bytes-on-disk")
    state = RomSaveState()
    state.adopt_baseline("game.srm", tracked_save_id=11, last_sync_hash="stale-baseline-hash")
    seed_save_state(harness, 2, state, platform_slug="gba")

    result = await harness.plugin.check_local_drift(2)

    assert result == {"drifted": True, "rom_id": 2}


# ── probe_reachability ────────────────────────────────────────────────────


async def test_probe_reachability_online_shape(harness):
    """Healthy heartbeat → {"online": True}."""
    result = await harness.plugin.probe_reachability()

    assert result == {"online": True}


async def test_probe_reachability_offline_shape(harness):
    """Heartbeat failure → {"online": False}, never raises."""
    harness.romm.heartbeat_side_effect = RuntimeError("connection refused")

    result = await harness.plugin.probe_reachability()

    assert result == {"online": False}


# ── refresh_save_status ───────────────────────────────────────────────────


async def test_refresh_save_status_returns_success_and_schedules_emit(harness):
    """Returns {"success": True} immediately and schedules the background check.

    The background task runs ``check_save_status_background`` which emits
    ``save_status_updated``; we let the loop drain and assert the emit fired.
    """
    seed_install(harness, 3, system="gba", file_name="game.gba")
    harness.plugin.settings["save_sync_enabled"] = True

    result = await harness.plugin.refresh_save_status(3)

    assert result == {"success": True}
    # Drain the fire-and-forget task scheduled via loop.create_task.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        await task
    emitted = [call.args[0] for call in harness.emit.call_args_list]
    assert "save_status_updated" in emitted
