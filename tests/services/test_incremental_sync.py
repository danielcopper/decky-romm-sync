"""Tests for incremental shortcut persistence (report_incremental_results / report_sync_finalized).

These tests exercise the new incremental batch persistence flow where the
frontend flushes shortcut batches periodically (every 20 shortcuts or 5s)
instead of accumulating everything until the end of sync.

Uses the ``_make_library()`` helper to construct LibraryService directly
(no Plugin fixture) so tests run on all platforms — including Windows
where ``fcntl`` is unavailable.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.library import LibraryService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _make_library(**overrides) -> LibraryService:
    """Build a LibraryService with sensible mock defaults."""
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


def _make_pending_rom(name: str, platform_name: str, platform_slug: str, cover_path: str = "") -> dict:
    """Build a minimal pending-sync entry."""
    return {
        "name": name,
        "platform_name": platform_name,
        "platform_slug": platform_slug,
        "cover_path": cover_path,
    }


# ---------------------------------------------------------------------------
# TestReportIncrementalResults
# ---------------------------------------------------------------------------


class TestReportIncrementalResults:
    """Tests for report_incremental_results — the per-batch persistence call."""

    @pytest.mark.asyncio
    async def test_persists_batch_to_registry(self):
        """Incremental batch creates entries in shortcut_registry."""
        lib = _make_library()
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
            2: _make_pending_rom("Game B", "SNES", "snes"),
        }
        # _build_registry_entry and _finalize_cover_path are real methods on lib,
        # but steam_config.grid_dir() is already a MagicMock that returns a MagicMock.
        # Patch _finalize_cover_path to return a dummy string.
        lib._finalize_cover_path = MagicMock(return_value="/fake/cover.png")

        result = await lib.report_incremental_results({"1": 100001, "2": 100002}, [])

        assert result["success"] is True
        assert result["persisted"] == 2
        assert "1" in lib._state["shortcut_registry"]
        assert lib._state["shortcut_registry"]["1"]["app_id"] == 100001
        assert lib._state["shortcut_registry"]["1"]["name"] == "Game A"
        assert "2" in lib._state["shortcut_registry"]
        assert lib._state["shortcut_registry"]["2"]["app_id"] == 100002

    @pytest.mark.asyncio
    async def test_removes_entries(self):
        """Incremental batch removes stale entries from registry."""
        lib = _make_library()
        lib._state["shortcut_registry"]["99"] = {
            "app_id": 99999,
            "name": "Old Game",
            "platform_name": "NES",
        }
        lib._pending_sync = {}

        result = await lib.report_incremental_results({}, [99])

        assert result["success"] is True
        assert result["persisted"] == 1
        assert "99" not in lib._state["shortcut_registry"]

    @pytest.mark.asyncio
    async def test_calls_save_state(self):
        """Each incremental batch calls save_state to persist to disk."""
        save_mock = MagicMock()
        lib = _make_library(save_state=save_mock)
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
        }
        lib._finalize_cover_path = MagicMock(return_value="")

        await lib.report_incremental_results({"1": 100001}, [])

        save_mock.assert_called()

    @pytest.mark.asyncio
    async def test_does_not_update_last_sync(self):
        """Incremental batches must NOT touch last_sync — finalization does that."""
        lib = _make_library()
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
        }
        lib._finalize_cover_path = MagicMock(return_value="")

        await lib.report_incremental_results({"1": 100001}, [])

        assert lib._state["last_sync"] is None

    @pytest.mark.asyncio
    async def test_does_not_clear_pending_sync(self):
        """Incremental batches must NOT clear _pending_sync — finalization does that."""
        lib = _make_library()
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
        }
        lib._finalize_cover_path = MagicMock(return_value="")

        await lib.report_incremental_results({"1": 100001}, [])

        assert 1 in lib._pending_sync

    @pytest.mark.asyncio
    async def test_multiple_batches_accumulate(self):
        """Multiple incremental batches accumulate in the registry."""
        lib = _make_library()
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
            2: _make_pending_rom("Game B", "SNES", "snes"),
            3: _make_pending_rom("Game C", "GBA", "gba"),
        }
        lib._finalize_cover_path = MagicMock(return_value="")

        # Batch 1
        r1 = await lib.report_incremental_results({"1": 1001}, [])
        assert r1["persisted"] == 1
        assert len(lib._state["shortcut_registry"]) == 1

        # Batch 2
        r2 = await lib.report_incremental_results({"2": 1002, "3": 1003}, [])
        assert r2["persisted"] == 2
        assert len(lib._state["shortcut_registry"]) == 3

    @pytest.mark.asyncio
    async def test_steam_input_config_applied(self):
        """Steam Input config is applied per-batch when setting is not 'default'."""
        steam_config_mock = MagicMock()
        steam_config_mock.grid_dir.return_value = "/tmp/grid"
        lib = _make_library(
            steam_config=steam_config_mock,
            settings={"enabled_platforms": {}, "steam_input_mode": "gamepad_with_joystick_mouse"},
        )
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
        }
        lib._finalize_cover_path = MagicMock(return_value="")

        await lib.report_incremental_results({"1": 100001}, [])

        steam_config_mock.set_steam_input_config.assert_called_once_with(
            [100001], mode="gamepad_with_joystick_mouse"
        )


# ---------------------------------------------------------------------------
# TestReportSyncFinalized
# ---------------------------------------------------------------------------


class TestReportSyncFinalized:
    """Tests for report_sync_finalized — the one-time finalization call."""

    @pytest.mark.asyncio
    async def test_sets_last_sync_on_success(self):
        """Successful finalization sets last_sync timestamp."""
        lib = _make_library()
        lib._pending_sync = {}
        lib._pending_collection_memberships = {}
        lib._pending_platform_rom_ids = None
        lib._finalize_cover_path = MagicMock(return_value="")

        result = await lib.report_sync_finalized({}, [], False)

        assert result["success"] is True
        assert lib._state["last_sync"] is not None

    @pytest.mark.asyncio
    async def test_does_not_set_last_sync_on_cancel(self):
        """Cancelled finalization does NOT set last_sync."""
        lib = _make_library()
        lib._pending_sync = {}
        lib._pending_collection_memberships = {}
        lib._pending_platform_rom_ids = None
        lib._finalize_cover_path = MagicMock(return_value="")

        result = await lib.report_sync_finalized({}, [], True)

        assert result["success"] is True
        assert lib._state["last_sync"] is None

    @pytest.mark.asyncio
    async def test_clears_pending_sync(self):
        """Finalization clears _pending_sync."""
        lib = _make_library()
        lib._pending_sync = {1: _make_pending_rom("X", "Y", "y")}
        lib._pending_collection_memberships = {}
        lib._pending_platform_rom_ids = None
        lib._finalize_cover_path = MagicMock(return_value="")

        await lib.report_sync_finalized({}, [], False)

        assert lib._pending_sync == {}

    @pytest.mark.asyncio
    async def test_persists_stragglers(self):
        """Finalization persists remaining shortcuts not in any incremental batch."""
        lib = _make_library()
        lib._pending_sync = {
            5: _make_pending_rom("Straggler", "GBA", "gba"),
        }
        lib._pending_collection_memberships = {}
        lib._pending_platform_rom_ids = None
        lib._finalize_cover_path = MagicMock(return_value="")

        result = await lib.report_sync_finalized({"5": 100005}, [], False)

        assert result["success"] is True
        assert "5" in lib._state["shortcut_registry"]
        assert lib._state["shortcut_registry"]["5"]["app_id"] == 100005

    @pytest.mark.asyncio
    async def test_emits_sync_complete_event(self):
        """Finalization emits sync_complete with platform_app_ids."""
        emit_mock = AsyncMock()
        lib = _make_library(emit=emit_mock)

        # Pre-populate registry as if incremental batches already ran
        lib._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64", "platform_slug": "n64"},
            "2": {"app_id": 1002, "name": "Game B", "platform_name": "N64", "platform_slug": "n64"},
        }
        lib._pending_sync = {}
        lib._pending_collection_memberships = {}
        lib._pending_platform_rom_ids = {1, 2}
        lib._finalize_cover_path = MagicMock(return_value="")

        await lib.report_sync_finalized({}, [], False)

        # Find the sync_complete emission
        sync_complete_call = None
        for call in emit_mock.call_args_list:
            if call[0][0] == "sync_complete":
                sync_complete_call = call
                break
        assert sync_complete_call is not None
        payload = sync_complete_call[0][1]
        assert "N64" in payload["platform_app_ids"]

    @pytest.mark.asyncio
    async def test_cancel_emits_empty_platform_app_ids(self):
        """Cancelled finalization emits sync_complete with empty platform_app_ids."""
        emit_mock = AsyncMock()
        lib = _make_library(emit=emit_mock)

        lib._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64", "platform_slug": "n64"},
        }
        lib._pending_sync = {}
        lib._pending_collection_memberships = {"My Collection": [1]}
        lib._pending_platform_rom_ids = {1}
        lib._finalize_cover_path = MagicMock(return_value="")

        await lib.report_sync_finalized({}, [], True)

        sync_complete_call = None
        for call in emit_mock.call_args_list:
            if call[0][0] == "sync_complete":
                sync_complete_call = call
                break
        assert sync_complete_call is not None
        assert sync_complete_call[0][1]["platform_app_ids"] == {}


# ---------------------------------------------------------------------------
# TestIncrementalThenFinalizeFlow
# ---------------------------------------------------------------------------


class TestIncrementalThenFinalizeFlow:
    """End-to-end: incremental batches + finalization."""

    @pytest.mark.asyncio
    async def test_full_flow(self):
        """3 incremental batches + finalize → registry complete + last_sync set."""
        lib = _make_library()
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
            2: _make_pending_rom("Game B", "SNES", "snes"),
            3: _make_pending_rom("Game C", "GBA", "gba"),
            4: _make_pending_rom("Game D", "N64", "n64"),
            5: _make_pending_rom("Game E", "SNES", "snes"),
        }
        lib._pending_collection_memberships = {}
        lib._pending_platform_rom_ids = {1, 2, 3, 4, 5}
        lib._finalize_cover_path = MagicMock(return_value="")

        # Batch 1
        await lib.report_incremental_results({"1": 1001, "2": 1002}, [])
        # Batch 2
        await lib.report_incremental_results({"3": 1003, "4": 1004}, [])
        # Batch 3
        await lib.report_incremental_results({"5": 1005}, [])

        # Finalize
        result = await lib.report_sync_finalized({}, [], False)
        assert result["success"] is True
        assert len(lib._state["shortcut_registry"]) == 5
        assert lib._state["last_sync"] is not None
        assert lib._pending_sync == {}

    @pytest.mark.asyncio
    async def test_partial_cancel_preserves_completed_batches(self):
        """2 batches + cancel → registry has batch 1+2 entries only, no last_sync."""
        lib = _make_library()
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
            2: _make_pending_rom("Game B", "SNES", "snes"),
            3: _make_pending_rom("Game C", "GBA", "gba"),
        }
        lib._pending_collection_memberships = {}
        lib._pending_platform_rom_ids = {1, 2, 3}
        lib._finalize_cover_path = MagicMock(return_value="")

        # Batch 1
        await lib.report_incremental_results({"1": 1001}, [])
        # Batch 2
        await lib.report_incremental_results({"2": 1002}, [])

        # Cancel — game 3 never got processed
        result = await lib.report_sync_finalized({}, [], True)
        assert result["success"] is True
        assert len(lib._state["shortcut_registry"]) == 2
        assert "1" in lib._state["shortcut_registry"]
        assert "2" in lib._state["shortcut_registry"]
        assert "3" not in lib._state["shortcut_registry"]
        assert lib._state["last_sync"] is None

    @pytest.mark.asyncio
    async def test_backward_compat_report_sync_results(self):
        """Old report_sync_results still works identically (backward compat)."""
        lib = _make_library()
        lib._pending_sync = {
            1: _make_pending_rom("Game A", "N64", "n64"),
            2: _make_pending_rom("Game B", "SNES", "snes"),
        }
        lib._pending_collection_memberships = {}
        lib._pending_platform_rom_ids = {1, 2}
        lib._finalize_cover_path = MagicMock(return_value="")

        result = await lib.report_sync_results({"1": 100001, "2": 100002}, [])

        assert result["success"] is True
        assert "1" in lib._state["shortcut_registry"]
        assert "2" in lib._state["shortcut_registry"]
        assert lib._state["last_sync"] is not None
        assert lib._pending_sync == {}
