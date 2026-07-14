"""Tests for domain/emulator_commands — the bakeability classification kernel.

The command strings are sampled verbatim from a live RetroDECK
``es_systems.xml`` (linux/ flavor) so the rules are pinned against the real
shapes the plugin must classify: plain + env-prefixed emulators, the ``%INJECT%``
and OS-shell forms, the MAME quoting/``\\;`` templates, and the ``%STARTDIR%``
prefix. A handful of synthetic strings cover branches the current es_systems
does not exercise (an unknown ``%PLACEHOLDER%``, a lone double-quote).
"""

from __future__ import annotations

import dataclasses
from typing import ClassVar

import pytest

from domain.emulator_commands import (
    EmulatorOption,
    classify_command,
    downgrade_if_not_installed,
    label_to_invocation,
    option_to_invocation,
    options_to_payload,
    select_default_option,
)
from domain.shortcut_data import EmulatorInvocation

# --- Real command strings (sampled from a live es_systems.xml) --------------

PSX_SWANSTATION = "%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/swanstation_libretro.so %ROM%"
GC_DOLPHIN_STANDALONE = "env QT_QPA_PLATFORM=xcb %EMULATOR_DOLPHIN% -b -e %ROM%"
PS2_PCSX2_BATCH = "%EMULATOR_PCSX2% -batch %ROM%"
PS3_RPCS3_DIRECTORY = "%EMULATOR_RPCS3% --no-gui %ROM%"
PS3_RPCS3_SHORTCUT = "%ENABLESHORTCUTS% %EMULATOR_OS-SHELL% %ROM%"
PS3_RPCS3_GAME_SERIAL = "%EMULATOR_RPCS3% --no-gui %RPCS3_GAMEID%:%INJECT%=%BASENAME%.ps3"
XEMU_INJECT_WITH_ROM = "%INJECT%=%BASENAME%.esprefix %EMULATOR_XEMU% -dvd_path %ROM%"
VITA3K_INJECT_NO_ROM = "%EMULATOR_VITA3K% -r %INJECT%=%BASENAME%.psvita"
ARCADE_MAME_STANDALONE = (
    "%EMULATOR_MAME% -inipath /var/config/mame/ini -rompath %GAMEDIR%\\;%ROMPATH%/arcade %BASENAME%"
)
APPLE2_MAME_LIBRETRO_QUOTED = (
    "%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mame_libretro.so "
    '"apple2e -rompath \\"%GAMEDIRRAW%;%ROMPATH%/apple2\\" -gameio joy -flop1 \\"%GAMEDIRRAW%/%FILENAME%\\""'
)
ARCHIMEDES_MAME_BACKSLASH_SEMI = (
    "%EMULATOR_MAME% -inipath /var/config/mame/ini -rompath %GAMEDIR%\\;%ROMPATH%/archimedes aa4401 -flop1 %ROM%"
)
BBCMICRO_MAME_QUOTED = (
    "%STARTDIR%=~/.mame %EMULATOR_MAME% -rompath %GAMEDIR%\\;%ROMPATH%/bbcmicro bbcb "
    '-autoboot_delay "2" -autoboot_command "*cat\\n\\n*exec !boot\\n" -analogue acornjoy -flop1 %ROM%'
)
PC98_NEKOP2_STARTDIR = "%STARTDIR%=%GAMEDIR% %EMULATOR_RETROARCH% -L %CORE_RETROARCH%/nekop2_libretro.so %ROM%"
PICO8_STANDALONE = "%EMULATOR_PICO-8% -root_path %GAMEDIR% -run %ROM%"

# --- Synthetic strings for branches the live es_systems does not hit ---------

SYNTH_UNKNOWN_PLACEHOLDER = "%EMULATOR_FOO% --port %MYSTERY_TOKEN% %ROM%"
SYNTH_LONE_QUOTE = '%EMULATOR_FOO% --title "game" %ROM%'
SYNTH_TRAILING_DOT = "%EMULATOR_DREAMM% %GAMEDIR% ."


