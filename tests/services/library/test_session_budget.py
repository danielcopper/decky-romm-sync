"""Tests for SessionBudgetMonitor — Steam's per-session renderer-heap budget.

The monitor is reached through the library façade (``plugin._sync_service.
_session_budget``) so every test drives the same instance the sync orchestrator
holds, over the shared state box the pause verdict lands on. The renderer seams
are the in-memory ``FakeRendererRss`` / ``FakeRendererGc`` the shared fixture
wires in — ``plugin._renderer_rss.rss_kb`` sets the reading a test needs and
``.calls`` records how often the reading was taken, which is how the GC-skip
floor is pinned.

The gate's arithmetic lives in ``domain.session_budget`` and is covered by
``tests/domain/test_session_budget.py``; what these tests own is the
orchestration around it — when a GC is worth its round-trip, what a pause writes
to the state box, and the fail-open degradation when the renderer cannot be read.
"""

import asyncio

import pytest

from domain.sync_state import SyncState

# conftest.py patches decky before this import


class TestSessionBudgetMonitor:
    """Measurement, the chunk-boundary pause verdict, and the QAM status payload (#1383)."""

    # ── maybe_pause_for_budget (the gate primitive) ──────────────

    @pytest.mark.asyncio
    async def test_pauses_and_marks_paused_when_over_budget(self, plugin):
        from services.library.session_budget import SYNC_PAUSED_BUDGET

        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 2_100_000  # + 200*2500 = 2.6M ≥ ceiling 2.2M

        await budget.maybe_pause_for_budget(creates=200, updates=0)

        # 2.1M is above the GC-skip floor, so the reading is GC-settled: one GC and
        # two RSS reads (raw sample + post-GC re-read).
        assert plugin._renderer_gc.calls == 1
        assert plugin._renderer_rss.calls == 2
        # A budget stop flags run_paused (→ 'paused'), NOT run_interrupted.
        assert box.run_paused is True
        assert box.run_interrupted is False
        assert box.interrupt_reason == SYNC_PAUSED_BUDGET
        assert box.is_cancelling() is True

    @pytest.mark.asyncio
    async def test_proceeds_with_ample_headroom(self, plugin):
        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 440_000  # fresh baseline — nowhere near the cliff

        await budget.maybe_pause_for_budget(creates=200, updates=0)

        # 440K is below the GC-skip floor: even the worst-case chunk cost can't cross
        # the ceiling, so the raw reading is trusted and the ~5 s GC is skipped.
        assert plugin._renderer_gc.calls == 0
        assert plugin._renderer_rss.calls == 1  # a single raw sample, no re-read
        assert box.run_interrupted is False
        assert box.interrupt_reason is None
        assert box.is_cancelling() is False

    @pytest.mark.asyncio
    async def test_fail_open_when_rss_unavailable(self, plugin):
        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_rss.rss_kb = None  # measurement unavailable

        await budget.maybe_pause_for_budget(creates=200, updates=0)

        # Fail-open: no pause, and the "RSS unavailable" note is armed once-per-run.
        assert box.run_interrupted is False
        assert box.is_cancelling() is False
        assert box.budget_measure_unavailable_logged is True

    @pytest.mark.asyncio
    async def test_cliff_limit_proceeds_just_below_the_cliff_bound(self, plugin):
        from domain.session_budget import CLIFF_KB, COVER_TRANSIENT_KB, WORST_CASE_CREATE_KB

        # The first-chunk call passes limit_kb=CLIFF. One KB below the full-chunk
        # cliff bound the gate lets the chunk through (spends into the margin, never
        # past the crash line). Each item is priced create + cover (2500).
        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = CLIFF_KB - 200 * (WORST_CASE_CREATE_KB + COVER_TRANSIENT_KB) - 1  # 1_949_999

        await budget.maybe_pause_for_budget(creates=200, updates=0, limit_kb=CLIFF_KB)

        assert box.run_paused is False
        assert box.is_cancelling() is False

    @pytest.mark.asyncio
    async def test_cliff_limit_pauses_when_full_chunk_would_reach_the_cliff(self, plugin):
        from domain.session_budget import CLIFF_KB, COVER_TRANSIENT_KB, WORST_CASE_CREATE_KB
        from services.library.session_budget import SYNC_PAUSED_BUDGET

        # At the full-chunk cliff bound (each item priced create + cover = 2500) the
        # projection reaches the cliff exactly and the gate pauses (>=) — a first
        # chunk this high is stopped before the crash line even though it would clear
        # the more-permissive absolute-ceiling check the old first-chunk mode used.
        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = CLIFF_KB - 200 * (WORST_CASE_CREATE_KB + COVER_TRANSIENT_KB)  # 1_950_000

        await budget.maybe_pause_for_budget(creates=200, updates=0, limit_kb=CLIFF_KB)

        assert box.run_paused is True
        assert box.interrupt_reason == SYNC_PAUSED_BUDGET
        assert box.is_cancelling() is True

    @pytest.mark.asyncio
    async def test_composition_pricing_mixed_chunk_cheaper_than_all_creates(self, plugin):
        # Composition pricing (#1383): a chunk of 100 creates + 100 updates costs
        # 100*2500 + 100*1000 = 350_000 KB and proceeds at this RSS, while the SAME
        # 200-item count priced as all creates (500_000 KB) would pause — proof the
        # gate prices updates lighter than creates, not every item as a create.
        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-mix"
        plugin._renderer_gc.result = True
        # 1_800_000 + 350_000 = 2_150_000 < 2_200_000 ceiling; + 500_000 = 2_300_000 ≥ it.
        plugin._renderer_rss.rss_kb = 1_800_000

        await budget.maybe_pause_for_budget(creates=100, updates=100)
        assert box.run_paused is False
        assert box.is_cancelling() is False

        await budget.maybe_pause_for_budget(creates=200, updates=0)
        assert box.run_paused is True
        assert box.is_cancelling() is True

    @pytest.mark.asyncio
    async def test_pause_log_line_states_creates_and_updates_composition(self, plugin, caplog):
        # The pause log line must name the composition so an operator can read why
        # the gate fired without recomputing the arithmetic.
        import logging

        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-log"
        plugin._renderer_gc.result = True
        # 1_900_000 + (120*2500 + 80*1000 = 380_000) = 2_280_000 ≥ 2_200_000 → pause.
        plugin._renderer_rss.rss_kb = 1_900_000

        with caplog.at_level(logging.INFO):
            await budget.maybe_pause_for_budget(creates=120, updates=80)

        assert box.run_paused is True
        assert "120 creates + 80 updates" in caplog.text

    # ── measure_rss (GC-skip below the floor, LOW-3) ─────────────

    @pytest.mark.asyncio
    async def test_gc_skipped_below_floor_returns_raw_reading(self, plugin):
        budget = plugin._sync_service._session_budget
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 1_400_000  # below the 1.5M GC-skip floor

        result = await budget.measure_rss()

        assert result == 1_400_000  # the raw reading is returned as-is
        assert plugin._renderer_gc.calls == 0  # GC skipped — buys nothing this low
        assert plugin._renderer_rss.calls == 1  # a single raw read, no re-read

    @pytest.mark.asyncio
    async def test_gc_fires_and_rereads_at_or_above_floor(self, plugin):
        budget = plugin._sync_service._session_budget
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 1_600_000  # at/above the floor → settle first

        result = await budget.measure_rss()

        assert result == 1_600_000
        assert plugin._renderer_gc.calls == 1  # GC fired to settle the reading
        assert plugin._renderer_rss.calls == 2  # raw sample + post-GC re-read

    # ── record_run_start_baseline (the per-run re-arm) ───────────

    @pytest.mark.asyncio
    async def test_run_start_baseline_rearms_the_measurement_latch(self, plugin):
        # measure_rss latches "unreadable" for the rest of a run and short-circuits
        # every later read, so a run that starts without clearing the latch inherits
        # the previous run's verdict and the gate stays silently off from then on.
        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        box.budget_measure_unavailable_logged = True  # a prior run found RSS unreadable
        plugin._renderer_rss.rss_kb = 1_400_000

        await budget.record_run_start_baseline()

        assert box.budget_measure_unavailable_logged is False
        assert box.run_start_rss_kb == 1_400_000
        # The latch really is clear: the gate reads again on this run.
        assert await budget.measure_rss() == 1_400_000

    @pytest.mark.asyncio
    async def test_run_start_baseline_is_none_when_rss_unavailable(self, plugin):
        # Fail-open: no reading ⇒ no baseline (the delta is unmeasurable), never an
        # error out of the run's opening move.
        budget = plugin._sync_service._session_budget
        box = plugin._sync_service._box
        plugin._renderer_rss.rss_kb = None

        await budget.record_run_start_baseline()

        assert box.run_start_rss_kb is None

    # ── get_session_budget_status callable ───────────────────────

    @pytest.mark.asyncio
    async def test_session_budget_status_happy(self, plugin):
        from domain.session_budget import CLIFF_KB, EFFECTIVE_CEILING_KB, POST_RUN_ADVISORY_KB

        plugin.loop = asyncio.get_event_loop()
        plugin._renderer_rss.rss_kb = 1_100_000

        result = await plugin.get_session_budget_status()

        assert result == {
            "success": True,
            "rss_kb": 1_100_000,
            # All three colour thresholds ride the payload (single source of truth).
            "warn_kb": POST_RUN_ADVISORY_KB,
            "ceiling_kb": EFFECTIVE_CEILING_KB,
            "cliff_kb": CLIFF_KB,
            # No clean run has completed in this test, so the retained delta is None.
            "memory_delta_kb": None,
            # 1.1 + 2*0.5 = 2.1 < 2.2 ceiling → below the two-chunk headroom bar,
            # a paused run could resume now.
            "resume_ready": True,
            # No run has reached its plan in this process → the progress pair is
            # unknown, and a done count without its denominator is never surfaced.
            "run_done_items": None,
            "run_total_items": None,
        }

    @pytest.mark.asyncio
    async def test_session_budget_status_rss_none(self, plugin):
        from domain.session_budget import CLIFF_KB, EFFECTIVE_CEILING_KB, POST_RUN_ADVISORY_KB

        plugin.loop = asyncio.get_event_loop()
        plugin._renderer_rss.rss_kb = None  # measurement unavailable → fail-open

        result = await plugin.get_session_budget_status()

        assert result["success"] is True
        assert result["rss_kb"] is None
        assert result["warn_kb"] == POST_RUN_ADVISORY_KB
        assert result["ceiling_kb"] == EFFECTIVE_CEILING_KB
        assert result["cliff_kb"] == CLIFF_KB
        assert result["memory_delta_kb"] is None
        assert result["resume_ready"] is None  # RSS unreadable → undecidable

    @pytest.mark.asyncio
    async def test_session_budget_status_resume_not_ready_at_high_rss(self, plugin):
        # A still-high RSS (a paused run before a Steam restart): resume would re-pause.
        plugin.loop = asyncio.get_event_loop()
        plugin._renderer_rss.rss_kb = 2_100_000  # 2.1 + 0.5 = 2.6 ≥ 2.2 ceiling

        result = await plugin.get_session_budget_status()

        assert result["resume_ready"] is False

    @pytest.mark.asyncio
    async def test_session_budget_status_returns_retained_delta(self, plugin):
        # A prior clean run's delta is retained in the box and surfaced on a QAM
        # remount even though the live RSS read is a separate poll.
        plugin.loop = asyncio.get_event_loop()
        plugin._renderer_rss.rss_kb = 1_234_000
        plugin._sync_service._box.last_run_delta_kb = 800_000

        result = await plugin.get_session_budget_status()

        assert result["rss_kb"] == 1_234_000
        assert result["memory_delta_kb"] == 800_000


