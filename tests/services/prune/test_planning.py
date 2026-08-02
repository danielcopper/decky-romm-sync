"""Tests for services/prune/planning.py — what a group would do, and why not."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.rom import Rom
from domain.version_metadata import VersionMetadata
from services.prune._models import PruneOptions
from services.prune.planning import GroupPlan, GroupPlanner, GroupPlannerConfig
from services.prune.registry import PruneRegistry, PruneRegistryConfig
from services.prune.results import MutationLedger, PruneResultReporter, PruneResultReporterConfig


class _FakeLiveness:
    """Hands out a fixed verdict per rom id, as the real prober would."""

    def __init__(self, statuses: dict[int, str]) -> None:
        self._statuses = statuses
        self.probed: list[set[int]] = []

    async def probe_many(self, rom_ids: set[int]) -> dict[int, dict[str, str]]:
        self.probed.append(set(rom_ids))
        return {
            rom_id: {
                "status": self._statuses.get(rom_id, "uncertain"),
                "reason": {"vanished": "not_found", "live": "live"}.get(
                    self._statuses.get(rom_id, "uncertain"), "server_unreachable"
                ),
                "message": f"verdict for {rom_id}",
            }
            for rom_id in rom_ids
        }


def _rom(rom_id: int, *, group: str | None = "g", app_id: int | None = None, main: bool = False) -> Rom:
    return Rom.synced(
        rom_id=rom_id,
        platform_slug="gba",
        name=f"Game {rom_id}",
        fs_name=f"Game {rom_id}.gba",
        shortcut_app_id=app_id,
        synced_at="now",
        version=VersionMetadata(sibling_group_key=group, is_main_sibling=main),
    )


def _options(**overrides: Any) -> PruneOptions:
    values: dict[str, Any] = {
        "repoint_shortcuts": True,
        "remove_rows": True,
        "remove_fully_vanished": True,
        "create_recovery_bundle": True,
        "include_installed_rom_ids": frozenset(),
    }
    values.update(overrides)
    return PruneOptions(**values)


def _planner(
    rows: list[Rom],
    statuses: dict[int, str],
    *,
    active_downloads: set[int] | None = None,
    drifted: bool = False,
    settings: dict[str, Any] | None = None,
) -> tuple[GroupPlanner, _FakeLiveness]:
    uow = FakeUnitOfWork()
    with uow:
        for row in rows:
            uow.roms.save(row)
    liveness = _FakeLiveness(statuses)

    async def drift_probe(rom_id: int) -> dict[str, Any]:
        del rom_id
        return {"drifted": drifted}

    planner = GroupPlanner(
        config=GroupPlannerConfig(
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            results=PruneResultReporter(config=PruneResultReporterConfig(emit=_noop_emit)),
            registry=PruneRegistry(config=PruneRegistryConfig(uow_factory=FakeUnitOfWorkFactory(uow))),
            liveness=cast("Any", liveness),
            active_downloads=lambda: active_downloads or set(),
            drift_probe=drift_probe,
            settings=settings if settings is not None else {},
        )
    )
    return planner, liveness


async def _noop_emit(*_args: Any, **_kwargs: Any) -> None:
    return None


class _StubProbe:
    """A liveness prober whose probe_many the test supplies outright."""

    def __init__(self, probe) -> None:
        self.probe_many = probe


async def _plan(rows: list[Rom], statuses: dict[int, str], **kwargs: Any):
    options = kwargs.pop("options", None) or _options()
    planner, _ = _planner(rows, statuses, **kwargs)
    ledger = MutationLedger(rows)
    return await planner.plan("run-1", rows, {row.rom_id for row in rows}, options, 1, 1, ledger), ledger


class TestRefusals:
    async def test_a_group_whose_rows_vanished_locally_is_skipped(self):
        row = _rom(1)
        planner, _ = _planner([], {})
        ledger = MutationLedger([row])

        result = await planner.plan("run-1", [row], {1}, _options(), 1, 1, ledger)

        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "local_state_changed"

    async def test_two_shortcuts_in_one_group_are_refused(self):
        rows = [_rom(1, app_id=0x8001), _rom(2, app_id=0x8002)]
        result, _ = await _plan(rows, {1: "vanished", 2: "vanished"})
        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "multiple_bindings"

    async def test_an_active_download_refuses_the_group_before_any_probe(self):
        rows = [_rom(1)]
        planner, liveness = _planner(rows, {1: "vanished"}, active_downloads={1})
        result = await planner.plan("run-1", rows, {1}, _options(), 1, 1, MutationLedger(rows))
        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "download_in_progress"
        assert liveness.probed == [], "a refused group must not be probed"

    async def test_no_live_and_some_unproven_is_liveness_uncertain(self):
        rows = [_rom(1), _rom(2)]
        result, _ = await _plan(rows, {1: "vanished", 2: "uncertain"})
        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "liveness_uncertain"

    async def test_a_namespace_change_is_named_as_itself(self, monkeypatch):
        rows = [_rom(1)]
        planner, _ = _planner(rows, {})

        async def probe(rom_ids):
            return {
                rom_id: {"status": "uncertain", "reason": "server_namespace_changed", "message": "changed"}
                for rom_id in rom_ids
            }

        monkeypatch.setattr(planner, "_liveness", _StubProbe(probe))
        result = await planner.plan("run-1", rows, {1}, _options(), 1, 1, MutationLedger(rows))
        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "server_namespace_changed"

    async def test_options_that_exclude_everything_say_so(self):
        rows = [_rom(1)]
        result, _ = await _plan(rows, {1: "vanished"}, options=_options(remove_rows=False, remove_fully_vanished=False))
        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "options_excluded"

    async def test_an_unproven_member_outranks_the_options_explanation(self):
        """Fiddling with a toggle cannot fix a ROM RomM never answered for."""
        rows = [_rom(1, app_id=0x8001), _rom(2)]
        result, _ = await _plan(
            rows,
            {1: "live", 2: "uncertain"},
            options=_options(remove_rows=False, remove_fully_vanished=False, repoint_shortcuts=False),
        )
        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "liveness_uncertain"

    async def test_unsynced_saves_without_a_bundle_refuse_the_group(self):
        rows = [_rom(1, app_id=0x8001)]
        result, _ = await _plan(rows, {1: "vanished"}, drifted=True, options=_options(create_recovery_bundle=False))
        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "unsynced_saves"


class TestPlans:
    async def test_a_fully_vanished_bound_group_plans_the_whole_game_action(self):
        rows = [_rom(1, app_id=0x8001)]
        plan, _ = await _plan(rows, {1: "vanished"})

        assert isinstance(plan, GroupPlan)
        assert plan.delete_ids == {1}
        assert plan.fully_dead is True
        assert plan.whole_game_action is True
        assert plan.target_id is None
        assert plan.app_id == 0x8001
        assert plan.bound_row is not None
        assert plan.bound_row.rom_id == 1

    async def test_a_vanished_bound_row_with_a_live_sibling_plans_a_repoint(self):
        rows = [_rom(1, app_id=0x8001), _rom(2, main=True)]
        plan, _ = await _plan(rows, {1: "vanished", 2: "live"})

        assert isinstance(plan, GroupPlan)
        assert plan.target_id == 2
        assert plan.whole_game_action is False
        assert plan.fully_dead is False

    async def test_the_bound_row_is_kept_when_a_live_sibling_has_nowhere_to_repoint(self):
        """Deleting the row that owns the shortcut would strand a live game."""
        rows = [_rom(1, app_id=0x8001), _rom(2, main=True), _rom(3)]
        plan, _ = await _plan(
            rows, {1: "vanished", 2: "live", 3: "vanished"}, options=_options(repoint_shortcuts=False)
        )

        assert isinstance(plan, GroupPlan)
        assert plan.target_id is None
        assert plan.delete_ids == {3}, "the bound row is spared, the other confirmed-gone row is not"

    async def test_sparing_the_bound_row_can_leave_nothing_to_do(self):
        rows = [_rom(1, app_id=0x8001), _rom(2, main=True)]
        result, _ = await _plan(rows, {1: "vanished", 2: "live"}, options=_options(repoint_shortcuts=False))

        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "options_excluded"

    async def test_drift_rides_the_plan_when_a_bundle_will_be_sealed(self):
        rows = [_rom(1, app_id=0x8001)]
        plan, _ = await _plan(rows, {1: "vanished"}, drifted=True)
        assert isinstance(plan, GroupPlan)
        assert plan.drifted is True

    async def test_an_unbound_vanished_row_plans_a_plain_row_removal(self):
        rows = [_rom(1)]
        plan, _ = await _plan(rows, {1: "vanished"})

        assert isinstance(plan, GroupPlan)
        assert plan.app_id is None
        assert plan.bound_row is None
        assert plan.delete_ids == {1}

    async def test_the_ledger_carries_the_freshly_read_rows(self):
        rows = [_rom(1, app_id=0x8001), _rom(2)]
        _, ledger = await _plan(rows, {1: "vanished", 2: "vanished"})
        assert sorted(row.rom_id for row in ledger.rows) == [1, 2]


class TestNoLiveDefault:
    async def test_a_group_whose_live_rows_yield_no_default_is_skipped(self, monkeypatch):
        rows = [_rom(1, app_id=0x8001), _rom(2)]
        monkeypatch.setattr("services.prune.planning.natural_default", lambda *_args, **_kwargs: None)

        result, _ = await _plan(rows, {1: "vanished", 2: "live"})

        assert not isinstance(result, GroupPlan)
        assert result["reason"] == "no_live_default"

    async def test_the_configured_region_preference_reaches_the_resolver(self, monkeypatch):
        seen: list[str] = []

        def _spy(_rows, _live, preferred_region):
            seen.append(preferred_region)
            return 2

        monkeypatch.setattr("services.prune.planning.natural_default", _spy)
        rows = [_rom(1, app_id=0x8001), _rom(2)]

        await _plan(rows, {1: "vanished", 2: "live"}, settings={"preferred_region": "Europe"})

        assert seen == ["Europe"]


@pytest.mark.parametrize("status", ["live", "uncertain"])
async def test_only_a_404_ever_reaches_delete_ids(status):
    rows = [_rom(1, app_id=0x8001), _rom(2)]
    result, _ = await _plan(rows, {1: status, 2: "live"})
    if isinstance(result, GroupPlan):
        assert result.delete_ids == set()
