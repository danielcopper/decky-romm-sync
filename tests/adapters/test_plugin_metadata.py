"""Tests for the PluginMetadataAdapter — reads plugin package.json."""

from __future__ import annotations

import json

import pytest

from adapters.plugin_metadata import PluginMetadataAdapter


class TestPluginMetadataAdapter:
    def test_read_version_returns_declared_version(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"version": "1.2.3"}))

        adapter = PluginMetadataAdapter()
        assert adapter.read_version(str(plugin_dir)) == "1.2.3"

    def test_read_version_missing_file_returns_fallback(self, tmp_path):
        """A missing package.json must not abort bootstrap."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()

        adapter = PluginMetadataAdapter()
        assert adapter.read_version(str(plugin_dir)) == "0.0.0"

    def test_read_version_missing_directory_returns_fallback(self, tmp_path):
        adapter = PluginMetadataAdapter()
        assert adapter.read_version(str(tmp_path / "does-not-exist")) == "0.0.0"

    def test_read_version_malformed_json_returns_fallback(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text("{not valid json")

        adapter = PluginMetadataAdapter()
        assert adapter.read_version(str(plugin_dir)) == "0.0.0"

    def test_read_version_missing_version_field_returns_fallback(self, tmp_path):
        """A package.json without a ``version`` key falls back to ``0.0.0``."""
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"name": "decky-romm-sync"}))

        adapter = PluginMetadataAdapter()
        assert adapter.read_version(str(plugin_dir)) == "0.0.0"

    @pytest.mark.parametrize("empty_value", ["", None])
    def test_read_version_empty_version_returned_as_is(self, tmp_path, empty_value):
        """The adapter does not validate the version string — empty/None pass through.

        The fallback is only applied when the field is missing entirely; an
        empty string or ``None`` reflects what the file actually contained
        and surfaces upstream so a malformed publish can be diagnosed.
        """
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"version": empty_value}))

        adapter = PluginMetadataAdapter()
        assert adapter.read_version(str(plugin_dir)) == empty_value
