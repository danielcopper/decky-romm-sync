"""Tests for SyncOrchestrator — preview/apply/full-sync lifecycle and safety heartbeat."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from adapters.persistence import (
    PersistenceAdapter,
)
from domain.preview_delta import PreviewDelta
from domain.sync_state import SyncState

# conftest.py patches decky before this import


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

        # Mock _fetch_and_prepare to return known data
        platforms = [{"name": "N64", "slug": "n64"}]
        all_roms = [{"id": 1}, {"id": 2}, {"id": 3}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
            {"rom_id": 2, "name": "Game B", "platform_name": "N64", "platform_slug": "n64", "fs_name": "b.z64"},
            {"rom_id": 3, "name": "Game C", "platform_name": "N64", "platform_slug": "n64", "fs_name": "c.z64"},
        ]
        plugin._sync_service._fetcher._fetch_and_prepare = AsyncMock(
            return_value=(all_roms, shortcuts_data, platforms, {}, set())
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

        platforms = [{"name": "N64", "slug": "n64"}]
        all_roms = [{"id": 1}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
        ]
        plugin._sync_service._fetcher._fetch_and_prepare = AsyncMock(
            return_value=(all_roms, shortcuts_data, platforms, {}, set())
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        result = await plugin.sync_preview()
        assert plugin._sync_service._pending_delta is not None
        assert plugin._sync_service._pending_delta.preview_id == result["preview_id"]
        assert plugin._sync_service._pending_delta.created_at == plugin._sync_service._clock.time()
        assert len(plugin._sync_service._pending_delta.new) == 1
        assert plugin._sync_service._pending_delta.platforms_count == 1
        assert plugin._sync_service._pending_delta.total_roms == 1

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

        platforms = [{"name": "N64"}]
        all_roms = [{"id": 1}]
        shortcuts_data = [
            {"rom_id": 1, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
        ]
        plugin._sync_service._fetcher._fetch_and_prepare = AsyncMock(
            return_value=(all_roms, shortcuts_data, platforms, {}, set())
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        await plugin.sync_preview()
        assert plugin._sync_service._sync_state == SyncState.IDLE


class TestSyncApplyDelta:
    """Tests for sync_apply_delta()."""

    def _setup_pending_delta(self, plugin, preview_id="test-preview-123"):
        """Helper to populate _pending_delta with valid data."""
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id=preview_id,
            created_at=plugin._sync_service._clock.time(),
            new=[
                {
                    "rom_id": 3,
                    "name": "Game C",
                    "platform_name": "N64",
                    "platform_slug": "n64",
                    "fs_name": "c.z64",
                    "cover_path": "",
                },
            ],
            changed=[
                {
                    "rom_id": 2,
                    "name": "New B",
                    "existing_app_id": 1002,
                    "platform_name": "N64",
                    "platform_slug": "n64",
                    "fs_name": "b.z64",
                    "cover_path": "",
                },
            ],
            unchanged_ids=[1],
            remove_rom_ids=[99],
            all_shortcuts={
                1: {"rom_id": 1, "name": "Game A", "platform_name": "N64"},
                2: {"rom_id": 2, "name": "New B", "platform_name": "N64"},
                3: {"rom_id": 3, "name": "Game C", "platform_name": "N64"},
            },
            delta_roms=[],
            platforms_count=1,
            total_roms=3,
            collection_memberships={},
            platform_rom_ids=set(),
        )

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
        # Stale delta is cleared so a repeat apply can't pick it up.
        assert plugin._sync_service._pending_delta is None

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
        # Just under the 30-minute window.
        plugin._sync_service._clock.advance(1799)

        result = await plugin.sync_apply_delta("preview-xyz")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_emits_sync_apply_with_delta(self, plugin, tmp_path):

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        # Set up registry for unchanged rom
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin)
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        result = await plugin.sync_apply_delta("test-preview-123")
        assert result["success"] is True

        # Check decky.emit was called with sync_apply
        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "sync_apply"]
        assert len(emit_calls) == 1
        payload = emit_calls[0][0][1]
        assert len(payload["shortcuts"]) == 1  # new
        assert len(payload["changed_shortcuts"]) == 1  # changed
        assert payload["remove_rom_ids"] == [99]

    @pytest.mark.asyncio
    async def test_populates_pending_sync(self, plugin, tmp_path):

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin)
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")
        assert 1 in plugin._sync_service._pending_sync
        assert 2 in plugin._sync_service._pending_sync
        assert 3 in plugin._sync_service._pending_sync

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

        await plugin.sync_apply_delta("test-preview-123")
        assert plugin._sync_service._pending_delta is None

    @pytest.mark.asyncio
    async def test_sync_apply_does_not_include_collection_data(self, plugin, tmp_path):

        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
            "5": {"app_id": 1005, "name": "Game E", "platform_name": "SNES"},
        }
        # Include both rom 1 and 5 as unchanged
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id="test-preview-123",
            created_at=plugin._sync_service._clock.time(),
            new=[],
            changed=[],
            unchanged_ids=[1, 5],
            remove_rom_ids=[],
            all_shortcuts={
                1: {"rom_id": 1, "name": "Game A", "platform_name": "N64"},
                5: {"rom_id": 5, "name": "Game E", "platform_name": "SNES"},
            },
            delta_roms=[],
            platforms_count=2,
            total_roms=2,
            collection_memberships={},
            platform_rom_ids=set(),
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")

        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "sync_apply"]
        assert len(emit_calls) == 1
        # Platform collection data is no longer in sync_apply — it's built in report_sync_results
        # and sent via sync_complete instead.
        assert "collection_platform_app_ids" not in emit_calls[0][0][1]
        assert "platform_eligible_rom_ids" not in emit_calls[0][0][1]


class TestSyncCancelPreview:
    """Tests for sync_cancel_preview()."""

    @pytest.mark.asyncio
    async def test_clears_pending_delta(self, plugin):
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id="some-id",
            created_at=plugin._sync_service._clock.time(),
            new=[],
            changed=[],
            unchanged_ids=[],
            remove_rom_ids=[],
            all_shortcuts={},
            delta_roms=[],
            platforms_count=0,
            total_roms=0,
            collection_memberships={},
            platform_rom_ids=set(),
        )
        result = await plugin.sync_cancel_preview()
        assert plugin._sync_service._pending_delta is None
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
        """_finish_sync invalidates _current_sync_id so any in-flight safety
        timeout for the cancelled sync sees a stale generation."""
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_progress = {"running": True}
        plugin._sync_service._current_sync_id = "sync-abc"

        await plugin._sync_service._orchestrator._finish_sync("Sync cancelled")

        assert plugin._sync_service._current_sync_id is None


class TestSafetyTimeoutGenerationGuard:
    """Regression for #351 — safety timeout must not emit a stale "done"
    after _finish_sync (cancel/error) or report_sync_results (happy end)
    has already transitioned the sync."""

    @staticmethod
    def _gated_sleeper(release: "asyncio.Event"):
        """A sleeper that blocks until ``release`` is set."""

        class _Gated:
            async def sleep(self, _seconds: float) -> None:
                await release.wait()

        return _Gated()

    @pytest.mark.asyncio
    async def test_safety_timeout_silenced_after_finish_sync(self, plugin):
        """Cancel during sync → safety timeout's late wake-up emits nothing.

        Reproduces the original glitch: UI receiving `cancelled` followed by
        a phantom `done` because the background timeout fired after
        ``_finish_sync`` had already transitioned to IDLE.
        """
        import decky

        svc = plugin._sync_service
        loop = asyncio.get_event_loop()
        plugin.loop = loop
        svc._loop = loop

        release = asyncio.Event()
        svc._sleeper = self._gated_sleeper(release)
        svc._sync_state = SyncState.RUNNING
        svc._current_sync_id = "sync-abc"
        svc._sync_progress = {"running": True, "current": 5, "total": 10}

        decky.emit.reset_mock()
        task = svc._orchestrator._start_safety_timeout(heartbeat_timeout_sec=1)
        # Advance past the heartbeat timeout so the elapsed check would
        # otherwise fire — the generation guard must override it.
        svc._clock.advance(999)

        # Let the safety-timeout task park on the gated sleep.
        await asyncio.sleep(0)
        # Cancel completes — clears _current_sync_id while timeout is parked.
        await svc._orchestrator._finish_sync("Sync cancelled")
        # Release the timeout; its generation guard should fire and exit.
        release.set()
        await task

        progress_phases = [
            call.args[1]["phase"] for call in decky.emit.call_args_list if call.args and call.args[0] == "sync_progress"
        ]
        assert "cancelled" in progress_phases
        # The original glitch: a phantom "done" landing after "cancelled".
        assert "done" not in progress_phases

    @pytest.mark.asyncio
    async def test_safety_timeout_fires_when_generation_unchanged(self, plugin):
        """Sanity check the guard isn't over-eager: same generation → still fires."""
        import decky

        svc = plugin._sync_service
        loop = asyncio.get_event_loop()
        plugin.loop = loop
        svc._loop = loop

        release = asyncio.Event()
        svc._sleeper = self._gated_sleeper(release)
        svc._sync_state = SyncState.RUNNING
        svc._current_sync_id = "sync-xyz"
        svc._sync_progress = {"running": True, "current": 5, "total": 10}
        svc._state["sync_stats"] = {"roms": 5, "platforms": 1}

        decky.emit.reset_mock()
        task = svc._orchestrator._start_safety_timeout(heartbeat_timeout_sec=1)
        # Advance the FakeClock past the timeout so elapsed > heartbeat_timeout.
        svc._clock.advance(999)

        await asyncio.sleep(0)
        # No cancel — generation id unchanged. Release the sleep; timeout fires.
        release.set()
        await task

        progress_phases = [
            call.args[1]["phase"] for call in decky.emit.call_args_list if call.args and call.args[0] == "sync_progress"
        ]
        assert "done" in progress_phases
        assert svc._sync_state == SyncState.IDLE
        assert svc._current_sync_id is None

    @pytest.mark.asyncio
    async def test_safety_timeout_silenced_after_report_sync_results(self, plugin, tmp_path):
        """Happy-end path → safety timeout's late wake-up emits nothing.

        Mirrors the cancel scenario for the report_sync_results clearing
        path: frontend reports successfully, _current_sync_id is cleared,
        any in-flight safety timeout sees the stale captured id and exits.
        """
        import decky

        from adapters.persistence import PersistenceAdapter

        svc = plugin._sync_service
        loop = asyncio.get_event_loop()
        plugin.loop = loop
        svc._loop = loop
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        release = asyncio.Event()
        svc._sleeper = self._gated_sleeper(release)
        svc._sync_state = SyncState.RUNNING
        svc._current_sync_id = "sync-happy"
        svc._sync_progress = {"running": True, "current": 5, "total": 10}
        svc._pending_sync = {}

        decky.emit.reset_mock()
        task = svc._orchestrator._start_safety_timeout(heartbeat_timeout_sec=1)
        svc._clock.advance(999)

        await asyncio.sleep(0)
        # Happy end: report_sync_results clears the id and transitions to IDLE.
        await plugin.report_sync_results({}, [])
        release.set()
        await task

        progress_phases = [
            call.args[1]["phase"] for call in decky.emit.call_args_list if call.args and call.args[0] == "sync_progress"
        ]
        # report_sync_results emits its own "done"; the safety timeout's
        # captured id no longer matches, so it does NOT emit a second one.
        assert progress_phases.count("done") == 1
        assert svc._current_sync_id is None

    @pytest.mark.asyncio
    async def test_safety_timeout_does_not_stomp_new_sync_started_during_emit(self, plugin):
        """Post-emit re-check: a new sync starting between safety timeout's
        emit and its IDLE/clear must not have its state stomped."""
        import decky

        svc = plugin._sync_service
        loop = asyncio.get_event_loop()
        plugin.loop = loop
        svc._loop = loop

        release = asyncio.Event()
        svc._sleeper = self._gated_sleeper(release)
        svc._sync_state = SyncState.RUNNING
        svc._current_sync_id = "sync-old"
        svc._sync_progress = {"running": True, "current": 5, "total": 10}
        svc._state["sync_stats"] = {"roms": 5, "platforms": 1}

        # Inject a new-sync start during the safety timeout's _emit_progress
        # await by stubbing _emit_progress to mutate the live state mid-call.
        async def _emit_progress_mid_start(*_a, **_kw):
            # Simulate a fresh sync racing in between emit and stomp.
            svc._sync_state = SyncState.RUNNING
            svc._current_sync_id = "sync-new"

        svc._orchestrator._emit_progress = _emit_progress_mid_start

        decky.emit.reset_mock()
        task = svc._orchestrator._start_safety_timeout(heartbeat_timeout_sec=1)
        svc._clock.advance(999)

        await asyncio.sleep(0)
        release.set()
        await task

        # The new sync's state must be intact — safety timeout's second
        # generation check observed the change and exited.
        assert svc._sync_state == SyncState.RUNNING
        assert svc._current_sync_id == "sync-new"


class TestSyncPreviewErrorHandling:
    """Tests for sync_preview error paths — lines 210-219."""

    @pytest.mark.asyncio
    async def test_general_exception_returns_error(self, plugin):

        plugin._sync_service._fetcher._fetch_and_prepare = AsyncMock(side_effect=RuntimeError("Something broke"))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        result = await plugin._sync_service.sync_preview()
        assert result["success"] is False
        assert "error_code" in result
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_cancelled_error_reraises(self, plugin):

        import decky

        decky.emit.reset_mock()

        plugin._sync_service._fetcher._fetch_and_prepare = AsyncMock(side_effect=asyncio.CancelledError("cancelled"))
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service.sync_preview()
        assert plugin._sync_service._sync_state == SyncState.IDLE


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
