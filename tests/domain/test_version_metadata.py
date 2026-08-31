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

    def test_empty_group_key_is_rejected(self):
        # "No group derived yet" is None; an empty key is neither a group nor a
        # state, and would leave the residency readers disagreeing.
        with pytest.raises(ValueError, match="sibling_group_key"):
            VersionMetadata(sibling_group_key="")


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

    @pytest.mark.parametrize("override", [None, ""])
    def test_falsy_override_falls_back_to_mapping_key(self, override: str | None):
        meta = VersionMetadata.from_mapping({"sibling_group_key": "mapping-key"}, sibling_group_key=override)
        assert meta.sibling_group_key == "mapping-key"

    def test_empty_key_with_no_other_source_degrades_to_none(self):
        # from_mapping degrades rather than raises: with no real key on the other
        # side to fall back to, an empty one coalesces to None instead of
        # tripping the constructor's invariant.
        assert VersionMetadata.from_mapping({"sibling_group_key": ""}).sibling_group_key is None
        assert VersionMetadata.from_mapping({}, sibling_group_key="").sibling_group_key is None
