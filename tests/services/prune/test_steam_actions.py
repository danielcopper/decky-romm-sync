"""Tests for services/prune/steam_actions.py — an unknown outcome is never success."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.rom import Rom
from domain.version_metadata import VersionMetadata
from services.prune.planning import GroupPlan
from services.prune.registry import PruneRegistry, PruneRegistryConfig
from services.prune.results import MutationLedger, PruneResultReporter, PruneResultReporterConfig
from services.prune.steam_actions import SteamActionRunner, SteamActionRunnerConfig

APP_ID = 0x8001


def _rom(rom_id: int, *, app_id: int | None = None) -> Rom:
    return Rom.synced(
        rom_id=rom_id,
        platform_slug="gba",
        name=f"Game {rom_id}",
        fs_name=f"Game {rom_id}.gba",
        shortcut_app_id=app_id,
        synced_at="now",
        version=VersionMetadata(sibling_group_key="g"),
    )


def _plan(*, rows: list[Rom], target_id: int | None = None, whole_game: bool = False) -> GroupPlan:
    bound = next((row for row in rows if row.shortcut_app_id is not None), None)
    return GroupPlan(
        rows=rows,
        group_ids={row.rom_id for row in rows},
        bound_row=bound,
        app_id=bound.shortcut_app_id if bound is not None else None,
        delete_ids={rows[0].rom_id},
        target_id=target_id,
        fully_dead=whole_game,
        whole_game_action=whole_game,
        drifted=False,
    )


async def _noop_emit(*_args: Any, **_kwargs: Any) -> None:
    return None


def _runner(
    rows: list[Rom],
    *,
    action_result: dict[str, Any] | None = None,
    switch_result: dict[str, Any] | None = None,
) -> tuple[SteamActionRunner, FakeUnitOfWork, list[str]]:
    uow = FakeUnitOfWork()
    with uow:
        for row in rows:
            uow.roms.save(row)
    requested: list[str] = []

    async def request_action(_run_id, kind, _data, _bound, _target, _group):
        requested.append(kind)
        return dict(action_result or {"success": True, "message": "ok"})

    async def switch_version(app_id: int, target_rom_id: int, allow_stranded: bool) -> dict[str, Any]:
        del allow_stranded
        return dict(
            switch_result
            or {
                "success": True,
                "app_id": app_id,
                "rom_id": target_rom_id,
                "launch_options": "cmd",
                "target_installed": False,
            }
        )

    runner = SteamActionRunner(
        config=SteamActionRunnerConfig(
            loop=asyncio.get_event_loop(),
            results=PruneResultReporter(config=PruneResultReporterConfig(emit=_noop_emit)),
            registry=PruneRegistry(config=PruneRegistryConfig(uow_factory=FakeUnitOfWorkFactory(uow))),
            switch_version=switch_version,
            request_action=request_action,
        )
    )
    return runner, uow, requested


class TestCaptureSnapshot:
    async def test_a_valid_snapshot_rides_through_with_no_terminal_result(self):
        rows = [_rom(1, app_id=APP_ID)]
        runner, _, requested = _runner(rows, action_result={"success": True, "snapshot": {"app_id": APP_ID}})

        snapshot, result = await runner.capture_snapshot(
            "run-1", _plan(rows=rows, whole_game=True), MutationLedger(rows)
        )

        assert snapshot == {"app_id": APP_ID}
        assert result is None
        assert requested == ["capture_shortcut_snapshot"]

    async def test_a_failed_capture_is_a_terminal_failure(self):
        rows = [_rom(1, app_id=APP_ID)]
        runner, _, _ = _runner(rows, action_result={"success": False, "message": "Steam is gone"})

        snapshot, result = await runner.capture_snapshot(
            "run-1", _plan(rows=rows, whole_game=True), MutationLedger(rows)
        )

        assert snapshot is None
        assert result is not None
        assert result["status"] == "failed"
        assert result["reason"] == "steam_snapshot_failed"

    async def test_a_success_without_a_snapshot_is_still_a_failure(self):
        rows = [_rom(1, app_id=APP_ID)]
        runner, _, _ = _runner(rows, action_result={"success": True})

        _, result = await runner.capture_snapshot("run-1", _plan(rows=rows, whole_game=True), MutationLedger(rows))

        assert result is not None
        assert result["reason"] == "steam_snapshot_failed"

    async def test_an_already_absent_shortcut_reconciles_the_binding_instead(self):
        rows = [_rom(1, app_id=APP_ID)]
        runner, uow, _ = _runner(rows, action_result={"success": True, "shortcut_absent": True})
        ledger = MutationLedger(rows)

        snapshot, result = await runner.capture_snapshot("run-1", _plan(rows=rows, whole_game=True), ledger)

        assert snapshot is None
        assert result is not None
        assert result["reason"] == "shortcut_absence_reconciled"
        assert result["removed_app_id"] == APP_ID
        assert "shortcut_binding" in ledger.mutations
        with uow:
            row = uow.roms.get(1)
        assert row is not None
        assert row.shortcut_app_id is None


class TestRepoint:
    async def test_a_confirmed_repoint_reports_the_committed_action(self):
        rows = [_rom(1, app_id=APP_ID), _rom(2)]
        runner, _, requested = _runner(rows)
        ledger = MutationLedger(rows)

        launch_options, committed, result = await runner.repoint(
            "run-1", _plan(rows=rows, target_id=2), ledger, None, 1, 1
        )

        assert (launch_options, committed, result) == ("cmd", "repoint_shortcut", None)
        assert ledger.action_ambiguous is False
        assert ledger.target_rom_id == 2
        assert requested == ["repoint_shortcut"]

    async def test_a_failed_switch_clears_the_provisional_commit(self):
        """Nothing changed, so the ledger must not claim an ambiguous mutation."""
        rows = [_rom(1, app_id=APP_ID), _rom(2)]
        runner, _, requested = _runner(rows, switch_result={"success": False, "reason": "boom", "message": "no"})
        ledger = MutationLedger(rows)

        _, _, result = await runner.repoint("run-1", _plan(rows=rows, target_id=2), ledger, None, 1, 1)

        assert result is not None
        assert result["status"] == "failed"
        assert ledger.committed_action is None
        assert ledger.action_ambiguous is False
        assert requested == [], "Steam is never asked once the switch itself failed"

    async def test_an_inconsistent_switch_result_is_partial_not_success(self):
        rows = [_rom(1, app_id=APP_ID), _rom(2)]
        runner, _, _ = _runner(
            rows, switch_result={"success": True, "app_id": APP_ID, "rom_id": 99, "launch_options": "cmd"}
        )

        _, _, result = await runner.repoint("run-1", _plan(rows=rows, target_id=2), MutationLedger(rows), None, 1, 1)

        assert result is not None
        assert result["status"] == "partial"
        assert result["reason"] == "repoint_result_invalid"

    async def test_an_attempted_but_unconfirmed_steam_action_is_ambiguous(self):
        rows = [_rom(1, app_id=APP_ID), _rom(2)]
        runner, _, _ = _runner(rows, action_result={"success": False, "mutation_attempted": True, "message": "lost"})
        ledger = MutationLedger(rows)

        _, _, result = await runner.repoint("run-1", _plan(rows=rows, target_id=2), ledger, None, 1, 1)

        assert result is not None
        assert result["reason"] == "action_ambiguous"
        assert result["action_ambiguous"] is True
        assert ledger.action_ambiguous is True

    async def test_a_cleanly_refused_steam_action_is_a_failure_not_an_ambiguity(self):
        rows = [_rom(1, app_id=APP_ID), _rom(2)]
        runner, _, _ = _runner(rows, action_result={"success": False, "message": "refused"})

        _, _, result = await runner.repoint("run-1", _plan(rows=rows, target_id=2), MutationLedger(rows), None, 1, 1)

        assert result is not None
        assert result["reason"] == "steam_action_failed"


class TestRemove:
    async def test_a_confirmed_removal_reconciles_the_binding(self):
        rows = [_rom(1, app_id=APP_ID)]
        runner, uow, requested = _runner(rows)
        ledger = MutationLedger(rows)

        committed, result = await runner.remove(
            "run-1", _plan(rows=rows, whole_game=True), ledger, None, {"app_id": APP_ID}, 1, 1
        )

        assert (committed, result) == ("remove_shortcut", None)
        assert ledger.action_ambiguous is False
        assert "shortcut_binding" in ledger.mutations
        assert requested == ["remove_shortcut"]
        with uow:
            row = uow.roms.get(1)
        assert row is not None
        assert row.shortcut_app_id is None

    async def test_a_claimed_but_unconfirmed_removal_retains_source_data(self):
        rows = [_rom(1, app_id=APP_ID)]
        runner, uow, _ = _runner(rows, action_result={"success": False, "reason": "action_ambiguous"})
        ledger = MutationLedger(rows)

        committed, result = await runner.remove("run-1", _plan(rows=rows, whole_game=True), ledger, None, None, 1, 1)

        assert committed is None
        assert result is not None
        assert result["reason"] == "action_ambiguous"
        assert ledger.committed_action == "remove_shortcut"
        assert ledger.action_ambiguous is True
        with uow:
            assert uow.roms.get(1) is not None, "an ambiguous removal never cascades"

    async def test_a_refused_removal_is_a_plain_failure(self):
        rows = [_rom(1, app_id=APP_ID)]
        runner, _, _ = _runner(rows, action_result={"success": False, "message": "refused"})

        committed, result = await runner.remove(
            "run-1", _plan(rows=rows, whole_game=True), MutationLedger(rows), None, None, 1, 1
        )

        assert committed is None
        assert result is not None
        assert result["status"] == "failed"
        assert result["reason"] == "steam_action_failed"


@pytest.mark.parametrize(
    "call",
    ["capture_snapshot", "repoint", "remove"],
)
async def test_every_action_refuses_a_plan_without_a_bound_shortcut(call):
    rows = [_rom(1)]
    runner, _, _ = _runner(rows)
    plan = _plan(rows=rows, whole_game=True)
    ledger = MutationLedger(rows)

    if call == "capture_snapshot":
        pending = runner.capture_snapshot("run-1", plan, ledger)
    elif call == "repoint":
        pending = runner.repoint("run-1", plan, ledger, None, 1, 1)
    else:
        pending = runner.remove("run-1", plan, ledger, None, None, 1, 1)
    with pytest.raises(RuntimeError):
        await pending
