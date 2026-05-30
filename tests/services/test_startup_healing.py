"""Tests for StartupHealingService."""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import MagicMock

import pytest
from fakes.fake_path_exists_reader import FakePathExistsReader
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock
from models.state import InstalledRomEntry, PluginState, make_default_plugin_state

from domain.sync_run import SyncRun
from services.startup_healing import StartupHealingService, StartupHealingServiceConfig

_RETRODECK_HOME = "/run/media/deck/Emulation/retrodeck"


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_startup_healing")


@pytest.fixture
def state_persister() -> MagicMock:
    return MagicMock()


def _make_state() -> PluginState:
    return make_default_plugin_state()


def _installed(**fields: object) -> InstalledRomEntry:
    """Build a partial InstalledRomEntry for tests that intentionally probe sparse shapes."""
    return cast("InstalledRomEntry", dict(fields))


def _make_service(
    *,
    state: PluginState,
    logger: logging.Logger,
    state_persister: MagicMock,
    retrodeck_home: str = _RETRODECK_HOME,
    path_probe: FakePathExistsReader | None = None,
    uow: FakeUnitOfWork | None = None,
    clock: FakeClock | None = None,
) -> StartupHealingService:
    probe = path_probe if path_probe is not None else FakePathExistsReader(paths={retrodeck_home})
    return StartupHealingService(
        config=StartupHealingServiceConfig(
            state=state,
            logger=logger,
            clock=clock if clock is not None else FakeClock(),
            state_persister=state_persister,
            retrodeck_paths=FakeRetroDeckPaths(home=retrodeck_home),
            path_probe=probe,
            uow_factory=FakeUnitOfWorkFactory(uow) if uow is not None else FakeUnitOfWorkFactory(),
        ),
    )


class TestPruneStaleInstalledRoms:
    def test_skip_when_retrodeck_home_missing_on_disk(self, logger, state_persister, caplog):
        """Guard: retrodeck home not present on disk → skip prune, log info."""
        state = _make_state()
        state["installed_roms"] = {
            "1": _installed(rom_id=1, file_path="/run/media/deck/Emulation/retrodeck/roms/n64/a.z64"),
        }
        # path_probe knows nothing — retrodeck home not on disk.
        service = _make_service(
            state=state,
            logger=logger,
            state_persister=state_persister,
            path_probe=FakePathExistsReader(),
        )
        with caplog.at_level(logging.INFO):
            service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        state_persister.save_state.assert_not_called()
        assert any("retrodeck home unavailable" in rec.message for rec in caplog.records)

    def test_skip_when_retrodeck_home_unset(self, logger, state_persister):
        """Empty retrodeck_home (first-run) → skip prune."""
        state = _make_state()
        state["installed_roms"] = {
            "1": _installed(rom_id=1, file_path="/somewhere/a.z64"),
        }
        service = _make_service(
            state=state,
            logger=logger,
            state_persister=state_persister,
            retrodeck_home="",
            path_probe=FakePathExistsReader(),
        )
        service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        state_persister.save_state.assert_not_called()

    def test_prune_missing_file_path(self, logger, state_persister):
        state = _make_state()
        state["installed_roms"] = {
            "1": _installed(rom_id=1, file_path="/nonexistent/game.z64"),
        }
        service = _make_service(state=state, logger=logger, state_persister=state_persister)
        service.prune_stale_installed_roms()
        assert "1" not in state["installed_roms"]
        state_persister.save_state.assert_called_once()

    def test_preserve_existing_file_path(self, logger, state_persister):
        state = _make_state()
        rom_file = "/run/media/deck/Emulation/retrodeck/roms/n64/game.z64"
        state["installed_roms"] = {"1": _installed(rom_id=1, file_path=rom_file)}
        probe = FakePathExistsReader(paths={_RETRODECK_HOME, rom_file})
        service = _make_service(state=state, logger=logger, state_persister=state_persister, path_probe=probe)
        service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        state_persister.save_state.assert_not_called()

    def test_preserve_via_rom_dir_fallback(self, logger, state_persister):
        """file_path missing but rom_dir exists → entry preserved (PSX multi-file fallback)."""
        state = _make_state()
        rom_dir = "/run/media/deck/Emulation/retrodeck/roms/psx/FF7"
        state["installed_roms"] = {
            "1": _installed(
                rom_id=1,
                file_path=f"{rom_dir}/FF7.m3u",  # file gone
                rom_dir=rom_dir,
            ),
        }
        probe = FakePathExistsReader(paths={_RETRODECK_HOME, rom_dir})
        service = _make_service(state=state, logger=logger, state_persister=state_persister, path_probe=probe)
        service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        state_persister.save_state.assert_not_called()

    def test_preserve_pending_migration_entry(self, logger, state_persister, caplog):
        """Entry under pending migration's previous home → preserved with info log."""
        state = _make_state()
        state["retrodeck_home_path_previous"] = "/old/retrodeck"
        state["installed_roms"] = {
            "1": _installed(rom_id=1, file_path="/old/retrodeck/roms/n64/zelda.z64"),
        }
        service = _make_service(state=state, logger=logger, state_persister=state_persister)
        with caplog.at_level(logging.INFO):
            service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        state_persister.save_state.assert_not_called()
        assert any("Skipping prune" in rec.message and "/old/retrodeck" in rec.message for rec in caplog.records)

    def test_no_prune_does_not_save(self, logger, state_persister):
        """When no entry is pruned, state_persister is not invoked."""
        state = _make_state()
        # Empty installed_roms — nothing to prune.
        service = _make_service(state=state, logger=logger, state_persister=state_persister)
        service.prune_stale_installed_roms()
        state_persister.save_state.assert_not_called()

    def test_mixed_prune_some_preserve_others(self, logger, state_persister):
        state = _make_state()
        existing = "/run/media/deck/Emulation/retrodeck/roms/n64/keep.z64"
        state["installed_roms"] = {
            "1": _installed(rom_id=1, file_path=existing),
            "2": _installed(rom_id=2, file_path="/gone/dead.z64"),
        }
        probe = FakePathExistsReader(paths={_RETRODECK_HOME, existing})
        service = _make_service(state=state, logger=logger, state_persister=state_persister, path_probe=probe)
        service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        assert "2" not in state["installed_roms"]
        state_persister.save_state.assert_called_once()

    def test_prefix_false_match_not_preserved(self, logger, state_persister):
        """``pending_home="/foo"`` does NOT preserve ``/foobar/x``."""
        state = _make_state()
        state["retrodeck_home_path_previous"] = "/foo"
        state["installed_roms"] = {
            "1": _installed(rom_id=1, file_path="/foobar/x.z64"),
        }
        service = _make_service(state=state, logger=logger, state_persister=state_persister)
        service.prune_stale_installed_roms()
        assert "1" not in state["installed_roms"]
        state_persister.save_state.assert_called_once()


