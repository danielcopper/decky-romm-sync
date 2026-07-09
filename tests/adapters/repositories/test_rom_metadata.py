"""Tests for ``SqliteRomMetadataRepository`` over the ``rom_metadata`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.rom import Rom
from domain.rom_metadata import RomMetadata

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
    def test_full_metadata_preserved(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        meta = RomMetadata(
            summary="A classic",
            genres=("RPG", "Adventure"),
            companies=("Squaresoft",),
            first_release_date=946684800,
            average_rating=9.5,
            game_modes=("Single player",),
            player_count="1",
            cached_at=1700000000.0,
            steam_categories=(1, 2, 3),
        )
        uow.rom_metadata.save(5, meta)

        assert uow.rom_metadata.get(5) == meta

    def test_json_array_columns_round_trip_as_tuples(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        meta = RomMetadata(
            summary="s",
            genres=("a", "b"),
            companies=(),
            first_release_date=None,
            average_rating=None,
            game_modes=(),
            player_count="2",
            cached_at=1.0,
            steam_categories=(10, 20),
        )
        uow.rom_metadata.save(5, meta)

        loaded = uow.rom_metadata.get(5)
        assert loaded is not None
        assert loaded.genres == ("a", "b")
        assert loaded.companies == ()
        assert loaded.steam_categories == (10, 20)
        assert all(isinstance(c, int) for c in loaded.steam_categories)

    def test_null_release_date_and_rating_preserved(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        meta = RomMetadata(
            summary="s",
            genres=(),
            companies=(),
            first_release_date=None,
            average_rating=None,
            game_modes=(),
            player_count="1",
            cached_at=1.0,
        )
        uow.rom_metadata.save(5, meta)

        loaded = uow.rom_metadata.get(5)
        assert loaded is not None
        assert loaded.first_release_date is None
        assert loaded.average_rating is None


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.rom_metadata.get(999) is None


class TestDelete:
    def test_delete_removes_row(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.rom_metadata.save(
            5,
            RomMetadata(
                summary="s",
                genres=(),
                companies=(),
                first_release_date=None,
                average_rating=None,
                game_modes=(),
                player_count="1",
                cached_at=1.0,
            ),
        )
        uow.rom_metadata.delete(5)
        assert uow.rom_metadata.get(5) is None

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.rom_metadata.delete(404)
        assert uow.rom_metadata.get(404) is None


class TestIteration:
    def test_iter_all_yields_rom_id_pairs(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        _seed_rom(uow, 2)
        uow.rom_metadata.save(
            1,
            RomMetadata(
                summary="one",
                genres=("RPG",),
                companies=(),
                first_release_date=None,
                average_rating=None,
                game_modes=(),
                player_count="1",
                cached_at=10.0,
            ),
        )
        uow.rom_metadata.save(
            2,
            RomMetadata(
                summary="two",
                genres=("Action",),
                companies=(),
                first_release_date=None,
                average_rating=None,
                game_modes=(),
                player_count="2",
                cached_at=20.0,
            ),
        )

        by_id = dict(uow.rom_metadata.iter_all())
        assert set(by_id) == {1, 2}
        assert by_id[1].summary == "one"
        assert by_id[1].genres == ("RPG",)
        assert by_id[2].summary == "two"
        assert by_id[2].cached_at == 20.0

    def test_iter_all_empty(self, uow: SqliteUnitOfWork):
        assert list(uow.rom_metadata.iter_all()) == []


def _meta(summary: str) -> RomMetadata:
    return RomMetadata(
        summary=summary,
        genres=(),
        companies=(),
        first_release_date=None,
        average_rating=None,
        game_modes=(),
        player_count="1",
        cached_at=1.0,
    )


class TestPaging:
    def test_iter_page_is_rom_id_ordered_and_bounded(self, uow: SqliteUnitOfWork):
        # Insert out of order — the page read must still order by rom_id.
        for rom_id in (5, 1, 3, 4, 2):
            _seed_rom(uow, rom_id)
            uow.rom_metadata.save(rom_id, _meta(f"game-{rom_id}"))

        first = list(uow.rom_metadata.iter_page(0, 2))
        second = list(uow.rom_metadata.iter_page(2, 2))
        third = list(uow.rom_metadata.iter_page(4, 2))

        assert [rom_id for rom_id, _ in first] == [1, 2]
        assert [rom_id for rom_id, _ in second] == [3, 4]
        assert [rom_id for rom_id, _ in third] == [5]

    def test_iter_page_past_end_is_empty(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        uow.rom_metadata.save(1, _meta("only"))
        assert list(uow.rom_metadata.iter_page(500, 500)) == []

    def test_iter_page_zero_limit_is_empty(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        uow.rom_metadata.save(1, _meta("only"))
        assert list(uow.rom_metadata.iter_page(0, 0)) == []

    def test_iter_page_preserves_wire_shape(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 7)
        uow.rom_metadata.save(
            7,
            RomMetadata(
                summary="paged",
                genres=("RPG",),
                companies=(),
                first_release_date=None,
                average_rating=None,
                game_modes=(),
                player_count="1",
                cached_at=9.0,
            ),
        )
        ((rom_id, meta),) = list(uow.rom_metadata.iter_page(0, 10))
        assert rom_id == 7
        assert meta.summary == "paged"
        assert meta.genres == ("RPG",)


class TestCount:
    def test_count_empty(self, uow: SqliteUnitOfWork):
        assert uow.rom_metadata.count() == 0

    def test_count_reflects_rows(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        _seed_rom(uow, 2)
        uow.rom_metadata.save(1, _meta("one"))
        uow.rom_metadata.save(2, _meta("two"))
        assert uow.rom_metadata.count() == 2


class TestUpsert:
    def test_save_existing_overwrites(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        base = RomMetadata(
            summary="old",
            genres=(),
            companies=(),
            first_release_date=None,
            average_rating=None,
            game_modes=(),
            player_count="1",
            cached_at=1.0,
        )
        uow.rom_metadata.save(5, base)
        updated = RomMetadata(
            summary="new",
            genres=("X",),
            companies=(),
            first_release_date=None,
            average_rating=None,
            game_modes=(),
            player_count="1",
            cached_at=2.0,
        )
        uow.rom_metadata.save(5, updated)

        loaded = uow.rom_metadata.get(5)
        assert loaded is not None
        assert loaded.summary == "new"
        assert loaded.genres == ("X",)
