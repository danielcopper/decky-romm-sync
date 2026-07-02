"""Tests for PlaytimeService — SQLite ``rom_playtime`` aggregate + native play-session ingest."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import _make_retry
from fakes.fake_romm_api import FakeRommApi
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

from domain.playtime import PendingPlaySession, Playtime
from domain.rom import Rom
from lib.errors import RommApiError, RommForbiddenError
from services.playtime import PlaytimeService, PlaytimeServiceConfig, _coerce_duration_ms


class FakeDeviceIdProvider:
    """In-memory ``DeviceIdProvider`` — ``None`` models an unregistered device."""

    def __init__(self, device_id: str | None = "device-1") -> None:
        self._device_id = device_id

    def get_device_id(self) -> str | None:
        return self._device_id


def _seed_rom(uow: FakeUnitOfWork, rom_id: int) -> None:
    """Insert the FK-parent ``roms`` row so a child ``rom_playtime`` write commits."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug="n64",
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.z64",
        shortcut_app_id=1000 + rom_id,
        last_synced_at="2025-01-01T00:00:00",
    )
    with uow:
        uow.roms.save(rom)


def _seed_playtime(uow: FakeUnitOfWork, rom_id: int, playtime: Playtime) -> None:
    """Seed a Rom (FK parent) THEN its playtime aggregate, in one commit."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug="n64",
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.z64",
        shortcut_app_id=1000 + rom_id,
        last_synced_at="2025-01-01T00:00:00",
    )
    with uow:
        uow.roms.save(rom)
        uow.playtime.save(rom_id, playtime)


def make_service(fake_api=None, clock=None, uow=None, device_id: str | None = "device-1", **overrides):
    """Create a PlaytimeService with sensible defaults.

    Returns ``(svc, fake, uow)``. ``device_id`` seeds the injected
    ``DeviceIdProvider`` — pass ``None`` to model an unregistered device.
    """
    fake = fake_api or FakeRommApi()
    unit = uow or FakeUnitOfWork()
    clk = clock or FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))

    defaults: dict[str, Any] = {
        "romm_api": fake,
        "retry": _make_retry(),
        "device_id_provider": FakeDeviceIdProvider(device_id),
        "loop": asyncio.get_event_loop(),
        "logger": logging.getLogger("test"),
        "clock": clk,
        "log_debug": lambda _msg: None,
        "uow_factory": FakeUnitOfWorkFactory(unit),
    }
    defaults.update(overrides)
    svc = PlaytimeService(config=PlaytimeServiceConfig(**defaults))
    return svc, fake, unit


# ---------------------------------------------------------------------------
# TestRecordSession
# ---------------------------------------------------------------------------


class TestRecordSession:
    @pytest.mark.asyncio
    async def test_start_creates_entry(self):
        svc, _, uow = make_service()
        _seed_rom(uow, 42)

        result = svc.record_session_start(42)

        assert result["success"] is True
        assert uow.committed is True
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.last_session_start is not None

    @pytest.mark.asyncio
    async def test_start_on_orphan_rom_id_fails(self):
        """No ``roms`` row → FK violation at commit → failure dict, not committed."""
        svc, _, uow = make_service()  # no _seed_rom

        result = svc.record_session_start(42)

        assert result["success"] is False
        assert "Unknown ROM" in result["message"]
        assert uow.committed is False

    @pytest.mark.asyncio
    async def test_end_records_duration(self):
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        svc, _, uow = make_service(clock=clk)
        start = (clk.now() - timedelta(seconds=60)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))

        result = await svc.record_session_end(42)

        assert result["success"] is True
        assert result["duration_sec"] == 60
        assert result["session_count"] == 1
        assert result["total_seconds"] == 60
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 60
        assert entry.last_session_start is None

    @pytest.mark.asyncio
    async def test_end_without_start(self):
        svc, _, uow = make_service()
        _seed_playtime(uow, 42, Playtime())  # no open session

        result = await svc.record_session_end(42)

        assert result["success"] is False
        assert "No active session" in result["message"]

    @pytest.mark.asyncio
    async def test_end_with_no_aggregate(self):
        """No playtime row at all → No active session."""
        svc, _, uow = make_service()
        _seed_rom(uow, 42)  # roms row exists, no playtime row

        result = await svc.record_session_end(42)

        assert result["success"] is False
        assert "No active session" in result["message"]

    @pytest.mark.asyncio
    async def test_end_with_unparseable_start(self):
        """Malformed last_session_start -> record_session raises -> failure."""
        svc, _, uow = make_service()
        _seed_playtime(uow, 42, Playtime(last_session_start="not-a-date"))

        result = await svc.record_session_end(42)

        assert result["success"] is False
        assert "Failed to calculate session duration" in result["message"]

    @pytest.mark.asyncio
    async def test_multiple_sessions_accumulate(self):
        clk = FakeClock(now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC))
        svc, _, uow = make_service(clock=clk)
        _seed_rom(uow, 42)

        start1 = (clk.now() - timedelta(seconds=30)).isoformat()
        with uow:
            uow.playtime.save(42, Playtime(last_session_start=start1))
        await svc.record_session_end(42)

        start2 = (clk.now() - timedelta(seconds=45)).isoformat()
        with uow:
            entry = uow.playtime.get(42)
            assert entry is not None
            entry.begin_session(start2)
            uow.playtime.save(42, entry)
        result2 = await svc.record_session_end(42)

        assert result2["session_count"] == 2
        assert result2["total_seconds"] == 75  # 30 + 45

    @pytest.mark.asyncio
    async def test_session_clamps_to_24h(self):
        clk = FakeClock(now=datetime(2026, 1, 2, 1, 0, tzinfo=UTC))
        svc, _, uow = make_service(clock=clk)
        start = (clk.now() - timedelta(hours=25)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))

        result = await svc.record_session_end(42)

        assert result["success"] is True
        assert result["duration_sec"] == 86400

    @pytest.mark.asyncio
    async def test_suspended_seconds_threaded_to_domain(self):
        """``suspended_seconds`` is subtracted from the counted session duration."""
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
        svc, _, uow = make_service(clock=clk)
        start = (clk.now() - timedelta(seconds=300)).isoformat()  # 5min elapsed
        _seed_playtime(uow, 42, Playtime(last_session_start=start))

        result = await svc.record_session_end(42, 120)  # 120s suspended

        assert result["success"] is True
        assert result["duration_sec"] == 180  # 300 minus 120
        assert result["total_seconds"] == 180
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 180

    @pytest.mark.asyncio
    async def test_default_suspend_counts_full_duration(self):
        """Omitting ``suspended_seconds`` (default 0) counts the full elapsed span."""
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
        svc, _, uow = make_service(clock=clk)
        start = (clk.now() - timedelta(seconds=300)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))

        result = await svc.record_session_end(42)

        assert result["success"] is True
        assert result["duration_sec"] == 300


# ---------------------------------------------------------------------------
# TestRecordSessionEndIngest — the enqueue + flush behaviour on exit
# ---------------------------------------------------------------------------


class TestRecordSessionEndIngest:
    @pytest.mark.asyncio
    async def test_registered_device_ingests_and_dequeues(self):
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        svc, fake, uow = make_service(clock=clk)
        start = (clk.now() - timedelta(seconds=60)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))

        await svc.record_session_end(42)

        # The session was POSTed with device_id + duration_ms = seconds * 1000.
        ingests = [c for c in fake.call_log if c[0] == "ingest_play_sessions"]
        assert len(ingests) == 1
        device_id, sessions = ingests[0][1]
        assert device_id == "device-1"
        assert sessions[0]["rom_id"] == 42
        assert sessions[0]["duration_ms"] == 60_000
        # Server stored it, and the outbox drained on success.
        assert fake.play_sessions[42][0]["duration_ms"] == 60_000
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}

    @pytest.mark.asyncio
    async def test_unregistered_device_folds_locally_no_ingest(self):
        """Unregistered device → fold locally, never enqueue, never POST (decision #8)."""
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        logs: list[str] = []
        svc, fake, uow = make_service(clock=clk, device_id=None, log_debug=logs.append)
        start = (clk.now() - timedelta(seconds=60)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))

        result = await svc.record_session_end(42)

        assert result["success"] is True
        assert result["total_seconds"] == 60
        assert not any(c[0] == "ingest_play_sessions" for c in fake.call_log)
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 60
        assert entry.pending_sessions == {}
        assert any("not enqueued" in m and "unregistered" in m for m in logs)

    @pytest.mark.asyncio
    async def test_ingest_failure_keeps_session_queued(self):
        """Offline exit: fold commits, the session stays in the outbox for a later flush."""
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        logs: list[str] = []
        svc, fake, uow = make_service(clock=clk, log_debug=logs.append)
        start = (clk.now() - timedelta(seconds=60)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))
        fake.fail_on_next(RommApiError("offline"))

        result = await svc.record_session_end(42)

        assert result["success"] is True
        assert result["total_seconds"] == 60
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 60
        assert set(entry.pending_sessions) == {start}
        assert any("Play-session ingest failed" in m for m in logs)

    @pytest.mark.asyncio
    async def test_queued_session_flushes_on_next_launch(self):
        """A session queued while offline drains on the next successful flush."""
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        svc, fake, uow = make_service(clock=clk)
        start = (clk.now() - timedelta(seconds=60)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))
        fake.fail_on_next(RommApiError("offline"))
        await svc.record_session_end(42)  # queues offline

        await svc.flush_pending_sessions()  # reconnect

        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}
        assert fake.play_sessions[42][0]["duration_ms"] == 60_000


