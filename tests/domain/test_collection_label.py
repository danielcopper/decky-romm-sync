"""Tests for domain.collection_label.collection_label.

The fine display label appended to a Steam collection name under the ``by_label``
naming mode. The strings MUST match the frontend vocabulary (``SUB_TAB_LABELS`` /
``VIRTUAL_TYPE_LABELS`` in ``src/components/LibraryPage.tsx``), and no label may
contain ``]`` (it sits inside the ``RomM: [<name> (<label>)]`` bracket pair the
frontend reconcile parses).
"""

from __future__ import annotations

import pytest

from domain.collection_label import collection_label

# The known (kind, virtual_type) → label mapping, mirroring the frontend labels.
_KNOWN_CASES = [
    ("standard", None, "Standard"),
    ("smart", None, "Smart"),
    ("virtual", "franchise", "Franchise"),
    ("virtual", "collection", "IGDB Collection"),
]


class TestKnownLabels:
    @pytest.mark.parametrize(("kind", "virtual_type", "expected"), _KNOWN_CASES)
    def test_known_kind_and_type_map_to_frontend_label(self, kind, virtual_type, expected):
        assert collection_label(kind, virtual_type) == expected

    def test_standard_ignores_a_stray_virtual_type(self):
        # A non-virtual kind never consults virtual_type — a spurious value is harmless.
        assert collection_label("standard", "franchise") == "Standard"

    def test_smart_ignores_a_stray_virtual_type(self):
        assert collection_label("smart", "collection") == "Smart"


class TestVirtualFallback:
    """A virtual collection with a missing/unrecognised type degrades to 'Virtual'."""

    def test_virtual_without_type_falls_back(self):
        assert collection_label("virtual", None) == "Virtual"

    def test_virtual_with_unknown_type_falls_back(self):
        # A virtual type the plugin does not sync (genre/company/mode) → 'Virtual'.
        assert collection_label("virtual", "genre") == "Virtual"


class TestUnknownKindFallback:
    def test_unknown_kind_capitalised(self):
        assert collection_label("weird", None) == "Weird"

    def test_empty_kind_degrades_to_collection(self):
        assert collection_label("", None) == "Collection"


class TestNoClosingBracketInLabel:
    """A label must never contain ']' — it would truncate the reconcile name-parse."""

    @pytest.mark.parametrize(("kind", "virtual_type", "expected"), _KNOWN_CASES)
    def test_known_labels_have_no_closing_bracket(self, kind, virtual_type, expected):
        assert "]" not in collection_label(kind, virtual_type)

    @pytest.mark.parametrize(
        ("kind", "virtual_type"),
        [("virtual", None), ("virtual", "genre"), ("weird", None), ("", None)],
    )
    def test_fallback_labels_have_no_closing_bracket(self, kind, virtual_type):
        assert "]" not in collection_label(kind, virtual_type)
