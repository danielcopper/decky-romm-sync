"""Tests for lib.perf — PerfCollector and ETAEstimator."""

import time

from lib.perf import ETAEstimator, PerfCollector, _fmt_bytes, _fmt_duration, _percentile


# ── PerfCollector ────────────────────────────────────────────


class TestPerfCollectorLifecycle:
    """Tests for start/end sync and wall time."""

    def test_wall_time_is_zero_before_start(self):
        pc = PerfCollector()
        assert pc.wall_time == 0.0

    def test_wall_time_accumulates_after_start(self):
        pc = PerfCollector()
        pc.start_sync()
        time.sleep(0.05)
        assert pc.wall_time >= 0.04

    def test_end_sync_freezes_wall_time(self):
        pc = PerfCollector()
        pc.start_sync()
        time.sleep(0.05)
        pc.end_sync()
        wt = pc.wall_time
        time.sleep(0.05)
        assert pc.wall_time == wt

    def test_start_sync_clears_prior_data(self):
        pc = PerfCollector()
        pc.start_sync()
        pc.increment("test_counter", 42)
        pc.set_gauge("test_gauge", 3.14)
        pc.record_http_request("GET", "/api/test", 0.1, 200, 1024)

        pc.start_sync()
        report = pc.generate_report()
        assert report["counters"] == {}
        assert report["gauges"] == {}
        assert report["http"]["total_requests"] == 0


class TestPerfCollectorPhases:
    """Tests for time_phase context manager."""

    def test_time_phase_records_elapsed(self):
        pc = PerfCollector()
        pc.start_sync()
        with pc.time_phase("test_phase"):
            time.sleep(0.05)
        report = pc.generate_report()
        assert "test_phase" in report["phases"]
        assert report["phases"]["test_phase"]["elapsed_sec"] >= 0.04

    def test_multiple_phases(self):
        pc = PerfCollector()
        pc.start_sync()
        with pc.time_phase("phase_a"):
            time.sleep(0.02)
        with pc.time_phase("phase_b"):
            time.sleep(0.02)
        report = pc.generate_report()
        assert len(report["phases"]) == 2


class TestPerfCollectorOperations:
    """Tests for time_operation context manager (repeated timings)."""

    def test_time_operation_appends_samples(self):
        pc = PerfCollector()
        pc.start_sync()
        for _ in range(5):
            with pc.time_operation("page_fetch"):
                time.sleep(0.01)
        report = pc.generate_report()
        assert report["operations"]["page_fetch"]["count"] == 5
        assert report["operations"]["page_fetch"]["avg_ms"] > 0

    def test_empty_operations(self):
        pc = PerfCollector()
        pc.start_sync()
        pc.end_sync()
        report = pc.generate_report()
        assert report["operations"] == {}


class TestPerfCollectorHttp:
    """Tests for HTTP sample recording."""

    def test_record_http_requests(self):
        pc = PerfCollector()
        pc.start_sync()
        pc.record_http_request("GET", "/api/platforms", 0.150, 200, 4096)
        pc.record_http_request("GET", "/api/roms?offset=0", 0.200, 200, 8192)
        pc.record_http_request("GET", "/api/roms?offset=50", 0.500, 500, 0)

        report = pc.generate_report()
        assert report["http"]["total_requests"] == 3
        assert report["http"]["total_bytes"] == 4096 + 8192
        assert report["http"]["errors"] == 1

    def test_http_latency_stats(self):
        pc = PerfCollector()
        pc.start_sync()
        for i in range(100):
            pc.record_http_request("GET", f"/api/roms?offset={i * 50}", 0.1 + i * 0.002, 200, 1024)

        report = pc.generate_report()
        assert report["http"]["avg_latency_ms"] > 0
        assert report["http"]["p95_latency_ms"] > report["http"]["avg_latency_ms"]
        assert report["http"]["p50_latency_ms"] > 0


class TestPerfCollectorCountersGauges:
    """Tests for increment() and set_gauge()."""

    def test_increment(self):
        pc = PerfCollector()
        pc.start_sync()
        pc.increment("roms_fetched", 50)
        pc.increment("roms_fetched", 50)
        pc.increment("artwork_downloaded")
        report = pc.generate_report()
        assert report["counters"]["roms_fetched"] == 100
        assert report["counters"]["artwork_downloaded"] == 1

    def test_set_gauge(self):
        pc = PerfCollector()
        pc.start_sync()
        pc.set_gauge("bandwidth_mbps", 25.6)
        report = pc.generate_report()
        assert report["gauges"]["bandwidth_mbps"] == 25.6


