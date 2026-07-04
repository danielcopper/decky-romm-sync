"""Tests for adapters/es_de_config — CoreResolver (system-layer core resolution)."""

import logging
import os
import tempfile
from typing import ClassVar
from unittest import mock

import pytest

from adapters.es_de_config import CoreResolver, _emulator_token
from domain.shortcut_data import EmulatorInvocation

# conftest.py patches decky before this import.
# main.py adds py_modules to sys.path (provides vdf, etc.).
from main import Plugin  # noqa: F401

_TEST_LOGGER = logging.getLogger("test_es_de")


def _make_resolver(user_home: str = "/nonexistent/home") -> CoreResolver:
    plugin_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return CoreResolver(
        plugin_dir=plugin_dir,
        logger=_TEST_LOGGER,
        user_home=user_home,
    )


@pytest.fixture
def resolver() -> CoreResolver:
    return _make_resolver()


# --- Helpers ---

SAMPLE_ES_SYSTEMS_XML = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>gba</name>
    <command label="mGBA">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mgba_libretro.so %ROM%</command>
    <command label="gpSP">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/gpsp_libretro.so %ROM%</command>
    <command label="VBA-M">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/vbam_libretro.so %ROM%</command>
    <command label="mGBA Standalone">%EMULATOR_MGBA% %ROM%</command>
  </system>
  <system>
    <name>snes</name>
    <command label="Snes9x">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/snes9x_libretro.so %ROM%</command>
    <command label="bsnes">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/bsnes_libretro.so %ROM%</command>
  </system>
</systemList>
"""


def _write_temp_xml(content):
    """Write content to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _es_systems_path(files_dir, *, flavor: str) -> str:
    """Build the ``es_systems.xml`` path for *flavor* (``linux``/``unix``) under
    a flatpak app ``files`` dir."""
    return os.path.join(
        files_dir,
        "retrodeck",
        "components",
        "es-de",
        "share",
        "es-de",
        "resources",
        "systems",
        flavor,
        "es_systems.xml",
    )


def _user_files_dir(user_home):
    """The per-user flatpak app ``files`` dir for the RetroDECK app under *user_home*."""
    return (
        user_home / ".local" / "share" / "flatpak" / "app" / "net.retrodeck.retrodeck" / "current" / "active" / "files"
    )


class TestFindEsSystemsXml:
    @pytest.fixture(autouse=True)
    def _isolate_system_root(self, tmp_path):
        """Point the shared system flatpak root at a non-existent tmp location so
        tests only see files placed under the per-user root."""
        with mock.patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(tmp_path / "nonexistent_system_root")):
            yield

    def test_finds_xml_in_linux_path(self, tmp_path):
        files_dir = _user_files_dir(tmp_path)
        linux_path = _es_systems_path(str(files_dir), flavor="linux")
        os.makedirs(os.path.dirname(linux_path))
        with open(linux_path, "w") as f:
            f.write(SAMPLE_ES_SYSTEMS_XML)

        resolver = _make_resolver(user_home=str(tmp_path))
        result = resolver.find_es_systems_xml()
        assert result == linux_path
        assert result is not None
        assert "linux" in result

    def test_falls_back_to_unix_path(self, tmp_path):
        # Only unix/ exists under the per-user root.
        files_dir = _user_files_dir(tmp_path)
        unix_path = _es_systems_path(str(files_dir), flavor="unix")
        os.makedirs(os.path.dirname(unix_path))
        with open(unix_path, "w") as f:
            f.write(SAMPLE_ES_SYSTEMS_XML)

        resolver = _make_resolver(user_home=str(tmp_path))
        result = resolver.find_es_systems_xml()
        assert result == unix_path
        assert result is not None
        assert "unix" in result

    def test_returns_none_when_not_found(self, tmp_path):
        resolver = _make_resolver(user_home=str(tmp_path))
        assert resolver.find_es_systems_xml() is None


