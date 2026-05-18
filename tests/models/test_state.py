"""Tests for models.state TypedDicts and the default-state factory."""

from typing import ClassVar

from models.state import make_default_plugin_state


class TestMakeDefaultPluginState:
    _REQUIRED_KEYS: ClassVar[set[str]] = {
        "shortcut_registry",
        "installed_roms",
        "last_sync",
        "sync_stats",
        "downloaded_bios",
        "retrodeck_home_path",
        "save_sort_settings",
    }

    def test_returns_all_required_keys(self):
        state = make_default_plugin_state()
        assert set(state.keys()) == self._REQUIRED_KEYS

    def test_default_values_match_canonical_shape(self):
        state = make_default_plugin_state()
        assert state["shortcut_registry"] == {}
        assert state["installed_roms"] == {}
        assert state["last_sync"] is None
        assert state["sync_stats"] == {"platforms": 0, "roms": 0}
        assert state["downloaded_bios"] == {}
        assert state["retrodeck_home_path"] == ""
        assert state["save_sort_settings"] is None

    def test_sync_stats_zero_initialised(self):
        state = make_default_plugin_state()
        assert state["sync_stats"]["platforms"] == 0
        assert state["sync_stats"]["roms"] == 0

    def test_successive_calls_return_independent_containers(self):
        """Each call must return a fresh dict tree so per-test mutations
        don't leak across fixtures."""
        first = make_default_plugin_state()
        second = make_default_plugin_state()

        assert first is not second
        assert first["shortcut_registry"] is not second["shortcut_registry"]
        assert first["installed_roms"] is not second["installed_roms"]
        assert first["sync_stats"] is not second["sync_stats"]
        assert first["downloaded_bios"] is not second["downloaded_bios"]

    def test_mutation_does_not_leak_across_calls(self):
        first = make_default_plugin_state()
        second = make_default_plugin_state()

        first["installed_roms"]["42"] = {  # type: ignore[typeddict-item]
            "rom_id": 42,
            "file_name": "game.gba",
            "file_path": "/roms/gba/game.gba",
            "system": "gba",
            "platform_slug": "gba",
            "installed_at": "2026-01-01T00:00:00",
        }
        first["sync_stats"]["platforms"] = 5
        first["last_sync"] = "2026-01-01T00:00:00"

        assert second["installed_roms"] == {}
        assert second["sync_stats"] == {"platforms": 0, "roms": 0}
        assert second["last_sync"] is None

    def test_transient_keys_absent_by_default(self):
        """NotRequired keys must be absent until a particular event populates them."""
        state = make_default_plugin_state()
        assert "retrodeck_home_path_previous" not in state
        assert "save_sort_settings_previous" not in state
        assert "last_synced_collections" not in state
        assert "last_synced_platforms" not in state
