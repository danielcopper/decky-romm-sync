"""Tests for the platform-name cache decoder."""

from __future__ import annotations

import pytest

from domain.platform_names import decode_platform_names


class TestDecodePlatformNames:
    def test_decodes_valid_object(self):
        raw = '{"snes": "Super Nintendo", "n64": "Nintendo 64"}'
        assert decode_platform_names(raw) == {"snes": "Super Nintendo", "n64": "Nintendo 64"}

    def test_empty_object_round_trips(self):
        assert decode_platform_names("{}") == {}

    @pytest.mark.parametrize("raw", [None, ""])
    def test_absent_value_yields_empty(self, raw):
        assert decode_platform_names(raw) == {}

    def test_invalid_json_yields_empty(self):
        assert decode_platform_names("not json at all {") == {}

    @pytest.mark.parametrize("raw", ['"a json string, not a dict"', "[1, 2, 3]", "42", "true", "null"])
    def test_non_object_json_yields_empty(self, raw):
        # A corrupt cache must degrade to the slug, never surface a non-dict.
        assert decode_platform_names(raw) == {}
