"""Tests for StartupHealingService."""

from __future__ import annotations

import logging

import pytest
from fakes.fake_path_exists_reader import FakePathExistsReader
from fakes.fake_relaunch_options_resolver import FakeRelaunchOptionsResolver
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.sync_run import SyncRun
from services.startup_healing import StartupHealingService, StartupHealingServiceConfig

_RETRODECK_HOME = "/run/media/deck/Emulation/retrodeck"


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_startup_healing")


def _make_rom(rom_id: int, *, shortcut_app_id: int | None = None) -> Rom:
    return Rom(
        rom_id=rom_id,
        platform_slug="n64",
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.z64",
        shortcut_app_id=1000 + rom_id if shortcut_app_id is None else shortcut_app_id,
        last_synced_at="2025-01-01T00:00:00",
    )


def _seed_install(
    uow: FakeUnitOfWork,
    rom_id: int,
    *,
    file_path: str,
    rom_dir: str | None = None,
    rom: Rom | None = None,
) -> None:
    """Seed the FK-parent Rom THEN its install record, in one commit.

    *rom* overrides the default ROM identity row.
    """
    install = RomInstall.mark_installed(
        rom_id=rom_id,
        file_path=file_path,
        rom_dir=rom_dir,
        platform_slug="n64",
        system="n64",
        installed_at="2025-01-01T00:00:00",
    )
    with uow:
        uow.roms.save(rom if rom is not None else _make_rom(rom_id))
        uow.rom_installs.save(install)


def _make_service(
    *,
    logger: logging.Logger,
    retrodeck_home: str = _RETRODECK_HOME,
    path_probe: FakePathExistsReader | None = None,
    uow: FakeUnitOfWork | None = None,
    clock: FakeClock | None = None,
    relaunch_options: FakeRelaunchOptionsResolver | None = None,
) -> StartupHealingService:
    probe = path_probe if path_probe is not None else FakePathExistsReader(paths={retrodeck_home})
    return StartupHealingService(
        config=StartupHealingServiceConfig(
            logger=logger,
            clock=clock if clock is not None else FakeClock(),
            retrodeck_paths=FakeRetroDeckPaths(home=retrodeck_home),
            path_probe=probe,
            uow_factory=FakeUnitOfWorkFactory(uow) if uow is not None else FakeUnitOfWorkFactory(),
            relaunch_options=relaunch_options if relaunch_options is not None else FakeRelaunchOptionsResolver(),
        ),
    )


class TestPruneStaleInstalledRoms:
    def test_skip_when_retrodeck_home_missing_on_disk(self, logger, caplog):
        """Guard: retrodeck home not present on disk → skip prune, log info, no UoW write."""
        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path="/run/media/deck/Emulation/retrodeck/roms/n64/a.z64")
        # path_probe knows nothing — retrodeck home not on disk.
        service = _make_service(
            logger=logger,
            path_probe=FakePathExistsReader(),
            uow=uow,
        )
        with caplog.at_level(logging.INFO):
            service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is not None
        assert any("retrodeck home unavailable" in rec.message for rec in caplog.records)

    def test_skip_when_retrodeck_home_unset(self, logger):
        """Empty retrodeck_home (first-run) → skip prune."""
        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path="/somewhere/a.z64")
        service = _make_service(
            logger=logger,
            retrodeck_home="",
            path_probe=FakePathExistsReader(),
            uow=uow,
        )
        service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is not None

    def test_prune_missing_file_path(self, logger):
        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path="/nonexistent/game.z64")
        service = _make_service(logger=logger, uow=uow)
        service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is None
        assert uow.committed is True

    def test_preserve_existing_file_path(self, logger):
        uow = FakeUnitOfWork()
        rom_file = "/run/media/deck/Emulation/retrodeck/roms/n64/game.z64"
        _seed_install(uow, 1, file_path=rom_file)
        probe = FakePathExistsReader(paths={_RETRODECK_HOME, rom_file})
        service = _make_service(logger=logger, path_probe=probe, uow=uow)
        service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is not None

    def test_preserve_via_rom_dir_fallback(self, logger):
        """file_path missing but rom_dir exists → record preserved (PSX multi-file fallback)."""
        uow = FakeUnitOfWork()
        rom_dir = "/run/media/deck/Emulation/retrodeck/roms/psx/FF7"
        _seed_install(uow, 1, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir)  # file gone
        probe = FakePathExistsReader(paths={_RETRODECK_HOME, rom_dir})
        service = _make_service(logger=logger, path_probe=probe, uow=uow)
        service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is not None

    def test_preserve_pending_migration_entry(self, logger, caplog):
        """Install under pending migration's previous home → preserved with info log."""
        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path="/old/retrodeck/roms/n64/zelda.z64")
        with uow:
            uow.kv_config.set("retrodeck_home_path_previous", "/old/retrodeck")
        service = _make_service(logger=logger, uow=uow)
        with caplog.at_level(logging.INFO):
            service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is not None
        assert any("Skipping prune" in rec.message and "/old/retrodeck" in rec.message for rec in caplog.records)

    def test_no_prune_does_not_write(self, logger):
        """When no record is pruned, no write UoW is opened."""
        uow = FakeUnitOfWork()
        # Empty rom_installs — nothing to prune.
        service = _make_service(logger=logger, uow=uow)
        service.prune_stale_installed_roms()
        assert uow.rom_installs.save_count == 0

    def test_mixed_prune_some_preserve_others(self, logger):
        uow = FakeUnitOfWork()
        existing = "/run/media/deck/Emulation/retrodeck/roms/n64/keep.z64"
        _seed_install(uow, 1, file_path=existing)
        _seed_install(uow, 2, file_path="/gone/dead.z64")
        probe = FakePathExistsReader(paths={_RETRODECK_HOME, existing})
        service = _make_service(logger=logger, path_probe=probe, uow=uow)
        service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is not None
        assert uow.rom_installs.get(2) is None
        assert uow.committed is True

    def test_prefix_false_match_not_preserved(self, logger):
        """``pending_home="/foo"`` does NOT preserve ``/foobar/x``."""
        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path="/foobar/x.z64")
        with uow:
            uow.kv_config.set("retrodeck_home_path_previous", "/foo")
        service = _make_service(logger=logger, uow=uow)
        service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is None
        assert uow.committed is True

    def test_pruned_record_drops_only_install_keeps_roms_row(self, logger):
        """RETENTION (ADR-0007): a stale prune drops the ``rom_installs`` row, never the ``roms`` identity row."""
        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path="/gone/dead.z64")
        service = _make_service(logger=logger, uow=uow)
        service.prune_stale_installed_roms()
        assert uow.rom_installs.get(1) is None
        assert uow.roms.get(1) is not None


