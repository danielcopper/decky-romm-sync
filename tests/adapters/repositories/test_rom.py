"""Tests for ``SqliteRomRepository`` over the ``roms`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.rom import Rom

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _rom(rom_id: int, *, platform: str = "snes", app_id: int = 1000) -> Rom:
    return Rom(
        rom_id=rom_id,
        platform_slug=platform,
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.sfc",
        shortcut_app_id=app_id,
        last_synced_at="2026-01-01T00:00:00Z",
    )


class TestRoundTrip:
    def test_all_fields_preserved_with_optionals_set(self, uow: SqliteUnitOfWork):
        rom = Rom(
            rom_id=42,
            platform_slug="gba",
            name="Pokemon",
            fs_name="pokemon.gba",
            shortcut_app_id=98765,
            last_synced_at="2026-05-01T12:00:00Z",
            cover_path="/covers/42.png",
            igdb_id=111,
            sgdb_id=222,
            ra_id=333,
        )
        uow.roms.save(rom)

        loaded = uow.roms.get(42)
        assert loaded == rom

    def test_null_optionals_preserved(self, uow: SqliteUnitOfWork):
        rom = _rom(7)
        uow.roms.save(rom)

        loaded = uow.roms.get(7)
        assert loaded is not None
        assert loaded.cover_path is None
        assert loaded.igdb_id is None
        assert loaded.sgdb_id is None
        assert loaded.ra_id is None


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.roms.get(999) is None

    def test_get_by_app_id_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.roms.get_by_app_id(123) is None


class TestGetByAppId:
    def test_finds_rom_by_shortcut_app_id(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1, app_id=5000))
        uow.roms.save(_rom(2, app_id=6000))

        found = uow.roms.get_by_app_id(6000)
        assert found is not None
        assert found.rom_id == 2


class TestUnboundShortcut:
    def test_null_app_id_round_trips(self, uow: SqliteUnitOfWork):
        rom = _rom(1, app_id=5000)
        rom.unbind_shortcut()
        uow.roms.save(rom)

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.shortcut_app_id is None

    def test_get_by_app_id_skips_unbound_rows(self, uow: SqliteUnitOfWork):
        bound = _rom(1, app_id=5000)
        unbound = _rom(2, app_id=6000)
        unbound.unbind_shortcut()
        uow.roms.save(bound)
        uow.roms.save(unbound)

        assert uow.roms.get_by_app_id(5000) is not None
        # The reverse lookup must never resolve a NULL (unbound) row.
        assert uow.roms.get_by_app_id(6000) is None


class TestDelete:
    def test_delete_removes_row(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.delete(1)
        assert uow.roms.get(1) is None

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.roms.delete(404)  # no row — must not raise
        assert uow.roms.get(404) is None


class TestIteration:
    def test_iter_all_yields_every_rom(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1))
        uow.roms.save(_rom(2))
        uow.roms.save(_rom(3))

        ids = {rom.rom_id for rom in uow.roms.iter_all()}
        assert ids == {1, 2, 3}

    def test_iter_by_platform_returns_subset(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1, platform="snes"))
        uow.roms.save(_rom(2, platform="gba"))
        uow.roms.save(_rom(3, platform="snes"))

        snes_ids = {rom.rom_id for rom in uow.roms.iter_by_platform("snes")}
        assert snes_ids == {1, 3}

    def test_count_reflects_row_count(self, uow: SqliteUnitOfWork):
        assert uow.roms.count() == 0
        uow.roms.save(_rom(1))
        uow.roms.save(_rom(2))
        assert uow.roms.count() == 2


class TestUpsert:
    def test_save_existing_id_overwrites(self, uow: SqliteUnitOfWork):
        uow.roms.save(_rom(1, app_id=100))
        uow.roms.save(_rom(1, app_id=200))

        loaded = uow.roms.get(1)
        assert loaded is not None
        assert loaded.shortcut_app_id == 200
        assert uow.roms.count() == 1
