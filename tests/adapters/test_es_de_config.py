"""Tests for adapters/es_de_config — CoreResolver and GamelistXmlEditorAdapter."""

import logging
import os
import tempfile
from typing import Any, ClassVar
from unittest import mock

import pytest

from adapters import es_de_config as es_de_config_mod
from adapters.es_de_config import CoreResolver, GamelistXmlEditorAdapter

# conftest.py patches decky before this import.
# main.py adds py_modules to sys.path (provides vdf, etc.).
from main import Plugin  # noqa: F401

_TEST_LOGGER = logging.getLogger("test_es_de")


def _make_resolver(get_retrodeck_home=None) -> CoreResolver:
    plugin_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return CoreResolver(
        plugin_dir=plugin_dir,
        logger=_TEST_LOGGER,
        get_retrodeck_home=get_retrodeck_home,
    )


def _make_editor() -> GamelistXmlEditorAdapter:
    return GamelistXmlEditorAdapter(logger=_TEST_LOGGER)


@pytest.fixture
def resolver() -> CoreResolver:
    return _make_resolver()


@pytest.fixture
def editor() -> GamelistXmlEditorAdapter:
    return _make_editor()


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

SAMPLE_GAMELIST_WITH_OVERRIDE = """\
<?xml version="1.0"?>
<gameList>
  <alternativeEmulator>
    <label>gpSP</label>
  </alternativeEmulator>
</gameList>
"""

SAMPLE_GAMELIST_NO_OVERRIDE = """\
<?xml version="1.0"?>
<gameList>
  <game>
    <path>./some_game.gba</path>
    <name>Some Game</name>
  </game>
</gameList>
"""


