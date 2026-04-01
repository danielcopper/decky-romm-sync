"""Tests for PR 8 — Collection Removal Guard.

Validates that the ``remove_on_unsync`` setting controls whether stale
ROM IDs are included in the removal list during sync.  When the setting
is ``False`` (guard active), the stale list must be empty so that
unchecking a collection/platform does **not** delete its shortcuts.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — same pattern as other service tests
# ---------------------------------------------------------------------------

import sys, os  # noqa: E401,E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "py_modules"))

from services.library import LibraryService  # noqa: E402


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


def _shortcut(rid: int, name: str = "", platform: str = "Test") -> dict:
    """Build a minimal shortcut data dict."""
    return {
        "rom_id": rid,
        "name": name or f"ROM_{rid}",
        "platform_name": platform,
        "platform_slug": "test",
        "fs_name": f"rom_{rid}.zip",
    }


def _registry_entry(rid: int, platform: str = "Test") -> dict:
    """Build a minimal registry entry."""
    return {
        "app_id": 1000 + rid,
        "name": f"ROM_{rid}",
        "platform_name": platform,
        "platform_slug": "test",
        "fs_name": f"rom_{rid}.zip",
    }


# ---------------------------------------------------------------------------
# _classify_roms — delta sync path
# ---------------------------------------------------------------------------


class TestClassifyRomsRemovalGuard:
    """_classify_roms respects the remove_on_unsync setting."""

    def test_stale_returned_when_setting_true(self):
        """Default behaviour: stale ROMs are returned for removal."""
        registry = {
            "1": _registry_entry(1, "SNES"),
            "2": _registry_entry(2, "SNES"),
            "3": _registry_entry(3, "NES"),
        }
        lib = _make_library(
            state={"shortcut_registry": registry, "installed_roms": {}},
            settings={"remove_on_unsync": True},
        )
        # Only ROM 1 is in the current fetch — 2 and 3 are stale
        shortcuts_data = [_shortcut(1, platform="SNES")]
        _new, _changed, _unchanged, stale, _dc = lib._classify_roms(
            shortcuts_data, fetched_platform_names={"SNES"}
        )
        assert sorted(stale) == [2, 3]

    def test_stale_empty_when_setting_false(self):
        """Guard active: stale list is always empty."""
        registry = {
            "1": _registry_entry(1, "SNES"),
            "2": _registry_entry(2, "SNES"),
            "3": _registry_entry(3, "NES"),
        }
        lib = _make_library(
            state={"shortcut_registry": registry, "installed_roms": {}},
            settings={"remove_on_unsync": False},
        )
        shortcuts_data = [_shortcut(1, platform="SNES")]
        _new, _changed, _unchanged, stale, _dc = lib._classify_roms(
            shortcuts_data, fetched_platform_names={"SNES"}
        )
        assert stale == []

    def test_stale_returned_when_setting_absent(self):
        """Backward compat: missing key defaults to True (removals happen)."""
        registry = {
            "1": _registry_entry(1, "SNES"),
            "2": _registry_entry(2, "NES"),
        }
        lib = _make_library(
            state={"shortcut_registry": registry, "installed_roms": {}},
            settings={},  # no remove_on_unsync key
        )
        shortcuts_data = [_shortcut(1, platform="SNES")]
        _new, _changed, _unchanged, stale, _dc = lib._classify_roms(
            shortcuts_data, fetched_platform_names={"SNES"}
        )
        assert stale == [2]

    def test_disabled_count_still_computed_when_guard_active(self):
        """Even with guard active, disabled_count is reported for the UI."""
        registry = {
            "1": _registry_entry(1, "SNES"),
            "2": _registry_entry(2, "NES"),  # NES not in fetched platforms
        }
        lib = _make_library(
            state={"shortcut_registry": registry, "installed_roms": {}},
            settings={"remove_on_unsync": False},
        )
        shortcuts_data = [_shortcut(1, platform="SNES")]
        _new, _changed, _unchanged, stale, disabled_count = lib._classify_roms(
            shortcuts_data, fetched_platform_names={"SNES"}
        )
        assert stale == []
        assert disabled_count == 1  # NES ROM is from a disabled platform

    def test_no_stale_when_all_roms_present(self):
        """No stale ROMs even without guard — nothing to remove."""
        registry = {
            "1": _registry_entry(1, "SNES"),
            "2": _registry_entry(2, "SNES"),
        }
        lib = _make_library(
            state={"shortcut_registry": registry, "installed_roms": {}},
            settings={"remove_on_unsync": True},
        )
        shortcuts_data = [_shortcut(1, platform="SNES"), _shortcut(2, platform="SNES")]
        _new, _changed, _unchanged, stale, _dc = lib._classify_roms(
            shortcuts_data, fetched_platform_names={"SNES"}
        )
        assert stale == []


# ---------------------------------------------------------------------------
# Classification correctness (new/changed/unchanged not affected)
# ---------------------------------------------------------------------------


class TestClassifyRomsUnchanged:
    """Guard setting doesn't affect new/changed/unchanged classification."""

    def test_new_detected_with_guard_active(self):
        """New ROMs still detected when guard is active."""
        lib = _make_library(
            state={"shortcut_registry": {}, "installed_roms": {}},
            settings={"remove_on_unsync": False},
        )
        shortcuts_data = [_shortcut(99, platform="SNES")]
        new, _changed, _unchanged, _stale, _dc = lib._classify_roms(
            shortcuts_data, fetched_platform_names={"SNES"}
        )
        assert len(new) == 1
        assert new[0]["rom_id"] == 99

    def test_changed_detected_with_guard_active(self):
        """Changed ROMs still detected when guard is active."""
        registry = {
            "1": {**_registry_entry(1, "SNES"), "name": "Old Name"},
        }
        lib = _make_library(
            state={"shortcut_registry": registry, "installed_roms": {}},
            settings={"remove_on_unsync": False},
        )
        shortcuts_data = [_shortcut(1, name="New Name", platform="SNES")]
        _new, changed, _unchanged, _stale, _dc = lib._classify_roms(
            shortcuts_data, fetched_platform_names={"SNES"}
        )
        assert len(changed) == 1

    def test_unchanged_detected_with_guard_active(self):
        """Unchanged ROMs still detected when guard is active."""
        entry = _registry_entry(1, "SNES")
        entry["name"] = "ROM_1"
        registry = {"1": entry}
        lib = _make_library(
            state={"shortcut_registry": registry, "installed_roms": {}},
            settings={"remove_on_unsync": False},
        )
        shortcuts_data = [_shortcut(1, name="ROM_1", platform="SNES")]
        _new, _changed, unchanged, _stale, _dc = lib._classify_roms(
            shortcuts_data, fetched_platform_names={"SNES"}
        )
        assert unchanged == [1]


