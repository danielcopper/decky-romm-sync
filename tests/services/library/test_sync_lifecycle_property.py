"""Property test for the ``LibrarySyncStateBox`` run-lifecycle state machine.

Hypothesis drives random interleavings of the three lifecycle verbs
(``try_begin_run`` / ``request_cancel`` / ``finish_run``) against a reference
model and asserts the safety invariants directly: only one run is ever in
flight, a non-owner ``finish_run`` never disturbs the active run, and
``current_sync_id`` is ``None`` iff the box is IDLE. A counterexample here is
exactly the #1202 class of bug — a stale terminal nulling a fresh run.
"""

from __future__ import annotations

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from domain.sync_state import SyncState
from services.library._state import LibrarySyncStateBox


class SyncLifecycleMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.box = LibrarySyncStateBox()
        self._counter = 0
        # Reference model: the run id we expect to own the slot while a run is
        # in flight; None when the box is idle.
        self.expected_owner: str | None = None

    def _fresh_id(self) -> str:
        self._counter += 1
        return f"run-{self._counter}"

    @rule()
    def begin(self) -> None:
        run_id = self._fresh_id()
        ok = self.box.try_begin_run(run_id)
        if self.expected_owner is None:
            # The slot was free — the run is admitted and becomes the owner.
            assert ok is True
            self.expected_owner = run_id
        else:
            # A run is already in flight — a second, distinct run is refused,
            # so two distinct ids can never both be in flight.
            assert ok is False

    @rule(target_owner=st.booleans())
    def cancel(self, target_owner: bool) -> None:
        run_id = self.expected_owner if (target_owner and self.expected_owner is not None) else self._fresh_id()
        outcome = self.box.request_cancel(run_id)
        if self.expected_owner is None:
            assert outcome == "no_sync"
        elif run_id == self.expected_owner:
            assert outcome == "cancelling"
        else:
            assert outcome == "stale"
        # A cancel — for any id — never changes which run owns the slot.
        assert self.box.current_sync_id == self.expected_owner

    @rule(target_owner=st.booleans())
    def finish(self, target_owner: bool) -> None:
        owner_before = self.expected_owner
        run_id = owner_before if (target_owner and owner_before is not None) else self._fresh_id()
        self.box.finish_run(run_id)
        if owner_before is not None and run_id == owner_before:
            self.expected_owner = None
        else:
            # A non-owner (late/foreign/doubled) terminal leaves the active run
            # — the heart of #1202 — untouched.
            assert self.box.current_sync_id == owner_before

    @invariant()
    def owner_tracks_box(self) -> None:
        assert self.box.current_sync_id == self.expected_owner

    @invariant()
    def id_none_iff_idle(self) -> None:
        is_idle = self.box.sync_state is SyncState.IDLE
        assert (self.box.current_sync_id is None) == is_idle

    @invariant()
    def at_most_one_in_flight(self) -> None:
        # A single ``current_sync_id`` field can hold only one id, so two
        # distinct runs can never both be in flight; pin it explicitly.
        if self.box.is_in_flight():
            assert self.box.current_sync_id is not None


TestSyncLifecycle = SyncLifecycleMachine.TestCase
