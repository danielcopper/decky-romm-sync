"""Tests for domain/sibling_group.py — client-side sibling-group key derivation."""

from __future__ import annotations

from domain.sibling_group import compute_sibling_group_key


class TestCoalesceOrder:
    """The metadata ids coalesce in RomM's fixed order; the first non-null wins."""

    def test_igdb_wins_over_all_others(self):
        rom = {
            "id": 1,
            "platform_id": 57,
            "igdb_id": 3404,
            "ss_id": 9,
            "moby_id": 8,
            "ra_id": 7,
            "hasheous_id": 6,
            "launchbox_id": 5,
            "tgdb_id": 4,
            "flashpoint_id": 3,
        }
        assert compute_sibling_group_key(rom) == "igdb:3404:57"

    def test_ss_wins_when_igdb_absent(self):
        rom = {"id": 1, "platform_id": 57, "ss_id": 22, "moby_id": 8, "ra_id": 7}
        assert compute_sibling_group_key(rom) == "ss:22:57"

    def test_moby_wins_over_ra(self):
        rom = {"id": 1, "platform_id": 57, "moby_id": 88, "ra_id": 7}
        assert compute_sibling_group_key(rom) == "moby:88:57"

    def test_ra_wins_over_hasheous(self):
        rom = {"id": 1, "platform_id": 57, "ra_id": 70, "hasheous_id": 6}
        assert compute_sibling_group_key(rom) == "ra:70:57"

    def test_hasheous_wins_over_launchbox(self):
        rom = {"id": 1, "platform_id": 57, "hasheous_id": 60, "launchbox_id": 5}
        assert compute_sibling_group_key(rom) == "hasheous:60:57"

    def test_launchbox_wins_over_tgdb(self):
        rom = {"id": 1, "platform_id": 57, "launchbox_id": 50, "tgdb_id": 4}
        assert compute_sibling_group_key(rom) == "launchbox:50:57"

    def test_tgdb_wins_over_flashpoint(self):
        rom = {"id": 1, "platform_id": 57, "tgdb_id": 40, "flashpoint_id": 3}
        assert compute_sibling_group_key(rom) == "tgdb:40:57"

    def test_flashpoint_is_last_matched_source(self):
        rom = {"id": 1, "platform_id": 57, "flashpoint_id": 30}
        assert compute_sibling_group_key(rom) == "flashpoint:30:57"


class TestPlatformScoping:
    """The same metadata id on two platforms yields two distinct groups."""

    def test_same_igdb_different_platform_is_different_key(self):
        base = {"id": 1, "igdb_id": 3404}
        key_a = compute_sibling_group_key({**base, "platform_id": 57})
        key_b = compute_sibling_group_key({**base, "platform_id": 12})
        assert key_a == "igdb:3404:57"
        assert key_b == "igdb:3404:12"
        assert key_a != key_b

    def test_same_igdb_same_platform_is_same_key(self):
        a = compute_sibling_group_key({"id": 1, "platform_id": 57, "igdb_id": 3404})
        b = compute_sibling_group_key({"id": 2, "platform_id": 57, "igdb_id": 3404})
        assert a == b == "igdb:3404:57"


class TestUnmatchedFallback:
    """An unmatched ROM falls back to its own id — a solo group."""

    def test_no_metadata_ids_falls_back_to_rom_id(self):
        rom = {"id": 4409, "platform_id": 57}
        assert compute_sibling_group_key(rom) == "romm:4409:57"

    def test_all_metadata_ids_none_falls_back(self):
        rom = {
            "id": 4409,
            "platform_id": 57,
            "igdb_id": None,
            "ss_id": None,
            "moby_id": None,
            "ra_id": None,
            "hasheous_id": None,
            "launchbox_id": None,
            "tgdb_id": None,
            "flashpoint_id": None,
        }
        assert compute_sibling_group_key(rom) == "romm:4409:57"

    def test_two_unmatched_roms_are_distinct_solo_groups(self):
        a = compute_sibling_group_key({"id": 100, "platform_id": 57})
        b = compute_sibling_group_key({"id": 200, "platform_id": 57})
        assert a == "romm:100:57"
        assert b == "romm:200:57"
        assert a != b


class TestMissingAndNoneIds:
    """A None id is skipped as if absent; a later non-null id still wins."""

    def test_none_igdb_skips_to_next_present_source(self):
        rom = {"id": 1, "platform_id": 57, "igdb_id": None, "moby_id": 88}
        assert compute_sibling_group_key(rom) == "moby:88:57"

    def test_missing_igdb_key_skips_to_next(self):
        rom = {"id": 1, "platform_id": 57, "ra_id": 70}
        assert compute_sibling_group_key(rom) == "ra:70:57"

    def test_zero_id_is_treated_as_present(self):
        # 0 is a legitimate (if unusual) id — only None means "unmatched".
        rom = {"id": 1, "platform_id": 57, "igdb_id": 0}
        assert compute_sibling_group_key(rom) == "igdb:0:57"
