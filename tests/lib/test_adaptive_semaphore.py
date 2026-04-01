"""Tests for AdaptiveSemaphore — bandwidth-adaptive concurrency control."""

from __future__ import annotations

import asyncio

import pytest

from lib.perf import AdaptiveSemaphore


# ── Helpers ──────────────────────────────────────────────────


async def _run_tasks(sem: AdaptiveSemaphore, latencies: list[float]) -> None:
    """Simulate tasks that record the given latencies (in seconds)."""
    for lat in latencies:
        async with sem:
            sem.record_latency(lat)


# ── Basic behaviour ──────────────────────────────────────────


class TestBasicBehaviour:
    """Core contract: context-manager gating, properties, defaults."""

    @pytest.mark.asyncio
    async def test_initial_limit(self):
        sem = AdaptiveSemaphore(initial=5, min_concurrent=1, max_concurrent=10)
        assert sem.limit == 5

    @pytest.mark.asyncio
    async def test_active_starts_at_zero(self):
        sem = AdaptiveSemaphore()
        assert sem.active == 0

    @pytest.mark.asyncio
    async def test_active_increments_and_decrements(self):
        sem = AdaptiveSemaphore(initial=4)
        async with sem:
            assert sem.active == 1
        assert sem.active == 0

    @pytest.mark.asyncio
    async def test_context_manager_returns_self(self):
        sem = AdaptiveSemaphore()
        async with sem as s:
            assert s is sem

    @pytest.mark.asyncio
    async def test_initial_clamped_to_min(self):
        sem = AdaptiveSemaphore(initial=0, min_concurrent=2, max_concurrent=8)
        assert sem.limit == 2

    @pytest.mark.asyncio
    async def test_initial_clamped_to_max(self):
        sem = AdaptiveSemaphore(initial=20, min_concurrent=2, max_concurrent=8)
        assert sem.limit == 8

    @pytest.mark.asyncio
    async def test_adjustments_empty_initially(self):
        sem = AdaptiveSemaphore()
        assert sem.adjustments == []

    @pytest.mark.asyncio
    async def test_concurrent_gating(self):
        """Verify that no more than `limit` tasks run concurrently."""
        sem = AdaptiveSemaphore(initial=2, min_concurrent=2, max_concurrent=2)
        peak = 0
        results = []

        async def _task():
            nonlocal peak
            async with sem:
                current = sem.active
                if current > peak:
                    peak = current
                # Yield to let other tasks try to enter
                await asyncio.sleep(0)
                results.append(current)

        await asyncio.gather(*[_task() for _ in range(6)])
        assert peak <= 2
        assert len(results) == 6


# ── Scaling up ───────────────────────────────────────────────


class TestScaleUp:
    """When latency is low, concurrency should increase."""

    @pytest.mark.asyncio
    async def test_low_latency_increases_limit(self):
        sem = AdaptiveSemaphore(
            initial=3,
            min_concurrent=1,
            max_concurrent=8,
            low_latency_ms=500.0,
            high_latency_ms=2000.0,
            window=4,
            adjust_every=4,
        )
        # Record 4 very fast latencies (50ms each, well under 500ms threshold)
        await _run_tasks(sem, [0.05] * 4)
        assert sem.limit == 4  # increased by 1

    @pytest.mark.asyncio
    async def test_multiple_scale_ups(self):
        sem = AdaptiveSemaphore(
            initial=2,
            min_concurrent=1,
            max_concurrent=6,
            low_latency_ms=500.0,
            high_latency_ms=2000.0,
            window=3,
            adjust_every=3,
        )
        # 6 fast tasks → 2 adjustments
        await _run_tasks(sem, [0.05] * 6)
        assert sem.limit == 4  # 2 → 3 → 4

    @pytest.mark.asyncio
    async def test_does_not_exceed_max(self):
        sem = AdaptiveSemaphore(
            initial=4,
            min_concurrent=1,
            max_concurrent=5,
            low_latency_ms=500.0,
            high_latency_ms=2000.0,
            window=3,
            adjust_every=3,
        )
        # 9 fast tasks → would want 3 increases, but max is 5
        await _run_tasks(sem, [0.05] * 9)
        assert sem.limit == 5

    @pytest.mark.asyncio
    async def test_scale_up_records_adjustment(self):
        sem = AdaptiveSemaphore(
            initial=3,
            min_concurrent=1,
            max_concurrent=8,
            low_latency_ms=500.0,
            high_latency_ms=2000.0,
            window=4,
            adjust_every=4,
        )
        await _run_tasks(sem, [0.05] * 4)
        adjustments = sem.adjustments
        assert len(adjustments) == 1
        _ts, old, new, avg_ms = adjustments[0]
        assert old == 3
        assert new == 4
        assert avg_ms < 500.0  # should be ~50ms


# ── Scaling down ─────────────────────────────────────────────


