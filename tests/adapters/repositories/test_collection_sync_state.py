"""Tests for ``SqliteCollectionSyncStateRepository`` over the ``collection_sync_state`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.collection_sync_state import CollectionSyncState

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _stamp(
    collection_id: str,
    *,
    kind: str = "standard",
    updated_at: str = "2026-01-01T00:00:00+00:00",
    completed_at: str = "2026-01-01T00:05:00+00:00",
    rom_count: int = 3,
    member_rom_ids: tuple[int, ...] = (10, 11, 12),
) -> CollectionSyncState:
    return CollectionSyncState.stamp(
        collection_id=collection_id,
        collection_kind=kind,
        updated_at=updated_at,
        completed_at=completed_at,
        rom_count=rom_count,
        member_rom_ids=member_rom_ids,
    )


class TestRoundTrip:
    def test_saved_stamp_reads_back_equal(self, uow: SqliteUnitOfWork):
        stamp = _stamp("7", updated_at="2026-03-01T12:00:00+00:00", rom_count=2, member_rom_ids=(100, 200))
        uow.collection_sync_state.save(stamp)

        loaded = uow.collection_sync_state.get("7", "standard")
        assert loaded is not None
        assert loaded == stamp
        assert loaded.updated_at == "2026-03-01T12:00:00+00:00"
        assert loaded.rom_count == 2
        assert loaded.member_rom_ids == (100, 200)

    def test_empty_member_set_round_trips(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("7", rom_count=0, member_rom_ids=()))
        loaded = uow.collection_sync_state.get("7", "standard")
        assert loaded is not None
        assert loaded.member_rom_ids == ()


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.collection_sync_state.get("nope", "standard") is None


class TestCompositeKey:
    def test_same_id_different_kind_coexist(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("5", kind="standard", rom_count=3, member_rom_ids=(1, 2, 3)))
        uow.collection_sync_state.save(_stamp("5", kind="smart", rom_count=1, member_rom_ids=(9,)))

        user = uow.collection_sync_state.get("5", "standard")
        smart = uow.collection_sync_state.get("5", "smart")
        assert user is not None
        assert smart is not None
        assert user.rom_count == 3
        assert smart.rom_count == 1


class TestUpsert:
    def test_save_same_key_overwrites(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("7", updated_at="2026-01-01T00:00:00+00:00", rom_count=3))
        uow.collection_sync_state.save(
            _stamp("7", updated_at="2026-02-01T00:00:00+00:00", rom_count=4, member_rom_ids=(10, 11, 12, 13))
        )

        loaded = uow.collection_sync_state.get("7", "standard")
        assert loaded is not None
        assert loaded.updated_at == "2026-02-01T00:00:00+00:00"
        assert loaded.rom_count == 4
        assert loaded.member_rom_ids == (10, 11, 12, 13)


class TestDelete:
    def test_delete_removes_only_the_named_key(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("7", kind="standard"))
        uow.collection_sync_state.save(_stamp("7", kind="smart"))

        uow.collection_sync_state.delete("7", "standard")

        assert uow.collection_sync_state.get("7", "standard") is None
        assert uow.collection_sync_state.get("7", "smart") is not None

    def test_delete_absent_key_is_noop(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.delete("nope", "standard")  # no row → no error
        assert uow.collection_sync_state.get("nope", "standard") is None


class TestIterAll:
    def test_iter_all_yields_every_stamp(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("7", kind="standard", member_rom_ids=(1, 2)))
        uow.collection_sync_state.save(_stamp("9", kind="smart", member_rom_ids=(3,)))

        stamps = sorted(uow.collection_sync_state.iter_all(), key=lambda s: s.collection_id)
        assert [(s.collection_id, s.collection_kind) for s in stamps] == [("7", "standard"), ("9", "smart")]
        assert stamps[0].member_rom_ids == (1, 2)

    def test_iter_all_empty_when_no_stamps(self, uow: SqliteUnitOfWork):
        assert list(uow.collection_sync_state.iter_all()) == []


class TestHasAny:
    def test_false_when_no_stamps(self, uow: SqliteUnitOfWork):
        assert uow.collection_sync_state.has_any() is False

    def test_true_once_any_collection_is_stamped(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("7", kind="smart"))

        assert uow.collection_sync_state.has_any() is True

    def test_follows_delete_down_to_the_last_stamp(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("7", kind="standard"))
        uow.collection_sync_state.save(_stamp("9", kind="smart"))

        uow.collection_sync_state.delete("7", "standard")
        assert uow.collection_sync_state.has_any() is True

        uow.collection_sync_state.delete("9", "smart")
        assert uow.collection_sync_state.has_any() is False

    def test_false_after_clear(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("7", kind="standard"))
        uow.collection_sync_state.clear()

        assert uow.collection_sync_state.has_any() is False


class TestClear:
    def test_clear_removes_every_stamp(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.save(_stamp("7", kind="standard"))
        uow.collection_sync_state.save(_stamp("9", kind="smart"))

        uow.collection_sync_state.clear()

        assert uow.collection_sync_state.get("7", "standard") is None
        assert uow.collection_sync_state.get("9", "smart") is None

    def test_clear_is_idempotent_when_empty(self, uow: SqliteUnitOfWork):
        uow.collection_sync_state.clear()
        assert uow.collection_sync_state.get("7", "standard") is None