# ---------------------------------------------------------------------------
# TestFlushPendingSessions — the outbox flush worker in isolation
# ---------------------------------------------------------------------------


def _pending(
    device_id: str = "device-1", end_time: str = "e", duration_ms: int = 1000, attempts: int = 0
) -> PendingPlaySession:
    return PendingPlaySession(device_id=device_id, end_time=end_time, duration_ms=duration_ms, attempts=attempts)


class TestFlushPendingSessions:
    @pytest.mark.asyncio
    async def test_empty_outbox_makes_no_call(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=10))

        await svc.flush_pending_sessions()

        assert not any(c[0] == "ingest_play_sessions" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_flush_posts_under_stored_device_id_even_when_current_unregistered(self):
        """FIX 4: the flush uses each row's STORED device_id, not the current provider.

        A row queued while registered must still flush after the current device id
        clears/changes — the old get_device_id() gate is gone. Enqueue is the only
        gate on registration, so every queued row already carries a valid id.
        """
        svc, fake, uow = make_service(device_id=None)  # current provider: unregistered
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s1": _pending(device_id="device-1")}))

        await svc.flush_pending_sessions()

        ingests = [c for c in fake.call_log if c[0] == "ingest_play_sessions"]
        assert len(ingests) == 1
        device_id, _sessions = ingests[0][1]
        assert device_id == "device-1"  # stored id, not the (None) current provider
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}  # drained under the stored id

    @pytest.mark.asyncio
    async def test_created_dequeues_outbox(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s1": _pending(duration_ms=500)}))

        await svc.flush_pending_sessions()

        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}
        assert fake.play_sessions[42][0]["duration_ms"] == 500

    @pytest.mark.asyncio
    async def test_byte_duplicate_repost_still_dequeues(self):
        """A re-POST of an already-ingested window returns ``duplicate`` — still dequeued."""
        svc, fake, uow = make_service()
        # Pre-ingest the exact window so the flush POST comes back as a duplicate.
        fake.ingest_play_sessions("device-1", [{"rom_id": 42, "start_time": "s1", "end_time": "e", "duration_ms": 500}])
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s1": _pending(duration_ms=500)}))

        await svc.flush_pending_sessions()

        assert fake.call_log[-1][0] == "ingest_play_sessions"
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}  # duplicate counts as sent
        assert len(fake.play_sessions[42]) == 1  # no second server row

    @pytest.mark.asyncio
    async def test_error_status_stays_queued(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s1": _pending()}))

        def _all_error(_device_id, sessions):
            return {
                "results": [{"index": i, "status": "error"} for i, _ in enumerate(sessions)],
                "created_count": 0,
                "skipped_count": 0,
            }

        fake.ingest_play_sessions = _all_error  # type: ignore[method-assign]

        await svc.flush_pending_sessions()

        entry = uow.playtime.get(42)
        assert entry is not None
        assert set(entry.pending_sessions) == {"s1"}  # error rows stay queued

    @pytest.mark.asyncio
    async def test_over_100_backlog_flushes_incrementally(self):
        svc, fake, uow = make_service()
        outbox = {f"s{i}": _pending(end_time=f"e{i}", duration_ms=i + 1) for i in range(150)}
        _seed_playtime(uow, 42, Playtime(pending_sessions=outbox))

        await svc.flush_pending_sessions()  # drains up to 100
        entry = uow.playtime.get(42)
        assert entry is not None
        assert len(entry.pending_sessions) == 50
        assert len(fake.play_sessions[42]) == 100

        await svc.flush_pending_sessions()  # drains the rest
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}
        assert len(fake.play_sessions[42]) == 150

    @pytest.mark.asyncio
    async def test_ingest_exception_is_swallowed(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s1": _pending()}))
        fake.fail_on_next(RommApiError("boom"))

        await svc.flush_pending_sessions()  # must not raise

        entry = uow.playtime.get(42)
        assert entry is not None
        assert set(entry.pending_sessions) == {"s1"}  # stays queued


# ---------------------------------------------------------------------------
# TestGetPlaytime
# ---------------------------------------------------------------------------


class TestGetPlaytime:
    @pytest.mark.asyncio
    async def test_get_all_playtime_minimal_wire_shape(self):
        svc, _, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=2))
        _seed_playtime(uow, 99, Playtime(total_seconds=200, session_count=5))

        result = svc.get_all_playtime()

        assert set(result.keys()) == {"playtime"}
        assert result["playtime"]["42"] == {"total_seconds": 100, "session_count": 2}
        assert result["playtime"]["99"] == {"total_seconds": 200, "session_count": 5}
        assert set(result["playtime"]["42"].keys()) == {"total_seconds", "session_count"}

    @pytest.mark.asyncio
    async def test_get_all_playtime_empty(self):
        svc, _, _ = make_service()

        result = svc.get_all_playtime()

        assert result == {"playtime": {}}


