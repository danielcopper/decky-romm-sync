"""Unit tests for ``domain.preview_delta.PreviewDelta``.

The dataclass is pure data — its contract is "all 4 fields are required,
immutable, and exposed as typed attributes". Tests cover construction,
frozen semantics, and the zero-counts boundary case.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from domain.preview_delta import PreviewDelta


def _build(**overrides) -> PreviewDelta:
    defaults: dict = {
        "preview_id": "preview-abc",
        "created_at": 1_700_000_000.0,
        "platforms_count": 2,
        "total_roms": 3,
    }
    defaults.update(overrides)
    return PreviewDelta(**defaults)


def test_construction_exposes_all_fields_as_attributes() -> None:
    delta = _build()
    assert delta.preview_id == "preview-abc"
    assert delta.created_at == 1_700_000_000.0
    assert delta.platforms_count == 2
    assert delta.total_roms == 3


def test_is_frozen_attribute_rebinding_raises() -> None:
    delta = _build()
    with pytest.raises(FrozenInstanceError):
        delta.preview_id = "other"  # type: ignore[misc]


def test_zero_counts_are_accepted() -> None:
    delta = _build(platforms_count=0, total_roms=0)
    assert delta.platforms_count == 0
    assert delta.total_roms == 0


def test_equality_by_field_values() -> None:
    a = _build()
    b = _build()
    assert a == b
    c = _build(preview_id="preview-xyz")
    assert a != c
