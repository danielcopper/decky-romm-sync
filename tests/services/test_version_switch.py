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


class _FakeDriftProbe:
    """Stand-in for ``LaunchGateService.check_local_drift`` (async, per rom_id)."""

    def __init__(self, *, drifted: bool = False) -> None:
        self.drifted = drifted
        self.calls: list[int] = []

    async def __call__(self, rom_id: int) -> dict[str, Any]:
        self.calls.append(rom_id)
        return {"drifted": self.drifted, "rom_id": rom_id}


class _FakeReachabilityProbe:
    """Stand-in for ``ConnectionService.probe_reachability`` (async, no args)."""

    def __init__(self, *, online: bool = True) -> None:
        self.online = online
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        return {"online": self.online}


class _FakeRelaunchResolver:
    """Stand-in for ``RelaunchOptionsResolver.relaunch_item_for_rom``.

    ``items`` maps rom_id → the ``{app_id, launch_options}`` item (or ``None`` for
    an unexpected resolver miss / uninstalled ROM). Records each queried rom_id.
    """

    def __init__(self, *, items: dict[int, dict[str, Any] | None] | None = None) -> None:
        self.items = items or {}
        self.calls: list[int] = []

    def relaunch_item_for_rom(self, rom_id: int) -> dict[str, Any] | None:
        self.calls.append(rom_id)
        return self.items.get(rom_id)


class _FakeActiveDownloads:
    """Stand-in for ``DownloadService.active_download_rom_ids`` (returns a set)."""

    def __init__(self, ids: set[int] | None = None) -> None:
        self.ids = set(ids or ())

    def __call__(self) -> set[int]:
        return set(self.ids)


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
def drift_probe() -> _FakeDriftProbe:
    return _FakeDriftProbe()


@pytest.fixture
def reachability_probe() -> _FakeReachabilityProbe:
    return _FakeReachabilityProbe()


@pytest.fixture
def relaunch_resolver() -> _FakeRelaunchResolver:
    return _FakeRelaunchResolver()


@pytest.fixture
def active_downloads() -> _FakeActiveDownloads:
    return _FakeActiveDownloads()


