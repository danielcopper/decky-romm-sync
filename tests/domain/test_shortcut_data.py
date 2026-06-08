"""Tests for domain/shortcut_data.py pure functions."""

import os

from domain.shortcut_data import (
    RETRODECK_INVOCATION,
    build_launch_options,
    build_shortcuts_data,
    label_to_core_so,
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

    def test_explicit_none_core_is_plain_invocation(self):
        # active_core_so=None must behave exactly like the 1-arg call: no -e override.
        result = resolve_emulator_invocation({"id": 1}, None)
        assert result == RETRODECK_INVOCATION
        assert "-e" not in result

    def test_one_arg_default_has_no_override(self):
        assert "-e" not in resolve_emulator_invocation({"id": 1})

    def test_core_so_bakes_golden_e_override(self):
        # Byte-exact golden -e string: literal cores dir, preserved %…% placeholders.
        # The core name is BARE (no extension) as the es_systems parser yields it;
        # the bake appends exactly one ".so" for the on-disk RetroArch core path.
        result = resolve_emulator_invocation({"id": 1}, "pcsx_rearmed_libretro")
        assert result == (
            "flatpak run net.retrodeck.retrodeck "
            '-e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/pcsx_rearmed_libretro.so %ROM%"'
        )

    def test_bare_core_name_yields_exactly_one_so_suffix(self):
        # Regression for the on-device crash: the bake appended no ".so" and the
        # fakes hid it by passing ".so"-suffixed names. With the real bare name
        # the baked -L path must carry exactly one ".so" — not zero, not two.
        result = resolve_emulator_invocation({"id": 1}, "pcsx_rearmed")
        assert "/var/config/retroarch/cores/pcsx_rearmed.so" in result
        assert "pcsx_rearmed.so.so" not in result
        assert "/cores/pcsx_rearmed %ROM%" not in result

    def test_core_so_uses_literal_cores_dir_and_keeps_placeholders(self):
        result = resolve_emulator_invocation({"id": 1}, "pcsx_rearmed_libretro")
        assert "/var/config/retroarch/cores" in result
        assert "%EMULATOR_RETROARCH%" in result
        assert "%ROM%" in result
        # The cores dir is baked literally; %CORE_RETROARCH% is NOT used.
        assert "%CORE_RETROARCH%" not in result

    def test_none_never_yields_none_so(self):
        # B4 guard: a None core must never reach the f-string as the literal "None.so".
        assert "None.so" not in resolve_emulator_invocation({"id": 1}, None)
        assert "None" not in resolve_emulator_invocation({"id": 1}, None)


# Shape mirrors CoreInfoProvider.get_available_cores():
# [{"core_so": str, "label": str, "is_default": bool}, ...]. core_so is the BARE
# core name (no ".so") as the es_systems parser and core_defaults.json yield it.
_AVAILABLE_CORES = [
    {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
    {"core_so": "mednafen_psx_hw_libretro", "label": "Beetle PSX HW", "is_default": False},
]


class TestLabelToCoreSo:
    """Tests for label_to_core_so()."""

    def test_match_returns_core_so(self):
        assert label_to_core_so(_AVAILABLE_CORES, "PCSX ReARMed") == "pcsx_rearmed_libretro"
        assert label_to_core_so(_AVAILABLE_CORES, "Beetle PSX HW") == "mednafen_psx_hw_libretro"

    def test_miss_returns_none(self):
        assert label_to_core_so(_AVAILABLE_CORES, "No Such Core") is None

    def test_empty_cores_list_returns_none(self):
        assert label_to_core_so([], "PCSX ReARMed") is None

    def test_empty_label_returns_none(self):
        assert label_to_core_so(_AVAILABLE_CORES, "") is None


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
        result = build_shortcuts_data(roms, plugin_dir, {1: "/roms/n64/gamea.z64"}, {})
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
        result = build_shortcuts_data(roms, "/plugin", {1: "/roms/n64/gamea.z64"}, {})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/n64/gamea.z64"'

    def test_installed_rom_path_with_spaces_is_quoted(self):
        roms = [{"id": 7, "name": "Spacey"}]
        result = build_shortcuts_data(roms, "/plugin", {7: "/roms/dc/My Game.chd"}, {})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/dc/My Game.chd"'

    def test_uninstalled_rom_gets_empty_launch_options(self):
        roms = [{"id": 2, "name": "Game B"}]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["launch_options"] == ""

    def test_mixed_installed_and_uninstalled(self):
        roms = [
            {"id": 1, "name": "Installed"},
            {"id": 2, "name": "NotInstalled"},
        ]
        result = build_shortcuts_data(roms, "/plugin", {1: "/roms/snes/installed.sfc"}, {})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/snes/installed.sfc"'
        assert result[1]["launch_options"] == ""

    def test_installed_rom_with_core_override_bakes_e_form(self):
        # A rom_id present in core_overrides bakes the -e override into its launch.
        roms = [{"id": 1, "name": "PSX Game"}]
        result = build_shortcuts_data(roms, "/plugin", {1: "/roms/psx/game.chd"}, {1: "pcsx_rearmed_libretro"})
        assert result[0]["launch_options"] == (
            "flatpak run net.retrodeck.retrodeck "
            '-e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/pcsx_rearmed_libretro.so %ROM%" '
            '"/roms/psx/game.chd"'
        )

    def test_installed_rom_absent_from_overrides_is_plain(self):
        # A rom_id NOT in core_overrides follows the default — plain launch, no -e.
        roms = [{"id": 1, "name": "Plain"}]
        result = build_shortcuts_data(roms, "/plugin", {1: "/roms/n64/g.z64"}, {2: "other_libretro"})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/n64/g.z64"'
        assert "-e" not in result[0]["launch_options"]

    def test_uninstalled_rom_with_override_still_empty(self):
        # An override on an UNINSTALLED rom can't bake — no path, empty placeholder.
        roms = [{"id": 1, "name": "NotDownloaded"}]
        result = build_shortcuts_data(roms, "/plugin", {}, {1: "pcsx_rearmed_libretro"})
        assert result[0]["launch_options"] == ""

    def test_empty_roms(self):
        result = build_shortcuts_data([], "/some/dir", {}, {})
        assert result == []

    def test_missing_optional_fields(self):
        roms = [{"id": 5, "name": "Minimal"}]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["rom_id"] == 5
        assert result[0]["platform_name"] == "Unknown"
        assert result[0]["platform_slug"] == ""
        assert result[0]["igdb_id"] is None
        assert result[0]["sgdb_id"] is None

    def test_exe_path_contains_rom_launcher(self):
        plugin_dir = "/home/deck/homebrew/plugins/decky-romm-sync"
        roms = [{"id": 1, "name": "Game"}]
        result = build_shortcuts_data(roms, plugin_dir, {}, {})
        assert result[0]["exe"].endswith("/bin/rom-launcher")

    def test_start_dir_is_parent_of_exe(self):
        plugin_dir = "/home/deck/homebrew/plugins/decky-romm-sync"
        roms = [{"id": 1, "name": "Game"}]
        result = build_shortcuts_data(roms, plugin_dir, {}, {})
        assert result[0]["start_dir"] == os.path.dirname(result[0]["exe"])

    def test_multiple_roms_each_has_required_fields(self):
        required_fields = {"rom_id", "name", "exe", "start_dir", "launch_options", "platform_name", "platform_slug"}
        roms = [{"id": i, "name": f"Game {i}"} for i in range(5)]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        for item in result:
            for field in required_fields:
                assert field in item, f"Missing field '{field}' in shortcut data"
