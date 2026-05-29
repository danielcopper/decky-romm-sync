"""Tests for ``SqliteSyncRunRepository`` over the ``sync_runs`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.sync_run import SyncRun

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _running(run_id: str, *, started_at: str = "2026-01-01T00:00:00Z") -> SyncRun:
    return SyncRun.start(id=run_id, at=started_at, platforms_planned=2, roms_planned=10)


class TestRoundTrip:
    def test_running_run_preserved_with_null_terminal_fields(self, uow: SqliteUnitOfWork):
        run = _running("run-1")
        uow.sync_runs.save(run)

        loaded = uow.sync_runs.get("run-1")
        assert loaded is not None
        assert loaded == run
        assert loaded.finished_at is None
        assert loaded.platforms_completed is None
        assert loaded.collections_completed is None
        assert loaded.error is None

    def test_completed_run_with_json_arrays_preserved(self, uow: SqliteUnitOfWork):
        run = _running("run-2")
        run.complete(at="2026-01-01T01:00:00Z", platforms=["snes", "gba"], collections=["favs"])
        uow.sync_runs.save(run)

        loaded = uow.sync_runs.get("run-2")
        assert loaded is not None
        assert loaded == run
        assert loaded.platforms_completed == ["snes", "gba"]
        assert loaded.collections_completed == ["favs"]

    def test_errored_run_carries_error_text(self, uow: SqliteUnitOfWork):
        run = _running("run-3")
        run.mark_errored(at="2026-01-01T01:00:00Z", error="boom")
        uow.sync_runs.save(run)

        loaded = uow.sync_runs.get("run-3")
        assert loaded is not None
        assert loaded.status == "errored"
        assert loaded.error == "boom"


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.sync_runs.get("nope") is None


class TestGetLatestCompleted:
    def test_returns_newest_completed_by_started_at(self, uow: SqliteUnitOfWork):
        older = _running("old", started_at="2026-01-01T00:00:00Z")
        older.complete(at="2026-01-01T02:00:00Z", platforms=[], collections=[])
        newer = _running("new", started_at="2026-02-01T00:00:00Z")
        newer.complete(at="2026-02-01T02:00:00Z", platforms=[], collections=[])
        uow.sync_runs.save(older)
        uow.sync_runs.save(newer)

        latest = uow.sync_runs.get_latest_completed()
        assert latest is not None
        assert latest.id == "new"

    def test_ignores_running_runs(self, uow: SqliteUnitOfWork):
        uow.sync_runs.save(_running("running-1"))
        assert uow.sync_runs.get_latest_completed() is None

    def test_returns_none_when_no_runs(self, uow: SqliteUnitOfWork):
        assert uow.sync_runs.get_latest_completed() is None


class TestGetRunning:
    def test_returns_the_running_run(self, uow: SqliteUnitOfWork):
        completed = _running("done")
        completed.complete(at="2026-01-01T02:00:00Z", platforms=[], collections=[])
        uow.sync_runs.save(completed)
        uow.sync_runs.save(_running("active"))

        running = uow.sync_runs.get_running()
        assert running is not None
        assert running.id == "active"

    def test_returns_none_when_none_running(self, uow: SqliteUnitOfWork):
        completed = _running("done")
        completed.complete(at="2026-01-01T02:00:00Z", platforms=[], collections=[])
        uow.sync_runs.save(completed)
        assert uow.sync_runs.get_running() is None


class TestUpsert:
    def test_save_existing_id_overwrites_status(self, uow: SqliteUnitOfWork):
        run = _running("run-1")
        uow.sync_runs.save(run)
        run.complete(at="2026-01-01T03:00:00Z", platforms=["snes"], collections=[])
        uow.sync_runs.save(run)

        loaded = uow.sync_runs.get("run-1")
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.platforms_completed == ["snes"]
