"""Tests for SyncRunRecorder — the ``SyncRun`` row one library-sync run leaves behind.

Driven through the shared ``plugin`` fixture so every write lands in the same
``FakeUnitOfWork`` the rest of the library suite seeds against: what a transition
did is only observable as the row it left, and the row is what the next preview's
baseline and the QAM's "Last sync" line read.

**Which** terminal a stopped run earns is the orchestrator's branch, not this
module's — a session-budget pause over a heartbeat timeout over the user's own
Cancel — and it is covered where that branch lives (``TestSyncRunLifecycle`` in
``tests/services/library/test_sync_orchestrator.py``). What is pinned here is
that each transition writes the status, reason and timestamp it names, and that
the three ways a write must decline to happen all decline silently.
"""

import pytest

from domain.sync_run import SyncRun


def _recorder(plugin):
    return plugin._sync_service._sync_run_recorder


def _clock(plugin):
    return plugin._sync_service._sync_run_recorder._clock


def _seed_running(plugin, run_id="run-1", *, platforms_planned=1, roms_planned=1):
    """Persist a ``running`` row directly, so a transition test starts from one."""
    with plugin._uow as uow:
        uow.sync_runs.save(
            SyncRun.start(
                id=run_id,
                at="2025-01-01T00:00:00",
                platforms_planned=platforms_planned,
                roms_planned=roms_planned,
            )
        )


class TestOpenRun:
    """The ``running`` row written at apply-dispatch, from the run's plan."""

    def test_open_run_persists_a_running_row_with_the_planned_counts(self, plugin):
        _recorder(plugin).do_open_run("run-open", 3, 42)

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-open")
        assert run is not None
        assert run.status == "running"
        assert run.platforms_planned == 3
        assert run.roms_planned == 42
        assert run.finished_at is None
        # The start timestamp comes from the injected clock, never from the
        # domain — SyncRun owns no clock.
        assert run.started_at == _clock(plugin).now().isoformat()

    @pytest.mark.parametrize("run_id", [None, ""])
    def test_open_run_without_a_run_id_writes_nothing(self, plugin, run_id):
        # The falsy id is the "no run was ever admitted" case; opening a row for
        # it would leave a run nothing can ever terminate.
        before = plugin._uow.sync_runs.save_count

        _recorder(plugin).do_open_run(run_id, 1, 1)

        assert plugin._uow.sync_runs.save_count == before
        with plugin._uow as uow:
            assert uow.sync_runs.get_running() is None


class TestTerminalTransitions:
    """Each transition records its own status, its reason, and when it happened."""

    def test_complete_run_records_the_synced_platforms_and_collections(self, plugin):
        _seed_running(plugin, "run-done", platforms_planned=2, roms_planned=7)
        _clock(plugin).advance(60)

        _recorder(plugin).do_complete_run("run-done", ["N64", "GBA"], ["Faves"])

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-done")
        assert run.status == "completed"
        assert run.platforms_completed == ["N64", "GBA"]
        assert run.collections_completed == ["Faves"]
        # Read at transition time, not at open time: the two timestamps are what
        # the "Last sync" line and the run's duration are computed from.
        assert run.finished_at == _clock(plugin).now().isoformat()
        assert run.started_at == "2025-01-01T00:00:00"
        assert run.error is None

    @pytest.mark.parametrize(
        ("method", "status"),
        [
            ("do_mark_cancelled", "cancelled"),
            ("do_mark_interrupted", "interrupted"),
            ("do_mark_paused", "paused"),
            ("do_mark_errored", "errored"),
        ],
    )
    def test_each_stopped_transition_records_its_own_status_and_reason(self, plugin, method, status):
        # The four stopped terminals are distinct on purpose: the UI reports a
        # pause and a crash differently from the user's own Cancel, and the only
        # thing carrying that distinction is the status this write lands.
        _seed_running(plugin, "run-stopped")
        _clock(plugin).advance(30)

        getattr(_recorder(plugin), method)("run-stopped", "because")

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-stopped")
        assert run.status == status
        assert run.error == "because"
        assert run.finished_at == _clock(plugin).now().isoformat()


class TestTheWriteThatDeclines:
    """The three ways a terminal write must decline — each silent, none a raise."""

    def test_an_already_terminal_run_is_left_exactly_as_it_stands(self, plugin):
        # A run really can reach a terminal write twice (an exception raised
        # after a cancel already recorded one). The first outcome is the true
        # one, so the second must neither raise nor overwrite it — SyncRun's own
        # ``_require_running`` would raise if the guard let the transition run.
        with plugin._uow as uow:
            run = SyncRun.start(id="run-terminal", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=1)
            run.complete("2025-01-01T01:00:00", ["N64"], [])
            uow.sync_runs.save(run)

        _recorder(plugin).do_complete_run("run-terminal", ["SNES"], ["Faves"])

        with plugin._uow as uow:
            after = uow.sync_runs.get("run-terminal")
        assert after.status == "completed"
        assert after.platforms_completed == ["N64"]
        assert after.collections_completed == []
        assert after.finished_at == "2025-01-01T01:00:00"

    def test_a_run_that_was_never_opened_is_a_noop(self, plugin):
        # The error path terminates a run that may have failed before its
        # ``running`` row was written — the work-queue build is outside the
        # opened run.
        before = plugin._uow.sync_runs.save_count

        _recorder(plugin).do_mark_errored("run-never-opened", "boom")

        assert plugin._uow.sync_runs.save_count == before
        with plugin._uow as uow:
            assert uow.sync_runs.get("run-never-opened") is None

    @pytest.mark.parametrize("run_id", [None, ""])
    def test_a_falsy_run_id_is_a_noop(self, plugin, run_id):
        # The error path passes ``run_id or box.current_sync_id``, which is
        # falsy when neither exists; it must not have to know that.
        _seed_running(plugin, "run-untouched")
        before = plugin._uow.sync_runs.save_count

        _recorder(plugin).do_mark_cancelled(run_id, "Sync cancelled")

        assert plugin._uow.sync_runs.save_count == before
        with plugin._uow as uow:
            assert uow.sync_runs.get("run-untouched").status == "running"