class TestReconcileOrphanedSyncRuns:
    def _seed_run(self, uow: FakeUnitOfWork, run: SyncRun) -> None:
        with uow:
            uow.sync_runs.save(run)

    def test_running_run_marked_errored(self, logger, state_persister):
        """A crash-orphaned running run transitions to errored with the restart reason."""
        uow = FakeUnitOfWork()
        clock = FakeClock()
        self._seed_run(
            uow, SyncRun.start(id="run-1", at="2026-01-01T00:00:00+00:00", platforms_planned=2, roms_planned=5)
        )
        service = _make_service(
            state=_make_state(), logger=logger, state_persister=state_persister, uow=uow, clock=clock
        )

        service.reconcile_orphaned_sync_runs()

        with uow:
            healed = uow.sync_runs.get("run-1")
        assert healed is not None
        assert healed.status == "errored"
        assert healed.error == "interrupted by restart"
        assert healed.finished_at == clock.now().isoformat()
        assert uow.committed is True

    def test_completed_run_untouched(self, logger, state_persister):
        """A completed run is terminal — reconciliation leaves it exactly as-is."""
        uow = FakeUnitOfWork()
        run = SyncRun.start(id="run-1", at="2026-01-01T00:00:00+00:00", platforms_planned=1, roms_planned=3)
        run.complete(at="2026-01-01T00:05:00+00:00", platforms=["n64"], collections=[])
        self._seed_run(uow, run)
        service = _make_service(state=_make_state(), logger=logger, state_persister=state_persister, uow=uow)

        service.reconcile_orphaned_sync_runs()

        with uow:
            unchanged = uow.sync_runs.get("run-1")
        assert unchanged is not None
        assert unchanged.status == "completed"
        assert unchanged.error is None
        assert unchanged.finished_at == "2026-01-01T00:05:00+00:00"

    def test_no_running_run_is_noop(self, logger, state_persister):
        """No running run → nothing to heal, no save."""
        uow = FakeUnitOfWork()
        service = _make_service(state=_make_state(), logger=logger, state_persister=state_persister, uow=uow)

        service.reconcile_orphaned_sync_runs()

        assert uow.sync_runs.save_count == 0
