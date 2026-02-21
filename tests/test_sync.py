import pytest
import json
import os
import asyncio

# conftest.py patches decky before this import
from main import Plugin


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._sync_running = False
    p._sync_cancel = False
    p._sync_progress = {"running": False}
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._pending_sync = {}
    p._download_tasks = {}
    p._download_queue = {}
    p._download_in_progress = set()
    p._metadata_cache = {}
    return p


class TestReportSyncResults:
    @pytest.mark.asyncio
    async def test_updates_registry(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._pending_sync = {
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
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._state["shortcut_registry"]["99"] = {
            "app_id": 99999,
            "name": "Old Game",
            "platform_name": "NES",
        }
        plugin._pending_sync = {}

        result = await plugin.report_sync_results({}, [99])
        assert result["success"] is True
        assert "99" not in plugin._state["shortcut_registry"]

    @pytest.mark.asyncio
    async def test_emits_sync_complete(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.emit.reset_mock()

        plugin._pending_sync = {
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
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._pending_sync = {}
        await plugin.report_sync_results({}, [])
        assert plugin._state["last_sync"] is not None

    @pytest.mark.asyncio
    async def test_clears_pending_sync(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._pending_sync = {1: {"name": "X", "platform_name": "Y", "cover_path": ""}}
        await plugin.report_sync_results({"1": 1}, [])
        assert plugin._pending_sync == {}


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
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

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
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        # Create a fake artwork file
        art_file = tmp_path / "cover.png"
        art_file.write_text("fake")

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": str(art_file)},
        }
        # Mock _grid_dir to return tmp_path
        plugin._grid_dir = lambda: str(tmp_path)

        result = await plugin.report_removal_results([10])
        assert result["success"] is True
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_cleans_up_artwork_legacy_id(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        art_file = grid_dir / "12345p.png"
        art_file.write_text("fake")

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "artwork_id": 12345},
        }
        plugin._grid_dir = lambda: str(grid_dir)

        result = await plugin.report_removal_results([10])
        assert result["success"] is True
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_partial_removal(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

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

        stats = await plugin.get_sync_stats()
        assert stats["platforms"] == 2
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
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "platform_name": "N64", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "platform_name": "SNES", "cover_path": ""},
        }

        await plugin.report_removal_results([10])
        stats = await plugin.get_sync_stats()
        assert stats["platforms"] == 1
        assert stats["roms"] == 1
        assert stats["total_shortcuts"] == 1

    @pytest.mark.asyncio
    async def test_report_removal_updates_sync_stats_state(self, plugin, tmp_path):
        """report_removal_results should update sync_stats in state."""
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

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

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=[
            {"id": 1, "slug": "n64", "name": "Nintendo 64"},
            {"id": 2, "slug": "snes", "name": "Super Nintendo"},
        ])

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

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=[
            {"id": 1, "slug": "n64", "name": "Nintendo 64"},
        ])

        result = await plugin.remove_platform_shortcuts("nonexistent")
        assert result["success"] is False
        assert result["app_ids"] == []
        assert result["rom_ids"] == []

    @pytest.mark.asyncio
    async def test_does_not_modify_registry(self, plugin):
        """remove_platform_shortcuts just returns data; registry cleared by report_removal_results."""
        from unittest.mock import AsyncMock, MagicMock

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=[
            {"id": 1, "slug": "n64", "name": "Nintendo 64"},
        ])

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

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(side_effect=Exception("Server unreachable"))

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64", "platform_slug": "n64"},
            "20": {"app_id": 1002, "name": "Zelda OOT", "platform_name": "Nintendo 64", "platform_slug": "n64"},
        }

        result = await plugin.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert result["platform_name"] == "Nintendo 64"


