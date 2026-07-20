"""Tests for the domain.version_metadata value object."""

from __future__ import annotations

import pytest

from domain.version_metadata import VersionMetadata


class TestConstruction:
    def test_defaults_are_the_empty_state(self):
        meta = VersionMetadata()
        assert meta.sibling_group_key is None
        assert meta.regions == ()
        assert meta.languages == ()
        assert meta.revision == ""
        assert meta.tags == ()
        assert meta.is_main_sibling is False

    def test_exposes_provided_fields(self):
        meta = VersionMetadata(
            sibling_group_key="igdb:1:2",
            regions=("USA",),
            languages=("En",),
            revision="1",
            tags=("Demo",),
            is_main_sibling=True,
        )
        assert meta.sibling_group_key == "igdb:1:2"
        assert meta.regions == ("USA",)
        assert meta.languages == ("En",)
        assert meta.revision == "1"
        assert meta.tags == ("Demo",)
        assert meta.is_main_sibling is True

    def test_frozen_rejects_mutation(self):
        meta = VersionMetadata()
        with pytest.raises((AttributeError, TypeError)):
            meta.revision = "2"  # type: ignore[misc]


class TestFromMapping:
    def test_all_keys_present_are_carried(self):
        meta = VersionMetadata.from_mapping(
            {
                "sibling_group_key": "igdb:3404:57",
                "regions": ["USA", "Europe"],
                "languages": ["En", "Fr"],
                "revision": "1",
                "tags": ["Demo"],
                "is_main_sibling": True,
            }
        )
        assert meta.sibling_group_key == "igdb:3404:57"
        assert meta.regions == ("USA", "Europe")
        assert meta.languages == ("En", "Fr")
        assert meta.revision == "1"
        assert meta.tags == ("Demo",)
        assert meta.is_main_sibling is True

    def test_missing_keys_degrade_to_defaults(self):
        meta = VersionMetadata.from_mapping({})
        assert meta.sibling_group_key is None
        assert meta.regions == ()
        assert meta.languages == ()
        assert meta.revision == ""
        assert meta.tags == ()
        assert meta.is_main_sibling is False

    def test_null_valued_keys_degrade_to_defaults(self):
        meta = VersionMetadata.from_mapping(
            {
                "sibling_group_key": None,
                "regions": None,
                "languages": None,
                "revision": None,
                "tags": None,
            }
        )
        assert meta.sibling_group_key is None
        assert meta.regions == ()
        assert meta.languages == ()
        assert meta.revision == ""
        assert meta.tags == ()

    def test_truthy_override_wins_over_mapping_key(self):
        meta = VersionMetadata.from_mapping({"sibling_group_key": "mapping-key"}, sibling_group_key="bound-key")
        assert meta.sibling_group_key == "bound-key"

    def test_falsy_override_falls_back_to_mapping_key(self):
        meta = VersionMetadata.from_mapping({"sibling_group_key": "mapping-key"}, sibling_group_key=None)
        assert meta.sibling_group_key == "mapping-key"