# ---------------------------------------------------------------------------
# Setting persistence (main.py endpoint)
# ---------------------------------------------------------------------------


class TestRemoveOnUnsyncSetting:
    """Tests for the save_remove_on_unsync endpoint pattern."""

    def test_default_is_true_when_missing(self):
        """Settings dict without key should default to True."""
        lib = _make_library(settings={})
        assert lib._settings.get("remove_on_unsync", True) is True

    def test_setting_can_be_set_false(self):
        """Setting the value to False is preserved."""
        settings: dict[str, Any] = {"remove_on_unsync": False}
        lib = _make_library(settings=settings)
        assert lib._settings.get("remove_on_unsync", True) is False

    def test_setting_can_be_toggled(self):
        """Toggle from True→False changes stale behaviour."""
        registry = {"1": _registry_entry(1, "SNES"), "2": _registry_entry(2, "NES")}
        settings: dict[str, Any] = {"remove_on_unsync": True}
        lib = _make_library(
            state={"shortcut_registry": registry, "installed_roms": {}},
            settings=settings,
        )
        shortcuts_data = [_shortcut(1, platform="SNES")]

        # With True: stale returned
        _, _, _, stale_on, _ = lib._classify_roms(shortcuts_data, {"SNES"})
        assert len(stale_on) == 1

        # Toggle to False
        settings["remove_on_unsync"] = False
        _, _, _, stale_off, _ = lib._classify_roms(shortcuts_data, {"SNES"})
        assert stale_off == []
