"""Tests for domain.sgdb_artwork — SGDB asset-type and endpoint helpers."""

from __future__ import annotations

import pytest

from domain.sgdb_artwork import (
    asset_type_endpoint,
    asset_type_name,
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
