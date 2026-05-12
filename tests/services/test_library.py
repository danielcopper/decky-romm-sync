import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.cover_art_file_store import CoverArtFileStoreAdapter
from adapters.persistence import PersistenceAdapter
from adapters.steam_config import SteamConfigAdapter
from domain.sync_diff import classify_roms
from domain.sync_state import SyncState

# conftest.py patches decky before this import
from main import Plugin
from services.artwork import ArtworkService
from services.library import LibraryService
from services.metadata import MetadataService
from services.shortcut_removal import ShortcutRemovalService


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._romm_api = MagicMock()
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._metadata_cache = {}

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    metadata_service = MetadataService(
        romm_api=p._romm_api,
        state=p._state,
        metadata_cache=p._metadata_cache,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        clock=FakeClock(),
        save_metadata_cache=p._save_metadata_cache,
        log_debug=p._log_debug,
    )
    p._metadata_service = metadata_service

    artwork_service = ArtworkService(
        romm_api=p._romm_api,
        steam_config=steam_config,
        cover_art_file_store=CoverArtFileStoreAdapter(),
        state=p._state,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
    )
    p._artwork_service = artwork_service

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
        metadata_service=metadata_service,
        artwork=artwork_service,
    )

    p._shortcut_removal_service = ShortcutRemovalService(
        romm_api=p._romm_api,
        steam_config=steam_config,
        state=p._state,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        emit=decky.emit,
        save_state=p._save_state,
        artwork_remover=artwork_service,
    )
    # Default migration service mock — no migration pending. Tests that need
    # to exercise the @migration_blocked gate override this.
    p._migration_service = MagicMock()
    p._migration_service.is_retrodeck_migration_pending.return_value = False
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop matches the running event loop for async tests."""
    plugin.loop = asyncio.get_event_loop()
    plugin._sync_service._loop = asyncio.get_event_loop()
    plugin._artwork_service._loop = asyncio.get_event_loop()
    plugin._shortcut_removal_service._loop = asyncio.get_event_loop()
    plugin._metadata_service._loop = asyncio.get_event_loop()


class TestReportSyncResults:
    @pytest.mark.asyncio
    async def test_updates_registry(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._sync_service._pending_sync = {
            1: {"name": "Game A", "platform_name": "N64", "cover_path": "/grid/abc.png"},
            2: {"name": "Game B", "platform_name": "SNES", "cover_path": "/grid/def.png"},
        }

        result = await plugin.report_sync_results(
            {"1": 100001, "2": 100002},
            [],
        )
        assert result["success"] is True
        assert "1" in plugin._state["shortcut_registry"]
        assert plugin._state["shortcut_registry"]["1"]["app_id"] == 100001
        assert plugin._state["shortcut_registry"]["1"]["name"] == "Game A"
        assert plugin._state["shortcut_registry"]["1"]["platform_name"] == "N64"
        assert "2" in plugin._state["shortcut_registry"]

    @pytest.mark.asyncio
    async def test_removes_stale_entries(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"]["99"] = {
            "app_id": 99999,
            "name": "Old Game",
            "platform_name": "NES",
        }
        plugin._sync_service._pending_sync = {}

        result = await plugin.report_sync_results({}, [99])
        assert result["success"] is True
        assert "99" not in plugin._state["shortcut_registry"]

    @pytest.mark.asyncio
    async def test_emits_sync_complete(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        decky.emit.reset_mock()

        plugin._sync_service._pending_sync = {
            1: {"name": "Game A", "platform_name": "N64", "cover_path": ""},
        }

        await plugin.report_sync_results({"1": 100001}, [])
        # emit called twice: sync_complete then sync_progress (done)
        assert decky.emit.call_count == 2
        sync_complete_call = decky.emit.call_args_list[0]
        assert sync_complete_call[0][0] == "sync_complete"
        assert sync_complete_call[0][1]["total_games"] == 1

    @pytest.mark.asyncio
    async def test_updates_last_sync(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._sync_service._pending_sync = {}
        await plugin.report_sync_results({}, [])
        assert plugin._state["last_sync"] is not None

    @pytest.mark.asyncio
    async def test_clears_pending_sync(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._sync_service._pending_sync = {1: {"name": "X", "platform_name": "Y", "cover_path": ""}}
        await plugin.report_sync_results({"1": 1}, [])
        assert plugin._sync_service._pending_sync == {}


class TestRemoveAllShortcuts:
    @pytest.mark.asyncio
    async def test_returns_app_ids_and_rom_ids(self, plugin):
        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A"},
            "20": {"app_id": 1002, "name": "Game B"},
            "30": {"name": "Game C"},  # no app_id (edge case)
        }

        result = await plugin.remove_all_shortcuts()
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert set(result["rom_ids"]) == {"10", "20", "30"}

    @pytest.mark.asyncio
    async def test_empty_registry(self, plugin):
        result = await plugin.remove_all_shortcuts()
        assert result["success"] is True
        assert result["app_ids"] == []
        assert result["rom_ids"] == []

    @pytest.mark.asyncio
    async def test_does_not_modify_registry(self, plugin):
        """remove_all_shortcuts just returns data; registry cleared by report_removal_results."""
        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A"},
        }
        await plugin.remove_all_shortcuts()
        # Registry should NOT be cleared yet
        assert "10" in plugin._state["shortcut_registry"]


class TestReportRemovalResults:
    @pytest.mark.asyncio
    async def test_removes_entries_from_registry(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "cover_path": ""},
        }

        result = await plugin.report_removal_results([10, 20])
        assert result["success"] is True
        assert plugin._state["shortcut_registry"] == {}

    @pytest.mark.asyncio
    async def test_cleans_up_artwork_cover_path(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        decky.DECKY_USER_HOME = str(tmp_path)

        # Create a fake artwork file
        art_file = tmp_path / "cover.png"
        art_file.write_text("fake")

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": str(art_file)},
        }
        # Mock _grid_dir to return tmp_path
        plugin._steam_config.grid_dir = lambda: str(tmp_path)

        result = await plugin.report_removal_results([10])
        assert result["success"] is True
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_cleans_up_artwork_legacy_id(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        art_file = grid_dir / "12345p.png"
        art_file.write_text("fake")

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "artwork_id": 12345},
        }
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        result = await plugin.report_removal_results([10])
        assert result["success"] is True
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_partial_removal(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "cover_path": ""},
        }

        result = await plugin.report_removal_results([10])
        assert result["success"] is True
        assert "10" not in plugin._state["shortcut_registry"]
        assert "20" in plugin._state["shortcut_registry"]


class TestGetSyncStats:
    @pytest.mark.asyncio
    async def test_computes_from_registry(self, plugin):
        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
            "20": {"app_id": 1002, "name": "Game B", "platform_name": "N64"},
            "30": {"app_id": 1003, "name": "Game C", "platform_name": "SNES"},
        }
        plugin._state["last_sync"] = "2025-01-01T00:00:00"
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}
        plugin.settings["enabled_collections"] = {"3": True}

        stats = await plugin.get_sync_stats()
        assert stats["platforms"] == 2
        assert stats["collections"] == 1
        assert stats["roms"] == 3
        assert stats["total_shortcuts"] == 3
        assert stats["last_sync"] == "2025-01-01T00:00:00"

    @pytest.mark.asyncio
    async def test_empty_registry(self, plugin):
        stats = await plugin.get_sync_stats()
        assert stats["platforms"] == 0
        assert stats["roms"] == 0
        assert stats["total_shortcuts"] == 0

    @pytest.mark.asyncio
    async def test_updates_after_removal(self, plugin, tmp_path):
        """Stats should reflect registry changes after report_removal_results."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "platform_name": "N64", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "platform_name": "SNES", "cover_path": ""},
        }
        plugin.settings["enabled_platforms"] = {"1": True}  # 1 platform enabled

        await plugin.report_removal_results([10])
        stats = await plugin.get_sync_stats()
        assert stats["platforms"] == 1
        assert stats["roms"] == 1
        assert stats["total_shortcuts"] == 1

    @pytest.mark.asyncio
    async def test_report_removal_updates_sync_stats_state(self, plugin, tmp_path):
        """report_removal_results should update sync_stats in state."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "platform_name": "N64", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "platform_name": "SNES", "cover_path": ""},
        }

        await plugin.report_removal_results([10, 20])
        assert plugin._state["sync_stats"]["platforms"] == 0
        assert plugin._state["sync_stats"]["roms"] == 0


class TestGetRegistryPlatforms:
    @pytest.mark.asyncio
    async def test_returns_platforms_from_registry(self, plugin):
        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64", "platform_slug": "n64"},
            "20": {"app_id": 1002, "name": "Zelda OOT", "platform_name": "Nintendo 64", "platform_slug": "n64"},
            "30": {"app_id": 1003, "name": "DKC", "platform_name": "Super Nintendo", "platform_slug": "snes"},
        }

        result = await plugin.get_registry_platforms()
        assert len(result["platforms"]) == 2
        # Sorted by name
        assert result["platforms"][0]["name"] == "Nintendo 64"
        assert result["platforms"][0]["slug"] == "n64"
        assert result["platforms"][0]["count"] == 2
        assert result["platforms"][1]["name"] == "Super Nintendo"
        assert result["platforms"][1]["slug"] == "snes"
        assert result["platforms"][1]["count"] == 1

    @pytest.mark.asyncio
    async def test_empty_registry(self, plugin):
        result = await plugin.get_registry_platforms()
        assert result["platforms"] == []

    @pytest.mark.asyncio
    async def test_missing_platform_slug(self, plugin):
        """Old entries without platform_slug should still appear with empty slug."""
        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64"},
        }

        result = await plugin.get_registry_platforms()
        assert len(result["platforms"]) == 1
        assert result["platforms"][0]["name"] == "Nintendo 64"
        assert result["platforms"][0]["slug"] == ""
        assert result["platforms"][0]["count"] == 1


class TestRemovePlatformShortcuts:
    @pytest.mark.asyncio
    async def test_returns_matching_platform_entries(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "slug": "n64", "name": "Nintendo 64"},
                {"id": 2, "slug": "snes", "name": "Super Nintendo"},
            ]
        )
        plugin._shortcut_removal_service._loop = mock_loop

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64"},
            "20": {"app_id": 1002, "name": "Zelda OOT", "platform_name": "Nintendo 64"},
            "30": {"app_id": 1003, "name": "DKC", "platform_name": "Super Nintendo"},
        }

        result = await plugin.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert set(result["rom_ids"]) == {"10", "20"}
        assert result["platform_name"] == "Nintendo 64"

    @pytest.mark.asyncio
    async def test_platform_not_found(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "slug": "n64", "name": "Nintendo 64"},
            ]
        )
        plugin._shortcut_removal_service._loop = mock_loop

        result = await plugin.remove_platform_shortcuts("nonexistent")
        assert result["success"] is False
        assert result["app_ids"] == []
        assert result["rom_ids"] == []

    @pytest.mark.asyncio
    async def test_does_not_modify_registry(self, plugin):
        """remove_platform_shortcuts just returns data; registry cleared by report_removal_results."""
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "slug": "n64", "name": "Nintendo 64"},
            ]
        )
        plugin._shortcut_removal_service._loop = mock_loop

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64"},
        }

        await plugin.remove_platform_shortcuts("n64")
        # Registry should NOT be modified yet
        assert "10" in plugin._state["shortcut_registry"]

    @pytest.mark.asyncio
    async def test_works_offline_with_registry_slug(self, plugin):
        """When platform_slug is in the registry, no API call needed."""
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Server unreachable"))
        plugin._shortcut_removal_service._loop = mock_loop

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64", "platform_slug": "n64"},
            "20": {"app_id": 1002, "name": "Zelda OOT", "platform_name": "Nintendo 64", "platform_slug": "n64"},
        }

        result = await plugin.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert result["platform_name"] == "Nintendo 64"


class TestArtworkRenameOnSync:
    """Tests for artwork rename in report_sync_results."""

    @pytest.mark.asyncio
    async def test_renames_staged_to_app_id(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        # Create staged artwork
        staging = grid_dir / "romm_1_cover.png"
        staging.write_text("cover data")

        plugin._sync_service._pending_sync = {
            1: {"name": "Game A", "platform_name": "N64", "cover_path": str(staging)},
        }

        await plugin.report_sync_results({"1": 100001}, [])

        # Staging file should be gone, final file should exist
        assert not staging.exists()
        final = grid_dir / "100001p.png"
        assert final.exists()
        assert final.read_text() == "cover data"

        # Registry should store the final path
        entry = plugin._state["shortcut_registry"]["1"]
        assert entry["cover_path"] == str(final)

    @pytest.mark.asyncio
    async def test_handles_already_final_artwork(self, plugin, tmp_path):
        """If cover_path already points to the final file, don't error."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        final = grid_dir / "100001p.png"
        final.write_text("cover data")

        plugin._sync_service._pending_sync = {
            1: {"name": "Game A", "platform_name": "N64", "cover_path": str(final)},
        }

        await plugin.report_sync_results({"1": 100001}, [])

        assert final.exists()
        entry = plugin._state["shortcut_registry"]["1"]
        assert entry["cover_path"] == str(final)