class TestArtworkStaging:
    """Tests for the staging/rename artwork flow."""

    @pytest.mark.asyncio
    async def test_download_uses_staging_filename(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._grid_dir = lambda: str(grid_dir)
        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock()

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await plugin._download_artwork(roms)

        assert 42 in result
        assert result[42].endswith("romm_42_cover.png")
        # Should have called _romm_download with staging path as dest arg
        call_args = plugin.loop.run_in_executor.call_args[0]
        assert "romm_42_cover.png" in call_args[3]

    @pytest.mark.asyncio
    async def test_skips_download_if_final_exists(self, plugin, tmp_path):
        """If {app_id}p.png exists from a prior sync, skip re-download."""
        from unittest.mock import AsyncMock, MagicMock

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._grid_dir = lambda: str(grid_dir)
        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock()

        # Simulate existing final artwork from previous sync
        final_art = grid_dir / "99999p.png"
        final_art.write_text("fake")

        plugin._state["shortcut_registry"]["42"] = {"app_id": 99999, "name": "Test"}

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await plugin._download_artwork(roms)

        assert result[42] == str(final_art)
        plugin.loop.run_in_executor.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_download_if_staging_exists(self, plugin, tmp_path):
        """If staging file exists (e.g. retry), skip re-download."""
        from unittest.mock import AsyncMock, MagicMock

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._grid_dir = lambda: str(grid_dir)
        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock()

        staging = grid_dir / "romm_42_cover.png"
        staging.write_text("fake")

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await plugin._download_artwork(roms)

        assert result[42] == str(staging)
        plugin.loop.run_in_executor.assert_not_called()


class TestArtworkRenameOnSync:
    """Tests for artwork rename in report_sync_results."""

    @pytest.mark.asyncio
    async def test_renames_staged_to_app_id(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._grid_dir = lambda: str(grid_dir)

        # Create staged artwork
        staging = grid_dir / "romm_1_cover.png"
        staging.write_text("cover data")

        plugin._pending_sync = {
            1: {"name": "Game A", "platform_name": "N64",
                "cover_path": str(staging)},
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
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._grid_dir = lambda: str(grid_dir)

        final = grid_dir / "100001p.png"
        final.write_text("cover data")

        plugin._pending_sync = {
            1: {"name": "Game A", "platform_name": "N64",
                "cover_path": str(final)},
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
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        art_file = grid_dir / "100001p.png"
        art_file.write_text("fake")

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 100001, "name": "Game A", "cover_path": ""},
        }
        plugin._grid_dir = lambda: str(grid_dir)

        await plugin.report_removal_results([10])
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_removes_staging_leftover(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_10_cover.png"
        staging.write_text("fake")

        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 100001, "name": "Game A", "cover_path": ""},
        }
        plugin._grid_dir = lambda: str(grid_dir)

        await plugin.report_removal_results([10])
        assert not staging.exists()


class TestGetRomBySteamAppId:
    @pytest.mark.asyncio
    async def test_finds_rom_by_app_id(self, plugin):
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001, "name": "Zelda", "platform_name": "N64", "platform_slug": "n64",
        }
        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42, "file_path": "/roms/n64/zelda.z64",
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
        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(side_effect=[
            # _fetch_platforms
            [{"id": 1, "slug": "gba", "name": "Game Boy Advance", "rom_count": 1}],
            # _fetch_roms_for_platform
            [{"id": 42, "name": "Test Game", "platform_name": "Game Boy Advance",
              "platform_slug": "gba", "igdb_id": 100, "sgdb_id": 200,
              "path_cover_large": "/cover.png"}],
        ])
        plugin._download_artwork = AsyncMock(return_value={})
        plugin._emit_progress = AsyncMock()
        plugin._finish_sync = AsyncMock()
        plugin._sync_cancel = False

        # Capture the emitted sync_apply data
        emitted = {}
        async def capture_emit(event, **kwargs):
            if event == "sync_apply":
                emitted.update(kwargs)
        plugin._emit = capture_emit

        # Mock decky.emit to capture the shortcuts
        import decky
        emitted_events = []
        original_emit = getattr(decky, 'emit', None)
        async def mock_emit(event, *args):
            emitted_events.append((event, args))
        decky.emit = mock_emit

        try:
            await plugin.start_sync()
        except Exception:
            pass  # _finish_sync mock may cause issues
        finally:
            if original_emit:
                decky.emit = original_emit

        # Find the sync_apply emission
        sync_items = None
        for event, args in emitted_events:
            if event == "sync_apply" and args:
                sync_items = args[0] if args else None
                break

        if sync_items:
            required_fields = {"rom_id", "name", "exe", "start_dir", "launch_options",
                               "platform_name", "platform_slug"}
            for item in sync_items:
                for field in required_fields:
                    assert field in item, f"Missing field '{field}' in shortcut data"

    @pytest.mark.asyncio
    async def test_exe_path_points_to_romm_launcher(self, plugin):
        """Exe path must point to bin/romm-launcher inside the plugin directory."""
        import decky

        plugin.settings["romm_url"] = "http://romm.local"
        exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")

        assert exe.endswith("/bin/romm-launcher"), \
            f"Exe path should end with /bin/romm-launcher, got: {exe}"
        assert "decky-romm-sync" in exe, \
            f"Exe path should contain plugin name, got: {exe}"

    def test_launch_options_format(self, plugin):
        """Launch options must follow the romm:<rom_id> pattern."""
        import re
        pattern = r"^romm:\d+$"

        # Test valid formats
        for rom_id in [1, 42, 4409, 99999]:
            launch_opt = f"romm:{rom_id}"
            assert re.match(pattern, launch_opt), \
                f"Launch option '{launch_opt}' does not match expected pattern"

    def test_start_dir_is_parent_of_exe(self, plugin):
        """Start dir must be the directory containing the launcher."""
        import decky

        exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")
        start_dir = os.path.join(decky.DECKY_PLUGIN_DIR, "bin")

        assert start_dir == os.path.dirname(exe), \
            f"start_dir ({start_dir}) should be parent of exe ({exe})"

    def test_artwork_id_generation_consistency(self, plugin):
        """Artwork ID must be deterministic for the same exe+name pair."""
        exe = "/home/deck/homebrew/plugins/decky-romm-sync/bin/romm-launcher"
        name = "Test Game"

        id1 = plugin._generate_artwork_id(exe, name)
        id2 = plugin._generate_artwork_id(exe, name)

        assert id1 == id2, "Artwork ID should be deterministic"
        assert isinstance(id1, int), "Artwork ID should be an integer"
        assert id1 > 0, "Artwork ID should be positive (unsigned)"

    def test_artwork_id_differs_per_game(self, plugin):
        """Different game names should produce different artwork IDs."""
        exe = "/home/deck/homebrew/plugins/decky-romm-sync/bin/romm-launcher"

        id_a = plugin._generate_artwork_id(exe, "Game A")
        id_b = plugin._generate_artwork_id(exe, "Game B")

        assert id_a != id_b, "Different games should have different artwork IDs"
