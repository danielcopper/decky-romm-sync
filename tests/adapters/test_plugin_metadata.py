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

    def test_read_name_returns_declared_name_and_has_safe_fallback(self, tmp_path):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"name": "custom-plugin"}))
        adapter = PluginMetadataAdapter()
        assert adapter.read_name(str(plugin_dir)) == "custom-plugin"
        (plugin_dir / "package.json").write_text(json.dumps({"name": None}))
        assert adapter.read_name(str(plugin_dir)) == "custom-plugin"
        assert PluginMetadataAdapter().read_name(str(plugin_dir)) == "decky-plugin"

    def test_name_and_version_share_one_package_read(self, tmp_path, monkeypatch):
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"name": "plugin", "version": "1.2.3"}))
        adapter = PluginMetadataAdapter()
        original = adapter._read
        calls = 0

        def counted(path):
            nonlocal calls
            calls += 1
            return original(path)

        monkeypatch.setattr(adapter, "_read", counted)
        assert adapter.read_metadata(str(plugin_dir)) == ("plugin", "1.2.3")
        assert adapter.read_name(str(plugin_dir)) == "plugin"
        assert adapter.read_version(str(plugin_dir)) == "1.2.3"
        assert calls == 1

    def test_read_version_empty_version_returned_as_is(self, tmp_path):
        """An empty version string passes through rather than becoming the fallback.

        The fallback is reserved for a version this adapter cannot represent;
        an empty string reflects what the file actually contained and surfaces
        upstream so a malformed publish can be diagnosed.
        """
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"version": ""}))

        adapter = PluginMetadataAdapter()
        assert adapter.read_version(str(plugin_dir)) == ""

    @pytest.mark.parametrize("non_string", [None, 3, 1.2, ["1.0.0"], {"major": 1}])
    def test_read_version_non_string_returns_fallback(self, tmp_path, non_string):
        """A version that is not a string cannot be surfaced as one.

        ``read_version`` is declared to return ``str`` and its value is
        interpolated into the outgoing User-Agent, so a JSON number, object,
        list, or ``null`` takes the documented fallback instead of travelling
        onward mistyped.
        """
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"version": non_string}))

        adapter = PluginMetadataAdapter()
        assert adapter.read_version(str(plugin_dir)) == "0.0.0"
