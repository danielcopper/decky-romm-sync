"""Tests for the EsDeCoreInfoAdapter — wraps domain.es_de_config module functions."""

from __future__ import annotations

from unittest.mock import patch

from adapters.es_de_core_info import EsDeCoreInfoAdapter


class TestGetActiveCore:
    def test_delegates_to_es_de_config_with_rom_filename(self):
        adapter = EsDeCoreInfoAdapter()
        with patch(
            "domain.es_de_config.get_active_core",
            return_value=("mgba_libretro.so", "mGBA"),
        ) as mock_fn:
            result = adapter.get_active_core("gba", rom_filename="game.gba")
        mock_fn.assert_called_once_with("gba", rom_filename="game.gba")
        assert result == ("mgba_libretro.so", "mGBA")

    def test_default_rom_filename_is_none(self):
        adapter = EsDeCoreInfoAdapter()
        with patch(
            "domain.es_de_config.get_active_core",
            return_value=("gpsp_libretro.so", "gpSP"),
        ) as mock_fn:
            adapter.get_active_core("gba")
        mock_fn.assert_called_once_with("gba", rom_filename=None)

    def test_returns_none_tuple_when_underlying_returns_none(self):
        adapter = EsDeCoreInfoAdapter()
        with patch("domain.es_de_config.get_active_core", return_value=(None, None)):
            result = adapter.get_active_core("unknown_platform")
        assert result == (None, None)


class TestGetAvailableCores:
    def test_delegates_to_es_de_config(self):
        adapter = EsDeCoreInfoAdapter()
        cores = [
            {"core_so": "mgba_libretro.so", "label": "mGBA", "is_default": True},
            {"core_so": "gpsp_libretro.so", "label": "gpSP", "is_default": False},
        ]
        with patch(
            "domain.es_de_config.get_available_cores",
            return_value=cores,
        ) as mock_fn:
            result = adapter.get_available_cores("gba")
        mock_fn.assert_called_once_with("gba")
        assert result == cores

    def test_returns_empty_list_when_underlying_returns_empty(self):
        adapter = EsDeCoreInfoAdapter()
        with patch("domain.es_de_config.get_available_cores", return_value=[]):
            result = adapter.get_available_cores("unknown_platform")
        assert result == []
