"""Tests for domain/cover_refresh.py — the cover-fingerprint compare kernel (#1386)."""

from domain.cover_refresh import (
    _strip_cover_ts,
    count_cover_refreshes,
    cover_ts_only_change,
    fresh_cover_source,
    scan_cover_refresh_candidates,
)

_OLD = "/cover/big.png?ts=2026-01-01 00:00:00"
_NEW = "/cover/big.png?ts=2026-07-11 12:00:00"


def _bound(app_id, cover_source):
    """A bound-row registry entry as the orchestrator projections shape it."""
    return {"app_id": app_id, "cover_source": cover_source}


class TestFreshCoverSource:
    def test_prefers_path_cover_large(self):
        rom = {"path_cover_large": "/large.png?ts=1", "path_cover_small": "/small.png?ts=1"}
        assert fresh_cover_source(rom) == "/large.png?ts=1"

    def test_falls_back_to_path_cover_small(self):
        assert fresh_cover_source({"path_cover_small": "/small.png?ts=1"}) == "/small.png?ts=1"

    def test_no_cover_fields_is_none(self):
        assert fresh_cover_source({"id": 1, "name": "Game"}) is None

    def test_empty_strings_are_none(self):
        assert fresh_cover_source({"path_cover_large": "", "path_cover_small": ""}) is None


class TestScanCoverRefreshCandidates:
    def test_changed_fingerprint_lands_in_changed(self):
        scan = scan_cover_refresh_candidates(
            [{"id": 10, "path_cover_large": _NEW}],
            {"10": _bound(1010, _OLD)},
        )
        assert scan.changed == [(10, 1010, _NEW)]
        assert scan.null_fingerprint == []

    def test_null_fingerprint_lands_in_null_bucket(self):
        scan = scan_cover_refresh_candidates(
            [{"id": 10, "path_cover_large": _NEW}],
            {"10": _bound(1010, None)},
        )
        assert scan.changed == []
        assert scan.null_fingerprint == [(10, _NEW)]

    def test_unchanged_fingerprint_is_skipped(self):
        scan = scan_cover_refresh_candidates(
            [{"id": 10, "path_cover_large": _NEW}],
            {"10": _bound(1010, _NEW)},
        )
        assert scan.changed == []
        assert scan.null_fingerprint == []

    def test_rom_without_registry_entry_is_skipped(self):
        scan = scan_cover_refresh_candidates([{"id": 999, "path_cover_large": _NEW}], {})
        assert scan.changed == []
        assert scan.null_fingerprint == []

    def test_rom_without_cover_or_id_is_skipped(self):
        scan = scan_cover_refresh_candidates(
            [{"id": 10, "name": "No Cover"}, {"path_cover_large": _NEW}],
            {"10": _bound(1010, _OLD)},
        )
        assert scan.changed == []
        assert scan.null_fingerprint == []

    def test_entry_without_app_id_is_skipped(self):
        # Defensive: a projection entry that somehow lost its binding is never
        # a refresh candidate — there is no Steam tile to push the cover to.
        scan = scan_cover_refresh_candidates(
            [{"id": 10, "path_cover_large": _NEW}],
            {"10": _bound(None, _OLD)},
        )
        assert scan.changed == []
        assert scan.null_fingerprint == []

    def test_small_cover_fallback_participates_in_the_compare(self):
        scan = scan_cover_refresh_candidates(
            [{"id": 10, "path_cover_small": _NEW}],
            {"10": _bound(1010, _OLD)},
        )
        assert scan.changed == [(10, 1010, _NEW)]

    def test_order_follows_input_and_buckets_split(self):
        scan = scan_cover_refresh_candidates(
            [
                {"id": 1, "path_cover_large": "/a.png?ts=new"},
                {"id": 2, "path_cover_large": "/b.png?ts=new"},
                {"id": 3, "path_cover_large": "/c.png?ts=new"},
            ],
            {
                "1": _bound(1001, "/a.png?ts=old"),
                "2": _bound(1002, None),
                "3": _bound(1003, "/c.png?ts=old"),
            },
        )
        assert scan.changed == [(1, 1001, "/a.png?ts=new"), (3, 1003, "/c.png?ts=new")]
        assert scan.null_fingerprint == [(2, "/b.png?ts=new")]

    def test_duplicate_rom_id_counts_once_first_wins(self):
        scan = scan_cover_refresh_candidates(
            [{"id": 10, "path_cover_large": _NEW}, {"id": 10, "path_cover_large": "/other.png?ts=x"}],
            {"10": _bound(1010, _OLD)},
        )
        assert scan.changed == [(10, 1010, _NEW)]

    def test_string_ids_compare_against_string_keys(self):
        # RomM payloads carry numeric ids, but the kernel normalizes via int()
        # so a stringly-typed id still matches its registry key.
        scan = scan_cover_refresh_candidates(
            [{"id": "10", "path_cover_large": _NEW}],
            {"10": _bound(1010, _OLD)},
        )
        assert scan.changed == [(10, 1010, _NEW)]


