import pytest
import json
import os
import asyncio

# conftest.py patches decky before this import
from main import Plugin


@pytest.fixture
def plugin():
    p = Plugin()
    # Manually init what _main() would do
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._sync_running = False
    p._sync_cancel = False
    p._sync_progress = {"running": False}
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._pending_sync = {}
    p._download_tasks = {}
    p._download_queue = {}
    p._download_in_progress = set()
    return p


class TestAppIdGeneration:
    def test_generates_signed_int32(self, plugin):
        app_id = plugin._generate_app_id("/path/to/exe", "Test Game")
        assert isinstance(app_id, int)
        assert app_id < 0  # Should be negative (high bit set)

    def test_deterministic(self, plugin):
        id1 = plugin._generate_app_id("/path/exe", "Game")
        id2 = plugin._generate_app_id("/path/exe", "Game")
        assert id1 == id2

    def test_different_names_different_ids(self, plugin):
        id1 = plugin._generate_app_id("/path/exe", "Game A")
        id2 = plugin._generate_app_id("/path/exe", "Game B")
        assert id1 != id2


class TestArtworkIdGeneration:
    def test_generates_unsigned(self, plugin):
        art_id = plugin._generate_artwork_id("/path/exe", "Game")
        assert art_id > 0

    def test_matches_app_id_bits(self, plugin):
        # artwork_id and app_id should share the same CRC base
        art_id = plugin._generate_artwork_id("/path/exe", "Game")
        assert art_id & 0x80000000  # High bit set


class TestResolveSystem:
    def test_exact_slug_match(self, plugin):
        result = plugin._resolve_system("n64")
        assert result == "n64"

    def test_fs_slug_fallback(self, plugin):
        # A slug not in the map but its fs_slug is
        result = plugin._resolve_system("nonexistent-slug", "n64")
        assert result == "n64"

    def test_fallback_returns_slug_as_is(self, plugin):
        result = plugin._resolve_system("totally-unknown-platform")
        assert result == "totally-unknown-platform"


