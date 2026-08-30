"""One library-sync run's ``SyncRun`` row, from its planned counts to its one terminal status.

The run's durable record and nothing else: a ``running`` row written with what
the run set out to do, and exactly one terminal transition — completed,
cancelled, interrupted, paused or errored — written once it is over. Every
method here opens its own short write Unit of Work and is synchronous, so the
caller offloads it through its own executor.

**A row is not a run.** The in-flight run — admitted, cancelled, released — is
:class:`~services.library._state.LibrarySyncStateBox`'s, mutated only through its
verbs (``try_begin_run`` / ``request_cancel`` / ``finish_run``), and calling those
is :class:`~services.library.sync_orchestrator.SyncOrchestrator`'s alone. The two
vocabularies sit close enough to be mistaken for each other, so: ``finish_run``
releases the slot the next Sync press needs, while :meth:`SyncRunRecorder.do_complete_run`
writes the row the next preview reads its baseline out of. Nothing here can begin
or end a run — it can only describe one.

**Which terminal status a stopped run gets is not decided here.** The order a
stopped run is read in — a deliberate session-budget pause over a heartbeat
timeout over the user's own Cancel — follows from what the run observed while it
ran, so it belongs at the branch that observed it. This module offers one method
per status and holds no view of its own; that is why there is no single
``do_terminate(status)`` taking the verdict as an argument.

The terminal write is **single-shot**: an already-terminal run is left exactly as
it stands, silently. A run really can reach a terminal write twice — an exception
raised after a cancel has already recorded one — and the first outcome is the
true one, so the guard sits on the write rather than being owed by each caller.
A falsy run id is a no-op for the same reason: the error path terminates a run
that may never have been opened at all (a work-queue build that failed before the
``running`` row was written), and no caller should have to know which side of that
line it is on.

What the dependency surface does **not** carry is the contract. No event emitter:
this module tells nobody. On the paths that finish or stop a run, the
orchestrator emits the terminal progress frame and ``sync_complete`` only once
this write has landed (#39). The error path is not one of them and does not need
to be: it schedules its ERROR frame before awaiting this write, and emits no
``sync_complete`` at all, so there is no refetch of the run there to race.
No state box: the run id arrives as an argument, which keeps the write a
function of what the caller handed over rather than of what the box happens to
hold by the time the executor gets round to it — the orchestrator captures its
run id once, at the top of the run, and hands that same value to every write
here, the terminal one included.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.sync_run import SyncRun

if TYPE_CHECKING:
    from collections.abc import Callable

    from services.protocols import Clock, UnitOfWorkFactory


@dataclass(frozen=True)
class SyncRunRecorderConfig:
    """Frozen wiring bundle handed to ``SyncRunRecorder.__init__``.

    Holds the ``Clock`` every transition takes its timestamp from and the SQLite
    Unit-of-Work factory each short write opens. Two entries is the whole
    surface on purpose: the run id and its outcome arrive as arguments, so a
    third dependency here would mean the module had started deciding something
    rather than recording it.
    """

    clock: Clock
    uow_factory: UnitOfWorkFactory


class SyncRunRecorder:
    """Writes one library-sync run's ``SyncRun`` row: opened at its plan, closed at its outcome."""

    def __init__(self, *, config: SyncRunRecorderConfig) -> None:
        self._clock = config.clock
        self._uow_factory = config.uow_factory

    def do_open_run(self, run_id: str | None, platforms_planned: int, roms_planned: int) -> None:
        """Persist a fresh ``running`` SyncRun for the planned counts."""
        if not run_id:
            return
        run = SyncRun.start(
            id=run_id,
            at=self._clock.now().isoformat(),
            platforms_planned=platforms_planned,
            roms_planned=roms_planned,
        )
        with self._uow_factory() as uow:
            uow.sync_runs.save(run)

    def do_complete_run(self, run_id: str | None, platforms: list[str], collections: list[str]) -> None:
        """Transition the SyncRun to ``completed`` with its synced platform/collection names."""
        self._terminate_run(run_id, lambda run: run.complete(self._clock.now().isoformat(), platforms, collections))

    def do_mark_cancelled(self, run_id: str | None, reason: str) -> None:
        """Transition the SyncRun to ``cancelled``."""
        self._terminate_run(run_id, lambda run: run.mark_cancelled(self._clock.now().isoformat(), reason))

    def do_mark_interrupted(self, run_id: str | None, reason: str) -> None:
        """Transition the SyncRun to ``interrupted`` (external death, not user cancel)."""
        self._terminate_run(run_id, lambda run: run.mark_interrupted(self._clock.now().isoformat(), reason))

    def do_mark_paused(self, run_id: str | None, reason: str) -> None:
        """Transition the SyncRun to ``paused`` (a deliberate session-budget gate stop)."""
        self._terminate_run(run_id, lambda run: run.mark_paused(self._clock.now().isoformat(), reason))

    def do_mark_errored(self, run_id: str | None, error: str) -> None:
        """Transition the SyncRun to ``errored``."""
        self._terminate_run(run_id, lambda run: run.mark_errored(self._clock.now().isoformat(), error))

    def _terminate_run(self, run_id: str | None, transition: Callable[[SyncRun], None]) -> None:
        """Load the SyncRun, apply *transition*, and save it in one write UoW.

        No-op when the run is absent (never opened) or already terminal —
        the per-run lifecycle is single-shot, so a double-terminal call
        (e.g. an exception after a cancel) is silently dropped.
        """
        if not run_id:
            return
        with self._uow_factory() as uow:
            run = uow.sync_runs.get(run_id)
            if run is None or run.status != "running":
                return
            transition(run)
            uow.sync_runs.save(run)