class TestParseEsSystems:
    def test_parses_system_with_retroarch_cores(self, resolver):
        path = _write_temp_xml(SAMPLE_ES_SYSTEMS_XML)
        try:
            result = resolver.parse_es_systems(path)
            assert "gba" in result
            gba = result["gba"]
            assert gba["default_core"] == "mgba_libretro"
            assert gba["default_label"] == "mGBA"
            assert gba["cores"] == {
                "mgba_libretro": "mGBA",
                "gpsp_libretro": "gpSP",
                "vbam_libretro": "VBA-M",
            }
            assert gba["label_to_core"] == {
                "mGBA": "mgba_libretro",
                "gpSP": "gpsp_libretro",
                "VBA-M": "vbam_libretro",
            }
        finally:
            os.unlink(path)

    def test_first_retroarch_command_is_default(self, resolver):
        path = _write_temp_xml(SAMPLE_ES_SYSTEMS_XML)
        try:
            result = resolver.parse_es_systems(path)
            snes = result["snes"]
            assert snes["default_core"] == "snes9x_libretro"
            assert snes["default_label"] == "Snes9x"
        finally:
            os.unlink(path)

    def test_standalone_emulators_excluded(self, resolver):
        path = _write_temp_xml(SAMPLE_ES_SYSTEMS_XML)
        try:
            result = resolver.parse_es_systems(path)
            gba = result["gba"]
            # "mGBA Standalone" should NOT be in cores (no %CORE_RETROARCH%)
            assert "mGBA Standalone" not in gba["label_to_core"]
            assert len(gba["cores"]) == 3  # only the 3 RetroArch cores
        finally:
            os.unlink(path)

    def test_invalid_xml_returns_empty(self, resolver):
        path = _write_temp_xml("this is not xml at all {{{")
        try:
            result = resolver.parse_es_systems(path)
            assert result == {}
        finally:
            os.unlink(path)

    def test_wrong_root_tag_returns_empty(self, resolver):
        path = _write_temp_xml('<?xml version="1.0"?><wrongTag><system><name>gba</name></system></wrongTag>')
        try:
            result = resolver.parse_es_systems(path)
            assert result == {}
        finally:
            os.unlink(path)

    def test_system_with_only_standalone_cores(self, resolver):
        xml = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>switch</name>
    <command label="Yuzu">%EMULATOR_YUZU% %ROM%</command>
    <command label="Ryujinx">%EMULATOR_RYUJINX% %ROM%</command>
  </system>
</systemList>
"""
        path = _write_temp_xml(xml)
        try:
            result = resolver.parse_es_systems(path)
            assert "switch" in result
            assert result["switch"]["default_core"] is None
            assert result["switch"]["default_label"] is None
            assert result["switch"]["cores"] == {}
        finally:
            os.unlink(path)

    def test_label_to_core_mapping(self, resolver):
        path = _write_temp_xml(SAMPLE_ES_SYSTEMS_XML)
        try:
            result = resolver.parse_es_systems(path)
            gba = result["gba"]
            # Verify label -> core_so reverse mapping
            assert gba["label_to_core"]["mGBA"] == "mgba_libretro"
            assert gba["label_to_core"]["gpSP"] == "gpsp_libretro"
            assert gba["label_to_core"]["VBA-M"] == "vbam_libretro"
        finally:
            os.unlink(path)

    def test_every_command_captured_including_standalone(self, resolver):
        # The standalone seam (#129): ``commands`` records EVERY <command> by
        # label, libretro AND standalone — even the ones excluded from ``cores``.
        path = _write_temp_xml(SAMPLE_ES_SYSTEMS_XML)
        try:
            result = resolver.parse_es_systems(path)
            gba = result["gba"]
            assert set(gba["commands"]) == {"mGBA", "gpSP", "VBA-M", "mGBA Standalone"}
            # The standalone command text is preserved verbatim for the bake.
            assert gba["commands"]["mGBA Standalone"] == "%EMULATOR_MGBA% %ROM%"
        finally:
            os.unlink(path)

    def test_standalone_first_keeps_libretro_default_core(self, resolver):
        # ``commands`` records EVERY <command> in document order (standalone
        # first here), while the libretro ``default_core`` capture stays the
        # first LIBRETRO command regardless of a preceding standalone.
        xml = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>ps2</name>
    <command label="PCSX2 (Standalone)">%EMULATOR_PCSX2% -batch %ROM%</command>
    <command label="LRPS2">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/pcsx2_libretro.so %ROM%</command>
  </system>
</systemList>
"""
        path = _write_temp_xml(xml)
        try:
            result = resolver.parse_es_systems(path)
            ps2 = result["ps2"]
            assert list(ps2["commands"]) == ["PCSX2 (Standalone)", "LRPS2"]
            assert ps2["default_core"] == "pcsx2_libretro"
            assert ps2["commands"]["PCSX2 (Standalone)"] == "%EMULATOR_PCSX2% -batch %ROM%"
        finally:
            os.unlink(path)


# A realistic multi-platform excerpt mirroring RetroDECK's shipped es_systems.xml
# (linux/). The first RetroArch %CORE_RETROARCH% command per system is the
# es_systems default; standalone emulators (no %CORE_RETROARCH%) are excluded.
GOLDEN_ES_SYSTEMS_XML = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>psx</name>
    <fullname>Sony PlayStation</fullname>
    <command label="SwanStation">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/swanstation_libretro.so %ROM%</command>
    <command label="Beetle PSX">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mednafen_psx_libretro.so %ROM%</command>
    <command label="DuckStation">%EMULATOR_DUCKSTATION% %ROM%</command>
  </system>
  <system>
    <name>gba</name>
    <fullname>Nintendo Game Boy Advance</fullname>
    <command label="mGBA">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mgba_libretro.so %ROM%</command>
    <command label="gpSP">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/gpsp_libretro.so %ROM%</command>
  </system>
  <system>
    <name>snes</name>
    <fullname>Nintendo SNES</fullname>
    <command label="Snes9x - Current">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/snes9x_libretro.so %ROM%</command>
    <command label="bsnes">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/bsnes_libretro.so %ROM%</command>
  </system>
  <system>
    <name>n64</name>
    <fullname>Nintendo 64</fullname>
    <command label="Mupen64Plus-Next">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mupen64plus_next_libretro.so</command>
    <command label="ParaLLEl N64">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/parallel_n64_libretro.so %ROM%</command>
  </system>
  <system>
    <name>megadrive</name>
    <fullname>Sega Mega Drive</fullname>
    <command label="Genesis Plus GX">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/genesis_plus_gx_libretro.so</command>
    <command label="BlastEm">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/blastem_libretro.so %ROM%</command>
  </system>
  <system>
    <name>gbc</name>
    <fullname>Nintendo Game Boy Color</fullname>
    <command label="Gambatte">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/gambatte_libretro.so %ROM%</command>
  </system>
  <system>
    <name>nes</name>
    <fullname>Nintendo Entertainment System</fullname>
    <command label="Mesen">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mesen_libretro.so %ROM%</command>
    <command label="Nestopia">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/nestopia_libretro.so %ROM%</command>
  </system>
  <system>
    <name>gb</name>
    <fullname>Nintendo Game Boy</fullname>
    <command label="Gambatte">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/gambatte_libretro.so %ROM%</command>
  </system>