class TestClassifyCommand:
    """Each real/synthetic command classifies to exactly one status + reason."""

    @pytest.mark.parametrize(
        ("label", "text", "status", "reason", "kind", "core_so"),
        [
            ("SwanStation", PSX_SWANSTATION, "bakeable", None, "libretro", "swanstation_libretro"),
            ("Dolphin (Standalone)", GC_DOLPHIN_STANDALONE, "bakeable", None, "standalone", None),
            ("PCSX2 (Standalone)", PS2_PCSX2_BATCH, "bakeable", None, "standalone", None),
            ("RPCS3 Directory (Standalone)", PS3_RPCS3_DIRECTORY, "bakeable", None, "standalone", None),
            ("PICO-8 (Standalone)", PICO8_STANDALONE, "bakeable", None, "standalone", None),
            ("RPCS3 Shortcut (Standalone)", PS3_RPCS3_SHORTCUT, "unbakeable", "shortcut_script", "standalone", None),
            ("RPCS3 Game Serial (Standalone)", PS3_RPCS3_GAME_SERIAL, "needs_setup", "inject", "standalone", None),
            ("xemu (Standalone)", XEMU_INJECT_WITH_ROM, "needs_setup", "inject", "standalone", None),
            ("Vita3K (Standalone)", VITA3K_INJECT_NO_ROM, "needs_setup", "inject", "standalone", None),
            ("MAME (Standalone)", ARCADE_MAME_STANDALONE, "unbakeable", "no_rom_target", "standalone", None),
            ("MAME - Current", APPLE2_MAME_LIBRETRO_QUOTED, "unbakeable", "no_rom_target", "standalone", None),
            ("MAME [A440/1]", ARCHIMEDES_MAME_BACKSLASH_SEMI, "unbakeable", "quoting", "standalone", None),
            ("MAME (Standalone)", BBCMICRO_MAME_QUOTED, "unbakeable", "quoting", "standalone", None),
            ("Neko Project II", PC98_NEKOP2_STARTDIR, "unbakeable", "startdir", "standalone", None),
            ("Foo", SYNTH_UNKNOWN_PLACEHOLDER, "unbakeable", "unknown_placeholder", "standalone", None),
            ("Foo", SYNTH_LONE_QUOTE, "unbakeable", "quoting", "standalone", None),
            ("DREAMM", SYNTH_TRAILING_DOT, "unbakeable", "no_rom_target", "standalone", None),
        ],
    )
    def test_classification(self, label, text, status, reason, kind, core_so):
        option = classify_command(label, text)
        assert option.label == label
        assert option.status == status
        assert option.reason == reason
        assert option.kind == kind
        assert option.core_so == core_so
        assert option.command == text.strip()

    def test_inject_wins_over_missing_rom_target(self):
        """A ``%INJECT%`` command with no ``%ROM%`` is needs-setup, not no_rom_target."""
        option = classify_command("Vita3K", VITA3K_INJECT_NO_ROM)
        assert option.status == "needs_setup"
        assert option.reason == "inject"

    def test_startdir_beats_unknown_placeholder(self):
        """``%STARTDIR%`` is a known placeholder — it surfaces its own reason."""
        option = classify_command("Neko Project II", PC98_NEKOP2_STARTDIR)
        assert option.reason == "startdir"

    def test_env_prefixed_command_is_bakeable_standalone(self):
        option = classify_command("Dolphin (Standalone)", GC_DOLPHIN_STANDALONE)
        assert option.status == "bakeable"
        assert option.kind == "standalone"


class TestDowngradeIfNotInstalled:
    """The pure half of the standalone existence probe (ADR-0020)."""

    def test_bakeable_standalone_missing_becomes_needs_setup(self):
        option = classify_command("Ryubing (Standalone)", "%EMULATOR_RYUBING% %ROM%")
        result = downgrade_if_not_installed(option, emulator_installed=False)
        assert result.status == "needs_setup"
        assert result.reason == "not_installed"
        # Identity fields survive the downgrade.
        assert result.label == "Ryubing (Standalone)"
        assert result.kind == "standalone"
        assert result.command == "%EMULATOR_RYUBING% %ROM%"

    def test_bakeable_standalone_installed_unchanged(self):
        option = classify_command("PCSX2 (Standalone)", PS2_PCSX2_BATCH)
        assert downgrade_if_not_installed(option, emulator_installed=True) == option

    def test_libretro_never_downgraded_even_when_flagged_missing(self):
        # RetroArch ships with RetroDECK — a libretro option is always installed,
        # and the kind guard means the missing flag is ignored regardless.
        option = classify_command("SwanStation", PSX_SWANSTATION)
        assert downgrade_if_not_installed(option, emulator_installed=False) == option

    def test_already_needs_setup_unchanged(self):
        option = classify_command("Vita3K", VITA3K_INJECT_NO_ROM)
        assert downgrade_if_not_installed(option, emulator_installed=False) == option

    def test_already_unbakeable_unchanged(self):
        option = classify_command("RPCS3 Shortcut (Standalone)", PS3_RPCS3_SHORTCUT)
        assert downgrade_if_not_installed(option, emulator_installed=False) == option

    def test_downgraded_option_drops_out_of_default_selection(self):
        # A system whose only bakeable command is a missing standalone resolves to
        # no default → the caller plain-launches (the pre-standalone behavior).
        missing = downgrade_if_not_installed(
            classify_command("Ryubing (Standalone)", "%EMULATOR_RYUBING% %ROM%"),
            emulator_installed=False,
        )
        assert select_default_option([missing]) is None
        assert label_to_invocation([missing], "Ryubing (Standalone)") is None

    def test_downgraded_option_reads_disabled_with_reason_in_payload(self):
        missing = downgrade_if_not_installed(
            classify_command("Ryubing (Standalone)", "%EMULATOR_RYUBING% %ROM%"),
            emulator_installed=False,
        )
        payload = options_to_payload([missing])
        assert payload[0]["bakeable"] is False
        assert payload[0]["reason"] == "not_installed"
        assert payload[0]["is_default"] is False


