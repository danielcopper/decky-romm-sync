"""Tests for SessionLifecycleService."""

from __future__ import annotations

import asyncio
import logging

import pytest

from services.session_lifecycle import (
    SessionFinalizeMigration,
    SessionFinalizeResult,
    SessionFinalizeSyncResult,
    SessionLifecycleService,
    SessionLifecycleServiceConfig,
)


class FakePlaytimeRecorder:
    """In-memory ``SessionPlaytimeRecorder`` for tests."""

    def __init__(
        self,
        *,
        payload: dict | None = None,
        side_effect: BaseException | None = None,
    ) -> None:
        self.payload: dict = payload if payload is not None else {"success": True, "total_seconds": 3600}
        self.side_effect = side_effect
        self.calls: list[int] = []

    async def record_session_end(self, rom_id: int) -> dict:
        self.calls.append(rom_id)
        if self.side_effect is not None:
            raise self.side_effect
        return self.payload


class FakePostExitSync:
    """In-memory ``SessionPostExitSync`` for tests."""

    def __init__(
        self,
        *,
        payload: dict | None = None,
        side_effect: BaseException | None = None,
    ) -> None:
        self.payload: dict = payload if payload is not None else {"success": True, "synced": 0, "conflicts": []}
        self.side_effect = side_effect
        self.calls: list[int] = []

    async def post_exit_sync(self, rom_id: int) -> dict:
        self.calls.append(rom_id)
        if self.side_effect is not None:
            raise self.side_effect
        return self.payload


class FakeAchievementSync:
    """In-memory ``SessionAchievementSync`` for tests."""

    def __init__(
        self,
        *,
        payload: dict | None = None,
        side_effect: BaseException | None = None,
        completion_event: asyncio.Event | None = None,
    ) -> None:
        self.payload: dict = payload if payload is not None else {"success": True}
        self.side_effect = side_effect
        self.completion_event = completion_event
        self.calls: list[int] = []

    async def sync_achievements_after_session(self, rom_id: int) -> dict:
        self.calls.append(rom_id)
        try:
            if self.side_effect is not None:
                raise self.side_effect
            return self.payload
        finally:
            if self.completion_event is not None:
                self.completion_event.set()


class FakeMigrationReader:
    """In-memory ``SessionMigrationReader`` for tests."""

    def __init__(
        self,
        *,
        payload: dict | None = None,
        side_effect: BaseException | None = None,
        pending: bool = False,
    ) -> None:
        self.payload: dict = (
            payload if payload is not None else {"retrodeck": {"pending": False}, "save_sort": {"pending": False}}
        )
        self.side_effect = side_effect
        self.pending = pending
        self.refresh_calls = 0
        self.pending_calls = 0

    async def refresh_state(self) -> dict:
        self.refresh_calls += 1
        if self.side_effect is not None:
            raise self.side_effect
        return self.payload

    def is_retrodeck_migration_pending(self) -> bool:
        self.pending_calls += 1
        return self.pending


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_session_lifecycle")


def _make_service(
    *,
    playtime_recorder: FakePlaytimeRecorder,
    post_exit_sync: FakePostExitSync,
    achievement_sync: FakeAchievementSync,
    migration_reader: FakeMigrationReader,
    logger: logging.Logger,
) -> SessionLifecycleService:
    return SessionLifecycleService(
        config=SessionLifecycleServiceConfig(
            playtime_recorder=playtime_recorder,
            post_exit_sync=post_exit_sync,
            achievement_sync=achievement_sync,
            migration_reader=migration_reader,
            logger=logger,
        ),
    )


