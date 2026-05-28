"""Unit tests for the ``RomMetadata`` aggregate."""

from __future__ import annotations

from domain.rom_metadata import _METADATA_TTL_SEC, RomMetadata


def _make_metadata(*, cached_at: float, steam_categories: tuple[int, ...] | None = None) -> RomMetadata:
    kwargs = {
        "summary": "A great game.",
        "genres": ("Action", "Adventure"),
        "companies": ("Nintendo",),
        "first_release_date": 1994,
        "average_rating": 9.5,
        "game_modes": ("Single player",),
        "player_count": "1",
        "cached_at": cached_at,
    }
    if steam_categories is not None:
        kwargs["steam_categories"] = steam_categories
    return RomMetadata.cached(**kwargs)


class TestCached:
    def test_sets_all_fields(self):
        meta = RomMetadata.cached(
            summary="A great game.",
            genres=("Action",),
            companies=("Nintendo",),
            first_release_date=1994,
            average_rating=9.5,
            game_modes=("Single player",),
            player_count="1",
            cached_at=1000.0,
            steam_categories=(2, 22),
        )
        assert meta.summary == "A great game."
        assert meta.genres == ("Action",)
        assert meta.companies == ("Nintendo",)
        assert meta.first_release_date == 1994
        assert meta.average_rating == 9.5
        assert meta.game_modes == ("Single player",)
        assert meta.player_count == "1"
        assert meta.cached_at == 1000.0
        assert meta.steam_categories == (2, 22)

    def test_steam_categories_defaults_to_empty_tuple(self):
        meta = _make_metadata(cached_at=1000.0)
        assert meta.steam_categories == ()


class TestIsStale:
    def test_stale_when_older_than_ttl(self):
        meta = _make_metadata(cached_at=0.0)
        assert meta.is_stale(_METADATA_TTL_SEC + 1) is True

    def test_not_stale_within_ttl(self):
        meta = _make_metadata(cached_at=0.0)
        assert meta.is_stale(_METADATA_TTL_SEC - 1) is False

    def test_boundary_exactly_at_ttl_is_not_stale(self):
        # Strict ``>``: exactly cached_at + TTL is NOT stale.
        meta = _make_metadata(cached_at=100.0)
        assert meta.is_stale(100.0 + _METADATA_TTL_SEC) is False

    def test_just_past_ttl_is_stale(self):
        meta = _make_metadata(cached_at=100.0)
        assert meta.is_stale(100.0 + _METADATA_TTL_SEC + 1) is True
