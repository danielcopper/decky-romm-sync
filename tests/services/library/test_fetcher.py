"""Tests for LibraryFetcher — platform/collection roundtrips, ROM fetch pipeline."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.sync_state import SyncState

# conftest.py patches decky before this import
from tests.services.library._helpers import _make_loop_raising, _make_loop_with_executor


class TestCheckCancelling:
    """Tests for _check_cancelling() — lines 505-508."""

    def test_raises_when_cancelling(self, plugin):
        plugin._sync_service._sync_state = SyncState.CANCELLING
        with pytest.raises(asyncio.CancelledError):
            plugin._sync_service._fetcher._check_cancelling()

    def test_noop_when_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._fetcher._check_cancelling()  # should not raise

    def test_noop_when_idle(self, plugin):
        plugin._sync_service._fetcher._check_cancelling()  # should not raise


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
        result = plugin._sync_service._fetcher._build_shortcuts_data(roms)
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
        result = plugin._sync_service._fetcher._build_shortcuts_data([])
        assert result == []

    def test_missing_optional_fields(self, plugin):
        roms = [{"id": 5, "name": "Minimal"}]
        result = plugin._sync_service._fetcher._build_shortcuts_data(roms)
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

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
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

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_for_non_list_response(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"error": "bad response"})
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
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
        result = plugin._sync_service._fetcher._reconstruct_platform_from_registry(
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
        result = plugin._sync_service._fetcher._reconstruct_platform_from_registry(
            plugin._state["shortcut_registry"], "N64", "n64"
        )
        assert result == []

    def test_empty_registry(self, plugin):
        result = plugin._sync_service._fetcher._reconstruct_platform_from_registry({}, "N64", "n64")
        assert result == []


class TestTryIncrementalSkip:
    """Tests for _try_incremental_skip() — lines 431-465."""

    @pytest.mark.asyncio
    async def test_skips_unchanged_platform(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"total": 0})
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
            "2": {"name": "Game B", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 2}
        all_roms = []

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
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
        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, {}, None, "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False

    @pytest.mark.asyncio
    async def test_no_skip_when_registry_empty(self, plugin):
        from unittest.mock import MagicMock

        mock_loop = MagicMock()
        plugin._sync_service._loop = mock_loop

        platform = {"id": 1, "rom_count": 5}
        all_roms = []

        # registry has no entries for this platform
        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
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

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
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

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
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

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
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
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
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
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 51

    @pytest.mark.asyncio
    async def test_handles_api_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Server error"))
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 0  # gracefully handles error

    @pytest.mark.asyncio
    async def test_cancelling_during_fetch(self, plugin):
        from unittest.mock import AsyncMock

        plugin._sync_service._sync_state = SyncState.CANCELLING
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)


class TestFetchCollectionRoms:
    """Tests for LibraryService._fetch_collection_roms()."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_collections_enabled(self, plugin):
        """When no collections are enabled, returns empty results immediately."""
        plugin._sync_service._settings["enabled_collections"] = {"1": False, "2": False}

        roms, memberships = await plugin._sync_service._fetcher._fetch_collection_roms(set())

        assert roms == []
        assert memberships == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_enabled_collections_absent(self, plugin):
        """When enabled_collections key is absent, returns empty results."""
        plugin._sync_service._settings.pop("enabled_collections", None)

        roms, memberships = await plugin._sync_service._fetcher._fetch_collection_roms(set())

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

        roms, memberships = await plugin._sync_service._fetcher._fetch_collection_roms({10})

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

        roms, memberships = await plugin._sync_service._fetcher._fetch_collection_roms(set())

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

        _roms, memberships = await plugin._sync_service._fetcher._fetch_collection_roms(set())

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

        roms, _ = await plugin._sync_service._fetcher._fetch_collection_roms(set())

        assert "files" not in roms[0]

    @pytest.mark.asyncio
    async def test_handles_api_error_gracefully(self, plugin):
        """Generic API errors are caught and empty results are returned."""
        plugin._sync_service._settings["enabled_collections"] = {"1": True}
        plugin._sync_service._loop = _make_loop_raising(Exception("Connection refused"))

        roms, memberships = await plugin._sync_service._fetcher._fetch_collection_roms(set())

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

        roms, memberships = await plugin._sync_service._fetcher._fetch_collection_roms(set())

        # The third call should use list_roms_by_virtual_collection
        third_fn = captured_calls[2][0]
        assert third_fn == plugin._sync_service._romm_api.list_roms_by_virtual_collection
        assert "Mario" in memberships
        assert roms[0]["id"] == 42


# ---------------------------------------------------------------------------
# TestCollectionSyncEdgeCases
# ---------------------------------------------------------------------------
