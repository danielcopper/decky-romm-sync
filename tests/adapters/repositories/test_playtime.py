"""Tests for ``SqlitePlaytimeRepository`` over the ``rom_playtime`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.playtime import Playtime
from domain.rom import Rom

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


class TestRoundTrip:
    def test_full_playtime_preserved(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        playtime = Playtime(
            total_seconds=3600,
            session_count=4,
            last_session_start="2026-03-03T10:00:00Z",
            last_session_duration_sec=900,
            note_id=77,
        )
        uow.playtime.save(5, playtime)

        assert uow.playtime.get(5) == playtime

    def test_nullable_fields_preserved(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        playtime = Playtime()  # all defaults: open-session/duration/note are None
        uow.playtime.save(5, playtime)

        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert loaded.total_seconds == 0
        assert loaded.last_session_start is None
        assert loaded.last_session_duration_sec is None
        assert loaded.note_id is None


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.playtime.get(999) is None


class TestDelete:
    def test_delete_removes_row(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.playtime.save(5, Playtime(total_seconds=10))
        uow.playtime.delete(5)
        assert uow.playtime.get(5) is None

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.playtime.delete(404)
        assert uow.playtime.get(404) is None


class TestIteration:
    def test_iter_all_yields_rom_id_pairs(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        _seed_rom(uow, 2)
        uow.playtime.save(1, Playtime(total_seconds=10))
        uow.playtime.save(2, Playtime(total_seconds=20))

        by_id = dict(uow.playtime.iter_all())
        assert set(by_id) == {1, 2}
        assert by_id[1].total_seconds == 10
        assert by_id[2].total_seconds == 20


class TestUpsert:
    def test_save_existing_overwrites(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        uow.playtime.save(1, Playtime(total_seconds=10, session_count=1))
        uow.playtime.save(1, Playtime(total_seconds=50, session_count=3))

        loaded = uow.playtime.get(1)
        assert loaded is not None
        assert loaded.total_seconds == 50
        assert loaded.session_count == 3
