"""Property-based tests for ``domain.platform_prefs`` (#1007).

The per-platform "all enabled" defaulting kernel is what stood between a
single un-toggle and a silent mass-stale wipe. Hand-enumerated cases
(``test_platform_prefs.py``) pin specific shapes; these properties state
the *invariants* the kernel must hold across the whole id space.

Property-test convention — pinning open bugs
---------------------------------------------
A property states the TRUE invariant. If it FAILS today, the invariant's
bug is still open, so the property is pinned ``@pytest.mark.xfail(
strict=True, reason="#<issue>: …")``. The fix in this PR closes #1007, so
the properties below are expected to pass LIVE (regression guards), not
xfail-pinned.

Invariants encoded here:
- Inv1: materialization is semantics-preserving for the initial all-True
  state — resolving any id over the materialized map equals resolving it
  over the empty sentinel (both True).
- Inv2 (#1007): after materializing the full map, flipping ONE id to False
  never changes any OTHER id's resolved value. This is exactly the
  invariant the bug violated — a single write turned the empty sentinel
  into a one-entry map and flipped every absent id to disabled.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from domain.platform_prefs import materialize_enabled_platforms, resolve_sync_enabled

# Platform ids are stringified ints on the wire; a small, distinct, non-empty
# set keeps the search focused on the defaulting logic, not id formatting.
_platform_ids = st.lists(
    st.integers(min_value=1, max_value=999).map(str),
    min_size=1,
    max_size=12,
    unique=True,
)


@given(platform_ids=_platform_ids)
def test_materialization_preserves_initial_all_enabled(platform_ids: list[str]) -> None:
    """Inv1: every shown id resolves True before and after materialization."""
    materialized = materialize_enabled_platforms({}, platform_ids)
    assert materialized is not None
    for pid in platform_ids:
        # Sentinel default and the explicit map agree: all enabled.
        assert resolve_sync_enabled({}, pid) is True
        assert resolve_sync_enabled(materialized, pid) is True


@given(platform_ids=_platform_ids, flip_index=st.integers(min_value=0))
def test_single_off_toggle_does_not_disable_others(platform_ids: list[str], flip_index: int) -> None:
    """Inv2 (#1007): one un-toggle leaves every other platform's value intact."""
    materialized = materialize_enabled_platforms({}, platform_ids)
    assert materialized is not None

    target = platform_ids[flip_index % len(platform_ids)]
    # Single-key write exactly as ``save_platform_sync`` does it.
    materialized[target] = False

    for pid in platform_ids:
        expected = pid != target
        assert resolve_sync_enabled(materialized, pid) is expected


@given(platform_ids=_platform_ids, flip_index=st.integers(min_value=0))
def test_single_on_toggle_does_not_disable_others(platform_ids: list[str], flip_index: int) -> None:
    """Inv2 (#1007): re-toggling one platform ON keeps every other enabled."""
    materialized = materialize_enabled_platforms({}, platform_ids)
    assert materialized is not None

    target = platform_ids[flip_index % len(platform_ids)]
    materialized[target] = True

    for pid in platform_ids:
        assert resolve_sync_enabled(materialized, pid) is True