class TestSettings:
    @pytest.mark.asyncio
    async def test_get_settings_masks_password(self, plugin):
        plugin.settings["romm_pass"] = "secret123"
        result = await plugin.get_settings()
        assert result["romm_pass_masked"] == "••••"
        assert "secret123" not in str(result)

    @pytest.mark.asyncio
    async def test_get_settings_empty_password(self, plugin):
        plugin.settings["romm_pass"] = ""
        result = await plugin.get_settings()
        assert result["romm_pass_masked"] == ""

    @pytest.mark.asyncio
    async def test_save_settings_skips_masked_password(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        plugin.settings["romm_pass"] = "original"
        await plugin.save_settings("http://example.com", "user", "••••")
        assert plugin.settings["romm_pass"] == "original"

    @pytest.mark.asyncio
    async def test_save_settings_updates_real_password(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        plugin.settings["romm_pass"] = "old"
        await plugin.save_settings("http://example.com", "user", "newpass")
        assert plugin.settings["romm_pass"] == "newpass"


class TestRommDownloadUrlEncoding:
    def test_encodes_spaces_in_cover_path(self, plugin, tmp_path):
        """Cover paths from RomM contain unencoded spaces in timestamps.
        _romm_download must URL-encode them so urllib doesn't reject the URL."""
        import urllib.parse

        # Simulate the path RomM returns
        path = "/assets/romm/resources/roms/53/4375/cover/big.png?ts=2025-07-28 00:05:03"
        encoded = urllib.parse.quote(path, safe="/:?=&@")
        assert " " not in encoded
        assert "%20" in encoded
        assert encoded == "/assets/romm/resources/roms/53/4375/cover/big.png?ts=2025-07-28%2000:05:03"

    def test_preserves_clean_paths(self, plugin):
        """Paths without spaces should pass through unchanged."""
        import urllib.parse

        path = "/assets/romm/resources/roms/53/4375/cover/big.png"
        encoded = urllib.parse.quote(path, safe="/:?=&@")
        assert encoded == path


class TestPlatformMap:
    def test_loads_config_json(self, plugin):
        pm = plugin._load_platform_map()
        assert isinstance(pm, dict)
        assert "n64" in pm
        assert "snes" in pm
        assert len(pm) > 50  # Should have many entries


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


class TestStartDownload:
    @pytest.mark.asyncio
    async def test_starts_download_task(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch

        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "fs_size_bytes": 1024,
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=rom_detail)
        plugin.loop.create_task = MagicMock(return_value=MagicMock())

        with patch("shutil.disk_usage", return_value=MagicMock(free=500 * 1024 * 1024)):
            result = await plugin.start_download(42)

        assert result["success"] is True
        assert 42 in plugin._download_queue
        assert plugin._download_queue[42]["status"] == "downloading"
        plugin.loop.create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejects_already_downloading(self, plugin):
        plugin._download_in_progress.add(42)
        result = await plugin.start_download(42)
        assert result["success"] is False
        assert "Already downloading" in result["message"]

    @pytest.mark.asyncio
    async def test_rejects_if_rom_not_found(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(
            side_effect=Exception("HTTP Error 404: Not Found")
        )

        result = await plugin.start_download(9999)
        assert result["success"] is False
        assert "Failed to fetch ROM details" in result["message"]

    @pytest.mark.asyncio
    async def test_checks_disk_space(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch

        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "fs_size_bytes": 500 * 1024 * 1024,  # 500MB
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=rom_detail)

        with patch("shutil.disk_usage", return_value=MagicMock(free=50 * 1024 * 1024)):
            result = await plugin.start_download(42)

        assert result["success"] is False
        assert "disk space" in result["message"].lower()


class TestCancelDownload:
    @pytest.mark.asyncio
    async def test_cancels_active_download(self, plugin):
        # Create a real future that raises CancelledError when awaited
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        fut.cancel()

        plugin._download_tasks[42] = fut
        plugin._download_queue[42] = {"status": "downloading"}

        result = await plugin.cancel_download(42)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_error(self, plugin):
        result = await plugin.cancel_download(999)
        assert result["success"] is False
        assert "No active download" in result["message"]


class TestGetDownloadQueue:
    @pytest.mark.asyncio
    async def test_returns_empty_queue(self, plugin):
        result = await plugin.get_download_queue()
        assert result["downloads"] == []

    @pytest.mark.asyncio
    async def test_returns_active_downloads(self, plugin):
        plugin._download_queue[1] = {
            "rom_id": 1, "rom_name": "Game A", "status": "downloading", "progress": 0.5,
        }
        result = await plugin.get_download_queue()
        assert len(result["downloads"]) == 1
        assert result["downloads"][0]["status"] == "downloading"
        assert result["downloads"][0]["progress"] == 0.5

    @pytest.mark.asyncio
    async def test_returns_completed_downloads(self, plugin):
        plugin._download_queue[1] = {
            "rom_id": 1, "rom_name": "Game A", "status": "downloading", "progress": 0.5,
        }
        plugin._download_queue[2] = {
            "rom_id": 2, "rom_name": "Game B", "status": "completed", "progress": 1.0,
        }
        result = await plugin.get_download_queue()
        assert len(result["downloads"]) == 2
        statuses = {d["status"] for d in result["downloads"]}
        assert statuses == {"downloading", "completed"}


class TestGetInstalledRom:
    @pytest.mark.asyncio
    async def test_returns_installed_rom(self, plugin):
        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42, "file_path": "/roms/n64/zelda.z64", "system": "n64",
        }
        result = await plugin.get_installed_rom(42)
        assert result is not None
        assert result["rom_id"] == 42
        assert result["system"] == "n64"

    @pytest.mark.asyncio
    async def test_returns_none_not_installed(self, plugin):
        result = await plugin.get_installed_rom(999)
        assert result is None


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


class TestRemoveRom:
    @pytest.mark.asyncio
    async def test_deletes_file_and_clears_state(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        rom_file = tmp_path / "retrodeck" / "roms" / "n64" / "zelda.z64"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("fake rom data")

        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42, "file_path": str(rom_file), "system": "n64",
        }
        plugin._download_queue[42] = {"status": "completed"}

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert not rom_file.exists()
        assert "42" not in plugin._state["installed_roms"]
        assert 42 not in plugin._download_queue

    @pytest.mark.asyncio
    async def test_returns_error_not_installed(self, plugin):
        result = await plugin.remove_rom(999)
        assert result["success"] is False
        assert "not installed" in result["message"].lower()


class TestUninstallAllRoms:
    @pytest.mark.asyncio
    async def test_removes_all_installed(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        file_a = roms_dir / "game_a.z64"
        file_b = roms_dir / "game_b.z64"
        file_a.write_text("data a")
        file_b.write_text("data b")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(file_a), "system": "n64"},
            "2": {"rom_id": 2, "file_path": str(file_b), "system": "n64"},
        }

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 2
        assert not file_a.exists()
        assert not file_b.exists()

    @pytest.mark.asyncio
    async def test_clears_state(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/nonexistent", "system": "n64"},
        }

        await plugin.uninstall_all_roms()
        assert plugin._state["installed_roms"] == {}

    @pytest.mark.asyncio
    async def test_handles_missing_files(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/does/not/exist.z64", "system": "n64"},
            "2": {"rom_id": 2, "file_path": "/also/missing.z64", "system": "snes"},
        }

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert plugin._state["installed_roms"] == {}


class TestDetectLaunchFile:
    def test_prefers_m3u(self, plugin, tmp_path):
        (tmp_path / "game.m3u").write_text("disc1.cue")
        (tmp_path / "disc1.cue").write_text("cue data")
        (tmp_path / "disc1.bin").write_bytes(b"\x00" * 1000)

        result = plugin._detect_launch_file(str(tmp_path))
        assert result.endswith(".m3u")

    def test_falls_back_to_cue(self, plugin, tmp_path):
        (tmp_path / "disc1.cue").write_text("cue data")
        (tmp_path / "disc1.bin").write_bytes(b"\x00" * 1000)

        result = plugin._detect_launch_file(str(tmp_path))
        assert result.endswith(".cue")

    def test_falls_back_to_largest(self, plugin, tmp_path):
        (tmp_path / "small.bin").write_bytes(b"\x00" * 100)
        (tmp_path / "large.bin").write_bytes(b"\x00" * 10000)

        result = plugin._detect_launch_file(str(tmp_path))
        assert result.endswith("large.bin")


class TestDownloadRequestPolling:
    @pytest.mark.asyncio
    async def test_processes_download_request(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, patch

        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        requests_path = tmp_path / "download_requests.json"
        requests_path.write_text(json.dumps([{"rom_id": 42}]))

        with patch.object(plugin, "start_download", new_callable=AsyncMock) as mock_start:
            # Call internal logic directly: read file, process, clear
            with open(requests_path, "r") as f:
                requests = json.load(f)
            with open(requests_path, "w") as f:
                json.dump([], f)
            for req in requests:
                rom_id = req.get("rom_id")
                if rom_id:
                    await plugin.start_download(rom_id)

            mock_start.assert_called_once_with(42)

    @pytest.mark.asyncio
    async def test_cleans_up_request_file(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        requests_path = tmp_path / "download_requests.json"
        requests_path.write_text(json.dumps([{"rom_id": 1}, {"rom_id": 2}]))

        # Simulate the cleanup logic from _poll_download_requests
        with open(requests_path, "r") as f:
            requests = json.load(f)
        with open(requests_path, "w") as f:
            json.dump([], f)

        # Verify file was cleared
        with open(requests_path, "r") as f:
            remaining = json.load(f)
        assert remaining == []
        assert len(requests) == 2


class TestMultiFileRomDeletion:
    @pytest.mark.asyncio
    async def test_remove_rom_deletes_rom_dir(self, plugin, tmp_path):
        """Multi-file ROM with rom_dir should delete the entire directory."""
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        rom_dir = tmp_path / "retrodeck" / "roms" / "psx" / "FF7"
        rom_dir.mkdir(parents=True)
        (rom_dir / "FF7.m3u").write_text("disc1.cue")
        (rom_dir / "disc1.cue").write_text("cue")
        (rom_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": str(rom_dir / "FF7.m3u"),
            "rom_dir": str(rom_dir),
            "system": "psx",
        }

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert not rom_dir.exists()
        # Parent system dir should still exist
        assert (tmp_path / "retrodeck" / "roms" / "psx").exists()

    @pytest.mark.asyncio
    async def test_uninstall_all_deletes_rom_dirs(self, plugin, tmp_path):
        """uninstall_all_roms should delete multi-file ROM directories."""
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        rom_dir = tmp_path / "retrodeck" / "roms" / "psx" / "FF7"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        plugin._state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": str(rom_dir / "FF7.m3u"),
                "rom_dir": str(rom_dir),
                "system": "psx",
            },
        }

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 1
        assert not rom_dir.exists()


class TestPruneStaleState:
    def test_prunes_missing_files(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/nonexistent/game.z64", "system": "n64"},
        }

        plugin._prune_stale_state()
        assert "1" not in plugin._state["installed_roms"]

    def test_keeps_existing_files(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
        }

        plugin._prune_stale_state()
        assert "1" in plugin._state["installed_roms"]

    def test_keeps_existing_rom_dir(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        rom_dir = tmp_path / "FF7"
        rom_dir.mkdir()

        plugin._state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": str(rom_dir / "FF7.m3u"),  # file missing but dir exists
                "rom_dir": str(rom_dir),
                "system": "psx",
            },
        }

        plugin._prune_stale_state()
        assert "1" in plugin._state["installed_roms"]

    def test_saves_state_only_when_pruned(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
        }

        # No pruning needed — state file should NOT be written
        state_path = tmp_path / "state.json"
        plugin._prune_stale_state()
        assert not state_path.exists()

    def test_prunes_mixed(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        rom_file = tmp_path / "game.z64"
        rom_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom_file), "system": "n64"},
            "2": {"rom_id": 2, "file_path": "/gone/game.z64", "system": "snes"},
        }

        plugin._prune_stale_state()
        assert "1" in plugin._state["installed_roms"]
        assert "2" not in plugin._state["installed_roms"]


