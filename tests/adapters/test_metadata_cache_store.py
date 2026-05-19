"""Tests for ``MetadataCacheStoreAdapter`` — typed-patch writes over the live cache dict."""

import pytest
from models.metadata_patches import MetadataStampPatch
from models.state import MetadataCache, MetadataCacheEntry

from adapters.metadata_cache_store import MetadataCacheStoreAdapter


def _make_entry(summary: str = "Summary") -> MetadataCacheEntry:
    return {
        "summary": summary,
        "genres": ["RPG"],
        "companies": ["Acme"],
        "first_release_date": 1234567890,
        "average_rating": 90.0,
        "game_modes": ["Single player"],
        "player_count": "1",
        "cached_at": 1700.0,
        "steam_categories": [1],
    }


@pytest.fixture
def cache() -> MetadataCache:
    return {}


@pytest.fixture
def store(cache: MetadataCache) -> MetadataCacheStoreAdapter:
    return MetadataCacheStoreAdapter(metadata_cache=cache)


class TestApplyStamp:
    def test_creates_entry_on_empty_cache(self, store, cache):
        entry = _make_entry()
        store.apply_stamp(MetadataStampPatch(rom_id_str="42", entry=entry))

        assert cache["42"] == entry

    def test_replaces_existing_entry(self, store, cache):
        cache["42"] = _make_entry(summary="Old")
        new = _make_entry(summary="New")

        store.apply_stamp(MetadataStampPatch(rom_id_str="42", entry=new))

        assert cache["42"]["summary"] == "New"

    def test_other_entries_untouched(self, store, cache):
        cache["1"] = _make_entry(summary="One")
        cache["2"] = _make_entry(summary="Two")

        store.apply_stamp(MetadataStampPatch(rom_id_str="2", entry=_make_entry(summary="Updated")))

        assert cache["1"]["summary"] == "One"
        assert cache["2"]["summary"] == "Updated"