class TestReconcileOrphanedSyncRuns:
    def _seed_run(self, uow: FakeUnitOfWork, run: SyncRun) -> None:
        with uow:
            uow.sync_runs.save(run)

    def test_running_run_marked_errored(self, logger):
        """A crash-orphaned running run transitions to errored with the restart reason."""
        uow = FakeUnitOfWork()
        clock = FakeClock()
        self._seed_run(
            uow, SyncRun.start(id="run-1", at="2026-01-01T00:00:00+00:00", platforms_planned=2, roms_planned=5)
        )
        service = _make_service(logger=logger, uow=uow, clock=clock)

        service.reconcile_orphaned_sync_runs()

        with uow:
            healed = uow.sync_runs.get("run-1")
        assert healed is not None
        assert healed.status == "errored"
        assert healed.error == "interrupted by restart"
        assert healed.finished_at == clock.now().isoformat()
        assert uow.committed is True

    def test_completed_run_untouched(self, logger):
        """A completed run is terminal — reconciliation leaves it exactly as-is."""
        uow = FakeUnitOfWork()
        run = SyncRun.start(id="run-1", at="2026-01-01T00:00:00+00:00", platforms_planned=1, roms_planned=3)
        run.complete(at="2026-01-01T00:05:00+00:00", platforms=["n64"], collections=[])
        self._seed_run(uow, run)
        service = _make_service(logger=logger, uow=uow)

        service.reconcile_orphaned_sync_runs()

        with uow:
            unchanged = uow.sync_runs.get("run-1")
        assert unchanged is not None
        assert unchanged.status == "completed"
        assert unchanged.error is None
        assert unchanged.finished_at == "2026-01-01T00:05:00+00:00"

    def test_no_running_run_is_noop(self, logger):
        """No running run → nothing to heal, no save."""
        uow = FakeUnitOfWork()
        service = _make_service(logger=logger, uow=uow)

        service.reconcile_orphaned_sync_runs()

        assert uow.sync_runs.save_count == 0


class TestGetInstalledRelaunchOptions:
    """The callable delegates to the shared relaunch-options resolver.

    The deep resolution behavior (skip rules, core/disc baking) is owned by
    ``test_relaunch_options_resolver.py``; here we pin only that the service
    forwards the resolver's list through unchanged and queries it once.
    """

    def test_delegates_to_resolver(self, logger):
        """Returns the resolver's items verbatim and queries the seam once."""
        items = [
            {"app_id": 11, "launch_options": 'flatpak run net.retrodeck.retrodeck "/roms/n64/a.z64"'},
            {"app_id": 22, "launch_options": 'flatpak run net.retrodeck.retrodeck "/roms/n64/b.z64"'},
        ]
        relaunch_options = FakeRelaunchOptionsResolver(items=items)
        service = _make_service(logger=logger, relaunch_options=relaunch_options)
        result = service.get_installed_relaunch_options()
        assert result == items
        assert relaunch_options.calls == 1

    def test_empty_resolver_yields_empty_list(self, logger):
        """An empty resolver list passes straight through."""
        relaunch_options = FakeRelaunchOptionsResolver(items=[])
        service = _make_service(logger=logger, relaunch_options=relaunch_options)
        assert service.get_installed_relaunch_options() == []
        assert relaunch_options.calls == 1
