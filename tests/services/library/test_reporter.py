"""Tests for SyncReporter — post-apply registry updates, finalisation, registry queries."""

import os

import pytest
from fakes.fake_cover_art_file_store import FakeCoverArtFileStore

from adapters.persistence import (
    PersistenceAdapter,
)

# conftest.py patches decky before this import


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
        result = plugin._sync_service.get_rom_by_steam_app_id(100001)
        assert result is not None
        assert result["rom_id"] == 42
        assert result["name"] == "Zelda"
        assert result["installed"] is not None

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown(self, plugin):
        result = plugin._sync_service.get_rom_by_steam_app_id(999999)
        assert result is None


class TestFinalizeCoverPath:
    """Tests for _finalize_cover_path() — lines 699-712."""

    def test_renames_staging_to_final(self, plugin, tmp_path):
        grid = str(tmp_path)
        staging = tmp_path / "romm_1_cover.png"
        staging.write_text("cover data")

        result = plugin._sync_service._reporter._finalize_cover_path(grid, str(staging), 100001, "1")
        expected = os.path.join(grid, "100001p.png")
        assert result == expected
        assert not staging.exists()
        assert os.path.exists(expected)

    def test_returns_existing_final(self, plugin, tmp_path):
        grid = str(tmp_path)
        final = tmp_path / "100001p.png"
        final.write_text("final data")

        result = plugin._sync_service._reporter._finalize_cover_path(grid, "/nonexistent/path.png", 100001, "1")
        assert result == str(final)

    def test_returns_cover_path_when_no_grid(self, plugin):
        result = plugin._sync_service._reporter._finalize_cover_path(None, "/some/path.png", 100001, "1")
        assert result == "/some/path.png"

    def test_returns_cover_path_when_empty(self, plugin, tmp_path):
        result = plugin._sync_service._reporter._finalize_cover_path(str(tmp_path), "", 100001, "1")
        assert result == ""

    def test_handles_rename_os_error(self, plugin, tmp_path):
        grid = str(tmp_path)
        staging_path = os.path.join(grid, "romm_1_cover.png")

        # Inject OSError on rename through the CoverArtFileStore Protocol —
        # mirrors the Wave 3 fake-adapter failure-injection pattern instead
        # of patching ``os.replace`` globally.
        fake_store = FakeCoverArtFileStore(files={staging_path: b"data"})
        fake_store.rename_failures.add(staging_path)
        plugin._artwork_service._cover_art_file_store = fake_store

        result = plugin._sync_service._reporter._finalize_cover_path(grid, staging_path, 100001, "1")
        # Should return original path on error
        assert result == staging_path


class TestCommitUnitResults:
    """Tests for _commit_unit_results_io — registry write via store with field preservation."""

    def test_commit_preserves_sgdb_id_when_pending_lacks_it(self, plugin):
        """Field-clobber regression (#745): pending without sgdb_id must not wipe an existing sgdb_id."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Old",
            "fs_name": "old.z64",
            "platform_name": "N64",
            "platform_slug": "n64",
            "cover_path": "/covers/old.png",
            "sgdb_id": 999,
        }
        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_name": "Game Boy",
            "platform_slug": "gb",
            "cover_path": "",
        }

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001})

        entry = plugin._state["shortcut_registry"]["42"]
        assert entry["sgdb_id"] == 999
        assert entry["name"] == "Game"
        assert entry["app_id"] == 100001

    def test_commit_preserves_igdb_and_ra_ids_when_pending_lacks_them(self, plugin):
        """The preservation guarantee extends to igdb_id and ra_id."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Old",
            "fs_name": "old.z64",
            "platform_name": "N64",
            "platform_slug": "n64",
            "cover_path": "/covers/old.png",
            "igdb_id": 555,
            "ra_id": 777,
        }
        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_name": "Game Boy",
            "platform_slug": "gb",
            "cover_path": "",
        }

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001})

        entry = plugin._state["shortcut_registry"]["42"]
        assert entry["igdb_id"] == 555
        assert entry["ra_id"] == 777


class TestClearSyncCache:
    """Tests for clear_sync_cache() — lines 1037-1042."""

    def test_clears_last_sync(self, plugin):
        plugin._state["last_sync"] = "2025-01-01T00:00:00"
        result = plugin._sync_service.clear_sync_cache()
        assert result["success"] is True
        assert plugin._state["last_sync"] is None


class TestFinalizePerUnitRun:
    """SyncReporter.finalize_per_unit_run — emits sync_collections + sync_complete after the per-unit loop."""

    @pytest.mark.asyncio
    async def test_builds_platform_collections_from_registry(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        decky.emit.reset_mock()

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "A", "platform_name": "N64", "platform_slug": "n64"},
            "2": {"app_id": 1002, "name": "B", "platform_name": "SNES", "platform_slug": "snes"},
        }
        plugin.settings["collection_create_platform_groups"] = True

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids={1, 2},
            total_games=2,
        )

        collections_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_collections"]
        assert len(collections_events) == 1
        payload = collections_events[0][0][1]
        assert set(payload["platform_app_ids"].keys()) == {"N64", "SNES"}

    @pytest.mark.asyncio
    async def test_emits_sync_complete_terminal(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        decky.emit.reset_mock()

        plugin._state["shortcut_registry"] = {}

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids=set(),
            total_games=0,
        )

        complete_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert len(complete_events) == 1
        assert "cancelled" not in complete_events[0][0][1]

    @pytest.mark.asyncio
    async def test_sets_state_to_idle_at_end(self, plugin, tmp_path):
        import decky

        from domain.sync_state import SyncState

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        decky.emit.reset_mock()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "sync-xyz"

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids=set(),
            total_games=0,
        )

        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._current_sync_id is None

    @pytest.mark.asyncio
    async def test_persists_last_sync_metadata(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        decky.emit.reset_mock()
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "A", "platform_name": "N64", "platform_slug": "n64"},
        }
        plugin.settings["collection_create_platform_groups"] = True

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={"Faves": [1]},
            pending_platform_rom_ids={1},
            total_games=1,
        )

        assert plugin._state["last_sync"] is not None
        assert plugin._state["last_synced_platforms"] == ["N64"]
        assert plugin._state["last_synced_collections"] == ["Faves"]
