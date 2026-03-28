import asyncio
import os
from unittest.mock import MagicMock

import pytest

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
        save_state=MagicMock(),
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
        """If retrodeck_config returns empty string, do nothing."""
        from unittest.mock import MagicMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)

        mock_loop = MagicMock()
        plugin._migration_service._loop = mock_loop

        plugin._migration_service._get_retrodeck_home = MagicMock(return_value="")
        plugin._migration_service.detect_retrodeck_path_change()

        mock_loop.create_task.assert_not_called()
        assert plugin._state["retrodeck_home_path"] == ""


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
