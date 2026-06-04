"""Tests for domain.save_extensions pure functions."""

from __future__ import annotations

from unittest.mock import patch

from domain.save_extensions import get_save_extensions

_DEFAULTS = (".srm", ".rtc", ".sav")


class TestGetSaveExtensionsDefault:
    """get_save_extensions returns defaults when no override exists."""

    def test_no_argument_returns_default(self):
        result = get_save_extensions()
        assert result == _DEFAULTS

    def test_none_argument_returns_default(self):
        result = get_save_extensions(None)
        assert result == _DEFAULTS

    def test_known_platform_without_override_returns_default(self):
        """A real platform slug with no override returns the default."""
        result = get_save_extensions("gba")
        assert result == _DEFAULTS

    def test_unknown_platform_returns_default(self):
        result = get_save_extensions("unknown_platform")
        assert result == _DEFAULTS


class TestGetSaveExtensionsWithOverride:
    """get_save_extensions respects per-system overrides (keys are RetroDECK systems)."""

    def test_nds_override_includes_dsv(self):
        """NDS system returns DeSmuME .dsv extension."""
        result = get_save_extensions("nds")
        assert ".dsv" in result
        assert ".srm" in result
        assert ".sav" in result

    def test_segacd_override_includes_brm(self):
        """Sega CD system returns Genesis Plus GX .brm extension."""
        result = get_save_extensions("segacd")
        assert ".brm" in result
        assert ".srm" in result

    def test_saturn_override_appends_backup_ram_extensions(self):
        """Saturn returns Beetle Saturn / yabasanshiro backup RAM extensions, defaults retained."""
        result = get_save_extensions("saturn")
        assert result == (".srm", ".rtc", ".sav", ".bkr", ".bcr", ".smpc")

    def test_ngp_override_appends_flash_and_ngf(self):
        """NGP returns Beetle NeoPop (.flash) and RACE (.ngf) extensions, defaults retained."""
        result = get_save_extensions("ngp")
        assert result == (".srm", ".rtc", ".sav", ".flash", ".ngf")

    def test_ngpc_matches_ngp(self):
        """The ngpc system returns the same tuple as ngp."""
        assert get_save_extensions("ngpc") == get_save_extensions("ngp")

    def test_pokemini_override_appends_eep(self):
        """PokeMini returns the EEPROM .eep extension, defaults retained."""
        result = get_save_extensions("pokemini")
        assert result == (".srm", ".rtc", ".sav", ".eep")

    def test_amiga_override_appends_nvr(self):
        """Amiga returns the PUAE .nvr extension, defaults retained."""
        result = get_save_extensions("amiga")
        assert result == (".srm", ".rtc", ".sav", ".nvr")

    def test_amigacd32_override_appends_nvr(self):
        """AmigaCD32 returns the PUAE .nvr extension, defaults retained."""
        result = get_save_extensions("amigacd32")
        assert result == (".srm", ".rtc", ".sav", ".nvr")

    def test_dropped_keys_now_return_default(self):
        """Keys unreachable under system-keying are dropped → they return defaults.

        ``saturnjp`` normalizes to system ``saturn``, ``amiga1200``/``amiga600``
        to system ``amiga``, and ``commodore-cdtv`` is not yet in platform_map
        (so no ``cdtv`` system can arrive). None of these strings is a system
        name any install produces, so they are no longer override keys and fall
        through to the defaults.
        """
        for dropped in ("saturnjp", "amiga1200", "amiga600", "cdtv"):
            assert get_save_extensions(dropped) == _DEFAULTS

    def test_non_override_platform_still_returns_default(self):
        """Platforms without overrides get defaults."""
        result = get_save_extensions("gba")
        assert result == _DEFAULTS
        assert ".dsv" not in result
        assert ".brm" not in result
        assert ".nvr" not in result
        assert ".flash" not in result

    def test_patched_override_replaces_defaults(self):
        """A patched override completely replaces the default list."""
        custom = (".foo", ".bar")
        with patch("domain.save_extensions._PLATFORM_OVERRIDES", {"test": custom}):
            result = get_save_extensions("test")
            assert result == custom
