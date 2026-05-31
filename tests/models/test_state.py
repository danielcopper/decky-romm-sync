"""Tests for models.state TypedDicts and the default-state factory."""

from typing import ClassVar

from models.state import make_default_plugin_state


class TestMakeDefaultPluginState:
    _REQUIRED_KEYS: ClassVar[set[str]] = {"downloaded_bios"}

    def test_returns_all_required_keys(self):
        state = make_default_plugin_state()
        assert set(state.keys()) == self._REQUIRED_KEYS

    def test_default_values_match_canonical_shape(self):
        state = make_default_plugin_state()
        assert state["downloaded_bios"] == {}

    def test_successive_calls_return_independent_containers(self):
        """Each call must return a fresh dict tree so per-test mutations
        don't leak across fixtures."""
        first = make_default_plugin_state()
        second = make_default_plugin_state()

        assert first is not second
        assert first["downloaded_bios"] is not second["downloaded_bios"]

    def test_mutation_does_not_leak_across_calls(self):
        first = make_default_plugin_state()
        second = make_default_plugin_state()

        first["downloaded_bios"]["gba_bios.bin"] = {  # type: ignore[typeddict-item]
            "file_path": "/bios/gba_bios.bin",
            "firmware_id": 1,
            "platform_slug": "gba",
            "downloaded_at": "2026-01-01T00:00:00",
        }

        assert second["downloaded_bios"] == {}