class TestSelectDefaultOption:
    def test_first_bakeable_wins_over_earlier_unbakeable(self):
        """The ps3 chain: Shortcut (script) → Game Serial (inject) → Directory (bakeable)."""
        options = [
            classify_command("RPCS3 Shortcut (Standalone)", PS3_RPCS3_SHORTCUT),
            classify_command("RPCS3 Game Serial (Standalone)", PS3_RPCS3_GAME_SERIAL),
            classify_command("RPCS3 Directory (Standalone)", PS3_RPCS3_DIRECTORY),
            classify_command("RPCS3 ISO (Standalone)", PS3_RPCS3_DIRECTORY),
        ]
        default = select_default_option(options)
        assert default is not None
        assert default.label == "RPCS3 Directory (Standalone)"

    def test_libretro_default_when_first_in_order(self):
        options = [classify_command("SwanStation", PSX_SWANSTATION)]
        default = select_default_option(options)
        assert default is not None
        assert default.core_so == "swanstation_libretro"

    def test_none_when_nothing_bakeable(self):
        options = [
            classify_command("MAME - Current", APPLE2_MAME_LIBRETRO_QUOTED),
            classify_command("MAME (Standalone)", ARCADE_MAME_STANDALONE),
        ]
        assert select_default_option(options) is None

    def test_empty_options_returns_none(self):
        assert select_default_option([]) is None


class TestOptionToInvocation:
    def test_libretro_option_renders_libretro_invocation(self):
        option = classify_command("SwanStation", PSX_SWANSTATION)
        assert option_to_invocation(option) == EmulatorInvocation.libretro("swanstation_libretro", "SwanStation")

    def test_standalone_option_bakes_command_verbatim(self):
        option = classify_command("PCSX2 (Standalone)", PS2_PCSX2_BATCH)
        assert option_to_invocation(option) == EmulatorInvocation.standalone(PS2_PCSX2_BATCH, "PCSX2 (Standalone)")

    def test_unbakeable_option_returns_none(self):
        option = classify_command("RPCS3 Shortcut (Standalone)", PS3_RPCS3_SHORTCUT)
        assert option_to_invocation(option) is None

    def test_needs_setup_option_returns_none(self):
        option = classify_command("Vita3K", VITA3K_INJECT_NO_ROM)
        assert option_to_invocation(option) is None

    def test_none_option_returns_none(self):
        assert option_to_invocation(None) is None


class TestLabelToInvocation:
    OPTIONS: ClassVar[list[EmulatorOption]] = [
        classify_command("RPCS3 Shortcut (Standalone)", PS3_RPCS3_SHORTCUT),
        classify_command("RPCS3 Directory (Standalone)", PS3_RPCS3_DIRECTORY),
    ]

    def test_bakeable_label_resolves(self):
        result = label_to_invocation(self.OPTIONS, "RPCS3 Directory (Standalone)")
        assert result == EmulatorInvocation.standalone(PS3_RPCS3_DIRECTORY, "RPCS3 Directory (Standalone)")

    def test_unbakeable_label_returns_none(self):
        assert label_to_invocation(self.OPTIONS, "RPCS3 Shortcut (Standalone)") is None

    def test_unknown_label_returns_none(self):
        assert label_to_invocation(self.OPTIONS, "Not A Real Emulator") is None