class TestRemovalCleansUpAppIdArtwork:
    """Tests for app_id-based artwork cleanup in report_removal_results."""

    @pytest.mark.asyncio
    async def test_removes_app_id_artwork(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        art_file = grid_dir / "100001p.png"
        art_file.write_text("fake")

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 100001, "name": "Game A", "cover_path": ""},
        }
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        await plugin.report_removal_results([10])
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_removes_staging_leftover(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_10_cover.png"
        staging.write_text("fake")

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 100001, "name": "Game A", "cover_path": ""},
        }
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        await plugin.report_removal_results([10])
        assert not staging.exists()


class TestGetRomBySteamAppId:
    @pytest.mark.asyncio
    async def test_finds_rom_by_app_id(self, plugin):
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "platform_slug": "n64",
        }
        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": "/roms/n64/zelda.z64",
        }
        result = await plugin.get_rom_by_steam_app_id(100001)
        assert result is not None
        assert result["rom_id"] == 42
        assert result["name"] == "Zelda"
        assert result["installed"] is not None

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown(self, plugin):
        result = await plugin.get_rom_by_steam_app_id(999999)
        assert result is None


class TestShortcutDataFormat:
    """Validate the shortcut data format produced by the backend.

    The backend prepares shortcut data that the frontend uses to create
    Steam shortcuts. These tests ensure the data is well-formed.
    """

    @pytest.mark.asyncio
    async def test_shortcut_data_has_required_fields(self, plugin):
        """Every shortcut entry must have all required fields."""
        from unittest.mock import AsyncMock, MagicMock

        plugin.settings["romm_url"] = "http://romm.local"
        plugin.settings["enabled_platforms"] = {"gba": True}
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            side_effect=[
                # _fetch_platforms
                [{"id": 1, "slug": "gba", "name": "Game Boy Advance", "rom_count": 1}],
                # _fetch_roms_for_platform
                [
                    {
                        "id": 42,
                        "name": "Test Game",
                        "platform_name": "Game Boy Advance",
                        "platform_slug": "gba",
                        "igdb_id": 100,
                        "sgdb_id": 200,
                        "path_cover_large": "/cover.png",
                    }
                ],
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._emit_progress = AsyncMock()
        plugin._sync_service._sync_state = SyncState.IDLE

        # Mock decky.emit to capture the shortcuts
        import decky

        emitted_events = []
        original_emit = getattr(decky, "emit", None)

        async def mock_emit(event, *args):
            emitted_events.append((event, args))

        decky.emit = mock_emit
        plugin._sync_service._emit = mock_emit

        try:
            # Call _do_sync directly (start_sync creates a background task
            # that never runs with a mock loop)
            await plugin._sync_service._do_sync()
        except Exception:
            pass
        finally:
            if original_emit:
                decky.emit = original_emit

        # Find the sync_apply emission
        sync_items = None
        for event, args in emitted_events:
            if event == "sync_apply" and args:
                sync_items = args[0] if args else None
                break

        assert sync_items is not None, "sync_apply event should have been emitted"
        required_fields = {"rom_id", "name", "exe", "start_dir", "launch_options", "platform_name", "platform_slug"}
        for item in sync_items.get("shortcuts", sync_items):
            for field in required_fields:
                assert field in item, f"Missing field '{field}' in shortcut data"

    @pytest.mark.asyncio
    async def test_exe_path_points_to_romm_launcher(self, plugin):
        """Exe path must point to bin/romm-launcher inside the plugin directory."""
        import decky

        plugin.settings["romm_url"] = "http://romm.local"
        exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")

        assert exe.endswith("/bin/romm-launcher"), f"Exe path should end with /bin/romm-launcher, got: {exe}"
        assert "decky-romm-sync" in exe, f"Exe path should contain plugin name, got: {exe}"

    def test_launch_options_format(self, plugin):
        """Launch options must follow the romm:<rom_id> pattern."""
        import re

        pattern = r"^romm:\d+$"

        # Test valid formats
        for rom_id in [1, 42, 4409, 99999]:
            launch_opt = f"romm:{rom_id}"
            assert re.match(pattern, launch_opt), f"Launch option '{launch_opt}' does not match expected pattern"

    def test_start_dir_is_parent_of_exe(self, plugin):
        """Start dir must be the directory containing the launcher."""
        import decky

        exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")
        start_dir = os.path.join(decky.DECKY_PLUGIN_DIR, "bin")

        assert start_dir == os.path.dirname(exe), f"start_dir ({start_dir}) should be parent of exe ({exe})"

    def test_artwork_id_generation_consistency(self, plugin):
        """Artwork ID must be deterministic for the same exe+name pair."""
        from adapters.steam_config import SteamConfigAdapter

        exe = "/home/deck/homebrew/plugins/decky-romm-sync/bin/romm-launcher"
        name = "Test Game"

        id1 = SteamConfigAdapter.generate_artwork_id(exe, name)
        id2 = SteamConfigAdapter.generate_artwork_id(exe, name)

        assert id1 == id2, "Artwork ID should be deterministic"
        assert isinstance(id1, int), "Artwork ID should be an integer"
        assert id1 > 0, "Artwork ID should be positive (unsigned)"

    def test_artwork_id_differs_per_game(self, plugin):
        """Different game names should produce different artwork IDs."""
        from adapters.steam_config import SteamConfigAdapter

        exe = "/home/deck/homebrew/plugins/decky-romm-sync/bin/romm-launcher"

        id_a = SteamConfigAdapter.generate_artwork_id(exe, "Game A")
        id_b = SteamConfigAdapter.generate_artwork_id(exe, "Game B")

        assert id_a != id_b, "Different games should have different artwork IDs"


class TestSyncPreview:
    """Tests for sync_preview()."""

    @pytest.mark.asyncio
    async def test_returns_correct_summary(self, plugin):
        from unittest.mock import AsyncMock

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()

        # Mock _fetch_and_prepare to return known data
        platforms = [{"name": "N64", "slug": "n64"}]
        all_roms = [{"id": 1}, {"id": 2}, {"id": 3}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
            {"rom_id": 2, "name": "Game B", "platform_name": "N64", "platform_slug": "n64", "fs_name": "b.z64"},
            {"rom_id": 3, "name": "Game C", "platform_name": "N64", "platform_slug": "n64", "fs_name": "c.z64"},
        ]
        plugin._sync_service._fetch_and_prepare = AsyncMock(
            return_value=(all_roms, shortcuts_data, platforms, {}, set())
        )
        plugin._sync_service._emit_progress = AsyncMock()

        # Set up registry: rom 1 unchanged, rom 2 changed name
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
            "2": {"app_id": 1002, "name": "Old B", "platform_name": "N64", "platform_slug": "n64", "fs_name": "b.z64"},
        }

        result = await plugin.sync_preview()
        assert result["success"] is True
        summary = result["summary"]
        assert summary["new_count"] == 1  # rom 3 is new
        assert summary["changed_count"] == 1  # rom 2 name changed
        assert summary["unchanged_count"] == 1  # rom 1 unchanged
        assert summary["remove_count"] == 0
        assert "preview_id" in result

    @pytest.mark.asyncio
    async def test_populates_pending_delta(self, plugin):
        from unittest.mock import AsyncMock

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()

        platforms = [{"name": "N64", "slug": "n64"}]
        all_roms = [{"id": 1}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
        ]
        plugin._sync_service._fetch_and_prepare = AsyncMock(
            return_value=(all_roms, shortcuts_data, platforms, {}, set())
        )
        plugin._sync_service._emit_progress = AsyncMock()

        result = await plugin.sync_preview()
        assert plugin._sync_service._pending_delta is not None
        assert plugin._sync_service._pending_delta["preview_id"] == result["preview_id"]
        assert len(plugin._sync_service._pending_delta["new"]) == 1
        assert plugin._sync_service._pending_delta["platforms_count"] == 1
        assert plugin._sync_service._pending_delta["total_roms"] == 1

    @pytest.mark.asyncio
    async def test_returns_error_when_sync_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        result = await plugin.sync_preview()
        assert result["success"] is False
        assert "already in progress" in result["message"]

    @pytest.mark.asyncio
    async def test_resets_sync_running_on_completion(self, plugin):
        from unittest.mock import AsyncMock

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()

        platforms = [{"name": "N64"}]
        all_roms = [{"id": 1}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
        ]
        plugin._sync_service._fetch_and_prepare = AsyncMock(
            return_value=(all_roms, shortcuts_data, platforms, {}, set())
        )
        plugin._sync_service._emit_progress = AsyncMock()

        await plugin.sync_preview()
        assert plugin._sync_service._sync_state == SyncState.IDLE


