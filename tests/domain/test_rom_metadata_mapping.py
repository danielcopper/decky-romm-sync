"""Tests for ``build_rom_metadata`` — RomM dict → ``RomMetadata`` aggregate mapping."""

from __future__ import annotations

import pytest

from domain.rom_metadata import RomMetadata
from domain.rom_metadata_mapping import build_rom_metadata


class TestFullMapping:
    def test_full_metadatum(self):
        rom = {
            "summary": "An adventure game",
            "metadatum": {
                "genres": ["RPG", "Adventure"],
                "companies": ["Nintendo", "HAL Laboratory"],
                "first_release_date": 1082592000000,
                "average_rating": 79.665,
                "game_modes": ["Single player", "Multiplayer"],
                "player_count": "1-4",
            },
        }
        meta = build_rom_metadata(rom, cached_at=123.0)
        assert isinstance(meta, RomMetadata)
        assert meta.summary == "An adventure game"
        assert meta.genres == ("RPG", "Adventure")
        assert meta.companies == ("Nintendo", "HAL Laboratory")
        assert meta.first_release_date == 1082592000  # ms → s
        assert meta.average_rating == 79.665
        assert meta.game_modes == ("Single player", "Multiplayer")
        assert meta.player_count == "1-4"
        assert meta.cached_at == 123.0


class TestReleaseDate:
    def test_first_release_date_ms_to_seconds(self):
        """RomM sends milliseconds; the aggregate stores whole seconds."""
        rom = {"metadatum": {"first_release_date": 946684800000}}
        meta = build_rom_metadata(rom, cached_at=1.0)
        assert meta.first_release_date == 946684800


class TestMissingAndNone:
    def test_missing_metadatum(self):
        """ROM with no metadatum field returns empty defaults."""
        rom = {"summary": "A game", "id": 1}
        meta = build_rom_metadata(rom, cached_at=1.0)
        assert meta.summary == "A game"
        assert meta.genres == ()
        assert meta.companies == ()
        assert meta.first_release_date is None
        assert meta.average_rating is None
        assert meta.game_modes == ()
        assert meta.player_count == ""

    def test_none_metadatum(self):
        """ROM with metadatum=None returns empty defaults."""
        rom = {"summary": "A game", "metadatum": None}
        meta = build_rom_metadata(rom, cached_at=1.0)
        assert meta.genres == ()
        assert meta.first_release_date is None

    def test_none_fields_in_metadatum(self):
        """Metadatum fields that are None fall back to empty tuple/string."""
        rom = {
            "metadatum": {
                "genres": None,
                "companies": None,
                "game_modes": None,
                "player_count": None,
            },
        }
        meta = build_rom_metadata(rom, cached_at=1.0)
        assert meta.genres == ()
        assert meta.companies == ()
        assert meta.game_modes == ()
        assert meta.player_count == ""


class TestSummary:
    @pytest.mark.parametrize(
        "rom",
        [
            {"summary": None, "metadatum": {}},
            {"summary": "", "metadatum": {}},
            {"metadatum": {}},
        ],
    )
    def test_empty_or_missing_summary(self, rom):
        assert build_rom_metadata(rom, cached_at=1.0).summary == ""


class TestSteamCategories:
    def test_steam_categories_computed_from_genres_and_modes(self):
        rom = {
            "summary": "Test",
            "metadatum": {
                "genres": ["Action", "Puzzle"],
                "game_modes": ["Single player"],
            },
        }
        meta = build_rom_metadata(rom, cached_at=1.0)
        assert 28 in meta.steam_categories  # full controller support
        assert 21 in meta.steam_categories  # Action
        assert 4 in meta.steam_categories  # Puzzle
        assert 2 in meta.steam_categories  # Single player

    def test_empty_metadatum_still_has_full_controller_support(self):
        """Even a bare ROM gets the FULL_CONTROLLER_SUPPORT category (28)."""
        meta = build_rom_metadata({"metadatum": {}}, cached_at=1.0)
        assert meta.steam_categories == (28,)
