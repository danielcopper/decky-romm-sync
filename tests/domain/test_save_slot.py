"""Tests for ``domain.save_slot`` — the slot-addressing wire primitives."""

from __future__ import annotations

import pytest

from domain.save_slot import filter_saves_to_slot, normalize_slot, save_in_slot, slot_query_param


class TestNormalizeSlot:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("", None),
            ("default", "default"),
            ("desktop", "desktop"),
        ],
    )
    def test_collapses_legacy_spellings(self, value, expected):
        """``None`` and ``""`` both normalize to ``None``; named slots pass through."""
        assert normalize_slot(value) == expected


class TestSlotQueryParam:
    def test_legacy_none_omits_param(self):
        """The legacy slot (None) omits ``slot=`` — returns None."""
        assert slot_query_param(None) is None

    def test_legacy_empty_string_omits_param(self):
        """The legacy slot ("") omits ``slot=`` — RomM stores legacy as null (#1061)."""
        assert slot_query_param("") is None

    def test_named_slot_passes_through(self):
        """A named slot is sent literally for server-side filtering."""
        assert slot_query_param("default") == "default"
        assert slot_query_param("desktop") == "desktop"


class TestSaveInSlot:
    def test_null_server_save_matches_legacy_none(self):
        """A save with ``slot: null`` belongs to the legacy slot addressed as None."""
        assert save_in_slot({"slot": None}, None) is True

    def test_null_server_save_matches_legacy_empty_string(self):
        """A save with ``slot: null`` also belongs to the legacy slot addressed as ""."""
        assert save_in_slot({"slot": None}, "") is True

    def test_missing_slot_key_matches_legacy(self):
        """A save with no ``slot`` key (slot defaults to null) belongs to the legacy slot."""
        assert save_in_slot({}, None) is True
        assert save_in_slot({}, "") is True

    def test_empty_string_server_save_matches_legacy(self):
        """A save stored with ``slot: ""`` is legacy-equivalent and matches the legacy slot."""
        assert save_in_slot({"slot": ""}, None) is True
        assert save_in_slot({"slot": ""}, "") is True

    def test_named_save_does_not_match_legacy(self):
        """A named-slot save must NOT leak into the legacy listing."""
        assert save_in_slot({"slot": "default"}, None) is False
        assert save_in_slot({"slot": "default"}, "") is False

    def test_legacy_save_does_not_match_named_slot(self):
        """A legacy null save must NOT match a named slot."""
        assert save_in_slot({"slot": None}, "default") is False
        assert save_in_slot({}, "default") is False

    def test_named_save_matches_same_name(self):
        """A named-slot save matches its own slot name."""
        assert save_in_slot({"slot": "default"}, "default") is True

    def test_named_save_does_not_match_other_name(self):
        """A named-slot save does not match a different slot name."""
        assert save_in_slot({"slot": "default"}, "desktop") is False


class TestFilterSavesToSlot:
    def test_isolates_legacy_from_every_named_slot(self):
        """#1061: a legacy (slot:null) save belongs ONLY to the legacy slot.

        Regression for the on-device carry-over: the old filter matched a null
        save under ANY named active slot (``slot == active or slot is None``), so
        the legacy save bled into a named slot's status and got synced into it.
        Exact slot membership keeps each slot isolated.
        """
        saves = [{"id": 75, "slot": "default"}, {"id": 77, "slot": None}, {"id": 74, "slot": "default"}]
        # Named slot the legacy save does NOT belong to → empty (no leak).
        assert [s["id"] for s in filter_saves_to_slot(saves, "test")] == []
        # Named slot → only its own saves, never the legacy null one.
        assert [s["id"] for s in filter_saves_to_slot(saves, "default")] == [75, 74]
        # Legacy mode (None or "") → only the null save.
        assert [s["id"] for s in filter_saves_to_slot(saves, None)] == [77]
        assert [s["id"] for s in filter_saves_to_slot(saves, "")] == [77]

    def test_empty_list_and_no_match_both_yield_empty(self):
        assert filter_saves_to_slot([], "default") == []
        assert filter_saves_to_slot([{"id": 1, "slot": "a"}], "b") == []

    def test_save_without_a_slot_key_counts_as_legacy(self):
        assert [s["id"] for s in filter_saves_to_slot([{"id": 3}], None)] == [3]
        assert filter_saves_to_slot([{"id": 3}], "default") == []