# ---------------------------------------------------------------------------
# TestReconcilePlaytime
# ---------------------------------------------------------------------------


def _seed_server_sessions(fake: FakeRommApi, rom_id: int, *durations_ms: int) -> None:
    """Stage server-side play-session rows for a ROM (each carries ``duration_ms``)."""
    fake.play_sessions[rom_id] = [
        {"id": 3000 + i, "rom_id": rom_id, "duration_ms": d} for i, d in enumerate(durations_ms)
    ]


class TestReconcilePlaytime:
    @pytest.mark.asyncio
    async def test_server_ahead_raises_local_total(self):
        """Local < Σ server → reconcile raises the local total to the server union."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=2))
        _seed_server_sessions(fake, 42, 300_000, 200_000)  # 500s total

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 500
        assert result["session_count"] == 2  # untouched by a pull
        assert result["server_query_failed"] is False
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 500

    @pytest.mark.asyncio
    async def test_local_ahead_is_noop(self):
        """Local >= Σ server → reconcile_total never regresses, total unchanged."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=900, session_count=4))
        _seed_server_sessions(fake, 42, 300_000)  # 300s

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 900
        assert result["server_query_failed"] is False
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 900

    @pytest.mark.asyncio
    async def test_no_server_sessions_keeps_local_row(self):
        """Empty server history → reconcile_total(0) is a no-op; local total preserved."""
        svc, _, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=120, session_count=1))

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 120
        assert result["server_query_failed"] is False
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 120

    @pytest.mark.asyncio
    async def test_no_server_data_no_local_row_returns_zero_no_seed(self):
        """No server history AND no local row → return zero, do NOT seed a row."""
        svc, _, uow = make_service()
        _seed_rom(uow, 42)  # roms row, no playtime row, no server sessions

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 0
        assert result["session_count"] == 0
        assert result["server_query_failed"] is False
        assert uow.playtime.get(42) is None

    @pytest.mark.asyncio
    async def test_server_unreachable_returns_local_total(self):
        """Fetch raises → server_query_failed True, returns the local row's total."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=250, session_count=3))
        fake.fail_on_next(RommApiError("unreachable"))

        result = await svc.reconcile_playtime(42)

        assert result["server_query_failed"] is True
        assert result["total_seconds"] == 250
        assert result["session_count"] == 3

    @pytest.mark.asyncio
    async def test_orphan_rom_id_is_graceful_noop(self):
        """rom_id absent from roms → IntegrityError at commit → graceful result."""
        svc, fake, uow = make_service()  # no _seed_rom: rom_id 42 orphaned
        _seed_server_sessions(fake, 42, 500_000)

        result = await svc.reconcile_playtime(42)

        assert result["server_query_failed"] is False
        assert result["total_seconds"] == 0
        assert result["session_count"] == 0
        # The orphan write's FK check aborts the commit and rolls it back, so no
        # rom_playtime row is left behind.
        assert uow.rolled_back is True
        assert uow.playtime.get(42) is None

    @pytest.mark.asyncio
    async def test_double_run_is_idempotent(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=1))
        _seed_server_sessions(fake, 42, 500_000)

        first = await svc.reconcile_playtime(42)
        second = await svc.reconcile_playtime(42)

        assert first["total_seconds"] == second["total_seconds"] == 500
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 500

    @pytest.mark.asyncio
    async def test_flushes_outbox_before_reading_then_maxes(self):
        """Reconcile drains the outbox first, so the fresh session is in the server union."""
        svc, fake, uow = make_service()
        # Local total 300 (from an as-yet-unflushed session) + a foreign device's
        # 500s already on the server. After the flush the union is 800s.
        _seed_playtime(
            uow,
            42,
            Playtime(total_seconds=300, session_count=1, pending_sessions={"s1": _pending(duration_ms=300_000)}),
        )
        _seed_server_sessions(fake, 42, 500_000)  # foreign device's history

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 800  # max(300, 300 + 500)
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}  # drained
        assert entry.total_seconds == 800

    @pytest.mark.asyncio
    async def test_emits_outcome_debug_line(self):
        """Each reconcile logs one debug line naming the rom and its outcome."""
        logs: list[str] = []
        svc, fake, uow = make_service(log_debug=logs.append)
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=1))
        _seed_server_sessions(fake, 42, 500_000)
        _seed_rom(uow, 99)  # roms row, no server sessions, no local row

        await svc.reconcile_playtime(42)  # server data present
        await svc.reconcile_playtime(99)  # no server data, no local row

        assert any("rom 42" in m and "server=500s" in m and "total=500s" in m for m in logs)
        assert any("rom 99" in m and "no server sessions" in m for m in logs)


# ---------------------------------------------------------------------------
# Helpers for the ingest-verdict tests below
# ---------------------------------------------------------------------------


def _canned_ingest(response: dict[str, Any]):
    """Build an ``ingest_play_sessions`` stub that always returns ``response``."""

    def _ingest(_device_id: str, _sessions: list[dict[str, Any]]) -> dict[str, Any]:
        return response

    return _ingest


def _verdict_by_start(verdicts: dict[str, str]):
    """Build an ``ingest_play_sessions`` stub verdicting each row by its ``start_time``.

    Unlisted start times default to ``created`` so a "healthy" row alongside a
    failing one still dequeues.
    """

    def _ingest(_device_id: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        results = [{"index": i, "status": verdicts.get(s["start_time"], "created")} for i, s in enumerate(sessions)]
        return {"results": results, "created_count": 0, "skipped_count": 0}

    return _ingest


# ---------------------------------------------------------------------------
# TestCoerceDurationMs — FIX 1
# ---------------------------------------------------------------------------


class TestCoerceDurationMs:
    def test_real_int_returns_value(self):
        assert _coerce_duration_ms({"duration_ms": 5000}) == 5000

    def test_float_is_truncated_to_int(self):
        assert _coerce_duration_ms({"duration_ms": 1999.9}) == 1999

    def test_none_returns_zero(self):
        assert _coerce_duration_ms({"duration_ms": None}) == 0

    def test_string_returns_zero(self):
        assert _coerce_duration_ms({"duration_ms": "nope"}) == 0

    def test_missing_key_returns_zero(self):
        assert _coerce_duration_ms({"id": 1}) == 0

    def test_non_dict_row_returns_zero(self):
        assert _coerce_duration_ms("not-a-dict") == 0
        assert _coerce_duration_ms(None) == 0
        assert _coerce_duration_ms([1, 2]) == 0

    def test_bool_is_not_counted(self):
        # bool is an int subclass — must NOT count True as 1ms.
        assert _coerce_duration_ms({"duration_ms": True}) == 0


# ---------------------------------------------------------------------------
# TestReconcileMalformedRows — FIX 1 (reconcile never crashes on a bad row)
# ---------------------------------------------------------------------------


class TestReconcileMalformedRows:
    @pytest.mark.asyncio
    async def test_null_and_string_durations_are_coerced_not_raised(self):
        """A row with duration_ms null/non-numeric is coerced to 0, not a crash."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=0, session_count=1))
        fake.play_sessions[42] = [
            {"id": 1, "rom_id": 42, "duration_ms": None},
            {"id": 2, "rom_id": 42, "duration_ms": "nope"},
            {"id": 3, "rom_id": 42, "duration_ms": 5000},
        ]

        result = await svc.reconcile_playtime(42)

        # Only the real 5000ms row counts -> 5s. Partial success, never a raise.
        assert result["server_query_failed"] is False
        assert result["total_seconds"] == 5


