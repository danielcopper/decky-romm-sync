"""Unit tests for the session-budget decision kernel.

Boundary-focused: the gate/prognosis fire exactly at the effective ceiling and
not one KB below it, the worst-case rate is the default, an explicit rate
overrides it (the PR-2 cover-term seam), and the zero-item / large-item edges
behave. The gate/prognosis kernels take ints only, so their ``None`` handling is
the caller's fail-open skip and is not tested here; ``session_memory_delta`` is
the one kernel that DOES accept ``None`` endpoints, so its ``None`` cases live
below.
"""

from __future__ import annotations

from domain.session_budget import (
    CLIFF_KB,
    EFFECTIVE_CEILING_KB,
    FULL_CHUNK_WORST_KB,
    GC_SKIP_BELOW_KB,
    POST_RUN_ADVISORY_KB,
    SAFETY_MARGIN_KB,
    UPDATE_TOUCH_KB,
    WORST_CASE_CREATE_KB,
    gate_decision,
    post_run_advisory,
    predict_run_crosses,
    resume_would_proceed,
    session_memory_delta,
)


def test_effective_ceiling_is_cliff_minus_margin() -> None:
    assert EFFECTIVE_CEILING_KB == CLIFF_KB - SAFETY_MARGIN_KB


def test_margin_and_ceiling_pin_the_widened_thrash_cushion() -> None:
    # The margin was widened 150 → 250 MB so the gate pauses ~2.2 GB, keeping a
    # chunk's transient peak out of V8's aggressive-GC thrash zone below the cliff.
    assert SAFETY_MARGIN_KB == 250_000
    assert EFFECTIVE_CEILING_KB == 2_200_000


# ── gate_decision ────────────────────────────────────────────────


def test_gate_proceeds_with_ample_headroom() -> None:
    decision = gate_decision(rss_kb=440_000, chunk_items=200)
    assert decision.should_pause is False
    assert decision.projected_kb == 440_000 + 200 * WORST_CASE_CREATE_KB
    assert decision.threshold_kb == EFFECTIVE_CEILING_KB


def test_gate_pauses_when_projection_reaches_ceiling_exactly() -> None:
    # Choose rss so that rss + 200*1500 == ceiling exactly — the >= boundary pauses.
    rss = EFFECTIVE_CEILING_KB - 200 * WORST_CASE_CREATE_KB
    decision = gate_decision(rss_kb=rss, chunk_items=200)
    assert decision.projected_kb == EFFECTIVE_CEILING_KB
    assert decision.should_pause is True


def test_gate_proceeds_one_kb_below_ceiling() -> None:
    rss = EFFECTIVE_CEILING_KB - 200 * WORST_CASE_CREATE_KB - 1
    decision = gate_decision(rss_kb=rss, chunk_items=200)
    assert decision.projected_kb == EFFECTIVE_CEILING_KB - 1
    assert decision.should_pause is False


def test_gate_zero_items_never_pauses_below_ceiling() -> None:
    # A zero-item chunk projects to the current RSS; below the ceiling → proceed.
    assert gate_decision(rss_kb=EFFECTIVE_CEILING_KB - 1, chunk_items=0).should_pause is False


def test_gate_zero_items_at_ceiling_pauses() -> None:
    # Already at/over the ceiling with no additional items still pauses (>=).
    assert gate_decision(rss_kb=EFFECTIVE_CEILING_KB, chunk_items=0).should_pause is True


def test_gate_explicit_per_item_rate_overrides_default() -> None:
    # A higher per-item rate (the PR-2 cover-term seam) pauses where the default
    # rate would have proceeded, for the same RSS + chunk size.
    rss = 2_000_000
    assert gate_decision(rss_kb=rss, chunk_items=100, per_item_kb=WORST_CASE_CREATE_KB).should_pause is False
    assert gate_decision(rss_kb=rss, chunk_items=100, per_item_kb=5_000).should_pause is True


