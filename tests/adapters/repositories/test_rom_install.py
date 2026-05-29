"""Tests for ``SqliteRomInstallRepository`` over the ``rom_installs`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.rom import Rom
from domain.rom_install import RomInstall

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _seed_rom(uow: SqliteUnitOfWork, rom_id: int) -> None:
    uow.roms.save(
        Rom(
            rom_id=rom_id,
            platform_slug="snes",
            name=f"Game {rom_id}",
            fs_name=f"game_{rom_id}.sfc",
            shortcut_app_id=1000 + rom_id,
            last_synced_at="2026-01-01T00:00:00Z",
        )
    )


def _install(rom_id: int, *, path: str = "/roms/game.sfc") -> RomInstall:
    return RomInstall(
        rom_id=rom_id,
        file_path=path,
        install_path="/roms",
        platform_slug="snes",
        system="snes",
        installed_at="2026-02-02T00:00:00Z",
    )


class TestRoundTrip:
    def test_all_fields_preserved(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        install = _install(5)
        uow.rom_installs.save(install)

        assert uow.rom_installs.get(5) == install


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.rom_installs.get(999) is None


class TestDelete:
    def test_delete_removes_row(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.rom_installs.save(_install(5))
        uow.rom_installs.delete(5)
        assert uow.rom_installs.get(5) is None

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.rom_installs.delete(404)
        assert uow.rom_installs.get(404) is None


class TestIteration:
    def test_iter_all_yields_every_install(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        _seed_rom(uow, 2)
        uow.rom_installs.save(_install(1))
        uow.rom_installs.save(_install(2))

        ids = {install.rom_id for install in uow.rom_installs.iter_all()}
        assert ids == {1, 2}


class TestUpsert:
    def test_save_existing_id_overwrites(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        uow.rom_installs.save(_install(1, path="/old"))
        uow.rom_installs.save(_install(1, path="/new"))

        loaded = uow.rom_installs.get(1)
        assert loaded is not None
        assert loaded.file_path == "/new"
