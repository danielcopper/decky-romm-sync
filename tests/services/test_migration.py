import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_firmware_cache_persister import FakeFirmwareCachePersister
from fakes.fake_migration_file_store import FakeMigrationFileStore
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_settings_persister import FakeSettingsPersister
from fakes.fake_state_persister import FakeStatePersister
from fakes.library_peers import FakeArtworkManager, FakeMetadataExtractor
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen
from models.state import make_default_plugin_state

from adapters.firmware_file import FirmwareFileAdapter
from adapters.migration_file import MigrationFileAdapter
from adapters.persistence import PersistenceAdapter
from adapters.registry_store import RegistryStoreAdapter
from adapters.steam_config import SteamConfigAdapter

# conftest.py patches decky before this import
from main import Plugin
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
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._state = make_default_plugin_state()
    p._metadata_cache = {}

    import decky

    p._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._romm_api = fake_romm_api
    p._state_persister = FakeStatePersister()
    p._settings_persister = FakeSettingsPersister()
    p._firmware_service = FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=fake_romm_api,
            state=p._state,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            state_persister=FakeStatePersister(),
            firmware_cache_persister=FakeFirmwareCachePersister(),
            firmware_file_store=FirmwareFileAdapter(),
            retrodeck_paths=FakeRetroDeckPaths(),
            core_info=FakeCoreInfoProvider(),
        ),
    )
    p._firmware_service.load_bios_registry()

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=fake_romm_api,
            steam_config=steam_config,
            state=p._state,
            settings=p.settings,
            metadata_cache=p._metadata_cache,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            state_persister=p._state_persister,
            settings_persister=p._settings_persister,
            registry_store=RegistryStoreAdapter(state=p._state, logger=decky.logger),
            log_debug=p._log_debug,
            metadata_service=FakeMetadataExtractor(),
            artwork=FakeArtworkManager(),
        ),
    )

    def _no_active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
        return (None, None)

    def _no_core_name(core_so: str) -> str | None:
        return None

    def _default_save_sorting() -> tuple[bool, bool]:
        return (True, False)

    p._migration_service = MigrationService(
        config=MigrationServiceConfig(
            migration_file_store=MigrationFileAdapter(),
            state=p._state,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            state_persister=p._state_persister,
            settings_persister=p._settings_persister,
            emit=RecordingEmitter(),
            get_bios_files_index=lambda: p._firmware_service.bios_files_index,
            retrodeck_paths=FakeRetroDeckPaths(),
            get_retroarch_save_sorting=_default_save_sorting,
            get_active_core=_no_active_core,
            get_core_name=_no_core_name,
        ),
    )
    return p


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
        return None


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

        assert plugin._state["retrodeck_home_path"] == fake_home
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
        plugin._state["retrodeck_home_path"] = fake_home
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

        plugin._state["retrodeck_home_path"] = old_home

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=new_home)
        plugin._migration_service.detect_retrodeck_path_change()

        # ``create_task`` schedules the emit coroutine on the running loop —
        # yield once so the scheduled coroutine runs and the emitter records.
        await asyncio.sleep(0)

        assert plugin._state["retrodeck_home_path"] == new_home
        assert plugin._state["retrodeck_home_path_previous"] == old_home

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
        assert plugin._state["retrodeck_home_path"] == ""

    async def test_detect_path_change_auto_clears_when_reverted_to_previous(self, plugin, tmp_path):
        """User reverted RetroDECK to the previous home — drop the marker, emit cleared event."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old_retrodeck")
        new_home = str(tmp_path / "new_retrodeck")
        os.makedirs(old_home, exist_ok=True)

        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["retrodeck_home_path_previous"] = old_home

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=old_home)
        plugin._migration_service.detect_retrodeck_path_change()

        # ``create_task`` schedules the emit coroutine on the running loop —
        # yield once so the scheduled coroutine runs and the emitter records.
        await asyncio.sleep(0)

        assert plugin._state["retrodeck_home_path"] == old_home
        assert "retrodeck_home_path_previous" not in plugin._state

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

        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["retrodeck_home_path_previous"] = old_home

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
        plugin._state.pop("retrodeck_home_path_previous", None)
        assert plugin._migration_service.is_retrodeck_migration_pending() is False

    def test_is_retrodeck_migration_pending_returns_true_when_set(self, plugin):
        plugin._state["retrodeck_home_path_previous"] = "/some/old/path"
        assert plugin._migration_service.is_retrodeck_migration_pending() is True

    def test_is_retrodeck_migration_pending_returns_false_for_empty_string(self, plugin):
        plugin._state["retrodeck_home_path_previous"] = ""
        assert plugin._migration_service.is_retrodeck_migration_pending() is False


class TestDismissRetroDeckMigration:
    def test_dismiss_retrodeck_migration_clears_marker(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state["retrodeck_home_path_previous"] = "/old/path"

        result = plugin._migration_service.dismiss_retrodeck_migration()

        assert result == {"success": True}
        assert "retrodeck_home_path_previous" not in plugin._state

    def test_dismiss_retrodeck_migration_idempotent_when_no_marker(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state.pop("retrodeck_home_path_previous", None)

        result = plugin._migration_service.dismiss_retrodeck_migration()

        assert result == {"success": True}
        assert "retrodeck_home_path_previous" not in plugin._state


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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": old_rom,
                "system": "n64",
            }
        }

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True
        assert result["roms_moved"] == 1
        assert os.path.exists(new_rom)
        assert not os.path.exists(old_rom)
        assert plugin._state["installed_roms"]["1"]["file_path"] == new_rom

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["downloaded_bios"] = {
            "scph5501.bin": {
                "file_path": old_bios,
                "firmware_id": 42,
                "platform_slug": "psx",
            }
        }

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True
        assert result["bios_moved"] == 1
        assert os.path.exists(new_bios)
        assert plugin._state["downloaded_bios"]["scph5501.bin"]["file_path"] == new_bios

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["installed_roms"] = {"1": {"rom_id": 1, "file_path": old_rom, "system": "n64"}}

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["installed_roms"] = {"1": {"rom_id": 1, "file_path": old_rom, "system": "n64"}}

        result = await plugin.migrate_retrodeck_files("overwrite")
        assert result["success"] is True
        assert result["roms_moved"] == 1
        with open(new_rom) as f:
            assert f.read() == "old data"
        assert plugin._state["installed_roms"]["1"]["file_path"] == new_rom

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["installed_roms"] = {"1": {"rom_id": 1, "file_path": old_rom, "system": "n64"}}

        result = await plugin.migrate_retrodeck_files("skip")
        assert result["success"] is True
        assert result["roms_moved"] == 1
        # Destination file preserved
        with open(new_rom) as f:
            assert f.read() == "new data"
        # State updated to new path
        assert plugin._state["installed_roms"]["1"]["file_path"] == new_rom

    @pytest.mark.asyncio
    async def test_migrate_source_missing(self, plugin, tmp_path):
        """Source file gone — skip silently."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old")
        new_home = str(tmp_path / "new")

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": os.path.join(old_home, "roms", "n64", "gone.z64"), "system": "n64"}
        }

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["downloaded_bios"] = {
            "dc_boot.bin": {
                "file_path": old_bios,
                "firmware_id": 7,
                "platform_slug": "dc",
            }
        }

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home
        # No files to move — success with 0 moved

        result = await plugin.migrate_retrodeck_files()
        assert result["success"] is True
        assert "retrodeck_home_path_previous" not in plugin._state


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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home

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

        plugin._state["retrodeck_home_path_previous"] = old_home
        plugin._state["retrodeck_home_path"] = new_home

        plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(saves=os.path.join(new_home, "saves"))
        status = await plugin.get_migration_status()

        assert status["pending"] is True
        assert status["saves_count"] == 1


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
        errors: list = []
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
        plugin._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}
        plugin._migration_service._get_retroarch_save_sorting = lambda: (True, True)

        # Use an ``asyncio.Queue``-backed emitter so the test can await the
        # emission from the loop thread regardless of which thread scheduled
        # it. We swap in a queue-aware ``EventEmitter`` rather than reading
        # the recorder fixture because we need an awaitable barrier.
        emit_queue: asyncio.Queue = asyncio.Queue()

        async def fake_emit(event_name: str, payload: dict) -> None:
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

        # State is updated via the shared dict — visible to whoever holds it.
        assert plugin._state["save_sort_settings_previous"] == {
            "sort_by_content": True,
            "sort_by_core": False,
        }
        assert plugin._state["save_sort_settings"] == {
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

    def _make_service(self, fake_files, **overrides):
        import decky

        defaults: dict = {
            "state": {
                "installed_roms": {},
                "downloaded_bios": {},
            },
            "settings": {},
            "loop": asyncio.get_event_loop(),
            "logger": decky.logger,
            "state_persister": FakeStatePersister(),
            "settings_persister": FakeSettingsPersister(),
            "emit": RecordingEmitter(),
            "get_bios_files_index": lambda: {},
            "retrodeck_paths": FakeRetroDeckPaths(),
            "get_retroarch_save_sorting": lambda: (False, False),
            "get_active_core": lambda system, rom_filename: (None, None),
            "get_core_name": lambda core_so: None,
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

        service = self._make_service(
            fake,
            state={
                "installed_roms": {
                    "1": {"rom_id": 1, "file_path": bad_rom, "system": "n64"},
                    "2": {"rom_id": 2, "file_path": good_rom, "system": "n64"},
                },
                "downloaded_bios": {},
                "retrodeck_home_path_previous": old_home,
                "retrodeck_home_path": new_home,
            },
        )

        result = service._migrate_retrodeck_files_io(old_home, new_home, None)

        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert "bad.z64" in result["errors"][0]
        # Good ROM was moved successfully despite the bad one failing.
        assert result["roms_moved"] == 1
        # Marker is retained so the user can retry.
        assert service._state.get("retrodeck_home_path_previous") == old_home

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
        errors: list = []
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
        errors: list = []
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
        """User dismissing the warning pops the marker and persists state once."""
        plugin._state["save_sort_settings_previous"] = {
            "sort_by_content": True,
            "sort_by_core": False,
        }
        persister = plugin._migration_service._state_persister
        saves_before = persister.save_count

        result = plugin._migration_service.dismiss_save_sort_migration()

        assert result == {"success": True}
        assert "save_sort_settings_previous" not in plugin._state
        assert persister.save_count == saves_before + 1


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

        plugin._state["retrodeck_home_path"] = old_home
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

        plugin._state["retrodeck_home_path"] = old_home
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


class TestApplySettingsSchemaMigrations:
    """Tests for apply_settings_schema_migrations() — settings-schema bumps (#738)."""

    def test_v0_clears_last_sync(self, plugin):
        """Pre-versioning settings (version=0) trigger the v1→v2 migration: clear last_sync."""
        plugin.settings["version"] = 0
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"

        plugin._migration_service.apply_settings_schema_migrations()

        assert plugin._state["last_sync"] is None

    def test_v1_clears_last_sync(self, plugin):
        """Version 1 settings (pre-fetch-apply-split) trigger the migration."""
        plugin.settings["version"] = 1
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"

        plugin._migration_service.apply_settings_schema_migrations()

        assert plugin._state["last_sync"] is None

    def test_v2_is_noop(self, plugin):
        """Version 2 settings (post-fetch-apply-split) are already migrated — preserve last_sync."""
        plugin.settings["version"] = 2
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"

        plugin._migration_service.apply_settings_schema_migrations()

        assert plugin._state["last_sync"] == "2025-01-01T00:00:00Z"

    def test_persists_state_after_migration(self, plugin):
        """The state-persister is invoked so the cleared last_sync lands on disk."""
        plugin.settings["version"] = 1
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"

        save_count_before = plugin._state_persister.save_count
        plugin._migration_service.apply_settings_schema_migrations()
        assert plugin._state_persister.save_count > save_count_before

    def test_persists_settings_after_migration(self, plugin):
        """The settings-persister is invoked so the version-bump stamp lands on disk."""
        plugin.settings["version"] = 1
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"

        save_count_before = plugin._settings_persister.save_count
        plugin._migration_service.apply_settings_schema_migrations()
        assert plugin._settings_persister.save_count > save_count_before

    def test_v2_does_not_persist(self, plugin):
        """When already at v2, no persistence calls are made (avoid spurious disk writes)."""
        plugin.settings["version"] = 2

        state_save_before = plugin._state_persister.save_count
        settings_save_before = plugin._settings_persister.save_count

        plugin._migration_service.apply_settings_schema_migrations()

        assert plugin._state_persister.save_count == state_save_before
        assert plugin._settings_persister.save_count == settings_save_before

    def test_missing_version_treated_as_zero(self, plugin):
        """Settings without a ``version`` field are treated as v0 (oldest possible)."""
        plugin.settings.pop("version", None)
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"

        plugin._migration_service.apply_settings_schema_migrations()

        assert plugin._state["last_sync"] is None
