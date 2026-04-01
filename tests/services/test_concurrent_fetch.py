"""Tests for PR 4 — Concurrent HTTP Fetching.

Validates that the Phase 2 platform ROM fetch uses bounded concurrency
(``AdaptiveSemaphore``), the isolated fetch helpers work correctly in
isolation, and results are correctly flattened into ``all_roms``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — same pattern as other service tests
# ---------------------------------------------------------------------------

import sys, os  # noqa: E401,E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "py_modules"))

from services.library import LibraryService  # noqa: E402
from domain.sync_state import SyncState  # noqa: E402
from lib.perf import AdaptiveSemaphore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_library(**overrides: Any) -> LibraryService:
    """Create a LibraryService with sensible mock defaults."""
    loop = asyncio.get_event_loop()
    defaults: dict[str, Any] = {
        "romm_api": MagicMock(),
        "steam_config": MagicMock(),
        "state": {"shortcut_registry": {}, "installed_roms": {}},
        "settings": {"enabled_platforms": {}, "enabled_collections": {}},
        "metadata_cache": {},
        "loop": loop,
        "logger": MagicMock(),
        "plugin_dir": "/tmp/test_plugin",
        "emit": AsyncMock(),
        "save_state": MagicMock(),
        "save_settings_to_disk": MagicMock(),
        "log_debug": MagicMock(),
    }
    defaults.update(overrides)
    return LibraryService(**defaults)


def _rom(rid: int, name: str = "", platform: str = "Test") -> dict:
    """Build a minimal ROM dict."""
    return {"id": rid, "name": name or f"ROM_{rid}", "platform_name": platform, "platform_slug": "test"}


# ---------------------------------------------------------------------------
# _try_incremental_skip_isolated
# ---------------------------------------------------------------------------


class TestIncrementalSkipIsolated:
    """Tests for _try_incremental_skip_isolated."""

    @pytest.mark.asyncio
    async def test_no_last_sync_returns_not_skipped(self):
        lib = _make_library()
        skipped, roms = await lib._try_incremental_skip_isolated(
            {"id": 1, "name": "SNES", "slug": "snes", "rom_count": 10},
            registry={},
            last_sync=None,
            platform_name="SNES",
            platform_slug="snes",
        )
        assert skipped is False
        assert roms == []

    @pytest.mark.asyncio
    async def test_no_registry_entries_returns_not_skipped(self):
        lib = _make_library()
        skipped, roms = await lib._try_incremental_skip_isolated(
            {"id": 1, "name": "SNES", "slug": "snes", "rom_count": 10},
            registry={},
            last_sync="2025-01-01T00:00:00Z",
            platform_name="SNES",
            platform_slug="snes",
        )
        assert skipped is False
        assert roms == []

    @pytest.mark.asyncio
    async def test_skip_when_no_updates_and_counts_match(self):
        """Platform unchanged: delta_resp total=0, rom_count matches registry count."""
        lib = _make_library()
        # The API returns zero updates
        lib._loop.run_in_executor = AsyncMock(return_value={"total": 0})

        registry = {
            "100": {"platform_name": "SNES", "name": "Game A", "platform_slug": "snes"},
            "101": {"platform_name": "SNES", "name": "Game B", "platform_slug": "snes"},
        }

        skipped, roms = await lib._try_incremental_skip_isolated(
            {"id": 1, "name": "SNES", "slug": "snes", "rom_count": 2},
            registry=registry,
            last_sync="2025-01-01T00:00:00Z",
            platform_name="SNES",
            platform_slug="snes",
        )
        assert skipped is True
        assert len(roms) == 2
        # Reconstructed ROMs should have correct metadata
        assert all(r["platform_name"] == "SNES" for r in roms)

    @pytest.mark.asyncio
    async def test_not_skipped_when_updates_exist(self):
        """Delta API reports updated ROMs — must do full fetch."""
        lib = _make_library()
        lib._loop.run_in_executor = AsyncMock(return_value={"total": 3})

        registry = {
            "100": {"platform_name": "SNES", "name": "Game A", "platform_slug": "snes"},
        }

        skipped, roms = await lib._try_incremental_skip_isolated(
            {"id": 1, "name": "SNES", "slug": "snes", "rom_count": 1},
            registry=registry,
            last_sync="2025-01-01T00:00:00Z",
            platform_name="SNES",
            platform_slug="snes",
        )
        assert skipped is False
        assert roms == []


# ---------------------------------------------------------------------------
# _full_fetch_platform_roms_isolated
# ---------------------------------------------------------------------------


class TestFullFetchIsolated:
    """Tests for _full_fetch_platform_roms_isolated pagination."""

    @pytest.mark.asyncio
    async def test_single_page(self):
        lib = _make_library()
        roms = [_rom(i) for i in range(5)]
        lib._loop.run_in_executor = AsyncMock(return_value={"items": roms})

        progress = {"done": 0, "roms_found": 0, "total": 1}
        result = await lib._full_fetch_platform_roms_isolated(1, "SNES", "snes", progress)

        assert len(result) == 5
        assert progress["roms_found"] == 5

    @pytest.mark.asyncio
    async def test_multi_page_pagination(self):
        """Multiple pages: first returns PAGE_SIZE items, second returns < PAGE_SIZE → stop."""
        lib = _make_library()
        page_size = lib._PAGE_SIZE  # 250
        page1 = [_rom(i) for i in range(page_size)]
        page2 = [_rom(i + page_size) for i in range(10)]
        lib._loop.run_in_executor = AsyncMock(side_effect=[{"items": page1}, {"items": page2}])

        progress = {"done": 0, "roms_found": 0, "total": 1}
        result = await lib._full_fetch_platform_roms_isolated(1, "SNES", "snes", progress)

        assert len(result) == page_size + 10
        assert progress["roms_found"] == page_size + 10
        assert lib._loop.run_in_executor.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_platform(self):
        lib = _make_library()
        lib._loop.run_in_executor = AsyncMock(return_value={"items": []})

        progress = {"done": 0, "roms_found": 0, "total": 1}
        result = await lib._full_fetch_platform_roms_isolated(1, "SNES", "snes", progress)

        assert result == []
        assert progress["roms_found"] == 0

    @pytest.mark.asyncio
    async def test_platform_name_and_slug_injected(self):
        """Each ROM gets platform_name and platform_slug set."""
        lib = _make_library()
        lib._loop.run_in_executor = AsyncMock(
            return_value={"items": [{"id": 1, "name": "Mario"}]}
        )

        progress = {"done": 0, "roms_found": 0, "total": 1}
        result = await lib._full_fetch_platform_roms_isolated(1, "SNES", "snes", progress)

        assert result[0]["platform_name"] == "SNES"
        assert result[0]["platform_slug"] == "snes"

    @pytest.mark.asyncio
    async def test_files_key_stripped(self):
        """ROM dicts should have 'files' key removed to save memory."""
        lib = _make_library()
        lib._loop.run_in_executor = AsyncMock(
            return_value={"items": [{"id": 1, "name": "Mario", "files": [{"name": "rom.zip"}]}]}
        )

        progress = {"done": 0, "roms_found": 0, "total": 1}
        result = await lib._full_fetch_platform_roms_isolated(1, "SNES", "snes", progress)

        assert "files" not in result[0]


# ---------------------------------------------------------------------------
# _fetch_one_platform
# ---------------------------------------------------------------------------


class TestFetchOnePlatform:
    """Tests for the semaphore-bounded single-platform dispatcher."""

    @pytest.mark.asyncio
    async def test_incremental_skip_returns_cached_roms(self):
        """When skip succeeds, returns reconstructed ROMs without full fetch."""
        lib = _make_library()
        # Mock: delta check returns zero updates
        lib._loop.run_in_executor = AsyncMock(return_value={"total": 0})

        registry = {
            "100": {"platform_name": "SNES", "name": "Game A", "platform_slug": "snes"},
        }
        platform = {"id": 1, "name": "SNES", "slug": "snes", "rom_count": 1}
        sem = AdaptiveSemaphore(initial=4, min_concurrent=1, max_concurrent=8)
        progress = {"done": 0, "roms_found": 0, "total": 1}

        roms = await lib._fetch_one_platform(
            platform, registry, "2025-01-01T00:00:00Z", sem, progress
        )

        assert len(roms) == 1
        assert progress["done"] == 1

    @pytest.mark.asyncio
    async def test_full_fetch_when_not_skipped(self):
        """When skip fails, does full paginated fetch."""
        lib = _make_library()
        # First call: delta check (returns updates exist)
        # Second call: list_roms page 1
        lib._loop.run_in_executor = AsyncMock(
            side_effect=[
                {"total": 5},  # delta check: changes found
                {"items": [_rom(1), _rom(2)]},  # full fetch: page 1 (<50 items → done)
            ]
        )

        registry = {
            "100": {"platform_name": "SNES", "name": "Game A", "platform_slug": "snes"},
        }
        platform = {"id": 1, "name": "SNES", "slug": "snes", "rom_count": 1}
        sem = AdaptiveSemaphore(initial=4, min_concurrent=1, max_concurrent=8)
        progress = {"done": 0, "roms_found": 0, "total": 1}

        roms = await lib._fetch_one_platform(
            platform, registry, "2025-01-01T00:00:00Z", sem, progress
        )

        assert len(roms) == 2
        assert progress["done"] == 1
        assert progress["roms_found"] == 2

    @pytest.mark.asyncio
    async def test_no_last_sync_always_full_fetch(self):
        """No previous sync → skip is impossible → full fetch."""
        lib = _make_library()
        lib._loop.run_in_executor = AsyncMock(
            return_value={"items": [_rom(1)]}
        )

        platform = {"id": 1, "name": "NES", "slug": "nes", "rom_count": 100}
        sem = AdaptiveSemaphore(initial=4, min_concurrent=1, max_concurrent=8)
        progress = {"done": 0, "roms_found": 0, "total": 1}

        roms = await lib._fetch_one_platform(platform, {}, None, sem, progress)

        assert len(roms) == 1
        assert progress["roms_found"] == 1


# ---------------------------------------------------------------------------
# Concurrency behaviour
# ---------------------------------------------------------------------------


class TestConcurrencyBehaviour:
    """Verify semaphore bounds concurrent platform fetches."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """With concurrency=2 and 4 platforms, at most 2 should run simultaneously."""
        lib = _make_library()
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        original_fetch = lib._full_fetch_platform_roms_isolated

        async def tracking_fetch(pid, pname, pslug, progress):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                max_concurrent = max(max_concurrent, current_concurrent)
            # Yield control to let other tasks run
            await asyncio.sleep(0.01)
            async with lock:
                current_concurrent -= 1
            progress["roms_found"] += 2
            return [_rom(pid * 100 + 1), _rom(pid * 100 + 2)]

        lib._full_fetch_platform_roms_isolated = tracking_fetch
        # Force full fetch (no skip) by not providing last_sync
        lib._try_incremental_skip_isolated = AsyncMock(return_value=(False, []))

        platforms = [
            {"id": i, "name": f"P{i}", "slug": f"p{i}", "rom_count": 10}
            for i in range(4)
        ]

        sem = AdaptiveSemaphore(initial=2, min_concurrent=2, max_concurrent=2)
        progress = {"done": 0, "roms_found": 0, "total": 4}

        tasks = [
            lib._fetch_one_platform(p, {}, None, sem, progress)
            for p in platforms
        ]
        results = await asyncio.gather(*tasks)

        all_roms = [rom for platform_roms in results for rom in platform_roms]

        assert max_concurrent <= 2, f"Max concurrency was {max_concurrent}, expected ≤ 2"
        assert len(all_roms) == 8  # 4 platforms × 2 roms each
        assert progress["done"] == 4

    @pytest.mark.asyncio
    async def test_results_correctly_flattened(self):
        """Concurrent results from gather are properly flattened into all_roms."""
        lib = _make_library()
        # Each platform returns its own rom list
        call_count = 0

        async def sequential_fetch(pid, pname, pslug, progress):
            nonlocal call_count
            call_count += 1
            roms = [_rom(pid * 100 + i, name=f"{pname}_ROM_{i}") for i in range(3)]
            progress["roms_found"] += len(roms)
            return roms

        lib._full_fetch_platform_roms_isolated = sequential_fetch
        lib._try_incremental_skip_isolated = AsyncMock(return_value=(False, []))

        platforms = [
            {"id": 1, "name": "SNES", "slug": "snes", "rom_count": 3},
            {"id": 2, "name": "NES", "slug": "nes", "rom_count": 3},
            {"id": 3, "name": "GBA", "slug": "gba", "rom_count": 3},
        ]

        sem = AdaptiveSemaphore(initial=4, min_concurrent=1, max_concurrent=8)
        progress = {"done": 0, "roms_found": 0, "total": 3}

        tasks = [
            lib._fetch_one_platform(p, {}, None, sem, progress)
            for p in platforms
        ]
        results = await asyncio.gather(*tasks)
        all_roms = [rom for platform_roms in results for rom in platform_roms]

        assert len(all_roms) == 9  # 3 platforms × 3 roms
        assert progress["roms_found"] == 9
        assert progress["done"] == 3
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        """If cancellation is requested, tasks raise CancelledError."""
        lib = _make_library()
        lib._sync_state = SyncState.CANCELLING  # Already cancelling

        platform = {"id": 1, "name": "SNES", "slug": "snes", "rom_count": 10}
        sem = AdaptiveSemaphore(initial=4, min_concurrent=1, max_concurrent=8)
        progress = {"done": 0, "roms_found": 0, "total": 1}

        with pytest.raises(asyncio.CancelledError):
            await lib._fetch_one_platform(platform, {}, None, sem, progress)


# ---------------------------------------------------------------------------
# FETCH_CONCURRENCY configuration
# ---------------------------------------------------------------------------


class TestFetchConcurrencyConfig:
    """Verify the _FETCH_CONCURRENCY attribute is present and configurable."""

    def test_default_concurrency_is_4(self):
        lib = _make_library()
        assert lib._FETCH_CONCURRENCY == 4

    def test_concurrency_can_be_changed(self):
        lib = _make_library()
        lib._FETCH_CONCURRENCY = 8
        assert lib._FETCH_CONCURRENCY == 8