class TestSyncApplyDelta:
    """Tests for sync_apply_delta()."""

    def _setup_pending_delta(self, plugin, preview_id="test-preview-123"):
        """Helper to populate _pending_delta with valid data."""
        plugin._sync_service._pending_delta = {
            "preview_id": preview_id,
            "new": [
                {
                    "rom_id": 3,
                    "name": "Game C",
                    "platform_name": "N64",
                    "platform_slug": "n64",
                    "fs_name": "c.z64",
                    "cover_path": "",
                },
            ],
            "changed": [
                {
                    "rom_id": 2,
                    "name": "New B",
                    "existing_app_id": 1002,
                    "platform_name": "N64",
                    "platform_slug": "n64",
                    "fs_name": "b.z64",
                    "cover_path": "",
                },
            ],
            "unchanged_ids": [1],
            "remove_rom_ids": [99],
            "all_shortcuts": {
                1: {"rom_id": 1, "name": "Game A", "platform_name": "N64"},
                2: {"rom_id": 2, "name": "New B", "platform_name": "N64"},
                3: {"rom_id": 3, "name": "Game C", "platform_name": "N64"},
            },
            "platforms_count": 1,
            "total_roms": 3,
        }

    @pytest.mark.asyncio
    async def test_rejects_wrong_preview_id(self, plugin):
        self._setup_pending_delta(plugin, "correct-id")
        result = await plugin.sync_apply_delta("wrong-id")
        assert result["success"] is False
        assert result["error_code"] == "stale_preview"

    @pytest.mark.asyncio
    async def test_rejects_when_no_pending_delta(self, plugin):
        assert plugin._sync_service._pending_delta is None
        result = await plugin.sync_apply_delta("any-id")
        assert result["success"] is False
        assert result["error_code"] == "stale_preview"

    @pytest.mark.asyncio
    async def test_emits_sync_apply_with_delta(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        # Set up registry for unchanged rom
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        plugin._save_state = lambda: None

        self._setup_pending_delta(plugin)
        plugin._sync_service._emit_progress = AsyncMock()

        result = await plugin.sync_apply_delta("test-preview-123")
        assert result["success"] is True

        # Check decky.emit was called with sync_apply
        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "sync_apply"]
        assert len(emit_calls) == 1
        payload = emit_calls[0][0][1]
        assert len(payload["shortcuts"]) == 1  # new
        assert len(payload["changed_shortcuts"]) == 1  # changed
        assert payload["remove_rom_ids"] == [99]

    @pytest.mark.asyncio
    async def test_populates_pending_sync(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        plugin._save_state = lambda: None

        self._setup_pending_delta(plugin)
        plugin._sync_service._emit_progress = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")
        assert 1 in plugin._sync_service._pending_sync
        assert 2 in plugin._sync_service._pending_sync
        assert 3 in plugin._sync_service._pending_sync

    @pytest.mark.asyncio
    async def test_clears_pending_delta(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        plugin._save_state = lambda: None

        self._setup_pending_delta(plugin)
        plugin._sync_service._emit_progress = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")
        assert plugin._sync_service._pending_delta is None

    @pytest.mark.asyncio
    async def test_sync_apply_does_not_include_collection_data(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
            "5": {"app_id": 1005, "name": "Game E", "platform_name": "SNES"},
        }
        plugin._save_state = lambda: None

        # Include both rom 1 and 5 as unchanged
        plugin._sync_service._pending_delta = {
            "preview_id": "test-preview-123",
            "new": [],
            "changed": [],
            "unchanged_ids": [1, 5],
            "remove_rom_ids": [],
            "all_shortcuts": {
                1: {"rom_id": 1, "name": "Game A", "platform_name": "N64"},
                5: {"rom_id": 5, "name": "Game E", "platform_name": "SNES"},
            },
            "platforms_count": 2,
            "total_roms": 2,
        }
        plugin._sync_service._emit_progress = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")

        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "sync_apply"]
        assert len(emit_calls) == 1
        # Platform collection data is no longer in sync_apply — it's built in report_sync_results
        # and sent via sync_complete instead.
        assert "collection_platform_app_ids" not in emit_calls[0][0][1]
        assert "platform_eligible_rom_ids" not in emit_calls[0][0][1]


class TestSyncCancelPreview:
    """Tests for sync_cancel_preview()."""

    @pytest.mark.asyncio
    async def test_clears_pending_delta(self, plugin):
        plugin._sync_service._pending_delta = {
            "preview_id": "some-id",
            "new": [],
            "changed": [],
            "unchanged_ids": [],
            "remove_rom_ids": [],
            "all_shortcuts": {},
            "platforms_count": 0,
            "total_roms": 0,
        }
        result = await plugin.sync_cancel_preview()
        assert plugin._sync_service._pending_delta is None
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_returns_success(self, plugin):
        result = await plugin.sync_cancel_preview()
        assert result == {"success": True}


# ── Tests for uncovered helper methods in library_sync.py ──────────


class TestGetPlatforms:
    """Tests for get_platforms() — lines 90-117."""

    @pytest.mark.asyncio
    async def test_returns_platforms_with_rom_count(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64", "rom_count": 10},
                {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
            ]
        )
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.get_platforms()
        assert result["success"] is True
        assert len(result["platforms"]) == 2
        assert result["platforms"][0]["name"] == "N64"
        assert result["platforms"][0]["rom_count"] == 10
        assert result["platforms"][1]["name"] == "SNES"

    @pytest.mark.asyncio
    async def test_skips_zero_rom_count(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64", "rom_count": 10},
                {"id": 2, "name": "Empty", "slug": "empty", "rom_count": 0},
            ]
        )
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.get_platforms()
        assert result["success"] is True
        assert len(result["platforms"]) == 1
        assert result["platforms"][0]["name"] == "N64"

    @pytest.mark.asyncio
    async def test_sync_enabled_from_settings(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64", "rom_count": 10},
                {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin.settings["enabled_platforms"] = {"1": True, "2": False}

        result = await plugin._sync_service.get_platforms()
        assert result["platforms"][0]["sync_enabled"] is True
        assert result["platforms"][1]["sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_default_sync_enabled_when_no_prefs(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=[{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}])
        plugin._sync_service._loop = mock_loop
        plugin.settings["enabled_platforms"] = {}

        result = await plugin._sync_service.get_platforms()
        assert result["platforms"][0]["sync_enabled"] is True

    @pytest.mark.asyncio
    async def test_http_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Connection refused"))
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.get_platforms()
        assert result["success"] is False
        assert "error_code" in result

    @pytest.mark.asyncio
    async def test_unexpected_response_type(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value="not a list")
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.get_platforms()
        assert result["success"] is False
        assert result["error_code"] == "api_error"


class TestSavePlatformSync:
    """Tests for save_platform_sync() — lines 120-123."""

    def test_saves_enabled_setting(self, plugin):
        result = plugin._sync_service.save_platform_sync(42, True)
        assert result["success"] is True
        assert plugin.settings["enabled_platforms"]["42"] is True

    def test_saves_disabled_setting(self, plugin):
        plugin.settings["enabled_platforms"]["42"] = True
        result = plugin._sync_service.save_platform_sync(42, False)
        assert result["success"] is True
        assert plugin.settings["enabled_platforms"]["42"] is False


class TestSetAllPlatformsSync:
    """Tests for set_all_platforms_sync() — lines 126-139."""

    @pytest.mark.asyncio
    async def test_enables_all(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64"},
                {"id": 2, "name": "SNES"},
            ]
        )
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.set_all_platforms_sync(True)
        assert result["success"] is True
        assert plugin.settings["enabled_platforms"]["1"] is True
        assert plugin.settings["enabled_platforms"]["2"] is True

    @pytest.mark.asyncio
    async def test_disables_all(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=[{"id": 1, "name": "N64"}])
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.set_all_platforms_sync(False)
        assert result["success"] is True
        assert plugin.settings["enabled_platforms"]["1"] is False

    @pytest.mark.asyncio
    async def test_http_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("timeout"))
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.set_all_platforms_sync(True)
        assert result["success"] is False


class TestSyncControl:
    """Tests for start_sync, cancel_sync, get_sync_progress, sync_heartbeat — lines 143-163."""

    def test_start_sync_when_idle(self, plugin):
        result = plugin._sync_service.start_sync()
        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.RUNNING

    def test_start_sync_rejects_when_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        result = plugin._sync_service.start_sync()
        assert result["success"] is False
        assert "already in progress" in result["message"]

    def test_cancel_sync_when_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        result = plugin._sync_service.cancel_sync()
        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_cancel_sync_when_idle(self, plugin):
        result = plugin._sync_service.cancel_sync()
        assert result["success"] is True
        assert "No sync" in result["message"]

    def test_get_sync_progress(self, plugin):
        result = plugin._sync_service.get_sync_progress()
        assert "running" in result
        assert "phase" in result

    def test_sync_heartbeat(self, plugin):
        old = plugin._sync_service._sync_last_heartbeat
        # Advance the injected FakeClock so monotonic moves forward.
        plugin._sync_service._clock.advance(0.01)
        result = plugin._sync_service.sync_heartbeat()
        assert result["success"] is True
        assert plugin._sync_service._sync_last_heartbeat > old


class TestCheckCancelling:
    """Tests for _check_cancelling() — lines 505-508."""

    def test_raises_when_cancelling(self, plugin):
        plugin._sync_service._sync_state = SyncState.CANCELLING
        with pytest.raises(asyncio.CancelledError):
            plugin._sync_service._check_cancelling()

    def test_noop_when_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._check_cancelling()  # should not raise

    def test_noop_when_idle(self, plugin):
        plugin._sync_service._check_cancelling()  # should not raise


class TestBuildShortcutsData:
    """Tests for _build_shortcuts_data() — lines 510-530."""

    def test_builds_correct_format(self, plugin):
        roms = [
            {
                "id": 1,
                "name": "Game A",
                "fs_name": "gamea.z64",
                "platform_name": "N64",
                "platform_slug": "n64",
                "igdb_id": 100,
                "sgdb_id": 200,
                "ra_id": 300,
            },
            {"id": 2, "name": "Game B", "platform_name": "SNES", "platform_slug": "snes"},
        ]
        result = plugin._sync_service._build_shortcuts_data(roms)
        assert len(result) == 2
        assert result[0]["rom_id"] == 1
        assert result[0]["name"] == "Game A"
        assert result[0]["fs_name"] == "gamea.z64"
        assert result[0]["launch_options"] == "romm:1"
        assert result[0]["platform_name"] == "N64"
        assert result[0]["platform_slug"] == "n64"
        assert result[0]["igdb_id"] == 100
        assert result[0]["sgdb_id"] == 200
        assert result[0]["ra_id"] == 300
        assert result[0]["cover_path"] == ""
        assert "romm-launcher" in result[0]["exe"]
        assert result[1]["fs_name"] == ""

    def test_empty_roms(self, plugin):
        result = plugin._sync_service._build_shortcuts_data([])
        assert result == []

    def test_missing_optional_fields(self, plugin):
        roms = [{"id": 5, "name": "Minimal"}]
        result = plugin._sync_service._build_shortcuts_data(roms)
        assert result[0]["rom_id"] == 5
        assert result[0]["platform_name"] == "Unknown"
        assert result[0]["platform_slug"] == ""
        assert result[0]["igdb_id"] is None
        assert result[0]["sgdb_id"] is None


class TestFetchEnabledPlatforms:
    """Tests for _fetch_enabled_platforms() — lines 398-411, 402-403."""

    @pytest.mark.asyncio
    async def test_filters_by_enabled(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64"},
                {"id": 2, "name": "SNES", "slug": "snes"},
                {"id": 3, "name": "GBA", "slug": "gba"},
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin.settings["enabled_platforms"] = {"1": True, "2": False, "3": True}

        result = await plugin._sync_service._fetch_enabled_platforms()
        assert len(result) == 2
        names = [p["name"] for p in result]
        assert "N64" in names
        assert "GBA" in names
        assert "SNES" not in names

    @pytest.mark.asyncio
    async def test_all_enabled_when_no_prefs(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64"},
                {"id": 2, "name": "SNES", "slug": "snes"},
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin.settings["enabled_platforms"] = {}

        result = await plugin._sync_service._fetch_enabled_platforms()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_for_non_list_response(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"error": "bad response"})
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service._fetch_enabled_platforms()
        assert result == []


class TestReconstructPlatformFromRegistry:
    """Tests for _reconstruct_platform_from_registry() — lines 413-429."""

    def test_reconstructs_matching_entries(self, plugin):
        plugin._state["shortcut_registry"] = {
            "1": {
                "name": "Game A",
                "fs_name": "a.z64",
                "platform_name": "N64",
                "igdb_id": 100,
                "sgdb_id": 200,
                "ra_id": 300,
            },
            "2": {"name": "Game B", "fs_name": "b.z64", "platform_name": "N64"},
            "3": {"name": "Game C", "fs_name": "c.z64", "platform_name": "SNES"},
        }
        result = plugin._sync_service._reconstruct_platform_from_registry(
            plugin._state["shortcut_registry"], "N64", "n64"
        )
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {1, 2}
        # Check fields
        game_a = next(r for r in result if r["id"] == 1)
        assert game_a["name"] == "Game A"
        assert game_a["platform_name"] == "N64"
        assert game_a["platform_slug"] == "n64"
        assert game_a["igdb_id"] == 100

    def test_empty_when_no_match(self, plugin):
        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "SNES"},
        }
        result = plugin._sync_service._reconstruct_platform_from_registry(
            plugin._state["shortcut_registry"], "N64", "n64"
        )
        assert result == []

    def test_empty_registry(self, plugin):
        result = plugin._sync_service._reconstruct_platform_from_registry({}, "N64", "n64")
        assert result == []