class TestScaleDown:
    """When latency is high, concurrency should decrease."""

    @pytest.mark.asyncio
    async def test_high_latency_decreases_limit(self):
        sem = AdaptiveSemaphore(
            initial=5,
            min_concurrent=2,
            max_concurrent=8,
            low_latency_ms=200.0,
            high_latency_ms=1000.0,
            window=4,
            adjust_every=4,
        )
        # Record 4 slow latencies (1.5s each, above 1000ms threshold)
        await _run_tasks(sem, [1.5] * 4)
        assert sem.limit == 4  # decreased by 1

    @pytest.mark.asyncio
    async def test_multiple_scale_downs(self):
        sem = AdaptiveSemaphore(
            initial=5,
            min_concurrent=2,
            max_concurrent=8,
            low_latency_ms=200.0,
            high_latency_ms=1000.0,
            window=3,
            adjust_every=3,
        )
        # 6 slow tasks → 2 decreases
        await _run_tasks(sem, [1.5] * 6)
        assert sem.limit == 3  # 5 → 4 → 3

    @pytest.mark.asyncio
    async def test_does_not_go_below_min(self):
        sem = AdaptiveSemaphore(
            initial=3,
            min_concurrent=2,
            max_concurrent=8,
            low_latency_ms=200.0,
            high_latency_ms=1000.0,
            window=3,
            adjust_every=3,
        )
        # 9 slow tasks → would want 3 decreases, but min is 2
        await _run_tasks(sem, [1.5] * 9)
        assert sem.limit == 2


# ── No adjustment in dead zone ───────────────────────────────


class TestDeadZone:
    """Latency between thresholds should not trigger adjustments."""

    @pytest.mark.asyncio
    async def test_medium_latency_no_change(self):
        sem = AdaptiveSemaphore(
            initial=4,
            min_concurrent=2,
            max_concurrent=8,
            low_latency_ms=200.0,
            high_latency_ms=1000.0,
            window=4,
            adjust_every=4,
        )
        # Record latencies right in the middle (500ms)
        await _run_tasks(sem, [0.5] * 8)
        assert sem.limit == 4
        assert sem.adjustments == []

    @pytest.mark.asyncio
    async def test_insufficient_samples_no_change(self):
        sem = AdaptiveSemaphore(
            initial=4,
            min_concurrent=2,
            max_concurrent=8,
            window=10,
            adjust_every=5,
        )
        # Only 5 samples but window is 10 — too few for adjustment
        await _run_tasks(sem, [0.01] * 5)
        assert sem.limit == 4
        assert sem.adjustments == []


# ── Mixed workloads ──────────────────────────────────────────


class TestMixedWorkloads:
    """Scale up then down as latency changes."""

    @pytest.mark.asyncio
    async def test_up_then_down(self):
        sem = AdaptiveSemaphore(
            initial=4,
            min_concurrent=2,
            max_concurrent=8,
            low_latency_ms=200.0,
            high_latency_ms=1000.0,
            window=4,
            adjust_every=4,
        )
        # Fast phase: scale up
        await _run_tasks(sem, [0.05] * 4)
        assert sem.limit == 5

        # Slow phase: scale down
        await _run_tasks(sem, [1.5] * 4)
        assert sem.limit == 4

    @pytest.mark.asyncio
    async def test_adjustments_history_tracks_both_directions(self):
        sem = AdaptiveSemaphore(
            initial=4,
            min_concurrent=2,
            max_concurrent=8,
            low_latency_ms=200.0,
            high_latency_ms=1000.0,
            window=4,
            adjust_every=4,
        )
        await _run_tasks(sem, [0.05] * 4)  # up
        await _run_tasks(sem, [1.5] * 4)  # down
        assert len(sem.adjustments) == 2
        assert sem.adjustments[0][2] == 5  # went up to 5
        assert sem.adjustments[1][2] == 4  # came back to 4


# ── Edge cases ───────────────────────────────────────────────


class TestEdgeCases:
    """Boundary conditions and corner cases."""

    @pytest.mark.asyncio
    async def test_adjustments_returns_copy(self):
        sem = AdaptiveSemaphore()
        adj = sem.adjustments
        adj.append((0, 0, 0, 0.0))
        assert sem.adjustments == []  # original unchanged

    @pytest.mark.asyncio
    async def test_zero_latency_records_fine(self):
        sem = AdaptiveSemaphore(
            initial=3,
            min_concurrent=1,
            max_concurrent=8,
            low_latency_ms=100.0,
            high_latency_ms=500.0,
            window=3,
            adjust_every=3,
        )
        await _run_tasks(sem, [0.0] * 3)
        assert sem.limit == 4  # 0ms is below 100ms threshold

    @pytest.mark.asyncio
    async def test_acquire_release_manual(self):
        """Test the explicit acquire/release API (not context manager)."""
        sem = AdaptiveSemaphore(initial=2, min_concurrent=1, max_concurrent=4)
        await sem.acquire()
        assert sem.active == 1
        await sem.acquire()
        assert sem.active == 2
        await sem.release()
        assert sem.active == 1
        await sem.release()
        assert sem.active == 0
