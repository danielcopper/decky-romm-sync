"""Contract tests for the version-picker callables over the real Plugin/bootstrap.

Driven frontend-shaped per ``src/api/backend.ts``:
``getVersionList = callable<[number], VersionList>`` and
``switchVersion = callable<[number, number, boolean], SwitchVersionResult>`` —
positional JSON-shaped args (the Steam appId, the target rom_id, and the
``allow_stranded`` "switch anyway" override).

The group is resolved over the REAL SQLite ``roms`` table (migration-010 index)
and the FakeRommApi's ``sibling_roms`` view; only the RomM transport is faked.
Save drift is exercised through the REAL ``check_local_drift`` (real save-file
store + ``rom_save_states``); reachability through the REAL
``probe_reachability`` over the FakeRommApi heartbeat.
"""

from __future__ import annotations

import hashlib
import os

from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_save_state import RomSaveState

from ._seed import seed_group_member

_GROUP = "igdb:100:57"
_APP_ID = 42
_DRIFT_CONTENT = b"changed-on-disk"


def _seed_rom(
    harness,
    *,
    rom_id: int,
    app_id: int | None,
    group_key: str | None = _GROUP,
    regions: tuple[str, ...] = (),
    is_main_sibling: bool = False,
    name: str | None = None,
) -> None:
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug="snes",
                name=name or f"Game {rom_id}",
                fs_name=f"game_{rom_id}.sfc",
                shortcut_app_id=app_id,
                last_synced_at="2026-01-01T00:00:00",
                sibling_group_key=group_key,
                regions=regions,
                is_main_sibling=is_main_sibling,
            )
        )


def _seed_install(harness, rom_id: int) -> None:
    with harness.uow_factory() as uow:
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=f"/roms/snes/game_{rom_id}.sfc",
                rom_dir=None,
                platform_slug="snes",
                system="snes",
                installed_at="2026-01-01T00:00:00",
            )
        )


# ── get_version_list ─────────────────────────────────────────────────────


async def test_get_version_list_happy_shape(harness):
    """A multi-version group returns every version with active/default markers."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, regions=("USA",))
    _seed_rom(harness, rom_id=2, app_id=None, regions=("Japan",), is_main_sibling=True)
    harness.romm.roms[1] = {"id": 1, "sibling_roms": []}

    result = await harness.plugin.get_version_list(_APP_ID)
    assert result["multi_version"] is True
    assert result["server_query_failed"] is False
    by_id = {v["rom_id"]: v for v in result["versions"]}
    assert set(by_id) == {1, 2}
    assert by_id[1]["active"] is True
    assert by_id[2]["active"] is False
    # Default badge ignores the current binding — the is_main_sibling wins.
    assert by_id[2]["is_default"] is True
    assert by_id[1]["synced"] is True and by_id[2]["synced"] is True


async def test_get_version_list_solo_group_not_multi(harness):
    """A single-version group renders no picker."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    harness.romm.roms[1] = {"id": 1, "sibling_roms": []}
    result = await harness.plugin.get_version_list(_APP_ID)
    assert result == {"multi_version": False}


async def test_get_version_list_unknown_app_not_multi(harness):
    """An unknown / unbound appId renders no picker."""
    result = await harness.plugin.get_version_list(999)
    assert result == {"multi_version": False}


async def test_get_version_list_server_fail_partial_shape(harness):
    """A server outage degrades to the local-only list + ``server_query_failed``."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=None)
    harness.romm.get_rom_side_effect = ConnectionError("down")

    result = await harness.plugin.get_version_list(_APP_ID)
    assert result["multi_version"] is True
    assert result["server_query_failed"] is True
    assert {v["rom_id"] for v in result["versions"]} == {1, 2}


# ── switch_version ───────────────────────────────────────────────────────


def _seed_bound_drift(harness, bound_id: int, *, baseline: str) -> None:
    """Seed the bound version installed with a local save file + a recorded baseline.

    ``baseline`` != the on-disk content's hash reproduces un-uploaded drift; ==
    reproduces a synced state. The save discovery keys the filename off the
    install path stem (``game`` → ``game.srm``), matching ``_DRIFT_CONTENT``.
    """
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), "gba")
    os.makedirs(saves_dir, exist_ok=True)
    with open(os.path.join(saves_dir, "game.srm"), "wb") as fh:
        fh.write(_DRIFT_CONTENT)
    state = RomSaveState()
    state.adopt_baseline("game.srm", tracked_save_id=1, last_sync_hash=baseline)
    with harness.uow_factory() as uow:
        uow.rom_save_states.save(bound_id, state)


async def test_switch_version_happy_moves_binding(harness):
    """Switching rebinds the target and unbinds the previous representative."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, name="Game 1")
    _seed_rom(harness, rom_id=2, app_id=None, name="Game 2")
    harness.romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 2}]}

    result = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert result == {
        "success": True,
        "rom_id": 2,
        "target_installed": False,
        "launch_options": "",
        "app_id": _APP_ID,
    }

    with harness.uow_factory() as uow:
        assert uow.roms.get(2).shortcut_app_id == _APP_ID
        assert uow.roms.get(1).shortcut_app_id is None
    # The picker now reports version 2 as active.
    follow_up = await harness.plugin.get_version_list(_APP_ID)
    by_id = {v["rom_id"]: v for v in follow_up["versions"]}
    assert by_id[2]["active"] is True


async def test_switch_version_unknown_app_failure_shape(harness):
    """An unknown appId → canonical ``{success, reason, message}``."""
    result = await harness.plugin.switch_version(999, 2, False)
    assert result["success"] is False
    assert result["reason"] == "not_found"
    assert isinstance(result["message"], str)
    assert "error" not in result
    assert "error_code" not in result


