"""Tests for domain.retroarch_core_info.parse_core_info."""

from __future__ import annotations

from domain.retroarch_core_info import parse_core_info


class TestParseCoreInfo:
    def test_snes9x_happy_path(self):
        """Real-world Snes9x .info snippet: corename is ``Snes9x`` (not the
        ES-DE display label)."""
        text = (
            "# Software Information\n"
            'display_name = "Nintendo - SNES / SFC (Snes9x)"\n'
            'categories = "Emulator"\n'
            'authors = "Snes9x Team"\n'
            'corename = "Snes9x"\n'
            'supported_extensions = "smc|sfc|swc|fig|bs|st"\n'
        )
        result = parse_core_info(text)
        assert result["corename"] == "Snes9x"
        assert result["display_name"] == "Nintendo - SNES / SFC (Snes9x)"
        assert result["supported_extensions"] == "smc|sfc|swc|fig|bs|st"

    def test_empty_file(self):
        assert parse_core_info("") == {}

    def test_only_comments(self):
        text = "# Comment 1\n# Comment 2\n#Comment 3\n"
        assert parse_core_info(text) == {}

    def test_only_blank_lines(self):
        assert parse_core_info("\n\n   \n\t\n") == {}

    def test_value_with_spaces(self):
        """Multi-word quoted values keep their internal spaces."""
        text = 'corename = "Beetle PSX HW"\n'
        assert parse_core_info(text) == {"corename": "Beetle PSX HW"}

    def test_multiple_equals_in_value(self):
        """Only the first ``=`` splits the key from the value."""
        text = 'database = "a=1;b=2"\n'
        assert parse_core_info(text) == {"database": "a=1;b=2"}

    def test_line_without_equals_ignored(self):
        text = 'no equals here\ncorename = "Snes9x"\n'
        assert parse_core_info(text) == {"corename": "Snes9x"}

    def test_missing_corename_field(self):
        """File with other keys but no corename — dict exists, no corename."""
        text = 'display_name = "Some Core"\n'
        result = parse_core_info(text)
        assert "corename" not in result
        assert result["display_name"] == "Some Core"

    def test_unicode_content(self):
        text = 'display_name = "Café Core ÜÄÖ"\n'
        assert parse_core_info(text) == {"display_name": "Café Core ÜÄÖ"}

    def test_trailing_whitespace_variations(self):
        """Leading/trailing spaces, tabs, and CRLF endings are tolerated."""
        text = '  corename   =   "Snes9x"  \r\n\tdisplay_name="X"\r\n'
        result = parse_core_info(text)
        assert result["corename"] == "Snes9x"
        assert result["display_name"] == "X"

    def test_unquoted_value(self):
        """Unquoted values are returned as-is."""
        text = "firmware_count = 3\n"
        assert parse_core_info(text) == {"firmware_count": "3"}

    def test_empty_value(self):
        """Empty quoted value results in empty string (not missing)."""
        text = 'permissions = ""\n'
        assert parse_core_info(text) == {"permissions": ""}

    def test_comment_after_key_line_not_stripped(self):
        """Inline ``#`` on a key line is NOT a comment — it's part of the
        value if inside quotes, or the value text if unquoted. The parser
        does not attempt RFC-style comment handling."""
        text = 'corename = "Snes9x # not a comment"\n'
        assert parse_core_info(text) == {"corename": "Snes9x # not a comment"}

    def test_only_whitespace_key(self):
        """Line with only ``=`` and no key is ignored."""
        text = '   = "value"\n'
        assert parse_core_info(text) == {}

    def test_duplicate_keys_last_wins(self):
        """If a key appears twice, the last value wins."""
        text = 'corename = "First"\ncorename = "Second"\n'
        assert parse_core_info(text) == {"corename": "Second"}

    def test_value_with_only_one_quote(self):
        """Value with a lone quote is preserved as-is (not stripped)."""
        text = 'note = "unterminated\n'
        # Only one ``"`` — len >= 2 but endswith('"') is False
        result = parse_core_info(text)
        assert result["note"] == '"unterminated'

    def test_various_info_values_from_real_files(self):
        """Verified against actual .info files shipping with RetroDECK."""
        for text, expected in [
            ('corename = "mGBA"\n', "mGBA"),
            ('corename = "SwanStation"\n', "SwanStation"),
            ('corename = "Beetle PSX HW"\n', "Beetle PSX HW"),
            ('corename = "Genesis Plus GX"\n', "Genesis Plus GX"),
        ]:
            assert parse_core_info(text)["corename"] == expected
