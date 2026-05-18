"""SessionLifecycleService — single-callable end-of-session orchestration.

Composes the four cross-service reads the frontend's game-stop handler
used to interleave into one round-trip: end-of-session playtime
record, fire-and-forget achievement refresh, post-exit save sync, and
the migration-state refresh. Returns a typed ``SessionFinalizeResult``
carrying the playtime delta plus the rendered toast strings and the
migration-status payloads the frontend feeds into its in-memory
stores. The playtime-display update (Steam's ``appStore`` mutation)
stays on the frontend because it touches Steam IPC; everything else
about the end-of-session flow is now a backend decision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging

    from services.protocols.cross_service import (
        SessionAchievementSync,
        SessionMigrationReader,
        SessionPlaytimeRecorder,
        SessionPostExitSync,
    )


_TOAST_TITLE = "RomM Save Sync"
_TOAST_BODY_OFFLINE = "Server offline — saves will sync next time"
_TOAST_BODY_SYNCED = "Saves synced with RomM"
_TOAST_BODY_FAILED = "Failed to sync saves after exit"


@dataclass(frozen=True)
class SessionFinalizeSyncResult:
    """Post-exit-sync verdict rendered for the frontend.

    Carries the raw outcome flags the frontend still needs for event
    dispatch (``offline`` / ``success``) plus the pre-rendered toast
    strings. ``toast_body=None`` means "do not fire a toast for this
    scenario" — for example a successful sync that uploaded nothing.
    ``conflicts_toast`` is the second, additive toast fired when the
    sync surfaces unresolved conflicts; ``None`` when there are no
    conflicts. Conflict-string rendering lives here so the frontend
    receives a ready-to-display body.
    """

    offline: bool
    success: bool
    synced: int | None
    conflicts: list[dict]
    toast_title: str | None
    toast_body: str | None
    conflicts_toast: str | None


@dataclass(frozen=True)
class SessionFinalizeMigration:
    """Migration-status payloads returned from ``MigrationService.refresh_state``.

    Repacked into a typed aggregate so the frontend feeds each field
    into its dedicated store (``migrationStore`` / ``saveSortMigrationStore``)
    without re-deriving them from a loose dict.
    """

    retrodeck: dict
    save_sort: dict


@dataclass(frozen=True)
class SessionFinalizeResult:
    """End-of-session verdict consumed by the frontend session manager.

    ``total_seconds`` is ``None`` when the playtime record failed (no
    active session, malformed timestamp, etc.) — the frontend then
    leaves Steam's playtime display untouched. ``sync`` is always
    present; its fields encode whatever action the frontend still
    needs to take (toast, event dispatch). ``migration`` is ``None``
    when the migration-state refresh raised — the frontend then leaves
    the migration stores untouched (any stale ``pending`` badge keeps
    showing) and logs the failure backend-side. When the refresh
    succeeds, ``migration`` carries the two typed status payloads the
    frontend feeds into its stores.
    """

    total_seconds: int | None
    sync: SessionFinalizeSyncResult
    migration: SessionFinalizeMigration | None


@dataclass(frozen=True)
class SessionLifecycleServiceConfig:
    """Frozen wiring bundle handed to ``SessionLifecycleService.__init__``.

    All four deps are Protocol-typed cross-service seams that the
    composition root satisfies with the existing playtime, save,
    achievement, and migration services. ``logger`` is carried for
    backend-side reporting of the fire-and-forget achievement sync —
    its result and failures never reach the frontend.
    """

    playtime_recorder: SessionPlaytimeRecorder
    post_exit_sync: SessionPostExitSync
    achievement_sync: SessionAchievementSync
    migration_reader: SessionMigrationReader
    logger: logging.Logger


def _render_sync_toast(*, offline: bool, success: bool, synced: int | None) -> tuple[str | None, str | None]:
    """Map the raw post-exit-sync flags onto the (title, body) toast pair.

    Branching: offline wins over success, success-with-uploads renders
    the synced toast, a successful no-op produces no toast at all, and
    any non-success/non-offline state falls through to the failure
    toast.
    """
    if offline:
        return _TOAST_TITLE, _TOAST_BODY_OFFLINE
    if success and synced is not None and synced > 0:
        return _TOAST_TITLE, _TOAST_BODY_SYNCED
    if success:
        return None, None
    return _TOAST_TITLE, _TOAST_BODY_FAILED


def _render_conflicts_toast(conflicts: list[dict]) -> str | None:
    """Render the additive "N save conflict(s) need resolution" body.

    Singular when ``count == 1``, plural otherwise. Returns ``None``
    when there are no conflicts so the frontend skips the second toast.
    """
    count = len(conflicts)
    if count == 0:
        return None
    plural = "" if count == 1 else "s"
    return f"{count} save conflict{plural} need resolution"


class SessionLifecycleService:
    """End-of-session orchestration composed from four cross-service reads."""

    def __init__(self, *, config: SessionLifecycleServiceConfig) -> None:
        self._playtime_recorder = config.playtime_recorder
        self._post_exit_sync = config.post_exit_sync
        self._achievement_sync = config.achievement_sync
        self._migration_reader = config.migration_reader
        self._logger = config.logger
        # Strong refs to in-flight background tasks. ``asyncio.create_task``
        # alone is not enough — without a strong ref, the loop is free to
        # garbage-collect the task before it completes. ``add_done_callback``
        # prunes finished entries to keep the set bounded.
        self._background_tasks: set[asyncio.Task] = set()

    async def shutdown(self) -> None:
        """Cancel any in-flight background tasks and await their completion.

        Called from ``main._unload`` so detached achievement-refresh
        coroutines do not leak across the plugin unload boundary. No-op
        when no tasks are pending.
        """
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    async def finalize(self, rom_id: int) -> SessionFinalizeResult:
        """Run the four end-of-session steps and return the combined verdict.

        Parameters
        ----------
        rom_id:
            RomM ROM id whose session just ended.

        Returns
        -------
        SessionFinalizeResult
            ``total_seconds`` carries the updated total when the
            playtime record succeeded, ``None`` otherwise. ``sync``
            carries the pre-rendered toast strings and the raw offline /
            success flags the frontend still needs for the
            ``romm_data_changed`` event dispatch. ``migration`` carries
            the two migration-status payloads.
        """
        total_seconds = await self._record_playtime(rom_id)
        self._schedule_achievement_sync(rom_id)
        sync_result = await self._build_sync_result(rom_id)
        migration = await self._refresh_migration()

        return SessionFinalizeResult(
            total_seconds=total_seconds,
            sync=sync_result,
            migration=migration,
        )

    async def _record_playtime(self, rom_id: int) -> int | None:
        """Record session end and return the updated ``total_seconds``.

        Returns ``None`` on any non-success outcome (no active session,
        malformed timestamp, downstream exception) so the frontend
        knows to leave Steam's playtime display alone.
        """
        try:
            result = await self._playtime_recorder.record_session_end(rom_id)
        except Exception as e:
            self._logger.warning(f"SessionLifecycle playtime record failed for rom_id={rom_id}: {e}")
            return None

        if not result.get("success"):
            return None
        total = result.get("total_seconds")
        if not isinstance(total, int):
            return None
        return total

    def _schedule_achievement_sync(self, rom_id: int) -> None:
        """Kick off the achievement refresh as a background task.

        The frontend does not surface achievement-sync progress, so the
        call runs detached from the lifecycle return value. A strong
        ref into ``_background_tasks`` keeps the task alive long enough
        for the executor to finish; ``add_done_callback`` prunes the set
        once the task completes.
        """
        task = asyncio.create_task(self._run_achievement_sync(rom_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_achievement_sync(self, rom_id: int) -> None:
        """Wrap the achievement-sync call so its failure is logged backend-side."""
        try:
            await self._achievement_sync.sync_achievements_after_session(rom_id)
        except Exception as e:
            self._logger.warning(f"SessionLifecycle achievement sync failed for rom_id={rom_id}: {e}")

    async def _build_sync_result(self, rom_id: int) -> SessionFinalizeSyncResult:
        """Run post-exit sync and render the resulting toast strings.

        Honours the ``@migration_blocked`` gate: when a RetroDECK
        migration is pending the destructive post-exit sync is skipped
        and rendered as the standard "failed to sync saves after exit"
        toast.
        """
        if self._migration_reader.is_retrodeck_migration_pending():
            return SessionFinalizeSyncResult(
                offline=False,
                success=False,
                synced=None,
                conflicts=[],
                toast_title=_TOAST_TITLE,
                toast_body=_TOAST_BODY_FAILED,
                conflicts_toast=None,
            )

        try:
            result = await self._post_exit_sync.post_exit_sync(rom_id)
        except Exception as e:
            self._logger.warning(f"SessionLifecycle post-exit sync failed for rom_id={rom_id}: {e}")
            return SessionFinalizeSyncResult(
                offline=False,
                success=False,
                synced=None,
                conflicts=[],
                toast_title=_TOAST_TITLE,
                toast_body=_TOAST_BODY_FAILED,
                conflicts_toast=None,
            )

        offline = bool(result.get("offline"))
        success = bool(result.get("success"))
        raw_synced = result.get("synced")
        synced = raw_synced if isinstance(raw_synced, int) else None
        raw_conflicts = result.get("conflicts")
        conflicts: list[dict] = list(raw_conflicts) if isinstance(raw_conflicts, list) else []

        toast_title, toast_body = _render_sync_toast(offline=offline, success=success, synced=synced)
        conflicts_toast = _render_conflicts_toast(conflicts)

        return SessionFinalizeSyncResult(
            offline=offline,
            success=success,
            synced=synced,
            conflicts=conflicts,
            toast_title=toast_title,
            toast_body=toast_body,
            conflicts_toast=conflicts_toast,
        )

    async def _refresh_migration(self) -> SessionFinalizeMigration | None:
        """Re-detect migration state and return the typed status pair.

        Returns ``None`` on refresh failure (exception or non-dict
        payload) — the frontend then leaves the migration stores
        untouched (any stale ``pending`` badge keeps showing) and the
        failure is logged backend-side.
        """
        try:
            payload = await self._migration_reader.refresh_state()
        except Exception as e:
            self._logger.warning(f"SessionLifecycle migration refresh failed: {e}")
            return None

        if not isinstance(payload, dict):
            return None
        retrodeck = payload.get("retrodeck")
        save_sort = payload.get("save_sort")
        return SessionFinalizeMigration(
            retrodeck=retrodeck if isinstance(retrodeck, dict) else {"pending": False},
            save_sort=save_sort if isinstance(save_sort, dict) else {"pending": False},
        )
