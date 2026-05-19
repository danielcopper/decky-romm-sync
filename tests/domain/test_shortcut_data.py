"""Tests for domain/shortcut_data.py pure functions."""

import os

from domain.shortcut_data import build_shortcuts_data


class TestBuildShortcutsData:
    """Tests for build_shortcuts_data()."""

    def test_builds_correct_format(self):
        plugin_dir = "/home/deck/homebrew/plugins/decky-romm-sync"
        roms = [
            {
                "id": 1,
                "name": "Game A",
                "fs_name": "gamea.z64",
                "platform_name": "N64",
                "platform_slug": "n64",
                "igdb_id": 100,
                "sgdb_id": 200,
                "ra_id": 300,
            },
            {"id": 2, "name": "Game B", "platform_name": "SNES", "platform_slug": "snes"},
        ]
        result = build_shortcuts_data(roms, plugin_dir)
        assert len(result) == 2
        assert result[0]["rom_id"] == 1
        assert result[0]["name"] == "Game A"
        assert result[0]["fs_name"] == "gamea.z64"
        assert result[0]["launch_options"] == "romm:1"
        assert result[0]["platform_name"] == "N64"
        assert result[0]["platform_slug"] == "n64"
        assert result[0]["igdb_id"] == 100
        assert result[0]["sgdb_id"] == 200
        assert result[0]["ra_id"] == 300
        assert result[0]["cover_path"] == ""
        assert result[0]["exe"] == os.path.join(plugin_dir, "bin", "romm-launcher")
        assert result[0]["start_dir"] == os.path.join(plugin_dir, "bin")
        assert result[1]["fs_name"] == ""

    def test_empty_roms(self):
        result = build_shortcuts_data([], "/some/dir")
        assert result == []

    def test_missing_optional_fields(self):
        roms = [{"id": 5, "name": "Minimal"}]
        result = build_shortcuts_data(roms, "/plugin")
        assert result[0]["rom_id"] == 5
        assert result[0]["platform_name"] == "Unknown"
        assert result[0]["platform_slug"] == ""
        assert result[0]["igdb_id"] is None
        assert result[0]["sgdb_id"] is None

    def test_exe_path_contains_romm_launcher(self):
        plugin_dir = "/home/deck/homebrew/plugins/decky-romm-sync"
        roms = [{"id": 1, "name": "Game"}]
        result = build_shortcuts_data(roms, plugin_dir)
        assert result[0]["exe"].endswith("/bin/romm-launcher")

    def test_start_dir_is_parent_of_exe(self):
        plugin_dir = "/home/deck/homebrew/plugins/decky-romm-sync"
        roms = [{"id": 1, "name": "Game"}]
        result = build_shortcuts_data(roms, plugin_dir)
        assert result[0]["start_dir"] == os.path.dirname(result[0]["exe"])

    def test_launch_options_format(self):
        import re

        pattern = r"^romm:\d+$"
        roms = [{"id": i, "name": f"Game {i}"} for i in [1, 42, 99999]]
        result = build_shortcuts_data(roms, "/plugin")
        for item in result:
            assert re.match(pattern, item["launch_options"])

    def test_multiple_roms_each_has_required_fields(self):
        required_fields = {"rom_id", "name", "exe", "start_dir", "launch_options", "platform_name", "platform_slug"}
        roms = [{"id": i, "name": f"Game {i}"} for i in range(5)]
        result = build_shortcuts_data(roms, "/plugin")
        for item in result:
            for field in required_fields:
                assert field in item, f"Missing field '{field}' in shortcut data"
