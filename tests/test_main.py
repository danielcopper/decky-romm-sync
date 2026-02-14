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