@pytest.fixture
def service(
    event_loop, uow_factory, romm, settings, drift_probe, reachability_probe, relaunch_resolver, active_downloads
) -> VersionSwitchService:
    return VersionSwitchService(
        config=VersionSwitchServiceConfig(
            loop=event_loop,
            logger=logging.getLogger("test_version_switch"),
            clock=FakeClock(),
            uow_factory=uow_factory,
            romm_api=romm,
            settings=settings,
            drift_probe=drift_probe,
            reachability_probe=reachability_probe,
            relaunch_resolver=relaunch_resolver,
            active_downloads=active_downloads,
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

    def test_local_members_are_switchable(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, regions=("USA",))
        _seed_rom(uow, rom_id=2, app_id=None, regions=("Japan",))
        romm.roms[1] = {"id": 1, "sibling_roms": []}

        result = _run(event_loop, service.get_version_list(_APP_ID))
        by_id = {v["rom_id"]: v for v in result["versions"]}
        assert by_id[1]["switchable"] is True
        assert by_id[2]["switchable"] is True

    def test_never_synced_sibling_matching_key_is_switchable(self, event_loop, service, uow, romm):
        # A RomM sibling with no local row yet whose WOULD-BE key (derived from its
        # server metadata) matches the bound group — selecting it persists a row
        # that joins the group, so it is switchable (and marked not synced, #1360).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP, regions=("USA",))
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Game (Japan)"}]}
        romm.roms[5] = {"id": 5, "platform_id": 57, "igdb_id": 100, "name": "Game (Japan)"}

        result = _run(event_loop, service.get_version_list(_APP_ID))
        by_id = {v["rom_id"]: v for v in result["versions"]}
        assert by_id[5]["synced"] is False
        assert by_id[5]["switchable"] is True

    def test_never_synced_bridged_sibling_different_key_not_switchable(self, event_loop, service, uow, romm):
        # #1360: a never-synced RomM sibling whose would-be key lands it in its OWN
        # group (shares only a lower-priority id — igdb differs) is LISTED but not
        # switchable; a switch would bind the shortcut cross-group.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP, regions=("USA",))
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Lara"}]}
        romm.roms[5] = {"id": 5, "platform_id": 57, "igdb_id": 999, "ss_id": 22, "name": "Lara"}

        result = _run(event_loop, service.get_version_list(_APP_ID))
        by_id = {v["rom_id"]: v for v in result["versions"]}
        assert set(by_id) == {1, 5}  # the bridged sibling is still LISTED
        assert by_id[5]["synced"] is False
        assert by_id[5]["switchable"] is False

    def test_cross_group_local_sibling_is_not_switchable(self, event_loop, service, uow, romm):
        # #1359: RomM's sibling_roms bridges two local groups (a shared lower-
        # priority metadata id). Rom 5 is synced locally under a DIFFERENT group
        # key, so the picker lists it (the user sees the version) but it is not
        # switchable — switch_version would reject a cross-group local target.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP, regions=("USA",))
        _seed_rom(uow, rom_id=5, app_id=None, group_key="ss:19274:57", regions=("Japan",))
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Lara", "fs_name_no_ext": "Lara"}]}

        result = _run(event_loop, service.get_version_list(_APP_ID))
        by_id = {v["rom_id"]: v for v in result["versions"]}
        assert set(by_id) == {1, 5}  # the cross-group sibling is still LISTED
        assert by_id[1]["switchable"] is True
        assert by_id[5]["switchable"] is False

    def test_switchable_flag_agrees_with_switch_version(self, event_loop, service, uow, romm):
        # Single-authority property: the picker's switchable flag predicts EXACTLY
        # whether switch_version rejects the target as not_in_group — both decide
        # via target_in_sibling_group. allow_stranded skips the unrelated drift
        # gate so only the membership verdict decides.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)  # bound, in group
        _seed_rom(uow, rom_id=2, app_id=None, group_key=_GROUP)  # local, in group
        _seed_rom(uow, rom_id=5, app_id=None, group_key="ss:19274:57")  # local, other group
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Lara", "fs_name_no_ext": "Lara"}]}

        by_id = {v["rom_id"]: v for v in _run(event_loop, service.get_version_list(_APP_ID))["versions"]}
        assert by_id[2]["switchable"] is True
        assert by_id[5]["switchable"] is False

        # The non-switchable cross-group target IS rejected by the switch...
        cross = _run(event_loop, service.switch_version(_APP_ID, 5, True))
        assert cross["success"] is False
        assert cross["reason"] == "not_in_group"
        # ...and the switchable in-group target is NOT rejected as not_in_group.
        in_group = _run(event_loop, service.switch_version(_APP_ID, 2, True))
        assert in_group.get("reason") != "not_in_group"

    def test_switchable_flag_agrees_with_switch_version_server_only(self, event_loop, service, uow, romm):
        # Single-authority property for NEVER-SYNCED siblings (#1360): the picker's
        # switchable flag predicts EXACTLY whether switch_version accepts the target
        # — both derive the target's would-be key and compare it to the bound group.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)  # bound, igdb:100:57
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "in"}, {"id": 6, "name": "out"}]}
        # rom 5: would-be key matches the bound group. rom 6: bridged, differs.
        romm.roms[5] = {"id": 5, "platform_id": 57, "igdb_id": 100, "platform_slug": "snes", "fs_name": "in.sfc"}
        romm.roms[6] = {"id": 6, "platform_id": 57, "igdb_id": 999, "ss_id": 22, "platform_slug": "snes"}

        by_id = {v["rom_id"]: v for v in _run(event_loop, service.get_version_list(_APP_ID))["versions"]}
        assert by_id[5]["switchable"] is True
        assert by_id[6]["switchable"] is False

        # The bridged (non-switchable) server-only target IS rejected by the switch...
        rejected = _run(event_loop, service.switch_version(_APP_ID, 6, True))
        assert rejected["success"] is False
        assert rejected["reason"] == "not_in_group"
        # ...and the matching server-only target is accepted (persisted + bound).
        accepted = _run(event_loop, service.switch_version(_APP_ID, 5, True))
        assert accepted["success"] is True
        assert accepted["rom_id"] == 5
        with uow as u:
            assert u.roms.get(5).sibling_group_key == _GROUP
            assert u.roms.get(5).shortcut_app_id == _APP_ID

    def test_cross_group_sibling_excluded_from_default(self, event_loop, service, uow, romm):
        # A cross-group RomM sibling that WOULD rank as the default (is_main_sibling
        # on the server) must not win the Default badge — it is not a switchable
        # member of this group, so it is dropped from the default ranking.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP, regions=("USA",))
        _seed_rom(uow, rom_id=2, app_id=None, group_key=_GROUP, regions=("Japan",))
        _seed_rom(uow, rom_id=5, app_id=None, group_key="ss:19274:57")
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Lara", "fs_name_no_ext": "Lara"}]}
        romm.roms[5] = {"id": 5, "platform_id": 57, "is_main_sibling": True}

        by_id = {v["rom_id"]: v for v in _run(event_loop, service.get_version_list(_APP_ID))["versions"]}
        assert by_id[5]["switchable"] is False
        assert by_id[5]["is_default"] is False  # excluded despite is_main_sibling
        assert by_id[1]["is_default"] or by_id[2]["is_default"]  # default stays in-group

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


