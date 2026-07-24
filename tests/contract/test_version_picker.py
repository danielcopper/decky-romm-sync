"""Contract tests for the version-picker callables over the real Plugin/bootstrap.

Driven frontend-shaped per ``src/api/backend.ts``:
``getVersionList = callable<[number], VersionList>`` and
``switchVersion = callable<[number, number, boolean], SwitchVersionResult>`` —
positional JSON-shaped args (the Steam appId, the target rom_id, and the
``allow_stranded`` "switch anyway" override).

The group is resolved over the REAL SQLite ``roms`` table (migration-010 index)
and the FakeRommApi's ``sibling_roms`` view; only the RomM transport is faked.
Save drift is exercised through the REAL ``check_local_drift`` (real save-file
store + ``rom_save_sync_states``); reachability through the REAL
``probe_reachability`` over the FakeRommApi heartbeat.
"""

from __future__ import annotations

import hashlib
import os

from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_save_sync_state import RomSaveSyncState
from lib.errors import RommNotFoundError

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
    applied_launch_options: str | None = None,
) -> None:
    with harness.uow_factory() as uow:
        rom = Rom(
            rom_id=rom_id,
            platform_slug="snes",
            name=name or f"Game {rom_id}",
            fs_name=f"game_{rom_id}.sfc",
            shortcut_app_id=app_id,
            last_synced_at="2026-01-01T00:00:00",
            sibling_group_key=group_key,
            regions=regions,
            is_main_sibling=is_main_sibling,
            applied_launch_options=applied_launch_options,
        )
        uow.roms.save(rom)
        if applied_launch_options is not None:
            uow.roms.set_applied_launch_options(rom_id, applied_launch_options)


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
    assert result["bound_vanished"] is False
    by_id = {v["rom_id"]: v for v in result["versions"]}
    assert set(by_id) == {1, 2}
    assert by_id[1]["active"] is True
    assert by_id[2]["active"] is False
    # Default badge ignores the current binding — the is_main_sibling wins.
    assert by_id[2]["is_default"] is True
    assert by_id[1]["synced"] is True and by_id[2]["synced"] is True
    # Both local members share the bound key, so both are switchable.
    assert by_id[1]["switchable"] is True and by_id[2]["switchable"] is True
    assert by_id[1]["vanished"] is False and by_id[2]["vanished"] is False
    assert set(by_id[1]) == {
        "rom_id",
        "name",
        "label",
        "regions",
        "languages",
        "revision",
        "tags",
        "synced",
        "installed",
        "switchable",
        "vanished",
        "active",
        "is_default",
    }


