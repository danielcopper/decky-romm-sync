import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from conftest import FakeCoreInfoProvider, FakeFirmwareCachePersister
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.persistence import PersistenceAdapter
from adapters.steam_config import SteamConfigAdapter

# conftest.py patches decky before this import
from main import Plugin
from services.firmware import FirmwareService
from services.library import LibraryService
from services.migration import MigrationService


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._state = {
        "shortcut_registry": {},
        "installed_roms": {},
        "last_sync": None,
        "sync_stats": {},
        "downloaded_bios": {},
        "retrodeck_home_path": "",
    }
    p._metadata_cache = {}

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._romm_api = MagicMock()
    p._firmware_service = FirmwareService(
        romm_api=p._romm_api,
        state=p._state,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        plugin_dir=decky.DECKY_PLUGIN_DIR,
        clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
        save_state=MagicMock(),
        firmware_cache_persister=FakeFirmwareCachePersister(),
        get_bios_path=MagicMock(return_value=""),
        core_info=FakeCoreInfoProvider(),
    )

    p._sync_service = LibraryService(
        romm_api=p._romm_api,
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
        save_state=p._save_state,
        save_settings_to_disk=p._save_settings_to_disk,
        log_debug=p._log_debug,
    )

    p._migration_service = MigrationService(
        state=p._state,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        save_state=p._save_state,
        emit=decky.emit,
        get_bios_files_index=lambda: p._firmware_service.bios_files_index,
        get_retrodeck_home=MagicMock(return_value=""),
        get_saves_path=MagicMock(return_value=""),
        get_bios_path=MagicMock(return_value=""),
        get_retroarch_save_sorting=MagicMock(return_value=(True, False)),
        get_roms_path=MagicMock(return_value=""),
        get_active_core=MagicMock(return_value=(None, None)),
        get_core_name=MagicMock(return_value=None),
    )
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop and migration service loop match the running event loop."""
    loop = asyncio.get_event_loop()
    plugin.loop = loop
    plugin._migration_service._loop = loop


class TestPathChangeDetection:
    def test_first_run_stores_path(self, plugin, tmp_path):
        """First run (empty stored path) stores current path, no event."""
        from unittest.mock import MagicMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        mock_loop = MagicMock()
        plugin._migration_service._loop = mock_loop

        fake_home = str(tmp_path / "retrodeck")
        os.makedirs(fake_home, exist_ok=True)

        plugin._migration_service._get_retrodeck_home = MagicMock(return_value=fake_home)
        plugin._migration_service.detect_retrodeck_path_change()

        assert plugin._state["retrodeck_home_path"] == fake_home
        # No event emitted on first run
        mock_loop.create_task.assert_not_called()

    def test_no_change_no_notification(self, plugin, tmp_path):
        """Same path as stored — no event, no state change."""
        from unittest.mock import MagicMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        fake_home = str(tmp_path / "retrodeck")
        os.makedirs(fake_home, exist_ok=True)
        plugin._state["retrodeck_home_path"] = fake_home
        mock_loop = MagicMock()
        plugin._migration_service._loop = mock_loop

        plugin._migration_service._get_retrodeck_home = MagicMock(return_value=fake_home)
        plugin._migration_service.detect_retrodeck_path_change()

        mock_loop.create_task.assert_not_called()

    def test_path_change_emits_event(self, plugin, tmp_path):
        """Path changed — stores both old and new, emits event."""
        from unittest.mock import MagicMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old_retrodeck")
        new_home = str(tmp_path / "new_retrodeck")
        os.makedirs(new_home, exist_ok=True)

        plugin._state["retrodeck_home_path"] = old_home
        mock_loop = MagicMock()
        _create_task_calls = []

        def _close_coro_task(coro):
            coro.close()
            _create_task_calls.append(coro)
            return MagicMock()

        mock_loop.create_task = _close_coro_task
        plugin._migration_service._loop = mock_loop

        plugin._migration_service._get_retrodeck_home = MagicMock(return_value=new_home)
        plugin._migration_service.detect_retrodeck_path_change()

        assert plugin._state["retrodeck_home_path"] == new_home
        assert plugin._state["retrodeck_home_path_previous"] == old_home
        assert len(_create_task_calls) == 1

    def test_empty_current_home_no_action(self, plugin, tmp_path):
        """If ``retrodeck_paths`` returns empty string, do nothing."""
        from unittest.mock import MagicMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)

        mock_loop = MagicMock()
        plugin._migration_service._loop = mock_loop

        plugin._migration_service._get_retrodeck_home = MagicMock(return_value="")
        plugin._migration_service.detect_retrodeck_path_change()

        mock_loop.create_task.assert_not_called()
        assert plugin._state["retrodeck_home_path"] == ""

    def test_detect_path_change_auto_clears_when_reverted_to_previous(self, plugin, tmp_path):
        """User reverted RetroDECK to the previous home — drop the marker, no event."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        old_home = str(tmp_path / "old_retrodeck")
        new_home = str(tmp_path / "new_retrodeck")
        os.makedirs(old_home, exist_ok=True)

        plugin._state["retrodeck_home_path"] = new_home
        plugin._state["retrodeck_home_path_previous"] = old_home

        mock_loop = MagicMock()
        plugin._migration_service._loop = mock_loop

        plugin._migration_service._get_retrodeck_home = MagicMock(return_value=old_home)
        plugin._migration_service.detect_retrodeck_path_change()

        assert plugin._state["retrodeck_home_path"] == old_home
        assert "retrodeck_home_path_previous" not in plugin._state

    def test_detect_path_change_auto_clear_emits_cleared_event(self, plugin, tmp_path):
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

        mock_loop = MagicMock()
        emitted: list = []

        def _capture_task(coro):
            # Drive the coroutine to capture what was emitted, then close it.
            try:
                coro.send(None)
            except StopIteration as e:
                emitted.append(("returned", e.value))
            except BaseException as e:
                emitted.append(("raised", e))
            coro.close()
            return MagicMock()

        mock_loop.create_task = _capture_task
        plugin._migration_service._loop = mock_loop

        # Replace _emit with a sync recorder so the coroutine resolves cleanly.
        emit_calls: list = []

        async def fake_emit(event, payload):
            emit_calls.append((event, payload))

        plugin._migration_service._emit = fake_emit  # type: ignore[method-assign]

        plugin._migration_service._get_retrodeck_home = MagicMock(return_value=old_home)
        plugin._migration_service.detect_retrodeck_path_change()

        assert len(emit_calls) == 1
        event, payload = emit_calls[0]
        assert event == "retrodeck_path_changed"
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

        plugin._migration_service._get_saves_path = MagicMock(return_value=os.path.join(new_home, "saves"))
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

        plugin._migration_service._get_saves_path = MagicMock(return_value=os.path.join(new_home, "saves"))
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

        plugin._migration_service._get_saves_path = MagicMock(return_value=os.path.join(new_home, "saves"))
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

        plugin._migration_service._get_saves_path = MagicMock(return_value=os.path.join(new_home, "saves"))
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

        plugin._migration_service._get_saves_path = MagicMock(return_value=os.path.join(new_home, "saves"))
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

        plugin._migration_service._get_saves_path = MagicMock(return_value=os.path.join(new_home, "saves"))
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
        plugin._migration_service._get_retroarch_save_sorting = MagicMock(return_value=(True, True))

        # Capture emit calls. Use an asyncio.Queue so the test can await
        # the emission from the loop thread regardless of which thread
        # scheduled it.
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
