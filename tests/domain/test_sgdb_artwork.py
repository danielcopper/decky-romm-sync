"""Tests for domain.sgdb_artwork — SGDB asset-type and endpoint helpers."""

from __future__ import annotations

import pytest

from domain.sgdb_artwork import (
    asset_type_endpoint,
    asset_type_name,
    build_autocomplete_path,
    classify_resolution,
    first_grid_url,
    parse_autocomplete_results,
    sgdb_endpoint_path,
    to_signed_app_id,
)


class TestAssetTypeName:
    @pytest.mark.parametrize(
        ("type_num", "expected"),
        [
            (1, "hero"),
            (2, "logo"),
            (3, "grid"),
            (4, "icon"),
        ],
    )
    def test_known_codes(self, type_num, expected):
        assert asset_type_name(type_num) == expected

    def test_unknown_returns_none(self):
        assert asset_type_name(99) is None

    def test_zero_returns_none(self):
        assert asset_type_name(0) is None


class TestAssetTypeEndpoint:
    @pytest.mark.parametrize(
        ("name", "endpoint"),
        [
            ("hero", "heroes"),
            ("logo", "logos"),
            ("grid", "grids"),
            ("icon", "icons"),
        ],
    )
    def test_known_names(self, name, endpoint):
        assert asset_type_endpoint(name) == endpoint

    def test_unknown_returns_none(self):
        assert asset_type_endpoint("banner") is None

    def test_empty_returns_none(self):
        assert asset_type_endpoint("") is None


class TestSgdbEndpointPath:
    def test_hero_path(self):
        assert sgdb_endpoint_path("hero", 9999) == "/heroes/game/9999"

    def test_logo_path(self):
        assert sgdb_endpoint_path("logo", 42) == "/logos/game/42"

    def test_icon_path(self):
        assert sgdb_endpoint_path("icon", 1) == "/icons/game/1"

    def test_grid_path_appends_dimensions_query(self):
        assert sgdb_endpoint_path("grid", 7) == "/grids/game/7?dimensions=460x215,920x430"

    def test_unknown_asset_returns_none(self):
        assert sgdb_endpoint_path("banner", 1) is None


class TestToSignedAppId:
    def test_low_positive_unchanged(self):
        # Values below 2^31 round-trip as themselves.
        assert to_signed_app_id(100001) == 100001
        assert to_signed_app_id(0) == 0
        assert to_signed_app_id(1) == 1

    def test_high_bit_becomes_negative(self):
        # 3_000_000_000 has its high bit set in 32-bit space → negative as int32.
        # Confirmed in steamgrid tests: app_id 3000000000 → signed = -1294967296.
        assert to_signed_app_id(3000000000) == -1294967296

    def test_max_uint32_is_negative_one(self):
        assert to_signed_app_id(0xFFFFFFFF) == -1

    def test_2_pow_31_is_int32_min(self):
        assert to_signed_app_id(0x80000000) == -2147483648


class TestBuildAutocompletePath:
    def test_simple_term(self):
        assert build_autocomplete_path("zelda") == "/search/autocomplete/zelda"

    def test_spaces_are_encoded(self):
        assert build_autocomplete_path("super mario") == "/search/autocomplete/super%20mario"

    def test_reserved_chars_encoded(self):
        # quote() leaves '/' alone by default, but encodes other reserved chars.
        assert build_autocomplete_path("a&b?c") == "/search/autocomplete/a%26b%3Fc"

    def test_empty_term(self):
        assert build_autocomplete_path("") == "/search/autocomplete/"


class TestParseAutocompleteResults:
    def test_happy_path_with_release_year(self):
        # 1234567890 → 2009-02-13 UTC
        payload = {
            "success": True,
            "data": [
                {"id": 1, "name": "Zelda", "release_date": 1234567890},
                {"id": 2, "name": "Mario", "release_date": 0},
            ],
        }
        result = parse_autocomplete_results(payload)
        assert result == [
            {"id": 1, "name": "Zelda", "release_year": 2009},
            {"id": 2, "name": "Mario", "release_year": 1970},
        ]

    def test_missing_release_date_yields_none_year(self):
        payload = {"success": True, "data": [{"id": 5, "name": "Metroid"}]}
        assert parse_autocomplete_results(payload) == [{"id": 5, "name": "Metroid", "release_year": None}]

    def test_none_release_date_yields_none_year(self):
        payload = {"success": True, "data": [{"id": 5, "name": "Metroid", "release_date": None}]}
        assert parse_autocomplete_results(payload) == [{"id": 5, "name": "Metroid", "release_year": None}]

    def test_success_false_returns_empty(self):
        assert parse_autocomplete_results({"success": False, "data": [{"id": 1, "name": "x"}]}) == []

    def test_none_payload_returns_empty(self):
        assert parse_autocomplete_results(None) == []

    def test_empty_payload_returns_empty(self):
        assert parse_autocomplete_results({}) == []

    def test_non_list_data_returns_empty(self):
        assert parse_autocomplete_results({"success": True, "data": "nope"}) == []

    def test_malformed_entries_skipped(self):
        payload = {
            "success": True,
            "data": [
                {"id": 1, "name": "ok", "release_date": None},
                {"id": "bad", "name": "string id"},
                {"name": "no id"},
                {"id": 2},  # no name
                "not a dict",
                {"id": 3, "name": "also ok"},
            ],
        }
        result = parse_autocomplete_results(payload)
        assert result == [
            {"id": 1, "name": "ok", "release_year": None},
            {"id": 3, "name": "also ok", "release_year": None},
        ]

    def test_bool_release_date_ignored(self):
        # bool is a subclass of int — must not be treated as a timestamp.
        payload = {"success": True, "data": [{"id": 1, "name": "x", "release_date": True}]}
        assert parse_autocomplete_results(payload) == [{"id": 1, "name": "x", "release_year": None}]


class TestFirstGridUrl:
    def test_returns_thumb(self):
        payload = {"success": True, "data": [{"thumb": "t.png", "url": "u.png"}]}
        assert first_grid_url(payload) == "t.png"

    def test_falls_back_to_url_when_no_thumb(self):
        payload = {"success": True, "data": [{"url": "u.png"}]}
        assert first_grid_url(payload) == "u.png"

    def test_empty_thumb_falls_back_to_url(self):
        payload = {"success": True, "data": [{"thumb": "", "url": "u.png"}]}
        assert first_grid_url(payload) == "u.png"

    def test_empty_data_returns_none(self):
        assert first_grid_url({"success": True, "data": []}) is None

    def test_none_payload_returns_none(self):
        assert first_grid_url(None) is None

    def test_success_false_returns_none(self):
        assert first_grid_url({"success": False, "data": [{"thumb": "t.png"}]}) is None

    def test_first_entry_without_urls_returns_none(self):
        assert first_grid_url({"success": True, "data": [{"id": 1}]}) is None

    def test_non_dict_first_entry_returns_none(self):
        assert first_grid_url({"success": True, "data": ["nope"]}) is None


class TestClassifyResolution:
    def test_state_only_uses_state(self):
        assert classify_resolution(9999, None) == "use_state"

    def test_romm_only_uses_romm(self):
        assert classify_resolution(None, 7777) == "use_romm"

    def test_romm_wins_over_equal_state(self):
        assert classify_resolution(9999, 9999) == "use_romm"

    def test_romm_wins_over_differing_state(self):
        assert classify_resolution(9999, 7777) == "use_romm"

    def test_both_none_unresolved(self):
        assert classify_resolution(None, None) == "unresolved"
