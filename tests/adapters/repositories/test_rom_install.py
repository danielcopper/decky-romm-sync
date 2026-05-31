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


def _install(rom_id: int, *, path: str = "/roms/snes/game.sfc", rom_dir: str | None = None) -> RomInstall:
    return RomInstall(
        rom_id=rom_id,
        file_path=path,
        rom_dir=rom_dir,
        platform_slug="snes",
        system="snes",
        installed_at="2026-02-02T00:00:00Z",
    )


class TestRoundTrip:
    def test_all_fields_preserved_multi_file(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        install = _install(5, path="/roms/psx/FF7/FF7.m3u", rom_dir="/roms/psx/FF7")
        uow.rom_installs.save(install)

        assert uow.rom_installs.get(5) == install

    def test_null_rom_dir_round_trips(self, uow: SqliteUnitOfWork):
        """A single-file ROM's ``rom_dir`` (``None``) persists as SQL NULL and reads back ``None``."""
        _seed_rom(uow, 6)
        install = _install(6, rom_dir=None)
        uow.rom_installs.save(install)

        loaded = uow.rom_installs.get(6)
        assert loaded is not None
        assert loaded.rom_dir is None
        assert loaded == install


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
