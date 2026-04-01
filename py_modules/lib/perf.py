"""Performance instrumentation for sync operations.

Provides timing contexts, throughput tracking, adaptive concurrency control,
and summary report generation.  All data is kept in memory during a sync and
optionally returned via callable for frontend display.

Part of the ``lib`` layer — no dependency on services or adapters.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class HttpSample:
    """A single HTTP request measurement."""

    method: str
    path: str
    elapsed: float
    status: int
    bytes_transferred: int = 0


class PerfCollector:
    """Collects performance measurements across a sync operation.

    Thread-safe for the common case (single-writer from the sync coroutine).
    Read-only accessors (``generate_report``, ``format_report``) can be called
    from any thread after the sync completes.
    """

    def __init__(self) -> None:
        self._timers: dict[str, list[float]] = defaultdict(list)
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._http_samples: list[HttpSample] = []
        self._phase_timings: dict[str, float] = {}
        self._sync_start: float = 0.0
        self._sync_end: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────

    def start_sync(self) -> None:
        """Mark the beginning of a sync operation — clears all prior data."""
        self._timers.clear()
        self._counters.clear()
        self._gauges.clear()
        self._http_samples.clear()
        self._phase_timings.clear()
        self._sync_start = time.monotonic()
        self._sync_end = 0.0

    def end_sync(self) -> None:
        """Mark the end of a sync operation."""
        self._sync_end = time.monotonic()

    @property
    def wall_time(self) -> float:
        """Total wall-clock seconds for the most recent sync."""
        end = self._sync_end or time.monotonic()
        return end - self._sync_start if self._sync_start else 0.0

    # ── Timing ───────────────────────────────────────────────

    @contextmanager
    def time_phase(self, name: str):
        """Context manager that records the wall-clock duration of a named phase.

        Usage::

            with perf.time_phase("fetch_roms"):
                await fetch_all_roms()
        """
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - start
            self._phase_timings[name] = elapsed

    @contextmanager
    def time_operation(self, name: str):
        """Context manager that appends one timing sample to a named bucket.

        Use for repeated operations (e.g., individual HTTP requests or downloads)
        where you want min/max/avg/p95 statistics.
        """
        start = time.monotonic()
        try:
            yield
        finally:
            self._timers[name].append(time.monotonic() - start)

    # ── HTTP tracking ────────────────────────────────────────

    def record_http_request(
        self,
        method: str,
        path: str,
        elapsed: float,
        status: int,
        bytes_transferred: int = 0,
    ) -> None:
        """Record an HTTP request's performance characteristics."""
        self._http_samples.append(
            HttpSample(
                method=method,
                path=path,
                elapsed=elapsed,
                status=status,
                bytes_transferred=bytes_transferred,
            )
        )

    # ── Counters / gauges ────────────────────────────────────

    def increment(self, name: str, amount: int = 1) -> None:
        """Increment a named counter (e.g., 'roms_fetched', 'artwork_downloaded')."""
        self._counters[name] += amount

    def set_gauge(self, name: str, value: float) -> None:
        """Set a point-in-time gauge (e.g., 'bandwidth_bps')."""
        self._gauges[name] = value

    # ── Report generation ────────────────────────────────────

    def generate_report(self) -> dict:
        """Generate a structured summary report of the sync operation.

        Returns a JSON-serialisable dict suitable for the frontend callable.
        """
        # HTTP statistics
        http_latencies = [s.elapsed for s in self._http_samples]
        http_bytes = sum(s.bytes_transferred for s in self._http_samples)
        http_errors = sum(1 for s in self._http_samples if s.status >= 400)
        retries = self._counters.get("http_retries", 0)

        http_stats: dict = {
            "total_requests": len(self._http_samples),
            "total_bytes": http_bytes,
            "errors": http_errors,
            "retries": retries,
        }
        if http_latencies:
            sorted_lat = sorted(http_latencies)
            http_stats.update(
                {
                    "avg_latency_ms": round(statistics.mean(sorted_lat) * 1000, 1),
                    "p50_latency_ms": round(_percentile(sorted_lat, 50) * 1000, 1),
                    "p95_latency_ms": round(_percentile(sorted_lat, 95) * 1000, 1),
                    "max_latency_ms": round(sorted_lat[-1] * 1000, 1),
                }
            )

        # Phase breakdown
        phases = {}
        for name, elapsed in self._phase_timings.items():
            phases[name] = {"elapsed_sec": round(elapsed, 2)}

        # Operation statistics (repeated timings)
        operations: dict = {}
        for name, samples in self._timers.items():
            sorted_s = sorted(samples)
            operations[name] = {
                "count": len(sorted_s),
                "total_sec": round(sum(sorted_s), 2),
                "avg_ms": round(statistics.mean(sorted_s) * 1000, 1) if sorted_s else 0,
                "p95_ms": round(_percentile(sorted_s, 95) * 1000, 1) if sorted_s else 0,
            }

        return {
            "wall_time_sec": round(self.wall_time, 2),
            "phases": phases,
            "http": http_stats,
            "operations": operations,
            "counters": dict(self._counters),
            "gauges": {k: round(v, 2) for k, v in self._gauges.items()},
        }

    def format_report(self) -> str:
        """Generate a human-readable performance report string."""
        data = self.generate_report()
        wt = data["wall_time_sec"]
        lines = [
            "═" * 55,
            "  SYNC PERFORMANCE REPORT",
            f"  Total wall time: {_fmt_duration(wt)}",
            "═" * 55,
            "",
        ]

        # Phase breakdown
        phases = data.get("phases", {})
        if phases:
            lines.append("  Phase Breakdown:")
            for name, info in phases.items():
                elapsed = info["elapsed_sec"]
                pct = (elapsed / wt * 100) if wt else 0
                lines.append(f"  ├── {_humanise(name):.<30s} {_fmt_duration(elapsed):>8s}  ({pct:4.1f}%)")
            lines.append("")

        # HTTP summary
        http = data.get("http", {})
        if http.get("total_requests"):
            lines.append("  HTTP Summary:")
            lines.append(f"  ├── Total requests: {http['total_requests']:,}")
            lines.append(f"  ├── Total bytes received: {_fmt_bytes(http.get('total_bytes', 0))}")
            if "avg_latency_ms" in http:
                lines.append(
                    f"  ├── Avg latency: {http['avg_latency_ms']:.0f}ms "
                    f"(P95: {http.get('p95_latency_ms', 0):.0f}ms)"
                )
            lines.append(f"  ├── Retries: {http.get('retries', 0)}")
            lines.append(f"  └── Errors: {http.get('errors', 0)}")
            lines.append("")

        # Counters
        counters = data.get("counters", {})
        if counters:
            lines.append("  Counters:")
            for k, v in sorted(counters.items()):
                lines.append(f"  ├── {_humanise(k)}: {v:,}")
            lines.append("")

        # Bottleneck identification
        if phases:
            slowest = max(phases.items(), key=lambda x: x[1]["elapsed_sec"])
            pct = slowest[1]["elapsed_sec"] / wt * 100 if wt else 0
            lines.append(f"  Bottleneck: {_humanise(slowest[0])} ({pct:.0f}% of wall time)")

        # Adaptive concurrency
        gauges = data.get("gauges", {})
        if "fetch_final_concurrency" in gauges:
            lines.append(
                f"  Fetch concurrency: adapted to {int(gauges['fetch_final_concurrency'])}"
            )

        lines.append("═" * 55)
        return "\n".join(lines)