class TestClipCoverRefreshes:
    """Trimming a chunk's additive cover work to the headroom the gate left (#1386)."""

    def test_clip_fails_open_when_rss_unavailable(self, plugin):
        budget = plugin._sync_service._session_budget
        refreshes = [{"rom_id": 1, "app_id": 10}, {"rom_id": 2, "app_id": 20}]
        assert (
            budget.clip_cover_refreshes(refreshes, rss_kb=None, creates=0, updates=0, limit_kb=1_000_000) == refreshes
        )

    def test_clip_keeps_all_with_headroom(self, plugin):
        from domain.session_budget import COVER_TRANSIENT_KB, EFFECTIVE_CEILING_KB

        budget = plugin._sync_service._session_budget
        refreshes = [{"rom_id": 1, "app_id": 10}, {"rom_id": 2, "app_id": 20}]
        rss = EFFECTIVE_CEILING_KB - 10 * COVER_TRANSIENT_KB
        assert (
            budget.clip_cover_refreshes(refreshes, rss_kb=rss, creates=0, updates=0, limit_kb=EFFECTIVE_CEILING_KB)
            == refreshes
        )

    def test_clip_accounts_for_the_chunks_own_cost(self, plugin):
        from domain.session_budget import EFFECTIVE_CEILING_KB, chunk_worst_cost_kb

        budget = plugin._sync_service._session_budget
        refreshes = [{"rom_id": i, "app_id": i * 10} for i in range(1, 6)]
        # Headroom of exactly the chunk's own cost → zero left for refreshes.
        rss = EFFECTIVE_CEILING_KB - chunk_worst_cost_kb(3, 2)
        assert (
            budget.clip_cover_refreshes(refreshes, rss_kb=rss, creates=3, updates=2, limit_kb=EFFECTIVE_CEILING_KB)
            == []
        )

    def test_clip_negative_headroom_yields_empty(self, plugin):
        from domain.session_budget import EFFECTIVE_CEILING_KB

        budget = plugin._sync_service._session_budget
        refreshes = [{"rom_id": 1, "app_id": 10}]
        assert (
            budget.clip_cover_refreshes(
                refreshes, rss_kb=EFFECTIVE_CEILING_KB + 1, creates=0, updates=0, limit_kb=EFFECTIVE_CEILING_KB
            )
            == []
        )

    def test_clip_keeps_list_order(self, plugin):
        from domain.session_budget import COVER_TRANSIENT_KB, EFFECTIVE_CEILING_KB

        budget = plugin._sync_service._session_budget
        refreshes = [{"rom_id": i, "app_id": i * 10} for i in range(1, 6)]
        rss = EFFECTIVE_CEILING_KB - 2 * COVER_TRANSIENT_KB
        assert budget.clip_cover_refreshes(
            refreshes, rss_kb=rss, creates=0, updates=0, limit_kb=EFFECTIVE_CEILING_KB
        ) == [{"rom_id": 1, "app_id": 10}, {"rom_id": 2, "app_id": 20}]
