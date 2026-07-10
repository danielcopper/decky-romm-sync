"""Tests for ``SqlitePlatformSyncStateRepository`` over the ``platform_sync_state`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.platform_sync_state import PlatformSyncState

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _stamp(slug: str, *, at: str = "2026-01-01T00:00:00+00:00", rom_count: int = 100) -> PlatformSyncState:
    return PlatformSyncState.stamp(platform_slug=slug, at=at, rom_count=rom_count)


class TestRoundTrip:
    def test_saved_stamp_reads_back_equal(self, uow: SqliteUnitOfWork):
        stamp = _stamp("n64", at="2026-03-01T12:00:00+00:00", rom_count=2091)
        uow.platform_sync_state.save(stamp)

        loaded = uow.platform_sync_state.get("n64")
        assert loaded is not None
        assert loaded == stamp
        assert loaded.completed_at == "2026-03-01T12:00:00+00:00"
        assert loaded.rom_count == 2091


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.platform_sync_state.get("nope") is None


class TestUpsert:
    def test_save_same_slug_overwrites(self, uow: SqliteUnitOfWork):
        uow.platform_sync_state.save(_stamp("n64", at="2026-01-01T00:00:00+00:00", rom_count=100))
        uow.platform_sync_state.save(_stamp("n64", at="2026-02-01T00:00:00+00:00", rom_count=105))

        loaded = uow.platform_sync_state.get("n64")
        assert loaded is not None
        assert loaded.completed_at == "2026-02-01T00:00:00+00:00"
        assert loaded.rom_count == 105

    def test_distinct_slugs_coexist(self, uow: SqliteUnitOfWork):
        uow.platform_sync_state.save(_stamp("n64", rom_count=100))
        uow.platform_sync_state.save(_stamp("snes", rom_count=200))

        n64 = uow.platform_sync_state.get("n64")
        snes = uow.platform_sync_state.get("snes")
        assert n64 is not None
        assert snes is not None
        assert n64.rom_count == 100
        assert snes.rom_count == 200


class TestDelete:
    def test_delete_removes_only_the_named_slug(self, uow: SqliteUnitOfWork):
        uow.platform_sync_state.save(_stamp("n64"))
        uow.platform_sync_state.save(_stamp("snes"))

        uow.platform_sync_state.delete("n64")

        assert uow.platform_sync_state.get("n64") is None
        assert uow.platform_sync_state.get("snes") is not None

    def test_delete_absent_slug_is_noop(self, uow: SqliteUnitOfWork):
        uow.platform_sync_state.delete("nope")  # no row → no error
        assert uow.platform_sync_state.get("nope") is None


class TestClear:
    def test_clear_removes_every_stamp(self, uow: SqliteUnitOfWork):
        uow.platform_sync_state.save(_stamp("n64"))
        uow.platform_sync_state.save(_stamp("snes"))

        uow.platform_sync_state.clear()

        assert uow.platform_sync_state.get("n64") is None
        assert uow.platform_sync_state.get("snes") is None

    def test_clear_is_idempotent_when_empty(self, uow: SqliteUnitOfWork):
        uow.platform_sync_state.clear()
        assert uow.platform_sync_state.get("n64") is None
