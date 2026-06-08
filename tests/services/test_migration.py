import asyncio
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import _make_testable_plugin
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_migration_file_store import FakeMigrationFileStore
from fakes.fake_platform_core_reader import FakePlatformCoreReader
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_settings_persister import FakeSettingsPersister
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.library_peers import FakeArtworkManager
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.firmware_file import FirmwareFileAdapter
from adapters.migration_file import MigrationFileAdapter
from adapters.persistence import PersistenceAdapter
from adapters.steam_config import SteamConfigAdapter
from services.active_core_resolver import ActiveCoreResolver, ActiveCoreResolverConfig
from services.firmware import FirmwareService, FirmwareServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.migration import MigrationService, MigrationServiceConfig


class RecordingEmitter:
    """Append-only emit recorder usable as an ``EventEmitter``.

    Stores ``(event_name, args)`` tuples in ``calls`` so tests can assert
    on the observable emit contract without resorting to ``MagicMock``.
    The call signature mirrors the ``EventEmitter`` Protocol exactly so
    basedpyright accepts the fake wherever ``EventEmitter`` is expected.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def __call__(self, event: str, /, *args: object) -> None:
        self.calls.append((event, args))


@pytest.fixture
def plugin(tmp_path, fake_romm_api):
    p = _make_testable_plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()

    import decky

    p._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._romm_api = fake_romm_api
    p._settings_persister = FakeSettingsPersister()

    # ONE shared FakeUnitOfWork the migration service reads/writes and tests
    # seed/assert against. Markers (retrodeck_home_path*, save_sort_settings*)
    # and install records both live in this unit now.
    uow = FakeUnitOfWork()
    p._uow = uow
    # Shared core-info fake so a relaunch test can seed ``available_cores`` and
    # assert a per-game emulator_override re-bakes the ``-e`` form post-move. The
    # real ActiveCoreResolver folds the DB override over this fake's es_systems
    # default — the seam MigrationService re-bakes through on relocation.
    p._core_info = FakeCoreInfoProvider()
    p._active_core = ActiveCoreResolver(
        config=ActiveCoreResolverConfig(
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
            core_info=p._core_info,
            platform_core_reader=FakePlatformCoreReader(),
            resolve_system=lambda platform_slug, platform_fs_slug=None: platform_slug,
            logger=decky.logger,
        ),
    )
    p._firmware_service = FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=fake_romm_api,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            firmware_file_store=FirmwareFileAdapter(),
            retrodeck_paths=FakeRetroDeckPaths(),
            core_info=FakeCoreInfoProvider(),
            resolve_system=lambda platform_slug, platform_fs_slug=None: platform_slug,
            uow_factory=FakeUnitOfWorkFactory(),
        ),
    )
    p._firmware_service.load_bios_registry()

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=fake_romm_api,
            steam_config=steam_config,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            settings_persister=p._settings_persister,
            log_debug=p._log_debug,
            artwork=FakeArtworkManager(),
            uow_factory=FakeUnitOfWorkFactory(),
            active_core=p._active_core,
        ),
    )

    def _no_core_name(core_so: str) -> str | None:
        return None

    def _default_save_sorting() -> tuple[bool, bool]:
        return (True, False)

    p._migration_service = MigrationService(
        config=MigrationServiceConfig(
            migration_file_store=MigrationFileAdapter(),
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            settings_persister=p._settings_persister,
            emit=RecordingEmitter(),
            get_bios_files_index=lambda: p._firmware_service.bios_files_index,
            retrodeck_paths=FakeRetroDeckPaths(),
            get_retroarch_save_sorting=_default_save_sorting,
            active_core=p._active_core,
            get_core_name=_no_core_name,
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
        ),
    )
    return p


def _seed_install(uow, rom_id, *, file_path, rom_dir=None, system="n64", platform_slug="", app_id=None):
    """Seed a Rom (FK parent) then its RomInstall into the shared fake UoW.

    ``rom_dir`` defaults to ``None`` (single-file ROM); pass a dedicated
    directory for a folder-backed (multi-file) ROM. ``app_id`` defaults to
    ``None`` (unbound ROM); pass an int to seed a bound shortcut so the
    re-resolve step picks the install up.
    """
    from domain.rom import Rom
    from domain.rom_install import RomInstall

    with uow:
        uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug=platform_slug or system,
                name=f"Game {rom_id}",
                fs_name=f"game{rom_id}",
                shortcut_app_id=app_id,
                last_synced_at="2025-01-01T00:00:00",
            )
        )
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=rom_dir,
                platform_slug=platform_slug,
                system=system,
                installed_at="2025-01-01T00:00:00",
            )
        )


def _seed_bios(uow, *, platform_slug, file_name, file_path, firmware_id=None):
    """Seed a ``BiosFile`` into the shared fake UoW the way FirmwareService writes it.

    ``bios_files`` has no FK onto ``roms``, so no parent row is required (unlike
    ``_seed_install``). Mirrors ``FirmwareService._download_firmware_post_io``'s
    ``uow.bios_files.save(BiosFile.mark_downloaded(...))`` write.
    """
    from domain.bios_file import BiosFile

    with uow:
        uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug=platform_slug,
                file_name=file_name,
                file_path=file_path,
                downloaded_at="2025-01-01T00:00:00",
                firmware_id=firmware_id,
            )
        )


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop and migration service loop match the running event loop."""
    loop = asyncio.get_event_loop()
    plugin.loop = loop
    plugin._migration_service._loop = loop


class _RecordingLoop:
    """Drop-in loop substitute that captures and immediately closes scheduled coroutines.

    Mirrors what the original tests built ad-hoc with ``MagicMock`` for
    ``loop.create_task``: schedule receives the coroutine and stores it
    (closing it so no pending-task warning fires), and the count is
    inspectable via ``len(tasks)``. Use this when a test wants to assert
    *whether* a coroutine was scheduled without actually pumping the
    event loop.
    """

    def __init__(self) -> None:
        self.tasks: list[object] = []

    def create_task(self, coro):
        coro.close()
        self.tasks.append(coro)
        return