class TestPerfCollectorReport:
    """Tests for report generation."""

    def test_generate_report_structure(self):
        pc = PerfCollector()
        pc.start_sync()
        pc.end_sync()
        report = pc.generate_report()
        assert "wall_time_sec" in report
        assert "phases" in report
        assert "http" in report
        assert "operations" in report
        assert "counters" in report
        assert "gauges" in report

    def test_format_report_returns_string(self):
        pc = PerfCollector()
        pc.start_sync()
        with pc.time_phase("test"):
            pass
        pc.record_http_request("GET", "/api/test", 0.1, 200, 1024)
        pc.increment("test_counter", 5)
        pc.end_sync()
        text = pc.format_report()
        assert "SYNC PERFORMANCE REPORT" in text
        assert "Phase Breakdown" in text
        assert "HTTP Summary" in text

    def test_format_report_identifies_bottleneck(self):
        pc = PerfCollector()
        pc.start_sync()
        with pc.time_phase("fast_phase"):
            time.sleep(0.01)
        with pc.time_phase("slow_phase"):
            time.sleep(0.05)
        pc.end_sync()
        text = pc.format_report()
        assert "Bottleneck" in text
        assert "Slow Phase" in text


# ── ETAEstimator ─────────────────────────────────────────────


class TestETAEstimator:
    """Tests for ETA estimation."""

    def test_eta_none_before_min_samples(self):
        est = ETAEstimator(min_samples=5)
        est.start()
        # Process 3 items — below min_samples threshold
        for i in range(1, 4):
            time.sleep(0.01)
            est.update(i)
        assert est.eta_seconds(3, 100) is None

    def test_eta_available_after_min_samples(self):
        est = ETAEstimator(alpha=0.3, min_samples=3)
        est.start()
        for i in range(1, 6):
            time.sleep(0.01)
            est.update(i)
        eta = est.eta_seconds(5, 100)
        assert eta is not None
        assert eta > 0

    def test_eta_decreases_as_progress_increases(self):
        est = ETAEstimator(alpha=0.3, min_samples=3)
        est.start()
        for i in range(1, 11):
            time.sleep(0.01)
            est.update(i)

        eta_early = est.eta_seconds(10, 100)

        for i in range(11, 51):
            time.sleep(0.005)
            est.update(i)

        eta_later = est.eta_seconds(50, 100)
        assert eta_early is not None
        assert eta_later is not None
        assert eta_later < eta_early

    def test_eta_is_none_when_total_reached(self):
        est = ETAEstimator(min_samples=1)
        est.start()
        time.sleep(0.01)
        est.update(100)
        assert est.eta_seconds(100, 100) is None

    def test_elapsed_tracks_time(self):
        est = ETAEstimator()
        est.start()
        time.sleep(0.05)
        assert est.elapsed >= 0.04

    def test_elapsed_zero_before_start(self):
        est = ETAEstimator()
        assert est.elapsed == 0.0

    def test_items_per_sec(self):
        est = ETAEstimator()
        est.start()
        time.sleep(0.05)
        est.update(10)
        ips = est.items_per_sec
        assert ips > 0

    def test_items_per_sec_zero_before_updates(self):
        est = ETAEstimator()
        est.start()
        assert est.items_per_sec == 0.0

    def test_no_update_on_zero_delta(self):
        est = ETAEstimator(min_samples=1)
        est.start()
        time.sleep(0.01)
        est.update(5)
        avg_before = est._avg_time
        est.update(5)  # same count — should be a no-op
        assert est._avg_time == avg_before


# ── Helper functions ─────────────────────────────────────────


class TestPercentile:
    def test_empty(self):
        assert _percentile([], 50) == 0.0

    def test_single_value(self):
        assert _percentile([0.5], 50) == 0.5
        assert _percentile([0.5], 95) == 0.5

    def test_known_values(self):
        data = sorted([float(i) for i in range(1, 101)])
        p50 = _percentile(data, 50)
        assert 49.0 < p50 < 51.0
        p95 = _percentile(data, 95)
        assert 94.0 < p95 < 96.0

    def test_p0_and_p100(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(data, 0) == 1.0
        assert _percentile(data, 100) == 5.0


class TestFmtDuration:
    def test_seconds(self):
        assert _fmt_duration(5.3) == "5.3s"

    def test_minutes(self):
        result = _fmt_duration(125.5)
        assert "2m" in result

    def test_hours(self):
        result = _fmt_duration(3661)
        assert "1h" in result


class TestFmtBytes:
    def test_bytes(self):
        assert _fmt_bytes(512) == "512 B"

    def test_kilobytes(self):
        assert "KB" in _fmt_bytes(2048)

    def test_megabytes(self):
        assert "MB" in _fmt_bytes(5 * 1024 * 1024)

    def test_gigabytes(self):
        assert "GB" in _fmt_bytes(2 * 1024 * 1024 * 1024)