async def test_switch_version_not_in_group_failure_shape(harness):
    """A target outside the group → canonical ``not_in_group`` failure."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=None, group_key="igdb:999:57")

    result = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert result["success"] is False
    assert result["reason"] == "not_in_group"
    assert "error" not in result


async def test_switch_version_bound_elsewhere_failure_shape(harness):
    """A target bound to a different shortcut → canonical ``bound_elsewhere``."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=777)

    result = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert result["success"] is False
    assert result["reason"] == "bound_elsewhere"
    assert "error" not in result


async def test_switch_version_blocked_by_active_download(harness):
    """A switch is refused while a group member has an active download (#1298 F1)."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID)
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None)
    # Mark sibling 2 as actively downloading in the real DownloadService state.
    harness.plugin._download_service._download_in_progress.add(2)

    result = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert result["success"] is False
    assert result["reason"] == "download_in_progress"
    assert isinstance(result["message"], str)
    assert "error" not in result and "error_code" not in result
    # Nothing switched — the binding is untouched.
    with harness.uow_factory() as uow:
        assert uow.roms.get(1).shortcut_app_id == _APP_ID
        assert uow.roms.get(2).shortcut_app_id is None


async def test_switch_version_downloaded_synced_is_free(harness):
    """A downloaded, synced bound version switches away freely (no #1298 block)."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID, installed=True, file_name="game.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None)
    # Baseline matches on-disk content → no drift → free switch.
    _seed_bound_drift(harness, 1, baseline=hashlib.md5(_DRIFT_CONTENT).hexdigest())

    result = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert result["success"] is True
    assert result["rom_id"] == 2
    with harness.uow_factory() as uow:
        assert uow.roms.get(2).shortcut_app_id == _APP_ID


async def test_switch_version_switch_back_returns_launch_options(harness):
    """Switching onto a still-downloaded version returns its full launch command."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID)  # bound, uninstalled
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None, installed=True, file_name="game.gba")

    result = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert result["success"] is True
    assert result["rom_id"] == 2
    assert result["target_installed"] is True
    # The real relaunch resolver baked the RetroDECK launch command for the install.
    assert "net.retrodeck.retrodeck" in result["launch_options"]
    assert "game.gba" in result["launch_options"]


async def test_switch_version_unsynced_saves_online_soft_blocks(harness):
    """Unsynced bound saves + reachable server → discriminated soft-block shape."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID, installed=True, file_name="game.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None)
    _seed_bound_drift(harness, 1, baseline="stale-baseline-hash")

    result = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert result["success"] is False
    assert result["reason"] == "unsynced_saves"
    assert result["server_reachable"] is True
    assert result["unsynced_rom_id"] == 1
    assert isinstance(result["unsynced_version_name"], str)
    assert isinstance(result["message"], str)
    assert "error" not in result and "error_code" not in result
    # Nothing switched.
    with harness.uow_factory() as uow:
        assert uow.roms.get(1).shortcut_app_id == _APP_ID
        assert uow.roms.get(2).shortcut_app_id is None


async def test_switch_version_unsynced_saves_offline_soft_blocks(harness):
    """Unsynced bound saves + unreachable server → soft-block, server_reachable False."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID, installed=True, file_name="game.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None)
    _seed_bound_drift(harness, 1, baseline="stale-baseline-hash")
    harness.romm.heartbeat_side_effect = ConnectionError("offline")

    result = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert result["success"] is False
    assert result["reason"] == "unsynced_saves"
    assert result["server_reachable"] is False


async def test_switch_version_switch_anyway_overrides_online(harness):
    """allow_stranded switches despite unsynced saves (server reachable)."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID, installed=True, file_name="game.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None)
    _seed_bound_drift(harness, 1, baseline="stale-baseline-hash")

    result = await harness.plugin.switch_version(_APP_ID, 2, True)
    assert result["success"] is True
    assert result["rom_id"] == 2
    with harness.uow_factory() as uow:
        assert uow.roms.get(2).shortcut_app_id == _APP_ID


async def test_switch_version_switch_anyway_overrides_offline(harness):
    """allow_stranded switches despite unsynced saves AND an unreachable server."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID, installed=True, file_name="game.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None)
    _seed_bound_drift(harness, 1, baseline="stale-baseline-hash")
    harness.romm.heartbeat_side_effect = ConnectionError("offline")

    result = await harness.plugin.switch_version(_APP_ID, 2, True)
    assert result["success"] is True
    assert result["rom_id"] == 2


async def test_switch_version_sync_then_retry_succeeds(harness):
    """After the drift is synced away, the previously-blocked switch succeeds."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID, installed=True, file_name="game.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None)
    _seed_bound_drift(harness, 1, baseline="stale-baseline-hash")

    blocked = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert blocked["reason"] == "unsynced_saves"

    # A completed sync records a fresh baseline matching the on-disk save.
    _seed_bound_drift(harness, 1, baseline=hashlib.md5(_DRIFT_CONTENT).hexdigest())
    retried = await harness.plugin.switch_version(_APP_ID, 2, False)
    assert retried["success"] is True
    assert retried["rom_id"] == 2


async def test_switch_version_unbuildable_target_failure_shape(harness):
    """A server-only sibling whose detail the aggregate rejects → ``invalid_target``."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    # Sibling of the bound rom, but an id <= 0 the Rom aggregate refuses.
    harness.romm.roms[3] = {"id": 0, "platform_slug": "snes", "sibling_roms": [{"id": 1}]}

    result = await harness.plugin.switch_version(_APP_ID, 3, False)
    assert result["success"] is False
    assert result["reason"] == "invalid_target"
    assert isinstance(result["message"], str)
    assert "error" not in result
    assert "error_code" not in result
