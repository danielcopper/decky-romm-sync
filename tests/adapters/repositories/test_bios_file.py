"""Tests for ``SqliteBiosFileRepository`` over the ``downloaded_bios`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.bios_file import BiosFile

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _bios(platform: str, file_name: str, *, firmware_id: int | None = None) -> BiosFile:
    return BiosFile(
        platform_slug=platform,
        file_name=file_name,
        file_path=f"/bios/{platform}/{file_name}",
        downloaded_at="2026-01-01T00:00:00Z",
        firmware_id=firmware_id,
    )


class TestRoundTrip:
    def test_all_fields_preserved_with_firmware_id(self, uow: SqliteUnitOfWork):
        bios = _bios("psx", "scph5501.bin", firmware_id=42)
        uow.bios_files.save(bios)

        assert uow.bios_files.get("psx", "scph5501.bin") == bios

    def test_null_firmware_id_preserved(self, uow: SqliteUnitOfWork):
        uow.bios_files.save(_bios("psx", "scph5501.bin"))
        loaded = uow.bios_files.get("psx", "scph5501.bin")
        assert loaded is not None
        assert loaded.firmware_id is None


class TestCompositeKey:
    def test_same_filename_different_platforms_are_distinct(self, uow: SqliteUnitOfWork):
        uow.bios_files.save(_bios("psx", "bios.bin", firmware_id=1))
        uow.bios_files.save(_bios("saturn", "bios.bin", firmware_id=2))

        psx = uow.bios_files.get("psx", "bios.bin")
        saturn = uow.bios_files.get("saturn", "bios.bin")
        assert psx is not None
        assert saturn is not None
        assert psx.firmware_id == 1
        assert saturn.firmware_id == 2


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.bios_files.get("nope", "nope.bin") is None


class TestDelete:
    def test_delete_removes_only_matching_composite_key(self, uow: SqliteUnitOfWork):
        uow.bios_files.save(_bios("psx", "bios.bin"))
        uow.bios_files.save(_bios("saturn", "bios.bin"))
        uow.bios_files.delete("psx", "bios.bin")

        assert uow.bios_files.get("psx", "bios.bin") is None
        assert uow.bios_files.get("saturn", "bios.bin") is not None

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.bios_files.delete("nope", "nope.bin")
        assert uow.bios_files.get("nope", "nope.bin") is None


class TestIteration:
    def test_iter_all_yields_every_record(self, uow: SqliteUnitOfWork):
        uow.bios_files.save(_bios("psx", "a.bin"))
        uow.bios_files.save(_bios("saturn", "b.bin"))

        keys = {(b.platform_slug, b.file_name) for b in uow.bios_files.iter_all()}
        assert keys == {("psx", "a.bin"), ("saturn", "b.bin")}

    def test_iter_by_platform_returns_subset(self, uow: SqliteUnitOfWork):
        uow.bios_files.save(_bios("psx", "a.bin"))
        uow.bios_files.save(_bios("psx", "b.bin"))
        uow.bios_files.save(_bios("saturn", "c.bin"))

        names = {b.file_name for b in uow.bios_files.iter_by_platform("psx")}
        assert names == {"a.bin", "b.bin"}


class TestUpsert:
    def test_save_existing_composite_key_overwrites(self, uow: SqliteUnitOfWork):
        uow.bios_files.save(_bios("psx", "bios.bin", firmware_id=1))
        uow.bios_files.save(_bios("psx", "bios.bin", firmware_id=2))

        loaded = uow.bios_files.get("psx", "bios.bin")
        assert loaded is not None
        assert loaded.firmware_id == 2
