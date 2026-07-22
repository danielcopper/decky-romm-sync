"""Unit tests for the ``CollectionSyncState`` aggregate."""

from __future__ import annotations

import pytest

from domain.collection_sync_state import CollectionSyncState


class TestStamp:
    def test_sets_all_fields(self):
        stamp = CollectionSyncState.stamp(
            collection_id="7",
            collection_kind="standard",
            updated_at="2026-05-28T10:00:00+00:00",
            completed_at="2026-05-28T10:05:00+00:00",
            rom_count=3,
            member_rom_ids=(10, 11, 12),
        )
        assert stamp.collection_id == "7"
        assert stamp.collection_kind == "standard"
        assert stamp.updated_at == "2026-05-28T10:00:00+00:00"
        assert stamp.completed_at == "2026-05-28T10:05:00+00:00"
        assert stamp.rom_count == 3
        assert stamp.member_rom_ids == (10, 11, 12)

    def test_coerces_member_ids_to_tuple(self):
        stamp = CollectionSyncState.stamp(
            collection_id="9",
            collection_kind="smart",
            updated_at="2026-05-28T10:00:00+00:00",
            completed_at="2026-05-28T10:05:00+00:00",
            rom_count=2,
            member_rom_ids=[1, 2],  # type: ignore[arg-type]
        )
        assert stamp.member_rom_ids == (1, 2)

    def test_zero_rom_count_and_empty_members_allowed(self):
        stamp = CollectionSyncState.stamp(
            collection_id="7",
            collection_kind="standard",
            updated_at="2026-05-28T10:00:00+00:00",
            completed_at="2026-05-28T10:05:00+00:00",
            rom_count=0,
            member_rom_ids=(),
        )
        assert stamp.rom_count == 0
        assert stamp.member_rom_ids == ()

    def test_empty_collection_id_raises(self):
        with pytest.raises(ValueError, match="collection_id is required"):
            CollectionSyncState.stamp(
                collection_id="",
                collection_kind="standard",
                updated_at="2026-05-28T10:00:00+00:00",
                completed_at="2026-05-28T10:05:00+00:00",
                rom_count=1,
                member_rom_ids=(1,),
            )

    @pytest.mark.parametrize("kind", ["virtual", "", "user", "STANDARD"])
    def test_non_standard_smart_kind_raises(self, kind):
        with pytest.raises(ValueError, match="collection_kind must be"):
            CollectionSyncState.stamp(
                collection_id="7",
                collection_kind=kind,
                updated_at="2026-05-28T10:00:00+00:00",
                completed_at="2026-05-28T10:05:00+00:00",
                rom_count=1,
                member_rom_ids=(1,),
            )

    def test_empty_updated_at_raises(self):
        with pytest.raises(ValueError, match="updated_at is required"):
            CollectionSyncState.stamp(
                collection_id="7",
                collection_kind="standard",
                updated_at="",
                completed_at="2026-05-28T10:05:00+00:00",
                rom_count=1,
                member_rom_ids=(1,),
            )

    def test_empty_completed_at_raises(self):
        with pytest.raises(ValueError, match="completed_at is required"):
            CollectionSyncState.stamp(
                collection_id="7",
                collection_kind="standard",
                updated_at="2026-05-28T10:00:00+00:00",
                completed_at="",
                rom_count=1,
                member_rom_ids=(1,),
            )

    def test_negative_rom_count_raises(self):
        with pytest.raises(ValueError, match="rom_count must be non-negative"):
            CollectionSyncState.stamp(
                collection_id="7",
                collection_kind="standard",
                updated_at="2026-05-28T10:00:00+00:00",
                completed_at="2026-05-28T10:05:00+00:00",
                rom_count=-1,
                member_rom_ids=(1,),
            )
