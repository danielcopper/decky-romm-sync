"""Wire-level contracts for explicit vanished-ROM cleanup."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from domain.platform_sync_state import PlatformSyncState
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_save_sync_state import FileSyncState, RomSaveSyncState
from domain.sync_state import SyncState
from domain.version_metadata import VersionMetadata
from lib.errors import RommNotFoundError

CONTROL_ROM_ID = 900041


def _seed_bulk_candidate(harness, rom_id: int = 41, *, control: bool = True) -> None:
    rom = Rom.synced(
        rom_id=rom_id,
        platform_slug="gba",
        name="Removed Game",
        fs_name="Removed Game.gba",
        shortcut_app_id=None,
        synced_at="2026-01-01T00:00:00",
    )
    rom.record_fetch_generation("older-fetch")
    control_rom = Rom.synced(
        rom_id=CONTROL_ROM_ID,
        platform_slug="snes",
        name="Kept Game",
        fs_name="Kept Game.sfc",
        shortcut_app_id=None,
        synced_at="2026-01-01T00:00:00",
    )
    control_rom.record_fetch_generation("snes-fetch")
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
        if control:
            # A row the server is known to have served, on its own platform so it
            # is neither a candidate nor part of any gba-scoped assertion. Cleanup
            # asks it whether the ROM endpoint answers before trusting a 404.
            uow.roms.save(control_rom)
            uow.platform_sync_state.save(
                PlatformSyncState.stamp(
                    platform_slug="snes",
                    at="2026-01-02T00:00:00",
                    rom_count=1,
                    fetch_id="snes-fetch",
                )
            )


def _seed_installed_bulk_candidate(harness, rom_id: int = 41) -> Path:
    _seed_bulk_candidate(harness, rom_id)
    rom_path = Path(harness.retrodeck_paths.roms_path()) / "gba" / "Removed Game.gba"
    rom_path.parent.mkdir(parents=True, exist_ok=True)
    rom_path.write_bytes(b"installed rom")
    with harness.uow_factory() as uow:
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=str(rom_path),
                rom_dir=None,
                platform_slug="gba",
                system="gba",
                installed_at="2026-01-01T00:00:00",
            )
        )
    return rom_path


def _preview_request(preview_id=None):
    return {"scope": "bulk", "rom_id": None, "preview_id": preview_id, "offset": 0, "limit": 50}


def _selection_request(preview_id, selection_id, rom_ids, final):
    return {"preview_id": preview_id, "selection_id": selection_id, "rom_ids": rom_ids, "final": final}


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
        "candidate_total",
        "free_bytes",
        "recovery_root",
    }
    assert result["success"] is True
    assert result["scope"] == "bulk"
    assert result["total"] == 1
    assert result["candidate_total"] == 1
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
    # Three re-proof rounds. Nothing answered live in any of them, so each also
    # asks one control whether the ROM endpoint answers at all before its 404
    # is honoured — one extra request per round, never per candidate.
    assert probes == [("get_rom_once", (41,), {}), ("get_rom_once", (CONTROL_ROM_ID,), {})] * 3
    complete_calls = [item for item in harness.emit.await_args_list if item.args[0] == "prune_complete"]
    assert len(complete_calls) == 1
    payload = complete_calls[0].args[1]
    assert payload["success"] is True
    assert payload["partial"] is False
    assert payload["removed_rom_ids"] == [41]
    assert payload["results"][0]["status"] == "removed"


async def test_a_misrouted_404_removes_nothing_over_the_real_wire(harness):
    """End to end: 404s that no control corroborates never reach the aggregate."""
    _seed_bulk_candidate(harness)
    harness.romm.get_rom_once_side_effect_by_id[41] = RommNotFoundError("misrouted")
    harness.romm.get_rom_once_side_effect_by_id[CONTROL_ROM_ID] = RommNotFoundError("misrouted")
    preview = await harness.plugin.get_prune_preview(_preview_request())

    await harness.plugin.start_prune(
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
    task = harness.plugin._prune_service._task
    assert task is not None
    await task

    with harness.uow_factory() as uow:
        assert uow.roms.get(41) is not None
    payload = [item for item in harness.emit.await_args_list if item.args[0] == "prune_complete"][-1].args[1]
    assert payload["removed_rom_ids"] == []
    assert payload["results"][0]["status"] == "skipped"
    assert payload["results"][0]["reason"] == "unconfirmed_server"


async def test_cancel_prune_stops_the_running_run_over_the_real_wire(harness):
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

    result = await harness.plugin.cancel_prune(started["run_id"])

    assert set(result) == {"success", "run_id", "already_cancelling", "message"}
    assert result["success"] is True
    assert result["run_id"] == started["run_id"]
    assert result["already_cancelling"] is False
    task = harness.plugin._prune_service._task
    assert task is not None
    with pytest.raises(asyncio.CancelledError):
        await task
    # The claim is released, so cleanup is reachable again immediately.
    assert harness.plugin._prune_service.is_active() is False


@pytest.mark.parametrize("run_id", ["no-such-run", "", None])
async def test_cancel_prune_refuses_an_unknown_run_with_the_canonical_shape(harness, run_id):
    result = await harness.plugin.cancel_prune(run_id)

    assert set(result) == {"success", "reason", "message"}
    assert result["success"] is False
    assert result["reason"] in {"stale_run", "invalid_run_id"}
    assert result["message"]


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
        ("test_connection", ()),
        ("connect_with_credentials", ("https://server.example", "user", "pass", None)),
        ("connect_with_token", ("https://server.example", "token", None)),
        ("connect_with_pairing_code", ("https://server.example", "code", None)),
        ("sign_out", ()),
        ("save_server_url", ("https://server.example", None)),
        ("start_download", (41,)),
        ("adopt_existing_rom", (41,)),
        ("migrate_retrodeck_files", (None,)),
        ("sync_rom_saves", (41,)),
        ("switch_version", (0x80000001, 41, False)),
        ("remove_rom", (41,)),
        ("uninstall_all_roms", ()),
        ("get_rom_relaunch_options", (41,)),
        ("get_installed_relaunch_options", ()),
        ("report_unit_results", ({}, "run", "unit", 0)),
        ("report_removal_results", ([], None)),
        ("reconcile_shortcuts", ([],)),
        ("refresh_save_status", (41,)),
        ("get_save_status", (41,)),
        ("get_save_slots", (41,)),
        ("record_session_start", (41,)),
        ("reconcile_playtime", (41,)),
        ("fetch_cover_base64", (41,)),
        ("get_sgdb_artwork_base64", (41, 0)),
        ("apply_sgdb_game_id", (41, 7)),
        ("save_shortcut_icon", (0x80000001, "")),
        ("clear_sync_cache", ()),
        ("apply_steam_input_setting", ()),
        ("set_system_core", ("n64", "")),
        ("set_game_core", (41, "core")),
        ("clear_game_core", (41,)),
        ("select_disc", (41, None)),
        ("evaluate_launch", (0x80000001,)),
    ],
)
async def test_prune_claim_reciprocally_blocks_conflicting_callable_entries(harness, method, args):
    harness.plugin._prune_service._starting = True

    result = await getattr(harness.plugin, method)(*args)

    assert result["success"] is False
    assert result["reason"] == "prune_active"
    assert result["message"]


@pytest.mark.parametrize("operation", ["save_status", "download"])
async def test_detached_writer_lifetime_blocks_prune_admission(harness, monkeypatch, operation):
    _seed_bulk_candidate(harness)
    preview = await harness.plugin.get_prune_preview(_preview_request())
    release = asyncio.Event()
    task = None
    if operation == "save_status":
        entered = asyncio.Event()
        finished = asyncio.Event()

        async def delayed_status(_rom_id):
            entered.set()
            await release.wait()
            finished.set()

        monkeypatch.setattr(harness.plugin._save_sync_service, "check_save_status_background", delayed_status)
        assert (await harness.plugin.refresh_save_status(41))["success"] is True
        await entered.wait()
    else:
        task = asyncio.create_task(release.wait())

        async def start_download(_rom_id, *_answers):
            return {"success": True, "message": "started"}

        monkeypatch.setattr(harness.plugin._download_service, "start_download", start_download)
        monkeypatch.setattr(harness.plugin._download_service, "task_for_rom", lambda _rom_id: task)
        assert (await harness.plugin.start_download(41))["success"] is True

    blocked = await harness.plugin.start_prune(
        {
            "preview_id": preview["preview_id"],
            "confirmed": True,
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": True,
            "create_recovery_bundle": False,
            "installed_selection_id": None,
        }
    )
    assert blocked["reason"] == "operation_active"

    release.set()
    if task is not None:
        await task
    else:
        await finished.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    started = await harness.plugin.start_prune(
        {
            "preview_id": preview["preview_id"],
            "confirmed": True,
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": True,
            "create_recovery_bundle": False,
            "installed_selection_id": None,
        }
    )
    assert started["success"] is True
    running = harness.plugin._prune_service._task
    assert running is not None
    await running


async def test_frontend_core_continuation_lease_blocks_prune_until_ack(harness, monkeypatch):
    _seed_bulk_candidate(harness)
    preview = await harness.plugin.get_prune_preview(_preview_request())

    async def set_game_core(_rom_id, _label):
        return {"success": True, "app_id": 0x80000001, "launch_options": "launch"}

    monkeypatch.setattr(harness.plugin._core_service, "set_game_core", set_game_core)
    result = await harness.plugin.set_game_core(41, "core")
    token = result["prune_lease_token"]

    blocked = await harness.plugin.start_prune(
        {
            "preview_id": preview["preview_id"],
            "confirmed": True,
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": True,
            "create_recovery_bundle": False,
            "installed_selection_id": None,
        }
    )
    assert blocked["reason"] == "operation_active"

    assert (await harness.plugin.release_prune_conflict_lease(token))["success"] is True
    started = await harness.plugin.start_prune(
        {
            "preview_id": preview["preview_id"],
            "confirmed": True,
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": True,
            "create_recovery_bundle": False,
            "installed_selection_id": None,
        }
    )
    assert started["success"] is True
    await harness.plugin._prune_service.shutdown()


async def _wait_for_prune_action(harness, action: str):
    for _ in range(200):
        for emitted in harness.emit.await_args_list:
            if emitted.args[0] == "prune_action_required" and emitted.args[1]["action"] == action:
                return emitted.args[1]
        await asyncio.sleep(0.001)
    raise AssertionError(f"Prune action {action} was not emitted")


async def test_recovery_on_repoint_uses_real_save_inventory_filesystem_and_sqlite(harness):
    app_id = 0x80000041
    source = Rom.synced(
        rom_id=41,
        platform_slug="gba",
        name="Removed Game",
        fs_name="Removed Game.gba",
        shortcut_app_id=app_id,
        synced_at="2026-01-01T00:00:00",
        version=VersionMetadata(sibling_group_key="group-41", regions=("USA",)),
    )
    source.record_fetch_generation("older-fetch")
    target = Rom.synced(
        rom_id=42,
        platform_slug="gba",
        name="Live Game",
        fs_name="Live Game.gba",
        shortcut_app_id=None,
        synced_at="2026-01-01T00:00:00",
        version=VersionMetadata(sibling_group_key="group-41", regions=("USA",)),
    )
    target.record_fetch_generation("completed-fetch")
    rom_path = Path(harness.retrodeck_paths.roms_path()) / "gba" / source.fs_name
    rom_path.parent.mkdir(parents=True)
    rom_path.write_bytes(b"installed rom")
    save_path = Path(harness.retrodeck_paths.saves_path()) / "gba" / "Removed Game.srm"
    save_path.parent.mkdir(parents=True)
    save_path.write_bytes(b"local save")
    with harness.uow_factory() as uow:
        uow.roms.save(source)
        uow.roms.save(target)
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=41,
                file_path=str(rom_path),
                rom_dir=None,
                platform_slug="gba",
                system="gba",
                installed_at="2026-01-01T00:00:00",
            )
        )
        uow.rom_save_sync_states.save(
            41,
            RomSaveSyncState(system="gba", files={"Removed Game.srm": FileSyncState(last_sync_hash="known")}),
        )
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(
                platform_slug="gba",
                at="2026-01-02T00:00:00",
                rom_count=2,
                fetch_id="completed-fetch",
            )
        )
    harness.romm.roms[42] = {"id": 42}
    harness.romm.get_rom_once_side_effect_by_id[41] = RommNotFoundError("gone")
    preview = await harness.plugin.get_prune_preview(_preview_request())
    staged = await harness.plugin.stage_prune_installed_selection(
        {
            "preview_id": preview["preview_id"],
            "selection_id": None,
            "rom_ids": [41],
            "final": True,
        }
    )

    started = await harness.plugin.start_prune(
        {
            "preview_id": preview["preview_id"],
            "confirmed": True,
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": False,
            "create_recovery_bundle": True,
            "installed_selection_id": staged["selection_id"],
        }
    )
    action = await _wait_for_prune_action(harness, "repoint_shortcut")
    claim = await harness.plugin.report_prune_action(
        {
            "phase": "claim",
            "run_id": action["run_id"],
            "action_token": action["action_token"],
            "action": action["action"],
            "app_id": action["app_id"],
            "target_rom_id": action["target_rom_id"],
        }
    )
    assert claim["success"] is True
    assert (
        await harness.plugin.report_prune_action(
            {
                "phase": "complete",
                "run_id": action["run_id"],
                "action_token": action["action_token"],
                "success": True,
                "message": "Steam confirmed the launch command.",
            }
        )
    )["success"] is True
    task = harness.plugin._prune_service._task
    assert task is not None
    await task

    with harness.uow_factory() as uow:
        assert uow.roms.get(41) is None
        bound = uow.roms.get(42)
        assert bound is not None
        assert bound.shortcut_app_id == app_id
        assert uow.rom_installs.get(41) is None
        assert uow.rom_save_sync_states.get(41) is None
    assert started["success"] is True
    assert not rom_path.exists()
    assert not save_path.exists()
    backups = list((save_path.parent / ".romm-backup").glob("Removed Game_*.srm"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"local save"
    complete = [call.args[1] for call in harness.emit.await_args_list if call.args[0] == "prune_complete"][-1]
    result = complete["results"][0]
    assert result["status"] == "removed"
    assert result["committed_action"] == "repoint_shortcut"
    assert {"save_quarantine", "installed_rom_content"} <= set(result["mutations"])
    bundle = Path(result["bundle_path"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert {item["kind"] for item in manifest["artifacts"]} >= {"current_save", "installed_rom"}


async def test_stage_selection_rejects_a_foreign_preview_id(harness):
    _seed_installed_bulk_candidate(harness)
    await harness.plugin.get_prune_preview(_preview_request())

    result = await harness.plugin.stage_prune_installed_selection(
        _selection_request("not-the-live-preview", None, [41], True)
    )

    assert result == {
        "success": False,
        "reason": "stale_preview",
        "message": "This cleanup preview is stale. Scan again before confirming.",
    }


async def test_stage_selection_rejects_a_rom_without_disclosed_installed_content(harness):
    _seed_bulk_candidate(harness)
    preview = await harness.plugin.get_prune_preview(_preview_request())

    result = await harness.plugin.stage_prune_installed_selection(
        _selection_request(preview["preview_id"], None, [41], False)
    )

    assert result == {
        "success": False,
        "reason": "invalid_selection",
        "message": "Selection contains a ROM without disclosed installed content.",
    }


async def test_stage_selection_rejects_a_foreign_selection_id(harness):
    _seed_installed_bulk_candidate(harness)
    preview = await harness.plugin.get_prune_preview(_preview_request())
    staged = await harness.plugin.stage_prune_installed_selection(
        _selection_request(preview["preview_id"], None, [41], False)
    )
    assert set(staged) == {"success", "selection_id", "selected_count", "finalized"}
    assert (staged["success"], staged["selected_count"], staged["finalized"]) == (True, 1, False)

    result = await harness.plugin.stage_prune_installed_selection(
        _selection_request(preview["preview_id"], "not-the-live-selection", [], True)
    )

    assert result == {
        "success": False,
        "reason": "stale_selection",
        "message": "This installed-content selection is stale.",
    }


async def test_stage_selection_rejects_a_page_after_the_selection_was_finalized(harness):
    _seed_installed_bulk_candidate(harness)
    preview = await harness.plugin.get_prune_preview(_preview_request())
    staged = await harness.plugin.stage_prune_installed_selection(
        _selection_request(preview["preview_id"], None, [41], True)
    )
    assert staged["finalized"] is True

    result = await harness.plugin.stage_prune_installed_selection(
        _selection_request(preview["preview_id"], staged["selection_id"], [41], True)
    )

    assert result == {
        "success": False,
        "reason": "selection_finalized",
        "message": "This installed-content selection is already complete.",
    }


async def test_conflict_lease_renewal_extends_a_live_lease_and_denies_a_released_one(harness, monkeypatch):
    _seed_bulk_candidate(harness)

    async def set_game_core(_rom_id, _label):
        return {"success": True, "app_id": 0x80000041, "launch_options": "launch"}

    monkeypatch.setattr(harness.plugin._core_service, "set_game_core", set_game_core)
    token = (await harness.plugin.set_game_core(41, "core"))["prune_lease_token"]

    assert await harness.plugin.renew_prune_conflict_lease(token) == {
        "success": True,
        "message": "Operation lease renewed.",
    }

    assert (await harness.plugin.release_prune_conflict_lease(token))["success"] is True
    assert await harness.plugin.renew_prune_conflict_lease(token) == {
        "success": False,
        "reason": "stale_lease",
        "message": "Operation lease is no longer active.",
    }


async def test_release_wait_rejects_an_empty_run_id(harness):
    assert await harness.plugin.wait_for_prune_release("") == {
        "success": False,
        "reason": "invalid_run_id",
        "message": "Cleanup run id must be a non-empty string.",
    }


async def test_release_wait_returns_immediately_for_an_unknown_run(harness):
    assert await harness.plugin.wait_for_prune_release("no-such-run") == {
        "success": True,
        "message": "Cleanup claim is released.",
    }


async def test_full_purge_leaves_save_states_completely_untouched(harness):
    _seed_bulk_candidate(harness)
    rom_path = Path(harness.retrodeck_paths.roms_path()) / "gba" / "Removed Game.gba"
    rom_path.parent.mkdir(parents=True)
    rom_path.write_bytes(b"installed rom")
    saves_dir = Path(harness.retrodeck_paths.saves_path()) / "gba"
    saves_dir.mkdir(parents=True)
    save_path = saves_dir / "Removed Game.srm"
    save_path.write_bytes(b"local save")
    state_path = saves_dir / "Removed Game.state"
    state_path.write_bytes(b"emulator save state")
    with harness.uow_factory() as uow:
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=41,
                file_path=str(rom_path),
                rom_dir=None,
                platform_slug="gba",
                system="gba",
                installed_at="2026-01-01T00:00:00",
            )
        )
        uow.rom_save_sync_states.save(
            41,
            RomSaveSyncState(system="gba", files={"Removed Game.srm": FileSyncState(last_sync_hash="known")}),
        )
    harness.romm.get_rom_once_side_effect_by_id[41] = RommNotFoundError("gone")
    preview = await harness.plugin.get_prune_preview(_preview_request())
    staged = await harness.plugin.stage_prune_installed_selection(
        _selection_request(preview["preview_id"], None, [41], True)
    )

    started = await harness.plugin.start_prune(
        {
            "preview_id": preview["preview_id"],
            "confirmed": True,
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": True,
            "create_recovery_bundle": True,
            "installed_selection_id": staged["selection_id"],
        }
    )
    assert started["success"] is True
    task = harness.plugin._prune_service._task
    assert task is not None
    await task

    with harness.uow_factory() as uow:
        assert uow.roms.get(41) is None
    assert not rom_path.exists()
    assert not save_path.exists()
    assert state_path.exists()
    assert state_path.read_bytes() == b"emulator save state"
    assert list((saves_dir / ".romm-backup").glob("*.state")) == []
    complete = [call.args[1] for call in harness.emit.await_args_list if call.args[0] == "prune_complete"][-1]
    result = complete["results"][0]
    assert result["status"] == "removed"
    bundle = Path(result["bundle_path"])
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert not any(str(item["source_path"]).endswith(".state") for item in manifest["artifacts"])
    assert list(bundle.rglob("*.state")) == []
