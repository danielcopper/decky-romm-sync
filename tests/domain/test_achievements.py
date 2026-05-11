"""Tests for domain.achievements — pure RA payload normalization."""

from domain.achievements import extract_achievements_from_rom, extract_game_progress


def _sample_achievements():
    """Return a list of two sample RA achievements as they appear in ra_metadata."""
    return [
        {
            "ra_id": 1001,
            "title": "First Blood",
            "description": "Defeat the first boss",
            "points": 10,
            "badge_id": "badge-1001",
            "badge_url": "http://badges/1001.png",
            "badge_url_lock": "http://badges/1001_lock.png",
            "display_order": 1,
            "type": "progression",
            "num_awarded": 5000,
            "num_awarded_hardcore": 2000,
        },
        {
            "ra_id": 1002,
            "title": "Completionist",
            "description": "Find all secrets",
            "points": 50,
            "badge_id": "badge-1002",
            "badge_url": "http://badges/1002.png",
            "badge_url_lock": "http://badges/1002_lock.png",
            "display_order": 2,
            "type": "missable",
            "num_awarded": 100,
            "num_awarded_hardcore": 50,
        },
    ]


def _sample_rom_data(achievements=None, use_merged=False):
    """Build a mock RomM ROM detail response with ra_metadata."""
    key = "merged_ra_metadata" if use_merged else "ra_metadata"
    return {
        "id": 42,
        "ra_id": 9999,
        key: {"achievements": achievements or _sample_achievements()},
    }


class TestExtractAchievementsFromRom:
    def test_full_achievement_list(self):
        rom_data = _sample_rom_data()
        result = extract_achievements_from_rom(rom_data)
        assert len(result) == 2
        assert result[0]["ra_id"] == 1001
        assert result[0]["title"] == "First Blood"
        assert result[0]["description"] == "Defeat the first boss"
        assert result[0]["points"] == 10
        assert result[0]["badge_id"] == "badge-1001"
        assert result[0]["badge_url"] == "http://badges/1001.png"
        assert result[0]["badge_url_lock"] == "http://badges/1001_lock.png"
        assert result[0]["display_order"] == 1
        assert result[0]["type"] == "progression"
        assert result[0]["num_awarded"] == 5000
        assert result[0]["num_awarded_hardcore"] == 2000

        assert result[1]["ra_id"] == 1002
        assert result[1]["title"] == "Completionist"

    def test_empty_ra_metadata(self):
        rom_data = {"ra_metadata": {}}
        result = extract_achievements_from_rom(rom_data)
        assert result == []

    def test_none_ra_metadata(self):
        rom_data = {"ra_metadata": None}
        result = extract_achievements_from_rom(rom_data)
        assert result == []

    def test_missing_ra_metadata_key(self):
        rom_data = {"id": 1}
        result = extract_achievements_from_rom(rom_data)
        assert result == []

    def test_fallback_to_merged_ra_metadata(self):
        """When ra_metadata is empty, falls back to merged_ra_metadata."""
        rom_data = {
            "ra_metadata": {},
            "merged_ra_metadata": {"achievements": _sample_achievements()},
        }
        result = extract_achievements_from_rom(rom_data)
        assert len(result) == 2
        assert result[0]["ra_id"] == 1001

    def test_fallback_to_merged_when_ra_metadata_is_none(self):
        rom_data = {
            "ra_metadata": None,
            "merged_ra_metadata": {"achievements": _sample_achievements()},
        }
        result = extract_achievements_from_rom(rom_data)
        assert len(result) == 2

    def test_ra_metadata_takes_priority_over_merged(self):
        """When ra_metadata has achievements, merged_ra_metadata is not used."""
        rom_data = {
            "ra_metadata": {"achievements": [_sample_achievements()[0]]},
            "merged_ra_metadata": {"achievements": _sample_achievements()},
        }
        result = extract_achievements_from_rom(rom_data)
        assert len(result) == 1
        assert result[0]["ra_id"] == 1001

    def test_missing_fields_get_defaults(self):
        """Achievement entries with missing fields get default values."""
        rom_data = {"ra_metadata": {"achievements": [{"ra_id": 2000}]}}
        result = extract_achievements_from_rom(rom_data)
        assert len(result) == 1
        a = result[0]
        assert a["ra_id"] == 2000
        assert a["title"] == ""
        assert a["description"] == ""
        assert a["points"] == 0
        assert a["badge_id"] == ""
        assert a["badge_url"] == ""
        assert a["badge_url_lock"] == ""
        assert a["display_order"] == 0
        assert a["type"] == ""
        assert a["num_awarded"] == 0
        assert a["num_awarded_hardcore"] == 0

    def test_empty_achievements_list(self):
        rom_data = {"ra_metadata": {"achievements": []}}
        result = extract_achievements_from_rom(rom_data)
        assert result == []

    def test_achievements_none_in_metadata(self):
        rom_data = {"ra_metadata": {"achievements": None}}
        result = extract_achievements_from_rom(rom_data)
        assert result == []


