"""Tests for domain/shortcut_data.py pure functions."""

import os

from domain.shortcut_data import (
    RETRODECK_INVOCATION,
    build_launch_options,
    build_shortcuts_data,
    resolve_emulator_invocation,
)


class TestResolveEmulatorInvocation:
    """Tests for resolve_emulator_invocation()."""

    def test_returns_retrodeck_command(self):
        assert resolve_emulator_invocation({"id": 1}) == "flatpak run net.retrodeck.retrodeck"
        assert resolve_emulator_invocation({"id": 1}) == RETRODECK_INVOCATION

    def test_ignores_rom_contents(self):
        # The per-emulator seam ignores the ROM today — any ROM resolves identically.
        assert resolve_emulator_invocation({}) == resolve_emulator_invocation(
            {"id": 5, "platform_slug": "n64", "name": "X"}
        )


class TestBuildLaunchOptions:
    """Tests for build_launch_options()."""

    def test_quotes_path(self):
        assert build_launch_options(RETRODECK_INVOCATION, "/roms/n64/zelda.z64") == (
            'flatpak run net.retrodeck.retrodeck "/roms/n64/zelda.z64"'
        )

    def test_quotes_path_with_spaces(self):
        result = build_launch_options(RETRODECK_INVOCATION, "/roms/dc/My Game.chd")
        assert result == 'flatpak run net.retrodeck.retrodeck "/roms/dc/My Game.chd"'

    def test_empty_path_still_quoted(self):
        assert build_launch_options(RETRODECK_INVOCATION, "") == 'flatpak run net.retrodeck.retrodeck ""'


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
        result = build_shortcuts_data(roms, plugin_dir, {1: "/roms/n64/gamea.z64"})
        assert len(result) == 2
        assert result[0]["rom_id"] == 1
        assert result[0]["name"] == "Game A"
        assert result[0]["fs_name"] == "gamea.z64"
        assert result[0]["platform_name"] == "N64"
        assert result[0]["platform_slug"] == "n64"
        assert result[0]["igdb_id"] == 100
        assert result[0]["sgdb_id"] == 200
        assert result[0]["ra_id"] == 300
        assert result[0]["cover_path"] == ""
        assert result[0]["exe"] == os.path.join(plugin_dir, "bin", "rom-launcher")
        assert result[0]["start_dir"] == os.path.join(plugin_dir, "bin")
        assert result[1]["fs_name"] == ""

    def test_installed_rom_gets_launch_command(self):
        roms = [{"id": 1, "name": "Game A"}]
        result = build_shortcuts_data(roms, "/plugin", {1: "/roms/n64/gamea.z64"})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/n64/gamea.z64"'

    def test_installed_rom_path_with_spaces_is_quoted(self):
        roms = [{"id": 7, "name": "Spacey"}]
        result = build_shortcuts_data(roms, "/plugin", {7: "/roms/dc/My Game.chd"})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/dc/My Game.chd"'

    def test_uninstalled_rom_gets_empty_launch_options(self):
        roms = [{"id": 2, "name": "Game B"}]
        result = build_shortcuts_data(roms, "/plugin", {})
        assert result[0]["launch_options"] == ""

    def test_mixed_installed_and_uninstalled(self):
        roms = [
            {"id": 1, "name": "Installed"},
            {"id": 2, "name": "NotInstalled"},
        ]
        result = build_shortcuts_data(roms, "/plugin", {1: "/roms/snes/installed.sfc"})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/snes/installed.sfc"'
        assert result[1]["launch_options"] == ""

    def test_empty_roms(self):
        result = build_shortcuts_data([], "/some/dir", {})
        assert result == []

    def test_missing_optional_fields(self):
        roms = [{"id": 5, "name": "Minimal"}]
        result = build_shortcuts_data(roms, "/plugin", {})
        assert result[0]["rom_id"] == 5
        assert result[0]["platform_name"] == "Unknown"
        assert result[0]["platform_slug"] == ""
        assert result[0]["igdb_id"] is None
        assert result[0]["sgdb_id"] is None

    def test_exe_path_contains_rom_launcher(self):
        plugin_dir = "/home/deck/homebrew/plugins/decky-romm-sync"
        roms = [{"id": 1, "name": "Game"}]
        result = build_shortcuts_data(roms, plugin_dir, {})
        assert result[0]["exe"].endswith("/bin/rom-launcher")

    def test_start_dir_is_parent_of_exe(self):
        plugin_dir = "/home/deck/homebrew/plugins/decky-romm-sync"
        roms = [{"id": 1, "name": "Game"}]
        result = build_shortcuts_data(roms, plugin_dir, {})
        assert result[0]["start_dir"] == os.path.dirname(result[0]["exe"])

    def test_multiple_roms_each_has_required_fields(self):
        required_fields = {"rom_id", "name", "exe", "start_dir", "launch_options", "platform_name", "platform_slug"}
        roms = [{"id": i, "name": f"Game {i}"} for i in range(5)]
        result = build_shortcuts_data(roms, "/plugin", {})
        for item in result:
            for field in required_fields:
                assert field in item, f"Missing field '{field}' in shortcut data"
