"""Tests for LibraryService collection list caching (PR 2).

These tests exercise _get_collections_cached(), _invalidate_collections_cache(),
and their integration with get_collections() / set_all_collections_sync() /
_fetch_collection_roms().

The tests create a minimal LibraryService directly (no Plugin fixture) so
they run on all platforms — including Windows where fcntl is unavailable.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from lib.errors import RommUnsupportedError
from services.library import LibraryService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_library(**overrides) -> LibraryService:
    """Build a LibraryService with sensible mock defaults.

    Override any constructor kwarg by passing it as a keyword argument.
    """
    defaults = dict(
        romm_api=MagicMock(),
        steam_config=MagicMock(),
        state={"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}},
        settings={"enabled_platforms": {}},
        metadata_cache={},
        loop=asyncio.get_event_loop(),
        logger=MagicMock(),
        plugin_dir="/tmp/plugin",
        emit=AsyncMock(),
        save_state=MagicMock(),
        save_settings_to_disk=MagicMock(),
        log_debug=MagicMock(),
    )
    defaults.update(overrides)
    return LibraryService(**defaults)


def _mock_loop_sequential(*return_values):
    """Mock loop whose run_in_executor returns values in order."""
    loop = MagicMock()
    if len(return_values) == 1:
        loop.run_in_executor = AsyncMock(return_value=return_values[0])
    else:
        loop.run_in_executor = AsyncMock(side_effect=list(return_values))
    return loop


# ---------------------------------------------------------------------------
# TestGetCollectionsCached
# ---------------------------------------------------------------------------


class TestGetCollectionsCached:
    """Tests for _get_collections_cached() TTL behaviour."""

    @pytest.mark.asyncio
    async def test_first_call_fetches_from_api(self):
        """First call hits the API and returns both lists."""
        user = [{"id": 1, "name": "Faves", "rom_count": 3}]
        franchise = [{"id": 101, "name": "Mario", "rom_count": 5}]
        svc = _make_library()
        svc._loop = _mock_loop_sequential(user, franchise)

        result = await svc._get_collections_cached()

        assert result == (user, franchise)
        assert svc._loop.run_in_executor.await_count == 2

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self):
        """Within TTL, a second call returns cached data with zero API calls."""
        user = [{"id": 1, "name": "Faves", "rom_count": 3}]
        franchise = [{"id": 101, "name": "Mario", "rom_count": 5}]
        svc = _make_library()
        svc._loop = _mock_loop_sequential(user, franchise)

        first = await svc._get_collections_cached()
        # Reset mock to prove second call doesn't touch the API
        svc._loop.run_in_executor.reset_mock()
        second = await svc._get_collections_cached()

        assert first == second
        svc._loop.run_in_executor.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self):
        """After TTL expires, the next call re-fetches from the API."""
        user1 = [{"id": 1, "name": "Faves", "rom_count": 3}]
        franchise1 = [{"id": 101, "name": "Mario", "rom_count": 5}]
        user2 = [{"id": 1, "name": "Faves", "rom_count": 4}]  # updated count
        franchise2 = [{"id": 101, "name": "Mario", "rom_count": 6}]

        svc = _make_library()
        svc._COLLECTIONS_CACHE_TTL = 0.01  # 10ms for fast test
        svc._loop = _mock_loop_sequential(user1, franchise1, user2, franchise2)

        first = await svc._get_collections_cached()
        assert first == (user1, franchise1)

        # Wait for TTL to expire (generous margin for Windows timer resolution)
        await asyncio.sleep(0.1)

        second = await svc._get_collections_cached()
        assert second == (user2, franchise2)
        assert svc._loop.run_in_executor.await_count == 4  # 2 + 2

    @pytest.mark.asyncio
    async def test_franchise_failure_returns_empty_list(self):
        """If franchise fetch fails, user collections still returned."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2}]

        svc = _make_library()
        loop = MagicMock()
        loop.run_in_executor = AsyncMock(
            side_effect=[user, RuntimeError("franchise endpoint down")]
        )
        svc._loop = loop

        result = await svc._get_collections_cached()

        assert result[0] == user
        assert result[1] == []  # Franchise failure handled gracefully

    @pytest.mark.asyncio
    async def test_unsupported_error_propagated(self):
        """RommUnsupportedError from list_collections is raised, not cached."""
        svc = _make_library()
        loop = MagicMock()
        loop.run_in_executor = AsyncMock(side_effect=RommUnsupportedError("too old", "4.7.0"))
        svc._loop = loop

        with pytest.raises(RommUnsupportedError):
            await svc._get_collections_cached()

        # Nothing cached on error
        assert svc._collections_cache is None