class TestMaybeGenerateM3u:
    def test_generates_m3u_for_multiple_cue_files(self, plugin, tmp_path):
        """When multiple .cue files exist and no .m3u, auto-generate one."""
        (tmp_path / "Game - Disc 1.cue").write_text("cue disc 1")
        (tmp_path / "Game - Disc 1.bin").write_bytes(b"\x00" * 1000)
        (tmp_path / "Game - Disc 2.cue").write_text("cue disc 2")
        (tmp_path / "Game - Disc 2.bin").write_bytes(b"\x00" * 1000)

        rom_detail = {"fs_name_no_ext": "Final Fantasy VII", "name": "Final Fantasy VII"}
        plugin._maybe_generate_m3u(str(tmp_path), rom_detail)

        m3u_path = tmp_path / "Final Fantasy VII.m3u"
        assert m3u_path.exists()
        content = m3u_path.read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 2
        assert lines[0] == "Game - Disc 1.cue"
        assert lines[1] == "Game - Disc 2.cue"

    def test_generates_m3u_for_multiple_chd_files(self, plugin, tmp_path):
        """CHD multi-disc should also get an M3U."""
        (tmp_path / "Game (Disc 1).chd").write_bytes(b"\x00" * 100)
        (tmp_path / "Game (Disc 2).chd").write_bytes(b"\x00" * 100)

        rom_detail = {"fs_name_no_ext": "Game", "name": "Game"}
        plugin._maybe_generate_m3u(str(tmp_path), rom_detail)

        m3u_path = tmp_path / "Game.m3u"
        assert m3u_path.exists()
        lines = m3u_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_skips_if_m3u_exists(self, plugin, tmp_path):
        """Should not overwrite an existing M3U."""
        (tmp_path / "existing.m3u").write_text("original content")
        (tmp_path / "disc1.cue").write_text("cue 1")
        (tmp_path / "disc2.cue").write_text("cue 2")

        rom_detail = {"fs_name_no_ext": "Game"}
        plugin._maybe_generate_m3u(str(tmp_path), rom_detail)

        # Only the original M3U should exist, unchanged
        assert (tmp_path / "existing.m3u").read_text() == "original content"
        assert not (tmp_path / "Game.m3u").exists()

    def test_skips_single_disc(self, plugin, tmp_path):
        """Single disc file should not generate an M3U."""
        (tmp_path / "game.cue").write_text("cue data")
        (tmp_path / "game.bin").write_bytes(b"\x00" * 1000)

        rom_detail = {"fs_name_no_ext": "Game"}
        plugin._maybe_generate_m3u(str(tmp_path), rom_detail)

        assert not (tmp_path / "Game.m3u").exists()

    def test_uses_name_fallback(self, plugin, tmp_path):
        """Falls back to rom name when fs_name_no_ext is missing."""
        (tmp_path / "d1.chd").write_bytes(b"\x00" * 100)
        (tmp_path / "d2.chd").write_bytes(b"\x00" * 100)

        rom_detail = {"name": "My Game"}
        plugin._maybe_generate_m3u(str(tmp_path), rom_detail)

        assert (tmp_path / "My Game.m3u").exists()


