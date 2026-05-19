"""Tests for the frozen metadata-patch dataclasses."""

import dataclasses

import pytest
from models.metadata_patches import MetadataStampPatch
from models.state import MetadataCacheEntry


def _make_entry() -> MetadataCacheEntry:
    return {
        "summary": "A game.",
        "genres": ["RPG"],
        "companies": ["Acme"],
        "first_release_date": 1234567890,
        "average_rating": 90.0,
        "game_modes": ["Single player"],
        "player_count": "1",
        "cached_at": 1700.0,
        "steam_categories": [1],
    }


class TestMetadataStampPatch:
    def test_fields(self):
        entry = _make_entry()
        patch = MetadataStampPatch(rom_id_str="42", entry=entry)
        assert patch.rom_id_str == "42"
        assert patch.entry == entry

    def test_frozen(self):
        patch = MetadataStampPatch(rom_id_str="42", entry=_make_entry())
        with pytest.raises(dataclasses.FrozenInstanceError):
            patch.rom_id_str = "0"  # type: ignore[misc]
