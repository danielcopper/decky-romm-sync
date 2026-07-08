"""Contract tests for the version-picker callables over the real Plugin/bootstrap.

Driven frontend-shaped per ``src/api/backend.ts``:
``getVersionList = callable<[number], VersionList>`` and
``switchVersion = callable<[number, number], SwitchVersionResult>`` — positional
JSON-shaped args (the Steam appId, then the target rom_id).

The group is resolved over the REAL SQLite ``roms`` table (migration-010 index)
and the FakeRommApi's ``sibling_roms`` view; only the RomM transport is faked.
"""

from __future__ import annotations

from domain.rom import Rom
from domain.rom_install import RomInstall

_GROUP = "igdb:100:57"
_APP_ID = 42


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


async def test_switch_version_happy_moves_binding(harness):
    """Switching rebinds the target and unbinds the previous representative."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID, name="Game 1")
    _seed_rom(harness, rom_id=2, app_id=None, name="Game 2")
    harness.romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 2}]}

    result = await harness.plugin.switch_version(_APP_ID, 2)
    assert result == {"success": True, "rom_id": 2, "rom_name": "Game 2"}

    with harness.uow_factory() as uow:
        assert uow.roms.get(2).shortcut_app_id == _APP_ID
        assert uow.roms.get(1).shortcut_app_id is None
    # The picker now reports version 2 as active.
    follow_up = await harness.plugin.get_version_list(_APP_ID)
    by_id = {v["rom_id"]: v for v in follow_up["versions"]}
    assert by_id[2]["active"] is True


async def test_switch_version_unknown_app_failure_shape(harness):
    """An unknown appId → canonical ``{success, reason, message}``."""
    result = await harness.plugin.switch_version(999, 2)
    assert result["success"] is False
    assert result["reason"] == "not_found"
    assert isinstance(result["message"], str)
    assert "error" not in result
    assert "error_code" not in result


async def test_switch_version_not_in_group_failure_shape(harness):
    """A target outside the group → canonical ``not_in_group`` failure."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=None, group_key="igdb:999:57")

    result = await harness.plugin.switch_version(_APP_ID, 2)
    assert result["success"] is False
    assert result["reason"] == "not_in_group"
    assert "error" not in result


async def test_switch_version_bound_elsewhere_failure_shape(harness):
    """A target bound to a different shortcut → canonical ``bound_elsewhere``."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=777)

    result = await harness.plugin.switch_version(_APP_ID, 2)
    assert result["success"] is False
    assert result["reason"] == "bound_elsewhere"
    assert "error" not in result


async def test_switch_version_installed_group_failure_shape(harness):
    """A downloaded group member → canonical ``installed`` rejection (#1298)."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    _seed_rom(harness, rom_id=2, app_id=None)
    _seed_install(harness, 1)

    result = await harness.plugin.switch_version(_APP_ID, 2)
    assert result["success"] is False
    assert result["reason"] == "installed"
    assert "error" not in result


async def test_switch_version_unbuildable_target_failure_shape(harness):
    """A server-only sibling whose detail the aggregate rejects → ``invalid_target``."""
    _seed_rom(harness, rom_id=1, app_id=_APP_ID)
    # Sibling of the bound rom, but an id <= 0 the Rom aggregate refuses.
    harness.romm.roms[3] = {"id": 0, "platform_slug": "snes", "sibling_roms": [{"id": 1}]}

    result = await harness.plugin.switch_version(_APP_ID, 3)
    assert result["success"] is False
    assert result["reason"] == "invalid_target"
    assert isinstance(result["message"], str)
    assert "error" not in result
    assert "error_code" not in result
