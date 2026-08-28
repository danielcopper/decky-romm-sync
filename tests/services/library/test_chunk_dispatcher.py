"""Tests for ChunkDispatcher — the per-unit emit → ack → commit round-trip.

The dispatcher is reached through the library façade
(``plugin._sync_service._chunk_dispatcher``) so every test drives the same
instance the orchestrator holds, over the shared state box the chunk
coordination lands on.

Two levels are covered here. The chunk **loop** is driven end-to-end through
``SyncOrchestrator._sync_one_unit`` against the seeded ``FakeRommApi`` — the
loop's inputs are a whole unit's fetch, collapse and delta, so entering below
that would mean hand-building them and asserting against a shape rather than
against what the pipeline produces. What each test then observes is the
dispatcher's own output: the ``sync_apply_unit`` frames, the per-chunk commits,
and what the box holds after a cancel or a timeout. The heartbeat **wait** is
called directly, because its whole behaviour is a function of the clock and the
box.

``_wait_for_unit_complete`` stands in for a frontend ``report_unit_results``
callback no test exercises; ``_download_artwork`` stands in for the SteamGridDB
pipeline. The exact chunk partition maths is pinned in
``tests/domain/test_sync_chunking.py``.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from domain.sync_state import SyncState
from domain.work_unit import WorkUnit

# conftest.py patches decky before this import
from tests.services.library._helpers import _fake_wait_set_event, _seed_platform, _use_fake_romm


class TestApplyChunking:
    """A unit's apply is split into durable commit chunks (#1025).

    Each chunk is emitted → acked → committed on its own, so a mid-unit CEF
    crash forfeits only the in-flight chunk. These tests drive ``_sync_one_unit``
    directly with a shrunk ``_APPLY_CHUNK_SIZE`` so a handful of singleton ROMs
    exercises the multi-chunk loop; the exact partition maths is pinned in
    ``tests/domain/test_sync_chunking.py``.
    """

    @pytest.mark.asyncio
    async def test_large_unit_emits_one_event_and_commit_per_chunk(self, plugin, fake_romm_api, monkeypatch):
        """Five singletons at chunk size 2 → three ``sync_apply_unit`` events with
        continuous unit-wide chunk fields, and one commit per chunk carrying only
        that chunk's rows."""
        import decky

        from services.library import chunk_dispatcher

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"Game {i}"} for i in range(1, 6)],
        )

        commit_rows: list[list[int]] = []

        async def capture_commit(_rid_to_aid, chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            commit_rows.append([r["id"] for r in chunk_rows])

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-chunk"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 3
        assert [e["chunk_index"] for e in unit_events] == [0, 1, 2]
        assert all(e["chunk_count"] == 3 for e in unit_events)
        assert [e["chunk_offset"] for e in unit_events] == [0, 2, 4]
        assert all(e["unit_total"] == 5 for e in unit_events)
        assert [len(e["shortcuts"]) for e in unit_events] == [2, 2, 1]
        # One commit per chunk, each with only its chunk's rows.
        assert commit_rows == [[1, 2], [3, 4], [5]]

    @pytest.mark.asyncio
    async def test_small_unit_emits_exactly_one_chunk(self, plugin, fake_romm_api):
        """A unit under the chunk size emits a single chunk — regression guard that
        the chunk fields collapse to the today's one-shot behaviour."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 4)],
        )

        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-single"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        event = unit_events[0]
        assert event["chunk_index"] == 0
        assert event["chunk_count"] == 1
        assert event["chunk_offset"] == 0
        assert event["unit_total"] == 3
        assert len(event["shortcuts"]) == 3

    @pytest.mark.asyncio
    async def test_user_cancel_between_chunks_keeps_committed_chunks(self, plugin, fake_romm_api, monkeypatch):
        """A user cancel during chunk 1's wait discards the rest but leaves chunk 0
        committed — the whole point of chunking (#1025)."""
        from services.library import chunk_dispatcher

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        commit_rows: list[list[int]] = []

        async def capture_commit(_rid_to_aid, chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            commit_rows.append([r["id"] for r in chunk_rows])

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        box = plugin._sync_service._box

        async def wait(_unit, event):
            if box.active_chunk_index == 0:
                event.set()
                return {}
            box.sync_state = SyncState.CANCELLING  # user cancel during chunk 1
            return None

        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = wait
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-cancel-chunk"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        # Only chunk 0 committed; the cancel discarded chunk 1 onward.
        assert commit_rows == [[1, 2]]
        # Staging + chunk identity cleared so a stray late ack can't commit.
        assert box.pending_sync == {}
        assert box.unit_complete_event is None
        assert box.active_chunk_index is None
        assert box.abandoned_chunk is None

    @pytest.mark.asyncio
    async def test_cancel_in_inter_chunk_window_never_emits_next_chunk(self, plugin, fake_romm_api, monkeypatch):
        """A cancel landing AFTER chunk 0's commit but BEFORE chunk 1's emit stops
        the unit at the top of the loop: chunk 1 is never emitted, chunk 0's commit
        persists, staging cleared. Complements
        ``test_user_cancel_between_chunks_keeps_committed_chunks`` (cancel DURING
        the wait) — this is the inter-chunk window, where an un-guarded loop would
        still emit chunk 1 and leave ~200 shortcuts orphaned until the next sync
        (#1025)."""
        import decky

        from services.library import chunk_dispatcher

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        commit_rows: list[list[int]] = []
        box = plugin._sync_service._box

        async def capture_commit(_rid_to_aid, chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            commit_rows.append([r["id"] for r in chunk_rows])
            # Cancel lands the instant chunk 0's commit resolves — before the loop
            # returns to the top to emit chunk 1.
            if len(commit_rows) == 1:
                box.sync_state = SyncState.CANCELLING

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = _fake_wait_set_event
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-inter-chunk"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        # Chunk 1 is never emitted — the loop stopped at its top before any emit —
        # so the frontend has no orphaned chunk to churn and later fail the ack on.
        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        assert unit_events[0]["chunk_index"] == 0
        # Chunk 0's commit persists.
        assert commit_rows == [[1, 2]]
        # Staging + chunk identity cleared so a stray late ack can't commit.
        assert box.pending_sync == {}
        assert box.pending_all_roms == {}
        assert box.unit_complete_event is None
        assert box.active_unit_id is None
        assert box.active_chunk_index is None
        assert box.abandoned_chunk is None

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_on_chunk_stashes_only_that_chunk(self, plugin, fake_romm_api, monkeypatch):
        """A heartbeat timeout on chunk 1 stashes ONLY chunk 1's rows (not the whole
        unit) under chunk 1's identity, so a late ack commits just that chunk."""
        from services.library import chunk_dispatcher

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        box = plugin._sync_service._box

        async def wait(_unit, event):
            if box.active_chunk_index == 0:
                event.set()
                return {}
            return None  # heartbeat timeout on chunk 1 (no cancel)

        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = wait
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-timeout-chunk"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        assert box.abandoned_chunk is not None
        # The dispatch identity is cleared; the chunk index lives on the stash.
        assert box.active_chunk_index is None
        assert box.abandoned_chunk.chunk_index == 1
        # Only chunk 1's rows are stashed for the late ack, not the whole unit.
        assert [r["id"] for r in box.abandoned_chunk.chunk_rows] == [3, 4]
        # The timeout requested cancel so the outer loop stops.
        assert box.sync_state == SyncState.CANCELLING


class TestWaitForUnitComplete:
    """Heartbeat-based per-unit timeout."""

    @pytest.mark.asyncio
    async def test_returns_results_when_event_set(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        event.set()
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._chunk_dispatcher._clock.monotonic()
        plugin._sync_service._box.last_unit_results = {"10": 9000}

        results = await plugin._sync_service._chunk_dispatcher._wait_for_unit_complete(unit, event)
        assert results == {"10": 9000}

    @pytest.mark.asyncio
    async def test_returns_none_on_cancel(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._box.sync_state = SyncState.CANCELLING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._chunk_dispatcher._clock.monotonic()

        results = await plugin._sync_service._chunk_dispatcher._wait_for_unit_complete(unit, event)
        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_on_heartbeat_timeout(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        # Heartbeat is way too old — should timeout immediately on first loop check
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._chunk_dispatcher._clock.monotonic() - 999.0

        results = await plugin._sync_service._chunk_dispatcher._wait_for_unit_complete(unit, event)
        assert results is None


class TestWaitForUnitCompleteCancelled:
    """Tests for asyncio.CancelledError in _wait_for_unit_complete."""

    @pytest.mark.asyncio
    async def test_cancelled_error_during_sleep_is_logged_and_reraised(self, plugin):
        """If the inner sleep is cancelled, log + re-raise so the outer loop sees the cancel."""

        class _CancellingSleeper:
            async def sleep(self, _seconds: float) -> None:
                raise asyncio.CancelledError()

        plugin._sync_service._chunk_dispatcher._sleeper = _CancellingSleeper()
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._chunk_dispatcher._clock.monotonic()

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()  # never set — wait will enter the sleep path

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._chunk_dispatcher._wait_for_unit_complete(unit, event)