async def test_get_version_list_cross_group_sibling_not_switchable(harness):
    """A RomM sibling synced locally under a different group key is listed but not
    switchable — and switch_version rejects it, agreeing with the picker (#1359)."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
    # Rom 5 is a RomM sibling of rom 1 but is locally synced under a DIFFERENT key
    # (RomM bridged the two groups on a shared lower-priority metadata id).
    _seed_rom(harness, rom_id=5, app_id=None, group_key="ss:19274:57")
    harness.romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Lara", "fs_name_no_ext": "Lara"}]}

    result = await harness.plugin.get_version_list(_APP_ID)
    by_id = {v["rom_id"]: v for v in result["versions"]}
    assert set(by_id) == {1, 5}  # both LISTED
    assert by_id[1]["switchable"] is True
    assert by_id[5]["switchable"] is False

    # The backend rejection agrees with the disabled picker row (defense-in-depth).
    rejected = await harness.plugin.switch_version(_APP_ID, 5, True)
    assert rejected["success"] is False
    assert rejected["reason"] == "not_in_group"


async def test_get_version_list_never_synced_matching_key_switchable(harness):
    """A never-synced RomM sibling whose would-be key matches the bound group is
    switchable, and switch_version persists + binds it into the group (#1360)."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
    harness.romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Game (JP)"}]}
    # Rom 5 has no local row; its server metadata derives the same igdb:100:57 key.
    harness.romm.roms[5] = {
        "id": 5,
        "platform_id": 57,
        "igdb_id": 100,
        "platform_slug": "snes",
        "fs_name": "game_5.sfc",
        "name": "Game (JP)",
    }

    result = await harness.plugin.get_version_list(_APP_ID)
    by_id = {v["rom_id"]: v for v in result["versions"]}
    assert set(by_id) == {1, 5}
    assert by_id[5]["synced"] is False
    assert by_id[5]["switchable"] is True

    # The backend agrees with the enabled picker row: it persists the server-only
    # row into the bound group and moves the binding.
    switched = await harness.plugin.switch_version(_APP_ID, 5, True)
    assert switched["success"] is True
    assert switched["rom_id"] == 5
    with harness.uow_factory() as uow:
        persisted = uow.roms.get(5)
        assert persisted is not None
        assert persisted.sibling_group_key == _GROUP
        assert persisted.shortcut_app_id == _APP_ID
        assert uow.roms.get(1).shortcut_app_id is None


async def test_get_version_list_uneven_coverage_switchable_adopts_bound_key(harness):
    """#1368: a never-synced sibling matched only on a lower-priority id (absent at
    the bound group's canonical source) is switchable, and the switch persists it
    under the BOUND group's key — not its own coalesce-first key. The whole
    component re-canonicalizes on the next sync."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, group_key="igdb:1001:57")
    harness.romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Game (EU)"}]}
    # Rom 5 shares only ss/hasheous with the bound group (NO igdb) — its own
    # coalesce-first key would be ss:2002:57, but it is canonical-compatible.
    harness.romm.roms[5] = {
        "id": 5,
        "platform_id": 57,
        "ss_id": 2002,
        "hasheous_id": 3003,
        "platform_slug": "snes",
        "fs_name": "game_5.sfc",
        "name": "Game (EU)",
        "sibling_roms": [{"id": 1}],
    }

    result = await harness.plugin.get_version_list(_APP_ID)
    by_id = {v["rom_id"]: v for v in result["versions"]}
    assert set(by_id) == {1, 5}
    assert by_id[5]["synced"] is False
    assert by_id[5]["switchable"] is True

    switched = await harness.plugin.switch_version(_APP_ID, 5, True)
    assert switched["success"] is True
    assert switched["rom_id"] == 5
    with harness.uow_factory() as uow:
        persisted = uow.roms.get(5)
        assert persisted is not None
        assert persisted.sibling_group_key == "igdb:1001:57"
        assert persisted.shortcut_app_id == _APP_ID
        assert uow.roms.get(1).shortcut_app_id is None


async def test_get_version_list_never_synced_bridged_key_not_switchable(harness):
    """A never-synced RomM sibling whose would-be key differs (bridged on a lower-
    priority id) is listed but not switchable, and switch_version rejects it (#1360)."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
    harness.romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 6, "name": "Lara"}]}
    # Rom 6 shares only ss_id with the bound group; its would-be key is igdb:999:57.
    harness.romm.roms[6] = {"id": 6, "platform_id": 57, "igdb_id": 999, "ss_id": 22, "name": "Lara"}

    result = await harness.plugin.get_version_list(_APP_ID)
    by_id = {v["rom_id"]: v for v in result["versions"]}
    assert set(by_id) == {1, 6}  # both LISTED
    assert by_id[6]["synced"] is False
    assert by_id[6]["switchable"] is False

    # The backend rejection agrees with the disabled picker row (defense-in-depth).
    rejected = await harness.plugin.switch_version(_APP_ID, 6, True)
    assert rejected["success"] is False
    assert rejected["reason"] == "not_in_group"
    # Nothing persisted or bound for the rejected target.
    with harness.uow_factory() as uow:
        assert uow.roms.get(6) is None
        assert uow.roms.get(1).shortcut_app_id == _APP_ID


async def test_get_version_list_solo_group_not_multi(harness):
    """A single-version group renders no picker."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    harness.romm.roms[1] = {"id": 1, "sibling_roms": []}
    result = await harness.plugin.get_version_list(_APP_ID)
    assert result == {
        "multi_version": False,
        "server_query_failed": False,
        "bound_vanished": False,
    }


async def test_get_version_list_unknown_app_not_multi(harness):
    """An unknown / unbound appId renders no picker."""
    result = await harness.plugin.get_version_list(999)
    assert result == {
        "multi_version": False,
        "server_query_failed": False,
        "bound_vanished": False,
    }


async def test_get_version_list_server_fail_partial_shape(harness):
    """A server outage degrades to the local-only list + ``server_query_failed``."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=None)
    harness.romm.get_rom_side_effect = ConnectionError("down")

    result = await harness.plugin.get_version_list(_APP_ID)
    assert result["multi_version"] is True
    assert result["server_query_failed"] is True
    assert result["bound_vanished"] is False
    assert {v["rom_id"] for v in result["versions"]} == {1, 2}
    assert all(v["vanished"] is False for v in result["versions"])


