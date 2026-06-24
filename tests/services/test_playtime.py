"""Tests for PlaytimeService — SQLite ``rom_playtime`` aggregate + RomM notes."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import _make_retry
from fakes.fake_save_api import FakeSaveApi
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

from domain.playtime import Playtime
from domain.rom import Rom
from lib.errors import RommApiError
from services.playtime import PlaytimeService, PlaytimeServiceConfig


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


def make_service(fake_api=None, clock=None, settings=None, uow=None, **overrides):
    """Create a PlaytimeService with sensible defaults.

    Returns ``(svc, fake, uow)``. The device label stamped onto synced
    playtime notes is read from the live settings.json view (#822),
    reachable in tests as ``svc._settings``.
    """
    fake = fake_api or FakeSaveApi()
    unit = uow or FakeUnitOfWork()
    clk = clock or FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))
    settings_dict = settings if settings is not None else {}

    defaults: dict[str, Any] = {
        "romm_api": fake,
        "retry": _make_retry(),
        "settings": settings_dict,
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
        # FK enforcement aborts the clean-exit commit: the unit never commits.
        # (Real SQLite's failed COMMIT persists nothing; the fake leaves the
        # orphaned write in its in-memory store but flips no commit flag.)
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
        # Aggregate folded and persisted.
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

        # Session 1: 30s
        start1 = (clk.now() - timedelta(seconds=30)).isoformat()
        with uow:
            uow.playtime.save(42, Playtime(last_session_start=start1))
        await svc.record_session_end(42)

        # Session 2: 45s
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
# TestSyncPlaytime
# ---------------------------------------------------------------------------


class TestSyncPlaytime:
    @pytest.mark.asyncio
    async def test_creates_note_on_first_sync(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=120, session_count=1, last_session_duration_sec=120))
        svc._settings["device_name"] = "deck"

        svc._sync_playtime_to_romm_io(42, 120)

        assert any(c[0] == "create_note" for c in fake.call_log)
        notes = fake.notes.get(42, [])
        assert len(notes) == 1
        content = json.loads(notes[0]["content"])
        assert content["seconds"] >= 120
        assert content["device"] == "deck"
        # The created note id is linked back onto the aggregate.
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.note_id == notes[0]["id"]
        assert uow.committed is True

    @pytest.mark.asyncio
    async def test_updates_existing_note(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=200, session_count=2, last_session_duration_sec=80))
        svc._settings["device_name"] = "deck"
        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": json.dumps({"seconds": 100, "updated": "2026-01-01T00:00:00Z"}),
                "is_public": False,
            }
        ]

        svc._sync_playtime_to_romm_io(42, 80)

        assert any(c[0] == "update_note" for c in fake.call_log)
        assert not any(c[0] == "create_note" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_merge_takes_max_and_never_regresses(self):
        """new_total = max(local_total, server_seconds + session_duration); clamp never lowers."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=300, session_count=3, last_session_duration_sec=60))
        svc._settings["device_name"] = "deck"
        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": json.dumps({"seconds": 200}),
                "is_public": False,
            }
        ]

        svc._sync_playtime_to_romm_io(42, 60)

        # max(300, 200 + 60) = 300 — local total wins, reconcile ignores the smaller merge.
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 300

    @pytest.mark.asyncio
    async def test_server_ahead_raises_local_total(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=1))
        svc._settings["device_name"] = "deck"
        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": json.dumps({"seconds": 500}),
                "is_public": False,
            }
        ]

        svc._sync_playtime_to_romm_io(42, 60)

        # max(100, 500 + 60) = 560 — server baseline + session wins.
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 560

    @pytest.mark.asyncio
    async def test_missing_aggregate_is_noop(self):
        svc, fake, _ = make_service()  # nothing seeded

        svc._sync_playtime_to_romm_io(42, 60)

        assert fake.call_log == []


# ---------------------------------------------------------------------------
# TestGetPlaytime
# ---------------------------------------------------------------------------


class TestGetPlaytime:
    @pytest.mark.asyncio
    async def test_get_all_playtime_minimal_wire_shape(self):
        svc, _, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=2, note_id=9))
        _seed_playtime(uow, 99, Playtime(total_seconds=200, session_count=5))

        result = svc.get_all_playtime()

        assert set(result.keys()) == {"playtime"}
        assert result["playtime"]["42"] == {"total_seconds": 100, "session_count": 2}
        assert result["playtime"]["99"] == {"total_seconds": 200, "session_count": 5}
        # Minimal wire dict — note_id / last_session_* are NOT exposed.
        assert set(result["playtime"]["42"].keys()) == {"total_seconds", "session_count"}

    @pytest.mark.asyncio
    async def test_get_all_playtime_empty(self):
        svc, _, _ = make_service()

        result = svc.get_all_playtime()

        assert result == {"playtime": {}}


# ---------------------------------------------------------------------------
# TestPlaytimeNotes
# ---------------------------------------------------------------------------


class TestPlaytimeNotes:
    def test_get_playtime_note_finds_correct_title(self):
        svc, fake, _ = make_service()
        fake.notes[42] = [
            {"id": 1, "title": "other-note", "content": "{}"},
            {"id": 2, "title": "romm-sync:playtime", "content": '{"seconds": 50}'},
        ]
        note = svc._get_playtime_note(42)
        assert note is not None
        assert note["id"] == 2

    def test_get_playtime_note_missing(self):
        svc, _, _ = make_service()
        note = svc._get_playtime_note(42)
        assert note is None


# ---------------------------------------------------------------------------
# TestReconcilePlaytime
# ---------------------------------------------------------------------------


