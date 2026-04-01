"""Tests for PR 3 — Enhanced Progress Reporting.

Validates that ``_emit_progress`` enriches payloads with ETA, elapsed, and
items-per-second data from the ETAEstimator, and that stepped phases auto-feed
the estimator while non-stepped phases leave it to the caller.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers — build a minimal LibraryService without importing platform-specific
# modules (fcntl).  We replicate the same pattern as test_collection_cache.py.
# ---------------------------------------------------------------------------

import sys, os  # noqa: E401,E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "py_modules"))

from services.library import LibraryService  # noqa: E402
from domain.sync_state import SyncState  # noqa: E402


def _make_library(**overrides: Any) -> LibraryService:
    """Create a LibraryService with sensible mock defaults."""
    defaults: dict[str, Any] = {
        "romm_api": MagicMock(),
        "steam_config": MagicMock(),
        "state": {"shortcut_registry": {}, "installed_roms": {}},
        "settings": {"enabled_platforms": {}, "enabled_collections": {}},
        "metadata_cache": {},
        "loop": MagicMock(),
        "logger": MagicMock(),
        "plugin_dir": "/tmp/test_plugin",
        "emit": AsyncMock(),
        "save_state": MagicMock(),
        "save_settings_to_disk": MagicMock(),
        "log_debug": MagicMock(),
    }
    defaults.update(overrides)
    return LibraryService(**defaults)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _get_last_emitted(lib: LibraryService) -> dict:
    """Return the most recently emitted sync_progress payload."""
    return lib._emit.call_args_list[-1].args[1]  # positional: (event_name, payload)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmitProgressPayload:
    """Verify the shape of the enriched payload."""

    @pytest.mark.asyncio
    async def test_payload_contains_enhanced_fields(self):
        lib = _make_library()
        await lib._emit_progress("roms", current=10, total=100, message="Fetching...")

        payload = _get_last_emitted(lib)
        assert "elapsedSec" in payload
        assert "etaSec" in payload
        assert "itemsPerSec" in payload
        assert "subPhase" in payload

    @pytest.mark.asyncio
    async def test_sub_phase_passed_through(self):
        lib = _make_library()
        await lib._emit_progress("roms", current=5, total=50, sub_phase="platform:SNES")

        payload = _get_last_emitted(lib)
        assert payload["subPhase"] == "platform:SNES"

    @pytest.mark.asyncio
    async def test_eta_none_when_no_samples(self):
        lib = _make_library()
        # Never started the ETA → should be None
        await lib._emit_progress("roms", current=1, total=100)

        payload = _get_last_emitted(lib)
        assert payload["etaSec"] is None

    @pytest.mark.asyncio
    async def test_elapsed_zero_before_start(self):
        lib = _make_library()
        await lib._emit_progress("roms", current=0, total=0)

        payload = _get_last_emitted(lib)
        assert payload["elapsedSec"] == 0

    @pytest.mark.asyncio
    async def test_elapsed_positive_after_start(self):
        lib = _make_library()
        lib._eta.start()
        # Sleep enough that rounding to 1 decimal still yields > 0
        time.sleep(0.12)
        await lib._emit_progress("roms", current=1, total=10)

        payload = _get_last_emitted(lib)
        assert payload["elapsedSec"] > 0


class TestAutoEtaUpdate:
    """Verify that stepped phases auto-feed the ETA estimator."""

    @pytest.mark.asyncio
    async def test_stepped_phase_auto_updates_eta(self):
        """When step > 0, _emit_progress should call _eta.update() automatically."""
        lib = _make_library()
        lib._eta.start()

        # Simulate min_samples=5 stepped emissions
        for i in range(1, 8):
            time.sleep(0.005)
            await lib._emit_progress(
                "applying",
                current=i,
                total=20,
                step=1,
                total_steps=2,
                message=f"Artwork {i}/20",
            )

        payload = _get_last_emitted(lib)
        # After 7 samples (>5 min_samples), ETA should be available
        assert payload["etaSec"] is not None
        assert payload["etaSec"] > 0
        assert payload["itemsPerSec"] is not None
        assert payload["itemsPerSec"] > 0

    @pytest.mark.asyncio
    async def test_non_stepped_phase_does_not_auto_update(self):
        """When step == 0 (fetch phase), ETA should NOT be auto-updated by _emit_progress."""
        lib = _make_library()
        lib._eta.start()

        # Emit without step — ETA estimator should NOT be fed
        for i in range(1, 10):
            time.sleep(0.005)
            await lib._emit_progress(
                "roms",
                current=i * 100,  # ROM count, not platform count
                total=0,  # total unknown during fetch
                message=f"Fetching... {i * 100} found",
            )

        # Estimator should still have 0 samples (no auto-update)
        assert lib._eta._samples == 0


class TestEtaResetOnPhaseTransition:
    """Verify that ETA resets at phase boundaries."""

    @pytest.mark.asyncio
    async def test_start_resets_eta(self):
        lib = _make_library()
        lib._eta.start()
        lib._eta.update(5)
        time.sleep(0.01)
        lib._eta.update(10)

        assert lib._eta._samples > 0

        # Simulating phase transition
        lib._eta.start()
        assert lib._eta._samples == 0
        assert lib._eta._last_count == 0


class TestProgressPayloadIntegrity:
    """Verify backward-compatibility: all original fields still present."""

    @pytest.mark.asyncio
    async def test_original_fields_preserved(self):
        lib = _make_library()
        await lib._emit_progress(
            "applying",
            current=5,
            total=10,
            message="Testing",
            running=True,
            step=1,
            total_steps=2,
        )

        payload = _get_last_emitted(lib)
        # Original fields
        assert payload["running"] is True
        assert payload["phase"] == "applying"
        assert payload["current"] == 5
        assert payload["total"] == 10
        assert payload["message"] == "Testing"
        assert payload["step"] == 1
        assert payload["totalSteps"] == 2

    @pytest.mark.asyncio
    async def test_done_phase_not_running(self):
        lib = _make_library()
        await lib._emit_progress("done", message="Complete", running=False)

        payload = _get_last_emitted(lib)
        assert payload["running"] is False
        assert payload["phase"] == "done"

    @pytest.mark.asyncio
    async def test_emit_called_with_sync_progress_event(self):
        lib = _make_library()
        await lib._emit_progress("roms", message="test")

        lib._emit.assert_called()
        event_name = lib._emit.call_args_list[-1].args[0]
        assert event_name == "sync_progress"
