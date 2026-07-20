"""Tests for domain/skip_prediction.py — plan-time estimate kernels (#1382)."""

import pytest

from domain.skip_prediction import collapsed_shortcut_count, predict_unit_skip


def _kwargs(**overrides):
    """A baseline input set that predicts a skip; each test flips one field."""
    base = {
        "stamp_completed_at": "2025-01-01T00:00:00",
        "stamp_rom_count": 3,
        "unit_rom_count": 3,
        "fetched_count": 3,
        "registry_count": 1,
        "needs_backfill": False,
    }
    base.update(overrides)
    return base


class TestPredictUnitSkip:
    """Every LOCAL condition of the wholesale-skip gate flips the prediction."""

    def test_predicts_skip_when_all_local_conditions_hold(self):
        assert predict_unit_skip(**_kwargs()) is True

    def test_no_stamp_completed_at_predicts_full_fetch(self):
        assert predict_unit_skip(**_kwargs(stamp_completed_at=None)) is False

    def test_empty_stamp_completed_at_predicts_full_fetch(self):
        """A falsy (empty-string) stamp timestamp counts as no stamp — mirrors the gate."""
        assert predict_unit_skip(**_kwargs(stamp_completed_at="")) is False

    def test_none_stamp_rom_count_predicts_full_fetch(self):
        assert predict_unit_skip(**_kwargs(stamp_rom_count=None)) is False

    def test_stamp_count_mismatch_predicts_full_fetch(self):
        assert predict_unit_skip(**_kwargs(stamp_rom_count=2)) is False

    def test_zero_countable_rows_predicts_full_fetch(self):
        assert (
            predict_unit_skip(**_kwargs(fetched_count=0, registry_count=0, stamp_rom_count=0, unit_rom_count=0))
            is False
        )

    def test_fetched_count_mismatch_predicts_full_fetch(self):
        """The gate's final line also requires server rom_count == the rows carrying
        the stamp's fetch generation (#1504), not every persisted row."""
        assert predict_unit_skip(**_kwargs(fetched_count=2)) is False

    def test_zero_bound_rows_predicts_full_fetch(self):
        """Unbind-only rows (mass delete, ADR-0007) mirror nothing — the gate re-fetches."""
        assert predict_unit_skip(**_kwargs(registry_count=0)) is False

    def test_needs_backfill_predicts_full_fetch(self):
        assert predict_unit_skip(**_kwargs(needs_backfill=True)) is False


class TestCollapsedShortcutCount:
    """Each group counts max(1, bound rows); keyless rows are singletons.

    Rows are ``(sibling_group_key, is_bound)``; the count mirrors the lane
    selection of ``collapse_sibling_groups`` (ADR-0021) — the property tier
    (``test_skip_prediction_property.py``) pins the two functions together.
    """

    def test_empty_rows_collapse_to_zero(self):
        assert collapsed_shortcut_count([]) == 0

    def test_distinct_unbound_groups_count_once_each(self):
        assert collapsed_shortcut_count([("igdb:1:1", False), ("igdb:1:1", False), ("igdb:2:1", False)]) == 2

    def test_none_keys_are_singletons(self):
        assert collapsed_shortcut_count([(None, False), (None, True), (None, False)]) == 3

    def test_mixed_groups_and_singletons(self):
        # One 3-sibling group (1 bound) + one 2-sibling unbound group + two
        # keyless singletons.
        rows = [
            ("g:a", True),
            ("g:a", False),
            ("g:a", False),
            ("g:b", False),
            ("g:b", False),
            (None, False),
            (None, True),
        ]
        assert collapsed_shortcut_count(rows) == 4

    @pytest.mark.parametrize("rows", [[("g:a", False)], [("g:a", True)], [(None, False)], [(None, True)]])
    def test_single_row_is_one_shortcut(self, rows):
        assert collapsed_shortcut_count(rows) == 1

    def test_grandfathered_group_counts_each_bound_sibling(self):
        # A legacy group with two independently-bound duplicates keeps BOTH
        # shortcuts (ADR-0021 §5) — the estimate must not read one-per-group.
        assert collapsed_shortcut_count([("g:a", True), ("g:a", True), ("g:a", False)]) == 2

    def test_group_with_one_bound_sibling_is_one_shortcut(self):
        assert collapsed_shortcut_count([("g:a", True), ("g:a", False)]) == 1

    def test_all_unbound_group_mints_one_representative(self):
        assert collapsed_shortcut_count([("g:a", False), ("g:a", False)]) == 1

    def test_grandfathered_group_mixed_with_null_singletons(self):
        # 2-bound grandfathered group (2) + all-unbound group (1) + keyless
        # rows (1 each, bound or not).
        rows = [("g:a", True), ("g:a", True), ("g:b", False), ("g:b", False), (None, True), (None, False)]
        assert collapsed_shortcut_count(rows) == 5