def _seed_playtime_note(fake: FakeSaveApi, rom_id: int, seconds: int, note_id: int = 2000) -> None:
    """Stage a server-side ``romm-sync:playtime`` note carrying ``seconds``."""
    fake.notes[rom_id] = [
        {
            "id": note_id,
            "rom_id": rom_id,
            "title": "romm-sync:playtime",
            "content": json.dumps({"seconds": seconds}),
            "is_public": False,
        }
    ]


class TestReconcilePlaytime:
    @pytest.mark.asyncio
    async def test_seeds_total_and_links_note_from_server(self):
        """Empty row + valid note → seed total to note seconds, link note id."""
        svc, fake, uow = make_service()
        _seed_rom(uow, 42)  # roms row exists, no playtime row
        _seed_playtime_note(fake, 42, 500, note_id=2000)

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 500
        assert result["server_query_failed"] is False
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 500
        assert entry.note_id == 2000

    @pytest.mark.asyncio
    async def test_server_ahead_raises_local_total(self):
        """Local < server → reconcile raises the local total to the server value."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=2))
        _seed_playtime_note(fake, 42, 500)

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 500
        assert result["session_count"] == 2  # untouched by a pull
        assert result["server_query_failed"] is False
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 500

    @pytest.mark.asyncio
    async def test_local_ahead_is_noop(self):
        """Local >= server → reconcile_total never regresses, total unchanged."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=900, session_count=4))
        _seed_playtime_note(fake, 42, 300)

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 900
        assert result["server_query_failed"] is False
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 900

    @pytest.mark.asyncio
    async def test_no_note_returns_local_without_creating_row(self):
        """No server note → return local total, do NOT seed an empty row."""
        svc, _, uow = make_service()
        _seed_rom(uow, 42)  # roms row, no playtime row, no note

        result = await svc.reconcile_playtime(42)

        assert result["total_seconds"] == 0
        assert result["session_count"] == 0
        assert result["server_query_failed"] is False
        # Pull-only with no note must not create a rom_playtime row.
        assert uow.playtime.get(42) is None

    @pytest.mark.asyncio
    async def test_malformed_note_does_not_seed(self):
        """Malformed note JSON → parse returns None → server_seconds 0, no raise."""
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=120, session_count=1))
        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": "not valid json",
                "is_public": False,
            }
        ]

        result = await svc.reconcile_playtime(42)

        # max(120, 0) = 120 — local total preserved, note id still linked.
        assert result["total_seconds"] == 120
        assert result["server_query_failed"] is False
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 120

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
        _seed_playtime_note(fake, 42, 500)

        result = await svc.reconcile_playtime(42)

        # The commit's FK check aborts (no raise out of the callable); the unit
        # never flips committed and the graceful no-op reports zero totals.
        assert result["server_query_failed"] is False
        assert result["total_seconds"] == 0
        assert result["session_count"] == 0
        assert uow.committed is False

    @pytest.mark.asyncio
    async def test_double_run_is_idempotent(self):
        """Running reconcile twice leaves the total and linked note id stable."""
        svc, fake, uow = make_service()
        _seed_rom(uow, 42)
        _seed_playtime_note(fake, 42, 500, note_id=2000)

        first = await svc.reconcile_playtime(42)
        second = await svc.reconcile_playtime(42)

        assert first["total_seconds"] == second["total_seconds"] == 500
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 500
        assert entry.note_id == 2000

    @pytest.mark.asyncio
    async def test_emits_outcome_debug_line(self):
        """Each reconcile logs one debug line naming the rom and its outcome."""
        logs: list[str] = []
        svc, fake, uow = make_service(log_debug=logs.append)
        _seed_rom(uow, 42)
        _seed_playtime_note(fake, 42, 500, note_id=2000)
        _seed_rom(uow, 99)  # roms row, no note

        await svc.reconcile_playtime(42)  # server note present
        await svc.reconcile_playtime(99)  # no server note

        assert any("rom 42" in m and "note_id=2000" in m and "total=500s" in m for m in logs)
        assert any("rom 99" in m and "no server note" in m for m in logs)


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_romm_push_suppressed_local_fold_still_committed(self):
        """RomM push fails — the durable session fold still committed, no note linked."""
        clk = FakeClock(now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
        svc, fake, uow = make_service(clock=clk)
        start = (clk.now() - timedelta(seconds=60)).isoformat()
        _seed_playtime(uow, 42, Playtime(last_session_start=start))
        svc._settings["device_name"] = "deck"
        fake.fail_on_next(RommApiError("Oops"))

        result = await svc.record_session_end(42)

        # The end result reflects the committed fold despite the push failure.
        assert result["success"] is True
        assert result["total_seconds"] == 60
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 60
        assert entry.session_count == 1
        assert entry.note_id is None  # push failed before any note was created

    @pytest.mark.asyncio
    async def test_sync_playtime_error_logged_not_raised(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=1, last_session_duration_sec=60))
        svc._settings["device_name"] = "deck"
        fake.fail_on_next(RommApiError("Oops"))

        # Should not raise; local total unchanged (no commit reached).
        svc._sync_playtime_to_romm_io(42, 60)

        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 100
        assert entry.note_id is None

    @pytest.mark.asyncio
    async def test_corrupted_note_content_falls_back_to_local_total(self):
        svc, fake, uow = make_service()
        _seed_playtime(uow, 42, Playtime(total_seconds=100, session_count=1, last_session_duration_sec=60))
        svc._settings["device_name"] = "deck"
        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": "not valid json",
                "is_public": False,
            }
        ]

        svc._sync_playtime_to_romm_io(42, 60)

        # Corrupted server content → server_seconds=0 → max(100, 0+60) = 100.
        calls = [c for c in fake.call_log if c[0] in ("update_note", "create_note")]
        assert len(calls) >= 1
        entry = uow.playtime.get(42)
        assert entry is not None
        assert entry.total_seconds == 100