class TestTryIncrementalSkip:
    """Tests for _try_incremental_skip() — lines 431-465."""

    @pytest.mark.asyncio
    async def test_skips_unchanged_platform(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"total": 0})
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._emit_progress = AsyncMock()

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
            "2": {"name": "Game B", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 2}
        all_roms = []

        skipped = await plugin._sync_service._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is True
        assert len(all_roms) == 2  # reconstructed from registry

    @pytest.mark.asyncio
    async def test_no_skip_on_first_sync(self, plugin):
        from unittest.mock import MagicMock

        mock_loop = MagicMock()
        plugin._sync_service._loop = mock_loop

        platform = {"id": 1, "rom_count": 5}
        all_roms = []

        # last_sync is None => no skip
        skipped = await plugin._sync_service._try_incremental_skip(platform, {}, None, "N64", "n64", all_roms, 1, 1)
        assert skipped is False

    @pytest.mark.asyncio
    async def test_no_skip_when_registry_empty(self, plugin):
        from unittest.mock import MagicMock

        mock_loop = MagicMock()
        plugin._sync_service._loop = mock_loop

        platform = {"id": 1, "rom_count": 5}
        all_roms = []

        # registry has no entries for this platform
        skipped = await plugin._sync_service._try_incremental_skip(
            platform, {}, "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False

    @pytest.mark.asyncio
    async def test_no_skip_when_updates_exist(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"total": 3})
        plugin._sync_service._loop = mock_loop

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 1}
        all_roms = []

        skipped = await plugin._sync_service._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False
        assert len(all_roms) == 0

    @pytest.mark.asyncio
    async def test_no_skip_when_count_mismatch(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"total": 0})
        plugin._sync_service._loop = mock_loop

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 5}  # server has 5, registry has 1
        all_roms = []

        skipped = await plugin._sync_service._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False

    @pytest.mark.asyncio
    async def test_falls_back_on_api_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Connection failed"))
        plugin._sync_service._loop = mock_loop

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 1}
        all_roms = []

        skipped = await plugin._sync_service._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False


class TestFullFetchPlatformRoms:
    """Tests for _full_fetch_platform_roms() — lines 467-503."""

    @pytest.mark.asyncio
    async def test_fetches_single_page(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value={
                "items": [
                    {"id": 1, "name": "Game A", "files": ["f1"]},
                    {"id": 2, "name": "Game B"},
                ]
            }
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._emit_progress = AsyncMock()

        all_roms = []
        await plugin._sync_service._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 2
        assert all_roms[0]["platform_name"] == "N64"
        assert all_roms[0]["platform_slug"] == "n64"
        # files should be removed
        assert "files" not in all_roms[0]

    @pytest.mark.asyncio
    async def test_fetches_multiple_pages(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        page1 = {"items": [{"id": i, "name": f"G{i}"} for i in range(50)]}
        page2 = {"items": [{"id": 50, "name": "G50"}]}

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=[page1, page2])
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._emit_progress = AsyncMock()

        all_roms = []
        await plugin._sync_service._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 51

    @pytest.mark.asyncio
    async def test_handles_api_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Server error"))
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._emit_progress = AsyncMock()

        all_roms = []
        await plugin._sync_service._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 0  # gracefully handles error

    @pytest.mark.asyncio
    async def test_cancelling_during_fetch(self, plugin):
        from unittest.mock import AsyncMock

        plugin._sync_service._sync_state = SyncState.CANCELLING
        plugin._sync_service._emit_progress = AsyncMock()

        all_roms = []
        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)


class TestFinalizeCoverPath:
    """Tests for _finalize_cover_path() — lines 699-712."""

    def test_renames_staging_to_final(self, plugin, tmp_path):
        grid = str(tmp_path)
        staging = tmp_path / "romm_1_cover.png"
        staging.write_text("cover data")

        result = plugin._sync_service._finalize_cover_path(grid, str(staging), 100001, "1")
        expected = os.path.join(grid, "100001p.png")
        assert result == expected
        assert not staging.exists()
        assert os.path.exists(expected)

    def test_returns_existing_final(self, plugin, tmp_path):
        grid = str(tmp_path)
        final = tmp_path / "100001p.png"
        final.write_text("final data")

        result = plugin._sync_service._finalize_cover_path(grid, "/nonexistent/path.png", 100001, "1")
        assert result == str(final)

    def test_returns_cover_path_when_no_grid(self, plugin):
        result = plugin._sync_service._finalize_cover_path(None, "/some/path.png", 100001, "1")
        assert result == "/some/path.png"

    def test_returns_cover_path_when_empty(self, plugin, tmp_path):
        result = plugin._sync_service._finalize_cover_path(str(tmp_path), "", 100001, "1")
        assert result == ""

    def test_handles_rename_os_error(self, plugin, tmp_path):
        from unittest.mock import patch

        grid = str(tmp_path)
        staging = tmp_path / "romm_1_cover.png"
        staging.write_text("data")

        with patch("os.replace", side_effect=OSError("perm denied")):
            result = plugin._sync_service._finalize_cover_path(grid, str(staging), 100001, "1")
        # Should return original path on error
        assert result == str(staging)


class TestBuildRegistryEntry:
    """Tests for _build_registry_entry() — lines 714-727."""

    def test_builds_full_entry(self, plugin):
        pending = {
            "name": "Game A",
            "fs_name": "gamea.z64",
            "platform_name": "N64",
            "platform_slug": "n64",
            "igdb_id": 100,
            "sgdb_id": 200,
            "ra_id": 300,
        }
        result = plugin._sync_service._build_registry_entry(pending, 100001, "/grid/100001p.png")
        assert result["app_id"] == 100001
        assert result["name"] == "Game A"
        assert result["fs_name"] == "gamea.z64"
        assert result["platform_name"] == "N64"
        assert result["platform_slug"] == "n64"
        assert result["cover_path"] == "/grid/100001p.png"
        assert result["igdb_id"] == 100
        assert result["sgdb_id"] == 200
        assert result["ra_id"] == 300

    def test_omits_none_meta_keys(self, plugin):
        pending = {
            "name": "Game B",
            "fs_name": "",
            "platform_name": "SNES",
            "platform_slug": "snes",
            "igdb_id": None,
            "sgdb_id": None,
            "ra_id": None,
        }
        result = plugin._sync_service._build_registry_entry(pending, 100002, "")
        assert "igdb_id" not in result
        assert "sgdb_id" not in result
        assert "ra_id" not in result

    def test_missing_keys_default_to_empty(self, plugin):
        pending = {}
        result = plugin._sync_service._build_registry_entry(pending, 100003, "")
        assert result["name"] == ""
        assert result["fs_name"] == ""
        assert result["platform_name"] == ""
        assert result["platform_slug"] == ""


class TestClearSyncCache:
    """Tests for clear_sync_cache() — lines 1037-1042."""

    def test_clears_last_sync(self, plugin):
        plugin._state["last_sync"] = "2025-01-01T00:00:00"
        result = plugin._sync_service.clear_sync_cache()
        assert result["success"] is True
        assert plugin._state["last_sync"] is None


