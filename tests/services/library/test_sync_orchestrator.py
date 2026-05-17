"""Tests for SyncOrchestrator — preview/apply/full-sync lifecycle and safety heartbeat."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.persistence import (
    PersistenceAdapter,
)
from domain.preview_delta import PreviewDelta
from domain.sync_state import SyncState
from domain.work_unit import WorkUnit
from services.library._state import PrefetchedUnit

# conftest.py patches decky before this import


def _prefetch_result(
    *,
    prefetched=None,
    all_roms=None,
    shortcuts_data=None,
    collection_memberships=None,
    platform_rom_ids=None,
):
    """Build the 5-tuple returned by ``LibraryFetcher.prefetch_all_units``."""
    return (
        prefetched or [],
        all_roms or [],
        shortcuts_data or [],
        collection_memberships or {},
        platform_rom_ids if platform_rom_ids is not None else set(),
    )


def _platform_prefetched(name="N64", slug="n64", roms=None, skipped=True):
    """Helper to build a single platform PrefetchedUnit for preview tests."""
    roms = roms or []
    return PrefetchedUnit(
        unit=WorkUnit(type="platform", id=1, name=name, slug=slug, rom_count=len(roms)),
        roms=roms,
        skipped=skipped,
    )


class TestShortcutDataFormat:
    """Validate the shortcut data format produced by the backend.

    The backend prepares shortcut data that the frontend uses to create
    Steam shortcuts. These tests ensure the data is well-formed.
    """

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


class TestSyncPreview:
    """Tests for sync_preview()."""

    @pytest.mark.asyncio
    async def test_returns_correct_summary(self, plugin):

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()

        # Mock prefetch_all_units to return known data
        all_roms = [{"id": 1}, {"id": 2}, {"id": 3}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
            {"rom_id": 2, "name": "Game B", "platform_name": "N64", "platform_slug": "n64", "fs_name": "b.z64"},
            {"rom_id": 3, "name": "Game C", "platform_name": "N64", "platform_slug": "n64", "fs_name": "c.z64"},
        ]
        prefetched = [_platform_prefetched(name="N64", slug="n64", roms=all_roms)]
        plugin._sync_service._fetcher.prefetch_all_units = AsyncMock(
            return_value=_prefetch_result(
                prefetched=prefetched,
                all_roms=all_roms,
                shortcuts_data=shortcuts_data,
                platform_rom_ids={1, 2, 3},
            )
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

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

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()

        all_roms = [{"id": 1}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
        ]
        prefetched = [_platform_prefetched(name="N64", slug="n64", roms=all_roms)]
        plugin._sync_service._fetcher.prefetch_all_units = AsyncMock(
            return_value=_prefetch_result(
                prefetched=prefetched,
                all_roms=all_roms,
                shortcuts_data=shortcuts_data,
                platform_rom_ids={1},
            )
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        result = await plugin.sync_preview()
        assert plugin._sync_service._pending_delta is not None
        assert plugin._sync_service._pending_delta.preview_id == result["preview_id"]
        assert plugin._sync_service._pending_delta.created_at == plugin._sync_service._clock.time()
        assert plugin._sync_service._pending_delta.platforms_count == 1
        assert plugin._sync_service._pending_delta.total_roms == 1
        # The unified path caches the prefetched units so apply can
        # dispatch the per-unit pipeline without refetching.
        assert plugin._sync_service._box.pending_prefetched_units is not None
        assert len(plugin._sync_service._box.pending_prefetched_units) == 1

    @pytest.mark.asyncio
    async def test_returns_error_when_sync_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        result = await plugin.sync_preview()
        assert result["success"] is False
        assert "already in progress" in result["message"]

    @pytest.mark.asyncio
    async def test_resets_sync_running_on_completion(self, plugin):

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()

        all_roms = [{"id": 1}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
        ]
        prefetched = [_platform_prefetched(name="N64", slug="n64", roms=all_roms)]
        plugin._sync_service._fetcher.prefetch_all_units = AsyncMock(
            return_value=_prefetch_result(
                prefetched=prefetched,
                all_roms=all_roms,
                shortcuts_data=shortcuts_data,
                platform_rom_ids={1},
            )
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        await plugin.sync_preview()
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_new_preview_clears_prior_prefetch_cache(self, plugin):
        """A fresh ``sync_preview`` invalidates any cached prefetched units."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()

        # Seed a stale cache as if a previous preview ran.
        plugin._sync_service._box.pending_prefetched_units = [
            _platform_prefetched(name="OLD", slug="old", roms=[{"id": 99}])
        ]

        plugin._sync_service._fetcher.prefetch_all_units = AsyncMock(
            return_value=_prefetch_result(
                prefetched=[_platform_prefetched(name="N64", slug="n64", roms=[{"id": 1}])],
                all_roms=[{"id": 1}],
                shortcuts_data=[
                    {"rom_id": 1, "name": "A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
                ],
                platform_rom_ids={1},
            )
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        await plugin.sync_preview()

        # New preview replaced the stale cache.
        units = plugin._sync_service._box.pending_prefetched_units
        assert units is not None
        assert [u.unit.name for u in units] == ["N64"]


class TestSyncApplyDelta:
    """Tests for sync_apply_delta()."""

    def _setup_pending_delta(self, plugin, preview_id="test-preview-123", *, with_prefetch=True):
        """Helper to populate _pending_delta with valid data."""
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id=preview_id,
            created_at=plugin._sync_service._clock.time(),
            platforms_count=1,
            total_roms=3,
        )
        if with_prefetch:
            plugin._sync_service._box.pending_prefetched_units = [
                _platform_prefetched(
                    name="N64",
                    slug="n64",
                    roms=[
                        {"id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64"},
                        {"id": 2, "name": "New B", "platform_name": "N64", "platform_slug": "n64"},
                        {"id": 3, "name": "Game C", "platform_name": "N64", "platform_slug": "n64"},
                    ],
                    skipped=True,
                )
            ]

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
    async def test_rejects_when_preview_older_than_max_age(self, plugin):
        """Preview snapshots older than 30 minutes are stale.

        Regression for #345: sync_apply_delta previously only validated
        preview_id, so a user could leave the preview open for hours and
        apply a stale RomM snapshot — silent data corruption.
        """
        self._setup_pending_delta(plugin, "preview-abc")
        # Advance the clock past the 30-minute max age.
        plugin._sync_service._clock.advance(1801)

        result = await plugin.sync_apply_delta("preview-abc")

        assert result["success"] is False
        assert result["error_code"] == "stale_preview"
        assert "30 minutes" in result["message"]
        # Stale delta + prefetch cache are cleared so a repeat apply
        # can't pick them up.
        assert plugin._sync_service._pending_delta is None
        assert plugin._sync_service._box.pending_prefetched_units is None

    @pytest.mark.asyncio
    async def test_accepts_when_preview_just_under_max_age(self, plugin, tmp_path):
        """Snapshots within the TTL window apply normally."""

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin, "preview-xyz")
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._do_sync_per_unit = AsyncMock()
        # Just under the 30-minute window.
        plugin._sync_service._clock.advance(1799)

        result = await plugin.sync_apply_delta("preview-xyz")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_dispatches_per_unit_with_cached_queue(self, plugin, tmp_path):
        """Apply hands the prefetched cache to ``_do_sync_per_unit`` and clears the box cache."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin)
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        do_sync = AsyncMock()
        plugin._sync_service._orchestrator._do_sync_per_unit = do_sync

        result = await plugin.sync_apply_delta("test-preview-123")
        # Drain the create_task'd dispatch.
        for _ in range(3):
            await asyncio.sleep(0)

        assert result["success"] is True
        # Per-unit dispatch was kicked off with the prefetched queue.
        do_sync.assert_called_once()
        prefetched_arg = do_sync.call_args.kwargs.get("prefetched") or do_sync.call_args.args[0]
        assert prefetched_arg is not None
        assert [pu.unit.name for pu in prefetched_arg] == ["N64"]
        # Apply takes the cache out of the box so a concurrent error path can't double-consume.
        assert plugin._sync_service._box.pending_prefetched_units is None

    @pytest.mark.asyncio
    async def test_apply_persists_sync_stats(self, plugin, tmp_path):
        """Apply writes the preview's platform/rom counts into ``sync_stats`` so
        ``get_sync_stats`` and the shortcut-removal pass see the apply's counts."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin)
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._do_sync_per_unit = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")

        assert plugin._state["sync_stats"]["platforms"] == 1
        assert plugin._state["sync_stats"]["roms"] == 3

    @pytest.mark.asyncio
    async def test_clears_pending_delta(self, plugin, tmp_path):

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin)
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._do_sync_per_unit = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")
        assert plugin._sync_service._pending_delta is None

    @pytest.mark.asyncio
    async def test_apply_without_prefetch_logs_warning_and_falls_back(self, plugin, tmp_path):
        """Cache missing at apply (e.g. server restart) → log warning + dispatch without cache."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        self._setup_pending_delta(plugin, with_prefetch=False)
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        do_sync = AsyncMock()
        plugin._sync_service._orchestrator._do_sync_per_unit = do_sync

        result = await plugin.sync_apply_delta("test-preview-123")
        for _ in range(3):
            await asyncio.sleep(0)

        assert result["success"] is True
        do_sync.assert_called_once()
        if do_sync.call_args.kwargs:
            prefetched_arg = do_sync.call_args.kwargs.get("prefetched")
        else:
            prefetched_arg = do_sync.call_args.args[0] if do_sync.call_args.args else None
        # Fallback path: per-unit pipeline runs without cached units (does a fresh fetch).
        assert prefetched_arg is None


class TestSyncCancelPreview:
    """Tests for sync_cancel_preview()."""

    @pytest.mark.asyncio
    async def test_clears_pending_delta(self, plugin):
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id="some-id",
            created_at=plugin._sync_service._clock.time(),
            platforms_count=0,
            total_roms=0,
        )
        plugin._sync_service._box.pending_prefetched_units = [
            _platform_prefetched(name="X", slug="x", roms=[{"id": 1}])
        ]
        result = await plugin.sync_cancel_preview()
        assert plugin._sync_service._pending_delta is None
        # Cancelling the preview also evicts the prefetched-units cache.
        assert plugin._sync_service._box.pending_prefetched_units is None
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_returns_success(self, plugin):
        result = await plugin.sync_cancel_preview()
        assert result == {"success": True}


# ── Tests for uncovered helper methods in library_sync.py ──────────


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

    def test_start_sync_clears_prior_prefetch_cache(self, plugin):
        """Skip Preview ON path: any cache left from a prior preview is
        stale and must be cleared at entry so _do_sync_per_unit refetches.
        """
        plugin._sync_service._box.pending_prefetched_units = [
            _platform_prefetched(name="OLD", slug="old", roms=[{"id": 99}])
        ]

        result = plugin._sync_service.start_sync()

        assert result["success"] is True
        assert plugin._sync_service._box.pending_prefetched_units is None

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


class TestFinishSync:
    """Tests for _finish_sync() — lines 685-695."""

    @pytest.mark.asyncio
    async def test_sets_cancelled_state(self, plugin):
        import decky

        decky.emit.reset_mock()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_progress = {"running": True, "current": 5, "total": 10}

        await plugin._sync_service._orchestrator._finish_sync("Sync cancelled")

        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._sync_progress["running"] is False
        assert plugin._sync_service._sync_progress["phase"] == "cancelled"
        assert plugin._sync_service._sync_progress["message"] == "Sync cancelled"

    @pytest.mark.asyncio
    async def test_clears_current_sync_id(self, plugin):
        """_finish_sync invalidates _current_sync_id so generation-guarded
        background work (per-unit heartbeat) sees a stale generation."""
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_progress = {"running": True}
        plugin._sync_service._current_sync_id = "sync-abc"

        await plugin._sync_service._orchestrator._finish_sync("Sync cancelled")

        assert plugin._sync_service._current_sync_id is None


class TestSyncPreviewErrorHandling:
    """Tests for sync_preview error paths."""

    @pytest.mark.asyncio
    async def test_general_exception_returns_error(self, plugin):
        # Seed a cache so we can verify it's cleared on error.
        plugin._sync_service._box.pending_prefetched_units = [
            _platform_prefetched(name="STALE", slug="stale", roms=[{"id": 1}])
        ]
        plugin._sync_service._fetcher.prefetch_all_units = AsyncMock(side_effect=RuntimeError("Something broke"))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        result = await plugin._sync_service.sync_preview()
        assert result["success"] is False
        assert "error_code" in result
        assert plugin._sync_service._sync_state == SyncState.IDLE
        # Error path evicts the prefetch cache so apply can't pick up a half-built one.
        assert plugin._sync_service._box.pending_prefetched_units is None

    @pytest.mark.asyncio
    async def test_cancelled_error_reraises(self, plugin):

        import decky

        decky.emit.reset_mock()

        plugin._sync_service._box.pending_prefetched_units = [
            _platform_prefetched(name="STALE", slug="stale", roms=[{"id": 1}])
        ]
        plugin._sync_service._fetcher.prefetch_all_units = AsyncMock(side_effect=asyncio.CancelledError("cancelled"))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service.sync_preview()
        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._box.pending_prefetched_units is None


# ──────────────────────────────────────────────────────────────
# Per-unit pipeline tests
# ──────────────────────────────────────────────────────────────


class TestBuildWorkQueue:
    """Phase 0 of the per-unit pipeline: enumerate platforms + collections without fetching ROMs."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_nothing_enabled(self, plugin):
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {}
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=[])
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._fetcher._loop = mock_loop

        units = await plugin._sync_service._fetcher.build_work_queue()
        assert units == []

    @pytest.mark.asyncio
    async def test_includes_enabled_platforms(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        plugin.settings["enabled_platforms"] = {"1": True, "2": False, "3": True}
        plugin.settings["enabled_collections"] = {}
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64", "rom_count": 12},
                {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 99},
                {"id": 3, "name": "GBA", "slug": "gba", "rom_count": 5},
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._fetcher._loop = mock_loop

        units = await plugin._sync_service._fetcher.build_work_queue()
        assert [u.name for u in units] == ["N64", "GBA"]
        assert all(u.type == "platform" for u in units)
        assert units[0].rom_count == 12

    @pytest.mark.asyncio
    async def test_includes_enabled_collections_after_platforms(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        plugin.settings["enabled_platforms"] = {"1": True}
        plugin.settings["enabled_collections"] = {"7": True, "9": True}

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            side_effect=[
                # list_platforms
                [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 4}],
                # list_collections
                [{"id": 7, "name": "Favorites", "rom_count": 3, "is_favorite": True}],
                # list_virtual_collections("franchise")
                [{"id": 9, "name": "Metroid", "rom_count": 8, "is_virtual": True}],
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._fetcher._loop = mock_loop

        units = await plugin._sync_service._fetcher.build_work_queue()
        assert [(u.type, u.name) for u in units] == [
            ("platform", "N64"),
            ("collection", "Favorites"),
            ("collection", "Metroid"),
        ]
        assert units[2].is_virtual is True


class TestFetchPlatformUnit:
    """Per-unit platform ROM fetch with incremental-skip path."""

    @pytest.mark.asyncio
    async def test_full_fetch_when_no_registry(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value={"items": [{"id": 10, "name": "A"}, {"id": 11, "name": "B"}]}
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._fetcher._loop = mock_loop
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is False
        assert [r["id"] for r in roms] == [10, 11]
        assert roms[0]["platform_name"] == "N64"

    @pytest.mark.asyncio
    async def test_skips_when_registry_matches_count(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"
        plugin._state["shortcut_registry"] = {
            "10": {"name": "A", "fs_name": "a.z64", "platform_name": "N64", "platform_slug": "n64"},
            "11": {"name": "B", "fs_name": "b.z64", "platform_name": "N64", "platform_slug": "n64"},
        }
        mock_loop = MagicMock()
        # list_roms_updated_after returns zero updates
        mock_loop.run_in_executor = AsyncMock(return_value={"total": 0, "items": []})
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._fetcher._loop = mock_loop

        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is True
        assert {r["id"] for r in roms} == {10, 11}

    @pytest.mark.asyncio
    async def test_full_fetch_when_count_mismatch(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"
        plugin._state["shortcut_registry"] = {
            "10": {"name": "A", "platform_name": "N64"},
        }
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            side_effect=[
                # incremental check — zero updates BUT registry count doesn't match
                {"total": 0, "items": []},
                # list_roms paginated full fetch
                {"items": [{"id": 10, "name": "A"}, {"id": 11, "name": "B"}, {"id": 12, "name": "C"}]},
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._fetcher._loop = mock_loop

        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is False
        assert len(roms) == 3


class TestFetchCollectionUnit:
    """Per-unit collection ROM fetch with cross-unit deduplication."""

    @pytest.mark.asyncio
    async def test_returns_new_roms_and_member_ids(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=3, is_virtual=False)
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value={
                "items": [
                    {"id": 1, "platform_name": "N64"},
                    {"id": 2, "platform_name": "SNES"},
                    {"id": 3, "platform_name": "GBA"},
                ]
            }
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._fetcher._loop = mock_loop

        synced: set[int] = set()
        new_roms, ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)
        assert [r["id"] for r in new_roms] == [1, 2, 3]
        assert ids == [1, 2, 3]
        assert synced == {1, 2, 3}

    @pytest.mark.asyncio
    async def test_dedups_against_already_synced(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="collection", id="9", name="Metroid", slug="", rom_count=2, is_virtual=True)
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value={"items": [{"id": 1, "platform_name": "N64"}, {"id": 2, "platform_name": "SNES"}]}
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._fetcher._loop = mock_loop

        # rom_id=1 was already fetched via a platform unit
        synced: set[int] = {1}
        new_roms, ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)
        assert [r["id"] for r in new_roms] == [2]
        # All collection rom_ids reported back even if not in new_roms
        assert ids == [1, 2]


class TestDoSyncPerUnit:
    """End-to-end orchestration of the per-unit pipeline."""

    @pytest.mark.asyncio
    async def test_empty_queue_terminates_cleanly(self, plugin):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=[])
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        assert plugin._sync_service._sync_state == SyncState.IDLE
        # Sync plan was emitted with empty units
        plan_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_plan"]
        assert len(plan_events) == 1
        assert plan_events[0][0][1]["total_units"] == 0

    @pytest.mark.asyncio
    async def test_emits_sync_plan_with_queue(self, plugin):
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        queue = [
            WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2),
        ]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(return_value=([], True))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        # Pre-set the unit-done so _wait_for_unit_complete returns immediately
        async def fake_wait(_unit, event):
            event.set()
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        plan_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_plan"]
        assert len(plan_events) == 1
        payload = plan_events[0][0][1]
        assert payload["total_units"] == 1
        assert payload["units"][0]["name"] == "N64"

    @pytest.mark.asyncio
    async def test_processes_each_unit_in_order(self, plugin):
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        queue = [
            WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1),
            WorkUnit(type="platform", id=2, name="GBA", slug="gba", rom_count=1),
        ]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)

        # Each platform unit returns its own ROM list
        async def fake_fetch(unit):
            return [{"id": int(unit.id) * 10, "name": unit.name, "platform_name": unit.name}], True

        plugin._sync_service._fetcher.fetch_platform_unit = fake_fetch
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_unit, event):
            event.set()
            return {str(_unit.id * 10): 9000 + int(_unit.id)}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 2
        assert unit_events[0]["unit_name"] == "N64"
        assert unit_events[1]["unit_name"] == "GBA"
        assert unit_events[0]["unit_index"] == 0
        assert unit_events[1]["unit_index"] == 1

    @pytest.mark.asyncio
    async def test_skips_artwork_when_incremental_skipped(self, plugin):
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        queue = [WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(
            return_value=([{"id": 10, "name": "A", "platform_name": "N64"}], True)
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        # skipped=True from fetcher → no artwork download
        plugin._sync_service._orchestrator._download_artwork.assert_not_called()

    @pytest.mark.asyncio
    async def test_downloads_artwork_when_not_skipped(self, plugin):
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        queue = [WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(
            return_value=([{"id": 10, "name": "A", "platform_name": "N64"}], False)
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={10: "/grid/a.png"})

        async def fake_wait(_u, event):
            event.set()
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        plugin._sync_service._orchestrator._download_artwork.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_between_units_stops_processing(self, plugin):
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        queue = [
            WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1),
            WorkUnit(type="platform", id=2, name="GBA", slug="gba", rom_count=1),
        ]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(
            return_value=([{"id": 10, "name": "A", "platform_name": "N64"}], True)
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            # Flip to CANCELLING after first unit completes
            plugin._sync_service._sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1  # second unit was skipped
        complete_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert len(complete_events) == 1
        assert complete_events[0].get("cancelled") is True


class TestWaitForUnitComplete:
    """Heartbeat-based per-unit timeout."""

    @pytest.mark.asyncio
    async def test_returns_results_when_event_set(self, plugin):
        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        event.set()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._clock.monotonic()
        plugin._sync_service._box.last_unit_results = {"10": 9000}

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results == {"10": 9000}

    @pytest.mark.asyncio
    async def test_returns_none_on_cancel(self, plugin):
        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._sync_state = SyncState.CANCELLING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._clock.monotonic()

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_on_heartbeat_timeout(self, plugin):
        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._sync_state = SyncState.RUNNING
        # Heartbeat is way too old — should timeout immediately on first loop check
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._clock.monotonic() - 999.0

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results is None


class TestReportUnitResults:
    """Per-unit registry update + state checkpoint."""

    @pytest.mark.asyncio
    async def test_updates_registry_for_unit_roms(self, plugin):
        plugin._sync_service._pending_sync = {
            10: {"rom_id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
            11: {"rom_id": 11, "name": "B", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
        }

        result = await plugin.report_unit_results({"10": 9001, "11": 9002})

        assert result["success"] is True
        assert result["count"] == 2
        registry = plugin._state["shortcut_registry"]
        assert "10" in registry
        assert registry["10"]["app_id"] == 9001
        assert "11" in registry
        assert registry["11"]["app_id"] == 9002

    @pytest.mark.asyncio
    async def test_signals_unit_complete_event(self, plugin):
        plugin._sync_service._pending_sync = {}
        event = asyncio.Event()
        plugin._sync_service._box.unit_complete_event = event
        assert not event.is_set()

        await plugin.report_unit_results({})

        assert event.is_set()
        assert plugin._sync_service._box.last_unit_results == {}

    @pytest.mark.asyncio
    async def test_persists_state_after_unit(self, plugin):
        # Wrap the state persister to count calls
        save_count = [0]
        orig_save_state = plugin._state_persister.save_state

        def counting_save():
            save_count[0] += 1
            orig_save_state()

        plugin._state_persister.save_state = counting_save
        plugin._sync_service._reporter._state_persister.save_state = counting_save
        plugin._sync_service._pending_sync = {
            10: {"rom_id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
        }

        await plugin.report_unit_results({"10": 9001})

        assert save_count[0] == 1, "report_unit_results must checkpoint state to disk"


class TestShutdown:
    """Tests for shutdown() — lines 146-148.

    Graceful shutdown flips a RUNNING sync into CANCELLING so the
    per-unit loop drops its in-flight work on the next checkpoint.
    """

    def test_shutdown_when_running_marks_cancelling(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service.shutdown()
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_shutdown_when_idle_is_noop(self, plugin):
        plugin._sync_service._sync_state = SyncState.IDLE
        plugin._sync_service.shutdown()
        assert plugin._sync_service._sync_state == SyncState.IDLE

    def test_shutdown_when_cancelling_is_noop(self, plugin):
        plugin._sync_service._sync_state = SyncState.CANCELLING
        plugin._sync_service.shutdown()
        assert plugin._sync_service._sync_state == SyncState.CANCELLING


class TestSyncApplyDeltaUnifiedDispatch:
    """The unified apply path drives the per-unit pipeline through ``_do_sync_per_unit``.

    Artwork download moves into the per-unit loop (``_sync_one_unit``)
    rather than a single bulk pre-step — verified by end-to-end tests in
    :class:`TestDoSyncPerUnit` that follow the cached-prefetch path
    through to ``sync_apply_unit`` emission.
    """

    @pytest.mark.asyncio
    async def test_apply_dispatches_per_unit_with_artwork_for_non_skipped_units(self, plugin, tmp_path):
        """End-to-end: prefetched non-skipped unit triggers per-unit artwork + sync_apply_unit."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state["shortcut_registry"] = {}

        roms = [
            {"id": 10, "name": "Game X", "platform_name": "N64", "platform_slug": "n64", "fs_name": "x.z64"},
        ]
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id="art-preview",
            created_at=plugin._sync_service._clock.time(),
            platforms_count=1,
            total_roms=1,
        )
        plugin._sync_service._box.pending_prefetched_units = [
            _platform_prefetched(name="N64", slug="n64", roms=roms, skipped=False)
        ]

        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={10: "/grid/x.png"})

        async def fake_wait(_unit, event):
            event.set()
            return {"10": 5000}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait

        result = await plugin.sync_apply_delta("art-preview")
        # Drain background dispatch.
        for _ in range(10):
            await asyncio.sleep(0)

        assert result["success"] is True
        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        assert unit_events[0]["unit_name"] == "N64"
        # Cover path was downloaded per-unit (not skipped) and stamped onto the unit's shortcut.
        plugin._sync_service._orchestrator._download_artwork.assert_called_once()
        assert unit_events[0]["shortcuts"][0]["cover_path"] == "/grid/x.png"

    @pytest.mark.asyncio
    async def test_apply_skips_artwork_for_incremental_units(self, plugin, tmp_path):
        """Prefetched unit marked ``skipped=True`` bypasses artwork download in the per-unit loop."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        roms = [{"id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64"}]
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id="skip-preview",
            created_at=plugin._sync_service._clock.time(),
            platforms_count=1,
            total_roms=1,
        )
        plugin._sync_service._box.pending_prefetched_units = [
            _platform_prefetched(name="N64", slug="n64", roms=roms, skipped=True)
        ]
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_unit, event):
            event.set()
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait

        await plugin.sync_apply_delta("skip-preview")
        for _ in range(10):
            await asyncio.sleep(0)

        # Skipped=True → artwork download was not called for the unit.
        plugin._sync_service._orchestrator._download_artwork.assert_not_called()


class TestDoSyncPerUnitErrors:
    """Tests for error/cancel paths inside _do_sync_per_unit — lines 433-441, 489-502."""

    @pytest.mark.asyncio
    async def test_build_work_queue_cancelled_error_finishes_sync(self, plugin):
        """CancelledError during build_work_queue triggers _finish_sync + re-raise."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(side_effect=asyncio.CancelledError())
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "sync-cancel-build"

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._orchestrator._do_sync_per_unit()

        # _finish_sync transitioned to IDLE + cleared sync id.
        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._current_sync_id is None
        progress_phases = [
            c.args[1].get("phase") for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_progress"
        ]
        assert "cancelled" in progress_phases

    @pytest.mark.asyncio
    async def test_build_work_queue_general_exception_emits_error(self, plugin):
        """A non-cancellation exception during build_work_queue is logged + surfaced."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(side_effect=RuntimeError("RomM down"))
        emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._emit_progress = emit_progress
        plugin._sync_service._sync_state = SyncState.RUNNING

        # Should NOT raise — outer flow swallows the exception after emitting an error.
        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # error phase was emitted via _emit_progress.
        error_calls = [c for c in emit_progress.call_args_list if c.args and c.args[0] == "error"]
        assert len(error_calls) == 1
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_outer_exception_handler_emits_error_progress(self, plugin):
        """An exception raised after build_work_queue (e.g. during a unit) hits the outer except."""
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        queue = [WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        # Fetching the unit blows up after the queue was built — exception propagates
        # past _sync_one_unit and hits the outer except in _do_sync_per_unit (489-502).
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(side_effect=RuntimeError("boom"))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        # Drain any pending tasks scheduled by the outer handler (loop.create_task).
        for _ in range(3):
            await asyncio.sleep(0)

        # sync_progress with phase=error was scheduled.
        error_events = [
            c
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("phase") == "error"
        ]
        assert len(error_events) >= 1
        assert "Sync failed" in error_events[0].args[1]["message"]
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_pagination_failure_does_not_emit_partial_stale_removal(self, plugin):
        """#630 safety invariant: a fetch_platform_unit failure must NOT trigger
        the stale-cleanup pass with a partial ROM set.

        Before the fix, ``fetch_platform_unit`` swallowed pagination exceptions
        and returned ``([], False)``. The orchestrator then ran ``_finalize_per_unit``
        with ``synced_rom_ids == set()`` and the registry's full ROM list was
        emitted via ``sync_stale``, which the frontend turned into a wholesale
        Steam shortcut deletion.

        Now that the fetcher re-raises, the exception hits the outer ``except``
        in ``_do_sync_per_unit`` BEFORE ``_finalize_per_unit`` runs, so no
        ``sync_stale`` event is ever emitted.
        """
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        # Existing registry the frontend would happily delete from if the
        # orchestrator ever emitted a partial sync_stale.
        plugin._state["shortcut_registry"] = {
            "10": {"name": "Game A", "platform_name": "N64", "app_id": 1000},
            "20": {"name": "Game B", "platform_name": "N64", "app_id": 2000},
            "30": {"name": "Game C", "platform_name": "N64", "app_id": 3000},
        }

        queue = [WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        # Mid-pagination failure — the bug scenario from #630.
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(side_effect=RuntimeError("HTTP 500 on page 2"))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        # Drain any pending tasks scheduled by the outer handler.
        for _ in range(3):
            await asyncio.sleep(0)

        # The load-bearing assertion: sync_stale must never have been emitted.
        stale_events = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        assert stale_events == [], (
            f"Pagination failure leaked a partial sync_stale event: {stale_events}. "
            "This is the #630 wipe-the-library bug."
        )
        # And the registry is untouched — the orchestrator never reaches the
        # reporter's finalize path when the fetcher raises.
        assert set(plugin._state["shortcut_registry"].keys()) == {"10", "20", "30"}
        # The error path was taken instead.
        error_events = [
            c
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("phase") == "error"
        ]
        assert len(error_events) >= 1
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_cancelling_state_before_first_unit_skips_processing(self, plugin):
        """If state is CANCELLING when the unit loop starts, no units run (lines 462-464)."""
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        queue = [
            WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1),
            WorkUnit(type="platform", id=2, name="GBA", slug="gba", rom_count=1),
        ]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        fetch_mock = AsyncMock()
        plugin._sync_service._fetcher.fetch_platform_unit = fetch_mock
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._sync_state = SyncState.CANCELLING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # No units were processed because the CANCELLING check fired first.
        fetch_mock.assert_not_called()
        # _finalize_per_unit still ran; sync_complete is emitted with cancelled=True.
        complete = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_complete"]
        assert len(complete) == 1
        assert complete[0].args[1].get("cancelled") is True


class TestSyncOneUnitCollectionAndCancel:
    """Tests for _sync_one_unit branches — lines 535-538, 541, 557, 584-587."""

    @pytest.mark.asyncio
    async def test_collection_unit_records_membership(self, plugin):
        """A collection unit populates collection_memberships with its rom_ids (535-538)."""
        import decky

        from domain.work_unit import WorkUnit

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()

        queue = [WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=2, is_virtual=False)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        plugin._sync_service._fetcher.fetch_collection_unit = AsyncMock(
            return_value=([{"id": 1, "name": "A", "platform_name": "N64"}], [1, 2])
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # sync_complete fired (collection_memberships flowed through to finalize).
        complete = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_complete"]
        assert len(complete) == 1

    @pytest.mark.asyncio
    async def test_cancel_after_fetch_returns_zero_applied(self, plugin):
        """CANCELLING flipped after fetch_platform_unit → unit returns 0 (line 540-541)."""
        from domain.work_unit import WorkUnit

        plugin.loop = asyncio.get_event_loop()

        async def fetch_then_cancel(_unit):
            # Flip the state mid-flight so the post-fetch guard observes CANCELLING.
            plugin._sync_service._sync_state = SyncState.CANCELLING
            return [{"id": 1, "name": "A", "platform_name": "N64"}], True

        plugin._sync_service._fetcher.fetch_platform_unit = fetch_then_cancel
        emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._emit_progress = emit_progress
        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
            all_rom_id_to_app_id={},
        )
        assert applied == 0

    @pytest.mark.asyncio
    async def test_cancel_after_artwork_returns_zero_applied(self, plugin):
        """CANCELLING flipped after the artwork download → unit returns 0 (line 556-557)."""
        from domain.work_unit import WorkUnit

        plugin.loop = asyncio.get_event_loop()

        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(
            return_value=([{"id": 1, "name": "A", "platform_name": "N64"}], False)
        )

        async def cancel_during_artwork(*_a, **_kw):
            # Trigger CANCELLING in between the post-fetch check and the post-artwork check.
            plugin._sync_service._sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._download_artwork = cancel_during_artwork
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
            all_rom_id_to_app_id={},
        )
        assert applied == 0

    @pytest.mark.asyncio
    async def test_wait_returning_none_clears_pending_and_cancels(self, plugin):
        """When _wait_for_unit_complete returns None, the unit drops state + flips CANCELLING (584-587)."""
        from domain.work_unit import WorkUnit

        plugin.loop = asyncio.get_event_loop()

        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(
            return_value=([{"id": 1, "name": "A", "platform_name": "N64"}], True)
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        # Simulate heartbeat timeout / cancel inside _wait_for_unit_complete.
        async def wait_returns_none(_unit, _event):
            return None

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait_returns_none
        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
            all_rom_id_to_app_id={},
        )
        assert applied == 0
        # pending_sync was cleared, unit event reference dropped, state flipped.
        assert plugin._sync_service._pending_sync == {}
        assert plugin._sync_service._box.unit_complete_event is None
        assert plugin._sync_service._sync_state == SyncState.CANCELLING


class TestSyncOneUnitWithPrefetched:
    """``_sync_one_unit`` consumes pre-fetched ROMs when handed a ``PrefetchedUnit``.

    The unified Skip Preview OFF apply path passes the preview's
    cached unit data down so the per-unit driver does not call back into
    the fetcher for the same ROMs.
    """

    @pytest.mark.asyncio
    async def test_platform_unit_uses_cached_roms_no_refetch(self, plugin):
        plugin.loop = asyncio.get_event_loop()

        cached_roms = [
            {"id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64"},
            {"id": 11, "name": "B", "platform_name": "N64", "platform_slug": "n64"},
        ]
        prefetched = _platform_prefetched(name="N64", slug="n64", roms=cached_roms, skipped=True)
        # If anything calls the fetcher, the test fails — there is no live mock.
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(side_effect=AssertionError("must not refetch"))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"10": 5001, "11": 5002}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        platform_rom_ids: set[int] = set()
        synced_rom_ids: set[int] = set()
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            prefetched.unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=synced_rom_ids,
            collection_memberships={},
            platform_rom_ids=platform_rom_ids,
            all_rom_id_to_app_id={},
            prefetched=prefetched,
        )

        assert applied == 2
        # platform_rom_ids and synced_rom_ids reflect the cached ROMs even though no fetch happened.
        assert platform_rom_ids == {10, 11}
        assert synced_rom_ids == {10, 11}
        plugin._sync_service._fetcher.fetch_platform_unit.assert_not_called()

    @pytest.mark.asyncio
    async def test_collection_unit_uses_cached_membership_no_refetch(self, plugin):
        """Cached collection units replay their membership without refetching."""
        from domain.work_unit import WorkUnit
        from services.library._state import PrefetchedUnit

        plugin.loop = asyncio.get_event_loop()
        unit = WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=2)
        cached_roms = [{"id": 30, "name": "X", "platform_name": "N64", "platform_slug": "n64"}]
        prefetched = PrefetchedUnit(
            unit=unit,
            roms=cached_roms,
            skipped=False,
            all_collection_rom_ids=[30, 31],
        )
        plugin._sync_service._fetcher.fetch_collection_unit = AsyncMock(side_effect=AssertionError("must not refetch"))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"30": 9001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        memberships: dict[str, list[int]] = {}
        synced_rom_ids: set[int] = set()
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=synced_rom_ids,
            collection_memberships=memberships,
            platform_rom_ids=set(),
            all_rom_id_to_app_id={},
            prefetched=prefetched,
        )

        assert applied == 1
        # Cached membership flows through as if fetch_collection_unit had returned it.
        assert memberships == {"Faves": [30, 31]}
        # ROMs marked as synced so subsequent collection units can dedup.
        assert synced_rom_ids == {30}
        plugin._sync_service._fetcher.fetch_collection_unit.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefetched_unit_skips_metadata_re_stamp(self, plugin):
        """``cache_metadata_for_unit`` is not invoked when ROMs come from prefetch (already stamped)."""
        plugin.loop = asyncio.get_event_loop()
        prefetched = _platform_prefetched(
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64"}],
            skipped=True,
        )
        cache_meta = MagicMock()
        plugin._sync_service._fetcher.cache_metadata_for_unit = cache_meta
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"10": 5001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._sync_one_unit(
            prefetched.unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
            all_rom_id_to_app_id={},
            prefetched=prefetched,
        )

        cache_meta.assert_not_called()


class TestWaitForUnitCompleteCancelled:
    """Tests for asyncio.CancelledError in _wait_for_unit_complete — lines 617-621."""

    @pytest.mark.asyncio
    async def test_cancelled_error_during_sleep_is_logged_and_reraised(self, plugin):
        """If the inner sleep is cancelled, log + re-raise so the outer loop sees the cancel."""
        from domain.work_unit import WorkUnit

        class _CancellingSleeper:
            async def sleep(self, _seconds: float) -> None:
                raise asyncio.CancelledError()

        plugin._sync_service._sleeper = _CancellingSleeper()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._clock.monotonic()

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()  # never set — wait will enter the sleep path

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)


