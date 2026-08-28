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
from tests.services.library._helpers import _fake_wait_set_event, _seed_platform, _seed_rom_row, _use_fake_romm


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


class TestWholeUnitStaging:
    """The three whole-unit staging dicts the reporter's commit reads.

    They are written here, once per unit, before the first chunk goes out — one
    production writer module from the moment they are staged to
    ``clear_active_unit``'s teardown. ``pending_sync`` and ``pending_all_roms``
    hold deliberately different sets and are the same shape, so the swap is
    silent everywhere except here: ``pending_sync`` is the DELTA the frontend
    applies, ``pending_all_roms`` the FULL built set every sibling's identity row
    is upserted from.
    """

    @pytest.mark.asyncio
    async def test_delta_and_full_set_are_staged_into_their_own_fields(self, plugin, fake_romm_api):
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        # rom 10 is content-unchanged (skipped from the delta); rom 11 changed its
        # name, so the delta is {11} while the built set is {10, 11}.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 10, "name": "Keep", "fs_name": "keep.z64"},
                {"id": 11, "name": "New Name", "fs_name": "changed.z64", "path_cover_large": "cover-11.png"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="Old Name", fs_name="changed.z64")
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={11: "/covers/11.png"})
        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = _fake_wait_set_event
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-staging"

        # Snapshot the staging as the commit sees it — ``clear_active_unit`` wipes
        # all three the moment the unit finishes.
        staged: list[tuple[set[int], set[int], dict[int, str]]] = []
        commit = plugin._sync_service._reporter.commit_unit_results

        async def capture_commit(*args, **kwargs):
            staged.append((set(box.pending_sync), set(box.pending_all_roms), dict(box.pending_cover_sources)))
            return await commit(*args, **kwargs)

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        assert len(staged) == 1
        pending_sync, pending_all_roms, cover_sources = staged[0]
        assert pending_sync == {11}, "pending_sync is the DELTA the frontend applies"
        assert pending_all_roms == {10, 11}, "pending_all_roms is the FULL built set, skipped siblings included"
        assert cover_sources == {11: "cover-11.png"}
        # Teardown clears all three, so a late ack after the unit finished stages nothing.
        assert box.pending_sync == {}
        assert box.pending_all_roms == {}
        assert box.pending_cover_sources == {}