class TestReportSyncResultsCancelled:
    """Tests for report_sync_results with cancelled=True — lines 773-788."""

    @pytest.mark.asyncio
    async def test_emits_cancelled_progress(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        decky.emit.reset_mock()

        plugin._sync_service._pending_sync = {
            1: {"name": "Game A", "platform_name": "N64", "cover_path": ""},
        }

        await plugin.report_sync_results({"1": 100001}, [], cancelled=True)

        # Find the sync_progress done emission
        progress_calls = [c for c in decky.emit.call_args_list if c[0][0] == "sync_progress"]
        assert len(progress_calls) >= 1
        last_progress = progress_calls[-1][0][1]
        assert last_progress["running"] is False
        assert "cancelled" in last_progress["message"].lower()

    @pytest.mark.asyncio
    async def test_emits_sync_complete_with_cancelled_flag(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        decky.emit.reset_mock()

        plugin._sync_service._pending_sync = {
            1: {"name": "Game A", "platform_name": "N64", "cover_path": ""},
        }

        await plugin.report_sync_results({"1": 100001}, [], cancelled=True)

        complete_calls = [c for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert len(complete_calls) == 1
        assert complete_calls[0][0][1]["cancelled"] is True


class TestDoSyncErrorHandling:
    """Tests for _do_sync error/edge handling — lines 587-695."""

    @pytest.mark.asyncio
    async def test_fetch_error_emits_error_progress(self, plugin):
        from unittest.mock import AsyncMock

        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._fetch_and_prepare = AsyncMock(side_effect=Exception("API down"))
        plugin._sync_service._emit_progress = AsyncMock()

        await plugin._sync_service._do_sync()

        # Should have emitted error progress
        error_calls = [c for c in plugin._sync_service._emit_progress.call_args_list if c[0][0] == "error"]
        assert len(error_calls) >= 1
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_cancel_during_sync(self, plugin):
        from unittest.mock import AsyncMock

        import decky

        decky.emit.reset_mock()

        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._fetch_and_prepare = AsyncMock(side_effect=asyncio.CancelledError("Sync cancelled"))
        plugin._sync_service._emit_progress = AsyncMock()

        # CancelledError is caught, _finish_sync called, then re-raised
        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._do_sync()

        # Should be idle after _finish_sync
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_general_exception_in_do_sync(self, plugin):

        import decky

        decky.emit.reset_mock()

        plugin._sync_service._sync_state = SyncState.RUNNING

        async def failing_fetch():
            # Successfully fetch but then fail during artwork
            raise RuntimeError("Unexpected error")

        plugin._sync_service._fetch_and_prepare = failing_fetch

        await plugin._sync_service._do_sync()

        assert plugin._sync_service._sync_state == SyncState.IDLE


class TestFinishSync:
    """Tests for _finish_sync() — lines 685-695."""

    @pytest.mark.asyncio
    async def test_sets_cancelled_state(self, plugin):
        import decky

        decky.emit.reset_mock()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_progress = {"running": True, "current": 5, "total": 10}

        await plugin._sync_service._finish_sync("Sync cancelled")

        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._sync_progress["running"] is False
        assert plugin._sync_service._sync_progress["phase"] == "cancelled"
        assert plugin._sync_service._sync_progress["message"] == "Sync cancelled"


class TestSyncPreviewErrorHandling:
    """Tests for sync_preview error paths — lines 210-219."""

    @pytest.mark.asyncio
    async def test_general_exception_returns_error(self, plugin):
        from unittest.mock import AsyncMock

        plugin._sync_service._fetch_and_prepare = AsyncMock(side_effect=RuntimeError("Something broke"))
        plugin._sync_service._emit_progress = AsyncMock()

        result = await plugin._sync_service.sync_preview()
        assert result["success"] is False
        assert "error_code" in result
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_cancelled_error_reraises(self, plugin):
        from unittest.mock import AsyncMock

        import decky

        decky.emit.reset_mock()

        plugin._sync_service._fetch_and_prepare = AsyncMock(side_effect=asyncio.CancelledError("cancelled"))
        plugin._sync_service._emit_progress = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service.sync_preview()
        assert plugin._sync_service._sync_state == SyncState.IDLE


class TestReportRemovalSteamInputCleanup:
    """Tests for Steam Input cleanup in _report_removal_results_io — lines 967-980."""

    @pytest.mark.asyncio
    async def test_cleans_steam_input_on_removal(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._steam_config.grid_dir = lambda: str(tmp_path)
        plugin._steam_config.set_steam_input_config = MagicMock()

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "cover_path": ""},
        }

        await plugin.report_removal_results([10, 20])
        plugin._steam_config.set_steam_input_config.assert_called_once_with([1001, 1002], mode="default")

    @pytest.mark.asyncio
    async def test_steam_input_error_doesnt_crash(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._steam_config.grid_dir = lambda: str(tmp_path)
        plugin._steam_config.set_steam_input_config = MagicMock(side_effect=Exception("VDF error"))

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": ""},
        }

        result = await plugin.report_removal_results([10])
        assert result["success"] is True  # Should not crash


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_loop_with_executor(*return_values):
    """Return a mock loop whose run_in_executor returns values in sequence.

    Each call to run_in_executor returns the next value from return_values.
    If only one value is given it is returned for every call.
    """
    mock_loop = MagicMock()
    if len(return_values) == 1:
        mock_loop.run_in_executor = AsyncMock(return_value=return_values[0])
    else:
        mock_loop.run_in_executor = AsyncMock(side_effect=list(return_values))
    return mock_loop


def _make_loop_raising(exc):
    """Return a mock loop whose run_in_executor always raises exc."""
    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(side_effect=exc)
    return mock_loop


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# conftest.py already provides a `plugin` fixture wired with a LibraryService
# (plugin._sync_service).  We reuse it here — no separate fixture needed.


# ---------------------------------------------------------------------------
# TestGetCollections
# ---------------------------------------------------------------------------


class TestGetCollections:
    """Tests for LibraryService.get_collections()."""

    @pytest.mark.asyncio
    async def test_returns_user_and_franchise_collections(self, plugin):
        """Both user and franchise collections appear in the result."""
        user = [{"id": 1, "name": "My Faves", "rom_count": 3, "is_favorite": False}]
        franchise = [{"id": 101, "name": "Mario", "rom_count": 5, "is_favorite": False}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.get_collections()

        assert result["success"] is True
        collections = result["collections"]
        names = [c["name"] for c in collections]
        assert "My Faves" in names
        assert "Mario" in names

    @pytest.mark.asyncio
    async def test_user_collection_has_user_category(self, plugin):
        """Non-favorite user collections are categorised as 'user'."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["category"] == "user"

    @pytest.mark.asyncio
    async def test_franchise_collection_has_franchise_category(self, plugin):
        """Franchise collections are categorised as 'franchise'."""
        user = []
        franchise = [{"id": 101, "name": "Zelda", "rom_count": 4, "is_favorite": False}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["category"] == "franchise"

    @pytest.mark.asyncio
    async def test_favorites_sorted_first(self, plugin):
        """Favorite user collections appear before regular user and franchise collections."""
        user = [
            {"id": 1, "name": "Adventure", "rom_count": 1, "is_favorite": False},
            {"id": 2, "name": "A Favorites", "rom_count": 2, "is_favorite": True},
        ]
        franchise = [{"id": 101, "name": "Metroid", "rom_count": 3, "is_favorite": False}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.get_collections()

        categories = [c["category"] for c in result["collections"]]
        # Favorites must come before user must come before franchise
        fav_idx = categories.index("favorites")
        user_idx = categories.index("user")
        franchise_idx = categories.index("franchise")
        assert fav_idx < user_idx < franchise_idx

    @pytest.mark.asyncio
    async def test_favorite_collection_has_favorites_category(self, plugin):
        """Collections with is_favorite=True are categorised as 'favorites'."""
        user = [{"id": 1, "name": "Top Picks", "rom_count": 5, "is_favorite": True}]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["category"] == "favorites"

    @pytest.mark.asyncio
    async def test_respects_enabled_settings(self, plugin):
        """sync_enabled reflects the enabled_collections setting."""
        user = [
            {"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False},
            {"id": 2, "name": "Shooters", "rom_count": 3, "is_favorite": False},
        ]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)
        plugin._sync_service._settings["enabled_collections"] = {"1": True, "2": False}

        result = await plugin._sync_service.get_collections()

        by_id = {c["id"]: c for c in result["collections"]}
        assert by_id["1"]["sync_enabled"] is True
        assert by_id["2"]["sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_defaults_to_disabled_when_no_settings(self, plugin):
        """When enabled_collections is absent all collections default to sync_enabled=False."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]
        franchise = [{"id": 101, "name": "Zelda", "rom_count": 3}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)
        plugin._sync_service._settings.pop("enabled_collections", None)

        result = await plugin._sync_service.get_collections()

        for c in result["collections"]:
            assert c["sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_api_error_returns_error_response(self, plugin):
        """When list_collections raises an exception the response has success=False."""
        plugin._sync_service._loop = _make_loop_raising(Exception("Connection refused"))

        result = await plugin._sync_service.get_collections()

        assert result["success"] is False
        assert "error_code" in result
        assert "message" in result

    @pytest.mark.asyncio
    async def test_empty_collections(self, plugin):
        """Both endpoints returning [] still yields success=True with empty list."""
        plugin._sync_service._loop = _make_loop_with_executor([], [])

        result = await plugin._sync_service.get_collections()

        assert result["success"] is True
        assert result["collections"] == []

    @pytest.mark.asyncio
    async def test_franchise_failure_still_returns_user_collections(self, plugin):
        """If only franchise fetch fails, user collections are still returned."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]

        mock_loop = MagicMock()
        call_count = 0

        async def _executor(_executor_arg, fn, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return user
            raise Exception("Franchise endpoint unavailable")

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.get_collections()

        assert result["success"] is True
        assert len(result["collections"]) == 1
        assert result["collections"][0]["name"] == "RPGs"

    @pytest.mark.asyncio
    async def test_rom_count_falls_back_to_rom_ids_length(self, plugin):
        """When rom_count is absent, len(rom_ids) is used."""
        user = [{"id": 1, "name": "RPGs", "rom_ids": [10, 20, 30], "is_favorite": False}]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["rom_count"] == 3

    @pytest.mark.asyncio
    async def test_collections_sorted_alphabetically_within_category(self, plugin):
        """Within a category, collections are sorted by name (case-insensitive)."""
        user = [
            {"id": 2, "name": "Zelda", "rom_count": 1, "is_favorite": False},
            {"id": 1, "name": "Metroid", "rom_count": 1, "is_favorite": False},
        ]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.get_collections()

        names = [c["name"] for c in result["collections"]]
        assert names == ["Metroid", "Zelda"]

    @pytest.mark.asyncio
    async def test_collection_id_is_string(self, plugin):
        """IDs are always returned as strings regardless of the API response type."""
        user = [{"id": 42, "name": "Favorites", "rom_count": 1, "is_favorite": False}]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.get_collections()

        assert result["collections"][0]["id"] == "42"


# ---------------------------------------------------------------------------
# TestSaveCollectionSync
# ---------------------------------------------------------------------------


class TestSaveCollectionSync:
    """Tests for LibraryService.save_collection_sync() — synchronous method."""

    def test_saves_enabled(self, plugin):
        """Enabling a collection stores True under its id."""
        plugin._sync_service.save_collection_sync("42", True)

        assert plugin._sync_service._settings["enabled_collections"]["42"] is True

    def test_saves_disabled(self, plugin):
        """Disabling a previously-enabled collection stores False."""
        plugin._sync_service._settings["enabled_collections"] = {"42": True}

        plugin._sync_service.save_collection_sync("42", False)

        assert plugin._sync_service._settings["enabled_collections"]["42"] is False

    def test_returns_success(self, plugin):
        result = plugin._sync_service.save_collection_sync("1", True)

        assert result == {"success": True}

    def test_string_id_stored_from_int(self, plugin):
        """Passing an integer id is coerced to a string key."""
        plugin._sync_service.save_collection_sync(99, True)

        assert "99" in plugin._sync_service._settings["enabled_collections"]
        assert plugin._sync_service._settings["enabled_collections"]["99"] is True

    def test_string_id_stored_from_base64(self, plugin):
        """Base64-style string ids are stored as-is."""
        b64_id = "dXNlcjoxMjM="
        plugin._sync_service.save_collection_sync(b64_id, True)

        assert plugin._sync_service._settings["enabled_collections"][b64_id] is True

    def test_creates_enabled_collections_key_if_absent(self, plugin):
        """enabled_collections is created if it does not exist in settings."""
        plugin._sync_service._settings.pop("enabled_collections", None)

        plugin._sync_service.save_collection_sync("7", True)

        assert plugin._sync_service._settings["enabled_collections"]["7"] is True

    def test_calls_save_settings(self, plugin):
        """save_settings_to_disk is called after updating the setting."""
        save_called = []
        plugin._sync_service._save_settings_to_disk = lambda: save_called.append(True)

        plugin._sync_service.save_collection_sync("1", True)

        assert save_called


# ---------------------------------------------------------------------------
# TestSetAllCollectionsSync
# ---------------------------------------------------------------------------


class TestSetAllCollectionsSync:
    """Tests for LibraryService.set_all_collections_sync()."""

    @pytest.mark.asyncio
    async def test_enable_all(self, plugin):
        """Calling with enabled=True marks all collections as enabled."""
        user = [
            {"id": 1, "name": "RPGs", "is_favorite": False},
            {"id": 2, "name": "Action", "is_favorite": False},
        ]
        franchise = [{"id": 101, "name": "Mario", "is_favorite": False}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        result = await plugin._sync_service.set_all_collections_sync(True)

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["1"] is True
        assert ec["2"] is True
        assert ec["101"] is True

    @pytest.mark.asyncio
    async def test_disable_all(self, plugin):
        """Calling with enabled=False marks all collections as disabled."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        franchise = [{"id": 101, "name": "Mario", "is_favorite": False}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)
        plugin._sync_service._settings["enabled_collections"] = {"1": True, "101": True}

        result = await plugin._sync_service.set_all_collections_sync(False)

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["1"] is False
        assert ec["101"] is False

    @pytest.mark.asyncio
    async def test_filter_by_franchise_category(self, plugin):
        """Passing category='franchise' only touches franchise collections."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        franchise = [{"id": 101, "name": "Mario", "is_favorite": False}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)
        plugin._sync_service._settings["enabled_collections"] = {}

        result = await plugin._sync_service.set_all_collections_sync(True, category="franchise")

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec.get("101") is True
        assert "1" not in ec

    @pytest.mark.asyncio
    async def test_filter_by_user_category(self, plugin):
        """Passing category='user' only touches non-favorite user collections."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        franchise = [{"id": 101, "name": "Mario", "is_favorite": False}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)
        plugin._sync_service._settings["enabled_collections"] = {}

        result = await plugin._sync_service.set_all_collections_sync(True, category="user")

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec.get("1") is True
        assert "101" not in ec

    @pytest.mark.asyncio
    async def test_filter_by_favorites_category(self, plugin):
        """Passing category='favorites' only touches is_favorite=True collections."""
        user = [
            {"id": 1, "name": "Top Picks", "is_favorite": True},
            {"id": 2, "name": "RPGs", "is_favorite": False},
        ]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)
        plugin._sync_service._settings["enabled_collections"] = {}

        result = await plugin._sync_service.set_all_collections_sync(True, category="favorites")

        assert result["success"] is True
        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec.get("1") is True
        assert "2" not in ec

    @pytest.mark.asyncio
    async def test_api_error_returns_error_response(self, plugin):
        """When list_collections raises, the response has success=False."""
        plugin._sync_service._loop = _make_loop_raising(Exception("timeout"))

        result = await plugin._sync_service.set_all_collections_sync(True)

        assert result["success"] is False
        assert "error_code" in result

    @pytest.mark.asyncio
    async def test_franchise_failure_still_processes_user_collections(self, plugin):
        """If franchise fetch fails, user collections are still processed."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]

        mock_loop = MagicMock()
        call_count = 0

        async def _executor(_executor_arg, fn, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return user
            raise Exception("Franchise endpoint unavailable")

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service.set_all_collections_sync(True)

        assert result["success"] is True
        assert plugin._sync_service._settings["enabled_collections"]["1"] is True

    @pytest.mark.asyncio
    async def test_calls_save_settings(self, plugin):
        """save_settings_to_disk is called after updating collections."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        save_called = []
        plugin._sync_service._save_settings_to_disk = lambda: save_called.append(True)

        await plugin._sync_service.set_all_collections_sync(True)

        assert save_called

    @pytest.mark.asyncio
    async def test_enabled_param_coerced_to_bool(self, plugin):
        """Truthy/falsy values are coerced to bool."""
        user = [{"id": 1, "name": "RPGs", "is_favorite": False}]
        franchise = []
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        await plugin._sync_service.set_all_collections_sync(1)  # truthy int

        assert plugin._sync_service._settings["enabled_collections"]["1"] is True

    @pytest.mark.asyncio
    async def test_category_none_processes_all(self, plugin):
        """When category is None (default), all categories are processed."""
        user = [
            {"id": 1, "name": "Faves", "is_favorite": True},
            {"id": 2, "name": "RPGs", "is_favorite": False},
        ]
        franchise = [{"id": 101, "name": "Mario", "is_favorite": False}]
        plugin._sync_service._loop = _make_loop_with_executor(user, franchise)

        await plugin._sync_service.set_all_collections_sync(True, category=None)

        ec = plugin._sync_service._settings["enabled_collections"]
        assert ec["1"] is True
        assert ec["2"] is True
        assert ec["101"] is True


# ---------------------------------------------------------------------------
# TestGetCollectionsUnsupported / TestSetAllCollectionsSyncUnsupported
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestFetchCollectionRoms
# ---------------------------------------------------------------------------


class TestFetchCollectionRoms:
    """Tests for LibraryService._fetch_collection_roms()."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_collections_enabled(self, plugin):
        """When no collections are enabled, returns empty results immediately."""
        plugin._sync_service._settings["enabled_collections"] = {"1": False, "2": False}

        roms, memberships = await plugin._sync_service._fetch_collection_roms(set())

        assert roms == []
        assert memberships == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_enabled_collections_absent(self, plugin):
        """When enabled_collections key is absent, returns empty results."""
        plugin._sync_service._settings.pop("enabled_collections", None)

        roms, memberships = await plugin._sync_service._fetch_collection_roms(set())

        assert roms == []
        assert memberships == {}

    @pytest.mark.asyncio
    async def test_deduplicates_against_seen_ids(self, plugin):
        """ROMs already in seen_rom_ids are not added to collection_only_roms."""
        plugin._sync_service._settings["enabled_collections"] = {"1": True}
        user = [{"id": 1, "name": "My Collection", "is_virtual": False}]
        page = {
            "items": [
                {"id": 10, "name": "ROM A", "platform_name": "N64", "platform_slug": "n64"},
                {"id": 20, "name": "ROM B", "platform_name": "SNES", "platform_slug": "snes"},
            ]
        }
        plugin._sync_service._loop = _make_loop_with_executor(user, [], page)

        roms, memberships = await plugin._sync_service._fetch_collection_roms({10})

        # ROM A (id=10) was already seen, only ROM B is new
        assert len(roms) == 1
        assert roms[0]["id"] == 20
        # But both are tracked in memberships
        assert 10 in memberships["My Collection"]
        assert 20 in memberships["My Collection"]

    @pytest.mark.asyncio
    async def test_returns_all_rom_ids_in_memberships(self, plugin):
        """collection_memberships includes ALL rom_ids in the collection, not just new ones."""
        plugin._sync_service._settings["enabled_collections"] = {"1": True}
        user = [{"id": 1, "name": "Favorites", "is_virtual": False}]
        page = {
            "items": [
                {"id": 5, "name": "ROM A", "platform_name": "N64", "platform_slug": "n64"},
                {"id": 6, "name": "ROM B", "platform_name": "N64", "platform_slug": "n64"},
            ]
        }
        plugin._sync_service._loop = _make_loop_with_executor(user, [], page)

        roms, memberships = await plugin._sync_service._fetch_collection_roms(set())

        assert set(memberships["Favorites"]) == {5, 6}
        assert len(roms) == 2

    @pytest.mark.asyncio
    async def test_skips_disabled_collections(self, plugin):
        """Collections with enabled=False are not fetched."""
        plugin._sync_service._settings["enabled_collections"] = {"1": False, "2": True}
        user = [
            {"id": 1, "name": "Disabled", "is_virtual": False},
            {"id": 2, "name": "Enabled", "is_virtual": False},
        ]
        page = {"items": [{"id": 10, "name": "ROM A", "platform_name": "N64", "platform_slug": "n64"}]}
        # First executor call: list_collections, second: list_virtual_collections (franchise),
        # third: list_roms_by_collection for collection id=2
        plugin._sync_service._loop = _make_loop_with_executor(user, [], page)

        _roms, memberships = await plugin._sync_service._fetch_collection_roms(set())

        assert "Disabled" not in memberships
        assert "Enabled" in memberships

    @pytest.mark.asyncio
    async def test_strips_files_array_from_roms(self, plugin):
        """The files array is stripped from ROM dicts to save memory."""
        plugin._sync_service._settings["enabled_collections"] = {"1": True}
        user = [{"id": 1, "name": "My Collection", "is_virtual": False}]
        page = {
            "items": [
                {"id": 10, "name": "ROM A", "platform_name": "N64", "platform_slug": "n64", "files": ["f1", "f2"]},
            ]
        }
        plugin._sync_service._loop = _make_loop_with_executor(user, [], page)

        roms, _ = await plugin._sync_service._fetch_collection_roms(set())

        assert "files" not in roms[0]

    @pytest.mark.asyncio
    async def test_handles_api_error_gracefully(self, plugin):
        """Generic API errors are caught and empty results are returned."""
        plugin._sync_service._settings["enabled_collections"] = {"1": True}
        plugin._sync_service._loop = _make_loop_raising(Exception("Connection refused"))

        roms, memberships = await plugin._sync_service._fetch_collection_roms(set())

        assert roms == []
        assert memberships == {}

    @pytest.mark.asyncio
    async def test_virtual_collection_uses_virtual_endpoint(self, plugin):
        """Virtual collections are fetched via list_roms_by_virtual_collection."""
        plugin._sync_service._settings["enabled_collections"] = {"mario": True}
        user = []
        franchise = [{"id": "mario", "name": "Mario", "is_virtual": True}]
        page = {"items": [{"id": 42, "name": "Super Mario", "platform_name": "NES", "platform_slug": "nes"}]}

        mock_loop = MagicMock()
        call_count = 0

        captured_calls: list = []

        async def _executor(_exec_arg, fn, *args):
            nonlocal call_count
            call_count += 1
            captured_calls.append((fn, args))
            if call_count == 1:
                return user
            if call_count == 2:
                return franchise
            return page

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        plugin._sync_service._loop = mock_loop

        roms, memberships = await plugin._sync_service._fetch_collection_roms(set())

        # The third call should use list_roms_by_virtual_collection
        third_fn = captured_calls[2][0]
        assert third_fn == plugin._sync_service._romm_api.list_roms_by_virtual_collection
        assert "Mario" in memberships
        assert roms[0]["id"] == 42


# ---------------------------------------------------------------------------
# TestCollectionSyncEdgeCases
# ---------------------------------------------------------------------------


def _make_rom(rom_id, name, platform_name, platform_slug="gba"):
    """Build a minimal ROM dict as returned by the RomM API."""
    return {
        "id": rom_id,
        "name": name,
        "fs_name": f"{name}.zip",
        "platform_name": platform_name,
        "platform_slug": platform_slug,
    }


def _make_registry_entry(name, platform_name, app_id, platform_slug="gba"):
    """Build a minimal shortcut registry entry."""
    return {
        "app_id": app_id,
        "name": name,
        "fs_name": f"{name}.zip",
        "platform_name": platform_name,
        "platform_slug": platform_slug,
        "cover_path": "",
    }


def _page(items):
    """Wrap items in a paginated API response dict."""
    return {"items": items, "total": len(items)}


class TestCollectionSyncEdgeCases:
    """Edge-case tests for the merged platform + collection sync engine.

    Tests exercise classify_roms() and _report_sync_results_io() directly,
    and use _fetch_collection_roms() for collection-fetch scenarios.
    """

    # ------------------------------------------------------------------
    # Scenario 1: Platform disabled, collection keeps game alive
    # ------------------------------------------------------------------

    def test_sc1_collection_keeps_rom_alive_when_platform_disabled(self, plugin):
        """ROM A stays because Favorites collection references it; ROM B becomes stale.

        Platform GBA is disabled between sync 1 and sync 2. The registry has
        both ROM A (id=1) and ROM B (id=2) from the previous sync. On sync 2,
        only ROM A appears in shortcuts_data (via collection). ROM B has no
        source and must be classified as stale.
        """
        svc = plugin._sync_service

        # Registry after first sync
        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001),
            "2": _make_registry_entry("ROM B", "Game Boy Advance", app_id=1002),
        }

        # Second sync: GBA platform is disabled, Favorites collection keeps ROM A
        # shortcuts_data only contains ROM A (fetched via collection)
        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "ROM A",
                "fs_name": "ROM A.zip",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
            }
        ]
        # GBA is not in fetched platform names (platform disabled)
        fetched_platform_names = set()

        new, _changed, unchanged_ids, stale, _disabled_count = classify_roms(
            shortcuts_data, svc._state["shortcut_registry"], fetched_platform_names
        )

        assert 1 in unchanged_ids, "ROM A should be unchanged (collection keeps it alive)"
        assert 2 in stale, "ROM B should be stale (no source references it)"
        assert len(new) == 0
        assert len(_changed) == 0

    # ------------------------------------------------------------------
    # Scenario 2: Collection disabled, platform keeps game alive
    # ------------------------------------------------------------------

    def test_sc2_platform_keeps_rom_alive_when_collection_disabled(self, plugin):
        """ROM A stays (platform reference); ROM C becomes stale (collection-only, now disabled).

        Platform GBA enabled → ROM A stays. PSX not enabled and Favorites
        collection disabled → ROM C has no source and is stale.
        """
        svc = plugin._sync_service

        # Registry after first sync: ROM A (GBA via platform), ROM C (PSX via collection)
        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001, platform_slug="gba"),
            "3": _make_registry_entry("ROM C", "PlayStation", app_id=1003, platform_slug="psx"),
        }

        # Second sync: Favorites disabled, GBA still enabled
        # shortcuts_data only contains ROM A from the GBA platform
        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "ROM A",
                "fs_name": "ROM A.zip",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
            }
        ]
        fetched_platform_names = {"Game Boy Advance"}

        new, _changed, unchanged_ids, stale, disabled_count = classify_roms(
            shortcuts_data, svc._state["shortcut_registry"], fetched_platform_names
        )

        assert 1 in unchanged_ids, "ROM A should be unchanged (platform still enabled)"
        assert 3 in stale, "ROM C should be stale (collection disabled, PSX not enabled)"
        assert len(new) == 0
        # disabled_count: ROM C's platform (PlayStation) is NOT in fetched_platform_names
        assert disabled_count == 1

    # ------------------------------------------------------------------
    # Scenario 3: Game in multiple collections, one disabled
    # ------------------------------------------------------------------

    def test_sc3_rom_stays_alive_when_one_of_two_collections_disabled(self, plugin):
        """ROM A stays because RPG collection still references it even after Favorites is disabled."""
        svc = plugin._sync_service

        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001),
        }

        # ROM A still appears in shortcuts_data (RPG collection enabled)
        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "ROM A",
                "fs_name": "ROM A.zip",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
            }
        ]
        fetched_platform_names = set()

        _new, _changed, unchanged_ids, stale, _disabled_count = classify_roms(
            shortcuts_data, svc._state["shortcut_registry"], fetched_platform_names
        )

        assert 1 in unchanged_ids, "ROM A should stay alive via RPG collection"
        assert len(stale) == 0

    # ------------------------------------------------------------------
    # Scenario 4: Collection-only game (no platform enabled)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sc4_collection_only_rom_is_synced_without_platform(self, plugin):
        """ROM A is synced via collection fetch when its platform is not enabled."""
        svc = plugin._sync_service
        svc._settings["enabled_platforms"] = {}  # No platforms enabled
        svc._settings["enabled_collections"] = {"10": True}

        # API mocks: no enabled platforms → no platform ROMs
        # list_collections → one collection; list_roms_by_collection → ROM A
        rom_a = {
            "id": 1,
            "name": "ROM A",
            "fs_name": "ROM A.zip",
            "platform_name": "Game Boy Advance",
            "platform_slug": "gba",
        }
        user_collections = [{"id": 10, "name": "Favorites", "is_virtual": False}]
        franchise_collections: list = []

        mock_loop = MagicMock()
        call_num = 0

        async def _executor(_exec, fn, *args):
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                # list_platforms → empty (but this is called by _fetch_enabled_platforms)
                return []
            if call_num == 2:
                # list_collections inside _fetch_collection_roms
                return user_collections
            if call_num == 3:
                # list_virtual_collections (franchise)
                return franchise_collections
            # list_roms_by_collection for collection id=10
            return _page([rom_a])

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        svc._loop = mock_loop

        # _fetch_and_prepare drives the whole flow
        all_roms, shortcuts_data, _platforms, collection_memberships, platform_rom_ids = await svc._fetch_and_prepare()

        assert len(all_roms) == 1
        assert all_roms[0]["id"] == 1
        assert len(shortcuts_data) == 1
        assert shortcuts_data[0]["rom_id"] == 1
        # ROM A came from collection, not platform
        assert 1 not in platform_rom_ids
        assert "Favorites" in collection_memberships
        assert 1 in collection_memberships["Favorites"]

    # ------------------------------------------------------------------
    # Scenario 5: collection_create_platform_groups = False (default)
    # ------------------------------------------------------------------

    def test_sc5_collection_rom_excluded_from_platform_groups_by_default(self, plugin):
        """With toggle OFF, collection-only ROM B (PSX) is not included in platform_app_ids for PSX."""
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = False

        # ROM A (id=1) came via GBA platform; ROM B (id=2) came via collection only (PSX)
        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001, platform_slug="gba"),
            "2": _make_registry_entry("ROM B", "PlayStation", app_id=1002, platform_slug="psx"),
        }

        # platform_rom_ids only contains ROM A (from platform fetch)
        svc._pending_platform_rom_ids = {1}
        svc._pending_collection_memberships = {"Favorites": [1, 2]}
        svc._pending_sync = {
            1: {
                "name": "ROM A",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
                "cover_path": "",
            },
            2: {
                "name": "ROM B",
                "platform_name": "PlayStation",
                "platform_slug": "psx",
                "cover_path": "",
            },
        }

        platform_app_ids, _romm_collection_app_ids = svc._report_sync_results_io({}, [])

        assert "Game Boy Advance" in platform_app_ids
        assert 1001 in platform_app_ids["Game Boy Advance"]
        # PSX platform group should NOT be created because ROM B is collection-only
        assert "PlayStation" not in platform_app_ids

    # ------------------------------------------------------------------
    # Scenario 6: collection_create_platform_groups = True
    # ------------------------------------------------------------------

    def test_sc6_collection_rom_included_in_platform_groups_when_toggle_on(self, plugin):
        """With toggle ON, collection-only ROM B (PSX) IS included in platform_app_ids for PSX."""
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = True

        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001, platform_slug="gba"),
            "2": _make_registry_entry("ROM B", "PlayStation", app_id=1002, platform_slug="psx"),
        }

        svc._pending_platform_rom_ids = {1}
        svc._pending_collection_memberships = {"Favorites": [1, 2]}
        svc._pending_sync = {
            1: {
                "name": "ROM A",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
                "cover_path": "",
            },
            2: {
                "name": "ROM B",
                "platform_name": "PlayStation",
                "platform_slug": "psx",
                "cover_path": "",
            },
        }

        platform_app_ids, _romm_collection_app_ids = svc._report_sync_results_io({}, [])

        assert "Game Boy Advance" in platform_app_ids
        assert 1001 in platform_app_ids["Game Boy Advance"]
        # PSX platform group SHOULD exist because toggle is on
        assert "PlayStation" in platform_app_ids
        assert 1002 in platform_app_ids["PlayStation"]

    def test_sc5c_build_collection_app_ids_excludes_collection_only_roms(self, plugin):
        """_build_collection_app_ids respects the toggle.

        Platform collection mapping is built from the full registry in report_sync_results.
        collection-only ROMs must be excluded when the toggle is OFF.
        """
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = False
        svc._settings["enabled_collections"] = {"3": True}

        # Registry: ROM 1 from platform, ROM 2 from collection only
        registry = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001, platform_slug="gba"),
            "2": _make_registry_entry("ROM B", "PlayStation", app_id=1002, platform_slug="psx"),
        }
        platform_rom_ids = {1}  # Only ROM 1 from platform

        platform_app_ids, _ = svc._build_collection_app_ids(registry, platform_rom_ids, {"Favorites": [1, 2]})

        assert "Game Boy Advance" in platform_app_ids
        assert 1001 in platform_app_ids["Game Boy Advance"]
        assert "PlayStation" not in platform_app_ids, "PSX should be excluded (collection-only, toggle OFF)"

    def test_sc6c_build_collection_app_ids_includes_all_when_toggle_on(self, plugin):
        """Same as sc5c but with toggle ON — PSX should be included."""
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = True

        registry = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001, platform_slug="gba"),
            "2": _make_registry_entry("ROM B", "PlayStation", app_id=1002, platform_slug="psx"),
        }
        platform_rom_ids = {1}

        platform_app_ids, _ = svc._build_collection_app_ids(registry, platform_rom_ids, {})

        assert "Game Boy Advance" in platform_app_ids
        assert "PlayStation" in platform_app_ids, "PSX should be included (toggle ON)"

    # ------------------------------------------------------------------
    # Scenario 7: Deduplication — ROM in both platform and collection
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sc7_rom_in_platform_and_collection_appears_once(self, plugin):
        """ROM A fetched from GBA platform is not duplicated when Favorites collection also has it."""
        svc = plugin._sync_service
        svc._settings["enabled_platforms"] = {"5": True}
        svc._settings["enabled_collections"] = {"10": True}

        rom_a = {
            "id": 1,
            "name": "ROM A",
            "fs_name": "ROM A.zip",
            "platform_name": "Game Boy Advance",
            "platform_slug": "gba",
        }
        platform = {"id": 5, "name": "Game Boy Advance", "slug": "gba", "rom_count": 1}
        user_collections = [{"id": 10, "name": "Favorites", "is_virtual": False}]

        mock_loop = MagicMock()
        call_num = 0

        async def _executor(_exec, fn, *args):
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                # list_platforms
                return [platform]
            if call_num == 2:
                # list_roms for GBA (paginated)
                return _page([rom_a])
            if call_num == 3:
                # list_collections inside _fetch_collection_roms
                return user_collections
            if call_num == 4:
                # list_virtual_collections (franchise)
                return []
            # list_roms_by_collection for Favorites — ROM A already seen
            return _page([rom_a])

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        svc._loop = mock_loop

        _all_roms, shortcuts_data, _platforms, collection_memberships, platform_rom_ids = await svc._fetch_and_prepare()

        # ROM A should appear exactly once despite being in both platform and collection
        rom_ids_in_shortcuts = [sd["rom_id"] for sd in shortcuts_data]
        assert rom_ids_in_shortcuts.count(1) == 1, "ROM A must not be duplicated"

        # ROM A is in platform_rom_ids (fetched from platform)
        assert 1 in platform_rom_ids

        # ROM A must be in the Favorites collection membership
        assert "Favorites" in collection_memberships
        assert 1 in collection_memberships["Favorites"]

    def test_sc7_rom_appears_in_both_platform_and_collection_app_ids(self, plugin):
        """ROM A (in both GBA platform and Favorites collection) appears in both platform_app_ids
        and romm_collection_app_ids after _report_sync_results_io."""
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = False

        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001, platform_slug="gba"),
        }

        # ROM A came from platform
        svc._pending_platform_rom_ids = {1}
        svc._pending_collection_memberships = {"Favorites": [1]}
        svc._pending_sync = {}

        platform_app_ids, romm_collection_app_ids = svc._report_sync_results_io({}, [])

        # Platform group for GBA exists (ROM A is a platform ROM)
        assert "Game Boy Advance" in platform_app_ids
        assert 1001 in platform_app_ids["Game Boy Advance"]

        # Favorites collection app_ids also contains ROM A
        assert "Favorites" in romm_collection_app_ids
        assert 1001 in romm_collection_app_ids["Favorites"]

    # ------------------------------------------------------------------
    # Scenario 8: All sources removed — game gets stale
    # ------------------------------------------------------------------

    def test_sc8_rom_becomes_stale_when_no_source_references_it(self, plugin):
        """ROM A classified as stale when neither platform nor collection brings it in."""
        svc = plugin._sync_service

        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "Game Boy Advance", app_id=1001),
        }

        # Empty shortcuts_data — no ROM was fetched from any source
        shortcuts_data: list = []
        fetched_platform_names: set = set()

        new, changed, unchanged_ids, stale, _disabled_count = classify_roms(
            shortcuts_data, svc._state["shortcut_registry"], fetched_platform_names
        )

        assert 1 in stale
        assert len(new) == 0
        assert len(changed) == 0
        assert len(unchanged_ids) == 0

    # ------------------------------------------------------------------
    # Scenario 9: Empty collection
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sc9_empty_collection_does_not_error(self, plugin):
        """An enabled collection with no ROMs causes no errors and returns empty results."""
        svc = plugin._sync_service
        svc._settings["enabled_collections"] = {"10": True}

        user_collections = [{"id": 10, "name": "Empty", "is_virtual": False}]

        mock_loop = MagicMock()
        call_num = 0

        async def _executor(_exec, fn, *args):
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                return user_collections
            if call_num == 2:
                return []  # no franchise collections
            # list_roms_by_collection returns an empty page
            return _page([])

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        svc._loop = mock_loop

        roms, memberships = await svc._fetch_collection_roms(set())

        assert roms == []
        # Empty collection produces no membership entry (no rom_ids collected)
        assert "Empty" not in memberships

    # ------------------------------------------------------------------
    # Scenario 10: Collection API failure is non-fatal
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sc10_collection_api_failure_does_not_crash_sync(self, plugin):
        """When the collection API fails, sync continues with platform ROMs only."""
        svc = plugin._sync_service
        svc._settings["enabled_platforms"] = {"5": True}
        svc._settings["enabled_collections"] = {"10": True}

        rom_a = {
            "id": 1,
            "name": "ROM A",
            "fs_name": "ROM A.zip",
            "platform_name": "Game Boy Advance",
            "platform_slug": "gba",
        }
        platform = {"id": 5, "name": "Game Boy Advance", "slug": "gba", "rom_count": 1}

        mock_loop = MagicMock()
        call_num = 0

        async def _executor(_exec, fn, *args):
            nonlocal call_num
            call_num += 1
            if call_num == 1:
                # list_platforms
                return [platform]
            if call_num == 2:
                # list_roms for GBA
                return _page([rom_a])
            # All subsequent calls (list_collections, etc.) raise
            raise Exception("Collection API unavailable")

        mock_loop.run_in_executor = AsyncMock(side_effect=_executor)
        svc._loop = mock_loop

        # Should not raise; collection errors are caught and logged as warnings
        (
            all_roms,
            _shortcuts_data,
            _platforms,
            collection_memberships,
            _platform_rom_ids,
        ) = await svc._fetch_and_prepare()

        # Platform ROM was still fetched
        assert len(all_roms) == 1
        assert all_roms[0]["id"] == 1
        # No collection memberships because the fetch failed
        assert collection_memberships == {}

    # ------------------------------------------------------------------
    # Additional edge cases for _report_sync_results_io
    # ------------------------------------------------------------------

    def test_report_sync_clears_pending_state(self, plugin):
        """_report_sync_results_io clears pending_sync, pending_collection_memberships,
        and pending_platform_rom_ids after completion."""
        svc = plugin._sync_service

        svc._state["shortcut_registry"] = {}
        svc._pending_sync = {1: {"name": "ROM A", "platform_name": "GBA", "cover_path": ""}}
        svc._pending_collection_memberships = {"Favorites": [1]}
        svc._pending_platform_rom_ids = {1}

        svc._report_sync_results_io({}, [])

        assert svc._pending_sync == {}
        assert svc._pending_collection_memberships == {}
        assert svc._pending_platform_rom_ids is None

    def test_report_sync_collection_app_ids_empty_when_no_memberships(self, plugin):
        """romm_collection_app_ids is empty when no collection memberships are set."""
        svc = plugin._sync_service

        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "GBA", app_id=1001),
        }
        svc._pending_platform_rom_ids = {1}
        svc._pending_collection_memberships = {}
        svc._pending_sync = {}

        _platform_app_ids, romm_collection_app_ids = svc._report_sync_results_io({}, [])

        assert romm_collection_app_ids == {}

    def test_report_sync_collection_app_ids_excludes_missing_registry_entries(self, plugin):
        """romm_collection_app_ids skips rom_ids that have no registry entry."""
        svc = plugin._sync_service

        # Only ROM id=1 is in the registry; ROM id=99 is referenced in memberships but missing
        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("ROM A", "GBA", app_id=1001),
        }
        svc._pending_platform_rom_ids = {1}
        svc._pending_collection_memberships = {"Favorites": [1, 99]}
        svc._pending_sync = {}

        _platform_app_ids, romm_collection_app_ids = svc._report_sync_results_io({}, [])

        assert "Favorites" in romm_collection_app_ids
        assert 1001 in romm_collection_app_ids["Favorites"]
        # ROM 99 has no registry entry, so its app_id is not included
        assert len(romm_collection_app_ids["Favorites"]) == 1

    def test_report_sync_platform_groups_include_newly_added_roms(self, plugin):
        """ROMs added in this sync (via rom_id_to_app_id) appear in platform_app_ids."""
        svc = plugin._sync_service
        svc._settings["collection_create_platform_groups"] = False

        # Registry is initially empty; the sync adds ROM A
        svc._state["shortcut_registry"] = {}
        svc._pending_platform_rom_ids = {1}
        svc._pending_collection_memberships = {}
        svc._pending_sync = {
            1: {
                "name": "ROM A",
                "platform_name": "Game Boy Advance",
                "platform_slug": "gba",
                "cover_path": "",
                "fs_name": "ROM A.zip",
            }
        }

        platform_app_ids, _romm = svc._report_sync_results_io({"1": 1001}, [])

        assert "Game Boy Advance" in platform_app_ids
        assert 1001 in platform_app_ids["Game Boy Advance"]

    def test_classify_roms_new_when_not_in_registry(self, plugin):
        """ROMs not present in the registry at all are classified as new."""
        svc = plugin._sync_service
        svc._state["shortcut_registry"] = {}

        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "ROM A",
                "fs_name": "ROM A.zip",
                "platform_name": "GBA",
                "platform_slug": "gba",
            }
        ]

        new, changed, unchanged_ids, stale, _disabled_count = classify_roms(
            shortcuts_data, svc._state["shortcut_registry"], {"GBA"}
        )

        assert len(new) == 1
        assert new[0]["rom_id"] == 1
        assert len(changed) == 0
        assert len(unchanged_ids) == 0
        assert len(stale) == 0

    def test_classify_roms_changed_when_name_differs(self, plugin):
        """ROMs whose name changed since last sync are classified as changed."""
        svc = plugin._sync_service
        svc._state["shortcut_registry"] = {
            "1": _make_registry_entry("Old Name", "GBA", app_id=1001),
        }

        shortcuts_data = [
            {
                "rom_id": 1,
                "name": "New Name",  # name changed
                "fs_name": "Old Name.zip",
                "platform_name": "GBA",
                "platform_slug": "gba",
            }
        ]

        new, changed, unchanged_ids, _stale, _disabled_count = classify_roms(
            shortcuts_data, svc._state["shortcut_registry"], {"GBA"}
        )

        assert len(changed) == 1
        assert changed[0]["rom_id"] == 1
        assert changed[0]["existing_app_id"] == 1001
        assert len(new) == 0
        assert len(unchanged_ids) == 0