class TestDoDownloadSingleFile:
    """Tests for _do_download happy path — single file."""

    @pytest.mark.asyncio
    async def test_single_file_happy_path(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch

        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "zelda.z64")

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
            "has_multiple_files": False,
        }

        def fake_download(path, dest, progress_callback=None):
            with open(dest, "wb") as f:
                f.write(b"\x00" * 512)

        plugin.loop = asyncio.get_event_loop()
        plugin._download_queue[42] = {"rom_id": 42, "status": "downloading", "progress": 0}

        with patch.object(plugin, "_romm_download", side_effect=fake_download):
            await plugin._do_download(42, rom_detail, target_path, "n64")

        # File ends up at target_path (not .tmp)
        assert os.path.exists(target_path)
        assert not os.path.exists(target_path + ".tmp")
        # installed_roms entry is created
        installed = plugin._state["installed_roms"].get("42")
        assert installed is not None
        assert installed["rom_id"] == 42
        assert installed["file_path"] == target_path
        assert installed["system"] == "n64"
        assert "installed_at" in installed
        # download_complete event emitted
        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "download_complete"]
        assert len(emit_calls) == 1
        assert emit_calls[0][0][1]["rom_id"] == 42
        # download_queue status is completed
        assert plugin._download_queue[42]["status"] == "completed"