# ---------------------------------------------------------------------------
# TestReconcileTruncation — test 16
# ---------------------------------------------------------------------------


class TestReconcileTruncation:
    @pytest.mark.asyncio
    async def test_sub_second_ms_truncates_down(self):
        """1999ms sums to 1s (floor division, no rounding up)."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=0, session_count=1))
        _seed_server_sessions(fake, 42, 1999)

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 1


# ---------------------------------------------------------------------------
# TestSessionStartTimeEndToEnd — test 11
# ---------------------------------------------------------------------------


class TestSessionStartTimeEndToEnd:
    @pytest.mark.asyncio
    async def test_ingested_session_carries_the_start_timestamp(self):
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        svc, fake, uow = make_service(clock=clk)
        start = (clk.now() - timedelta(seconds=60)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))

        await svc.record_session_end(42)

        ingests = [c for c in fake.call_log if c[0] == "ingest_play_sessions"]
        _device_id, sessions = ingests[0][1]
        assert sessions[0]["start_time"] == start


# ---------------------------------------------------------------------------
# TestFlushIndexCorrelation — test 12
# ---------------------------------------------------------------------------


class TestFlushIndexCorrelation:
    @pytest.mark.asyncio
    async def test_mixed_verdicts_only_accepted_dequeue(self):
        """created + duplicate dequeue; error stays queued (attempts bumped)."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s0": _pending(), "s1": _pending(), "s2": _pending()}))
        fake.ingest_play_sessions = _canned_ingest(  # type: ignore[method-assign]
            {
                "results": [
                    {"index": 0, "status": "created"},
                    {"index": 1, "status": "error"},
                    {"index": 2, "status": "duplicate"},
                ],
                "created_count": 1,
                "skipped_count": 1,
            }
        )

        await svc.flush_pending_sessions()

        entry = uow.playtime.get(42)
        assert entry is not None
        assert set(entry.pending_sessions) == {"s1"}  # only the error row stays
        assert entry.pending_sessions["s1"].attempts == 1

    @pytest.mark.asyncio
    async def test_reordered_results_correlate_by_index_not_position(self):
        """Results returned out of order still dequeue the right rows."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s0": _pending(), "s1": _pending(), "s2": _pending()}))
        fake.ingest_play_sessions = _canned_ingest(  # type: ignore[method-assign]
            {
                "results": [
                    {"index": 2, "status": "created"},
                    {"index": 0, "status": "created"},
                    {"index": 1, "status": "created"},
                ],
                "created_count": 3,
                "skipped_count": 0,
            }
        )

        await svc.flush_pending_sessions()

        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}  # all three dequeued regardless of order

    @pytest.mark.asyncio
    async def test_short_results_array_leaves_unacked_queued(self):
        """Rows absent from the results array stay queued (no attempt bump)."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s0": _pending(), "s1": _pending(), "s2": _pending()}))
        fake.ingest_play_sessions = _canned_ingest(  # type: ignore[method-assign]
            {"results": [{"index": 0, "status": "created"}], "created_count": 1, "skipped_count": 0}
        )

        await svc.flush_pending_sessions()

        entry = uow.playtime.get(42)
        assert entry is not None
        assert set(entry.pending_sessions) == {"s1", "s2"}
        # Absent (not error) → no attempt increment.
        assert entry.pending_sessions["s1"].attempts == 0

    @pytest.mark.asyncio
    async def test_out_of_range_and_missing_index_are_skipped_no_error(self):
        """An out-of-range / negative index is ignored, no IndexError, rows stay queued."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s0": _pending()}))
        fake.ingest_play_sessions = _canned_ingest(  # type: ignore[method-assign]
            {
                "results": [
                    {"index": 99, "status": "created"},
                    {"index": -1, "status": "created"},
                    {"status": "created"},
                ],
                "created_count": 0,
                "skipped_count": 0,
            }
        )

        await svc.flush_pending_sessions()  # must not raise

        entry = uow.playtime.get(42)
        assert entry is not None
        assert set(entry.pending_sessions) == {"s0"}  # untouched


# ---------------------------------------------------------------------------
# TestFlushPerDeviceGrouping — FIX 4 / test 15
# ---------------------------------------------------------------------------


class TestFlushPerDeviceGrouping:
    @pytest.mark.asyncio
    async def test_two_device_ids_produce_two_posts(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 1, Playtime(pending_sessions={"a1": _pending(device_id="device-A", duration_ms=100)}))
        _seed_playtime(uow, 2, Playtime(pending_sessions={"b1": _pending(device_id="device-B", duration_ms=200)}))

        await svc.flush_pending_sessions()

        ingests = [c for c in fake.call_log if c[0] == "ingest_play_sessions"]
        assert len(ingests) == 2
        posted_device_ids = {call[1][0] for call in ingests}
        assert posted_device_ids == {"device-A", "device-B"}
        # Each POST carries only its own device's row.
        by_device = {call[1][0]: call[1][1] for call in ingests}
        assert [s["rom_id"] for s in by_device["device-A"]] == [1]
        assert [s["rom_id"] for s in by_device["device-B"]] == [2]
        # Both roms dequeue.
        assert uow.playtime.get(1).pending_sessions == {}  # type: ignore[union-attr]
        assert uow.playtime.get(2).pending_sessions == {}  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_two_roms_same_device_one_post(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 1, Playtime(pending_sessions={"a1": _pending(device_id="device-A")}))
        _seed_playtime(uow, 2, Playtime(pending_sessions={"a2": _pending(device_id="device-A")}))

        await svc.flush_pending_sessions()

        ingests = [c for c in fake.call_log if c[0] == "ingest_play_sessions"]
        assert len(ingests) == 1  # one device -> one POST carrying both roms
        _device_id, sessions = ingests[0][1]
        assert sorted(s["rom_id"] for s in sessions) == [1, 2]
        assert uow.playtime.get(1).pending_sessions == {}  # type: ignore[union-attr]
        assert uow.playtime.get(2).pending_sessions == {}  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TestFlushBoundedRetry — D2
# ---------------------------------------------------------------------------


class TestFlushBoundedRetry:
    @pytest.mark.asyncio
    async def test_error_increments_attempts_and_stays_queued(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s1": _pending(attempts=0)}))
        fake.ingest_play_sessions = _verdict_by_start({"s1": "error"})  # type: ignore[method-assign]

        await svc.flush_pending_sessions()

        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions["s1"].attempts == 1

    @pytest.mark.asyncio
    async def test_row_quarantined_and_warned_at_threshold(self, caplog):
        """A row already at attempts=4 draws its 5th error -> dropped + warning."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s1": _pending(attempts=4)}))
        fake.ingest_play_sessions = _verdict_by_start({"s1": "error"})  # type: ignore[method-assign]

        with caplog.at_level("WARNING"):
            await svc.flush_pending_sessions()

        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.pending_sessions == {}  # quarantined (dropped)
        assert any("rom 42" in r.message and "s1" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_healthy_row_dequeues_while_error_row_retries(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s0": _pending(), "s1": _pending()}))
        fake.ingest_play_sessions = _verdict_by_start({"s1": "error"})  # type: ignore[method-assign]

        await svc.flush_pending_sessions()

        entry = uow.playtime.get(42)
        assert entry is not None
        assert set(entry.pending_sessions) == {"s1"}  # healthy s0 drained, s1 retries
        assert entry.pending_sessions["s1"].attempts == 1


# ---------------------------------------------------------------------------
# TestFlushUndrainedBreadcrumb — FIX 6
# ---------------------------------------------------------------------------


class TestFlushUndrainedBreadcrumb:
    @pytest.mark.asyncio
    async def test_undrained_rows_logged_with_count_and_roms(self):
        logs: list[str] = []
        svc, fake, uow = make_service(log_debug=logs.append)
        _seed_playtime(uow, 42, Playtime(pending_sessions={"s0": _pending(), "s1": _pending()}))
        # Only s0 acknowledged; s1 absent from results -> undrained.
        fake.ingest_play_sessions = _canned_ingest(  # type: ignore[method-assign]
            {"results": [{"index": 0, "status": "created"}], "created_count": 1, "skipped_count": 0}
        )

        await svc.flush_pending_sessions()

        assert any("not accepted" in m and "42" in m for m in logs)
        entry = uow.playtime.get(42)
        assert entry is not None
        assert set(entry.pending_sessions) == {"s1"}


# ---------------------------------------------------------------------------
# TestRecordSessionEndOuterCatch — FIX 10
# ---------------------------------------------------------------------------


class TestRecordSessionEndOuterCatch:
    @pytest.mark.asyncio
    async def test_flush_raise_is_swallowed_by_outer_catch(self):
        """A raising ``_flush_pending_sessions_io`` is caught; the record still succeeds + logs."""
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        logs: list[str] = []
        svc, _, uow = make_service(clock=clk, log_debug=logs.append)
        start = (clk.now() - timedelta(seconds=60)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))

        def _raise() -> None:
            raise RuntimeError("boom")

        svc._flush_pending_sessions_io = _raise  # type: ignore[method-assign]

        result = await svc.record_session_end(42)

        assert result["success"] is True
        assert result["total_seconds"] == 60
        assert any("play-session flush failed (non-fatal)" in m for m in logs)


# ---------------------------------------------------------------------------
# TestReconcilePreFlushSafety — FIX 3
# ---------------------------------------------------------------------------


class TestReconcilePreFlushSafety:
    @pytest.mark.asyncio
    async def test_preflush_error_does_not_escape_reconcile(self):
        """A raising flush worker is swallowed; reconcile still fetches + folds."""
        logs: list[str] = []
        svc, fake, uow = make_service(log_debug=logs.append)
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=1))
        _seed_server_sessions(fake, 42, 500_000)

        def _raise() -> None:
            raise RuntimeError("database is locked")

        svc._flush_pending_sessions_worker = _raise  # type: ignore[method-assign]

        result = await svc.reconcile_playtime(42)

        assert result["server_query_failed"] is False
        assert result["total_seconds"] == 500  # GET + fold still ran
        assert any("flush failed (non-fatal)" in m for m in logs)


