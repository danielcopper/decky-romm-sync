"""Tests for domain/skip_prediction.py — plan-time estimate kernels (#1382)."""

import pytest

from domain.skip_prediction import collapsed_shortcut_count, new_shortcut_count, predict_unit_skip


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


class TestNewShortcutCount:
    """A group with any binding owns its shortcut; everything else is a create.

    Rows are ``(sibling_group_key, is_bound)`` and *unit_rom_count* is the
    server's count for the platform, so ROMs the mirror holds no row for count
    as creates too.
    """

    def test_nothing_known_and_nothing_on_the_server_creates_nothing(self):
        assert new_shortcut_count([], unit_rom_count=0) == 0

    def test_never_synced_platform_creates_every_server_rom(self):
        assert new_shortcut_count([], unit_rom_count=25) == 25

    def test_group_with_a_bound_sibling_creates_nothing(self):
        # The duplicates collapse away — they are not new shortcuts.
        rows = [("g:a", True), ("g:a", False), ("g:a", False)]
        assert new_shortcut_count(rows, unit_rom_count=3) == 0

    def test_grandfathered_group_creates_nothing(self):
        # Two independently-bound duplicates (ADR-0021 §5) keep both shortcuts;
        # neither is new.
        rows = [("g:a", True), ("g:a", True), ("g:a", False)]
        assert new_shortcut_count(rows, unit_rom_count=3) == 0

    def test_group_with_no_binding_anywhere_creates_one_representative(self):
        rows = [("g:a", False), ("g:a", False), ("g:b", False)]
        assert new_shortcut_count(rows, unit_rom_count=3) == 2

    def test_keyless_rows_create_only_while_unbound(self):
        # A keyless row's real group is unknown until the backfill fetch, so an
        # unbound one must be assumed to be its own creation.
        assert new_shortcut_count([(None, False), (None, False), (None, True)], unit_rom_count=3) == 2

    def test_partial_mirror_creates_the_unmirrored_remainder_too(self):
        """The safety-critical shape: a never-synced platform holding only a
        few collection-sibling rows (ADR-0021). Counting the known rows alone
        would price the run at a handful of items instead of the whole
        platform — a short read, the one direction this estimate may not err in.
        """
        rows = [("g:a", True), ("g:b", True), ("g:c", False)]
        # 1 unbound group + the 97 server ROMs no row is held for.
        assert new_shortcut_count(rows, unit_rom_count=100) == 98

    def test_mirror_ahead_of_the_server_never_goes_negative(self):
        # Retained rows for rom_ids the server dropped (ADR-0007) leave more
        # rows than the server reports; the unmirrored term clamps at zero.
        rows = [("g:a", True), ("g:b", True), ("g:c", True)]
        assert new_shortcut_count(rows, unit_rom_count=1) == 0

    def test_creates_and_bound_rows_partition_the_collapsed_count(self):
        """The two terms the seed prices are disjoint and complete: over a fully
        mirrored platform, creates + bound rows is exactly the shortcut count
        the collapse emits — no item priced twice, none dropped.
        """
        rows = [("g:a", True), ("g:a", False), ("g:b", False), ("g:b", False), (None, True), (None, False)]
        bound_rows = sum(1 for _key, is_bound in rows if is_bound)
        assert new_shortcut_count(rows, unit_rom_count=len(rows)) + bound_rows == collapsed_shortcut_count(rows)
