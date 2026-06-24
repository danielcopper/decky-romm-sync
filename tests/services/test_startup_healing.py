"""Tests for StartupHealingService."""

from __future__ import annotations

import logging

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_path_exists_reader import FakePathExistsReader
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
    active_core: FakeActiveCoreResolver | None = None,
    disc_resolver: FakeDiscResolver | None = None,
) -> StartupHealingService:
    probe = path_probe if path_probe is not None else FakePathExistsReader(paths={retrodeck_home})
    return StartupHealingService(
        config=StartupHealingServiceConfig(
            logger=logger,
            clock=clock if clock is not None else FakeClock(),
            retrodeck_paths=FakeRetroDeckPaths(home=retrodeck_home),
            path_probe=probe,
            uow_factory=FakeUnitOfWorkFactory(uow) if uow is not None else FakeUnitOfWorkFactory(),
            active_core=active_core if active_core is not None else FakeActiveCoreResolver(),
            disc_resolver=disc_resolver if disc_resolver is not None else FakeDiscResolver(),
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
    def test_no_installs_returns_empty(self, logger):
        """No rom_installs rows → empty list (nothing to reconcile)."""
        uow = FakeUnitOfWork()
        service = _make_service(logger=logger, uow=uow)
        assert service.get_installed_relaunch_options() == []

    def test_skips_install_when_rom_lookup_returns_none(self, logger, monkeypatch):
        """Defensive skip: an install whose ``roms.get`` yields None is dropped.

        The real schema's FK keeps this from happening on disk, so the branch is
        forced by stubbing the lookup rather than by orphaning the install (the
        FK-modelling fake would reject that at commit).
        """
        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path="/roms/n64/a.z64", rom=_make_rom(1, shortcut_app_id=99))
        monkeypatch.setattr(uow.roms, "get", lambda _rom_id: None)
        service = _make_service(logger=logger, uow=uow)
        assert service.get_installed_relaunch_options() == []

    def test_skips_unbound_rom(self, logger):
        """An installed ROM with shortcut_app_id=None (unbound) is skipped."""
        uow = FakeUnitOfWork()
        _seed_install(
            uow,
            1,
            file_path="/roms/n64/a.z64",
            rom=Rom(
                rom_id=1,
                platform_slug="n64",
                name="Game 1",
                fs_name="game_1.z64",
                shortcut_app_id=None,
                last_synced_at="2025-01-01T00:00:00",
            ),
        )
        service = _make_service(logger=logger, uow=uow)
        assert service.get_installed_relaunch_options() == []

    def test_single_installed_bound_default_core(self, logger):
        """Installed+bound, core resolves None → plain flatpak launch command."""
        uow = FakeUnitOfWork()
        file_path = "/roms/n64/zelda.z64"
        _seed_install(uow, 1, file_path=file_path, rom=_make_rom(1, shortcut_app_id=4242))
        service = _make_service(logger=logger, uow=uow)
        items = service.get_installed_relaunch_options()
        assert items == [
            {
                "app_id": 4242,
                "launch_options": f'flatpak run net.retrodeck.retrodeck "{file_path}"',
            }
        ]

    def test_core_override_bakes_e_form(self, logger):
        """A resolved core .so produces the RetroDECK -e override in the command."""
        uow = FakeUnitOfWork()
        file_path = "/roms/n64/mario.z64"
        _seed_install(uow, 1, file_path=file_path, rom=_make_rom(1, shortcut_app_id=7))
        active_core = FakeActiveCoreResolver(per_rom={1: ("mupen64plus_next", "Mupen64Plus-Next")})
        service = _make_service(logger=logger, uow=uow, active_core=active_core)
        items = service.get_installed_relaunch_options()
        assert items == [
            {
                "app_id": 7,
                "launch_options": (
                    "flatpak run net.retrodeck.retrodeck -e "
                    '"%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/mupen64plus_next.so %ROM%" '
                    f'"{file_path}"'
                ),
            }
        ]
        assert active_core.calls == [1]

    def test_multiple_installs_yield_multiple_items(self, logger):
        """Every installed+bound ROM contributes one item, in iteration order."""
        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path="/roms/n64/a.z64", rom=_make_rom(1, shortcut_app_id=11))
        _seed_install(uow, 2, file_path="/roms/n64/b.z64", rom=_make_rom(2, shortcut_app_id=22))
        service = _make_service(logger=logger, uow=uow)
        items = service.get_installed_relaunch_options()
        assert {item["app_id"] for item in items} == {11, 22}
        assert items == [
            {"app_id": 11, "launch_options": 'flatpak run net.retrodeck.retrodeck "/roms/n64/a.z64"'},
            {"app_id": 22, "launch_options": 'flatpak run net.retrodeck.retrodeck "/roms/n64/b.z64"'},
        ]
