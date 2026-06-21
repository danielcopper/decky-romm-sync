"""Tests for adapters/es_de_config — CoreResolver (system-layer core resolution)."""

import logging
import os
import tempfile
from typing import Any, ClassVar
from unittest import mock

import pytest

from adapters.es_de_config import CoreResolver

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


class TestGetActiveCore:
    GBA_SYSTEM_INFO: ClassVar[dict[str, Any]] = {
        "gba": {
            "default_core": "mgba_libretro",
            "default_label": "mGBA",
            "cores": {
                "mgba_libretro": "mGBA",
                "gpsp_libretro": "gpSP",
                "vbam_libretro": "VBA-M",
            },
            "label_to_core": {
                "mGBA": "mgba_libretro",
                "gpSP": "gpsp_libretro",
                "VBA-M": "vbam_libretro",
            },
        }
    }

    def test_default_core_from_live_xml(self):
        resolver = _make_resolver()
        with mock.patch.object(CoreResolver, "_load_es_systems", return_value=self.GBA_SYSTEM_INFO):
            result = resolver.get_active_core("gba")
        assert result == ("mgba_libretro", "mGBA")

    def test_fallback_to_core_defaults(self):
        resolver = _make_resolver()
        defaults = {
            "gba": {
                "default_core": "mgba_libretro",
                "default_label": "mGBA",
                "cores": {"mgba_libretro": "mGBA"},
            }
        }
        with (
            mock.patch.object(CoreResolver, "_load_es_systems", return_value={}),
            mock.patch.object(CoreResolver, "_load_core_defaults", return_value=defaults),
        ):
            result = resolver.get_active_core("gba")
        assert result == ("mgba_libretro", "mGBA")

    def test_returns_none_when_all_fail(self):
        resolver = _make_resolver()
        with (
            mock.patch.object(CoreResolver, "_load_es_systems", return_value={}),
            mock.patch.object(CoreResolver, "_load_core_defaults", return_value={}),
        ):
            result = resolver.get_active_core("gba")
        assert result == (None, None)

    def test_unknown_system_returns_none(self):
        resolver = _make_resolver()
        with (
            mock.patch.object(CoreResolver, "_load_es_systems", return_value=self.GBA_SYSTEM_INFO),
            mock.patch.object(CoreResolver, "_load_core_defaults", return_value={}),
        ):
            result = resolver.get_active_core("totally_unknown_system")
        assert result == (None, None)


class TestGetAvailableCores:
    GBA_SYSTEM_INFO: ClassVar[dict[str, Any]] = {
        "gba": {
            "default_core": "mgba_libretro",
            "default_label": "mGBA",
            "cores": {
                "mgba_libretro": "mGBA",
                "gpsp_libretro": "gpSP",
                "vbam_libretro": "VBA-M",
            },
            "label_to_core": {
                "mGBA": "mgba_libretro",
                "gpSP": "gpsp_libretro",
                "VBA-M": "vbam_libretro",
            },
        }
    }

    def test_returns_cores_from_live_xml(self, resolver):
        with mock.patch.object(CoreResolver, "_load_es_systems", return_value=self.GBA_SYSTEM_INFO):
            result = resolver.get_available_cores("gba")
        assert len(result) == 3
        labels = [c["label"] for c in result]
        assert "mGBA" in labels
        assert "gpSP" in labels
        assert "VBA-M" in labels
        # Check is_default
        default = [c for c in result if c["is_default"]]
        assert len(default) == 1
        assert default[0]["core_so"] == "mgba_libretro"

    def test_falls_back_to_core_defaults(self, resolver):
        defaults = {
            "gba": {
                "default_core": "mgba_libretro",
                "default_label": "mGBA",
                "cores": {"mgba_libretro": "mGBA", "gpsp_libretro": "gpSP"},
            }
        }
        with (
            mock.patch.object(CoreResolver, "_load_es_systems", return_value={}),
            mock.patch.object(CoreResolver, "_load_core_defaults", return_value=defaults),
        ):
            result = resolver.get_available_cores("gba")
        assert len(result) == 2

    def test_unknown_system_returns_empty(self, resolver):
        with (
            mock.patch.object(CoreResolver, "_load_es_systems", return_value={}),
            mock.patch.object(CoreResolver, "_load_core_defaults", return_value={}),
        ):
            result = resolver.get_available_cores("unknown_system")
        assert result == []


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