# ---------------------------------------------------------------------------
# TestScopeNotice — D1
# ---------------------------------------------------------------------------


class TestScopeNotice:
    @pytest.mark.asyncio
    async def test_forbidden_get_sets_notice_and_degrades_local(self):
        logs: list[str] = []
        svc, fake, uow = make_service(log_debug=logs.append)
        _seed_playtime(uow, 42, Playtime(total_seconds=250, session_count=3))
        fake.list_play_sessions_side_effect = RommForbiddenError("token lacks scope")

        result = await svc.reconcile_playtime(42)

        assert result["server_query_failed"] is True
        assert result["total_seconds"] == 250  # local-only degrade, not a raise
        assert svc.get_scope_notice() == {"pending": True}
        assert any("roms.user.read" in m for m in logs)

    @pytest.mark.asyncio
    async def test_successful_get_clears_stale_notice(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=1))
        _seed_server_sessions(fake, 42, 500_000)
        with uow:
            uow.kv_config.set("playtime_scope_notice", "1")  # a prior 403 raised it

        await svc.reconcile_playtime(42)

        assert svc.get_scope_notice() == {"pending": False}

    @pytest.mark.asyncio
    async def test_clear_scope_notice_is_idempotent_when_absent(self):
        svc, _, _ = make_service()
        # No flag set — clearing must be a safe no-op.
        svc.clear_scope_notice()
        assert svc.get_scope_notice() == {"pending": False}

    def test_get_scope_notice_default_is_not_pending(self):
        svc, _, _ = make_service()
        assert svc.get_scope_notice() == {"pending": False}
