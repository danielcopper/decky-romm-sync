"""Unit tests for ``domain.preview_delta.PreviewDelta``.

The dataclass is pure data — its contract is "all fields are required,
immutable, and exposed as typed attributes", plus the one TTL predicate the
apply path and the pending-preview read share. Tests cover construction,
frozen semantics, equality, and both sides of the TTL boundary.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from domain.preview_delta import PreviewDelta, preview_expires_at


def _build(**overrides) -> PreviewDelta:
    defaults: dict[str, Any] = {
        "preview_id": "preview-abc",
        "created_at": 1_700_000_000.0,
        "answer": {"success": True, "preview_id": "preview-abc"},
    }
    defaults.update(overrides)
    return PreviewDelta(**defaults)


def test_construction_exposes_all_fields_as_attributes() -> None:
    delta = _build()
    assert delta.preview_id == "preview-abc"
    assert delta.created_at == 1_700_000_000.0
    assert delta.answer == {"success": True, "preview_id": "preview-abc"}


def test_is_frozen_attribute_rebinding_raises() -> None:
    delta = _build()
    with pytest.raises(FrozenInstanceError):
        delta.preview_id = "other"  # type: ignore[misc]


def test_equality_by_field_values() -> None:
    a = _build()
    b = _build()
    assert a == b
    c = _build(preview_id="preview-xyz")
    assert a != c


def test_expires_at_is_creation_plus_max_age() -> None:
    assert preview_expires_at(1_700_000_000.0, 1800) == 1_700_001_800.0


def test_not_expired_at_the_deadline_itself() -> None:
    delta = _build()
    assert delta.is_expired(1_700_001_800.0, 1800) is False


def test_expired_one_second_past_the_deadline() -> None:
    delta = _build()
    assert delta.is_expired(1_700_001_801.0, 1800) is True