</systemList>
"""


class TestGoldenEsSystems:
    """Lock-in for a realistic multi-platform es_systems.xml parse.

    Asserts the parsed ``default_core``/``default_label`` (the es_systems default,
    i.e. the first RetroArch command per system) for the platforms the plugin
    cares most about, plus the available-cores enumeration.
    """

    EXPECTED_DEFAULTS: ClassVar[dict[str, tuple[str, str]]] = {
        "psx": ("swanstation_libretro", "SwanStation"),
        "gba": ("mgba_libretro", "mGBA"),
        "snes": ("snes9x_libretro", "Snes9x - Current"),
        "n64": ("mupen64plus_next_libretro", "Mupen64Plus-Next"),
        "megadrive": ("genesis_plus_gx_libretro", "Genesis Plus GX"),
        "gbc": ("gambatte_libretro", "Gambatte"),
        "nes": ("mesen_libretro", "Mesen"),
        "gb": ("gambatte_libretro", "Gambatte"),
    }

    def test_parses_default_core_and_label_per_platform(self, resolver):
        path = _write_temp_xml(GOLDEN_ES_SYSTEMS_XML)
        try:
            parsed = resolver.parse_es_systems(path)
        finally:
            os.unlink(path)
        for system, (core_so, label) in self.EXPECTED_DEFAULTS.items():
            assert system in parsed, f"missing system {system}"
            assert parsed[system]["default_core"] == core_so
            assert parsed[system]["default_label"] == label

    def test_get_active_core_returns_es_systems_default(self, resolver):
        path = _write_temp_xml(GOLDEN_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                for system, (core_so, label) in self.EXPECTED_DEFAULTS.items():
                    assert resolver.get_active_core(system) == (core_so, label)
        finally:
            os.unlink(path)

    def test_standalone_emulators_excluded_from_available_cores(self, resolver):
        path = _write_temp_xml(GOLDEN_ES_SYSTEMS_XML)
        try:
            parsed = resolver.parse_es_systems(path)
        finally:
            os.unlink(path)
        # psx had a standalone DuckStation command — only the two RetroArch cores remain.
        psx_labels = set(parsed["psx"]["label_to_core"].keys())
        assert psx_labels == {"SwanStation", "Beetle PSX"}


# An es_systems.xml excerpt exercising the classification branches for
# get_default_emulator / get_emulator_options: a plain libretro system (gba), a
# standalone-first system whose libretro core comes second (gc: env-prefixed
# Dolphin Standalone selected over dolphin_libretro), and a system whose first
# commands are un-bakeable so the first bakeable wins (ps3: shortcut → inject →
# directory) plus one with nothing bakeable (apple2: quoted libretro + MAME).
RULE_ES_SYSTEMS_XML = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>gba</name>
    <command label="mGBA">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mgba_libretro.so %ROM%</command>
    <command label="VBA-M">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/vbam_libretro.so %ROM%</command>
  </system>
  <system>
    <name>gc</name>
    <command label="Dolphin (Standalone)">env QT_QPA_PLATFORM=xcb %EMULATOR_DOLPHIN% -b -e %ROM%</command>
    <command label="Dolphin">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/dolphin_libretro.so %ROM%</command>
  </system>
  <system>
    <name>ps3</name>
    <command label="RPCS3 Shortcut (Standalone)">%ENABLESHORTCUTS% %EMULATOR_OS-SHELL% %ROM%</command>
    <command label="RPCS3 Serial (Standalone)">%EMULATOR_RPCS3% --no-gui %INJECT%=%BASENAME%.ps3</command>
    <command label="RPCS3 Directory (Standalone)">%EMULATOR_RPCS3% --no-gui %ROM%</command>
  </system>
  <system>
    <name>apple2</name>
    <command label="MAME - Current">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mame_libretro.so "apple2 %ROM%"</command>
    <command label="MAME (Standalone)">%STARTDIR%=~/.mame %EMULATOR_MAME% -rompath %GAMEDIR%\\;%ROMPATH% %ROM%</command>
  </system>
</systemList>
"""