class ETAEstimator:
    """Estimates time remaining using exponential moving average (EMA).

    Alpha controls responsiveness:
    - Higher alpha (0.5+) → reacts quickly to speed changes
    - Lower alpha (0.1–0.2) → smoother but slower to adapt
    Default 0.3 balances responsiveness with stability.
    """

    def __init__(self, alpha: float = 0.3, min_samples: int = 5) -> None:
        self._alpha = alpha
        self._min_samples = min_samples
        self._avg_time: float = 0.0
        self._samples: int = 0
        self._start_time: float = 0.0
        self._last_update: float = 0.0
        self._last_count: int = 0

    def start(self) -> None:
        """Reset and begin timing."""
        self._start_time = time.monotonic()
        self._last_update = self._start_time
        self._last_count = 0
        self._samples = 0
        self._avg_time = 0.0

    def update(self, items_completed: int) -> None:
        """Update with the current cumulative completed item count."""
        now = time.monotonic()
        delta_items = items_completed - self._last_count
        if delta_items <= 0:
            return

        delta_time = now - self._last_update
        per_item = delta_time / delta_items

        if self._avg_time == 0:
            self._avg_time = per_item
        else:
            self._avg_time = self._alpha * per_item + (1 - self._alpha) * self._avg_time

        self._samples += delta_items
        self._last_count = items_completed
        self._last_update = now

    def eta_seconds(self, current: int, total: int) -> float | None:
        """Estimated seconds remaining, or None if insufficient data.

        Returns None until at least ``min_samples`` items have been processed.
        """
        if self._samples < self._min_samples or total <= current or self._avg_time == 0:
            return None
        return self._avg_time * (total - current)

    @property
    def elapsed(self) -> float:
        """Seconds since ``start()`` was called."""
        return time.monotonic() - self._start_time if self._start_time else 0.0

    @property
    def items_per_sec(self) -> float:
        """Average items per second since ``start()``, or 0.0."""
        elapsed = self.elapsed
        if elapsed == 0 or self._last_count == 0:
            return 0.0
        return self._last_count / elapsed