def test_gate_default_limit_is_the_effective_ceiling() -> None:
    # No explicit limit → the projection is measured against the effective ceiling,
    # and the verdict reports that ceiling as the threshold.
    decision = gate_decision(rss_kb=1_000_000, chunk_items=200)
    assert decision.threshold_kb == EFFECTIVE_CEILING_KB


def test_gate_cliff_limit_proceeds_one_kb_below_the_cliff_bound() -> None:
    # The run's FIRST chunk is gated against the CLIFF (not the ceiling). With a
    # full 200-item chunk the projection is rss + 300_000; one KB below the cliff
    # bound the run proceeds and the verdict reports the cliff as the threshold.
    rss = CLIFF_KB - 200 * WORST_CASE_CREATE_KB - 1
    decision = gate_decision(rss_kb=rss, chunk_items=200, limit_kb=CLIFF_KB)
    assert decision.projected_kb == CLIFF_KB - 1
    assert decision.threshold_kb == CLIFF_KB
    assert decision.should_pause is False


def test_gate_cliff_limit_pauses_when_projection_reaches_the_cliff() -> None:
    # At the cliff bound the full-chunk projection equals the cliff exactly; the
    # gate pauses at ``>=`` so this first chunk re-pauses with zero progress.
    rss = CLIFF_KB - 200 * WORST_CASE_CREATE_KB
    decision = gate_decision(rss_kb=rss, chunk_items=200, limit_kb=CLIFF_KB)
    assert decision.projected_kb == CLIFF_KB
    assert decision.should_pause is True


def test_gate_cliff_limit_still_pauses_a_run_that_would_pass_the_ceiling_check() -> None:
    # A first chunk at 2.199 GB clears the CLIFF check for a light (1-item) chunk
    # but a full 200-item chunk projects 2.199 + 0.3 = 2.499 GB ≥ the 2.45 GB cliff,
    # so the same RSS that the old absolute-ceiling check let through now re-pauses.
    rss = 2_199_000
    assert gate_decision(rss_kb=rss, chunk_items=1, limit_kb=CLIFF_KB).should_pause is False
    assert gate_decision(rss_kb=rss, chunk_items=200, limit_kb=CLIFF_KB).should_pause is True


# ── predict_run_crosses ──────────────────────────────────────────


def test_predict_false_for_small_run_from_fresh_baseline() -> None:
    assert predict_run_crosses(rss_kb=440_000, new_items=100, changed_items=0) is False


def test_predict_true_when_planned_creates_cross_ceiling() -> None:
    # From a fresh ~440 MB baseline, enough planned creates to reach the ceiling.
    planned = (EFFECTIVE_CEILING_KB - 440_000) // WORST_CASE_CREATE_KB + 1
    assert predict_run_crosses(rss_kb=440_000, new_items=planned, changed_items=0) is True


def test_predict_creates_boundary_is_inclusive() -> None:
    rss = EFFECTIVE_CEILING_KB - 1000 * WORST_CASE_CREATE_KB
    assert predict_run_crosses(rss_kb=rss, new_items=1000, changed_items=0) is True
    assert predict_run_crosses(rss_kb=rss - 1, new_items=1000, changed_items=0) is False


def test_predict_changed_items_priced_at_lighter_update_rate() -> None:
    # The same count of CHANGED items costs less than CREATES, so a run that would
    # cross when priced as creates does not cross when priced as updates.
    rss = EFFECTIVE_CEILING_KB - 1000 * WORST_CASE_CREATE_KB
    assert predict_run_crosses(rss_kb=rss, new_items=1000, changed_items=0) is True
    assert predict_run_crosses(rss_kb=rss, new_items=0, changed_items=1000) is False
    # Priced explicitly at the update rate, the changed boundary is inclusive.
    rss_u = EFFECTIVE_CEILING_KB - 1000 * UPDATE_TOUCH_KB
    assert predict_run_crosses(rss_kb=rss_u, new_items=0, changed_items=1000) is True


def test_predict_unchanged_items_are_never_priced() -> None:
    # Only new + changed drive the projection; a huge unchanged re-sync (0 new,
    # 0 changed) never warns regardless of how many rows exist elsewhere.
    assert predict_run_crosses(rss_kb=EFFECTIVE_CEILING_KB - 1, new_items=0, changed_items=0) is False