class TestGetActiveCore:
    """``get_active_core`` returns the first LIBRETRO command (BIOS filter)."""

    def test_default_core_is_first_libretro_command(self, resolver):
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.get_active_core("gba") == ("mgba_libretro", "mGBA")
                # gc's FIRST command is the standalone Dolphin, but the libretro
                # active core stays the first %CORE_RETROARCH% command.
                assert resolver.get_active_core("gc") == ("dolphin_libretro", "Dolphin")
        finally:
            os.unlink(path)

    def test_returns_none_when_es_systems_absent(self):
        resolver = _make_resolver()
        with mock.patch.object(CoreResolver, "_load_es_systems", return_value={}):
            assert resolver.get_active_core("gba") == (None, None)

    def test_unknown_system_returns_none(self, resolver):
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.get_active_core("totally_unknown_system") == (None, None)
        finally:
            os.unlink(path)

    def test_standalone_only_system_has_no_active_core(self, resolver):
        # ps3 carries no bakeable libretro command → (None, None) for the BIOS filter.
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.get_active_core("ps3") == (None, None)
        finally:
            os.unlink(path)


class TestGetDefaultEmulator:
    """The default emulator is the first *safely-bakeable* command (live only)."""

    def test_plain_libretro_system_selects_first_core(self, resolver):
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.get_default_emulator("gba") == EmulatorInvocation.libretro("mgba_libretro", "mGBA")
        finally:
            os.unlink(path)

    def test_standalone_first_selected_over_later_libretro(self, resolver):
        # gc: the env-prefixed Dolphin Standalone is bakeable and comes first, so
        # it wins over the later dolphin_libretro core (the gc/wii save-mover flip).
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.get_default_emulator("gc") == EmulatorInvocation.standalone(
                    "env QT_QPA_PLATFORM=xcb %EMULATOR_DOLPHIN% -b -e %ROM%", "Dolphin (Standalone)"
                )
        finally:
            os.unlink(path)

    def test_first_bakeable_wins_over_earlier_unbakeable(self, resolver):
        # ps3: Shortcut (script) → Game Serial (inject) → Directory (bakeable).
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.get_default_emulator("ps3") == EmulatorInvocation.standalone(
                    "%EMULATOR_RPCS3% --no-gui %ROM%", "RPCS3 Directory (Standalone)"
                )
        finally:
            os.unlink(path)

    def test_nothing_bakeable_returns_none(self, resolver):
        # apple2: quoted libretro (no_rom_target) + MAME standalone (quoting) →
        # nothing bakeable → plain launch (the caller lets RetroDECK resolve it).
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.get_default_emulator("apple2") is None
        finally:
            os.unlink(path)

    def test_returns_none_when_es_systems_absent(self):
        resolver = _make_resolver()
        with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=None):
            assert resolver.get_default_emulator("gba") is None


