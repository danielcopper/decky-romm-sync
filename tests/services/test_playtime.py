"""Tests for PlaytimeService with FakeSaveApi."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import _make_retry
from fakes.fake_save_api import FakeSaveApi
from fakes.system_time import FakeClock

from domain.save_state import PlaytimeEntry, SaveSyncState
from lib.errors import RommApiError
from services.playtime import PlaytimeService, PlaytimeServiceConfig


def make_service(tmp_path=None, fake_api=None, clock=None, **overrides):
    """Create a PlaytimeService with sensible defaults."""
    fake = fake_api or FakeSaveApi()
    state = SaveSyncState()
    saved: list[bool] = []
    clk = clock or FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))

    defaults: dict[str, Any] = dict(
        romm_api=fake,
        retry=_make_retry(),
        save_sync_state=state,
        loop=asyncio.get_event_loop(),
        logger=logging.getLogger("test"),
        clock=clk,
        save_state=lambda: saved.append(True),
        log_debug=lambda _msg: None,
    )
    defaults.update(overrides)
    svc = PlaytimeService(config=PlaytimeServiceConfig(**defaults))
    return svc, fake, state, saved


# ---------------------------------------------------------------------------
# TestRecordSession
# ---------------------------------------------------------------------------


class TestRecordSession:
    @pytest.mark.asyncio
    async def test_start_creates_entry(self):
        svc, _, state, saved = make_service()
        result = svc.record_session_start(42)
        assert result["success"] is True
        assert "42" in state.playtime
        assert state.playtime["42"].last_session_start is not None
        assert len(saved) == 1

    @pytest.mark.asyncio
    async def test_end_records_duration(self):
        svc, _, state, _ = make_service()

        # Start session
        svc.record_session_start(42)

        # Backdate session start by 60 seconds (relative to the fake clock)
        start = svc._clock.now() - timedelta(seconds=60)
        state.playtime["42"].last_session_start = start.isoformat()

        result = await svc.record_session_end(42)
        assert result["success"] is True
        assert result["duration_sec"] >= 59  # allow 1s tolerance
        assert result["session_count"] == 1
        assert result["total_seconds"] >= 59

    @pytest.mark.asyncio
    async def test_end_without_start(self):
        svc, _, _, _ = make_service()
        result = await svc.record_session_end(42)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_end_with_unparseable_start(self):
        """Malformed last_session_start -> parse_iso returns None -> failure."""
        svc, _, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(last_session_start="not-a-date")
        result = await svc.record_session_end(42)
        assert result["success"] is False
        assert "Failed to calculate session duration" in result["message"]

    @pytest.mark.asyncio
    async def test_multiple_sessions_accumulate(self):
        svc, _, state, _ = make_service()

        # Session 1
        svc.record_session_start(42)
        start = svc._clock.now() - timedelta(seconds=30)
        state.playtime["42"].last_session_start = start.isoformat()
        await svc.record_session_end(42)

        # Session 2
        svc.record_session_start(42)
        start = svc._clock.now() - timedelta(seconds=45)
        state.playtime["42"].last_session_start = start.isoformat()
        result2 = await svc.record_session_end(42)

        assert result2["session_count"] == 2
        assert result2["total_seconds"] >= 74  # ~30 + ~45


# ---------------------------------------------------------------------------
# TestSyncPlaytime
# ---------------------------------------------------------------------------


class TestSyncPlaytime:
    @pytest.mark.asyncio
    async def test_creates_note_on_first_sync(self):
        svc, fake, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(
            total_seconds=120,
            session_count=1,
            last_session_duration_sec=120,
        )
        state.device_name = "deck"

        svc._sync_playtime_to_romm(42, 120)

        assert any(c[0] == "create_note" for c in fake.call_log)
        # Verify note content
        notes = fake.notes.get(42, [])
        assert len(notes) == 1
        content = json.loads(notes[0]["content"])
        assert content["seconds"] >= 120

    @pytest.mark.asyncio
    async def test_updates_existing_note(self):
        svc, fake, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(
            total_seconds=200,
            session_count=2,
            last_session_duration_sec=80,
        )
        state.device_name = "deck"

        # Pre-existing note on server
        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": json.dumps({"seconds": 100, "updated": "2026-01-01T00:00:00Z"}),
                "is_public": False,
            }
        ]

        svc._sync_playtime_to_romm(42, 80)

        assert any(c[0] == "update_note" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_merge_takes_max(self):
        """new_total = max(local_total, server_seconds + session_duration)"""
        svc, fake, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(
            total_seconds=300,
            session_count=3,
            last_session_duration_sec=60,
        )
        state.device_name = "deck"

        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": json.dumps({"seconds": 200}),
                "is_public": False,
            }
        ]

        svc._sync_playtime_to_romm(42, 60)

        # max(300, 200+60) = 300
        entry = state.playtime["42"]
        assert entry.total_seconds == 300


# ---------------------------------------------------------------------------
# TestGetPlaytime
# ---------------------------------------------------------------------------


class TestGetPlaytime:
    @pytest.mark.asyncio
    async def test_get_server_playtime(self):
        svc, fake, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(total_seconds=100, session_count=2)

        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": json.dumps({"seconds": 200}),
                "is_public": False,
            }
        ]

        result = await svc.get_server_playtime(42)
        assert result["local_seconds"] == 100
        assert result["server_seconds"] == 200
        assert result["total_seconds"] == 200  # max(100, 200)

    @pytest.mark.asyncio
    async def test_get_server_playtime_no_note(self):
        svc, _, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(total_seconds=50, session_count=1)

        result = await svc.get_server_playtime(42)
        assert result["local_seconds"] == 50
        assert result["server_seconds"] == 0
        assert result["total_seconds"] == 50

    @pytest.mark.asyncio
    async def test_get_all_playtime(self):
        svc, _, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(total_seconds=100)
        state.playtime["99"] = PlaytimeEntry(total_seconds=200)

        result = svc.get_all_playtime()
        assert len(result["playtime"]) == 2
        assert result["playtime"]["42"]["total_seconds"] == 100


# ---------------------------------------------------------------------------
# TestPlaytimeNotes
# ---------------------------------------------------------------------------


class TestPlaytimeNotes:
    def test_parse_valid_content(self):
        data = PlaytimeService._parse_playtime_note_content('{"seconds": 100}')
        assert data == {"seconds": 100}

    def test_parse_empty(self):
        assert PlaytimeService._parse_playtime_note_content("") is None

    def test_parse_invalid_json(self):
        assert PlaytimeService._parse_playtime_note_content("not json") is None

    def test_parse_non_dict(self):
        assert PlaytimeService._parse_playtime_note_content("[1,2,3]") is None

    def test_get_playtime_note_finds_correct_title(self):
        svc, fake, _, _ = make_service()
        fake.notes[42] = [
            {"id": 1, "title": "other-note", "content": "{}"},
            {"id": 2, "title": "romm-sync:playtime", "content": '{"seconds": 50}'},
        ]
        note = svc._get_playtime_note(42)
        assert note is not None
        assert note["id"] == 2

    def test_get_playtime_note_missing(self):
        svc, _, _, _ = make_service()
        note = svc._get_playtime_note(42)
        assert note is None


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_api_error_on_server_playtime(self):
        svc, fake, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(total_seconds=100, session_count=1)
        fake.fail_on_next(RommApiError("Server down"))

        result = await svc.get_server_playtime(42)
        # Should still return local data
        assert result["local_seconds"] == 100
        assert result["server_seconds"] == 0

    @pytest.mark.asyncio
    async def test_sync_playtime_error_logged_not_raised(self):
        svc, fake, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(
            total_seconds=100,
            session_count=1,
            last_session_duration_sec=60,
        )
        state.device_name = "deck"
        fake.fail_on_next(RommApiError("Oops"))

        # Should not raise
        svc._sync_playtime_to_romm(42, 60)

    @pytest.mark.asyncio
    async def test_corrupted_note_content(self):
        svc, fake, state, _ = make_service()
        state.playtime["42"] = PlaytimeEntry(
            total_seconds=100,
            session_count=1,
            last_session_duration_sec=60,
        )
        state.device_name = "deck"

        fake.notes[42] = [
            {
                "id": 2000,
                "rom_id": 42,
                "title": "romm-sync:playtime",
                "content": "not valid json",
                "is_public": False,
            }
        ]

        # Should handle gracefully — create/update with local total
        svc._sync_playtime_to_romm(42, 60)
        # Should have updated or created a note
        calls = [c for c in fake.call_log if c[0] in ("update_note", "create_note")]
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_session_clamps_to_24h(self):
        svc, _, state, _ = make_service()
        svc.record_session_start(42)

        # Backdate by 25 hours
        start = svc._clock.now() - timedelta(hours=25)
        state.playtime["42"].last_session_start = start.isoformat()

        result = await svc.record_session_end(42)
        assert result["success"] is True
        assert result["duration_sec"] <= 86400
