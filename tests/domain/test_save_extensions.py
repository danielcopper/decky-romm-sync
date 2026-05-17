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
    """get_save_extensions respects platform-specific overrides."""

    def test_nds_override_includes_dsv(self):
        """NDS platform returns DeSmuME .dsv extension."""
        result = get_save_extensions("nds")
        assert ".dsv" in result
        assert ".srm" in result
        assert ".sav" in result

    def test_segacd_override_includes_brm(self):
        """Sega CD platform returns Genesis Plus GX .brm extension."""
        result = get_save_extensions("segacd")
        assert ".brm" in result
        assert ".srm" in result

    def test_non_override_platform_still_returns_default(self):
        """Platforms without overrides get defaults."""
        result = get_save_extensions("gba")
        assert result == _DEFAULTS
        assert ".dsv" not in result
        assert ".brm" not in result

    def test_patched_override_replaces_defaults(self):
        """A patched override completely replaces the default list."""
        custom = (".foo", ".bar")
        with patch("domain.save_extensions._PLATFORM_OVERRIDES", {"test": custom}):
            result = get_save_extensions("test")
            assert result == custom