class TestDownloadArtworkDelegation:
    """Tests for _download_artwork — lines 660-669."""

    @pytest.mark.asyncio
    async def test_delegates_to_artwork_manager(self, plugin):
        """When _artwork is bound, the call is forwarded with progress + cancel hooks."""
        from unittest.mock import AsyncMock as _AsyncMock

        fake_download = _AsyncMock(return_value={1: "/path/a.png", 2: "/path/b.png"})
        plugin._sync_service._orchestrator._artwork = MagicMock()
        plugin._sync_service._orchestrator._artwork.download_artwork = fake_download

        roms = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        result = await plugin._sync_service._orchestrator._download_artwork(
            roms, progress_step=3, progress_total_steps=7
        )

        assert result == {1: "/path/a.png", 2: "/path/b.png"}
        fake_download.assert_called_once()
        call_kwargs = fake_download.call_args.kwargs
        assert call_kwargs["progress_step"] == 3
        assert call_kwargs["progress_total_steps"] == 7
        # is_cancelling closure reflects the live sync_state.
        is_cancelling = call_kwargs["is_cancelling"]
        plugin._sync_service._sync_state = SyncState.RUNNING
        assert is_cancelling() is False
        plugin._sync_service._sync_state = SyncState.CANCELLING
        assert is_cancelling() is True

    @pytest.mark.asyncio
    async def test_returns_empty_when_artwork_manager_missing(self, plugin):
        """No _artwork bound → return {} without raising."""
        plugin._sync_service._orchestrator._artwork = None
        result = await plugin._sync_service._orchestrator._download_artwork(
            [{"id": 1}], progress_step=1, progress_total_steps=1
        )
        assert result == {}
