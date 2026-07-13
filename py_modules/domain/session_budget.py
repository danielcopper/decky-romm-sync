"""Session-budget decision kernel — the pure math behind the RSS-based sync gate.

Steam's ``SharedJSContext`` renderer (a child of ``steamwebhelper``) carries a
hard per-session heap budget: it dies of an out-of-memory crash once its RSS
reaches roughly 2.45-2.53 GB and the budget never self-recovers within a session
— only a Steam client restart resets it to a fresh ~400-440 MB baseline. Every
Steam shortcut the plugin creates costs 0.7-1.5 MB of that budget permanently
(the rate is constant per client boot but varies between boots), so a very large
first import can walk the renderer into the cliff.

This module is the pure compute that decides, from a single RSS reading and a
count of work about to be done, whether the run may proceed or must pause at a
chunk boundary. It takes plain integers and returns plain values — no I/O, no
clock, no ``None`` handling (the caller supplies a real reading or skips the gate
entirely when measurement is unavailable). The reader that samples ``/proc`` and
the CDP garbage-collect trigger that settles the heap before a reading live in
``adapters/``; the chunk-loop integration lives in the sync orchestrator.

Constants carry their measurement provenance inline — all figures measured
on-device (Steam Deck, RetroDECK) on 2026-07-10/11.
"""

from __future__ import annotations

from dataclasses import dataclass

# Session cliff: the RSS at which the renderer OOM-crashes. The observed crash
# cluster was 2456 / 2489 / 2514-2516 / 2528 MB RSS across two test days; this is
# the conservative FLOOR of that cluster, so the gate reasons about the earliest
# point a crash was ever seen rather than the average.
CLIFF_KB = 2_450_000

# Headroom below the cliff the gate refuses to spend. A chunk's real cost is only
# known after it is applied, and the per-boot create rate varies, so the gate
# keeps this margin as slack against an over-dense chunk or a rate above the
# modelled worst case. Widened to ~250 MB (from 150) after on-device observation
# (2026-07-12): near V8's heap limit the renderer enters aggressive GC thrash and
# the whole UI turns sluggish *before* it OOM-crashes, so a chunk whose transient
# peak reaches that zone degrades the experience even when it never crashes.
# Pausing 250 MB below the cliff keeps the final chunk's transient peak out of the
# thrash zone — a cushion against sluggishness, not just against the crash.
SAFETY_MARGIN_KB = 250_000

# The RSS ceiling the gate actually pauses at — the cliff minus the margin
# (≈2.20 GB). Projecting the next chunk's cost past this line is what triggers a
# pause. Shared by the per-chunk gate and the post-preview prognosis so both
# reason about the same line.
EFFECTIVE_CEILING_KB = CLIFF_KB - SAFETY_MARGIN_KB

# Worst-case PERMANENT RSS cost of one created shortcut, in KB. Measured creates
# cost 0.7-1.5 MB/item depending on the client boot; the gate uses the top of
# that range so a dense chunk on a bad boot still stops short of the cliff. This
# is the shortcut's own permanent cost; a created shortcut also applies a cover
# through Steam's artwork API, whose transient cost (``COVER_TRANSIENT_KB``) the
# caller adds on top — so ``per_item_kb`` defaults to the bare create rate and the
# orchestrator passes create + cover for a run that applies artwork.
WORST_CASE_CREATE_KB = 1_500

# Worst-case TRANSIENT RSS cost of applying one cover through Steam's native
# artwork API (``SetCustomArtworkForApp``), in KB. A cover costs ~1.0 MB of
# renderer RSS while it is decoded and applied; the cost is fully GC-reclaimable
# (the gate's boundary GC + GC-before-measure settle it), but it is resident at the
# transient peak, so the gate prices it into a create's worst-case chunk cost. Only
# CREATES apply a cover — an updated shortcut keeps its existing grid file — so the
# caller adds this to ``WORST_CASE_CREATE_KB`` for a created item while an update
# stays priced at ``UPDATE_TOUCH_KB`` alone. Measured on-device (Steam Deck,
# RetroDECK) 2026-07-11/12.
COVER_TRANSIENT_KB = 1_000