def test_predict_mixed_new_and_changed_sum() -> None:
    # rss + new*1500 + changed*1000 exactly reaches the ceiling → inclusive True.
    rss = EFFECTIVE_CEILING_KB - (100 * WORST_CASE_CREATE_KB + 100 * UPDATE_TOUCH_KB)
    assert predict_run_crosses(rss_kb=rss, new_items=100, changed_items=100) is True
    assert predict_run_crosses(rss_kb=rss - 1, new_items=100, changed_items=100) is False


def test_predict_zero_items_only_crosses_when_already_at_ceiling() -> None:
    assert predict_run_crosses(rss_kb=EFFECTIVE_CEILING_KB, new_items=0, changed_items=0) is True
    assert predict_run_crosses(rss_kb=EFFECTIVE_CEILING_KB - 1, new_items=0, changed_items=0) is False


# ── post_run_advisory ────────────────────────────────────────────


def test_post_run_advisory_below_threshold_is_false() -> None:
    assert post_run_advisory(POST_RUN_ADVISORY_KB) is False  # strict >, so equal → no nudge
    assert post_run_advisory(POST_RUN_ADVISORY_KB - 1) is False


def test_post_run_advisory_above_threshold_is_true() -> None:
    assert post_run_advisory(POST_RUN_ADVISORY_KB + 1) is True
    assert post_run_advisory(2_400_000) is True


# ── resume_would_proceed ─────────────────────────────────────────


def test_full_chunk_worst_is_chunk_size_times_create_rate() -> None:
    assert FULL_CHUNK_WORST_KB == 200 * WORST_CASE_CREATE_KB  # 300_000


def test_resume_ready_just_below_the_threshold() -> None:
    # Resumable iff rss + one full chunk stays strictly below the ceiling — the
    # highest resumable RSS is one KB under (ceiling - full-chunk).
    rss = EFFECTIVE_CEILING_KB - FULL_CHUNK_WORST_KB - 1
    assert resume_would_proceed(rss) is True


def test_resume_not_ready_at_the_threshold() -> None:
    # At exactly ceiling - full-chunk the projection equals the ceiling, and the gate
    # pauses at ``>=`` — so this is NOT resumable (mirrors the predictive gate).
    rss = EFFECTIVE_CEILING_KB - FULL_CHUNK_WORST_KB
    assert resume_would_proceed(rss) is False


def test_resume_ready_after_a_fresh_restart_baseline() -> None:
    assert resume_would_proceed(440_000) is True


def test_resume_not_ready_at_a_still_high_rss() -> None:
    assert resume_would_proceed(2_000_000) is False  # 2.0 + 0.3 = 2.3 ≥ 2.2 ceiling


# ── GC-skip floor ────────────────────────────────────────────────


def test_gc_skip_floor_leaves_worst_case_below_every_threshold() -> None:
    # The floor's rationale: at the floor, the worst-case max-chunk cost still
    # clears the pause ceiling, and the floor itself clears the advisory — so a GC
    # below it could not flip any decision. Pin that arithmetic so a threshold move
    # can't silently invalidate the skip.
    assert GC_SKIP_BELOW_KB + 500_000 < EFFECTIVE_CEILING_KB
    assert GC_SKIP_BELOW_KB < POST_RUN_ADVISORY_KB


# ── session_memory_delta ─────────────────────────────────────────


def test_memory_delta_positive_growth() -> None:
    assert session_memory_delta(500_000, 1_300_000) == 800_000


def test_memory_delta_negative_when_run_shrank_heap() -> None:
    assert session_memory_delta(1_300_000, 1_000_000) == -300_000


def test_memory_delta_none_when_start_missing() -> None:
    assert session_memory_delta(None, 1_300_000) is None


def test_memory_delta_none_when_end_missing() -> None:
    assert session_memory_delta(500_000, None) is None


def test_memory_delta_none_when_both_missing() -> None:
    assert session_memory_delta(None, None) is None
