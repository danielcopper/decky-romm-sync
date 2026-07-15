"""Tests for domain/skip_prediction.py — plan-time estimate kernels (#1382)."""

import pytest

from domain.skip_prediction import collapsed_shortcut_count, predict_unit_skip


def _kwargs(**overrides):
    """A baseline input set that predicts a skip; each test flips one field."""
    base = {
        "stamp_completed_at": "2025-01-01T00:00:00",
        "stamp_rom_count": 3,
        "unit_rom_count": 3,
        "persisted_count": 3,
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

    def test_zero_persisted_rows_predicts_full_fetch(self):
        assert (
            predict_unit_skip(**_kwargs(persisted_count=0, registry_count=0, stamp_rom_count=0, unit_rom_count=0))
            is False
        )

    def test_persisted_count_mismatch_predicts_full_fetch(self):
        """The gate's final line also requires server rom_count == persisted rows."""
        assert predict_unit_skip(**_kwargs(persisted_count=2)) is False

    def test_zero_bound_rows_predicts_full_fetch(self):
        """Unbind-only rows (mass delete, ADR-0007) mirror nothing — the gate re-fetches."""
        assert predict_unit_skip(**_kwargs(registry_count=0)) is False

    def test_needs_backfill_predicts_full_fetch(self):
        assert predict_unit_skip(**_kwargs(needs_backfill=True)) is False


class TestCollapsedShortcutCount:
    """Distinct group keys count once; keyless rows are singletons."""

    def test_empty_rows_collapse_to_zero(self):
        assert collapsed_shortcut_count([]) == 0

    def test_distinct_keys_count_once_each(self):
        assert collapsed_shortcut_count(["igdb:1:1", "igdb:1:1", "igdb:2:1"]) == 2

    def test_none_keys_are_singletons(self):
        assert collapsed_shortcut_count([None, None, None]) == 3

    def test_mixed_keys_and_singletons(self):
        # One 3-sibling group + one 2-sibling group + two keyless singletons.
        assert collapsed_shortcut_count(["g:a", "g:a", "g:a", "g:b", "g:b", None, None]) == 4

    @pytest.mark.parametrize("keys", [["g:a"], [None]])
    def test_single_row_is_one_shortcut(self, keys):
        assert collapsed_shortcut_count(keys) == 1
