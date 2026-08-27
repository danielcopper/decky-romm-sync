"""Steam's per-session renderer-heap budget — measurement, pause, and reporting.

Owns every renderer-RSS **reading** the sync subsystem takes, and the verdicts
drawn from one: the GC-settled measurement itself, the chunk-boundary pause
decision, the post-preview prognosis, the run-start baseline behind the last-run
delta, and the live reading the QAM banners poll. The pure arithmetic behind
every one of those decisions is :mod:`domain.session_budget`; the ``/proc``
reader and the CDP garbage-collect trigger are adapters reached through the
``RendererRssFn`` / ``RendererGcFn`` seams. What belongs here is the
orchestration between the two — measure, ask the domain, record the verdict on
the shared :class:`LibrarySyncStateBox`.

Two sites in :mod:`services.library.sync_orchestrator` still price against the
budget, from a reading this module handed them rather than one they took:
``_clip_cover_refreshes`` (trimming the cover-refresh list to a chunk's leftover
headroom) and the terminal ``session_memory_delta`` / ``post_run_advisory``
computation in ``_finalize_per_unit``. They sit with the chunk loop and the
finalize phase they belong to; what never happens outside this module is taking
a reading.

Fail-open is the contract of every path: an unavailable reading, or any seam
error, degrades to "no verdict" (no pause, no warning, no number) and never
raises into the caller. Steam's memory is an advisory input to a sync, never a
reason one fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.session_budget import (
    CLIFF_KB,
    COVER_TRANSIENT_KB,
    EFFECTIVE_CEILING_KB,
    GC_SKIP_BELOW_KB,
    POST_RUN_ADVISORY_KB,
    WORST_CASE_CREATE_KB,
    chunk_worst_cost_kb,
    gate_decision,
    predict_run_crosses,
    resume_would_proceed,
)

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.library._state import LibrarySyncStateBox
    from services.protocols import RendererGcFn, RendererRssFn


# Terminal reason when the run paused itself at a chunk boundary because the
# renderer's RSS is near Steam's per-session heap budget (the session-budget gate,
# #1383). Distinct from the heartbeat-timeout reason so the UI shows resume-friendly
# guidance ("restart Steam, then Resume Sync") rather than a crash message. Stored
# in ``sync_runs.error`` and surfaced in the ``sync_complete`` payload.
SYNC_PAUSED_BUDGET = "Sync paused: Steam's memory is nearly full. Restart Steam when convenient, then Resume Sync."

# Worst-case per-item cost of a created shortcut when the apply also pushes its
# cover through Steam's artwork API: the shortcut's permanent create cost plus the
# cover's transient peak. The chunk gate prices every emitted item at this rate
# (worst case = every item a cover-applying create) and the preview prognosis
# prices each planned CREATE at it; a CHANGED item stays at the lighter
# ``UPDATE_TOUCH_KB`` (an update reuses its existing grid file, no cover applied).
_CREATE_WITH_COVER_KB = WORST_CASE_CREATE_KB + COVER_TRANSIENT_KB


@dataclass(frozen=True)
class SessionBudgetMonitorConfig:
    """Frozen wiring bundle handed to ``SessionBudgetMonitor.__init__``.

    Holds the runtime infrastructure the measurement needs (the loop the blocking
    ``/proc`` read and the CDP round-trip are offloaded to, and the logger the
    fail-open paths note skipped readings on), the shared
    :class:`LibrarySyncStateBox` the pause verdict and the run-scoped budget
    counters live on, and the two renderer seams: ``renderer_rss`` samples the
    Steam renderer's resident heap, ``renderer_gc`` settles it before a reading
    near the ceiling.
    """

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    sync_state_box: LibrarySyncStateBox
    renderer_rss: RendererRssFn
    renderer_gc: RendererGcFn


class SessionBudgetMonitor:
    """Steam's renderer-heap budget: what it reads, what it decides, what it reports."""

    def __init__(self, *, config: SessionBudgetMonitorConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._sync_state = config.sync_state_box
        self._renderer_rss = config.renderer_rss
        self._renderer_gc = config.renderer_gc

    # ── Measurement ──────────────────────────────────────────────

    async def record_run_start_baseline(self) -> None:
        """Re-arm measurement for a fresh run and stamp its RSS baseline.

        The baseline is a RAW read (no GC — the settle isn't worth it for an
        informational number) taken before any chunk is applied, so even a
        fully-incremental-skip run (nothing to apply, no chunk gate ever fires) still
        records one and reports an honest ≈ "+0.0 GB" delta instead of wiping it to
        ``None``. Fail-open: an unavailable reading leaves the baseline ``None`` (the
        delta is then unmeasurable). ``last_run_delta_kb`` is deliberately NOT reset
        here (it retains the previous clean run's delta for QAM remounts).

        The re-arm comes first and is not optional: :meth:`measure_rss` latches
        ``budget_measure_unavailable_logged`` on the first unreadable reading and then
        short-circuits every later read, so a run that starts without clearing it
        inherits the previous run's verdict and the gate stays silently off for the
        life of the process. Clearing it here — in the one method every run entry
        passes through — keeps the latch's lifetime inside the module that sets it.
        """
        self._sync_state.budget_measure_unavailable_logged = False
        self._sync_state.run_start_rss_kb = None
        try:
            self._sync_state.run_start_rss_kb = await self._loop.run_in_executor(None, self._renderer_rss)
        except Exception as e:  # fail-open: the baseline read must never block a sync
            self._logger.debug(f"Session-budget run-start baseline read skipped: {e}")

    async def measure_rss(self) -> int | None:
        """Read RSS, GC-settling it first only when it matters — the measure seam.

        Reads RSS raw first (no GC). When the raw reading is already below
        :data:`GC_SKIP_BELOW_KB` it is returned as-is: a raw reading still holds
        transient garbage, so the true settled value can only be lower, and below
        that floor even the most conservative check passes every threshold — the
        ~5 s GC round-trip could not change any decision, so a small sync pays zero
        GC cost. Only a raw reading at/above the floor pays for a GC and a re-read,
        so the gate reasons about the settled heap near the ceiling.

        Returns the RSS in KB, or ``None`` when measurement is unavailable or any
        seam raises (fail-open). Once a run finds RSS unavailable it stays that way
        (no ``steamwebhelper`` / unreadable ``/proc``), so the once-per-run flag
        both suppresses repeat logging AND short-circuits further read attempts for
        the rest of the run.
        """
        box = self._sync_state
        if box.budget_measure_unavailable_logged:
            return None
        try:
            raw_kb = await self._loop.run_in_executor(None, self._renderer_rss)
            if raw_kb is None:
                box.budget_measure_unavailable_logged = True
                self._logger.debug("Session-budget measurement unavailable: renderer RSS not readable")
                return None
            if raw_kb < GC_SKIP_BELOW_KB:
                return raw_kb
            await self._loop.run_in_executor(None, self._renderer_gc)
            rss_kb = await self._loop.run_in_executor(None, self._renderer_rss)
        except Exception as e:  # fail-open: measurement must never block a sync
            self._logger.debug(f"Session-budget measurement skipped: {e}")
            return None
        if rss_kb is None:
            box.budget_measure_unavailable_logged = True
            self._logger.debug("Session-budget measurement unavailable: renderer RSS not readable")
        return rss_kb

    # ── Decisions ────────────────────────────────────────────────

    async def maybe_pause_for_budget(
        self, *, creates: int, updates: int, limit_kb: int = EFFECTIVE_CEILING_KB
    ) -> int | None:
        """GC, measure renderer RSS, and pause the run if this chunk would cross ``limit_kb``.

        Fired at a chunk boundary before emitting the next chunk's *creates* +
        *updates* shortcuts. The chunk is priced by composition
        (:func:`domain.session_budget.chunk_worst_cost_kb`): creates at the
        create+cover rate, updates (changed / rebind) at the lighter Set*-walk rate
        — the delta apply no longer prices every item as a cover-applying create.
        :func:`domain.session_budget.gate_decision` decides whether the projected
        cost crosses ``limit_kb`` — the effective ceiling for a later chunk, or the
        cliff itself for the run's first chunk (whose forward-progress guarantee is
        allowed to spend the safety margin but is still projected to stop before the
        crash line). On a pause it sets ``run_paused`` with the distinct
        session-budget reason and requests cancel — the chunk loop's next
        ``is_cancelling`` check returns cleanly with the prior chunks committed, and
        the terminal finalize records the resumable ``paused`` state.

        Returns the settled RSS reading (KB) so the caller can budget additive
        chunk work (the #1386 cover-refresh clip) against the same measurement,
        or ``None`` when measurement was unavailable / the gate errored.

        Fail-open throughout: an unavailable reading or any seam error skips the gate
        entirely — measurement must never block a sync.
        """
        box = self._sync_state
        try:
            rss_kb = await self.measure_rss()
            if rss_kb is None:
                return None
            cost_kb = chunk_worst_cost_kb(creates, updates)
            decision = gate_decision(rss_kb, cost_kb=cost_kb, limit_kb=limit_kb)
            if decision.should_pause:
                self._logger.info(
                    f"Session-budget pause at chunk boundary: renderer RSS {rss_kb} KB + "
                    f"{creates} creates + {updates} updates projects {decision.projected_kb} KB >= limit "
                    f"{decision.threshold_kb} KB"
                )
                box.run_paused = True
                box.interrupt_reason = SYNC_PAUSED_BUDGET
                box.request_cancel()
            return rss_kb
        except Exception as e:  # fail-open: the gate must never fail the run
            self._logger.debug(f"Session-budget gate skipped: {e}")
            return None

    async def predict_pause_likely(self, *, new_items: int, changed_items: int) -> bool:
        """Whether a run of *new_items* creates + *changed_items* updates would cross the ceiling.

        The post-preview prognosis: measure the renderer's current RSS (no GC — the
        preview is a fast read-only path) and predict whether the run's real work
        would cross the ceiling. Only NEW creates (worst-case rate) and CHANGED
        updates (lighter Set*-walk rate) grow the renderer heap; fully-unchanged
        items skip the per-item touch and are not projected, so a large unchanged
        re-sync never warns. Fail-open: an unavailable reading — or any seam error —
        yields no warning.
        """
        try:
            rss_kb = await self._loop.run_in_executor(None, self._renderer_rss)
            return rss_kb is not None and predict_run_crosses(
                rss_kb, new_items, changed_items, create_kb=_CREATE_WITH_COVER_KB
            )
        except Exception as e:  # fail-open: an advisory must never fail the preview
            self._logger.debug(f"Session-budget prognosis skipped: {e}")
            return False

    # ── Reporting ────────────────────────────────────────────────

    async def get_session_budget_status(self) -> dict[str, Any]:
        """Live renderer-heap reading for the QAM banners (#1383).

        Reads the renderer's current RSS (no GC — this is a cheap on-render poll,
        not the gate's settled measurement) alongside the three fixed budget lines so
        the frontend can render "Steam memory: X.X GB" and colour it against the
        thresholds: ``warn_kb`` (the advisory floor, ≈1.8 GB — where the yellow
        high-heap banner also appears), ``ceiling_kb`` (the effective pause ceiling,
        ≈2.2 GB), and ``cliff_kb`` (the OOM crash line, ≈2.45 GB). The frontend owns
        no thresholds of its own — all three ride the payload so there is a single
        source of truth. Also returns ``memory_delta_kb`` — the last clean run's
        signed RSS growth, retained in memory — so a QAM remount can show
        "last run: ±X GB" without a live sync — and ``resume_ready`` — whether the
        live reading is low enough that resuming a paused run would apply at least one
        full chunk without re-pausing (the gate's own predictive condition), so the
        paused banner can flip to "memory is free, press Resume Sync" once a Steam
        restart drops RSS. Fail-open: ``rss_kb`` is ``None`` when the reading is
        unavailable (no ``steamwebhelper`` / unreadable ``/proc``) or any seam raises
        — the banner then drops the number but keeps its guidance text;
        ``memory_delta_kb`` is ``None`` until a clean run has measured both endpoints,
        and ``resume_ready`` is ``None`` when RSS is unreadable (undecidable).

        Also carries the last run's progress — ``run_done_items`` of
        ``run_total_items`` — so the paused banner can say "X of Y games done". They
        ride this payload rather than a new callable because the QAM already polls it
        while a paused banner shows, and they live in the BACKEND because the plugin
        process survives the Steam restart the banner asks for. Both are ``None`` when
        no run has reached its plan in this process (a plugin reload wipes the
        in-memory counters); the banner then omits the sentence.
        """
        rss_kb: int | None = None
        try:
            rss_kb = await self._loop.run_in_executor(None, self._renderer_rss)
        except Exception as e:  # fail-open: a status poll must never raise
            self._logger.debug(f"Session-budget status read failed: {e}")
        run_total = self._sync_state.run_total_items
        return {
            "success": True,
            "rss_kb": rss_kb,
            "warn_kb": POST_RUN_ADVISORY_KB,
            "ceiling_kb": EFFECTIVE_CEILING_KB,
            "cliff_kb": CLIFF_KB,
            "memory_delta_kb": self._sync_state.last_run_delta_kb,
            "resume_ready": resume_would_proceed(rss_kb) if rss_kb is not None else None,
            # A done count without its denominator is unreadable, so the pair is
            # surfaced together: no known total ⇒ both None.
            "run_done_items": self._sync_state.run_done_items if run_total is not None else None,
            "run_total_items": run_total,
        }