def _assert_success(result: dict[str, Any], *, rom_id: int, installed: bool, launch_options: str) -> None:
    assert result == {
        "success": True,
        "rom_id": rom_id,
        "target_installed": installed,
        "launch_options": launch_options,
        "app_id": _APP_ID,
    }


class TestSwitchVersion:
    def test_unknown_app_id_not_found(self, event_loop, service):
        result = _run(event_loop, service.switch_version(999, 2, False))
        assert result["success"] is False
        assert result["reason"] == "not_found"
        assert "error" not in result

    def test_f1_active_download_of_group_member_blocks_switch(
        self, event_loop, service, uow, active_downloads, drift_probe
    ):
        # A sibling (rom 2) is mid-download → the switch is refused with the
        # cancel-first shape, before the drift probe or any write.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 1)
        active_downloads.ids = {2}

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        assert result["success"] is False
        assert result["reason"] == "download_in_progress"
        assert isinstance(result["message"], str)
        assert "error" not in result and "error_code" not in result
        assert drift_probe.calls == []  # refused upstream of the drift probe
        with uow as u:
            assert u.roms.get(1).shortcut_app_id == _APP_ID  # nothing switched
            assert u.roms.get(2).shortcut_app_id is None

    def test_f1_active_download_of_bound_member_blocks_switch(self, event_loop, service, uow, active_downloads):
        # The *bound* version itself is downloading → still refused (any group
        # member counts, including the one being switched away from).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        active_downloads.ids = {1}

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        assert result["success"] is False
        assert result["reason"] == "download_in_progress"

    def test_f1_active_download_outside_group_does_not_block(self, event_loop, service, uow, active_downloads):
        # A download of an unrelated ROM (not a group member) never blocks.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        active_downloads.ids = {999}

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        _assert_success(result, rom_id=2, installed=False, launch_options="")

    def test_t2_switch_away_synced_saves_is_free(self, event_loop, service, uow, drift_probe, reachability_probe):
        # Bound version installed, saves synced (no drift) → free switch, no prompt,
        # and the local rebind makes NO server contact (reachability untouched).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 1)

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        _assert_success(result, rom_id=2, installed=False, launch_options="")
        assert drift_probe.calls == [1]  # drift probed on the bound (installed) version
        assert reachability_probe.calls == 0  # no block → no reachability probe
        with uow as u:
            assert u.roms.get(2).shortcut_app_id == _APP_ID
            assert u.roms.get(1).shortcut_app_id is None

    def test_records_applied_launch_options_for_installed_target(self, event_loop, service, uow, relaunch_resolver):
        # Switching onto an installed version re-bakes its launch command (the
        # frontend writes it onto the sticky shortcut); recording it as the applied
        # state keeps the next sync from re-touching the now-correct shortcut (#1383).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 2)
        relaunch_resolver.items = {2: {"app_id": _APP_ID, "launch_options": "flatpak run … /game_2.sfc"}}

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))

        _assert_success(result, rom_id=2, installed=True, launch_options="flatpak run … /game_2.sfc")
        with uow as u:
            assert u.roms.get(2).applied_launch_options == "flatpak run … /game_2.sfc"

    def test_records_empty_applied_for_uninstalled_target(self, event_loop, service, uow):
        # Switching onto an uninstalled version leaves the shortcut on the empty
        # placeholder; that placeholder is recorded so the next sync skips it (#1383).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))

        _assert_success(result, rom_id=2, installed=False, launch_options="")
        with uow as u:
            assert u.roms.get(2).applied_launch_options == ""

    def test_t3_switch_back_to_installed_returns_launch_options(self, event_loop, service, uow, relaunch_resolver):
        # Switching onto a still-downloaded version re-bakes its full launch command.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 2)
        relaunch_resolver.items[2] = {"app_id": _APP_ID, "launch_options": "flatpak run net.retrodeck.retrodeck /x"}

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        _assert_success(result, rom_id=2, installed=True, launch_options="flatpak run net.retrodeck.retrodeck /x")
        assert relaunch_resolver.calls == [2]

    def test_t4_unsynced_saves_online_soft_blocks(self, event_loop, service, uow, drift_probe, reachability_probe):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, fs_name="Game (USA).sfc")
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 1)
        drift_probe.drifted = True
        reachability_probe.online = True

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        assert result["success"] is False
        assert result["reason"] == "unsynced_saves"
        assert result["server_reachable"] is True
        assert result["unsynced_rom_id"] == 1
        assert result["unsynced_version_name"] == "Game (USA)"
        assert isinstance(result["message"], str)
        assert "error" not in result and "error_code" not in result
        # Nothing switched — the binding is untouched.
        with uow as u:
            assert u.roms.get(1).shortcut_app_id == _APP_ID
            assert u.roms.get(2).shortcut_app_id is None

    def test_t5_unsynced_saves_offline_still_soft_blocks(
        self, event_loop, service, uow, drift_probe, reachability_probe
    ):
        # Server unreachable is ALSO a soft block (not a hard block); server_reachable
        # is reported False so the frontend omits the "Sync now & switch" action.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 1)
        drift_probe.drifted = True
        reachability_probe.online = False

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        assert result["success"] is False
        assert result["reason"] == "unsynced_saves"
        assert result["server_reachable"] is False

    def test_t5_allow_stranded_switches_even_offline(self, event_loop, service, uow, drift_probe, reachability_probe):
        # "Switch anyway" overrides the gate and is honoured offline — the drift and
        # reachability probes are not even consulted (allow_stranded short-circuits).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 1)
        drift_probe.drifted = True
        reachability_probe.online = False

        result = _run(event_loop, service.switch_version(_APP_ID, 2, True))
        _assert_success(result, rom_id=2, installed=False, launch_options="")
        assert drift_probe.calls == []
        assert reachability_probe.calls == 0
        with uow as u:
            assert u.roms.get(2).shortcut_app_id == _APP_ID

    def test_t6_synced_offline_switch_makes_no_server_contact(
        self, event_loop, service, uow, romm, drift_probe, reachability_probe
    ):
        # Bound installed, saves synced, offline — switch is purely local, allowed,
        # and touches neither the reachability probe nor the RomM transport.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 1)
        drift_probe.drifted = False
        reachability_probe.online = False

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        _assert_success(result, rom_id=2, installed=False, launch_options="")
        assert reachability_probe.calls == 0
        assert romm.call_log == []

    def test_switch_to_active_is_noop_success(self, event_loop, service, uow, relaunch_resolver):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, name="Game 1")
        _seed_install(uow, 1)
        relaunch_resolver.items[1] = {"app_id": _APP_ID, "launch_options": "cmd"}

        result = _run(event_loop, service.switch_version(_APP_ID, 1, False))
        _assert_success(result, rom_id=1, installed=True, launch_options="cmd")

    def test_resolver_miss_on_installed_target_still_succeeds(self, event_loop, service, uow, relaunch_resolver):
        # Installed target but the resolver unexpectedly returns nothing: the rebind
        # is committed, launch_options degrades to "" (logged loudly, not faked as failure).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 2)  # relaunch_resolver has no item for 2 → returns None

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        _assert_success(result, rom_id=2, installed=True, launch_options="")
        assert relaunch_resolver.calls == [2]
        with uow as u:
            assert u.roms.get(2).shortcut_app_id == _APP_ID  # binding committed

    def test_bound_elsewhere_rejects(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=777)  # already a different shortcut

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        assert result["success"] is False
        assert result["reason"] == "bound_elsewhere"

    def test_local_target_other_group_not_in_group(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
        _seed_rom(uow, rom_id=2, app_id=None, group_key="igdb:999:57")

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        assert result["success"] is False
        assert result["reason"] == "not_in_group"

    def test_not_in_group_message_is_human_readable(self, event_loop, service, uow):
        # The refusal message is human-readable and carries no raw internals (the
        # target rom id or the "sibling group" key jargon it once dumped, #1359).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
        _seed_rom(uow, rom_id=2, app_id=None, group_key="igdb:999:57")

        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        assert result["reason"] == "not_in_group"
        assert result["message"] == (
            "This version's metadata match conflicts with this game's — fix the match in RomM to switch to it."
        )
        assert "sibling group" not in result["message"].lower()
        assert "error" not in result and "error_code" not in result

    def test_unknown_target_not_in_group(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        romm.roms[1] = {"id": 1, "sibling_roms": []}
        # rom 99 has no server entry → get_rom returns {"id": 99}, not a sibling.
        result = _run(event_loop, service.switch_version(_APP_ID, 99, False))
        assert result["success"] is False
        assert result["reason"] == "not_in_group"

    def test_toctou_write_uow_recheck_catches_late_bind(self, event_loop, service, uow, uow_factory):
        # A rebind/download lands during the drift probe (after the context read,
        # before the write UoW): the target gets bound to another shortcut. The
        # write-UoW re-check reads the fresh row and refuses bound_elsewhere — the
        # stale context read (which saw it unbound) does NOT decide.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        _seed_rom(uow, rom_id=2, app_id=None)
        _seed_install(uow, 1)  # bound installed → drift probe runs

        class _MutatingDriftProbe:
            async def __call__(self, rom_id: int) -> dict[str, Any]:
                with uow_factory() as u:
                    target = u.roms.get(2)
                    target.bind_shortcut(999)
                    u.roms.save(target)
                return {"drifted": False, "rom_id": rom_id}

        service._drift_probe = _MutatingDriftProbe()
        result = _run(event_loop, service.switch_version(_APP_ID, 2, False))
        assert result["success"] is False
        assert result["reason"] == "bound_elsewhere"

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

        result = _run(event_loop, service.switch_version(_APP_ID, 3, False))
        _assert_success(result, rom_id=3, installed=False, launch_options="")
        with uow as u:
            persisted = u.roms.get(3)
            assert persisted is not None
            assert persisted.shortcut_app_id == _APP_ID
            assert persisted.regions == ("Japan",)
            assert persisted.sibling_group_key == "igdb:100:57"
            assert u.roms.get(1).shortcut_app_id is None

    def test_uneven_coverage_server_only_switchable_and_adopts_bound_key(self, event_loop, service, uow, romm):
        # #1368: the bound group keys on igdb; a never-synced sibling matched only on
        # ss/hasheous (NO igdb) is canonical-compatible (absent at the canonical
        # source), so it is switchable AND the switch persists it under the BOUND
        # group's key — not its own ss:2002:57 (which the old would-be logic wrongly
        # split on). The next sync re-canonicalizes the whole component together.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key="igdb:1001:57")
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 5, "name": "Game (Europe)"}]}
        romm.roms[5] = {
            "id": 5,
            "platform_id": 57,
            "ss_id": 2002,
            "hasheous_id": 3003,
            "platform_slug": "snes",
            "fs_name": "game_5.sfc",
            "name": "Game (Europe)",
            "sibling_roms": [{"id": 1}],
        }

        by_id = {v["rom_id"]: v for v in _run(event_loop, service.get_version_list(_APP_ID))["versions"]}
        assert by_id[5]["switchable"] is True

        switched = _run(event_loop, service.switch_version(_APP_ID, 5, True))
        assert switched["success"] is True
        assert switched["rom_id"] == 5
        with uow as u:
            assert u.roms.get(5).sibling_group_key == "igdb:1001:57"
            assert u.roms.get(5).shortcut_app_id == _APP_ID
            assert u.roms.get(1).shortcut_app_id is None

    def test_canonical_conflict_server_only_listed_disabled_and_rejected(self, event_loop, service, uow, romm):
        # #1368: a never-synced sibling carrying a DIFFERENT id at the bound
        # canonical source (a genuine cross-game bridge) is listed but disabled, and
        # the switch rejects it as not_in_group — nothing persisted.
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key="igdb:1001:57")
        romm.roms[1] = {"id": 1, "sibling_roms": [{"id": 6, "name": "Other"}]}
        romm.roms[6] = {
            "id": 6,
            "platform_id": 57,
            "igdb_id": 2002,
            "ss_id": 22,
            "platform_slug": "snes",
            "sibling_roms": [{"id": 1}],
        }

        by_id = {v["rom_id"]: v for v in _run(event_loop, service.get_version_list(_APP_ID))["versions"]}
        assert set(by_id) == {1, 6}
        assert by_id[6]["switchable"] is False

        rejected = _run(event_loop, service.switch_version(_APP_ID, 6, True))
        assert rejected["success"] is False
        assert rejected["reason"] == "not_in_group"
        with uow as u:
            assert u.roms.get(6) is None

    def test_server_only_target_soft_blocks_on_bound_drift_before_fetch(
        self, event_loop, service, uow, romm, drift_probe
    ):
        # A server-only target still gates on the BOUND version's save drift, and the
        # refusal is upstream of the target fetch (get_rom never fires).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
        _seed_install(uow, 1)
        drift_probe.drifted = True
        romm.roms[3] = {"id": 3, "platform_slug": "snes", "sibling_roms": [{"id": 1}]}

        result = _run(event_loop, service.switch_version(_APP_ID, 3, False))
        assert result["reason"] == "unsynced_saves"
        assert not any(name == "get_rom" for name, _args, _kwargs in romm.call_log)

    def test_server_unreachable_on_target_fetch(self, event_loop, service, uow, romm):
        _seed_rom(uow, rom_id=1, app_id=_APP_ID)
        romm.get_rom_side_effect = ConnectionError("down")

        result = _run(event_loop, service.switch_version(_APP_ID, 3, False))
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert "error" not in result

    def test_server_target_unbuildable_is_invalid_target(self, event_loop, service, uow, romm):
        # The server detail is IN the group (its would-be key matches the bound
        # group, so membership passes) but carries an id the Rom aggregate rejects
        # (<= 0), so Rom.synced raises and the switch fails as invalid_target — NOT
        # not_in_group (the payload just couldn't be turned into a local row).
        _seed_rom(uow, rom_id=1, app_id=_APP_ID, group_key=_GROUP)
        romm.roms[3] = {
            "id": 0,
            "platform_id": 57,
            "igdb_id": 100,
            "platform_slug": "snes",
            "sibling_roms": [{"id": 1}],
        }

        result = _run(event_loop, service.switch_version(_APP_ID, 3, False))
        assert result["success"] is False
        assert result["reason"] == "invalid_target"
        assert isinstance(result["message"], str)
        assert "error" not in result
        assert "error_code" not in result
