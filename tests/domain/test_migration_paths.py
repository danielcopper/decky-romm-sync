"""Tests for the RetroDECK pending-home kernel (``domain.migration_paths``).

Covers the pure change-detection transition, the longest-prefix base matching
and remap used to relocate a tracked path, the stranded-source candidate
ordering, and the kv reassembly helper. The hand-enumerated transitions pin
each named case from the #1042 design; the Hypothesis property replays random
home-change sequences and asserts the safety invariant directly — no home that
may hold un-migrated files is ever dropped from the pending set except by a
single-hop revert (the one documented, deliberately-preserved drop).
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from domain.migration_paths import (
    PendingHomeTransition,
    compute_pending_home_transition,
    match_pending_base,
    pending_homes_from_kv,
    remap_under_current,
    stranded_source_candidates,
)

A = "/home/a/retrodeck"
B = "/home/b/retrodeck"
C = "/home/c/retrodeck"
D = "/home/d/retrodeck"


class TestComputePendingHomeTransition:
    def test_first_run_stores_home_no_pending_no_emit(self):
        t = compute_pending_home_transition("", A, [])
        assert t.kind == "first_run"
        assert t.home == A
        assert t.pending == ()
        assert t.previous == ""
        assert t.emit_cleared is False

    def test_unchanged_when_current_equals_stored(self):
        t = compute_pending_home_transition(A, A, [])
        assert t.kind == "unchanged"

    def test_first_change_records_previous(self):
        """A→B with nothing pending: B is live, A becomes the sole pending home."""
        t = compute_pending_home_transition(A, B, [])
        assert t.kind == "changed"
        assert t.home == B
        assert t.pending == (A,)
        assert t.previous == A
        assert (t.emit_old, t.emit_new, t.emit_cleared) == (A, B, False)

    def test_chained_change_appends_hop_keeps_previous(self):
        """A→B then B→C before migrating: previous stays A, B is appended as a hop (#1042)."""
        t = compute_pending_home_transition(B, C, [A])
        assert t.kind == "changed"
        assert t.home == C
        assert t.pending == (A, B)
        assert t.previous == A
        # Banner still reads "From: A → To: C".
        assert (t.emit_old, t.emit_new) == (A, C)

    def test_triple_chain_accumulates_all_left_behind_homes(self):
        t = compute_pending_home_transition(C, D, [A, B])
        assert t.pending == (A, B, C)
        assert t.previous == A
        assert (t.emit_old, t.emit_new) == (A, D)

    def test_simple_revert_clears_and_emits_cleared(self):
        """Revert to the SOLE pending home → full clear (shipped UX preserved)."""
        t = compute_pending_home_transition(B, A, [A])
        assert t.kind == "cleared"
        assert t.home == A
        assert t.pending == ()
        assert (t.emit_old, t.emit_new, t.emit_cleared) == (A, A, True)

    def test_chained_revert_keeps_pending(self):
        """Revert to the oldest home while a later hop remains → NOT cleared."""
        t = compute_pending_home_transition(C, A, [A, B])
        assert t.kind == "changed"
        assert t.home == A
        # We left C (added), arrived at A (removed): pending is [B, C].
        assert t.pending == (B, C)
        assert t.previous == B

    def test_move_back_to_hop_removes_it_from_pending(self):
        """Move back onto an intermediate hop → that hop leaves the pending set."""
        t = compute_pending_home_transition(C, B, [A, B])
        assert t.kind == "changed"
        assert t.home == B
        assert t.pending == (A, C)
        assert t.previous == A

    def test_repeat_home_is_deduped(self):
        """The home we are leaving is not duplicated when already pending."""
        # Contrived state: stored home already appears in pending.
        t = compute_pending_home_transition(B, D, [A, B])
        assert t.pending == (A, B)
        assert t.pending.count(B) == 1

    def test_returns_pendinghometransition(self):
        assert isinstance(compute_pending_home_transition("", A, []), PendingHomeTransition)


class TestMatchPendingBase:
    def test_matches_single_home(self):
        assert match_pending_base(f"{A}/roms/n64/z.z64", [A]) == A

    def test_longest_prefix_wins_for_nested_homes(self):
        nested = f"{A}/inner"
        assert match_pending_base(f"{nested}/roms/x", [A, nested]) == nested

    def test_no_match_returns_none(self):
        assert match_pending_base(f"{B}/roms/x", [A]) is None

    def test_separator_guard_rejects_false_prefix(self):
        assert match_pending_base("/foobar/x", ["/foo"]) is None

    def test_blank_home_never_matches(self):
        assert match_pending_base("/x/roms/a", ["", A]) is None


class TestRemapUnderCurrent:
    def test_maps_relative_spot_under_current_home(self):
        assert remap_under_current(f"{A}/roms/n64/z.z64", A, C) == f"{C}/roms/n64/z.z64"

    def test_maps_rom_dir(self):
        assert remap_under_current(f"{A}/roms/psx/FF7", A, C) == f"{C}/roms/psx/FF7"


class TestStrandedSourceCandidates:
    def test_probes_other_homes_newest_first_skipping_base(self):
        rel = "roms/n64/z.z64"
        assert stranded_source_candidates(rel, A, [A, B, C]) == [
            f"{C}/{rel}",
            f"{B}/{rel}",
        ]

    def test_empty_when_only_base_is_pending(self):
        assert stranded_source_candidates("roms/x", A, [A]) == []

    def test_skips_blank_homes(self):
        assert stranded_source_candidates("roms/x", A, ["", A, B]) == [f"{B}/roms/x"]


class TestPendingHomesFromKv:
    def test_empty_previous_yields_empty_list(self):
        assert pending_homes_from_kv("", None) == []
        assert pending_homes_from_kv("", '["/anything"]') == []

    def test_single_hop_no_hops_key(self):
        assert pending_homes_from_kv(A, None) == [A]

    def test_previous_plus_hops(self):
        assert pending_homes_from_kv(A, json.dumps([B, C])) == [A, B, C]

    def test_empty_hops_array(self):
        assert pending_homes_from_kv(A, "[]") == [A]

    def test_corrupt_json_degrades_to_previous_without_raising(self):
        """Truncated / invalid JSON is ignored — the previous marker is kept."""
        assert pending_homes_from_kv(A, "{bad") == [A]

    def test_numeric_hops_degrades_to_previous(self):
        assert pending_homes_from_kv(A, "123") == [A]

    def test_null_hops_degrades_to_previous(self):
        assert pending_homes_from_kv(A, "null") == [A]

    def test_quoted_string_hops_is_not_spread_into_characters(self):
        """A bare JSON string must NOT become one hop per character — degrade to previous."""
        assert pending_homes_from_kv(A, '"/rogue"') == [A]

    def test_list_with_non_string_entry_degrades_to_previous(self):
        assert pending_homes_from_kv(A, json.dumps([B, 1])) == [A]

    def test_list_with_empty_string_entry_degrades_to_previous(self):
        assert pending_homes_from_kv(A, json.dumps([B, ""])) == [A]


# --- Hypothesis safety property ------------------------------------------------

_homes = st.sampled_from([A, B, C, D])
_sequences = st.lists(_homes, min_size=1, max_size=12)


@given(sequence=_sequences)
def test_property_pending_never_drops_a_stateful_home(sequence: list[str]) -> None:
    """Replay random home-change sequences; the pending set is always safe.

    After every detected change the following hold:

    * the current home is never in the pending set (you cannot be pending
      against where you already are),
    * the pending set has no duplicates,
    * ``previous`` is exactly the head of the pending set, and
    * SAFETY: every home that may still hold un-migrated files (the reference
      set ``S`` maintained independently below) is contained in the pending
      set — the #1042 invariant. The one allowed drop is a single-hop revert,
      which fully drains ``S`` (its stranded-intermediate hole is a separate,
      documented issue and out of scope here).
    """
    stored = ""
    pending: list[str] = []
    state: set[str] = set()  # homes that may hold un-migrated files (excl. current)

    for home in sequence:
        t = compute_pending_home_transition(stored, home, pending)

        assert home not in t.pending
        assert len(t.pending) == len(set(t.pending))
        assert t.previous == (t.pending[0] if t.pending else "")

        if t.kind in ("first_run", "cleared"):
            # First occupancy, or a full revert-clear: nothing left pending.
            state = set()
        elif t.kind == "changed":
            # We left ``stored`` (it may now hold state) and arrived at ``home``.
            state = (state | {stored}) - {home}
        # "unchanged" leaves state as-is.

        assert state <= set(t.pending), (sequence, home, state, t.pending)

        stored = t.home
        pending = list(t.pending)
