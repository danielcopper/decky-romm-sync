"""Tests for the ``LibrarySyncStateBox`` run-lifecycle verb methods.

The box's four verbs (``try_begin_run`` / ``request_cancel`` / ``finish_run`` /
``is_in_flight``) are the only writers of the run-lifecycle pair
(``sync_state`` / ``current_sync_id``). These tests pin the compare-and-swap
admission, the run-scoped cancel routing, and the compare-and-reset terminal so
a rapid Sync/Cancel can't leave a half-reset run id (#1202).
"""

from __future__ import annotations

import asyncio

from domain.sync_state import SyncState
from services.library._state import AbandonedChunk, LibrarySyncStateBox


class TestTryBeginRun:
    def test_claims_slot_from_idle(self):
        box = LibrarySyncStateBox()
        assert box.try_begin_run("run-1") is True
        assert box.sync_state is SyncState.RUNNING
        assert box.current_sync_id == "run-1"

    def test_rejected_when_running_leaves_state_unchanged(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        assert box.try_begin_run("run-2") is False
        # The incumbent run still owns the slot — no clobber.
        assert box.sync_state is SyncState.RUNNING
        assert box.current_sync_id == "run-1"

    def test_rejected_when_cancelling(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        box.request_cancel()
        assert box.try_begin_run("run-2") is False
        assert box.current_sync_id == "run-1"


class TestRequestCancel:
    def test_no_sync_when_idle(self):
        box = LibrarySyncStateBox()
        assert box.request_cancel("run-1") == "no_sync"
        assert box.sync_state is SyncState.IDLE

    def test_stale_when_run_id_mismatch(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        assert box.request_cancel("run-2") == "stale"
        # A stale cancel must NOT flip the active run.
        assert box.sync_state is SyncState.RUNNING

    def test_cancelling_when_run_id_matches(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        assert box.request_cancel("run-1") == "cancelling"
        assert box.sync_state is SyncState.CANCELLING

    def test_falsy_run_id_cancels_unconditionally(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        assert box.request_cancel() == "cancelling"
        assert box.sync_state is SyncState.CANCELLING

    def test_empty_string_run_id_cancels_unconditionally(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        assert box.request_cancel("") == "cancelling"
        assert box.sync_state is SyncState.CANCELLING


class TestFinishRun:
    def test_owner_resets_to_idle(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        assert box.finish_run("run-1") is True
        assert box.sync_state is SyncState.IDLE
        assert box.current_sync_id is None

    def test_owner_resets_from_cancelling(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        box.request_cancel("run-1")
        assert box.finish_run("run-1") is True
        assert box.sync_state is SyncState.IDLE
        assert box.current_sync_id is None

    def test_foreign_run_id_is_noop(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        # A late terminal from a different run must not null the active run.
        assert box.finish_run("run-OLD") is False
        assert box.sync_state is SyncState.RUNNING
        assert box.current_sync_id == "run-1"

    def test_repeat_finish_run_is_safe(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        assert box.finish_run("run-1") is True
        # A doubled terminal for the same run is a no-op once the slot is freed.
        assert box.finish_run("run-1") is False
        assert box.sync_state is SyncState.IDLE
        assert box.current_sync_id is None

    def test_finish_run_does_not_null_a_fresh_run(self):
        """The #1202 race: run A's late terminal lands after run B started."""
        box = LibrarySyncStateBox()
        box.try_begin_run("run-A")
        box.finish_run("run-A")
        box.try_begin_run("run-B")
        # Run A's doubled/late terminal must leave run B intact.
        assert box.finish_run("run-A") is False
        assert box.sync_state is SyncState.RUNNING
        assert box.current_sync_id == "run-B"

    def test_try_begin_run_clears_a_prior_abandoned_chunk_stash(self):
        """A heartbeat-timed-out chunk whose late ack never arrived is dropped
        when the next run starts — bounded stash lifetime (#1367)."""
        box = LibrarySyncStateBox()
        box.try_begin_run("run-A")
        box.active_unit_id = 1
        box.active_chunk_index = 0
        box.stash_abandoned_chunk([{"id": 1}])
        box.finish_run("run-A")
        assert box.abandoned_chunk is not None

        assert box.try_begin_run("run-B") is True
        # The stale abandon never survives into the next run.
        assert box.abandoned_chunk is None

    def test_finish_run_does_not_clear_the_abandoned_chunk_stash(self):
        """Run teardown must NOT drop the stash — the whole point is that a late
        ack arriving AFTER the run wound down can still recover it (#1367)."""
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        box.active_unit_id = 1
        box.active_chunk_index = 0
        box.stash_abandoned_chunk([{"id": 1}])

        box.finish_run("run-1")

        assert box.abandoned_chunk is not None
        assert box.current_sync_id is None


class TestChunkCoordination:
    def test_active_chunk_index_defaults_to_none(self):
        """A fresh box has no in-flight apply chunk — the reporter's chunk guard
        treats ``None`` as 'no active chunk' and rejects any ack (#1025)."""
        box = LibrarySyncStateBox()
        assert box.active_chunk_index is None

    def test_clear_active_unit_resets_the_dispatch_state_and_keeps_the_stash(self):
        """clear_active_unit tears down the active-chunk dispatch state and
        nothing else — ``last_unit_results``, the run-interrupted flag, the
        run-lifecycle pair, and an ``abandoned_chunk`` stash all survive so the
        late-ack path can still commit the delivered bindings (#1052 / #1367)."""
        box = LibrarySyncStateBox()
        # The dispatch state clear_active_unit owns …
        box.pending_sync = {1: {"id": 1}}
        box.pending_all_roms = {1: {"id": 1}, 2: {"id": 2}}
        box.pending_cover_sources = {1: "romm:cover:1"}
        box.unit_complete_event = asyncio.Event()
        box.active_unit_id = 7
        box.active_chunk_index = 3
        # … and every field it must leave untouched.
        box.abandoned_chunk = AbandonedChunk(run_id="run-1", unit_id=1, chunk_index=0, chunk_rows=[{"id": 2}])
        box.last_unit_results = {"1": 1001}
        box.run_interrupted = True
        box.committed_app_ids = {1001}
        box.current_sync_id = "run-1"
        box.sync_state = SyncState.CANCELLING

        box.clear_active_unit()

        # Dispatch state reset.
        assert box.pending_sync == {}
        assert box.pending_all_roms == {}
        assert box.pending_cover_sources == {}
        assert box.unit_complete_event is None
        assert box.active_unit_id is None
        assert box.active_chunk_index is None
        # Everything else survives.
        assert box.abandoned_chunk is not None
        assert box.abandoned_chunk.chunk_rows == [{"id": 2}]
        assert box.last_unit_results == {"1": 1001}
        assert box.run_interrupted is True
        assert box.committed_app_ids == {1001}
        assert box.current_sync_id == "run-1"
        assert box.sync_state is SyncState.CANCELLING


class TestAbandonedChunkStash:
    """The heartbeat-timeout recovery seam: stash on timeout, pop on the
    identity-matched late ack, ignore everything else (#1367)."""

    def _armed_box(self) -> LibrarySyncStateBox:
        """A box mid-apply on run-1 / unit 5 / chunk 2, ready to be abandoned."""
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        box.active_unit_id = 5
        box.active_chunk_index = 2
        box.unit_complete_event = asyncio.Event()
        box.pending_sync = {42: {"id": 42}}
        return box

    def test_stash_captures_identity_and_rows_and_clears_dispatch_identity(self):
        box = self._armed_box()

        box.stash_abandoned_chunk([{"id": 42}])

        stash = box.abandoned_chunk
        assert stash is not None
        assert (stash.run_id, stash.unit_id, stash.chunk_index) == ("run-1", 5, 2)
        assert stash.chunk_rows == [{"id": 42}]
        # The dispatch identity is cleared so a live-ack check can no longer match…
        assert box.unit_complete_event is None
        assert box.active_unit_id is None
        assert box.active_chunk_index is None
        # …but the whole-unit staging stays live for the late-ack commit to read.
        assert box.pending_sync == {42: {"id": 42}}

    def test_take_returns_and_clears_on_identity_match(self):
        box = self._armed_box()
        box.stash_abandoned_chunk([{"id": 42}])

        stash = box.take_abandoned_chunk("run-1", 5, 2)

        assert stash is not None
        assert stash.chunk_rows == [{"id": 42}]
        # Popped: a duplicate late ack finds nothing.
        assert box.abandoned_chunk is None
        assert box.take_abandoned_chunk("run-1", 5, 2) is None

    def test_take_coerces_unit_id_across_int_str_drift(self):
        """A platform unit id arrives as an int on the wire but the stash may hold
        either — the match is by string value, like the active-unit ack check."""
        box = self._armed_box()
        box.stash_abandoned_chunk([{"id": 42}])

        # Frontend echoes the unit id back as a string "5".
        assert box.take_abandoned_chunk("run-1", "5", 2) is not None

    def test_take_leaves_stash_intact_on_wrong_identity(self):
        box = self._armed_box()
        box.stash_abandoned_chunk([{"id": 42}])

        assert box.take_abandoned_chunk("run-1", 5, 999) is None  # wrong chunk
        assert box.take_abandoned_chunk("run-OTHER", 5, 2) is None  # wrong run
        assert box.take_abandoned_chunk("run-1", 99, 2) is None  # wrong unit
        # None of the misses consumed the stash.
        assert box.abandoned_chunk is not None

    def test_take_returns_none_when_no_stash(self):
        box = LibrarySyncStateBox()
        assert box.take_abandoned_chunk("run-1", 5, 2) is None


class TestIsInFlight:
    def test_idle_not_in_flight(self):
        box = LibrarySyncStateBox()
        assert box.is_in_flight() is False

    def test_running_in_flight(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        assert box.is_in_flight() is True

    def test_cancelling_in_flight(self):
        box = LibrarySyncStateBox()
        box.try_begin_run("run-1")
        box.request_cancel("run-1")
        assert box.is_in_flight() is True
        assert box.is_cancelling() is True
