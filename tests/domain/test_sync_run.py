"""Unit tests for the ``SyncRun`` aggregate."""

from __future__ import annotations

import pytest

from domain.sync_run import SyncRun


def _running() -> SyncRun:
    return SyncRun.start(
        id="run-1",
        at="2026-05-28T10:00:00",
        platforms_planned=3,
        roms_planned=120,
    )


class TestStart:
    def test_sets_running_state_and_plan_counts(self):
        run = SyncRun.start(
            id="run-1",
            at="2026-05-28T10:00:00",
            platforms_planned=3,
            roms_planned=120,
        )
        assert run.id == "run-1"
        assert run.started_at == "2026-05-28T10:00:00"
        assert run.status == "running"
        assert run.platforms_planned == 3
        assert run.roms_planned == 120

    def test_leaves_terminal_fields_unset(self):
        run = _running()
        assert run.finished_at is None
        assert run.platforms_completed is None
        assert run.collections_completed is None
        assert run.error is None

    def test_zero_plan_counts_allowed(self):
        run = SyncRun.start(
            id="run-1",
            at="2026-05-28T10:00:00",
            platforms_planned=0,
            roms_planned=0,
        )
        assert run.platforms_planned == 0
        assert run.roms_planned == 0

    def test_negative_platforms_planned_raises(self):
        with pytest.raises(ValueError, match="platforms_planned must be non-negative"):
            SyncRun.start(
                id="run-1",
                at="2026-05-28T10:00:00",
                platforms_planned=-1,
                roms_planned=0,
            )

    def test_negative_roms_planned_raises(self):
        with pytest.raises(ValueError, match="roms_planned must be non-negative"):
            SyncRun.start(
                id="run-1",
                at="2026-05-28T10:00:00",
                platforms_planned=0,
                roms_planned=-1,
            )

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="id is required"):
            SyncRun.start(
                id="",
                at="2026-05-28T10:00:00",
                platforms_planned=0,
                roms_planned=0,
            )


class TestComplete:
    def test_transitions_to_completed_and_records_results(self):
        run = _running()
        run.complete(
            at="2026-05-28T10:05:00",
            platforms=["snes", "gba"],
            collections=["Favorites"],
        )
        assert run.status == "completed"
        assert run.finished_at == "2026-05-28T10:05:00"
        assert run.platforms_completed == ["snes", "gba"]
        assert run.collections_completed == ["Favorites"]

    def test_clean_complete_leaves_error_none(self):
        run = _running()
        run.complete(at="2026-05-28T10:05:00", platforms=[], collections=[])
        assert run.error is None

    def test_empty_completed_lists_allowed(self):
        run = _running()
        run.complete(at="2026-05-28T10:05:00", platforms=[], collections=[])
        assert run.platforms_completed == []
        assert run.collections_completed == []

    def test_on_completed_run_raises(self):
        run = _running()
        run.complete(at="2026-05-28T10:05:00", platforms=[], collections=[])
        with pytest.raises(ValueError, match="run is not running"):
            run.complete(at="2026-05-28T10:06:00", platforms=[], collections=[])

    def test_on_cancelled_run_raises(self):
        run = _running()
        run.mark_cancelled(at="2026-05-28T10:05:00", reason="user aborted")
        with pytest.raises(ValueError, match="run is not running"):
            run.complete(at="2026-05-28T10:06:00", platforms=[], collections=[])

    def test_on_errored_run_raises(self):
        run = _running()
        run.mark_errored(at="2026-05-28T10:05:00", error="boom")
        with pytest.raises(ValueError, match="run is not running"):
            run.complete(at="2026-05-28T10:06:00", platforms=[], collections=[])


class TestMarkCancelled:
    def test_transitions_to_cancelled_and_records_reason(self):
        run = _running()
        run.mark_cancelled(at="2026-05-28T10:05:00", reason="user aborted")
        assert run.status == "cancelled"
        assert run.finished_at == "2026-05-28T10:05:00"
        assert run.error == "user aborted"

    def test_leaves_completed_lists_none(self):
        run = _running()
        run.mark_cancelled(at="2026-05-28T10:05:00", reason="user aborted")
        assert run.platforms_completed is None
        assert run.collections_completed is None

    def test_on_completed_run_raises(self):
        run = _running()
        run.complete(at="2026-05-28T10:05:00", platforms=[], collections=[])
        with pytest.raises(ValueError, match="run is not running"):
            run.mark_cancelled(at="2026-05-28T10:06:00", reason="too late")

    def test_on_cancelled_run_raises(self):
        run = _running()
        run.mark_cancelled(at="2026-05-28T10:05:00", reason="user aborted")
        with pytest.raises(ValueError, match="run is not running"):
            run.mark_cancelled(at="2026-05-28T10:06:00", reason="again")

    def test_on_errored_run_raises(self):
        run = _running()
        run.mark_errored(at="2026-05-28T10:05:00", error="boom")
        with pytest.raises(ValueError, match="run is not running"):
            run.mark_cancelled(at="2026-05-28T10:06:00", reason="too late")


class TestMarkErrored:
    def test_transitions_to_errored_and_records_error(self):
        run = _running()
        run.mark_errored(at="2026-05-28T10:05:00", error="connection refused")
        assert run.status == "errored"
        assert run.finished_at == "2026-05-28T10:05:00"
        assert run.error == "connection refused"

    def test_leaves_completed_lists_none(self):
        run = _running()
        run.mark_errored(at="2026-05-28T10:05:00", error="boom")
        assert run.platforms_completed is None
        assert run.collections_completed is None

    def test_on_completed_run_raises(self):
        run = _running()
        run.complete(at="2026-05-28T10:05:00", platforms=[], collections=[])
        with pytest.raises(ValueError, match="run is not running"):
            run.mark_errored(at="2026-05-28T10:06:00", error="boom")

    def test_on_cancelled_run_raises(self):
        run = _running()
        run.mark_cancelled(at="2026-05-28T10:05:00", reason="user aborted")
        with pytest.raises(ValueError, match="run is not running"):
            run.mark_errored(at="2026-05-28T10:06:00", error="boom")

    def test_on_errored_run_raises(self):
        run = _running()
        run.mark_errored(at="2026-05-28T10:05:00", error="boom")
        with pytest.raises(ValueError, match="run is not running"):
            run.mark_errored(at="2026-05-28T10:06:00", error="again")