class AdaptiveSemaphore:
    """Async concurrency limiter that adjusts capacity based on task latency.

    When average task duration over a recent sliding window drops below
    *low_latency_ms*, capacity increases by 1 (up to *max_concurrent*).
    When it rises above *high_latency_ms*, capacity decreases by 1 (down
    to *min_concurrent*).  Adjustments are evaluated every *adjust_every*
    task completions.

    Usage::

        sem = AdaptiveSemaphore(initial=4, min_concurrent=2, max_concurrent=8)
        async with sem:
            t0 = time.monotonic()
            result = await do_work()
            sem.record_latency(time.monotonic() - t0)

    The semaphore is also usable as an async context manager (``async with sem:``)
    which only gates entry — callers must still call ``record_latency`` explicitly
    after each task to feed the adaptation algorithm.
    """

    def __init__(
        self,
        initial: int = 4,
        min_concurrent: int = 2,
        max_concurrent: int = 8,
        *,
        low_latency_ms: float = 200.0,
        high_latency_ms: float = 1000.0,
        window: int = 8,
        adjust_every: int = 5,
    ) -> None:
        self._limit = max(min_concurrent, min(initial, max_concurrent))
        self._min = min_concurrent
        self._max = max_concurrent
        self._low_ms = low_latency_ms
        self._high_ms = high_latency_ms
        self._window = window
        self._adjust_every = adjust_every

        self._active = 0
        self._cond: asyncio.Condition = asyncio.Condition()
        self._samples: list[float] = []
        self._since_adjust = 0
        self._adjustments: list[tuple[float, int, int, float]] = []  # (time, old, new, avg_ms)

    # ── Properties ───────────────────────────────────────────

    @property
    def limit(self) -> int:
        """Current concurrency limit."""
        return self._limit

    @property
    def active(self) -> int:
        """Number of tasks currently inside the semaphore."""
        return self._active

    @property
    def adjustments(self) -> list[tuple[float, int, int, float]]:
        """History of (timestamp, old_limit, new_limit, avg_ms) adjustments."""
        return list(self._adjustments)

    # ── Async context manager ────────────────────────────────

    async def acquire(self) -> None:
        """Wait until the active count is below the current limit."""
        async with self._cond:
            while self._active >= self._limit:
                await self._cond.wait()
            self._active += 1

    async def release(self) -> None:
        """Release one slot and notify waiters."""
        async with self._cond:
            self._active -= 1
            self._cond.notify_all()

    async def __aenter__(self) -> AdaptiveSemaphore:
        await self.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()

    # ── Latency recording & adaptation ───────────────────────

    def record_latency(self, seconds: float) -> None:
        """Record a completed task's duration and maybe adjust the limit.

        Safe to call from an async context (non-blocking).  Adjustment
        decisions are made synchronously; the *next* ``acquire()`` call
        will see the updated limit.
        """
        self._samples.append(seconds)
        self._since_adjust += 1
        if self._since_adjust >= self._adjust_every:
            self._maybe_adjust()
            self._since_adjust = 0

    def _maybe_adjust(self) -> None:
        """Evaluate recent latency window and adjust limit if warranted."""
        if len(self._samples) < self._window:
            return
        recent = self._samples[-self._window :]
        avg_ms = statistics.mean(recent) * 1000

        old = self._limit
        if avg_ms < self._low_ms and self._limit < self._max:
            self._limit += 1
        elif avg_ms > self._high_ms and self._limit > self._min:
            self._limit -= 1

        if self._limit != old:
            self._adjustments.append((time.monotonic(), old, self._limit, round(avg_ms, 1)))


# ── Private helpers ──────────────────────────────────────────


def _percentile(sorted_data: list[float], pct: float) -> float:
    """Compute the p-th percentile from already-sorted data."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * pct / 100.0
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[-1]
    d = k - f
    return sorted_data[f] * (1 - d) + sorted_data[c] * d


def _fmt_duration(seconds: float) -> str:
    """Format seconds as a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds) // 60
    secs = seconds - minutes * 60
    if minutes < 60:
        return f"{minutes}m {secs:04.1f}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins:02d}m {secs:04.1f}s"


def _fmt_bytes(n: int) -> str:
    """Format a byte count as a human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _humanise(key: str) -> str:
    """Convert snake_case key to Title Case label."""
    return key.replace("_", " ").title()