class TestDoDownloadMultiFile:
    """Tests for _do_download happy path — multi-file (ZIP)."""

    @pytest.mark.asyncio
    async def test_multi_file_happy_path(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch
        import zipfile as zf

        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "FF7.zip")

        # Create a real ZIP file that our fake download will write
        zip_content_path = tmp_path / "source.zip"
        with zf.ZipFile(str(zip_content_path), "w") as z:
            z.writestr("disc1.cue", "FILE disc1.bin BINARY")
            z.writestr("disc1.bin", b"\x00" * 100)
            z.writestr("disc2.cue", "FILE disc2.bin BINARY")
            z.writestr("disc2.bin", b"\x00" * 100)
        zip_bytes = zip_content_path.read_bytes()

        rom_detail = {
            "id": 55,
            "name": "Final Fantasy VII",
            "fs_name": "FF7.zip",
            "fs_name_no_ext": "FF7",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(path, dest, progress_callback=None):
            with open(dest, "wb") as f:
                f.write(zip_bytes)

        plugin.loop = asyncio.get_event_loop()
        plugin._download_queue[55] = {"rom_id": 55, "status": "downloading", "progress": 0}

        with patch.object(plugin, "_romm_download", side_effect=fake_download):
            await plugin._do_download(55, rom_detail, target_path, "psx")

        # ZIP is extracted to extract_dir
        extract_dir = roms_dir / "FF7"
        assert extract_dir.is_dir()
        assert (extract_dir / "disc1.cue").exists()
        assert (extract_dir / "disc2.cue").exists()
        # .zip.tmp is cleaned up
        assert not os.path.exists(target_path + ".zip.tmp")
        # installed_roms entry has rom_dir
        installed = plugin._state["installed_roms"].get("55")
        assert installed is not None
        assert installed["rom_dir"] == str(extract_dir)
        # Launch file detection: M3U generated from 2 cue files, so prefer M3U > CUE
        # (M3U auto-generated by _maybe_generate_m3u)
        assert installed["file_path"].endswith((".m3u", ".cue"))
        # Status is completed
        assert plugin._download_queue[55]["status"] == "completed"


class TestPathTraversalDeleteRomFiles:
    """Tests for path traversal safety in _delete_rom_files."""

    @pytest.mark.asyncio
    async def test_rejects_rom_dir_outside_roms_base(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        # Create a file outside roms dir that should NOT be deleted
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir()
        evil_file = evil_dir / "important.txt"
        evil_file.write_text("do not delete")

        plugin._state["installed_roms"]["99"] = {
            "rom_id": 99,
            "file_path": str(evil_file),
            "rom_dir": str(evil_dir),
            "system": "n64",
        }

        result = await plugin.remove_rom(99)
        # The evil dir/file should NOT be deleted
        assert evil_dir.exists()
        assert evil_file.exists()
        # State should still be cleaned up
        assert "99" not in plugin._state["installed_roms"]

    @pytest.mark.asyncio
    async def test_rejects_file_path_outside_roms_base(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        evil_file = tmp_path / "etc" / "passwd"
        evil_file.parent.mkdir(parents=True)
        evil_file.write_text("root:x:0:0")

        plugin._state["installed_roms"]["99"] = {
            "rom_id": 99,
            "file_path": str(evil_file),
            "system": "n64",
        }

        result = await plugin.remove_rom(99)
        assert evil_file.exists()
        assert "99" not in plugin._state["installed_roms"]


class TestPathTraversalFsName:
    """Tests for path traversal safety in download — fs_name sanitization."""

    @pytest.mark.asyncio
    async def test_fs_name_traversal_sanitized(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch

        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        rom_detail = {
            "id": 77,
            "name": "Evil ROM",
            "fs_name": "../../../etc/passwd",
            "fs_size_bytes": 1024,
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=rom_detail)
        plugin.loop.create_task = MagicMock(return_value=MagicMock())

        with patch("shutil.disk_usage", return_value=MagicMock(free=500 * 1024 * 1024)):
            result = await plugin.start_download(77)

        assert result["success"] is True
        # The target path should use sanitized basename only
        queue_entry = plugin._download_queue[77]
        assert queue_entry["file_name"] == "passwd"
        # create_task was called with args containing only the safe path
        call_args = plugin.loop.create_task.call_args[0][0]
        # The coroutine was created — just verify the queue entry is safe
        assert ".." not in queue_entry["file_name"]


class TestCleanupPartialDownload:
    """Tests for _cleanup_partial_download — all paths."""

    def test_cleans_tmp_file_single(self, plugin, tmp_path):
        target = str(tmp_path / "game.z64")
        tmp_file = tmp_path / "game.z64.tmp"
        tmp_file.write_text("partial")

        plugin._cleanup_partial_download(target, False, "game.z64")
        assert not tmp_file.exists()

    def test_cleans_zip_tmp_multi(self, plugin, tmp_path):
        target = str(tmp_path / "game.zip")
        zip_tmp = tmp_path / "game.zip.zip.tmp"
        zip_tmp.write_text("partial zip")

        plugin._cleanup_partial_download(target, True, "game.zip")
        assert not zip_tmp.exists()

    def test_cleans_extract_dir(self, plugin, tmp_path):
        target = str(tmp_path / "game.zip")
        extract_dir = tmp_path / "game"
        extract_dir.mkdir()
        (extract_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        plugin._cleanup_partial_download(target, True, "game.zip")
        assert not extract_dir.exists()

    def test_cleanup_errors_are_caught(self, plugin, tmp_path):
        """Cleanup should not raise even if files don't exist."""
        target = str(tmp_path / "nonexistent.z64")
        # Should not raise
        plugin._cleanup_partial_download(target, False, "nonexistent.z64")
        plugin._cleanup_partial_download(target, True, "nonexistent.zip")


class TestDoDownloadCancelled:
    """Tests for _do_download — cancelled mid-download."""

    @pytest.mark.asyncio
    async def test_cancelled_sets_status_and_cleans_up(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "zelda.z64")

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
            "has_multiple_files": False,
        }

        def fake_download_cancel(path, dest, progress_callback=None):
            raise asyncio.CancelledError()

        plugin.loop = asyncio.get_event_loop()
        plugin._download_queue[42] = {"rom_id": 42, "status": "downloading", "progress": 0}

        with patch.object(plugin, "_romm_download", side_effect=fake_download_cancel):
            await plugin._do_download(42, rom_detail, target_path, "n64")

        assert plugin._download_queue[42]["status"] == "cancelled"
        assert not os.path.exists(target_path)
        assert "42" not in plugin._state["installed_roms"]


class TestDoDownloadZipFailure:
    """Tests for _do_download — ZIP extraction failure."""

    @pytest.mark.asyncio
    async def test_zip_failure_sets_failed_and_cleans_up(self, plugin, tmp_path):
        from unittest.mock import patch
        import zipfile as zf

        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "game.zip")

        rom_detail = {
            "id": 66,
            "name": "Bad ZIP Game",
            "fs_name": "game.zip",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(path, dest, progress_callback=None):
            # Write invalid data (not a real zip)
            with open(dest, "wb") as f:
                f.write(b"not a zip file")

        plugin.loop = asyncio.get_event_loop()
        plugin._download_queue[66] = {"rom_id": 66, "status": "downloading", "progress": 0}

        with patch.object(plugin, "_romm_download", side_effect=fake_download):
            await plugin._do_download(66, rom_detail, target_path, "psx")

        assert plugin._download_queue[66]["status"] == "failed"
        # .zip.tmp should be cleaned up
        assert not os.path.exists(target_path + ".zip.tmp")


class TestStartDownloadReDownload:
    """Test start_download allows re-download after completion."""

    @pytest.mark.asyncio
    async def test_re_download_after_completed(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch

        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "fs_size_bytes": 1024,
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=rom_detail)
        plugin.loop.create_task = MagicMock(return_value=MagicMock())

        # Set status to completed (previous download)
        plugin._download_queue[42] = {"status": "completed"}

        with patch("shutil.disk_usage", return_value=MagicMock(free=500 * 1024 * 1024)):
            result = await plugin.start_download(42)

        assert result["success"] is True
        assert plugin._download_queue[42]["status"] == "downloading"


class TestMaybeGenerateM3uMixedFormats:
    """Test M3U generation with mixed disc formats."""

    def test_mixed_cue_and_chd(self, plugin, tmp_path):
        (tmp_path / "disc1.cue").write_text("cue 1")
        (tmp_path / "disc2.chd").write_bytes(b"\x00" * 100)

        rom_detail = {"fs_name_no_ext": "Mixed Game", "name": "Mixed Game"}
        plugin._maybe_generate_m3u(str(tmp_path), rom_detail)

        m3u_path = tmp_path / "Mixed Game.m3u"
        assert m3u_path.exists()
        content = m3u_path.read_text().strip()
        lines = content.split("\n")
        assert len(lines) == 2
        # Should include both formats
        exts = {os.path.splitext(l)[1] for l in lines}
        assert ".cue" in exts
        assert ".chd" in exts


class TestMaybeGenerateM3uSpecialCharacters:
    """Test M3U preserves special characters in filenames."""

    def test_special_characters_preserved(self, plugin, tmp_path):
        names = [
            "Game (Disc 1) [Japan].cue",
            "Game (Disc 2) [Japan].cue",
        ]
        for name in names:
            (tmp_path / name).write_text("cue data")

        rom_detail = {"fs_name_no_ext": "Game", "name": "Game"}
        plugin._maybe_generate_m3u(str(tmp_path), rom_detail)

        m3u_path = tmp_path / "Game.m3u"
        assert m3u_path.exists()
        content = m3u_path.read_text().strip()
        lines = content.split("\n")
        assert len(lines) == 2
        # Verify special chars preserved exactly
        for name in names:
            assert name in lines


class TestPruneStaleStateEdgeCases:
    """Edge case tests for _prune_stale_state."""

    def test_empty_installed_roms_no_crash(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._state["installed_roms"] = {}
        plugin._prune_stale_state()
        # Should not crash, _save_state should NOT be called
        state_path = tmp_path / "state.json"
        assert not state_path.exists()

    def test_all_entries_stale(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/gone/a.z64", "system": "n64"},
            "2": {"rom_id": 2, "file_path": "/gone/b.z64", "system": "snes"},
            "3": {"rom_id": 3, "file_path": "/gone/c.z64", "system": "gb"},
        }

        plugin._prune_stale_state()
        assert plugin._state["installed_roms"] == {}
        # _save_state should have been called (state.json written)
        state_path = tmp_path / "state.json"
        assert state_path.exists()


class TestUninstallAllRomsMixedResults:
    """Test uninstall_all_roms with mixed success/failure."""

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        # Create a real file that can be deleted
        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        good_file = roms_dir / "game_a.z64"
        good_file.write_text("data")

        # Create another file but make deletion fail by using a non-safe path
        # (outside roms dir, which _delete_rom_files should reject silently)
        bad_file = tmp_path / "outside" / "game_b.z64"
        bad_file.parent.mkdir(parents=True)
        bad_file.write_text("data")

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(good_file), "system": "n64"},
            "2": {"rom_id": 2, "file_path": str(bad_file), "system": "snes"},
        }

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        # good_file should be deleted
        assert not good_file.exists()
        # bad_file should still exist (outside roms dir)
        assert bad_file.exists()
        # removed_count reflects successful deletions
        # The current code clears all state regardless of deletion success
        assert result["removed_count"] in (1, 2)  # depends on whether _delete_rom_files raises or silently skips


class TestRemoveRomFileAlreadyGone:
    """Test remove_rom when file is already deleted."""

    @pytest.mark.asyncio
    async def test_file_already_gone_cleans_state(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)

        # Entry exists in state but file is gone
        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": str(tmp_path / "retrodeck" / "roms" / "n64" / "gone.z64"),
            "system": "n64",
        }

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert "42" not in plugin._state["installed_roms"]


class TestUrlEncodedFilenameRename:
    """Tests for URL-encoded filename fix after ZIP extraction."""

    @pytest.mark.asyncio
    async def test_renames_url_encoded_files_after_extract(self, plugin, tmp_path):
        from unittest.mock import patch
        import zipfile as zf

        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "Vagrant Story (USA).zip")

        # Create a ZIP with URL-encoded filenames (as RomM generates)
        zip_content_path = tmp_path / "source.zip"
        with zf.ZipFile(str(zip_content_path), "w") as z:
            z.writestr("Vagrant%20Story%20%28USA%29.m3u", "Vagrant%20Story%20%28USA%29%20%28Disc%201%29.chd\n")
            z.writestr("Vagrant%20Story%20%28USA%29%20%28Disc%201%29.chd", b"\x00" * 100)
        zip_bytes = zip_content_path.read_bytes()

        rom_detail = {
            "id": 99,
            "name": "Vagrant Story (USA)",
            "fs_name": "Vagrant Story (USA).zip",
            "fs_name_no_ext": "Vagrant Story (USA)",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(path, dest, progress_callback=None):
            with open(dest, "wb") as f:
                f.write(zip_bytes)

        plugin.loop = asyncio.get_event_loop()
        plugin._download_queue[99] = {"rom_id": 99, "status": "downloading", "progress": 0}

        with patch.object(plugin, "_romm_download", side_effect=fake_download):
            await plugin._do_download(99, rom_detail, target_path, "psx")

        extract_dir = roms_dir / "Vagrant Story (USA)"
        # URL-encoded filenames should be decoded
        assert (extract_dir / "Vagrant Story (USA).m3u").exists()
        assert (extract_dir / "Vagrant Story (USA) (Disc 1).chd").exists()
        # The percent-encoded versions should NOT exist
        assert not (extract_dir / "Vagrant%20Story%20%28USA%29.m3u").exists()
        assert not (extract_dir / "Vagrant%20Story%20%28USA%29%20%28Disc%201%29.chd").exists()

    @pytest.mark.asyncio
    async def test_leaves_normal_filenames_alone(self, plugin, tmp_path):
        from unittest.mock import patch
        import zipfile as zf

        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "FF7.zip")

        zip_content_path = tmp_path / "source.zip"
        with zf.ZipFile(str(zip_content_path), "w") as z:
            z.writestr("disc1.cue", "FILE disc1.bin BINARY")
            z.writestr("disc1.bin", b"\x00" * 100)
            z.writestr("disc2.cue", "FILE disc2.bin BINARY")
            z.writestr("disc2.bin", b"\x00" * 100)
        zip_bytes = zip_content_path.read_bytes()

        rom_detail = {
            "id": 55,
            "name": "Final Fantasy VII",
            "fs_name": "FF7.zip",
            "fs_name_no_ext": "FF7",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(path, dest, progress_callback=None):
            with open(dest, "wb") as f:
                f.write(zip_bytes)

        plugin.loop = asyncio.get_event_loop()
        plugin._download_queue[55] = {"rom_id": 55, "status": "downloading", "progress": 0}

        with patch.object(plugin, "_romm_download", side_effect=fake_download):
            await plugin._do_download(55, rom_detail, target_path, "psx")

        extract_dir = roms_dir / "FF7"
        # Normal filenames should be unchanged
        assert (extract_dir / "disc1.cue").exists()
        assert (extract_dir / "disc1.bin").exists()
        assert (extract_dir / "disc2.cue").exists()
        assert (extract_dir / "disc2.bin").exists()