class TestExtractGameProgress:
    def test_game_found_returns_progress(self):
        ra_progression = {
            "results": [
                {
                    "rom_ra_id": 9999,
                    "num_awarded": 5,
                    "num_awarded_hardcore": 3,
                    "max_possible": 10,
                    "earned_achievements": [1001, 1002, 1003, 1004, 1005],
                },
            ],
        }
        result = extract_game_progress(ra_progression, ra_id=9999, total=10, cached_at=123.0)
        assert result == {
            "earned": 5,
            "earned_hardcore": 3,
            "total": 10,
            "earned_achievements": [1001, 1002, 1003, 1004, 1005],
            "cached_at": 123.0,
        }

    def test_game_not_found_returns_zero_stub(self):
        ra_progression = {"results": [{"rom_ra_id": 1111, "num_awarded": 5}]}
        result = extract_game_progress(ra_progression, ra_id=9999, total=7, cached_at=456.0)
        assert result == {
            "earned": 0,
            "earned_hardcore": 0,
            "total": 7,
            "earned_achievements": [],
            "cached_at": 456.0,
        }

    def test_empty_results_returns_zero_stub(self):
        result = extract_game_progress({"results": []}, ra_id=9999, total=4, cached_at=10.0)
        assert result == {
            "earned": 0,
            "earned_hardcore": 0,
            "total": 4,
            "earned_achievements": [],
            "cached_at": 10.0,
        }

    def test_none_ra_progression_returns_zero_stub(self):
        result = extract_game_progress(None, ra_id=9999, total=2, cached_at=20.0)
        assert result == {
            "earned": 0,
            "earned_hardcore": 0,
            "total": 2,
            "earned_achievements": [],
            "cached_at": 20.0,
        }

    def test_missing_results_key_returns_zero_stub(self):
        result = extract_game_progress({}, ra_id=9999, total=3, cached_at=30.0)
        assert result == {
            "earned": 0,
            "earned_hardcore": 0,
            "total": 3,
            "earned_achievements": [],
            "cached_at": 30.0,
        }

    def test_cached_at_is_propagated_verbatim(self):
        ra_progression = {
            "results": [
                {
                    "rom_ra_id": 9999,
                    "num_awarded": 1,
                    "num_awarded_hardcore": 1,
                    "max_possible": 5,
                    "earned_achievements": [1001],
                },
            ],
        }
        result = extract_game_progress(ra_progression, ra_id=9999, total=5, cached_at=987654.321)
        assert result["cached_at"] == 987654.321

    def test_none_num_awarded_falls_back_to_zero(self):
        ra_progression = {
            "results": [
                {
                    "rom_ra_id": 9999,
                    "num_awarded": None,
                    "num_awarded_hardcore": None,
                    "max_possible": 10,
                    "earned_achievements": [],
                },
            ],
        }
        result = extract_game_progress(ra_progression, ra_id=9999, total=10, cached_at=0.0)
        assert result["earned"] == 0
        assert result["earned_hardcore"] == 0

    def test_max_possible_missing_falls_back_to_total(self):
        ra_progression = {
            "results": [
                {
                    "rom_ra_id": 9999,
                    "num_awarded": 2,
                    "num_awarded_hardcore": 1,
                    "earned_achievements": [1001, 1002],
                },
            ],
        }
        result = extract_game_progress(ra_progression, ra_id=9999, total=8, cached_at=0.0)
        assert result["total"] == 8

    def test_max_possible_zero_falls_back_to_total(self):
        ra_progression = {
            "results": [
                {
                    "rom_ra_id": 9999,
                    "num_awarded": 1,
                    "num_awarded_hardcore": 0,
                    "max_possible": 0,
                    "earned_achievements": [1001],
                },
            ],
        }
        result = extract_game_progress(ra_progression, ra_id=9999, total=12, cached_at=0.0)
        assert result["total"] == 12