def _write_temp_xml(content):
    """Write content to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


class TestFindEsSystemsXml:
    @mock.patch("adapters.es_de_config.os.path.exists")
    def test_finds_xml_in_linux_path(self, mock_exists):
        mock_exists.return_value = True
        result = CoreResolver.find_es_systems_xml()
        assert result is not None
        assert result == es_de_config_mod._ES_SYSTEMS_CANDIDATES[0]
        assert "linux" in result

    @mock.patch("adapters.es_de_config.os.path.exists")
    def test_falls_back_to_unix_path(self, mock_exists):
        # linux/ doesn't exist, unix/ does
        mock_exists.side_effect = [False, True]
        result = CoreResolver.find_es_systems_xml()
        assert result is not None
        assert result == es_de_config_mod._ES_SYSTEMS_CANDIDATES[1]
        assert "unix" in result

    @mock.patch("adapters.es_de_config.os.path.exists")
    def test_returns_none_when_not_found(self, mock_exists):
        mock_exists.return_value = False
        result = CoreResolver.find_es_systems_xml()
        assert result is None


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


class TestReadSystemOverride:
    """Tests for ``CoreResolver._read_system_override`` (reads alternativeEmulator label)."""

    def test_no_gamelist_returns_none(self, resolver):
        result = resolver._read_system_override("/nonexistent/path", "gba")
        assert result is None

    def test_gamelist_with_alternative_emulator(self, resolver):
        with tempfile.TemporaryDirectory() as tmpdir:
            gamelist_dir = os.path.join(tmpdir, "ES-DE", "gamelists", "gba")
            os.makedirs(gamelist_dir)
            gamelist_path = os.path.join(gamelist_dir, "gamelist.xml")
            with open(gamelist_path, "w") as f:
                f.write(SAMPLE_GAMELIST_WITH_OVERRIDE)

            result = resolver._read_system_override(tmpdir, "gba")
            assert result == "gpSP"

    def test_gamelist_without_override_returns_none(self, resolver):
        with tempfile.TemporaryDirectory() as tmpdir:
            gamelist_dir = os.path.join(tmpdir, "ES-DE", "gamelists", "gba")
            os.makedirs(gamelist_dir)
            gamelist_path = os.path.join(gamelist_dir, "gamelist.xml")
            with open(gamelist_path, "w") as f:
                f.write(SAMPLE_GAMELIST_NO_OVERRIDE)

            result = resolver._read_system_override(tmpdir, "gba")
            assert result is None

    def test_malformed_gamelist_returns_none(self, resolver):
        with tempfile.TemporaryDirectory() as tmpdir:
            gamelist_dir = os.path.join(tmpdir, "ES-DE", "gamelists", "gba")
            os.makedirs(gamelist_dir)
            gamelist_path = os.path.join(gamelist_dir, "gamelist.xml")
            with open(gamelist_path, "w") as f:
                f.write("this is garbage not xml {{{")

            result = resolver._read_system_override(tmpdir, "gba")
            assert result is None


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
        resolver = _make_resolver(get_retrodeck_home=lambda: "/fake/retrodeck")
        with (
            mock.patch.object(CoreResolver, "_load_es_systems", return_value=self.GBA_SYSTEM_INFO),
            mock.patch.object(CoreResolver, "_read_system_override", return_value=None),
        ):
            result = resolver.get_active_core("gba")
        assert result == ("mgba_libretro", "mGBA")

    def test_system_override_takes_precedence(self):
        resolver = _make_resolver(get_retrodeck_home=lambda: "/fake/retrodeck")
        with (
            mock.patch.object(CoreResolver, "_load_es_systems", return_value=self.GBA_SYSTEM_INFO),
            mock.patch.object(CoreResolver, "_read_system_override", return_value="gpSP"),
        ):
            result = resolver.get_active_core("gba")
        assert result == ("gpsp_libretro", "gpSP")

    def test_fallback_to_core_defaults(self):
        resolver = _make_resolver(get_retrodeck_home=lambda: None)
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
        resolver = _make_resolver(get_retrodeck_home=lambda: None)
        with (
            mock.patch.object(CoreResolver, "_load_es_systems", return_value={}),
            mock.patch.object(CoreResolver, "_load_core_defaults", return_value={}),
        ):
            result = resolver.get_active_core("gba")
        assert result == (None, None)

    def test_unknown_system_returns_none(self):
        resolver = _make_resolver(get_retrodeck_home=lambda: None)
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


class TestSetSystemOverride:
    def test_creates_new_gamelist(self, editor, resolver):
        with tempfile.TemporaryDirectory() as tmpdir:
            editor.set_system_override(tmpdir, "gba", "gpSP")
            result = resolver._read_system_override(tmpdir, "gba")
            assert result == "gpSP"

    def test_updates_existing_gamelist_preserves_games(self, editor, resolver):
        with tempfile.TemporaryDirectory() as tmpdir:
            gamelist_dir = os.path.join(tmpdir, "ES-DE", "gamelists", "gba")
            os.makedirs(gamelist_dir)
            gamelist_path = os.path.join(gamelist_dir, "gamelist.xml")
            with open(gamelist_path, "w") as f:
                f.write(SAMPLE_GAMELIST_NO_OVERRIDE)

            editor.set_system_override(tmpdir, "gba", "gpSP")

            # Override should be set
            result = resolver._read_system_override(tmpdir, "gba")
            assert result == "gpSP"

            # Game entry should be preserved
            with open(gamelist_path) as f:
                content = f.read()
            assert "some_game.gba" in content

    def test_clears_override(self, editor, resolver):
        with tempfile.TemporaryDirectory() as tmpdir:
            # First set an override
            editor.set_system_override(tmpdir, "gba", "gpSP")
            assert resolver._read_system_override(tmpdir, "gba") == "gpSP"

            # Clear it
            editor.set_system_override(tmpdir, "gba", None)
            assert resolver._read_system_override(tmpdir, "gba") is None

    def test_replaces_existing_override(self, editor, resolver):
        with tempfile.TemporaryDirectory() as tmpdir:
            gamelist_dir = os.path.join(tmpdir, "ES-DE", "gamelists", "gba")
            os.makedirs(gamelist_dir)
            gamelist_path = os.path.join(gamelist_dir, "gamelist.xml")
            with open(gamelist_path, "w") as f:
                f.write(SAMPLE_GAMELIST_WITH_OVERRIDE)

            editor.set_system_override(tmpdir, "gba", "VBA-M")
            result = resolver._read_system_override(tmpdir, "gba")
            assert result == "VBA-M"


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