# Worst-case RSS cost of one UPDATE touch (a Set* walk over an existing shortcut),
# in KB. An update walk of ~2300 items measured ≈ 1.4 GB on-device 2026-07-10
# (~0.6 MB/item); 1000 KB is the rounded-up ceiling. Only CHANGED items pay this —
# fully-unchanged items skip the per-item Set* touch entirely and cost nothing, so
# they are not projected at all.
UPDATE_TOUCH_KB = 1_000

# Worst-case RSS cost of applying one FULL apply chunk — 200 fresh creates, each
# priced at the worst-case create rate PLUS its transient cover term. 200 is the
# apply chunk size (``_APPLY_CHUNK_SIZE`` in the sync orchestrator), kept as a literal
# here so the domain kernel stays service-free. This is the predictive gate's own
# per-chunk projection for a maxed chunk of cover-applying creates, reused by
# ``resume_would_proceed`` to decide whether a paused run could resume and make at
# least one chunk of forward progress — so it prices the same cover term the
# steady-state chunk gate does, or a resume could promise progress the gate then denies.
FULL_CHUNK_WORST_KB = 200 * (WORST_CASE_CREATE_KB + COVER_TRANSIENT_KB)

# How many full worst-case chunks of headroom ``resume_would_proceed`` demands
# below the effective ceiling before it announces a paused run as resumable.
# MUST be > 1: one chunk is exactly the gate's own pause point, so a one-chunk
# bar sits on the very RSS level every pause lands on and Steam's own small
# frees flicker the verdict (observed on-device 2026-07-13: the paused banner
# flipped to "memory is free again" at the pause level and hid the restart
# button). Two chunks (≈1 GB below the ceiling, ≈1.2 GB bar) is in practice
# reachable only through a real Steam restart — the state the flip exists to
# detect.
RESUME_HEADROOM_CHUNKS = 2

# Post-run advisory floor. A run that ends with RSS above this (≈1.8 GB) has
# spent most of the session budget; the next large operation is likely to pause
# or crash, so the UI recommends a Steam restart. Deliberately well below the
# ceiling — this is a "restart soon" nudge, not the hard pause line.
POST_RUN_ADVISORY_KB = 1_800_000

# RSS floor below which the chunk gate skips its GC-before-measure round-trip.
# A raw reading (no GC) still holds transient garbage, so the true settled
# resident value can only be LOWER than the raw one. Below this floor even the
# most conservative check passes every threshold — 1.5 GB + 0.5 GB max chunk
# worst-case = 2.0 < 2.2 GB ceiling, and 1.5 < 1.8 GB advisory — so a GC could
# not change any decision here. The gate therefore trusts the raw reading and
# skips the ~5 s GC entirely, making small syncs pay zero GC cost. Only when the
# raw reading is at/above this floor is the GC worth its round-trip to settle the
# reading before the gate reasons about the ceiling.
GC_SKIP_BELOW_KB = 1_500_000


@dataclass(frozen=True)
class GateDecision:
    """The per-chunk gate verdict plus the numbers behind it (for logging).

    ``should_pause`` is the only field the caller acts on; ``projected_kb`` and
    ``threshold_kb`` are surfaced so the pause log line can state exactly why the
    gate fired without recomputing the arithmetic.
    """

    should_pause: bool
    projected_kb: int
    threshold_kb: int


def chunk_worst_cost_kb(creates: int, updates: int) -> int:
    """Worst-case renderer-RSS cost of applying a chunk of *creates* + *updates* items.

    Composition pricing for the delta-restricted apply (#1383): now that a chunk
    carries only new + changed items, a CREATE is priced at the worst-case create
    rate PLUS its transient cover term (a created shortcut applies a cover through
    Steam's artwork API), while an UPDATE — a changed/rebind item's Set* walk over
    an existing shortcut — is priced at the lighter update rate (it reuses its
    existing grid file, no cover applied). The gate reasons about the resulting
    projected chunk cost instead of pricing every item as a cover-applying create.
    """
    return creates * (WORST_CASE_CREATE_KB + COVER_TRANSIENT_KB) + updates * UPDATE_TOUCH_KB