class TestPathChangeDetection:
    def test_first_run_stores_path(self, plugin, tmp_path):
        """First run (empty stored path) stores current path, no event."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        loop = _RecordingLoop()
        plugin._migration_service._loop = loop

        fake_home = str(tmp_path / "retrodeck")
        os.makedirs(fake_home, exist_ok=True)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=fake_home)
        plugin._migration_service.detect_retrodeck_path_change()

        with plugin._uow as uow:
            assert uow.kv_config.get("retrodeck_home_path") == fake_home
        # No event emitted on first run
        assert loop.tasks == []
        assert plugin._migration_service._emit.calls == []

    def test_no_change_no_notification(self, plugin, tmp_path):
        """Same path as stored — no event, no state change."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        fake_home = str(tmp_path / "retrodeck")
        os.makedirs(fake_home, exist_ok=True)
        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path", fake_home)
        loop = _RecordingLoop()
        plugin._migration_service._loop = loop

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=fake_home)
        plugin._migration_service.detect_retrodeck_path_change()

        assert loop.tasks == []
        assert plugin._migration_service._emit.calls == []

    async def test_path_change_emits_event(self, plugin, tmp_path):
        """Path changed — stores both old and new, emits event."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old_retrodeck")
        new_home = str(tmp_path / "new_retrodeck")
        os.makedirs(new_home, exist_ok=True)

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path", old_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=new_home)
        plugin._migration_service.detect_retrodeck_path_change()

        # ``create_task`` schedules the emit coroutine on the running loop —
        # yield once so the scheduled coroutine runs and the emitter records.
        await asyncio.sleep(0)

        with plugin._uow as uow:
            assert uow.kv_config.get("retrodeck_home_path") == new_home
            assert uow.kv_config.get("retrodeck_home_path_previous") == old_home

        emit_calls = plugin._migration_service._emit.calls
        assert len(emit_calls) == 1
        event, args = emit_calls[0]
        assert event == "retrodeck_path_changed"
        payload = args[0]
        assert isinstance(payload, dict)
        assert payload["old_path"] == old_home
        assert payload["new_path"] == new_home
        # Path-change emit does NOT carry ``cleared`` — only the auto-clear emit does.
        assert "cleared" not in payload

    def test_empty_current_home_no_action(self, plugin, tmp_path):
        """If ``retrodeck_paths`` returns empty string, do nothing."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)

        loop = _RecordingLoop()
        plugin._migration_service._loop = loop

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home="")
        plugin._migration_service.detect_retrodeck_path_change()

        assert loop.tasks == []
        assert plugin._migration_service._emit.calls == []
        with plugin._uow as uow:
            assert uow.kv_config.get("retrodeck_home_path") is None

    async def test_detect_path_change_auto_clears_when_reverted_to_previous(self, plugin, tmp_path):
        """User reverted RetroDECK to the previous home — drop the marker, emit cleared event."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old_retrodeck")
        new_home = str(tmp_path / "new_retrodeck")
        os.makedirs(old_home, exist_ok=True)

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path", new_home)
            uow.kv_config.set("retrodeck_home_path_previous", old_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=old_home)
        plugin._migration_service.detect_retrodeck_path_change()

        # ``create_task`` schedules the emit coroutine on the running loop —
        # yield once so the scheduled coroutine runs and the emitter records.
        await asyncio.sleep(0)

        with plugin._uow as uow:
            assert uow.kv_config.get("retrodeck_home_path") == old_home
            assert uow.kv_config.get("retrodeck_home_path_previous") is None

        emit_calls = plugin._migration_service._emit.calls
        assert len(emit_calls) == 1
        event, args = emit_calls[0]
        assert event == "retrodeck_path_changed"
        payload = args[0]
        assert isinstance(payload, dict)
        assert payload["cleared"] is True
        assert payload["old_path"] == old_home
        assert payload["new_path"] == old_home

    async def test_detect_path_change_auto_clear_emits_cleared_event(self, plugin, tmp_path):
        """Auto-clear MUST emit retrodeck_path_changed with cleared=True so the
        frontend listener can dismiss any pending migration UI."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old_retrodeck")
        new_home = str(tmp_path / "new_retrodeck")
        os.makedirs(old_home, exist_ok=True)

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path", new_home)
            uow.kv_config.set("retrodeck_home_path_previous", old_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=old_home)
        plugin._migration_service.detect_retrodeck_path_change()

        # ``create_task`` schedules the emit coroutine on the running loop —
        # yield once so the scheduled coroutine runs and the emitter records.
        await asyncio.sleep(0)

        emit_calls = plugin._migration_service._emit.calls
        assert len(emit_calls) == 1
        event, args = emit_calls[0]
        assert event == "retrodeck_path_changed"
        payload = args[0]
        assert isinstance(payload, dict)
        assert payload["cleared"] is True
        assert payload["old_path"] == old_home
        assert payload["new_path"] == old_home


class TestIsRetroDeckMigrationPending:
    def test_is_retrodeck_migration_pending_returns_false_when_unset(self, plugin):
        with plugin._uow as uow:
            uow.kv_config.delete("retrodeck_home_path_previous")
        assert plugin._migration_service.is_retrodeck_migration_pending() is False

    def test_is_retrodeck_migration_pending_returns_true_when_set(self, plugin):
        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", "/some/old/path")
        assert plugin._migration_service.is_retrodeck_migration_pending() is True

    def test_is_retrodeck_migration_pending_returns_false_for_empty_string(self, plugin):
        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", "")
        assert plugin._migration_service.is_retrodeck_migration_pending() is False