async def test_get_version_list_bound_404_mixed_local_liveness_shape(harness):
    """A bound 404 is explicit list state; each other local id gets its own verdict."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=None)
    _seed_rom(harness, rom_id=3, app_id=None)
    harness.romm.get_rom_side_effect = RommNotFoundError("bound gone")
    harness.romm.get_rom_once_side_effect_by_id[2] = RommNotFoundError("sibling gone")
    harness.romm.roms[3] = {"id": 3}

    result = await harness.plugin.get_version_list(_APP_ID)

    assert set(result) == {"multi_version", "versions", "server_query_failed", "bound_vanished"}
    assert result["multi_version"] is True
    assert result["server_query_failed"] is False
    assert result["bound_vanished"] is True
    assert {v["rom_id"]: v["vanished"] for v in result["versions"]} == {1: True, 2: True, 3: False}
    assert {v["rom_id"]: v["switchable"] for v in result["versions"]} == {1: True, 2: True, 3: True}


async def test_get_version_list_single_bound_404_preserves_non_multi_verdict(harness):
    """The no-picker shape must not silently discard a definitive bound-id 404."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    harness.romm.get_rom_side_effect = RommNotFoundError("bound gone")

    result = await harness.plugin.get_version_list(_APP_ID)

    assert result == {
        "multi_version": False,
        "server_query_failed": False,
        "bound_vanished": True,
    }


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
    state = RomSaveSyncState()
    state.adopt_baseline("game.srm", tracked_save_id=1, last_sync_hash=baseline)
    with harness.uow_factory() as uow:
        uow.rom_save_sync_states.save(bound_id, state)


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
    assert [args[0] for name, args, _kwargs in harness.romm.call_log if name == "get_rom_once"] == [2]
    assert not any(name == "get_rom" for name, _args, _kwargs in harness.romm.call_log)

    with harness.uow_factory() as uow:
        assert uow.roms.get(2).shortcut_app_id == _APP_ID
        assert uow.roms.get(1).shortcut_app_id is None
    # The picker now reports version 2 as active.
    follow_up = await harness.plugin.get_version_list(_APP_ID)
    by_id = {v["rom_id"]: v for v in follow_up["versions"]}
    assert by_id[2]["active"] is True


async def test_switch_version_local_404_has_canonical_shape_and_no_mutation(harness):
    """A definitive local-target 404 refuses before every write/effect boundary."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, applied_launch_options="old-command")
    _seed_rom(harness, rom_id=2, app_id=None, applied_launch_options="target-command")
    _seed_install(harness, 2)
    harness.romm.get_rom_once_side_effect_by_id[2] = RommNotFoundError("gone")
    harness.emit.reset_mock()

    result = await harness.plugin.switch_version(_APP_ID, 2, False)

    assert result == {
        "success": False,
        "reason": "version_vanished",
        "message": "This version is no longer available on RomM.",
    }
    assert [args[0] for name, args, _kwargs in harness.romm.call_log if name == "get_rom_once"] == [2]
    assert not any(name == "get_rom" for name, _args, _kwargs in harness.romm.call_log)
    assert not any(name.startswith("list_") for name, _args, _kwargs in harness.romm.call_log)
    harness.emit.assert_not_awaited()
    with harness.uow_factory() as uow:
        assert uow.roms.get(1).shortcut_app_id == _APP_ID
        assert uow.roms.get(1).applied_launch_options == "old-command"
        assert uow.roms.get(2).shortcut_app_id is None
        assert uow.roms.get(2).applied_launch_options == "target-command"
        assert uow.rom_installs.get(2) is not None


async def test_switch_version_local_probe_transport_failure_fails_open_once(harness):
    """The real callable preserves the fast offline local-switch path."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=None)
    harness.romm.get_rom_once_side_effect_by_id[2] = ConnectionError("offline")

    result = await harness.plugin.switch_version(_APP_ID, 2, False)

    assert result["success"] is True
    assert result["rom_id"] == 2
    assert [args[0] for name, args, _kwargs in harness.romm.call_log if name == "get_rom_once"] == [2]
    assert not any(name == "get_rom" for name, _args, _kwargs in harness.romm.call_log)


