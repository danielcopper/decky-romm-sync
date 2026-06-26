"""Tests for the ``LibrarySyncStateBox`` run-lifecycle verb methods.

The box's four verbs (``try_begin_run`` / ``request_cancel`` / ``finish_run`` /
``is_in_flight``) are the only writers of the run-lifecycle pair
(``sync_state`` / ``current_sync_id``). These tests pin the compare-and-swap
admission, the run-scoped cancel routing, and the compare-and-reset terminal so
a rapid Sync/Cancel can't leave a half-reset run id (#1202).
"""

from __future__ import annotations

from domain.sync_state import SyncState
from services.library._state import LibrarySyncStateBox


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
