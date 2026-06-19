"""Property-based tests for ``domain.sync_diff.select_stale_removals`` (#1036).

The stale-removal filter is pure and safety-critical: emitting an appId that
the current run just bound would wipe the freshly-synced Steam shortcut (a new
server-issued rom_id reuses an old appId — CRC32 of unchanged exe+name). The
hand-enumerated cases (``test_sync_diff.py``) pin specific shapes; these
properties state the *invariant* across the whole sampled input space.

Invariant:
- No removal carries an appId that the run bound this sync (``synced_app_ids``).
  This is the load-bearing #1036 safety property — no exception, ever.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from domain.sync_diff import select_stale_removals

# A stale candidate is a (rom_id, app_id) tuple; both positive ints.
_candidate = st.tuples(
    st.integers(min_value=1, max_value=10_000),
    st.integers(min_value=1, max_value=10_000),
)
_candidates = st.lists(_candidate, max_size=30)
_synced_app_ids = st.sets(st.integers(min_value=1, max_value=10_000), max_size=30)


@given(candidate_stale=_candidates, synced_app_ids=_synced_app_ids)
def test_never_removes_a_resynced_appid(candidate_stale, synced_app_ids):
    """No emitted removal carries an appId bound this run — the #1036 invariant."""
    result = select_stale_removals(candidate_stale, synced_app_ids)
    assert all(app_id not in synced_app_ids for _rom_id, app_id in result)


@given(candidate_stale=_candidates, synced_app_ids=_synced_app_ids)
def test_result_is_a_candidate_subset_preserving_order(candidate_stale, synced_app_ids):
    """The filter only drops candidates — it never invents, reorders, or mutates one."""
    result = select_stale_removals(candidate_stale, synced_app_ids)
    # Every result entry is a candidate; the relative order of survivors holds.
    expected = [c for c in candidate_stale if c[1] not in synced_app_ids]
    assert result == expected


@given(candidate_stale=_candidates)
def test_empty_synced_is_full_passthrough(candidate_stale):
    """No appId bound this run → every candidate survives unchanged."""
    assert select_stale_removals(candidate_stale, set()) == candidate_stale
