"""Wire-level contracts for explicit vanished-ROM cleanup."""

from __future__ import annotations

import pytest

from domain.platform_sync_state import PlatformSyncState
from domain.rom import Rom
from domain.sync_state import SyncState
from lib.errors import RommNotFoundError


def _seed_bulk_candidate(harness, rom_id: int = 41) -> None:
    rom = Rom.synced(
        rom_id=rom_id,
        platform_slug="gba",
        name="Removed Game",
        fs_name="Removed Game.gba",
        shortcut_app_id=None,
        synced_at="2026-01-01T00:00:00",
    )
    rom.record_fetch_generation("older-fetch")
    with harness.uow_factory() as uow:
        uow.roms.save(rom)
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(
                platform_slug="gba",
                at="2026-01-02T00:00:00",
                rom_count=1,
                fetch_id="completed-fetch",
            )
        )


def _preview_request(preview_id=None):
    return {"scope": "bulk", "rom_id": None, "preview_id": preview_id, "offset": 0, "limit": 50}


async def test_preview_is_local_paged_and_frontend_shaped(harness):
    _seed_bulk_candidate(harness)

    result = await harness.plugin.get_prune_preview(_preview_request())

    assert set(result) == {
        "success",
        "preview_id",
        "scope",
        "items",
        "offset",
        "limit",
        "total",
        "free_bytes",
        "recovery_root",
    }
    assert result["success"] is True
    assert result["scope"] == "bulk"
    assert result["total"] == 1
    assert result["items"][0]["rom_id"] == 41
    assert result["items"][0]["installed"] is False
    assert result["recovery_root"].endswith("-recovery")
    assert harness.romm.call_log == []


@pytest.mark.parametrize("state", [SyncState.RUNNING, SyncState.CANCELLING])
async def test_preview_refuses_active_sync_with_canonical_shape(harness, state):
    harness.plugin._sync_service._box.sync_state = state

    result = await harness.plugin.get_prune_preview(_preview_request())

    assert set(result) == {"success", "reason", "message"}
    assert result["success"] is False
    assert result["reason"] == "sync_active"
    assert result["message"]


async def test_unbound_exact_404_cleanup_deletes_real_aggregate_and_emits_completion(harness):
    _seed_bulk_candidate(harness)
    harness.romm.get_rom_once_side_effect_by_id[41] = RommNotFoundError("gone")
    preview = await harness.plugin.get_prune_preview(_preview_request())

    started = await harness.plugin.start_prune(
        {
            "preview_id": preview["preview_id"],
            "confirmed": True,
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": True,
            "create_recovery_bundle": False,
            "include_installed_rom_ids": [],
        }
    )

    assert set(started) == {"success", "run_id", "status"}
    assert started["success"] is True
    assert started["status"] == "running"
    task = harness.plugin._prune_service._task
    assert task is not None
    await task

    with harness.uow_factory() as uow:
        assert uow.roms.get(41) is None
    probes = [entry for entry in harness.romm.call_log if entry[0] == "get_rom_once"]
    assert probes == [("get_rom_once", (41,), {})] * 3
    complete_calls = [item for item in harness.emit.await_args_list if item.args[0] == "prune_complete"]
    assert len(complete_calls) == 1
    payload = complete_calls[0].args[1]
    assert payload["success"] is True
    assert payload["partial"] is False
    assert payload["removed_rom_ids"] == [41]
    assert payload["results"][0]["status"] == "removed"


async def test_action_report_rejects_stale_token_with_canonical_shape(harness):
    result = await harness.plugin.report_prune_action(
        {
            "phase": "complete",
            "run_id": "old-run",
            "action_token": "old-token",
            "success": True,
            "message": "late",
        }
    )

    assert result == {
        "success": False,
        "reason": "stale_action",
        "message": "This cleanup action token is no longer active.",
    }


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("start_sync", ()),
        ("start_download", (41,)),
        ("migrate_retrodeck_files", (None,)),
        ("sync_rom_saves", (41,)),
        ("switch_version", (0x80000001, 41, False)),
    ],
)
async def test_prune_claim_reciprocally_blocks_conflicting_callable_entries(harness, method, args):
    harness.plugin._prune_service._starting = True

    result = await getattr(harness.plugin, method)(*args)

    assert result["success"] is False
    assert result["reason"] == "prune_active"
    assert result["message"]