class TestGetEmulatorOptions:
    """``get_emulator_options`` returns the classified, document-ordered list."""

    def test_available_and_ordered_with_default_marked(self, resolver):
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result = resolver.get_emulator_options("ps3")
        finally:
            os.unlink(path)
        assert result["available"] is True
        labels = [o.label for o in result["options"]]
        # Document order preserved.
        assert labels == [
            "RPCS3 Shortcut (Standalone)",
            "RPCS3 Serial (Standalone)",
            "RPCS3 Directory (Standalone)",
        ]
        statuses = {o.label: o.status for o in result["options"]}
        assert statuses["RPCS3 Shortcut (Standalone)"] == "unbakeable"
        assert statuses["RPCS3 Serial (Standalone)"] == "needs_setup"
        assert statuses["RPCS3 Directory (Standalone)"] == "bakeable"

    def test_unavailable_when_es_systems_absent(self):
        resolver = _make_resolver()
        with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=None):
            result = resolver.get_emulator_options("gba")
        assert result == {"available": False, "options": []}

    def test_unavailable_when_parse_fails(self, resolver):
        # A corrupt es_systems.xml parses to {} → unavailable, no fallback data.
        path = _write_temp_xml("this is not xml {{{")
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result = resolver.get_emulator_options("gba")
        finally:
            os.unlink(path)
        assert result == {"available": False, "options": []}

    def test_available_but_empty_for_unknown_system(self, resolver):
        # es_systems readable but the system has no commands → available, empty.
        path = _write_temp_xml(RULE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result = resolver.get_emulator_options("totally_unknown_system")
        finally:
            os.unlink(path)
        assert result == {"available": True, "options": []}


# An es_systems.xml excerpt carrying <extension> lists: psx WITH .m3u (disc
# system), switch WITHOUT .m3u (Switch's emulator can't read a playlist).
EXTENSION_ES_SYSTEMS_XML = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>psx</name>
    <fullname>Sony PlayStation</fullname>
    <extension>.cue .CUE .chd .CHD .m3u .M3U</extension>
    <command label="SwanStation">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/swanstation_libretro.so %ROM%</command>
  </system>
  <system>
    <name>switch</name>
    <fullname>Nintendo Switch</fullname>
    <extension>.nsp .NSP .xci .XCI</extension>
    <command label="Yuzu">%EMULATOR_YUZU% %ROM%</command>
  </system>
</systemList>
"""


class TestSystemSupportsM3u:
    """``system_supports_m3u`` reads ES-DE's own ``<extension>`` list."""

    def test_psx_supports_m3u(self, resolver):
        path = _write_temp_xml(EXTENSION_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.system_supports_m3u("psx") is True
        finally:
            os.unlink(path)

    def test_switch_does_not_support_m3u(self, resolver):
        path = _write_temp_xml(EXTENSION_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.system_supports_m3u("switch") is False
        finally:
            os.unlink(path)

    def test_unknown_system_returns_false(self, resolver):
        path = _write_temp_xml(EXTENSION_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.system_supports_m3u("totally_unknown") is False
        finally:
            os.unlink(path)

    def test_default_safe_false_when_es_systems_absent(self, resolver):
        """es_systems.xml cannot be found → default-safe False (no playlist)."""
        with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=None):
            assert resolver.system_supports_m3u("psx") is False

    def test_extension_match_is_case_insensitive(self, resolver):
        """A system whose list carries only uppercase ``.M3U`` still matches."""
        xml = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>segacd</name>
    <extension>.CUE .CHD .M3U</extension>
    <command label="GX">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/genesis_plus_gx_libretro.so %ROM%</command>
  </system>
</systemList>
"""
        path = _write_temp_xml(xml)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                assert resolver.system_supports_m3u("segacd") is True
        finally:
            os.unlink(path)

    def test_extensions_parsed_into_system_entry(self, resolver):
        """The parser captures ``<extension>`` tokens (lowercased) per system."""
        path = _write_temp_xml(EXTENSION_ES_SYSTEMS_XML)
        try:
            parsed = resolver.parse_es_systems(path)
        finally:
            os.unlink(path)
        assert parsed["psx"]["extensions"] == {".cue", ".chd", ".m3u"}
        assert parsed["switch"]["extensions"] == {".nsp", ".xci"}


class TestGetSupportedExtensions:
    """``get_supported_extensions`` returns ES-DE's per-system ``<extension>`` set."""

    def test_known_system_returns_lowercased_frozenset(self, resolver):
        path = _write_temp_xml(EXTENSION_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result = resolver.get_supported_extensions("psx")
        finally:
            os.unlink(path)
        assert result == frozenset({".cue", ".chd", ".m3u"})
        assert isinstance(result, frozenset)

    def test_other_known_system_returns_its_own_set(self, resolver):
        path = _write_temp_xml(EXTENSION_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result = resolver.get_supported_extensions("switch")
        finally:
            os.unlink(path)
        assert result == frozenset({".nsp", ".xci"})

    def test_unknown_system_returns_empty_frozenset(self, resolver):
        path = _write_temp_xml(EXTENSION_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result = resolver.get_supported_extensions("totally_unknown")
        finally:
            os.unlink(path)
        assert result == frozenset()

    def test_empty_when_es_systems_absent(self, resolver):
        """es_systems.xml cannot be found → empty (caller falls back to full disc set)."""
        with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=None):
            assert resolver.get_supported_extensions("psx") == frozenset()

    def test_extensions_are_lowercased_case_insensitively(self, resolver):
        """A mixed/uppercase ``<extension>`` list is returned lowercased."""
        xml = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>segacd</name>
    <extension>.CUE .CHD .M3U</extension>
    <command label="GX">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/genesis_plus_gx_libretro.so %ROM%</command>
  </system>
</systemList>
"""
        path = _write_temp_xml(xml)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result = resolver.get_supported_extensions("segacd")
        finally:
            os.unlink(path)
        assert result == frozenset({".cue", ".chd", ".m3u"})


class TestMtimeInvalidation:
    """Caches invalidate when underlying files change on disk."""

    def test_es_systems_reloads_on_mtime_change(self, resolver):
        """``_load_es_systems`` should re-parse if es_systems.xml mtime changes."""
        xml_v1 = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>gba</name>
    <command label="mGBA">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mgba_libretro.so %ROM%</command>
  </system>
</systemList>
"""
        xml_v2 = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>gba</name>
    <command label="mGBA">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mgba_libretro.so %ROM%</command>
    <command label="gpSP">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/gpsp_libretro.so %ROM%</command>
  </system>
</systemList>
"""
        path = _write_temp_xml(xml_v1)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result1 = resolver._load_es_systems()
                assert len(result1["gba"]["cores"]) == 1

                # Overwrite file (changes mtime)
                import time

                time.sleep(0.05)  # ensure mtime differs
                with open(path, "w") as f:
                    f.write(xml_v2)

                result2 = resolver._load_es_systems()
                assert len(result2["gba"]["cores"]) == 2
        finally:
            os.unlink(path)

    def test_es_systems_cache_hit_when_unchanged(self, resolver):
        """``_load_es_systems`` should return cached result if mtime unchanged."""
        path = _write_temp_xml(SAMPLE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result1 = resolver._load_es_systems()
                result2 = resolver._load_es_systems()
                # Same object reference means cache was used
                assert result1 is result2
        finally:
            os.unlink(path)

    def test_reset_cache_forces_reparse(self, resolver):
        """``reset_cache`` drops the cached parse so the next read re-reads disk."""
        path = _write_temp_xml(SAMPLE_ES_SYSTEMS_XML)
        try:
            with mock.patch.object(CoreResolver, "find_es_systems_xml", return_value=path):
                result1 = resolver._load_es_systems()
                resolver.reset_cache()
                result2 = resolver._load_es_systems()
                # Different object after a reset — the cache was invalidated.
                assert result1 is not result2
                # ...but the parsed content is equivalent (same file).
                assert result1 == result2
        finally:
            os.unlink(path)


# es_systems with three standalone-default systems whose emulators the probe
# classifies differently: switch (Ryubing — RetroDECK component), psp (PPSSPP —
# RetroDECK component), and atari8 (Atari800 — systempath-only, unverifiable).
PROBE_ES_SYSTEMS_XML = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>switch</name>
    <command label="Ryubing (Standalone)">%EMULATOR_RYUBING% %ROM%</command>
  </system>
  <system>
    <name>psp</name>
    <command label="PPSSPP (Standalone)">%EMULATOR_PPSSPP% -b %ROM%</command>
    <command label="PPSSPP">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/ppsspp_libretro.so %ROM%</command>
  </system>
  <system>
    <name>atari8</name>
    <command label="Atari800 (Standalone)">%EMULATOR_ATARI800% %ROM%</command>
  </system>
  <system>
    <name>flash</name>
    <command label="Ruffle (Standalone)">%EMULATOR_RUFFLE% %ROM%</command>
  </system>
</systemList>
"""

# es_find_rules mixing the shapes the probe must reason about: a RetroDECK
# component with both a bundled (/app) and an external (/var/data) staticpath
# (RYUBING, PPSSPP, RUFFLE), and a systempath-only emulator with no staticpath at
# all (ATARI800 — unverifiable from outside the sandbox → assumed installed).
PROBE_ES_FIND_RULES_XML = """\
<?xml version="1.0"?>
<ruleList>
  <emulator name="RYUBING">
    <rule type="systempath">
      <entry>ryubing</entry>
    </rule>
    <rule type="staticpath">
      <entry>/app/retrodeck/components/ryubing/component_launcher.sh</entry>
      <entry>/var/data/retrodeck/external_components/ryubing/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="PPSSPP">
    <rule type="staticpath">
      <entry>/app/retrodeck/components/ppsspp/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="RUFFLE">
    <rule type="staticpath">
      <entry>/app/retrodeck/components/ruffle/component_launcher.sh</entry>
      <entry>/var/data/retrodeck/external_components/ruffle/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="ATARI800">
    <rule type="systempath">
      <entry>atari800</entry>
    </rule>
  </emulator>
</ruleList>
"""


def _es_find_rules_path(files_dir, *, flavor: str) -> str:
    """``es_find_rules.xml`` path beside ``es_systems.xml`` for *flavor*."""
    return os.path.join(os.path.dirname(_es_systems_path(files_dir, flavor=flavor)), "es_find_rules.xml")


def _component_launcher(files_dir, component: str) -> str:
    """Bundled RetroDECK component launcher path under the flatpak files tree."""
    return os.path.join(files_dir, "retrodeck", "components", component, "component_launcher.sh")


def _external_component_launcher(user_home, component: str) -> str:
    """User-installed external RetroDECK component launcher (sandbox ``/var/data``)."""
    return os.path.join(
        user_home,
        ".var",
        "app",
        "net.retrodeck.retrodeck",
        "data",
        "retrodeck",
        "external_components",
        component,
        "component_launcher.sh",
    )


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")


class TestStandaloneInstalledProbe:
    """``get_emulator_options`` downgrades bakeable standalones whose emulator is absent.

    Exercises the real ``find_es_systems_xml`` → ``find_es_find_rules_xml`` →
    on-disk probe path over a fabricated per-user flatpak tree (no mocking of the
    resolution seams), so the sandbox ``/app`` and ``/var/data`` prefix mappings
    are validated against real files.
    """

    @pytest.fixture(autouse=True)
    def _isolate_system_root(self, tmp_path):
        with mock.patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(tmp_path / "nonexistent_system_root")):
            yield

    def _seed(self, tmp_path, *, installed_components=(), external_components=()):
        """Lay down es_systems.xml + es_find_rules.xml and the named component launchers."""
        files_dir = str(_user_files_dir(tmp_path))
        linux_systems = _es_systems_path(files_dir, flavor="linux")
        os.makedirs(os.path.dirname(linux_systems))
        with open(linux_systems, "w") as f:
            f.write(PROBE_ES_SYSTEMS_XML)
        with open(_es_find_rules_path(files_dir, flavor="linux"), "w") as f:
            f.write(PROBE_ES_FIND_RULES_XML)
        for component in installed_components:
            _touch(_component_launcher(files_dir, component))
        for component in external_components:
            _touch(_external_component_launcher(str(tmp_path), component))
        return _make_resolver(user_home=str(tmp_path))

    def _status(self, resolver, system, label):
        option = next(o for o in resolver.get_emulator_options(system)["options"] if o.label == label)
        return option.status, option.reason

    def test_missing_retrodeck_component_downgrades_to_not_installed(self, tmp_path):
        # Only ppsspp is installed; ryubing's bundled + external component are both absent.
        resolver = self._seed(tmp_path, installed_components=["ppsspp"])
        assert self._status(resolver, "switch", "Ryubing (Standalone)") == ("needs_setup", "not_installed")

    def test_switch_default_falls_back_to_plain_launch(self, tmp_path):
        # switch's only bakeable command is the missing Ryubing → no default → plain launch.
        resolver = self._seed(tmp_path, installed_components=["ppsspp"])
        assert resolver.get_default_emulator("switch") is None

    def test_installed_bundled_component_stays_bakeable(self, tmp_path):
        resolver = self._seed(tmp_path, installed_components=["ppsspp"])
        assert self._status(resolver, "psp", "PPSSPP (Standalone)") == ("bakeable", None)
        assert resolver.get_default_emulator("psp") == EmulatorInvocation.standalone(
            "%EMULATOR_PPSSPP% -b %ROM%", "PPSSPP (Standalone)"
        )

    def test_external_component_counts_as_installed(self, tmp_path):
        # ryubing installed as a user external component under /var/data → installed.
        resolver = self._seed(tmp_path, installed_components=["ppsspp"], external_components=["ryubing"])
        assert self._status(resolver, "switch", "Ryubing (Standalone)") == ("bakeable", None)

    def test_systempath_only_emulator_assumed_installed(self, tmp_path):
        # ATARI800 has no staticpath rule — unverifiable from outside the sandbox,
        # so it is never downgraded (the probe only acts on positive absence).
        resolver = self._seed(tmp_path)
        assert self._status(resolver, "atari8", "Atari800 (Standalone)") == ("bakeable", None)

    def test_libretro_option_never_downgraded(self, tmp_path):
        # psp's libretro command stays bakeable regardless of standalone probing.
        resolver = self._seed(tmp_path)
        assert self._status(resolver, "psp", "PPSSPP") == ("bakeable", None)

    def test_absent_find_rules_leaves_everything_installed(self, tmp_path):
        # es_find_rules.xml missing → cannot disprove → no downgrade (additive probe).
        files_dir = str(_user_files_dir(tmp_path))
        linux_systems = _es_systems_path(files_dir, flavor="linux")
        os.makedirs(os.path.dirname(linux_systems))
        with open(linux_systems, "w") as f:
            f.write(PROBE_ES_SYSTEMS_XML)
        resolver = _make_resolver(user_home=str(tmp_path))
        assert resolver.find_es_find_rules_xml() is None
        assert self._status(resolver, "switch", "Ryubing (Standalone)") == ("bakeable", None)

    def test_find_rules_resolved_beside_es_systems(self, tmp_path):
        resolver = self._seed(tmp_path)
        found = resolver.find_es_find_rules_xml()
        assert found is not None
        assert found.endswith(os.path.join("systems", "linux", "es_find_rules.xml"))

    def test_reset_cache_reprobes_after_component_appears(self, tmp_path):
        # A downgraded standalone flips back to bakeable once the component is
        # installed and the cache is reset (the mtime guard is per-file; reset is
        # the eager path a per-platform write already takes).
        resolver = self._seed(tmp_path)
        assert self._status(resolver, "switch", "Ryubing (Standalone)") == ("needs_setup", "not_installed")
        _touch(_component_launcher(str(_user_files_dir(tmp_path)), "ryubing"))
        resolver.reset_cache()
        assert self._status(resolver, "switch", "Ryubing (Standalone)") == ("bakeable", None)


class TestEmulatorToken:
    """``_emulator_token`` extracts the find-rule name from a command."""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("%EMULATOR_RYUBING% %ROM%", "RYUBING"),
            ("env QT_QPA_PLATFORM=xcb %EMULATOR_DOLPHIN% -b -e %ROM%", "DOLPHIN"),
            ("%EMULATOR_PICO-8% -root_path %GAMEDIR% -run %ROM%", "PICO-8"),
            ("%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/swanstation_libretro.so %ROM%", "RETROARCH"),
            ("no token here %ROM%", None),
        ],
    )
    def test_token_extraction(self, command, expected):
        assert _emulator_token(command) == expected


# es_systems for the sandbox-launcher tests: find_es_find_rules_xml resolves the
# rules file as the es_systems sibling, so a findable es_systems.xml must exist.
_SANDBOX_ES_SYSTEMS_XML = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>ps3</name>
    <command label="RPCS3 Directory (Standalone)">%EMULATOR_RPCS3% --no-gui %ROM%</command>
  </system>
</systemList>
"""

# Mirrors the real RPCS3 rule's shape — host AppImage + host flatpak export
# entries listed BEFORE the /app RetroDECK component launcher — plus emulators
# exercising the /var-only, host-only, |command-suffixed, both-/app-and-/var, and
# systempath-only branches.
_SANDBOX_ES_FIND_RULES_XML = """\
<?xml version="1.0"?>
<ruleList>
  <emulator name="RPCS3">
    <rule type="systempath">
      <entry>rpcs3</entry>
    </rule>
    <rule type="staticpath">
      <entry>~/Applications/rpcs3*.AppImage</entry>
      <entry>/var/lib/flatpak/exports/bin/net.rpcs3.RPCS3</entry>
      <entry>/app/retrodeck/components/rpcs3/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="BOTHAPPVAR">
    <rule type="staticpath">
      <entry>/var/data/retrodeck/external_components/bothappvar/component_launcher.sh</entry>
      <entry>/app/retrodeck/components/bothappvar/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="EXTONLY">
    <rule type="staticpath">
      <entry>/var/data/retrodeck/external_components/extonly/component_launcher.sh</entry>
    </rule>
  </emulator>
  <emulator name="HOSTONLY">
    <rule type="staticpath">
      <entry>~/Applications/hostonly*.AppImage</entry>
    </rule>
  </emulator>
  <emulator name="PIPED">
    <rule type="staticpath">
      <entry>/app/retrodeck/components/piped/component_launcher.sh|--flag %ROM%</entry>
    </rule>
  </emulator>
  <emulator name="NOSTATIC">
    <rule type="systempath">
      <entry>nostatic</entry>
    </rule>
  </emulator>
</ruleList>
"""


class TestResolveSandboxLauncher:
    """``resolve_sandbox_launcher`` picks a standalone command's sandbox launcher.

    Drives the real ``find_es_find_rules_xml`` → parse path over a fabricated
    per-user flatpak tree; the method returns the sandbox-absolute RetroDECK
    component ``staticpath`` verbatim (no on-disk existence check — the default /
    pin resolution already gated installedness).
    """

    @pytest.fixture(autouse=True)
    def _isolate_system_root(self, tmp_path):
        with mock.patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(tmp_path / "nonexistent_system_root")):
            yield

    def _seed(self, tmp_path, *, find_rules: str | None = _SANDBOX_ES_FIND_RULES_XML):
        files_dir = str(_user_files_dir(tmp_path))
        linux_systems = _es_systems_path(files_dir, flavor="linux")
        os.makedirs(os.path.dirname(linux_systems))
        with open(linux_systems, "w") as f:
            f.write(_SANDBOX_ES_SYSTEMS_XML)
        if find_rules is not None:
            with open(_es_find_rules_path(files_dir, flavor="linux"), "w") as f:
                f.write(find_rules)
        return _make_resolver(user_home=str(tmp_path))

    def test_rpcs3_resolves_app_component_over_host_entries(self, tmp_path):
        resolver = self._seed(tmp_path)
        assert (
            resolver.resolve_sandbox_launcher("%EMULATOR_RPCS3% --no-gui %ROM%")
            == "/app/retrodeck/components/rpcs3/component_launcher.sh"
        )

    def test_prefers_app_over_var_data_component(self, tmp_path):
        resolver = self._seed(tmp_path)
        assert (
            resolver.resolve_sandbox_launcher("%EMULATOR_BOTHAPPVAR% %ROM%")
            == "/app/retrodeck/components/bothappvar/component_launcher.sh"
        )

    def test_var_data_external_component_resolves(self, tmp_path):
        resolver = self._seed(tmp_path)
        assert (
            resolver.resolve_sandbox_launcher("%EMULATOR_EXTONLY% %ROM%")
            == "/var/data/retrodeck/external_components/extonly/component_launcher.sh"
        )

    def test_host_only_staticpaths_yield_none(self, tmp_path):
        resolver = self._seed(tmp_path)
        assert resolver.resolve_sandbox_launcher("%EMULATOR_HOSTONLY% %ROM%") is None

    def test_pipe_suffixed_entry_is_stripped(self, tmp_path):
        resolver = self._seed(tmp_path)
        assert (
            resolver.resolve_sandbox_launcher("%EMULATOR_PIPED% %ROM%")
            == "/app/retrodeck/components/piped/component_launcher.sh"
        )

    def test_systempath_only_emulator_yields_none(self, tmp_path):
        resolver = self._seed(tmp_path)
        assert resolver.resolve_sandbox_launcher("%EMULATOR_NOSTATIC% %ROM%") is None

    def test_unknown_emulator_token_yields_none(self, tmp_path):
        resolver = self._seed(tmp_path)
        assert resolver.resolve_sandbox_launcher("%EMULATOR_UNKNOWN% %ROM%") is None

    def test_command_without_emulator_token_yields_none(self, tmp_path):
        resolver = self._seed(tmp_path)
        assert resolver.resolve_sandbox_launcher("just some text %ROM%") is None

    def test_missing_find_rules_yields_none(self, tmp_path):
        resolver = self._seed(tmp_path, find_rules=None)
        assert resolver.resolve_sandbox_launcher("%EMULATOR_RPCS3% --no-gui %ROM%") is None
