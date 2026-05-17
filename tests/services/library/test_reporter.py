"""Tests for SyncReporter — post-apply registry updates, finalisation, registry queries."""

import os

import pytest

from adapters.persistence import (
    PersistenceAdapter,
)

# conftest.py patches decky before this import


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
        from unittest.mock import patch

        grid = str(tmp_path)
        staging = tmp_path / "romm_1_cover.png"
        staging.write_text("data")

        with patch("os.replace", side_effect=OSError("perm denied")):
            result = plugin._sync_service._reporter._finalize_cover_path(grid, str(staging), 100001, "1")
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
        result = plugin._sync_service._reporter._build_registry_entry(pending, 100001, "/grid/100001p.png")
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
        result = plugin._sync_service._reporter._build_registry_entry(pending, 100002, "")
        assert "igdb_id" not in result
        assert "sgdb_id" not in result
        assert "ra_id" not in result

    def test_missing_keys_default_to_empty(self, plugin):
        pending = {}
        result = plugin._sync_service._reporter._build_registry_entry(pending, 100003, "")
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
