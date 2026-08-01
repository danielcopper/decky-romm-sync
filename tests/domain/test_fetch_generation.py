"""Tests for the skip's countable-row kernel (``domain/fetch_generation.py``)."""

from __future__ import annotations

import pytest

from domain.fetch_generation import (
    backfill_needed,
    count_rows_for_skip,
    current_generation_ids,
    prune_candidate_ids,
)
from domain.platform_sync_state import PlatformSyncState
from domain.rom import Rom


def _row(rom_id: int, fetch_id: str | None) -> Rom:
    rom = Rom.synced(
        rom_id=rom_id,
        platform_slug="dc",
        name=f"rom-{rom_id}",
        fs_name=f"rom-{rom_id}.gdi",
        shortcut_app_id=None,
        synced_at="2026-07-20T06:27:12",
    )
    if fetch_id is not None:
        rom.record_fetch_generation(fetch_id)
    return rom


class TestCountRowsForSkip:
    def test_counts_only_rows_of_the_named_generation(self):
        # The live #1504 shape: 4375/4376 superseded, 25135/25136 current.
        rows = [
            _row(4375, "run-old"),
            _row(4376, "run-old"),
            _row(25135, "run-new"),
            _row(25136, "run-new"),
        ]
        assert count_rows_for_skip(rows, "run-new") == 2

    def test_superseded_rows_stop_blocking_the_server_count_match(self):
        rows = [_row(4375, "run-old"), _row(25135, "run-new"), _row(25136, "run-new")]
        server_rom_count = 2
        assert count_rows_for_skip(rows, "run-new") == server_rom_count

    def test_counts_bound_and_unbound_rows_alike(self):
        # Group-aware sync persists every sibling (ADR-0021); only the generation
        # decides, never the binding.
        bound = _row(25135, "run-new")
        bound.bind_shortcut(4185303886)
        assert count_rows_for_skip([bound, _row(25136, "run-new")], "run-new") == 2

    def test_row_with_no_generation_never_matches(self):
        assert count_rows_for_skip([_row(4375, None), _row(25135, "run-new")], "run-new") == 1

    def test_unknown_stamp_generation_counts_every_row(self):
        # A stamp written before the generation contract cannot say what its fetch
        # saw, so the pre-#1504 behavior stands until the next fetch re-stamps.
        rows = [_row(4375, None), _row(25135, "run-new")]
        assert count_rows_for_skip(rows, None) == 2

    def test_empty_stamp_generation_counts_every_row(self):
        assert count_rows_for_skip([_row(4375, None)], "") == 1

    def test_no_rows_counts_zero(self):
        assert count_rows_for_skip([], "run-new") == 0
        assert count_rows_for_skip([], None) == 0

    def test_a_platform_with_no_superseded_rows_counts_all_of_them(self):
        rows = [_row(25135, "run-new"), _row(25136, "run-new")]
        assert count_rows_for_skip(rows, "run-new") == len(rows)


class TestPruneCandidateIds:
    def test_mismatch_and_null_are_candidates(self):
        stamp = PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=1, fetch_id="new")
        assert prune_candidate_ids([_row(1, "new"), _row(2, "old"), _row(3, None)], stamp) == {2, 3}

    def test_missing_legacy_and_empty_completion_yield_none(self):
        rows = [_row(1, "old")]
        assert prune_candidate_ids(rows, None) == set()
        assert (
            prune_candidate_ids(rows, PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=1, fetch_id=None))
            == set()
        )
        assert (
            prune_candidate_ids(
                rows, PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=0, fetch_id="new")
            )
            == set()
        )


def _keyed_row(rom_id: int, fetch_id: str | None, group_key: str | None) -> Rom:
    return Rom(
        rom_id=rom_id,
        platform_slug="dc",
        name=f"rom-{rom_id}",
        fs_name=f"rom-{rom_id}.gdi",
        shortcut_app_id=None,
        last_synced_at="2026-07-20T06:27:12",
        sibling_group_key=group_key,
        last_fetch_id=fetch_id,
    )


class TestBackfillNeeded:
    def test_null_key_in_the_stamped_generation_demands_a_backfill(self):
        rows = [_keyed_row(25135, "run-new", "group-a"), _keyed_row(25136, "run-new", None)]
        assert backfill_needed(rows, "run-new") is True

    def test_null_key_on_a_dropped_row_may_not_hold_the_skip_off(self):
        """#1504: no fetch can ever fill a key the server no longer returns."""
        rows = [_keyed_row(4375, "run-old", None), _keyed_row(25135, "run-new", "group-a")]
        assert backfill_needed(rows, "run-new") is False

    def test_every_key_present_needs_no_backfill(self):
        rows = [_keyed_row(25135, "run-new", "group-a"), _keyed_row(25136, "run-new", "group-a")]
        assert backfill_needed(rows, "run-new") is False

    def test_a_stamp_without_a_generation_counts_every_null_key(self):
        """The legacy path predates the contract and errs towards fetching."""
        rows = [_keyed_row(4375, "run-old", None)]
        assert backfill_needed(rows, None) is True
        assert backfill_needed(rows, "") is True

    def test_no_rows_never_demands_a_backfill(self):
        assert backfill_needed([], "run-new") is False
        assert backfill_needed([], None) is False


class TestCurrentGenerationIds:
    def test_returns_only_rows_carrying_the_stamped_generation(self):
        rows = [_row(4375, "run-old"), _row(25135, "run-new"), _row(25136, "run-new")]
        stamp = PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=2, fetch_id="run-new")
        assert current_generation_ids(rows, stamp) == {25135, 25136}

    def test_is_the_exact_complement_of_the_candidate_set(self):
        rows = [_row(4375, "run-old"), _row(25135, "run-new")]
        stamp = PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=1, fetch_id="run-new")
        assert current_generation_ids(rows, stamp) | prune_candidate_ids(rows, stamp) == {4375, 25135}
        assert current_generation_ids(rows, stamp) & prune_candidate_ids(rows, stamp) == set()

    @pytest.mark.parametrize(
        "stamp",
        [
            None,
            PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=1, fetch_id=""),
            PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=0, fetch_id="run-new"),
        ],
    )
    def test_an_unusable_stamp_establishes_nothing(self, stamp):
        """No stamp, no generation, or an empty fetch cannot vouch for any row."""
        assert current_generation_ids([_row(25135, "run-new")], stamp) == set()

    def test_no_rows_yields_nothing(self):
        stamp = PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=1, fetch_id="run-new")
        assert current_generation_ids([], stamp) == set()