async def _drain_background_tasks(service: SessionLifecycleService) -> None:
    """Await any in-flight fire-and-forget tasks so the loop can finish them.

    ``finalize`` schedules the achievement sync detached from its return
    value — tests that assert the achievement sync ran (or didn't) need
    to give the loop a chance to execute it before inspecting fakes.
    """
    tasks = list(service._background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class TestFinalizePlaytime:
    def test_success_returns_total_seconds(self, event_loop, logger):
        """Playtime record success → ``total_seconds`` carries the updated total."""
        playtime = FakePlaytimeRecorder(payload={"success": True, "total_seconds": 7200})
        service = _make_service(
            playtime_recorder=playtime,
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.total_seconds == 7200
        assert playtime.calls == [99]

    def test_no_session_returns_none(self, event_loop, logger):
        """Playtime record returns ``success=False`` → ``total_seconds=None``."""
        playtime = FakePlaytimeRecorder(payload={"success": False, "message": "No active session"})
        service = _make_service(
            playtime_recorder=playtime,
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.total_seconds is None

    def test_exception_returns_none(self, event_loop, logger):
        """Playtime recorder raises → ``total_seconds=None``, downstream still runs."""
        playtime = FakePlaytimeRecorder(side_effect=RuntimeError("boom"))
        post = FakePostExitSync()
        migration = FakeMigrationReader()
        service = _make_service(
            playtime_recorder=playtime,
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.total_seconds is None
        # Playtime failure must NOT short-circuit the rest of the orchestration.
        assert post.calls == [99]
        assert migration.refresh_calls == 1

    def test_missing_total_seconds_key_returns_none(self, event_loop, logger):
        """``success=True`` but no ``total_seconds`` key → ``total_seconds=None``."""
        playtime = FakePlaytimeRecorder(payload={"success": True})
        service = _make_service(
            playtime_recorder=playtime,
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.total_seconds is None

    def test_non_int_total_seconds_returns_none(self, event_loop, logger):
        """Non-int ``total_seconds`` (e.g. None, str) → ``total_seconds=None``."""
        playtime = FakePlaytimeRecorder(payload={"success": True, "total_seconds": None})
        service = _make_service(
            playtime_recorder=playtime,
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.total_seconds is None


class TestFinalizeSyncToasts:
    def test_offline_renders_offline_toast_with_dispatch_flag(self, event_loop, logger):
        """``offline=True`` → offline-body toast, offline flag preserved for dispatch."""
        post = FakePostExitSync(payload={"offline": True, "success": False})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.offline is True
        assert result.sync.toast_title == "RomM Save Sync"
        assert result.sync.toast_body == "Server offline — saves will sync next time"

    def test_success_with_uploads_renders_synced_toast(self, event_loop, logger):
        """``success=True, synced>0`` → synced toast."""
        post = FakePostExitSync(payload={"success": True, "synced": 3, "conflicts": []})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.success is True
        assert result.sync.synced == 3
        assert result.sync.toast_title == "RomM Save Sync"
        assert result.sync.toast_body == "Saves synced with RomM"

    def test_success_with_no_uploads_renders_no_toast(self, event_loop, logger):
        """``success=True, synced=0`` → no toast (frontend still dispatches event)."""
        post = FakePostExitSync(payload={"success": True, "synced": 0, "conflicts": []})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.success is True
        assert result.sync.synced == 0
        assert result.sync.toast_title is None
        assert result.sync.toast_body is None

    def test_failure_renders_failure_toast(self, event_loop, logger):
        """``success=False, offline=False`` → failure toast."""
        post = FakePostExitSync(payload={"success": False})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.toast_title == "RomM Save Sync"
        assert result.sync.toast_body == "Failed to sync saves after exit"

    def test_post_exit_exception_renders_failure_toast(self, event_loop, logger):
        """Post-exit sync raises → failure toast, downstream still runs."""
        post = FakePostExitSync(side_effect=RuntimeError("network down"))
        migration = FakeMigrationReader()
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.offline is False
        assert result.sync.success is False
        assert result.sync.toast_title == "RomM Save Sync"
        assert result.sync.toast_body == "Failed to sync saves after exit"
        assert result.sync.conflicts == []
        # Post-exit failure must NOT short-circuit migration refresh.
        assert migration.refresh_calls == 1

    def test_synced_field_non_int_treated_as_falsy(self, event_loop, logger):
        """``synced`` not an int (None, str) → no synced toast even when success=True."""
        post = FakePostExitSync(payload={"success": True, "synced": None, "conflicts": []})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        # synced was None → treated as no uploads → no toast
        assert result.sync.synced is None
        assert result.sync.toast_title is None
        assert result.sync.toast_body is None


class TestFinalizeConflicts:
    def test_single_conflict_renders_singular_string(self, event_loop, logger):
        """One conflict → "1 save conflict need resolution" (singular form)."""
        conflict = {
            "type": "sync_conflict",
            "rom_id": 99,
            "filename": "game.srm",
            "server_save_id": 7,
        }
        post = FakePostExitSync(payload={"success": True, "synced": 1, "conflicts": [conflict]})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.conflicts == [conflict]
        assert result.sync.conflicts_toast == "1 save conflict need resolution"

    def test_multiple_conflicts_renders_plural_string(self, event_loop, logger):
        """Multiple conflicts → "N save conflicts need resolution" (plural)."""
        conflicts = [
            {"type": "sync_conflict", "rom_id": 99, "filename": "a.srm", "server_save_id": 1},
            {"type": "sync_conflict", "rom_id": 99, "filename": "b.srm", "server_save_id": 2},
            {"type": "sync_conflict", "rom_id": 99, "filename": "c.srm", "server_save_id": 3},
        ]
        post = FakePostExitSync(payload={"success": True, "synced": 0, "conflicts": conflicts})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.conflicts_toast == "3 save conflicts need resolution"

    def test_no_conflicts_renders_none(self, event_loop, logger):
        """Empty ``conflicts`` → ``conflicts_toast=None``."""
        post = FakePostExitSync(payload={"success": True, "synced": 1, "conflicts": []})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.conflicts == []
        assert result.sync.conflicts_toast is None

    def test_missing_conflicts_key_treated_as_empty(self, event_loop, logger):
        """No ``conflicts`` key → empty list, no conflicts toast."""
        post = FakePostExitSync(payload={"success": True, "synced": 1})
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.sync.conflicts == []
        assert result.sync.conflicts_toast is None


class TestFinalizeMigrationRefresh:
    def test_typed_pair_returned(self, event_loop, logger):
        """``refresh_state`` payload is repacked into the typed migration aggregate."""
        migration_payload = {
            "retrodeck": {"pending": True, "old_path": "/old", "new_path": "/new"},
            "save_sort": {"pending": False},
        }
        migration = FakeMigrationReader(payload=migration_payload)
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.migration.retrodeck == migration_payload["retrodeck"]
        assert result.migration.save_sort == migration_payload["save_sort"]
        assert migration.refresh_calls == 1

    def test_refresh_success_returns_populated_aggregate(self, event_loop, logger):
        """Happy path returns a populated ``SessionFinalizeMigration``, not ``None``."""
        migration_payload = {
            "retrodeck": {"pending": False},
            "save_sort": {"pending": False},
        }
        migration = FakeMigrationReader(payload=migration_payload)
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert isinstance(result.migration, SessionFinalizeMigration)
        assert result.migration.retrodeck == {"pending": False}
        assert result.migration.save_sort == {"pending": False}

    def test_refresh_exception_returns_none(self, event_loop, logger):
        """``refresh_state`` raises → ``migration`` is ``None`` (frontend leaves stores untouched)."""
        migration = FakeMigrationReader(side_effect=RuntimeError("config parse fail"))
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.migration is None

    def test_refresh_returns_non_dict_returns_none(self, event_loop, logger):
        """``refresh_state`` returns a non-dict → ``migration`` is ``None``."""
        migration = FakeMigrationReader(payload="garbage")  # type: ignore[arg-type]
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result.migration is None

    def test_refresh_partial_payload_keeps_aggregate(self, event_loop, logger):
        """``refresh_state`` returns a dict with non-dict fields → aggregate present, fields cleared."""
        migration = FakeMigrationReader(payload={"retrodeck": None, "save_sort": "garbage"})  # type: ignore[arg-type]
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        # Outer payload was a dict — the aggregate is still returned, but
        # each non-dict field falls back to the safe "not pending" default.
        assert isinstance(result.migration, SessionFinalizeMigration)
        assert result.migration.retrodeck == {"pending": False}
        assert result.migration.save_sort == {"pending": False}


class TestFinalizeMigrationGate:
    def test_pending_migration_skips_post_exit_sync(self, event_loop, logger):
        """``is_retrodeck_migration_pending=True`` → skip post-exit sync, render failure toast."""
        post = FakePostExitSync()
        migration = FakeMigrationReader(pending=True)
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        # Post-exit sync must not be called when migration is pending.
        assert post.calls == []
        assert result.sync.offline is False
        assert result.sync.success is False
        assert result.sync.toast_title == "RomM Save Sync"
        assert result.sync.toast_body == "Failed to sync saves after exit"
        # Playtime + migration refresh still ran.
        assert result.total_seconds == 3600
        assert migration.refresh_calls == 1


class TestFinalizeAchievementSync:
    def test_achievement_sync_runs_as_background_task(self, event_loop, logger):
        """Achievement sync is scheduled but not awaited by ``finalize``."""
        completion = asyncio.Event()
        achievements = FakeAchievementSync(completion_event=completion)
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=achievements,
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        # Drain background tasks so the achievement call actually executes.
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert achievements.calls == [99]
        assert completion.is_set()
        # Result is constructed from playtime + post-exit + migration, never
        # waits on achievement sync.
        assert isinstance(result, SessionFinalizeResult)

    def test_achievement_sync_failure_does_not_affect_result(self, event_loop, logger):
        """Achievement sync raises → logged, but ``finalize`` still returns success."""
        achievements = FakeAchievementSync(side_effect=RuntimeError("RA down"))
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=achievements,
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        # The failing fire-and-forget call did run, but did not affect the
        # returned DTO.
        assert achievements.calls == [99]
        assert isinstance(result.sync, SessionFinalizeSyncResult)
        assert isinstance(result.migration, SessionFinalizeMigration)


class TestFinalizeResultShape:
    def test_happy_path_full_dto(self, event_loop, logger):
        """Happy path returns a fully populated ``SessionFinalizeResult``."""
        conflicts = [
            {"type": "sync_conflict", "rom_id": 99, "filename": "a.srm", "server_save_id": 1},
        ]
        playtime = FakePlaytimeRecorder(payload={"success": True, "total_seconds": 1234})
        post = FakePostExitSync(payload={"success": True, "synced": 2, "conflicts": conflicts})
        migration = FakeMigrationReader(payload={"retrodeck": {"pending": False}, "save_sort": {"pending": False}})
        service = _make_service(
            playtime_recorder=playtime,
            post_exit_sync=post,
            achievement_sync=FakeAchievementSync(),
            migration_reader=migration,
            logger=logger,
        )

        result = event_loop.run_until_complete(service.finalize(99))
        event_loop.run_until_complete(_drain_background_tasks(service))

        assert result == SessionFinalizeResult(
            total_seconds=1234,
            sync=SessionFinalizeSyncResult(
                offline=False,
                success=True,
                synced=2,
                conflicts=conflicts,
                toast_title="RomM Save Sync",
                toast_body="Saves synced with RomM",
                conflicts_toast="1 save conflict need resolution",
            ),
            migration=SessionFinalizeMigration(
                retrodeck={"pending": False},
                save_sort={"pending": False},
            ),
        )


class TestBackgroundTaskTracking:
    """Coverage for the background-task tracking + ``shutdown()`` lifecycle.

    ``finalize`` schedules the achievement refresh detached from its
    return value via ``asyncio.create_task``. Without strong refs into
    ``_background_tasks`` and a cancellation hook in ``shutdown()``,
    those tasks leak across plugin unload. These tests pin the contract.
    """

    @pytest.mark.asyncio
    async def test_spawned_task_added_to_background_set(self, logger):
        """``finalize`` adds the achievement-sync task to ``_background_tasks``."""
        # Block the achievement sync so the spawned task is observable as pending.
        blocker = asyncio.Event()

        class BlockingAchievementSync:
            async def sync_achievements_after_session(self, rom_id: int) -> dict:
                await blocker.wait()
                return {"success": True}

        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=BlockingAchievementSync(),  # type: ignore[arg-type]
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        await service.finalize(99)

        assert len(service._background_tasks) == 1
        (task,) = service._background_tasks
        assert isinstance(task, asyncio.Task)

        # Release the blocker and drain so no pending-task warning fires.
        blocker.set()
        await asyncio.gather(*service._background_tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_done_callback_removes_task_on_natural_completion(self, logger):
        """When the achievement-sync coro completes naturally, the done-callback prunes the set."""
        completion = asyncio.Event()
        achievements = FakeAchievementSync(completion_event=completion)
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=achievements,
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        await service.finalize(99)
        assert len(service._background_tasks) == 1

        # Yield until the spawned achievement coroutine finishes; the
        # done-callback then discards the task from the set.
        (task,) = service._background_tasks
        await task

        assert service._background_tasks == set()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_pending_tasks_and_empties_set(self, logger):
        """``shutdown()`` cancels in-flight tasks and the set is empty after."""
        # Block the achievement sync indefinitely via an unset Event so the
        # cancellation is genuinely observable.
        blocker = asyncio.Event()

        class BlockingAchievementSync:
            async def sync_achievements_after_session(self, rom_id: int) -> dict:
                await blocker.wait()
                return {"success": True}

        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=BlockingAchievementSync(),  # type: ignore[arg-type]
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        await service.finalize(99)
        assert len(service._background_tasks) == 1
        (task,) = service._background_tasks

        await service.shutdown()

        assert task.cancelled()
        assert service._background_tasks == set()

    @pytest.mark.asyncio
    async def test_shutdown_with_empty_set_is_noop(self, logger):
        """``shutdown()`` on an untouched service returns immediately."""
        service = _make_service(
            playtime_recorder=FakePlaytimeRecorder(),
            post_exit_sync=FakePostExitSync(),
            achievement_sync=FakeAchievementSync(),
            migration_reader=FakeMigrationReader(),
            logger=logger,
        )

        assert service._background_tasks == set()

        # Must not raise, must not block.
        await service.shutdown()

        assert service._background_tasks == set()
