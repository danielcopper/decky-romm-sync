"""Tests for PR 5 — Concurrent Artwork Downloads.

Validates that ArtworkService.download_artwork uses bounded concurrency
(``asyncio.Semaphore``), properly handles cache hits, missing cover URLs,
cancellation, and download failures.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys  # noqa: E401,E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "py_modules"))

from services.artwork import ArtworkService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artwork(**overrides: Any) -> ArtworkService:
    """Create an ArtworkService with sensible mock defaults."""
    loop = asyncio.get_event_loop()
    defaults: dict[str, Any] = {
        "romm_api": MagicMock(),
        "steam_config": MagicMock(),
        "state": {"shortcut_registry": {}},
        "loop": loop,
        "logger": MagicMock(),
        "emit": AsyncMock(),
        "sync_state_ref": MagicMock(return_value=False),
    }
    defaults.update(overrides)
    svc = ArtworkService(**defaults)
    # Default: grid_dir returns a valid path
    svc._steam_config.grid_dir.return_value = "/tmp/grid"
    return svc


def _rom(rid: int, cover: str = "http://example.com/cover.png", name: str = "") -> dict:
    """Build a minimal ROM dict for artwork tests."""
    d: dict[str, Any] = {"id": rid, "name": name or f"ROM_{rid}"}
    if cover:
        d["path_cover_large"] = cover
    return d


# ---------------------------------------------------------------------------
# Basic functionality
# ---------------------------------------------------------------------------


class TestArtworkDownload:
    """Core download_artwork tests."""

    @pytest.mark.asyncio
    async def test_downloads_cover_to_staging(self):
        """Each ROM gets its cover downloaded to romm_{id}_cover.png."""
        svc = _make_artwork()
        svc._loop.run_in_executor = AsyncMock(return_value=None)
        emit = AsyncMock()

        roms = [_rom(1), _rom(2)]
        result = await svc.download_artwork(roms, emit_progress=emit, is_cancelling=lambda: False)

        assert 1 in result
        assert 2 in result
        assert result[1].endswith("romm_1_cover.png")
        assert result[2].endswith("romm_2_cover.png")

    @pytest.mark.asyncio
    async def test_no_cover_url_skipped(self):
        """ROMs without cover URLs are skipped."""
        svc = _make_artwork()
        svc._loop.run_in_executor = AsyncMock(return_value=None)
        emit = AsyncMock()

        roms = [{"id": 1, "name": "No Cover"}]  # No path_cover_large or path_cover_small
        result = await svc.download_artwork(roms, emit_progress=emit, is_cancelling=lambda: False)

        assert 1 not in result
        assert svc._loop.run_in_executor.await_count == 0

    @pytest.mark.asyncio
    async def test_existing_cover_reused(self):
        """When existing_cover_path returns a path, no download happens."""
        svc = _make_artwork()
        svc._loop.run_in_executor = AsyncMock(return_value=None)
        svc.existing_cover_path = MagicMock(return_value="/tmp/grid/existing.png")
        emit = AsyncMock()

        roms = [_rom(1)]
        result = await svc.download_artwork(roms, emit_progress=emit, is_cancelling=lambda: False)

        assert result[1] == "/tmp/grid/existing.png"
        assert svc._loop.run_in_executor.await_count == 0

    @pytest.mark.asyncio
    async def test_no_grid_dir_returns_empty(self):
        """If grid_dir() returns None, return empty dict immediately."""
        svc = _make_artwork()
        svc._steam_config.grid_dir.return_value = None
        emit = AsyncMock()

        result = await svc.download_artwork([_rom(1)], emit_progress=emit, is_cancelling=lambda: False)

        assert result == {}

    @pytest.mark.asyncio
    async def test_download_failure_logged_not_raised(self):
        """Download errors are logged, not raised — other ROMs still succeed."""
        svc = _make_artwork()
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Network error")
            return None

        svc._loop.run_in_executor = side_effect
        emit = AsyncMock()

        roms = [_rom(1, name="FailROM"), _rom(2)]
        result = await svc.download_artwork(roms, emit_progress=emit, is_cancelling=lambda: False)

        assert 1 not in result  # Failed
        assert 2 in result  # Succeeded

    @pytest.mark.asyncio
    async def test_progress_reported_for_each_rom(self):
        """emit_progress called for every ROM (including skips/failures)."""
        svc = _make_artwork()
        svc._loop.run_in_executor = AsyncMock(return_value=None)
        emit = AsyncMock()

        roms = [_rom(1), _rom(2), _rom(3)]
        await svc.download_artwork(roms, emit_progress=emit, is_cancelling=lambda: False)

        # Each ROM triggers a progress emission
        assert emit.await_count == 3


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestArtworkCancellation:
    """Verify cancellation is respected during concurrent downloads."""

    @pytest.mark.asyncio
    async def test_cancellation_stops_downloads(self):
        """When is_cancelling returns True, tasks skip downloading."""
        svc = _make_artwork()
        download_count = 0

        async def counting_executor(*args, **kwargs):
            nonlocal download_count
            download_count += 1
            return None

        svc._loop.run_in_executor = counting_executor
        emit = AsyncMock()

        # Start cancelling immediately
        roms = [_rom(i) for i in range(10)]
        result = await svc.download_artwork(roms, emit_progress=emit, is_cancelling=lambda: True)

        # All tasks should check cancellation before downloading
        assert download_count == 0


# ---------------------------------------------------------------------------
# Concurrency behaviour
# ---------------------------------------------------------------------------


class TestArtworkConcurrency:
    """Verify semaphore bounds concurrent artwork downloads."""

    @pytest.mark.asyncio
    async def test_default_concurrency_is_6(self):
        svc = _make_artwork()
        assert svc._ARTWORK_CONCURRENCY == 6

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_downloads(self):
        """With concurrency=2, at most 2 downloads should run simultaneously."""
        svc = _make_artwork()
        svc._ARTWORK_CONCURRENCY = 2
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def tracking_executor(executor, fn, *args):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.01)  # simulate I/O
            async with lock:
                current_concurrent -= 1
            return None

        svc._loop.run_in_executor = tracking_executor
        emit = AsyncMock()

        roms = [_rom(i) for i in range(8)]
        await svc.download_artwork(roms, emit_progress=emit, is_cancelling=lambda: False)

        assert max_concurrent <= 2, f"Max concurrent was {max_concurrent}, expected ≤ 2"

    @pytest.mark.asyncio
    async def test_all_roms_processed_concurrently(self):
        """All ROMs are processed even with concurrency limit."""
        svc = _make_artwork()
        svc._ARTWORK_CONCURRENCY = 3
        svc._loop.run_in_executor = AsyncMock(return_value=None)
        emit = AsyncMock()

        roms = [_rom(i) for i in range(10)]
        result = await svc.download_artwork(roms, emit_progress=emit, is_cancelling=lambda: False)

        assert len(result) == 10
        assert svc._loop.run_in_executor.await_count == 10
