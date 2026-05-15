"""Unit tests for ``domain.preview_delta.PreviewDelta``.

The dataclass is pure data — its contract is "all 12 fields are required,
immutable, and exposed as typed attributes". Tests cover construction,
frozen semantics, and the empty-collections boundary case.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from domain.preview_delta import PreviewDelta


def _build(**overrides) -> PreviewDelta:
    defaults: dict = {
        "preview_id": "preview-abc",
        "created_at": 1_700_000_000.0,
        "new": [{"rom_id": 1, "name": "Game A"}],
        "changed": [{"rom_id": 2, "name": "Game B", "existing_app_id": 1002}],
        "unchanged_ids": [3],
        "remove_rom_ids": [4],
        "all_shortcuts": {1: {"rom_id": 1}, 2: {"rom_id": 2}, 3: {"rom_id": 3}},
        "delta_roms": [{"id": 1}, {"id": 2}],
        "platforms_count": 2,
        "total_roms": 3,
        "collection_memberships": {"Favorites": [1, 2]},
        "platform_rom_ids": {1, 2, 3},
    }
    defaults.update(overrides)
    return PreviewDelta(**defaults)


def test_construction_exposes_all_fields_as_attributes() -> None:
    delta = _build()
    assert delta.preview_id == "preview-abc"
    assert delta.created_at == 1_700_000_000.0
    assert delta.new == [{"rom_id": 1, "name": "Game A"}]
    assert delta.changed == [{"rom_id": 2, "name": "Game B", "existing_app_id": 1002}]
    assert delta.unchanged_ids == [3]
    assert delta.remove_rom_ids == [4]
    assert delta.all_shortcuts == {1: {"rom_id": 1}, 2: {"rom_id": 2}, 3: {"rom_id": 3}}
    assert delta.delta_roms == [{"id": 1}, {"id": 2}]
    assert delta.platforms_count == 2
    assert delta.total_roms == 3
    assert delta.collection_memberships == {"Favorites": [1, 2]}
    assert delta.platform_rom_ids == {1, 2, 3}


def test_is_frozen_attribute_rebinding_raises() -> None:
    delta = _build()
    with pytest.raises(FrozenInstanceError):
        delta.preview_id = "other"  # type: ignore[misc]


def test_empty_collections_are_accepted() -> None:
    delta = _build(
        new=[],
        changed=[],
        unchanged_ids=[],
        remove_rom_ids=[],
        all_shortcuts={},
        delta_roms=[],
        platforms_count=0,
        total_roms=0,
        collection_memberships={},
        platform_rom_ids=set(),
    )
    assert delta.new == []
    assert delta.all_shortcuts == {}
    assert delta.platform_rom_ids == set()


def test_equality_by_field_values() -> None:
    a = _build()
    b = _build()
    assert a == b
    c = _build(preview_id="preview-xyz")
    assert a != c