class TestFinalChunkCollectionStamp:
    """A standard/smart collection is stamped on its FINAL chunk only (#742).

    Driven through ``apply_unit_in_chunks`` directly rather than the whole
    pipeline: the condition reads only the unit and its position in the chunk
    sequence, so a fetch and a collapse in front of it would add setup without
    adding evidence. The stamp's atomicity with the chunk's row upserts is the
    reporter's, and is pinned in ``tests/services/library/test_reporter.py``.
    """

    @pytest.mark.asyncio
    async def test_stamp_rides_only_the_last_chunk(self, plugin, monkeypatch):
        from services.library import chunk_dispatcher

        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 1)
        emitted = [
            {"rom_id": 1, "sibling_group_key": None},
            {"rom_id": 2, "sibling_group_key": None},
        ]

        stamps = []

        async def capture_commit(_rid_to_aid, _chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            stamps.append(collection_stamp)

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = _fake_wait_set_event
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-collection-stamp"

        unit = WorkUnit(
            type="collection",
            id="7",
            name="Faves",
            slug="faves",
            rom_count=2,
            collection_kind="standard",
            collection_updated_at="2025-01-01T00:00:00",
        )
        await plugin._sync_service._chunk_dispatcher.apply_unit_in_chunks(
            unit,
            unit_index=0,
            total_units=1,
            emitted=emitted,
            shortcuts_data=list(emitted),
            unit_roms=[{"id": 1}, {"id": 2}],
            new_ids=set(),
            confirmed_cover_sources={},
            collection_member_ids=[1, 2],
        )

        assert len(stamps) == 2, "one commit per chunk"
        assert stamps[0] is None, "an incomplete collection is never stamped"
        assert stamps[1] is not None
        assert stamps[1].collection_id == "7"
        assert stamps[1].member_rom_ids == (1, 2), "the FULL membership, not the chunk's slice"

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_before_the_last_chunk_leaves_no_stamp(self, plugin, monkeypatch):
        """The wait giving up returns before the final chunk, so nothing is stamped."""
        from services.library import chunk_dispatcher

        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 1)
        emitted = [
            {"rom_id": 1, "sibling_group_key": None},
            {"rom_id": 2, "sibling_group_key": None},
        ]

        stamps = []
        box = plugin._sync_service._box

        async def capture_commit(_rid_to_aid, _chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            stamps.append(collection_stamp)

        async def wait(_unit, event):
            if box.active_chunk_index == 0:
                event.set()
                return {}
            return None  # heartbeat timeout on the final chunk

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = wait
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-collection-timeout"

        unit = WorkUnit(
            type="collection",
            id="7",
            name="Faves",
            slug="faves",
            rom_count=2,
            collection_kind="standard",
            collection_updated_at="2025-01-01T00:00:00",
        )
        await plugin._sync_service._chunk_dispatcher.apply_unit_in_chunks(
            unit,
            unit_index=0,
            total_units=1,
            emitted=emitted,
            shortcuts_data=list(emitted),
            unit_roms=[{"id": 1}, {"id": 2}],
            new_ids=set(),
            confirmed_cover_sources={},
            collection_member_ids=[1, 2],
        )

        assert stamps == [None], "only the first chunk committed, and it carries no stamp"
        assert box.abandoned_chunk is not None


class TestAckIdentityPrecedesTheEmit:
    """The per-chunk ack identity is stamped BEFORE the frame goes out (#1041).

    The reporter validates a frontend ack against ``current_sync_id`` /
    ``active_unit_id`` / ``active_chunk_index`` and signals the wait through
    ``unit_complete_event``. A frontend can ack within milliseconds of receiving
    the frame, so all four have to be in place at the moment the emit is made:
    stamp them after and a fast ack is rejected as stray, the event is never set,
    and the chunk dies of a heartbeat timeout a silent minute later with the
    shortcuts already applied.

    Nothing else in the suite sees that ordering — every other test lets the emit
    mock swallow the call — so this one wraps the dispatcher's own emitter and
    records the box AT CALL TIME.
    """

    @pytest.mark.asyncio
    async def test_identity_and_event_are_live_at_emit_time(self, plugin, monkeypatch):
        from services.library import chunk_dispatcher

        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 1)
        dispatcher = plugin._sync_service._chunk_dispatcher
        box = plugin._sync_service._box
        emitted = [
            {"rom_id": 1, "sibling_group_key": None},
            {"rom_id": 2, "sibling_group_key": None},
        ]

        # What the reporter would see if an ack landed while the emit is in flight.
        seen: list[tuple[str | None, int | str | None, int | None, bool]] = []
        inner_emit = dispatcher._emit

        async def recording_emit(event, payload):
            seen.append(
                (
                    box.current_sync_id,
                    box.active_unit_id,
                    box.active_chunk_index,
                    box.unit_complete_event is not None,
                )
            )
            return await inner_emit(event, payload)

        dispatcher._emit = recording_emit
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        dispatcher._wait_for_unit_complete = _fake_wait_set_event
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-ack-identity"

        unit = WorkUnit(type="platform", id=7, name="N64", slug="n64", rom_count=2)
        await dispatcher.apply_unit_in_chunks(
            unit,
            unit_index=0,
            total_units=1,
            emitted=emitted,
            shortcuts_data=list(emitted),
            unit_roms=[{"id": 1}, {"id": 2}],
            new_ids=set(),
            confirmed_cover_sources={},
        )

        assert seen == [
            ("run-ack-identity", 7, 0, True),
            ("run-ack-identity", 7, 1, True),
        ], "each chunk's ack identity and its event must already be live when its frame is emitted"


class TestInterChunkCancelGuard:
    """A cancel at a chunk boundary stays a cancel, not a budget pause.

    The guard at the top of the loop runs BEFORE the session-budget gate, and
    that ordering is what the run's terminal status depends on: the gate sets
    ``run_paused`` + ``interrupt_reason`` before requesting its own cancel, so a
    user cancel landing in the inter-chunk window while renderer RSS sits near
    the ceiling would be recorded as a resumable ``paused`` run — the wrong
    story, and a resumable one at that. Read through the terminal ``SyncRun``
    because that is where the confusion would surface.
    """

    @pytest.mark.asyncio
    async def test_cancel_near_the_ceiling_is_recorded_as_cancelled(self, plugin, fake_romm_api, monkeypatch):
        import decky

        from services.library import chunk_dispatcher

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Alpha"}, {"id": 11, "name": "Beta"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 1)
        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = _fake_wait_set_event

        # Just under the ceiling: the run's first chunk projects against the cliff
        # and proceeds, a second chunk would project against the ceiling and pause.
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 2_199_000

        box = plugin._sync_service._box
        commit = plugin._sync_service._reporter.commit_unit_results
        commits = 0

        async def cancel_after_first_commit(*args, **kwargs):
            nonlocal commits
            result = await commit(*args, **kwargs)
            commits += 1
            if commits == 1:
                # The user's Cancel lands the instant chunk 0's commit resolves —
                # before the loop returns to the top for chunk 1.
                box.request_cancel()
            return result

        plugin._sync_service._reporter.commit_unit_results = cancel_after_first_commit  # type: ignore[method-assign]
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-cancel-near-ceiling"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-cancel-near-ceiling")
        assert run is not None
        assert run.status == "cancelled", "a user cancel must not be recorded as a resumable budget pause"
        assert box.run_paused is False
        assert box.interrupt_reason is None
        # The guard returned at the top of the loop, so chunk 1 was never emitted
        # and the gate it would have passed through never ran for it.
        apply_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(apply_events) == 1
        assert apply_events[0]["chunk_index"] == 0