class TestOptionsToPayload:
    def test_marks_single_default_and_bakeability(self):
        options = [
            classify_command("RPCS3 Shortcut (Standalone)", PS3_RPCS3_SHORTCUT),
            classify_command("RPCS3 Game Serial (Standalone)", PS3_RPCS3_GAME_SERIAL),
            classify_command("RPCS3 Directory (Standalone)", PS3_RPCS3_DIRECTORY),
        ]
        payload = options_to_payload(options)
        assert payload == [
            {
                "label": "RPCS3 Shortcut (Standalone)",
                "kind": "standalone",
                "core_so": None,
                "is_default": False,
                "bakeable": False,
                "reason": "shortcut_script",
            },
            {
                "label": "RPCS3 Game Serial (Standalone)",
                "kind": "standalone",
                "core_so": None,
                "is_default": False,
                "bakeable": False,
                "reason": "inject",
            },
            {
                "label": "RPCS3 Directory (Standalone)",
                "kind": "standalone",
                "core_so": None,
                "is_default": True,
                "bakeable": True,
                "reason": None,
            },
        ]

    def test_libretro_payload_carries_core_so(self):
        payload = options_to_payload([classify_command("SwanStation", PSX_SWANSTATION)])
        assert payload[0]["kind"] == "libretro"
        assert payload[0]["core_so"] == "swanstation_libretro"
        assert payload[0]["is_default"] is True

    def test_no_default_when_none_bakeable(self):
        payload = options_to_payload([classify_command("MAME (Standalone)", ARCADE_MAME_STANDALONE)])
        assert all(entry["is_default"] is False for entry in payload)

    def test_command_text_not_leaked(self):
        payload = options_to_payload([classify_command("PCSX2 (Standalone)", PS2_PCSX2_BATCH)])
        assert "command" not in payload[0]


class TestClassificationInvariants:
    """Property-style checks over the sampled command corpus."""

    CORPUS: ClassVar[list[str]] = [
        PSX_SWANSTATION,
        GC_DOLPHIN_STANDALONE,
        PS2_PCSX2_BATCH,
        PS3_RPCS3_DIRECTORY,
        PS3_RPCS3_SHORTCUT,
        PS3_RPCS3_GAME_SERIAL,
        XEMU_INJECT_WITH_ROM,
        VITA3K_INJECT_NO_ROM,
        ARCADE_MAME_STANDALONE,
        APPLE2_MAME_LIBRETRO_QUOTED,
        ARCHIMEDES_MAME_BACKSLASH_SEMI,
        BBCMICRO_MAME_QUOTED,
        PC98_NEKOP2_STARTDIR,
        PICO8_STANDALONE,
        SYNTH_UNKNOWN_PLACEHOLDER,
        SYNTH_LONE_QUOTE,
        SYNTH_TRAILING_DOT,
    ]

    _VALID_STATUSES: ClassVar[set[str]] = {"bakeable", "needs_setup", "unbakeable"}
    _VALID_REASONS: ClassVar[set[str | None]] = {
        None,
        "inject",
        "shortcut_script",
        "no_rom_target",
        "quoting",
        "startdir",
        "unknown_placeholder",
    }

    @pytest.mark.parametrize("text", CORPUS)
    def test_exactly_one_valid_status_and_reason(self, text):
        option = classify_command("L", text)
        assert option.status in self._VALID_STATUSES
        assert option.reason in self._VALID_REASONS
        # A bakeable option never carries a reason; a non-bakeable one always does.
        assert (option.reason is None) == (option.status == "bakeable")

    @pytest.mark.parametrize("text", CORPUS)
    def test_classification_is_deterministic(self, text):
        first = classify_command("L", text)
        second = classify_command("L", text)
        assert first == second

    def test_select_default_returns_none_or_bakeable(self):
        options = [classify_command(f"L{i}", text) for i, text in enumerate(self.CORPUS)]
        default = select_default_option(options)
        assert default is None or default.status == "bakeable"

    def test_selected_default_renders_an_invocation(self):
        options = [classify_command("SwanStation", PSX_SWANSTATION), classify_command("Q", SYNTH_LONE_QUOTE)]
        default = select_default_option(options)
        assert isinstance(option_to_invocation(default), EmulatorInvocation)


def test_emulator_option_is_frozen():
    option = EmulatorOption(label="x", kind="standalone", core_so=None, command="%ROM%", status="bakeable", reason=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        option.label = "y"  # type: ignore[misc]