def gate_decision(
    rss_kb: int,
    chunk_items: int = 0,
    per_item_kb: int = WORST_CASE_CREATE_KB,
    limit_kb: int = EFFECTIVE_CEILING_KB,
    *,
    cost_kb: int | None = None,
) -> GateDecision:
    """Decide whether applying the next chunk would cross ``limit_kb``.

    Pauses iff the current renderer RSS plus the chunk's projected worst-case cost
    reaches ``limit_kb``. The chunk cost is either supplied directly as
    ``cost_kb`` (the composition-priced path — see :func:`chunk_worst_cost_kb`,
    creates and updates weighted differently) or, when ``cost_kb`` is ``None``,
    computed as the uniform ``chunk_items * per_item_kb`` (every item priced the
    same). Either way the projection is deliberately worst-case so the gate errs
    toward pausing early — a false pause costs a Steam restart, a false proceed
    costs a renderer crash mid-apply.

    ``limit_kb`` selects which line the projection is measured against, chosen by
    the chunk's position in the run. Every LATER chunk uses the default effective
    ceiling (``cliff - margin``), keeping the anti-thrash safety margin intact. A
    run's FIRST chunk instead passes ``CLIFF_KB``: forward progress must be
    guaranteed (the run has to apply at least one chunk or it would loop forever
    on a no-progress pause), so that one chunk is allowed to spend into the safety
    margin — but the predictive projection still stops it before the crash line
    itself. So the first chunk trades the margin's cushion for guaranteed
    progress; it can never be projected to peak past the cliff.
    """
    projected_kb = rss_kb + (cost_kb if cost_kb is not None else chunk_items * per_item_kb)
    return GateDecision(
        should_pause=projected_kb >= limit_kb,
        projected_kb=projected_kb,
        threshold_kb=limit_kb,
    )


def predict_run_crosses(
    rss_kb: int,
    new_items: int,
    changed_items: int,
    create_kb: int = WORST_CASE_CREATE_KB,
    update_kb: int = UPDATE_TOUCH_KB,
) -> bool:
    """Prognose for the post-preview advisory: will the whole run cross the ceiling?

    Projects each planned CREATE at the worst-case create rate and each planned
    UPDATE (a changed item's Set* walk) at the lighter update rate; fully-unchanged
    items are not projected at all, since they skip the per-item touch and cost no
    renderer heap. A ``True`` result tells the UI to warn up front that the sync
    will likely pause partway (and can always be resumed) — it does not stop
    anything itself. A fully-unchanged re-sync therefore never warns.
    """
    return rss_kb + new_items * create_kb + changed_items * update_kb >= EFFECTIVE_CEILING_KB


def post_run_advisory(rss_kb: int) -> bool:
    """Whether a finished run's RSS is high enough to recommend a Steam restart."""
    return rss_kb > POST_RUN_ADVISORY_KB


def resume_would_proceed(rss_kb: int) -> bool:
    """Whether a resume would make sustained progress under the steady-state predictive gate.

    Requires headroom for {RESUME_HEADROOM_CHUNKS} full worst-case chunks below
    the effective ceiling — deliberately MORE than the single chunk the gate
    itself requires to proceed. One chunk of headroom would put this bar exactly
    on the pause point (the gate pauses at ``rss + one chunk >= ceiling``), where
    Steam's own small frees oscillate the reading across the line: the paused
    banner then flickers to "memory is free again" and hides the restart button
    while the heap is still pinned at the pause level and a resume could make at
    most one chunk of progress. Demanding room for several chunks separates the
    only state worth announcing — a real Steam restart at the fresh baseline —
    from hovering at the pause point, and doubles as hysteresis against the
    flicker. ``None`` handling (unreadable RSS → undecidable) stays with the
    caller.
    """
    return rss_kb + RESUME_HEADROOM_CHUNKS * FULL_CHUNK_WORST_KB < EFFECTIVE_CEILING_KB


def session_memory_delta(start_kb: int | None, end_kb: int | None) -> int | None:
    """Signed renderer-RSS growth across a run (``end - start``), in KB.

    Positive means the run grew the renderer heap, negative means it shrank
    (a GC or reload reclaimed more than the run added). Returns ``None`` when
    either endpoint was unmeasurable, so the UI shows no delta rather than a
    number derived from a missing reading.
    """
    if start_kb is None or end_kb is None:
        return None
    return end_kb - start_kb
