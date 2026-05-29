"""Tests for ``SqliteFirmwareCacheRepository`` over the ``firmware_cache`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.firmware_cache import FirmwareCacheEntry

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _entry(platform: str, name: str, *, fw_id: int | None = 1, cached_at: float = 1700000000.0) -> FirmwareCacheEntry:
    return FirmwareCacheEntry(
        id=fw_id,
        name=name,
        platform_slug=platform,
        file_size_bytes=1024,
        cached_at=cached_at,
    )


class TestRoundTrip:
    def test_all_fields_preserved(self, uow: SqliteUnitOfWork):
        entry = _entry("psx", "bios.bin", fw_id=99)
        uow.firmware_cache.replace_all([entry])

        assert uow.firmware_cache.get("psx", "bios.bin") == entry

    def test_null_id_preserved(self, uow: SqliteUnitOfWork):
        uow.firmware_cache.replace_all([_entry("psx", "legacy.bin", fw_id=None)])
        loaded = uow.firmware_cache.get("psx", "legacy.bin")
        assert loaded is not None
        assert loaded.id is None


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.firmware_cache.get("nope", "nope.bin") is None


class TestReplaceAll:
    def test_replace_all_drops_prior_entries(self, uow: SqliteUnitOfWork):
        uow.firmware_cache.replace_all([_entry("psx", "old.bin")])
        uow.firmware_cache.replace_all([_entry("saturn", "new.bin")])

        keys = {(e.platform_slug, e.name) for e in uow.firmware_cache.iter_all()}
        assert keys == {("saturn", "new.bin")}

    def test_replace_all_with_empty_list_clears_cache(self, uow: SqliteUnitOfWork):
        uow.firmware_cache.replace_all([_entry("psx", "x.bin")])
        uow.firmware_cache.replace_all([])
        assert list(uow.firmware_cache.iter_all()) == []

    def test_replace_all_stores_multiple_entries(self, uow: SqliteUnitOfWork):
        uow.firmware_cache.replace_all([_entry("psx", "a.bin"), _entry("psx", "b.bin")])
        names = {e.name for e in uow.firmware_cache.iter_all()}
        assert names == {"a.bin", "b.bin"}


class TestClear:
    def test_clear_empties_cache(self, uow: SqliteUnitOfWork):
        uow.firmware_cache.replace_all([_entry("psx", "x.bin")])
        uow.firmware_cache.clear()
        assert list(uow.firmware_cache.iter_all()) == []


class TestCacheEpoch:
    def test_returns_cached_at_when_populated(self, uow: SqliteUnitOfWork):
        uow.firmware_cache.replace_all([_entry("psx", "x.bin", cached_at=1234.5)])
        assert uow.firmware_cache.get_cache_epoch() == 1234.5

    def test_returns_none_when_empty(self, uow: SqliteUnitOfWork):
        assert uow.firmware_cache.get_cache_epoch() is None