async def test_switch_version_allow_stranded_still_refuses_local_404(harness):
    """The frontend override bypasses save drift, never target liveness."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=None)
    harness.romm.get_rom_once_side_effect_by_id[2] = RommNotFoundError("gone")

    result = await harness.plugin.switch_version(_APP_ID, 2, True)

    assert result == {
        "success": False,
        "reason": "version_vanished",
        "message": "This version is no longer available on RomM.",
    }
    with harness.uow_factory() as uow:
        assert uow.roms.get(1).shortcut_app_id == _APP_ID
        assert uow.roms.get(2).shortcut_app_id is None


async def test_switch_version_server_only_404_is_version_vanished_without_row(harness):
    """Mandatory server-only detail loading peels only its typed 404."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, applied_launch_options="old-command")
    harness.romm.get_rom_side_effect = RommNotFoundError("gone")
    harness.emit.reset_mock()

    result = await harness.plugin.switch_version(_APP_ID, 3, False)

    assert result == {
        "success": False,
        "reason": "version_vanished",
        "message": "This version is no longer available on RomM.",
    }
    assert [args[0] for name, args, _kwargs in harness.romm.call_log if name == "get_rom"] == [3]
    assert not any(name == "get_rom_once" for name, _args, _kwargs in harness.romm.call_log)
    harness.emit.assert_not_awaited()
    with harness.uow_factory() as uow:
        assert uow.roms.get(1).shortcut_app_id == _APP_ID
        assert uow.roms.get(1).applied_launch_options == "old-command"
        assert uow.roms.get(3) is None


async def test_switch_version_unknown_app_failure_shape(harness):
    """An unknown appId → canonical ``{success, reason, message}``."""
    result = await harness.plugin.switch_version(999, 2, False)
    assert result["success"] is False
    assert result["reason"] == "not_found"
    assert isinstance(result["message"], str)
    assert "error" not in result
    assert "error_code" not in result
    assert not any(name == "get_rom_once" for name, _args, _kwargs in harness.romm.call_log)


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
    assert not any(name == "get_rom_once" for name, _args, _kwargs in harness.romm.call_log)
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
    assert not any(name == "get_rom_once" for name, _args, _kwargs in harness.romm.call_log)
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
    """allow_stranded skips save drift while target-probe uncertainty fails open."""
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=_APP_ID, installed=True, file_name="game.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=None)
    _seed_bound_drift(harness, 1, baseline="stale-baseline-hash")
    harness.romm.heartbeat_side_effect = ConnectionError("offline")
    harness.romm.get_rom_once_side_effect_by_id[2] = ConnectionError("offline")

    result = await harness.plugin.switch_version(_APP_ID, 2, True)
    assert result["success"] is True
    assert result["rom_id"] == 2
    assert [args[0] for name, args, _kwargs in harness.romm.call_log if name == "get_rom_once"] == [2]


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
    assert [args[0] for name, args, _kwargs in harness.romm.call_log if name == "get_rom_once"] == [2]


async def test_switch_version_unbuildable_target_failure_shape(harness):
    """A server-only sibling whose detail the aggregate rejects → ``invalid_target``."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    # In-group (its would-be key matches the bound igdb:100:57), so membership
    # passes — but an id <= 0 the Rom aggregate refuses, so the build fails.
    harness.romm.roms[3] = {
        "id": 0,
        "platform_id": 57,
        "igdb_id": 100,
        "platform_slug": "snes",
        "sibling_roms": [{"id": 1}],
    }

    result = await harness.plugin.switch_version(_APP_ID, 3, False)
    assert result["success"] is False
    assert result["reason"] == "invalid_target"
    assert isinstance(result["message"], str)
    assert "error" not in result
    assert "error_code" not in result