# ---------------------------------------------------------------------------
# TestInvalidateCollectionsCache
# ---------------------------------------------------------------------------


class TestInvalidateCollectionsCache:
    """Tests for _invalidate_collections_cache()."""

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache(self):
        """After invalidation, next call re-fetches."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2}]
        franchise = []
        svc = _make_library()
        svc._loop = _mock_loop_sequential(user, franchise, user, franchise)

        await svc._get_collections_cached()
        assert svc._collections_cache is not None

        svc._invalidate_collections_cache()
        assert svc._collections_cache is None

        # Next call must fetch again
        await svc._get_collections_cached()
        assert svc._loop.run_in_executor.await_count == 4  # 2 initial + 2 after invalidate

    def test_invalidate_on_empty_cache_is_safe(self):
        """Invalidating when cache is already empty doesn't raise."""
        svc = _make_library()
        svc._invalidate_collections_cache()  # Should not raise
        assert svc._collections_cache is None


# ---------------------------------------------------------------------------
# TestCacheIntegration
# ---------------------------------------------------------------------------


class TestCacheIntegration:
    """Verify get_collections and set_all_collections_sync use the cache."""

    @pytest.mark.asyncio
    async def test_get_collections_uses_cache(self):
        """get_collections() calls _get_collections_cached internally."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]
        franchise = [{"id": 101, "name": "Mario", "rom_count": 5, "is_favorite": False}]
        svc = _make_library()
        svc._loop = _mock_loop_sequential(user, franchise)

        result1 = await svc.get_collections()
        assert result1["success"] is True
        assert len(result1["collections"]) == 2

        # Second call should use cache (no new API calls)
        svc._loop.run_in_executor.reset_mock()
        result2 = await svc.get_collections()
        assert result2["success"] is True
        svc._loop.run_in_executor.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_all_collections_sync_uses_cache(self):
        """set_all_collections_sync() reuses cached collection list."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2, "is_favorite": False}]
        franchise = [{"id": 101, "name": "Mario", "rom_count": 5, "is_favorite": False}]
        svc = _make_library()
        svc._loop = _mock_loop_sequential(user, franchise)

        # Prime the cache
        await svc._get_collections_cached()
        svc._loop.run_in_executor.reset_mock()

        # set_all should use cache (zero API calls)
        result = await svc.set_all_collections_sync(True)
        assert result["success"] is True
        svc._loop.run_in_executor.assert_not_awaited()

        # Verify both collections got enabled
        ec = svc._settings["enabled_collections"]
        assert ec["1"] is True
        assert ec["101"] is True

    @pytest.mark.asyncio
    async def test_clear_sync_cache_invalidates_collection_cache(self):
        """clear_sync_cache() also invalidates the collection list cache."""
        user = [{"id": 1, "name": "RPGs", "rom_count": 2}]
        franchise = []
        svc = _make_library()
        svc._loop = _mock_loop_sequential(user, franchise)

        await svc._get_collections_cached()
        assert svc._collections_cache is not None

        svc.clear_sync_cache()
        assert svc._collections_cache is None


# ---------------------------------------------------------------------------
# TestParallelFetch
# ---------------------------------------------------------------------------


class TestParallelFetch:
    """Verify asyncio.gather parallelism of user + franchise fetches."""

    @pytest.mark.asyncio
    async def test_both_endpoints_called(self):
        """Both list_collections and list_virtual_collections are invoked."""
        svc = _make_library()
        svc._romm_api.list_collections.return_value = [{"id": 1, "name": "A"}]
        svc._romm_api.list_virtual_collections.return_value = [{"id": 2, "name": "B"}]

        # Use a real event loop (not mock) to test gather
        result = await svc._get_collections_cached()

        svc._romm_api.list_collections.assert_called_once()
        svc._romm_api.list_virtual_collections.assert_called_once_with("franchise")
        assert len(result[0]) == 1
        assert len(result[1]) == 1
