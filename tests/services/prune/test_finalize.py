"""Tests for services/prune/finalize.py — every proof holds, or nothing is destroyed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from typing import Any, cast

import pytest
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.rom import Rom
from domain.version_metadata import VersionMetadata
from services.prune._models import RecoveryHandle
from services.prune.finalize import GroupFinalizer, GroupFinalizerConfig
from services.prune.planning import GroupPlan
from services.prune.registry import PruneRegistry, PruneRegistryConfig
from services.prune.results import MutationLedger, PruneResultReporter, PruneResultReporterConfig
from services.prune.save_locks import SaveLockCoordinator, SaveLockCoordinatorConfig


class _FakeLiveness:
    def __init__(self, statuses: dict[int, str]) -> None:
        self._statuses = statuses

    async def probe_many(self, rom_ids: set[int]) -> dict[int, dict[str, str]]:
        return {
            rom_id: {
                "status": self._statuses.get(rom_id, "vanished"),
                "reason": "not_found" if self._statuses.get(rom_id, "vanished") == "vanished" else "live",
                "message": f"verdict for {rom_id}",
            }
            for rom_id in rom_ids
        }


class _FakeSaveCoordinator:
    def __init__(self, *, inventory: dict[str, Any] | None = None) -> None:
        self.inventory = inventory or {"lock_rom_ids": [1], "exclusive": [], "warnings": [], "source_claims": {}}
        # Per-request answers, keyed by the sorted ids asked about, so a test can
        # widen ownership for the delete set alone.
        self.by_request: dict[tuple[int, ...], dict[str, Any]] = {}
        self.quarantine_result: dict[str, Any] = {"success": True, "moved": [], "ambiguous": False}
        self.absences_valid = True
        self.calls: list[str] = []

    def inventory_prune_saves(self, purge_rom_ids: list[int]) -> dict[str, Any]:
        return self.by_request.get(tuple(sorted(purge_rom_ids)), self.inventory)

    @contextlib.asynccontextmanager
    async def lock_prune_roms(self, rom_ids: list[int]):
        yield

    def quarantine_prune_saves(self, files, claims=None) -> dict[str, Any]:
        self.calls.append("quarantine")
        return self.quarantine_result

    def validate_prune_absences(self, claims) -> bool:
        self.calls.append("validate_absences")
        return self.absences_valid


class _FakeArtifacts:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.result: dict[str, Any] = {"success": True, "changed": True, "ambiguous": False}

    def remove(self, rom_ids, claims) -> dict[str, Any]:
        self._calls.append("artifacts")
        return self.result

    def recovery_artifacts(self, rom_ids):  # pragma: no cover - unused here
        return []


class _FakeInstalledRemover:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.result: dict[str, Any] = {"success": True, "changed": True, "ambiguous": False}

    def __call__(self, rom_id, claims) -> dict[str, Any]:
        self._calls.append("installed_content")
        return self.result


class _FakeSteamRecovery:
    def snapshot(self, app_id):  # pragma: no cover - unused here
        raise NotImplementedError

    def validate_state(self, app_id, backend) -> bool:
        return True

    def remove_state(self, app_id, backend, claims) -> dict[str, Any]:
        return {"success": True, "changed": True, "ambiguous": False}


class _FakeRecoveryStore:
    def __init__(self) -> None:
        self.sources_match = True

    def validate_sources(self, bundle_path, digest) -> bool:
        return self.sources_match

    def source_claims(self, bundle_path):  # pragma: no cover - unused here
        raise NotImplementedError


class _FakeRecovery:
    def __init__(self) -> None:
        self.state_ok = True

    def state_matches(self, *_args) -> bool:
        return self.state_ok


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


async def _noop_emit(*_args: Any, **_kwargs: Any) -> None:
    return None


class Fixture:
    """One finalizer wired over fakes, with the order of destructive calls recorded."""

    def __init__(self, rows: list[Rom], statuses: dict[int, str], *, active_downloads: set[int] | None = None) -> None:
        self.uow = FakeUnitOfWork()
        with self.uow:
            for row in rows:
                self.uow.roms.save(row)
        self.order: list[str] = []
        self.saves = _FakeSaveCoordinator()
        self.saves.calls = self.order
        self.artifacts = _FakeArtifacts(self.order)
        self.installed = _FakeInstalledRemover(self.order)
        self.recovery = _FakeRecovery()
        self.recovery_store = _FakeRecoveryStore()
        loop = asyncio.get_event_loop()
        self.finalizer = GroupFinalizer(
            config=GroupFinalizerConfig(
                loop=loop,
                logger=logging.getLogger("test"),
                results=PruneResultReporter(config=PruneResultReporterConfig(emit=_noop_emit)),
                liveness=cast("Any", _FakeLiveness(statuses)),
                save_locks=SaveLockCoordinator(
                    config=SaveLockCoordinatorConfig(loop=loop, save_coordinator=cast("Any", self.saves))
                ),
                registry=PruneRegistry(config=PruneRegistryConfig(uow_factory=FakeUnitOfWorkFactory(self.uow))),
                recovery=cast("Any", self.recovery),
                recovery_store=cast("Any", self.recovery_store),
                steam_recovery=cast("Any", _FakeSteamRecovery()),
                save_coordinator=cast("Any", self.saves),
                prune_artifacts=cast("Any", self.artifacts),
                active_downloads=lambda: active_downloads or set(),
                remove_installed_files=cast("Any", self.installed),
            )
        )


def _plan(rows: list[Rom], *, whole_game: bool = True) -> GroupPlan:
    bound = next((row for row in rows if row.shortcut_app_id is not None), None)
    return GroupPlan(
        rows=rows,
        group_ids={row.rom_id for row in rows},
        bound_row=bound,
        app_id=bound.shortcut_app_id if bound is not None else None,
        delete_ids={row.rom_id for row in rows},
        target_id=None,
        fully_dead=whole_game,
        whole_game_action=whole_game,
        drifted=False,
    )


async def _finish(fixture: Fixture, rows: list[Rom], **overrides: Any) -> dict[str, Any]:
    plan = overrides.pop("plan", None) or _plan(rows)
    kwargs: dict[str, Any] = {
        "run_id": "run-1",
        "initial_rows": rows,
        "plan": plan,
        "committed_action": None,
        "handle": None,
        "recovery_ids": set(plan.delete_ids),
        "index": 1,
        "total": 1,
        "launch_options": None,
        "ledger": MutationLedger(rows),
        "vanished_source_id": None,
    }
    kwargs.update(overrides)
    return await fixture.finalizer.finish(**kwargs)


class TestCascade:
    async def test_a_clean_run_cascades_in_the_contracted_order(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        ledger = MutationLedger(rows)

        result = await _finish(fixture, rows, ledger=ledger)

        assert result["status"] == "removed"
        assert result["removed_rom_ids"] == [1]
        assert fixture.order == ["quarantine", "installed_content", "artifacts", "validate_absences"]
        assert ledger.mutations == ["installed_rom_content", "plugin_artifacts", "database_rows"]
        with fixture.uow:
            row = fixture.uow.roms.get(1)
        assert row is None

    async def test_the_message_is_singular_for_one_row_and_plural_for_more(self):
        one = [_rom(1)]
        assert "1 confirmed vanished entry" in (await _finish(Fixture(one, {1: "vanished"}), one))["message"]
        two = [_rom(1), _rom(2)]
        message = (await _finish(Fixture(two, {1: "vanished", 2: "vanished"}), two))["message"]
        assert "2 confirmed vanished entries" in message


class TestGuardsRetainEverything:
    async def test_a_resurrected_row_stops_the_group_before_any_mutation(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "live"})

        result = await _finish(fixture, rows)

        assert result["status"] == "skipped"
        assert fixture.order == []
        with fixture.uow:
            assert fixture.uow.roms.get(1) is not None

    async def test_a_download_that_became_active_retains_the_source(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"}, active_downloads={1})

        result = await _finish(fixture, rows)

        assert result["reason"] == "download_in_progress"
        assert fixture.order == []

    async def test_a_row_that_disappeared_locally_retains_the_rest(self):
        rows = [_rom(1), _rom(2)]
        fixture = Fixture([rows[0]], {1: "vanished", 2: "vanished"})

        result = await _finish(fixture, rows)

        assert result["reason"] == "local_state_changed"
        assert fixture.order == []

    async def test_widened_save_ownership_retains_the_source_and_surfaces_warnings(self):
        """A save the held locks do not cover may not be touched by this group."""
        rows = [_rom(1), _rom(2)]
        fixture = Fixture(rows, {1: "vanished", 2: "vanished"})
        plan = replace(_plan(rows), delete_ids={2})
        # Locking rom 1 alone, while rom 2's saves turn out to be co-owned by both.
        fixture.saves.by_request = {
            (1,): {"lock_rom_ids": [1], "exclusive": [], "warnings": [], "source_claims": {}},
            (2,): {
                "lock_rom_ids": [1, 2],
                "exclusive": [],
                "warnings": ["a save path could not be resolved"],
                "source_claims": {},
            },
        }

        result = await _finish(fixture, rows, plan=plan, recovery_ids={1})

        assert result["reason"] == "save_ownership_changed"
        assert result["warnings"] == ["a save path could not be resolved"]
        assert fixture.order == []
        with fixture.uow:
            assert fixture.uow.roms.get(2) is not None

    async def test_a_sealed_bundle_that_no_longer_matches_retains_the_source(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.recovery.state_ok = False
        handle = RecoveryHandle("/bundles/b", {}, fixture.saves.inventory, None, {}, "digest")

        result = await _finish(fixture, rows, handle=handle)

        assert result["reason"] == "recovery_state_changed"
        assert fixture.order == []

    async def test_a_bundle_whose_sources_changed_on_disk_retains_the_source(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.recovery_store.sources_match = False
        handle = RecoveryHandle("/bundles/b", {}, fixture.saves.inventory, None, {}, "digest")

        result = await _finish(fixture, rows, handle=handle)

        assert result["reason"] == "recovery_state_changed"
        assert result["bundle_path"] == "/bundles/b"


class TestCascadeStopsPartway:
    async def test_a_failed_quarantine_never_reaches_the_content_removal(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.saves.quarantine_result = {
            "success": False,
            "message": "a save reappeared",
            "moved": [],
            "ambiguous": False,
        }

        result = await _finish(fixture, rows)

        assert result["status"] == "failed"
        assert result["reason"] == "save_quarantine_failed"
        assert fixture.order == ["quarantine"]
        with fixture.uow:
            assert fixture.uow.roms.get(1) is not None

    async def test_a_quarantine_that_moved_files_before_failing_is_partial(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.saves.quarantine_result = {
            "success": False,
            "message": "stopped halfway",
            "moved": ["/saves/gba/g.srm"],
            "ambiguous": False,
        }
        ledger = MutationLedger(rows)

        result = await _finish(fixture, rows, ledger=ledger)

        assert result["status"] == "partial"
        assert ledger.mutations == ["save_quarantine"]

    async def test_a_failed_content_removal_stops_before_the_artifacts(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.installed.result = {"success": False, "message": "busy", "changed": False, "ambiguous": False}

        result = await _finish(fixture, rows)

        assert result["reason"] == "rom_removal_failed"
        assert fixture.order == ["quarantine", "installed_content"]

    async def test_a_not_installed_rom_is_not_a_removal_failure(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.installed.result = {"success": False, "reason": "not_installed", "changed": False, "ambiguous": False}

        assert (await _finish(fixture, rows))["status"] == "removed"

    async def test_a_failed_artifact_cleanup_stops_before_the_rows(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.artifacts.result = {"success": False, "message": "locked", "changed": False, "ambiguous": False}

        result = await _finish(fixture, rows)

        assert result["reason"] == "artifact_cleanup_failed"
        with fixture.uow:
            assert fixture.uow.roms.get(1) is not None

    async def test_a_save_recreated_after_quarantine_retains_the_aggregate(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.saves.absences_valid = False

        result = await _finish(fixture, rows)

        assert result["reason"] == "save_state_changed"
        assert result["status"] == "partial", "the filesystem was already changed by then"
        with fixture.uow:
            assert fixture.uow.roms.get(1) is not None

    async def test_an_ambiguous_mutation_is_recorded_as_such(self):
        rows = [_rom(1)]
        fixture = Fixture(rows, {1: "vanished"})
        fixture.installed.result = {"success": True, "changed": True, "ambiguous": True}
        ledger = MutationLedger(rows)

        await _finish(fixture, rows, ledger=ledger)

        assert ledger.ambiguous_mutations == ["installed_rom_content"]


@pytest.mark.parametrize("status", ["live", "uncertain"])
async def test_no_verdict_other_than_vanished_ever_reaches_the_cascade(status):
    rows = [_rom(1)]
    fixture = Fixture(rows, {1: status})

    result = await _finish(fixture, rows)

    assert result["status"] == "skipped"
    assert fixture.order == []