class TestDismissRetroDeckMigration:
    def test_dismiss_retrodeck_migration_clears_marker(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", "/old/path")

        result = plugin._migration_service.dismiss_retrodeck_migration()

        assert result == {"success": True}
        with plugin._uow as uow:
            assert uow.kv_config.get("retrodeck_home_path_previous") is None

    def test_dismiss_retrodeck_migration_idempotent_when_no_marker(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        with plugin._uow as uow:
            uow.kv_config.delete("retrodeck_home_path_previous")

        result = plugin._migration_service.dismiss_retrodeck_migration()

        assert result == {"success": True}
        with plugin._uow as uow:
            assert uow.kv_config.get("retrodeck_home_path_previous") is None


class TestMigrateRetroDeckFiles:
    @pytest.mark.asyncio
    async def test_no_migration_needed(self, plugin, tmp_path):
        """No previous path — nothing to migrate."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is False
        assert "No path migration needed" in result["message"]

    @pytest.mark.asyncio
    async def test_migrate_roms(self, plugin, tmp_path):
        """Moves ROM files from old to new path, updates state."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "n64", "zelda.z64")
        new_rom = os.path.join(new_home, "roms", "n64", "zelda.z64")

        os.makedirs(os.path.dirname(old_rom))
        with open(old_rom, "w") as f:
            f.write("rom data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="n64")

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True
        assert result["roms_moved"] == 1
        assert os.path.exists(new_rom)
        assert not os.path.exists(old_rom)
        assert plugin._uow.committed is True
        with plugin._uow as uow:
            install = uow.rom_installs.get(1)
            assert install.file_path == new_rom
            # Single-file ROM owns no folder before or after migration.
            assert install.rom_dir is None

    @pytest.mark.asyncio
    async def test_migrate_multi_file_moves_whole_rom_dir_with_siblings(self, plugin, tmp_path):
        """Regression (#784 data-loss): a multi-file ROM moves its WHOLE rom_dir.

        The launch file (an auto-generated ``.m3u``) sits directly in the
        dedicated extract dir, so ``dirname(file_path) == rom_dir`` exactly as
        for a single-file ROM. The old path-shape heuristic moved only the
        launch file and orphaned the sibling disc files. With the rom_dir model
        the whole directory migrates as a unit — every sibling (here
        ``disc2.bin``) must land at the new location.
        """
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom_dir = os.path.join(old_home, "roms", "psx", "FF7")
        new_rom_dir = os.path.join(new_home, "roms", "psx", "FF7")
        old_launch = os.path.join(old_rom_dir, "FF7.m3u")
        new_launch = os.path.join(new_rom_dir, "FF7.m3u")
        old_disc2 = os.path.join(old_rom_dir, "disc2.bin")
        new_disc2 = os.path.join(new_rom_dir, "disc2.bin")

        os.makedirs(old_rom_dir)
        with open(old_launch, "w") as f:
            f.write("disc1.bin\ndisc2.bin\n")
        with open(os.path.join(old_rom_dir, "disc1.bin"), "w") as f:
            f.write("disc1 data")
        with open(old_disc2, "w") as f:
            f.write("disc2 data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_launch, rom_dir=old_rom_dir, system="psx", platform_slug="psx")

        result = await plugin.migrate_retrodeck_files()

        assert result["success"] is True
        # One ROM moved — the moved directory counts as a single ROM.
        assert result["roms_moved"] == 1
        # The whole directory moved: launch file AND the sibling disc travelled.
        assert os.path.exists(new_launch)
        assert os.path.exists(new_disc2)
        assert not os.path.exists(old_rom_dir)
        # The sibling disc the data-loss bug orphaned is at the new location.
        with open(new_disc2) as f:
            assert f.read() == "disc2 data"
        assert plugin._uow.committed is True
        with plugin._uow as uow:
            install = uow.rom_installs.get(1)
            assert install.rom_dir == new_rom_dir
            assert install.file_path == new_launch

    @pytest.mark.asyncio
    async def test_migrate_single_file_moves_only_the_file(self, plugin, tmp_path):
        """A single-file ROM (``rom_dir`` is ``None``) moves only its launch file.

        Sibling ROMs sharing the platform's flat ``<roms>/<system>`` directory
        must NOT be dragged along — only this ROM's file moves.
        """
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_dir = os.path.join(old_home, "roms", "n64")
        old_rom = os.path.join(old_dir, "zelda.z64")
        new_rom = os.path.join(new_home, "roms", "n64", "zelda.z64")
        sibling = os.path.join(old_dir, "mario.z64")

        os.makedirs(old_dir)
        with open(old_rom, "w") as f:
            f.write("zelda")
        with open(sibling, "w") as f:
            f.write("mario")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, rom_dir=None, system="n64")

        result = await plugin.migrate_retrodeck_files()

        assert result["success"] is True
        assert result["roms_moved"] == 1
        assert os.path.exists(new_rom)
        assert not os.path.exists(old_rom)
        # The unrelated sibling ROM stays in the old shared dir — not dragged along.
        assert os.path.exists(sibling)
        with plugin._uow as uow:
            install = uow.rom_installs.get(1)
            assert install.file_path == new_rom
            assert install.rom_dir is None

    @pytest.mark.asyncio
    async def test_migrate_bios(self, plugin, tmp_path):
        """Moves tracked BIOS files from old to new path."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_bios = os.path.join(old_home, "bios", "scph5501.bin")
        new_bios = os.path.join(new_home, "bios", "scph5501.bin")

        os.makedirs(os.path.dirname(old_bios))
        with open(old_bios, "w") as f:
            f.write("bios data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_bios(plugin._uow, platform_slug="psx", file_name="scph5501.bin", file_path=old_bios, firmware_id=42)

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True
        assert result["bios_moved"] == 1
        # File physically moved to the new RetroDECK home.
        assert os.path.exists(new_bios)
        # Persisted BiosFile.file_path updated in SQLite, and the write committed.
        with plugin._uow as uow:
            persisted = uow.bios_files.get("psx", "scph5501.bin")
            assert persisted is not None
            assert persisted.file_path == new_bios
        assert plugin._uow.committed is True

    @pytest.mark.asyncio
    async def test_migrate_conflicts_need_confirmation(self, plugin, tmp_path):
        """Destination file already exists — first call returns conflicts for user decision."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "n64", "zelda.z64")
        new_rom = os.path.join(new_home, "roms", "n64", "zelda.z64")

        os.makedirs(os.path.dirname(old_rom))
        os.makedirs(os.path.dirname(new_rom))
        with open(old_rom, "w") as f:
            f.write("old data")
        with open(new_rom, "w") as f:
            f.write("new data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="n64")

        # First call with no strategy returns conflicts
        result = await plugin.migrate_retrodeck_files()
        assert result["needs_confirmation"] is True
        assert result["conflict_count"] == 1
        assert "zelda.z64" in result["conflicts"]
        # Nothing moved yet
        with open(new_rom) as f:
            assert f.read() == "new data"
        with open(old_rom) as f:
            assert f.read() == "old data"

    @pytest.mark.asyncio
    async def test_migrate_conflict_overwrite(self, plugin, tmp_path):
        """Overwrite strategy replaces destination with source."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "n64", "zelda.z64")
        new_rom = os.path.join(new_home, "roms", "n64", "zelda.z64")

        os.makedirs(os.path.dirname(old_rom))
        os.makedirs(os.path.dirname(new_rom))
        with open(old_rom, "w") as f:
            f.write("old data")
        with open(new_rom, "w") as f:
            f.write("new data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="n64")

        result = await plugin.migrate_retrodeck_files("overwrite")
        assert result["success"] is True
        assert result["roms_moved"] == 1
        with open(new_rom) as f:
            assert f.read() == "old data"
        with plugin._uow as uow:
            assert uow.rom_installs.get(1).file_path == new_rom

    @pytest.mark.asyncio
    async def test_migrate_conflict_skip(self, plugin, tmp_path):
        """Skip strategy keeps destination file, updates state path."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "n64", "zelda.z64")
        new_rom = os.path.join(new_home, "roms", "n64", "zelda.z64")

        os.makedirs(os.path.dirname(old_rom))
        os.makedirs(os.path.dirname(new_rom))
        with open(old_rom, "w") as f:
            f.write("old data")
        with open(new_rom, "w") as f:
            f.write("new data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="n64")

        result = await plugin.migrate_retrodeck_files("skip")
        assert result["success"] is True
        assert result["roms_moved"] == 1
        # Destination file preserved
        with open(new_rom) as f:
            assert f.read() == "new data"
        # Install record updated to new path
        with plugin._uow as uow:
            assert uow.rom_installs.get(1).file_path == new_rom

    @pytest.mark.asyncio
    async def test_migrate_source_missing(self, plugin, tmp_path):
        """Source file gone — skip silently."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=os.path.join(old_home, "roms", "n64", "gone.z64"), system="n64")

        result = await plugin.migrate_retrodeck_files()
        assert result["roms_moved"] == 0
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_migrate_creates_subdirs(self, plugin, tmp_path):
        """Target subdirectories are created as needed."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_bios = os.path.join(old_home, "bios", "dc", "dc_boot.bin")

        os.makedirs(os.path.dirname(old_bios))
        with open(old_bios, "w") as f:
            f.write("bios")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_bios(plugin._uow, platform_slug="dc", file_name="dc_boot.bin", file_path=old_bios, firmware_id=7)

        result = await plugin.migrate_retrodeck_files()
        assert result["bios_moved"] == 1
        new_bios = os.path.join(new_home, "bios", "dc", "dc_boot.bin")
        assert os.path.exists(new_bios)

    @pytest.mark.asyncio
    async def test_clears_previous_on_success(self, plugin, tmp_path):
        """After successful migration, retrodeck_home_path_previous is cleared."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        # No files to move — success with 0 moved

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True
        with plugin._uow as uow:
            assert uow.kv_config.get("retrodeck_home_path_previous") is None


class TestMigrateSaveFiles:
    """Tests for save file migration."""

    @pytest.mark.asyncio
    async def test_migrate_saves(self, plugin, tmp_path):
        """Save files are moved from old to new saves directory."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_save = os.path.join(old_home, "saves", "gba", "game.srm")
        new_save = os.path.join(new_home, "saves", "gba", "game.srm")

        os.makedirs(os.path.dirname(old_save))
        with open(old_save, "w") as f:
            f.write("save data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(saves=os.path.join(new_home, "saves"))
        result = await plugin.migrate_retrodeck_files()

        assert result["success"] is True
        assert result["saves_moved"] == 1
        assert os.path.exists(new_save)
        assert not os.path.exists(old_save)
        with open(new_save) as f:
            assert f.read() == "save data"

    @pytest.mark.asyncio
    async def test_save_conflict_needs_confirmation(self, plugin, tmp_path):
        """Save files at both locations trigger conflict confirmation."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_save = os.path.join(old_home, "saves", "gba", "game.srm")
        new_save = os.path.join(new_home, "saves", "gba", "game.srm")

        os.makedirs(os.path.dirname(old_save))
        os.makedirs(os.path.dirname(new_save))
        with open(old_save, "w") as f:
            f.write("old save")
        with open(new_save, "w") as f:
            f.write("new save")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(saves=os.path.join(new_home, "saves"))
        result = await plugin.migrate_retrodeck_files()

        assert result["needs_confirmation"] is True
        assert result["conflict_count"] == 1
        assert "gba/game.srm" in result["conflicts"]

    @pytest.mark.asyncio
    async def test_save_conflict_overwrite(self, plugin, tmp_path):
        """Overwrite strategy replaces destination save with source."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_save = os.path.join(old_home, "saves", "gba", "game.srm")
        new_save = os.path.join(new_home, "saves", "gba", "game.srm")

        os.makedirs(os.path.dirname(old_save))
        os.makedirs(os.path.dirname(new_save))
        with open(old_save, "w") as f:
            f.write("old save")
        with open(new_save, "w") as f:
            f.write("new save")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(saves=os.path.join(new_home, "saves"))
        result = await plugin.migrate_retrodeck_files("overwrite")

        assert result["success"] is True
        assert result["saves_moved"] == 1
        with open(new_save) as f:
            assert f.read() == "old save"

    @pytest.mark.asyncio
    async def test_save_conflict_skip(self, plugin, tmp_path):
        """Skip strategy keeps destination save file."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_save = os.path.join(old_home, "saves", "gba", "game.srm")
        new_save = os.path.join(new_home, "saves", "gba", "game.srm")

        os.makedirs(os.path.dirname(old_save))
        os.makedirs(os.path.dirname(new_save))
        with open(old_save, "w") as f:
            f.write("old save")
        with open(new_save, "w") as f:
            f.write("new save")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(saves=os.path.join(new_home, "saves"))
        result = await plugin.migrate_retrodeck_files("skip")

        assert result["success"] is True
        assert result["saves_moved"] == 1
        with open(new_save) as f:
            assert f.read() == "new save"

    @pytest.mark.asyncio
    async def test_hidden_dirs_skipped(self, plugin, tmp_path):
        """Hidden directories like .romm-backup are not migrated."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_save = os.path.join(old_home, "saves", "gba", "game.srm")
        old_backup = os.path.join(old_home, "saves", "gba", ".romm-backup", "game_old.srm")

        os.makedirs(os.path.dirname(old_save))
        os.makedirs(os.path.dirname(old_backup))
        with open(old_save, "w") as f:
            f.write("save data")
        with open(old_backup, "w") as f:
            f.write("backup data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(saves=os.path.join(new_home, "saves"))
        result = await plugin.migrate_retrodeck_files()

        assert result["saves_moved"] == 1  # only the real save, not the backup

    @pytest.mark.asyncio
    async def test_status_includes_saves_count(self, plugin, tmp_path):
        """get_migration_status includes saves_count."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_save = os.path.join(old_home, "saves", "gba", "game.srm")

        os.makedirs(os.path.dirname(old_save))
        with open(old_save, "w") as f:
            f.write("save data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(saves=os.path.join(new_home, "saves"))
        status = await plugin.get_migration_status()

        assert status["pending"] is True
        assert status["saves_count"] == 1

    @pytest.mark.asyncio
    async def test_status_counts_tracked_bios_from_sqlite(self, plugin, tmp_path):
        """get_migration_status counts tracked BIOS from the SQLite ``BiosFile`` snapshot."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_bios = os.path.join(old_home, "bios", "scph5501.bin")

        os.makedirs(os.path.dirname(old_bios))
        with open(old_bios, "w") as f:
            f.write("bios data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_bios(plugin._uow, platform_slug="psx", file_name="scph5501.bin", file_path=old_bios, firmware_id=42)

        status = await plugin.get_migration_status()

        assert status["pending"] is True
        assert status["bios_count"] == 1


class TestMigrationRelaunchOptions:
    """Re-resolve step: after a RetroDECK-home migration relocates ROM files and
    updates ``rom_installs`` to the new paths, the service emits
    ``migration_relaunch_options`` so the frontend rewrites each affected Steam
    shortcut's baked ``launch_options`` (ADR-0005). Only ROMs that are BOTH
    installed (have a ``rom_installs`` row) AND bound (``shortcut_app_id`` set)
    are eligible — uninstalled or unbound ROMs are skipped.
    """

    @staticmethod
    def _relaunch_emit(plugin):
        """Return the single ``migration_relaunch_options`` payload, or ``None``."""
        for event, args in plugin._migration_service._emit.calls:
            if event == "migration_relaunch_options":
                return args[0]
        return None

    @staticmethod
    def _seed_bound_uninstalled(uow, rom_id, *, app_id, system="n64"):
        """Seed a bound Rom row with NO ``rom_installs`` row (downloaded=false)."""
        from domain.rom import Rom

        with uow:
            uow.roms.save(
                Rom(
                    rom_id=rom_id,
                    platform_slug=system,
                    name=f"Game {rom_id}",
                    fs_name=f"game{rom_id}",
                    shortcut_app_id=app_id,
                    last_synced_at="2025-01-01T00:00:00",
                )
            )

    @pytest.mark.asyncio
    async def test_relocated_installed_bound_rom_emits_new_launch_options(self, plugin, tmp_path):
        """Happy path: a relocated installed+bound ROM emits its app_id + NEW-path command."""
        import decky

        from domain.shortcut_data import build_launch_options, resolve_emulator_invocation

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "n64", "zelda.z64")
        new_rom = os.path.join(new_home, "roms", "n64", "zelda.z64")

        os.makedirs(os.path.dirname(old_rom))
        with open(old_rom, "w") as f:
            f.write("rom data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="n64", app_id=4242)

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True

        payload = self._relaunch_emit(plugin)
        assert payload is not None
        expected_cmd = build_launch_options(resolve_emulator_invocation({"id": 1}), new_rom)
        assert payload["items"] == [{"app_id": 4242, "launch_options": expected_cmd}]
        # The command must point at the NEW path, never the stale old one.
        assert new_rom in payload["items"][0]["launch_options"]
        assert old_rom not in payload["items"][0]["launch_options"]

    @pytest.mark.asyncio
    async def test_relocated_rom_with_override_rebakes_e_form(self, plugin, tmp_path):
        """A relocated ROM with a resolvable ``emulator_override`` re-bakes the ``-e`` form."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "psx", "game.chd")
        new_rom = os.path.join(new_home, "roms", "psx", "game.chd")
        os.makedirs(os.path.dirname(old_rom))
        with open(old_rom, "w") as f:
            f.write("rom data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="psx", platform_slug="psx", app_id=4242)
        with plugin._uow as uow:
            uow.roms.set_emulator_override(1, "PCSX ReARMed")

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True

        payload = self._relaunch_emit(plugin)
        assert payload is not None
        assert payload["items"] == [
            {
                "app_id": 4242,
                "launch_options": (
                    "flatpak run net.retrodeck.retrodeck "
                    '-e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/pcsx_rearmed_libretro.so %ROM%" '
                    f'"{new_rom}"'
                ),
            }
        ]

    @pytest.mark.asyncio
    async def test_relocated_rom_with_stale_override_rebakes_plain_and_warns(self, plugin, tmp_path, caplog):
        """A stale override LABEL re-bakes the PLAIN launch + WARNs (B4) — never ``None.so``."""
        import logging

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        # available_cores does not carry the pinned label → resolution returns None.
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "psx", "game.chd")
        new_rom = os.path.join(new_home, "roms", "psx", "game.chd")
        os.makedirs(os.path.dirname(old_rom))
        with open(old_rom, "w") as f:
            f.write("rom data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="psx", platform_slug="psx", app_id=4242)
        with plugin._uow as uow:
            uow.roms.set_emulator_override(1, "Removed Core")

        with caplog.at_level(logging.WARNING):
            result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True

        payload = self._relaunch_emit(plugin)
        assert payload is not None
        # Stale → PLAIN launch at the NEW path, never -e None.so.
        assert payload["items"] == [
            {"app_id": 4242, "launch_options": f'flatpak run net.retrodeck.retrodeck "{new_rom}"'}
        ]
        assert "-e" not in payload["items"][0]["launch_options"]
        assert "Removed Core" in caplog.text
        assert "no longer resolves" in caplog.text

    @pytest.mark.asyncio
    async def test_installed_unbound_rom_excluded(self, plugin, tmp_path):
        """Edge: installed but UNBOUND (shortcut_app_id None) is excluded from items."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "n64", "zelda.z64")

        os.makedirs(os.path.dirname(old_rom))
        with open(old_rom, "w") as f:
            f.write("rom data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="n64", app_id=None)

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True

        # Event still fires (matches sync_stale always-emit convention) but the
        # unbound install is not in the items.
        payload = self._relaunch_emit(plugin)
        assert payload is not None
        assert payload["items"] == []

    @pytest.mark.asyncio
    async def test_bound_uninstalled_rom_excluded(self, plugin, tmp_path):
        """Edge: bound but NOT installed (no rom_installs row) is excluded."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        # Bound Rom row, but no install — nothing on disk, no rom_installs row.
        self._seed_bound_uninstalled(plugin._uow, 7, app_id=9999)

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True

        payload = self._relaunch_emit(plugin)
        assert payload is not None
        assert payload["items"] == []

    @pytest.mark.asyncio
    async def test_mixed_batch_includes_only_installed_and_bound(self, plugin, tmp_path):
        """Edge: mixed batch — only the installed+bound ROM appears in items."""
        import decky

        from domain.shortcut_data import build_launch_options, resolve_emulator_invocation

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        bound_rom = os.path.join(old_home, "roms", "n64", "bound.z64")
        new_bound_rom = os.path.join(new_home, "roms", "n64", "bound.z64")
        unbound_rom = os.path.join(old_home, "roms", "n64", "unbound.z64")

        os.makedirs(os.path.dirname(bound_rom))
        with open(bound_rom, "w") as f:
            f.write("bound data")
        with open(unbound_rom, "w") as f:
            f.write("unbound data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        # 1) installed + bound  → included
        _seed_install(plugin._uow, 1, file_path=bound_rom, system="n64", app_id=1111)
        # 2) installed + unbound → excluded
        _seed_install(plugin._uow, 2, file_path=unbound_rom, system="n64", app_id=None)
        # 3) bound + uninstalled → excluded
        self._seed_bound_uninstalled(plugin._uow, 3, app_id=3333)

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True

        payload = self._relaunch_emit(plugin)
        assert payload is not None
        expected_cmd = build_launch_options(resolve_emulator_invocation({"id": 1}), new_bound_rom)
        assert payload["items"] == [{"app_id": 1111, "launch_options": expected_cmd}]

    @pytest.mark.asyncio
    async def test_zero_eligible_roms_emits_empty_items(self, plugin, tmp_path):
        """Edge: zero eligible ROMs — still emits (sync_stale convention) with empty items."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        # No installs, no bound ROMs.

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True

        payload = self._relaunch_emit(plugin)
        assert payload is not None
        assert payload["items"] == []

    @pytest.mark.asyncio
    async def test_no_relaunch_emit_on_needs_confirmation(self, plugin, tmp_path):
        """The needs-confirmation early return must NOT emit relaunch options.

        Nothing was relocated and no paths were persisted, so re-resolving and
        rewriting shortcuts would point them at files that did not move.
        """
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "n64", "zelda.z64")
        new_rom = os.path.join(new_home, "roms", "n64", "zelda.z64")

        os.makedirs(os.path.dirname(old_rom))
        os.makedirs(os.path.dirname(new_rom))
        with open(old_rom, "w") as f:
            f.write("old data")
        with open(new_rom, "w") as f:
            f.write("new data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="n64", app_id=4242)

        result = await plugin.migrate_retrodeck_files()
        assert result["needs_confirmation"] is True

        assert self._relaunch_emit(plugin) is None

    @pytest.mark.asyncio
    async def test_relaunch_options_built_from_persisted_new_paths(self, plugin, tmp_path):
        """The event fires only after the relocated path is persisted to rom_installs.

        Asserting the emitted command equals the command for the persisted
        ``rom_installs.file_path`` ties the emit to post-commit state — a
        pre-commit emit would carry the stale old path.
        """
        import decky

        from domain.shortcut_data import build_launch_options, resolve_emulator_invocation

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")
        old_rom = os.path.join(old_home, "roms", "n64", "zelda.z64")

        os.makedirs(os.path.dirname(old_rom))
        with open(old_rom, "w") as f:
            f.write("rom data")

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)
        _seed_install(plugin._uow, 1, file_path=old_rom, system="n64", app_id=4242)

        await plugin.migrate_retrodeck_files()

        # The persisted install now points at the new path.
        with plugin._uow as uow:
            persisted_path = uow.rom_installs.get(1).file_path
        payload = self._relaunch_emit(plugin)
        assert payload is not None
        expected_cmd = build_launch_options(resolve_emulator_invocation({"id": 1}), persisted_path)
        assert payload["items"][0]["launch_options"] == expected_cmd


class TestResolveSaveSortConflict:
    """Regression lock for _resolve_save_sort_conflict's mtime-naive behavior.

    This test documents current mtime-naive behavior. It is deliberately NOT a
    semantic "correctness" test — #238 works around this limitation
    structurally at the save-sync layer (SaveService reads the previous
    layout when a migration is pending and skips server_only downloads so
    the mtime-naive resolver never sees a freshly-downloaded stale file).
    If you improve the resolver to be hash-aware, delete or rewrite this
    test rather than bypass it.
    """

    def test_resolve_save_sort_conflict_newest_mtime_wins_regression(self, plugin, tmp_path):
        """Newer mtime wins; older file is removed. Freezes current behavior (#238)."""
        # Stale file at the "old" path (older mtime).
        old_path = str(tmp_path / "old_saves" / "game.srm")
        new_path = str(tmp_path / "new_saves" / "game.srm")
        os.makedirs(os.path.dirname(old_path))
        os.makedirs(os.path.dirname(new_path))
        with open(old_path, "wb") as f:
            f.write(b"stale content")
        with open(new_path, "wb") as f:
            f.write(b"fresh content")

        # Force deterministic mtimes: old is older, new is newer.
        old_mtime = 1_700_000_000.0
        new_mtime = 1_700_000_500.0
        os.utime(old_path, (old_mtime, old_mtime))
        os.utime(new_path, (new_mtime, new_mtime))

        counts: dict[str, int] = {}
        errors: list[str] = []
        state_updates: list[str] = []

        plugin._migration_service._resolve_save_sort_conflict(
            label="gba/game.srm",
            old_path=old_path,
            new_path=new_path,
            state_updater=lambda: state_updates.append("called"),
            counts=counts,
            count_key="save",
            errors=errors,
        )

        # New (newer mtime) is kept; old (stale) is removed.
        assert os.path.exists(new_path)
        assert not os.path.exists(old_path)
        with open(new_path, "rb") as f:
            assert f.read() == b"fresh content"
        assert counts["save"] == 1
        assert state_updates == ["called"]
        assert errors == []


class TestDetectSaveSortChangeThreadSafety:
    """Regression tests for #238 review finding 1: ``detect_save_sort_change``
    is called from a worker thread (via ``SaveService._refresh_save_sort_state``
    → ``run_in_executor``) and must schedule the emit coroutine in a
    thread-safe manner. ``loop.create_task`` is NOT thread-safe — it must
    be ``asyncio.run_coroutine_threadsafe``.
    """

    async def test_detect_save_sort_change_is_thread_safe_when_called_from_executor(self, plugin):
        """detect_save_sort_change must be safe to call from a worker thread (#238).

        Drive the call via ``loop.run_in_executor`` and verify the emit
        coroutine is scheduled on the loop and runs without exception.
        Before the fix, this would call ``loop.create_task`` from a
        worker thread, which is undefined behavior.
        """
        loop = asyncio.get_event_loop()
        plugin._migration_service._loop = loop

        # Initial state: a populated OLD layout. Detect should observe a
        # change and emit ``save_sort_changed``.
        import json

        with plugin._uow as uow:
            uow.kv_config.set("save_sort_settings", json.dumps({"sort_by_content": True, "sort_by_core": False}))
        plugin._migration_service._get_retroarch_save_sorting = lambda: (True, True)

        # Use an ``asyncio.Queue``-backed emitter so the test can await the
        # emission from the loop thread regardless of which thread scheduled
        # it. We swap in a queue-aware ``EventEmitter`` rather than reading
        # the recorder fixture because we need an awaitable barrier.
        emit_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

        async def fake_emit(event_name: str, payload: dict[str, Any]) -> None:
            await emit_queue.put((event_name, payload))

        plugin._migration_service._emit = fake_emit

        # Run detect_save_sort_change on a worker thread.
        await loop.run_in_executor(None, plugin._migration_service.detect_save_sort_change)

        # Wait (with a generous timeout) for the emit coroutine that was
        # scheduled via run_coroutine_threadsafe to actually run.
        event = await asyncio.wait_for(emit_queue.get(), timeout=2.0)
        assert event[0] == "save_sort_changed"
        assert event[1]["old_settings"] == {"sort_by_content": True, "sort_by_core": False}
        assert event[1]["new_settings"] == {"sort_by_content": True, "sort_by_core": True}

        # State is persisted to kv_config — visible to any later reader.
        with plugin._uow as uow:
            assert json.loads(uow.kv_config.get("save_sort_settings_previous")) == {
                "sort_by_content": True,
                "sort_by_core": False,
            }
            assert json.loads(uow.kv_config.get("save_sort_settings")) == {
                "sort_by_content": True,
                "sort_by_core": True,
            }


class TestMigrationFailureInjection:
    """Adapter-level failure injection tests using FakeMigrationFileStore.

    These tests exercise paths the tmp_path-based integration tests cannot
    reach: simulated ``OSError`` during ``move`` / ``rename`` / ``remove``
    must be caught by the service, appended to the ``errors`` list, and
    must not abort the rest of the migration loop. The previous path
    marker is also retained on partial failure so the user can retry.
    """

    def _make_service(self, fake_files, *, uow=None, **overrides):
        import decky

        uow = uow if uow is not None else FakeUnitOfWork()
        defaults: dict[str, Any] = {
            "settings": {},
            "loop": asyncio.get_event_loop(),
            "logger": decky.logger,
            "settings_persister": FakeSettingsPersister(),
            "emit": RecordingEmitter(),
            "get_bios_files_index": dict,
            "retrodeck_paths": FakeRetroDeckPaths(),
            "get_retroarch_save_sorting": lambda: (False, False),
            "active_core": FakeActiveCoreResolver(default=(None, None)),
            "get_core_name": lambda core_so: None,
            "uow_factory": FakeUnitOfWorkFactory(uow=uow),
        }
        defaults.update(overrides)
        return MigrationService(
            config=MigrationServiceConfig(migration_file_store=fake_files, **defaults),
        )

    def test_move_failure_records_error_and_continues(self):
        """Mid-batch ``move`` failure is captured in ``errors``; other items still move."""
        fake = FakeMigrationFileStore()
        old_home = "/old"
        new_home = "/new"
        bad_rom = "/old/roms/n64/bad.z64"
        good_rom = "/old/roms/n64/good.z64"
        fake.files[bad_rom] = b"bad"
        fake.files[good_rom] = b"good"
        fake.move_failures.add(bad_rom)

        uow = FakeUnitOfWork()
        _seed_install(uow, 1, file_path=bad_rom, system="n64")
        _seed_install(uow, 2, file_path=good_rom, system="n64")
        with uow:
            uow.kv_config.set("retrodeck_home_path_previous", old_home)
            uow.kv_config.set("retrodeck_home_path", new_home)

        service = self._make_service(fake, uow=uow)

        result = service._migrate_retrodeck_files_io(old_home, new_home, None)

        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert "bad.z64" in result["errors"][0]
        # Good ROM was moved successfully despite the bad one failing.
        assert result["roms_moved"] == 1
        # Marker is retained so the user can retry.
        with uow:
            assert uow.kv_config.get("retrodeck_home_path_previous") == old_home

    def test_rename_failure_records_save_sort_error(self):
        """``OSError`` from ``rename`` during save-sort overwrite path is captured."""
        fake = FakeMigrationFileStore()
        old_path = "/saves/old/game.srm"
        new_path = "/saves/new/game.srm"
        fake.files[old_path] = b"new content"
        fake.files[new_path] = b"old content"
        # Source is newer => triggers rename path.
        fake.mtimes[old_path] = 2000.0
        fake.mtimes[new_path] = 1000.0
        fake.rename_failures.add(old_path)

        service = self._make_service(fake)

        counts: dict[str, int] = {}
        errors: list[str] = []
        state_updates: list[str] = []
        service._resolve_save_sort_conflict(
            label="gba/game.srm",
            old_path=old_path,
            new_path=new_path,
            state_updater=lambda: state_updates.append("called"),
            counts=counts,
            count_key="save",
            errors=errors,
        )

        assert len(errors) == 1
        assert "gba/game.srm" in errors[0]
        assert counts.get("save", 0) == 0
        # Failure path must not invoke the state updater.
        assert state_updates == []

    def test_remove_failure_records_save_sort_orphan_cleanup_error(self):
        """``OSError`` from ``remove`` during save-sort newest-wins cleanup is captured."""
        fake = FakeMigrationFileStore()
        old_path = "/saves/old/game.srm"
        new_path = "/saves/new/game.srm"
        fake.files[old_path] = b"stale"
        fake.files[new_path] = b"fresh"
        # Destination is newer => triggers orphan-removal path.
        fake.mtimes[old_path] = 1000.0
        fake.mtimes[new_path] = 2000.0
        fake.remove_failures.add(old_path)

        service = self._make_service(fake)

        counts: dict[str, int] = {}
        errors: list[str] = []
        state_updates: list[str] = []
        service._resolve_save_sort_conflict(
            label="gba/game.srm",
            old_path=old_path,
            new_path=new_path,
            state_updater=lambda: state_updates.append("called"),
            counts=counts,
            count_key="save",
            errors=errors,
        )

        assert len(errors) == 1
        assert "gba/game.srm" in errors[0]
        assert counts.get("save", 0) == 0
        # Failure path must not invoke the state updater.
        assert state_updates == []


class TestRefreshState:
    """Tests for ``MigrationService.refresh_state``.

    These tests exercise the orchestration contract: ``refresh_state``
    drives ``detect_retrodeck_path_change`` then ``detect_save_sort_change``
    then composes their status outputs. The detect/status methods are
    patched directly because the test is about *how* refresh_state wires
    them together, not what they observe — this is the small carve-out
    called out in the issue scope.
    """

    @pytest.mark.asyncio
    async def test_calls_both_detect_methods_and_returns_combined_status(self, plugin):
        mig = plugin._migration_service
        mig.detect_retrodeck_path_change = MagicMock()
        mig.detect_save_sort_change = MagicMock()

        retrodeck_status = {"pending": True, "old_path": "/a", "new_path": "/b"}
        save_sort_status = {"pending": True, "saves_count": 3}
        mig.get_migration_status = AsyncMock(return_value=retrodeck_status)
        mig.get_save_sort_migration_status = AsyncMock(return_value=save_sort_status)

        result = await mig.refresh_state()

        mig.detect_retrodeck_path_change.assert_called_once_with()
        mig.detect_save_sort_change.assert_called_once_with()
        assert result == {"retrodeck": retrodeck_status, "save_sort": save_sort_status}

    @pytest.mark.asyncio
    async def test_detect_order_preserved(self, plugin):
        mig = plugin._migration_service
        manager = MagicMock()
        mig.detect_retrodeck_path_change = manager.detect_retrodeck_path_change
        mig.detect_save_sort_change = manager.detect_save_sort_change
        mig.get_migration_status = AsyncMock(return_value={"pending": False})
        mig.get_save_sort_migration_status = AsyncMock(return_value={"pending": False})

        await mig.refresh_state()

        ordered = [name for name, _args, _kwargs in manager.mock_calls]
        assert ordered == ["detect_retrodeck_path_change", "detect_save_sort_change"]

    @pytest.mark.asyncio
    async def test_short_circuits_when_first_detect_raises(self, plugin):
        mig = plugin._migration_service
        mig.detect_retrodeck_path_change = MagicMock(side_effect=RuntimeError("boom"))
        mig.detect_save_sort_change = MagicMock()
        mig.get_migration_status = AsyncMock()
        mig.get_save_sort_migration_status = AsyncMock()

        with pytest.raises(RuntimeError, match="boom"):
            await mig.refresh_state()

        mig.detect_save_sort_change.assert_not_called()
        mig.get_migration_status.assert_not_called()
        mig.get_save_sort_migration_status.assert_not_called()


class TestBadPathDismissSaveSortMigration:
    """Coverage for the previously-untested ``dismiss_save_sort_migration`` callable."""

    def test_dismiss_save_sort_migration_clears_state_and_persists(self, plugin):
        """User dismissing the warning deletes the marker from kv_config and commits."""
        import json

        with plugin._uow as uow:
            uow.kv_config.set(
                "save_sort_settings_previous", json.dumps({"sort_by_content": True, "sort_by_core": False})
            )

        result = plugin._migration_service.dismiss_save_sort_migration()

        assert result == {"success": True}
        assert plugin._uow.committed is True
        with plugin._uow as uow:
            assert uow.kv_config.get("save_sort_settings_previous") is None


class TestBackgroundTaskTracking:
    """Coverage for the background-task tracking + ``shutdown()`` lifecycle.

    The path-change detection schedules a ``retrodeck_path_changed`` emit
    via ``loop.create_task``. Without strong refs into ``_background_tasks``
    and a cancellation hook in ``shutdown()``, those tasks leak across
    plugin unload. These tests pin the contract.
    """

    @pytest.mark.asyncio
    async def test_spawned_task_added_to_background_set(self, plugin, tmp_path):
        """``detect_retrodeck_path_change`` adds its emit task to the set."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old_retrodeck")
        new_home = str(tmp_path / "new_retrodeck")
        os.makedirs(new_home, exist_ok=True)

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path", old_home)
        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=new_home)

        assert plugin._migration_service._background_tasks == set()

        plugin._migration_service.detect_retrodeck_path_change()

        # The spawned task must be tracked before any await yields control.
        assert len(plugin._migration_service._background_tasks) == 1
        (task,) = plugin._migration_service._background_tasks
        assert isinstance(task, asyncio.Task)

        # Drain so no pending-task warning fires at loop teardown.
        await asyncio.gather(*plugin._migration_service._background_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_done_callback_removes_task_on_natural_completion(self, plugin, tmp_path):
        """When the spawned coro completes naturally, the done-callback prunes the set."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old_retrodeck")
        new_home = str(tmp_path / "new_retrodeck")
        os.makedirs(new_home, exist_ok=True)

        with plugin._uow as uow:
            uow.kv_config.set("retrodeck_home_path", old_home)
        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=new_home)

        plugin._migration_service.detect_retrodeck_path_change()
        assert len(plugin._migration_service._background_tasks) == 1

        # Yield until the spawned emit coroutine finishes; the done-callback
        # then discards the task from the set.
        (task,) = plugin._migration_service._background_tasks
        await task

        assert plugin._migration_service._background_tasks == set()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_pending_tasks_and_empties_set(self, plugin):
        """``shutdown()`` cancels in-flight tasks and the set is empty after."""
        loop = asyncio.get_event_loop()
        plugin._migration_service._loop = loop

        # Spawn a task that blocks forever via an unset Event.
        blocker = asyncio.Event()

        async def _block_forever() -> None:
            await blocker.wait()

        plugin._migration_service._spawn_background_task(_block_forever())
        assert len(plugin._migration_service._background_tasks) == 1
        (task,) = plugin._migration_service._background_tasks

        await plugin._migration_service.shutdown()

        assert task.cancelled()
        assert plugin._migration_service._background_tasks == set()

    @pytest.mark.asyncio
    async def test_shutdown_with_empty_set_is_noop(self, plugin):
        """``shutdown()`` on an untouched service returns immediately."""
        assert plugin._migration_service._background_tasks == set()

        # Must not raise, must not block.
        await plugin._migration_service.shutdown()

        assert plugin._migration_service._background_tasks == set()
