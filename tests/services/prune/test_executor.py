"""Tests for services/prune/executor.py — run-level sequencing and its audit trail.

The per-group phases are covered by their own modules' tests, and the full
frontend-facing run is covered end to end in ``test_service.py``. What is pinned
here is what only the executor owns: the namespace check that must precede every
group, the run's audit lines, and the terminal frame that must survive a group
blowing up.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.rom import Rom
from domain.version_metadata import VersionMetadata
from lib.url_host import romm_namespace
from services.prune._models import PruneOptions, PrunePreview
from services.prune.executor import PruneExecutor, PruneExecutorConfig
from services.prune.registry import PruneRegistry, PruneRegistryConfig

_SETTINGS = {"romm_url": "https://romm.example", "romm_user_id": 1}


def _rom(rom_id: int, *, app_id: int | None = None) -> Rom:
    return Rom.synced(
        rom_id=rom_id,
        platform_slug="gba",
        name=f"Game {rom_id}",
        fs_name=f"Game {rom_id}.gba",
        shortcut_app_id=app_id,
        synced_at="now",
        version=VersionMetadata(sibling_group_key=f"g{rom_id}"),
    )


class _Unusable:
    """Any use at all is a contract violation for the flows under test here."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"{name} must not be reached")


def _preview(rows: list[Rom], namespace: str) -> PrunePreview:
    return PrunePreview(
        preview_id="preview-1",
        scope="bulk",
        explicit_rom_id=None,
        candidate_ids=frozenset(row.rom_id for row in rows),
        fingerprint=(),
        entries=(),
        free_bytes=0,
        server_namespace=namespace,
    )


def _options() -> PruneOptions:
    return PruneOptions(
        repoint_shortcuts=False,
        remove_rows=True,
        remove_fully_vanished=True,
        create_recovery_bundle=False,
        include_installed_rom_ids=frozenset(),
    )


def _executor(rows: list[Rom], settings: dict[str, Any], emitted: list[tuple[str, dict[str, Any]]]) -> PruneExecutor:
    uow = FakeUnitOfWork()
    with uow:
        for row in rows:
            uow.roms.save(row)

    async def emit(event: str, payload: dict[str, Any]) -> None:
        emitted.append((event, payload))

    async def unusable_request(*_args: Any) -> dict[str, Any]:
        raise AssertionError("no Steam action may be requested by these flows")

    async def unusable_switch(app_id: int, target_rom_id: int, allow_stranded: bool) -> dict[str, Any]:
        del app_id, target_rom_id, allow_stranded
        raise AssertionError("no version switch may be requested by these flows")

    async def drift_probe(rom_id: int) -> dict[str, Any]:
        del rom_id
        return {"drifted": False}

    return PruneExecutor(
        config=PruneExecutorConfig(
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("prune-test"),
            emit=emit,
            romm_api=cast("Any", _Unusable()),
            recovery_store=cast("Any", _Unusable()),
            prune_artifacts=cast("Any", _Unusable()),
            steam_recovery=cast("Any", _Unusable()),
            save_coordinator=cast("Any", _Unusable()),
            active_downloads=set,
            drift_probe=drift_probe,
            remove_installed_files=cast("Any", _Unusable()),
            switch_version=unusable_switch,
            settings=settings,
            recovery=cast("Any", _Unusable()),
            registry=PruneRegistry(config=PruneRegistryConfig(uow_factory=FakeUnitOfWorkFactory(uow))),
            request_action=unusable_request,
        )
    )


class TestNamespaceGate:
    async def test_a_namespace_change_since_the_preview_aborts_before_any_group(self):
        rows = [_rom(1)]
        emitted: list[tuple[str, dict[str, Any]]] = []
        executor = _executor(rows, dict(_SETTINGS), emitted)

        await executor.run("run-1", _preview(rows, "a-namespace-from-another-server"), _options())

        completions = [payload for event, payload in emitted if event == "prune_complete"]
        assert len(completions) == 1
        assert completions[0]["success"] is False
        assert completions[0]["results"] == [], "no group may be started under a changed namespace"
        assert "server or user changed" in completions[0]["message"]

    async def test_the_terminal_frame_carries_the_authorizing_preview_id(self):
        rows = [_rom(1)]
        emitted: list[tuple[str, dict[str, Any]]] = []
        executor = _executor(rows, dict(_SETTINGS), emitted)

        await executor.run("run-1", _preview(rows, "changed"), _options())

        assert [payload["preview_id"] for _event, payload in emitted] == ["preview-1"]


class TestAuditTrail:
    async def test_the_run_is_opened_and_closed_in_the_log(self, caplog, monkeypatch):
        rows = [_rom(1)]
        emitted: list[tuple[str, dict[str, Any]]] = []
        settings = dict(_SETTINGS)
        executor = _executor(rows, settings, emitted)
        preview = _preview(rows, romm_namespace(_SETTINGS))
        # Every group refuses at the local re-read, so nothing is probed or changed.
        monkeypatch.setattr(executor._planner, "_registry", _EmptyRegistry())

        with caplog.at_level(logging.INFO, logger="prune-test"):
            await executor.run("run-1", preview, _options())

        messages = [record.message for record in caplog.records]
        assert any("Cleanup run run-1 starting" in message for message in messages)
        assert any("Cleanup run run-1 group 1/1" in message for message in messages)
        assert any("Cleanup run run-1 finished" in message for message in messages)

    async def test_a_group_that_raises_is_reported_as_failed_and_the_run_continues(self, caplog, monkeypatch):
        rows = [_rom(1), _rom(2)]
        emitted: list[tuple[str, dict[str, Any]]] = []
        executor = _executor(rows, dict(_SETTINGS), emitted)
        calls: list[int] = []

        async def exploding_plan(_run_id, initial_rows, *_args):
            calls.append(initial_rows[0].rom_id)
            if initial_rows[0].rom_id == 1:
                raise RuntimeError("planner exploded")
            return {"status": "skipped", "reason": "options_excluded", "message": "nothing", "rom_ids": [2]}

        monkeypatch.setattr(executor._planner, "plan", exploding_plan)

        with caplog.at_level(logging.INFO, logger="prune-test"):
            await executor.run("run-1", _preview(rows, romm_namespace(_SETTINGS)), _options())

        completion = [payload for event, payload in emitted if event == "prune_complete"][-1]
        statuses = [result["status"] for result in completion["results"]]
        assert statuses == ["failed", "skipped"], "one exploding group must not abort the run"
        assert calls == [1, 2]


class _EmptyRegistry:
    """A registry whose groups exist but whose rows are all gone by re-read time."""

    def groups_for_candidates(self, candidate_ids: set[int]):
        return [[_rom(rom_id)] for rom_id in sorted(candidate_ids)]

    def reread_group(self, _rom_id: int):
        return []


@pytest.mark.parametrize("namespace", [romm_namespace(_SETTINGS), "a-namespace-from-another-server"])
async def test_the_liveness_binding_is_released_when_the_run_ends(namespace, monkeypatch):
    rows = [_rom(1)]
    emitted: list[tuple[str, dict[str, Any]]] = []
    executor = _executor(rows, dict(_SETTINGS), emitted)
    monkeypatch.setattr(executor._planner, "_registry", _EmptyRegistry())

    await executor.run("run-1", _preview(rows, namespace), _options())

    assert executor._liveness._run_namespace is None
