"""Tests for VersionSwitchService — get_version_list (read) + switch_version (write)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fakes.fake_romm_api import FakeRommApi
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

from domain.rom import Rom
from domain.rom_install import RomInstall
from services.version_switch import VersionSwitchService, VersionSwitchServiceConfig

_GROUP = "igdb:100:57"
_APP_ID = 42


def _seed_rom(
    uow: FakeUnitOfWork,
    *,
    rom_id: int,
    group_key: str | None = _GROUP,
    app_id: int | None = None,
    name: str | None = None,
    fs_name: str | None = None,
    regions: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
    revision: str = "",
    tags: tuple[str, ...] = (),
    is_main_sibling: bool = False,
) -> None:
    uow.roms.save(
        Rom(
            rom_id=rom_id,
            platform_slug="snes",
            name=name or f"Game {rom_id}",
            fs_name=fs_name or f"game_{rom_id}.sfc",
            shortcut_app_id=app_id,
            last_synced_at="2026-01-01T00:00:00Z",
            sibling_group_key=group_key,
            regions=regions,
            languages=languages,
            revision=revision,
            tags=tags,
            is_main_sibling=is_main_sibling,
        )
    )


def _seed_install(uow: FakeUnitOfWork, rom_id: int) -> None:
    uow.rom_installs.save(
        RomInstall(
            rom_id=rom_id,
            file_path=f"/roms/snes/game_{rom_id}.sfc",
            rom_dir=None,
            platform_slug="snes",
            system="snes",
            installed_at="2026-01-01T00:00:00Z",
        )
    )


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def uow_factory(uow) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(uow=uow)


@pytest.fixture
def romm() -> FakeRommApi:
    return FakeRommApi()


@pytest.fixture
def settings() -> dict[str, Any]:
    return {}


@pytest.fixture
def service(event_loop, uow_factory, romm, settings) -> VersionSwitchService:
    return VersionSwitchService(
        config=VersionSwitchServiceConfig(
            loop=event_loop,
            logger=logging.getLogger("test_version_switch"),
            clock=FakeClock(),
            uow_factory=uow_factory,
            romm_api=romm,
            settings=settings,
        ),
    )


def _run(loop, coro):
    return loop.run_until_complete(coro)


# ── get_version_list ─────────────────────────────────────────────────────


class TestGetVersionList:
    def test_unknown_app_id_not_multi(self, event_loop, service):
        assert _run(event_loop, service.get_version_list(999)) == {"multi_version": False}

    def test_solo_group_not_multi(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        romm.roms[1] = {"id": 1, "sibling_roms": []}
        assert _run(event_loop, service.get_version_list(_APP_ID)) == {"multi_version": False}

    def test_local_members_listed_with_markers(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, regions=("USA",))
        _seed_rom(uow, rom_id=2, app_id=None, regions=("Japan",), is_main_sibling=True)
        romm.roms[1] = {"id": 1, "sibling_roms": []}

        result = _run(event_loop, service.get_version_list(_APP_ID))
        assert result["multi_version"] is True
        assert result["server_query_failed"] is False
        by_id = {v["rom_id"]: v for v in result["versions"]}
        assert by_id[1]["active"] is True
        assert by_id[2]["active"] is False
        assert by_id[1]["synced"] is True and by_id[2]["synced"] is True
        # Default badge ignores the current binding → the is_main_sibling wins.
        assert by_id[2]["is_default"] is True
        assert by_id[1]["is_default"] is False

    def test_server_only_stub_marked_unsynced(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, regions=("USA",))
        romm.roms[1] = {
            "id": 1,
            "sibling_roms": [{"id": 5, "name": "Game (Japan)", "fs_name_no_ext": "Game (Japan)"}],
        }
        romm.roms[5] = {"id": 5, "platform_id": 57, "regions": ["Japan"], "fs_name_no_ext": "Game (Japan)"}

        result = _run(event_loop, service.get_version_list(_APP_ID))
        by_id = {v["rom_id"]: v for v in result["versions"]}
        assert by_id[5]["synced"] is False
        assert by_id[5]["label"] == "Game (Japan)"
        assert by_id[5]["regions"] == ["Japan"]

    def test_server_unreachable_degrades_to_local_only(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        romm.get_rom_side_effect = ConnectionError("down")

        result = _run(event_loop, service.get_version_list(_APP_ID))
        assert result["multi_version"] is True
        assert result["server_query_failed"] is True
        assert {v["rom_id"] for v in result["versions"]} == {1, 2}

    def test_preferred_region_heads_default(self, event_loop, service, uow, romm, settings):
        # No is_main_sibling → the default falls to the 1G1R region ranking, and
        # preferred_region re-heads it (Japan over the USA that would win by default).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, regions=("USA",))
        _seed_rom(uow, rom_id=2, app_id=None, regions=("Japan",))
        romm.roms[1] = {"id": 1, "sibling_roms": []}
        settings["preferred_region"] = "Japan"

        result = _run(event_loop, service.get_version_list(_APP_ID))
        by_id = {v["rom_id"]: v for v in result["versions"]}
        assert by_id[2]["is_default"] is True
        assert by_id[1]["is_default"] is False


# ── switch_version ───────────────────────────────────────────────────────


class TestSwitchVersion:
    def test_unknown_app_id_not_found(self, event_loop, service):
        result = _run(event_loop, service.switch_version(999, 2))
        assert result["success"] is False
        assert result["reason"] == "not_found"
        assert "error" not in result

    def test_local_target_rebinds_and_unbinds_old(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 2}]}

        result = _run(event_loop, service.switch_version(_APP_ID, 2))
        assert result == {"success": True, "rom_id": 2, "rom_name": "Game 2"}
        with uow as u:
            assert u.roms.get(2).shortcut_app_id == _APP_ID
            assert u.roms.get(1).shortcut_app_id is None

    def test_switch_to_active_is_noop_success(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, name="Game 1")
        result = _run(event_loop, service.switch_version(_APP_ID, 1))
        assert result == {"success": True, "rom_id": 1, "rom_name": "Game 1"}

    def test_installed_member_rejects(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 1)

        result = _run(event_loop, service.switch_version(_APP_ID, 2))
        assert result["success"] is False
        assert result["reason"] == "installed"
        assert "error" not in result

    def test_bound_elsewhere_rejects(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=777)  # already a different shortcut

        result = _run(event_loop, service.switch_version(_APP_ID, 2))
        assert result["success"] is False
        assert result["reason"] == "bound_elsewhere"

    def test_local_target_other_group_not_in_group(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
        _seed_rom(uow, rom_id=2, app_id=None, group_key="igdb:999:57")

        result = _run(event_loop, service.switch_version(_APP_ID, 2))
        assert result["success"] is False
        assert result["reason"] == "not_in_group"

    def test_unknown_target_not_in_group(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        romm.roms[1] = {"id": 1, "sibling_roms": []}
        # rom 99 has no server entry → get_rom returns {"id": 99}, not a sibling.
        result = _run(event_loop, service.switch_version(_APP_ID, 99))
        assert result["success"] is False
        assert result["reason"] == "not_in_group"

    def test_server_only_target_persisted_and_bound(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
        romm.roms[3] = {
            "id": 3,
            "name": "Game (Japan)",
            "fs_name": "Game (Japan).sfc",
            "platform_id": 57,
            "platform_slug": "snes",
            "igdb_id": 100,
            "regions": ["Japan"],
            "sibling_roms": [{"id": 1}],
        }

        result = _run(event_loop, service.switch_version(_APP_ID, 3))
        assert result == {"success": True, "rom_id": 3, "rom_name": "Game (Japan)"}
        with uow as u:
            persisted = u.roms.get(3)
            assert persisted is not None
            assert persisted.shortcut_app_id == _APP_ID
            assert persisted.regions == ("Japan",)
            assert persisted.sibling_group_key == "igdb:100:57"
            assert u.roms.get(1).shortcut_app_id is None

    def test_server_unreachable_on_target_fetch(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        romm.get_rom_side_effect = ConnectionError("down")

        result = _run(event_loop, service.switch_version(_APP_ID, 3))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert "error" not in result

    def test_server_target_unbuildable_is_invalid_target(self, event_loop, service, uow, romm):
        # The server detail IS a sibling (its sibling_roms points back at the bound
        # rom) but carries an id the Rom aggregate rejects (<= 0), so Rom.synced
        # raises and the switch fails as invalid_target — NOT not_in_group (the
        # payload just couldn't be turned into a local row).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
        romm.roms[3] = {"id": 0, "platform_slug": "snes", "sibling_roms": [{"id": 1}]}

        result = _run(event_loop, service.switch_version(_APP_ID, 3))
        assert result["success"] is False
        assert result["reason"] == "invalid_target"
        assert isinstance(result["message"], str)
        assert "error" not in result
        assert "error_code" not in result
