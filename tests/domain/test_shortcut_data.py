"""Tests for domain/shortcut_data.py pure functions."""

import os
import shlex

from domain.shortcut_data import (
    RETRODECK_INVOCATION,
    EmulatorInvocation,
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

    def test_explicit_none_core_is_plain_invocation(self):
        # active_core_so=None must behave exactly like the 1-arg call: no -e override.
        result = resolve_emulator_invocation({"id": 1}, None)
        assert result == RETRODECK_INVOCATION
        assert "-e" not in result

    def test_unrenderable_invocation_degrades_to_plain(self):
        # A non-None but half-resolved invocation (standalone without a command,
        # libretro without a core_so, or an unknown kind) must never reach the
        # f-string and bake a broken -e — it degrades to the plain launch.
        for emulator in (
            EmulatorInvocation(kind="standalone", command=None),
            EmulatorInvocation(kind="libretro", core_so=None),
            EmulatorInvocation(kind="unknown"),
        ):
            result = resolve_emulator_invocation({"id": 1}, emulator)
            assert result == RETRODECK_INVOCATION
            assert "-e" not in result

    def test_one_arg_default_has_no_override(self):
        assert "-e" not in resolve_emulator_invocation({"id": 1})

    def test_core_so_bakes_golden_e_override(self):
        # Byte-exact golden -e string: literal cores dir, preserved %…% placeholders.
        # The core name is BARE (no extension) as the es_systems parser yields it;
        # the bake appends exactly one ".so" for the on-disk RetroArch core path.
        result = resolve_emulator_invocation({"id": 1}, EmulatorInvocation.libretro("pcsx_rearmed_libretro"))
        assert result == (
            "flatpak run net.retrodeck.retrodeck "
            '-e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/pcsx_rearmed_libretro.so %ROM%"'
        )

    def test_bare_core_name_yields_exactly_one_so_suffix(self):
        # Regression for the on-device crash: the bake appended no ".so" and the
        # fakes hid it by passing ".so"-suffixed names. With the real bare name
        # the baked -L path must carry exactly one ".so" — not zero, not two.
        result = resolve_emulator_invocation({"id": 1}, EmulatorInvocation.libretro("pcsx_rearmed"))
        assert "/var/config/retroarch/cores/pcsx_rearmed.so" in result
        assert "pcsx_rearmed.so.so" not in result
        assert "/cores/pcsx_rearmed %ROM%" not in result

    def test_core_so_uses_literal_cores_dir_and_keeps_placeholders(self):
        result = resolve_emulator_invocation({"id": 1}, EmulatorInvocation.libretro("pcsx_rearmed_libretro"))
        assert "/var/config/retroarch/cores" in result
        assert "%EMULATOR_RETROARCH%" in result
        assert "%ROM%" in result
        # The cores dir is baked literally; %CORE_RETROARCH% is NOT used.
        assert "%CORE_RETROARCH%" not in result

    def test_standalone_bakes_command_verbatim(self):
        # A standalone emulator's full ES-DE <command> (already ending in %ROM%) is
        # baked verbatim into -e; RetroDECK expands %EMULATOR_*% and %ROM% at launch.
        result = resolve_emulator_invocation(
            {"id": 1}, EmulatorInvocation.standalone("%EMULATOR_RPCS3% --no-gui %ROM%")
        )
        assert result == 'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RPCS3% --no-gui %ROM%"'
        # No libretro -L form leaks in for a standalone emulator.
        assert "-L " not in result
        assert "%CORE_RETROARCH%" not in result

    def test_standalone_ps2_batch_form(self):
        result = resolve_emulator_invocation({"id": 1}, EmulatorInvocation.standalone("%EMULATOR_PCSX2% -batch %ROM%"))
        assert result == 'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_PCSX2% -batch %ROM%"'

    def test_direct_runs_sandbox_launcher_bypassing_run_game(self):
        # A folder-boot direct invocation runs the emulator's sandbox launcher via
        # flatpak --command=, NOT run_game.sh (which reinterprets a directory %ROM%
        # as a "directory as a file"). No -e, no %ROM% — the folder is appended by
        # build_launch_options. Middle args (--no-gui) survive.
        result = resolve_emulator_invocation(
            {"id": 1},
            EmulatorInvocation.direct(
                "%EMULATOR_RPCS3% --no-gui %ROM%",
                "/app/retrodeck/components/rpcs3/component_launcher.sh",
                "RPCS3 Directory (Standalone)",
            ),
        )
        assert result == (
            "flatpak run --command=/app/retrodeck/components/rpcs3/component_launcher.sh "
            "net.retrodeck.retrodeck --no-gui"
        )
        assert "-e " not in result
        assert "%ROM%" not in result

    def test_direct_with_no_middle_args_has_no_trailing_space(self):
        result = resolve_emulator_invocation(
            {"id": 1},
            EmulatorInvocation.direct(
                "%EMULATOR_RPCS3% %ROM%", "/app/retrodeck/components/rpcs3/component_launcher.sh"
            ),
        )
        assert result == (
            "flatpak run --command=/app/retrodeck/components/rpcs3/component_launcher.sh net.retrodeck.retrodeck"
        )
        assert not result.endswith(" ")

    def test_direct_missing_launcher_degrades_to_plain_launch(self):
        # A half-resolved direct invocation (no launcher) must never render a
        # broken "--command=None"; it degrades to the plain RetroDECK launch.
        result = resolve_emulator_invocation(
            {"id": 1}, EmulatorInvocation(kind="direct", command="%EMULATOR_RPCS3% --no-gui %ROM%", launcher=None)
        )
        assert result == "flatpak run net.retrodeck.retrodeck"
        assert "--command=" not in result

    def test_none_never_yields_none_so(self):
        # B4 guard: a None core must never reach the f-string as the literal "None.so".
        assert "None.so" not in resolve_emulator_invocation({"id": 1}, None)
        assert "None" not in resolve_emulator_invocation({"id": 1}, None)


class TestBuildLaunchOptions:
    """Tests for build_launch_options()."""

    def test_quotes_path(self):
        assert build_launch_options(RETRODECK_INVOCATION, "/roms/n64/zelda.z64") == (
            'flatpak run net.retrodeck.retrodeck "/roms/n64/zelda.z64"'
        )

    def test_quotes_path_with_spaces(self):
        result = build_launch_options(RETRODECK_INVOCATION, "/roms/dc/My Game.chd")
        assert result == 'flatpak run net.retrodeck.retrodeck "/roms/dc/My Game.chd"'

    def test_empty_path_yields_no_launch_command(self):
        # An empty path is the "no launch target" signal — not downloaded, or
        # downloaded with nothing the system can boot. Quoting it would hand the
        # emulator a bare "" argument, which is the failure #1652 exists to
        # prevent; the shortcut gets the same empty command an uninstalled ROM's
        # carries instead.
        assert build_launch_options(RETRODECK_INVOCATION, "") == ""

    def test_folder_boot_direct_composes_full_mgs4_command(self):
        # End-to-end: the direct invocation + the quoted game folder reproduce the
        # exact on-device-verified RPCS3 boot command (ADR-0019 / #1212).
        invocation = resolve_emulator_invocation(
            {"id": 1},
            EmulatorInvocation.direct(
                "%EMULATOR_RPCS3% --no-gui %ROM%",
                "/app/retrodeck/components/rpcs3/component_launcher.sh",
                "RPCS3 Directory (Standalone)",
            ),
        )
        result = build_launch_options(invocation, "/run/media/deck/Emulation/retrodeck/roms/ps3/Metal Gear Solid 4")
        assert result == (
            "flatpak run --command=/app/retrodeck/components/rpcs3/component_launcher.sh "
            'net.retrodeck.retrodeck --no-gui "/run/media/deck/Emulation/retrodeck/roms/ps3/Metal Gear Solid 4"'
        )

    # The tests below use shlex.split(posix=True) as a reference POSIX /
    # ``\"``-honoring tokenizer: it proves the escaping is internally correct (a
    # server-controlled ROM filename round-trips to exactly ONE final argv token).
    # Parity with Steam's actual (closed-source) launch-time tokenizer is verified
    # on-device, not here.

    def test_quote_in_path_round_trips_to_one_arg(self):
        path = '/roms/gba/Game".gba'
        result = build_launch_options(RETRODECK_INVOCATION, path)
        assert shlex.split(result, posix=True) == [
            "flatpak",
            "run",
            "net.retrodeck.retrodeck",
            path,
        ]

    def test_argv_injection_attempt_is_neutralized(self):
        path = '/roms/gba/evil" --inject-flag --foo.gba'
        result = build_launch_options(RETRODECK_INVOCATION, path)
        tokens = shlex.split(result, posix=True)
        # The 3 invocation tokens + exactly ONE final token equal to the original
        # path: --inject-flag / --foo never become separate argv elements.
        assert tokens == ["flatpak", "run", "net.retrodeck.retrodeck", path]

    def test_backslash_in_path_round_trips(self):
        path = "/roms/gba/a\\b.gba"  # one literal backslash
        result = build_launch_options(RETRODECK_INVOCATION, path)
        tokens = shlex.split(result, posix=True)
        assert tokens[-1] == path
        assert tokens == ["flatpak", "run", "net.retrodeck.retrodeck", path]

    def test_trailing_backslash_does_not_eat_closing_quote(self):
        path = "/roms/gba/dir\\"  # ends in one literal backslash
        result = build_launch_options(RETRODECK_INVOCATION, path)
        tokens = shlex.split(result, posix=True)
        # Without escaping, the trailing \ would escape the closing " and merge
        # the token with whatever follows; backslash-escaping keeps it one arg.
        assert tokens[-1] == path
        assert tokens == ["flatpak", "run", "net.retrodeck.retrodeck", path]

    def test_combined_backslash_and_quote_round_trips(self):
        path = '/roms/gba/a\\"b.gba'  # one backslash followed by a quote
        result = build_launch_options(RETRODECK_INVOCATION, path)
        tokens = shlex.split(result, posix=True)
        assert tokens[-1] == path
        assert tokens == ["flatpak", "run", "net.retrodeck.retrodeck", path]

    def test_path_is_just_a_quote(self):
        path = '"'  # filename consisting of a single double-quote
        result = build_launch_options(RETRODECK_INVOCATION, path)
        assert shlex.split(result, posix=True) == [
            "flatpak",
            "run",
            "net.retrodeck.retrodeck",
            path,
        ]

    def test_override_invocation_is_preserved_unescaped(self):
        invocation = resolve_emulator_invocation({"id": 1}, EmulatorInvocation.libretro("mgba"))
        path = '/roms/gba/Game".gba'
        result = build_launch_options(invocation, path)
        # The invocation's own -e "..." quoting is part of the trusted prefix and
        # is NOT escaped — it survives verbatim in the launch string.
        assert '-e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/mgba.so %ROM%"' in result
        # The path still round-trips to a single final argv token under shlex.
        assert shlex.split(result, posix=True)[-1] == path


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

    def test_installed_rom_with_no_launch_target_gets_empty_launch_options(self):
        # The install resolved to the empty path (launchable is False, #1652). It
        # is still IN the map — it IS downloaded — but its shortcut carries the
        # same empty launch command an un-downloaded ROM's does, never a command
        # composed around a bare "".
        roms = [{"id": 1, "name": "Sealed In A PKG"}]
        result = build_shortcuts_data(roms, "/plugin", {1: ""}, {})
        assert result[0]["launch_options"] == ""

    def test_no_launch_target_beats_a_core_override(self):
        # An emulator override cannot resurrect a launch command for content the
        # system cannot boot — no ``-e`` form is composed either.
        roms = [{"id": 1, "name": "Sealed In A PKG"}]
        overrides = {1: EmulatorInvocation.libretro("pcsx_rearmed_libretro")}
        result = build_shortcuts_data(roms, "/plugin", {1: ""}, overrides)
        assert result[0]["launch_options"] == ""

    def test_installed_rom_with_core_override_bakes_e_form(self):
        # A rom_id present in core_overrides bakes the -e override into its launch.
        roms = [{"id": 1, "name": "PSX Game"}]
        result = build_shortcuts_data(
            roms, "/plugin", {1: "/roms/psx/game.chd"}, {1: EmulatorInvocation.libretro("pcsx_rearmed_libretro")}
        )
        assert result[0]["launch_options"] == (
            "flatpak run net.retrodeck.retrodeck "
            '-e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/pcsx_rearmed_libretro.so %ROM%" '
            '"/roms/psx/game.chd"'
        )

    def test_installed_rom_with_standalone_override_bakes_e_form(self):
        # A standalone emulator override bakes its verbatim ES-DE command into -e.
        roms = [{"id": 1, "name": "PS3 Game"}]
        result = build_shortcuts_data(
            roms,
            "/plugin",
            {1: "/roms/ps3/game/PS3_GAME/USRDIR/EBOOT.BIN"},
            {1: EmulatorInvocation.standalone("%EMULATOR_RPCS3% --no-gui %ROM%")},
        )
        assert result[0]["launch_options"] == (
            'flatpak run net.retrodeck.retrodeck -e "%EMULATOR_RPCS3% --no-gui %ROM%" '
            '"/roms/ps3/game/PS3_GAME/USRDIR/EBOOT.BIN"'
        )

    def test_installed_rom_absent_from_overrides_is_plain(self):
        # A rom_id NOT in core_overrides follows the default — plain launch, no -e.
        roms = [{"id": 1, "name": "Plain"}]
        result = build_shortcuts_data(
            roms, "/plugin", {1: "/roms/n64/g.z64"}, {2: EmulatorInvocation.libretro("other_libretro")}
        )
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/n64/g.z64"'
        assert "-e" not in result[0]["launch_options"]

    def test_uninstalled_rom_with_override_still_empty(self):
        # An override on an UNINSTALLED rom can't bake — no path, empty placeholder.
        roms = [{"id": 1, "name": "NotDownloaded"}]
        result = build_shortcuts_data(roms, "/plugin", {}, {1: EmulatorInvocation.libretro("pcsx_rearmed_libretro")})
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

    def test_carries_fs_size_bytes_from_raw_rom(self):
        # The server-reported size (#1395) rides the built dict onto the commit.
        roms = [{"id": 1, "name": "Game A", "fs_size_bytes": 3_145_728}]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["fs_size_bytes"] == 3_145_728

    def test_missing_fs_size_bytes_is_none(self):
        # A raw ROM without the key builds None — "size unknown".
        roms = [{"id": 1, "name": "Game A"}]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["fs_size_bytes"] is None

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


class TestBuildShortcutsDataVersionMetadata:
    """build_shortcuts_data derives the sibling-group key + version dimensions (#1295)."""

    def test_carries_group_key_and_version_dimensions(self):
        roms = [
            {
                "id": 1,
                "name": "Game A",
                "platform_id": 57,
                "igdb_id": 3404,
                "regions": ["USA", "Europe"],
                "languages": ["En", "Fr"],
                "revision": "1",
                "tags": ["Demo"],
                "rom_user": {"is_main_sibling": True},
            }
        ]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["sibling_group_key"] == "igdb:3404:57"
        assert result[0]["regions"] == ["USA", "Europe"]
        assert result[0]["languages"] == ["En", "Fr"]
        assert result[0]["revision"] == "1"
        assert result[0]["tags"] == ["Demo"]
        assert result[0]["is_main_sibling"] is True

    def test_unmatched_rom_gets_fallback_group_key(self):
        roms = [{"id": 4409, "name": "Solo", "platform_id": 57}]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["sibling_group_key"] == "romm:4409:57"

    def test_missing_version_fields_default_empty(self):
        # A ROM with no version metadata at all: empty arrays, blank revision,
        # is_main_sibling False (rom_user absent → the `or {}` guard).
        roms = [{"id": 5, "name": "Minimal", "platform_id": 9}]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["regions"] == []
        assert result[0]["languages"] == []
        assert result[0]["revision"] == ""
        assert result[0]["tags"] == []
        assert result[0]["is_main_sibling"] is False

    def test_null_rom_user_is_not_main_sibling(self):
        # Defensive guard: a missing or null rom_user (whatever the server
        # schema promises) must degrade to False without raising.
        roms = [{"id": 6, "name": "Untouched", "platform_id": 9, "rom_user": None}]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["is_main_sibling"] is False

    def test_null_version_arrays_default_empty(self):
        # RomM can send explicit nulls for the array dimensions.
        roms = [
            {
                "id": 7,
                "name": "Nulls",
                "platform_id": 9,
                "regions": None,
                "languages": None,
                "tags": None,
                "revision": None,
            }
        ]
        result = build_shortcuts_data(roms, "/plugin", {}, {})
        assert result[0]["regions"] == []
        assert result[0]["languages"] == []
        assert result[0]["tags"] == []
        assert result[0]["revision"] == ""

    def test_persisted_group_key_is_authoritative_over_recompute(self):
        # #1296: an incremental-skip reconstructed row carries the persisted
        # sibling_group_key but NO platform_id — recomputing it would yield
        # "igdb:100:None" and split the group's bucket from a freshly-fetched
        # sibling that DOES carry platform_id. The persisted key must win verbatim.
        reconstructed = {"id": 10, "name": "Zelda (USA)", "igdb_id": 100, "sibling_group_key": "igdb:100:57"}
        result = build_shortcuts_data([reconstructed], "/plugin", {}, {})
        assert result[0]["sibling_group_key"] == "igdb:100:57"

    def test_reconstructed_and_fetched_sibling_land_in_one_bucket(self):
        # The end-to-end #1296 regression: a reconstructed representative (persisted
        # key, no platform_id) and a freshly fetched sibling of the same game
        # (platform_id + igdb_id, no key) must resolve to the SAME group key so the
        # preview collapse buckets them as ONE game — not a phantom "new".
        from domain.sync_diff import collapse_sibling_groups

        reconstructed = {"id": 10, "name": "Zelda (USA)", "sibling_group_key": "igdb:100:57"}
        fetched = {"id": 11, "name": "Zelda (JP)", "igdb_id": 100, "platform_id": 57}
        result = build_shortcuts_data([reconstructed, fetched], "/plugin", {}, {})
        assert {sd["sibling_group_key"] for sd in result} == {"igdb:100:57"}

        emitted = collapse_sibling_groups(result, registry={}, installed_rom_ids=set(), complete_group_view=True)
        assert len(emitted) == 1  # one bucket → one representative