class TestCountCoverRefreshes:
    def test_counts_only_the_changed_bucket(self):
        roms = [
            {"id": 1, "path_cover_large": "/a.png?ts=new"},  # changed → counted
            {"id": 2, "path_cover_large": "/b.png?ts=new"},  # NULL fingerprint → not counted
            {"id": 3, "path_cover_large": "/c.png?ts=same"},  # unchanged → not counted
            {"id": 4, "path_cover_large": "/d.png?ts=new"},  # unbound (no entry) → not counted
        ]
        registry = {
            "1": _bound(1001, "/a.png?ts=old"),
            "2": _bound(1002, None),
            "3": _bound(1003, "/c.png?ts=same"),
        }
        assert count_cover_refreshes(roms, registry) == 1

    def test_empty_inputs_count_zero(self):
        assert count_cover_refreshes([], {}) == 0
        assert count_cover_refreshes([{"id": 1, "path_cover_large": _NEW}], {}) == 0


class TestStripCoverTs:
    """_strip_cover_ts — drop the ?ts= cache-buster, keep everything else (#1454)."""

    def test_drops_ts_query(self):
        assert _strip_cover_ts("/cover/big.png?ts=2026-01-01 00:00:00") == "/cover/big.png"

    def test_no_query_unchanged(self):
        assert _strip_cover_ts("/cover/big.png") == "/cover/big.png"

    def test_keeps_other_params_and_drops_only_ts(self):
        assert _strip_cover_ts("/c.png?w=100&ts=abc&h=200") == "/c.png?w=100&h=200"

    def test_ts_only_param_leaves_bare_path(self):
        assert _strip_cover_ts("/c.png?ts=abc") == "/c.png"

    def test_bare_ts_key_without_value_dropped(self):
        assert _strip_cover_ts("/c.png?ts") == "/c.png"

    def test_does_not_strip_a_param_that_merely_starts_like_ts(self):
        assert _strip_cover_ts("/c.png?tsx=1") == "/c.png?tsx=1"


class TestCoverTsOnlyChange:
    """cover_ts_only_change — the #1454 revalidation trigger's pure part."""

    _OLD_TS = "/cover/big.png?ts=2026-01-01 00:00:00"
    _NEW_TS = "/cover/big.png?ts=2026-07-11 12:00:00"

    def test_ts_only_change_is_true(self):
        assert cover_ts_only_change(self._OLD_TS, self._NEW_TS) is True

    def test_identical_is_false(self):
        assert cover_ts_only_change(self._NEW_TS, self._NEW_TS) is False

    def test_different_path_is_false(self):
        assert cover_ts_only_change(self._OLD_TS, "/cover/other.png?ts=2026-07-11 12:00:00") is False

    def test_url_cover_to_path_cover_switch_is_false(self):
        # A #1450 fallback→RomM transition must NOT be treated as ts-only.
        assert cover_ts_only_change("https://cdn.example.com/x.png", self._NEW_TS) is False

    def test_none_stored_is_false(self):
        assert cover_ts_only_change(None, self._NEW_TS) is False

    def test_none_fresh_is_false(self):
        assert cover_ts_only_change(self._OLD_TS, None) is False

    def test_empty_strings_are_false(self):
        assert cover_ts_only_change("", self._NEW_TS) is False
        assert cover_ts_only_change(self._OLD_TS, "") is False

    def test_gaining_a_ts_from_a_bare_path_is_ts_only(self):
        assert cover_ts_only_change("/cover/big.png", self._NEW_TS) is True
