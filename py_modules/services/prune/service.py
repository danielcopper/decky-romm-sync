"""Callable facade and ephemeral run state for explicit vanished-ROM cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from lib.list_result import ErrorCode
from lib.url_host import romm_namespace
from services.prune._models import InstalledSelection, PendingAction, PruneOptions, PrunePreview, cancellation_state
from services.prune.executor import PruneExecutor, PruneExecutorConfig
from services.prune.preview import PreviewBuilder, PreviewBuilderConfig
from services.prune.recovery import RecoveryCoordinator, RecoveryCoordinatorConfig
from services.prune.registry import PruneRegistry, PruneRegistryConfig
from services.prune.requests import parse_options, parse_preview_request, parse_selection_page, valid_snapshot

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        ActiveDownloadRomIdsFn,
        Clock,
        EventEmitter,
        InstalledRomFilesRemoverFn,
        PruneArtifactStore,
        PruneSaveCoordinator,
        RecoveryBundleStore,
        RetroDeckPaths,
        RommLivenessApi,
        SaveDriftProbeFn,
        SteamRecoveryStore,
        UnitOfWorkFactory,
        UuidGen,
        VersionSwitcherFn,
    )

_STALE_PREVIEW_MESSAGE = "This cleanup preview is stale. Scan again before confirming."
_ACTION_TIMEOUT_SECONDS = 60.0
_RELEASE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class PruneServiceConfig:
    """Frozen composition-root wiring for explicit vanished-ROM cleanup."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    uuid_gen: UuidGen
    emit: EventEmitter
    uow_factory: UnitOfWorkFactory
    romm_api: RommLivenessApi
    recovery_store: RecoveryBundleStore
    prune_artifacts: PruneArtifactStore
    steam_recovery: SteamRecoveryStore
    retrodeck_paths: RetroDeckPaths
    save_coordinator: PruneSaveCoordinator
    active_downloads: ActiveDownloadRomIdsFn
    drift_probe: SaveDriftProbeFn
    remove_installed_files: InstalledRomFilesRemoverFn
    switch_version: VersionSwitcherFn
    settings: dict[str, Any]


def _invalid_action_report(request: dict[str, Any], pending: PendingAction) -> tuple[str, str] | None:
    """The reason a completion report is unusable, or ``None`` when it is well-formed.

    Everything here is shape, not outcome: a report that cannot be read is
    refused rather than guessed at, because the value it carries decides whether
    the run treats Steam as mutated.
    """
    if type(request.get("success")) is not bool or not isinstance(request.get("message"), str):
        return "invalid_action_result", "Action success and message fields are required."
    mutation_attempted = request.get("mutation_attempted")
    if mutation_attempted is not None and type(mutation_attempted) is not bool:
        return "invalid_action_result", "Action mutation-attempted must be a boolean."
    snapshot = request.get("snapshot")
    shortcut_absent = request.get("shortcut_absent") is True
    snapshot_required = (
        pending.kind == "capture_shortcut_snapshot" and request["success"] is True and not shortcut_absent
    )
    if snapshot_required and snapshot is None:
        return "invalid_snapshot", "The Steam recovery snapshot was missing."
    if shortcut_absent and pending.kind not in {"capture_shortcut_snapshot", "remove_shortcut"}:
        return "invalid_action_result", "This action may not report an absent shortcut."
    if snapshot is not None and (
        pending.kind != "capture_shortcut_snapshot" or not valid_snapshot(snapshot, pending.app_id)
    ):
        return "invalid_snapshot", "The Steam recovery snapshot was invalid or too large."
    return None


class PruneService:
    """Own preview consumption, one run claim, and frontend action leases."""

    def __init__(self, *, config: PruneServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._uuid_gen = config.uuid_gen
        self._emit = config.emit
        self._settings = config.settings
        self._recovery_store = config.recovery_store
        self._preview_builder = PreviewBuilder(
            config=PreviewBuilderConfig(
                uow_factory=config.uow_factory,
                recovery_store=config.recovery_store,
                retrodeck_paths=config.retrodeck_paths,
                settings=config.settings,
            )
        )
        recovery = RecoveryCoordinator(
            config=RecoveryCoordinatorConfig(
                uow_factory=config.uow_factory,
                recovery_store=config.recovery_store,
                prune_artifacts=config.prune_artifacts,
                steam_recovery=config.steam_recovery,
                retrodeck_paths=config.retrodeck_paths,
                clock=config.clock,
                uuid_gen=config.uuid_gen,
            )
        )
        registry = PruneRegistry(config=PruneRegistryConfig(uow_factory=config.uow_factory))
        self._executor = PruneExecutor(
            config=PruneExecutorConfig(
                loop=config.loop,
                logger=config.logger,
                emit=config.emit,
                romm_api=config.romm_api,
                recovery_store=config.recovery_store,
                prune_artifacts=config.prune_artifacts,
                steam_recovery=config.steam_recovery,
                save_coordinator=config.save_coordinator,
                active_downloads=config.active_downloads,
                drift_probe=config.drift_probe,
                remove_installed_files=config.remove_installed_files,
                switch_version=config.switch_version,
                settings=config.settings,
                recovery=recovery,
                registry=registry,
                request_action=self._request_action,
            )
        )
        self._registry = registry
        self._preview: PrunePreview | None = None
        self._task: asyncio.Task[None] | None = None
        self._admission_task: asyncio.Task[Any] | None = None
        self._run_id: str | None = None
        self._starting = False
        self._closed = False
        self._selection: InstalledSelection | None = None
        self._pending_action: PendingAction | None = None
        self._completed_action_tokens: set[str] = set()
        self._release_run_id: str | None = None
        self._run_preview_id: str | None = None
        self._release_event = asyncio.Event()
        self._admission_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()

    def is_active(self) -> bool:
        """Return whether admission or execution currently owns the prune claim."""
        return self._starting or self._run_id is not None

    async def get_prune_preview(self, request: object) -> dict[str, Any]:
        """Create or page an ephemeral local-only candidate preview."""
        parsed = parse_preview_request(request)
        if isinstance(parsed, dict):
            return parsed
        if self.is_active():
            return self._failure("prune_active", "A removed-game cleanup is already running.")
        scope, explicit_rom_id, preview_id, offset, limit = parsed
        try:
            preview = self._preview
            if preview_id is None:
                token = self._uuid_gen.uuid4()
                preview = await self._loop.run_in_executor(
                    None, self._preview_builder.build, token, scope, explicit_rom_id
                )
                self._preview = preview
                self._selection = None
            elif (
                preview is None
                or preview.preview_id != preview_id
                or preview.scope != scope
                or preview.explicit_rom_id != explicit_rom_id
                or preview.server_namespace != romm_namespace(self._settings)
            ):
                return self._failure("stale_preview", _STALE_PREVIEW_MESSAGE)
            result = self._preview_builder.page(preview, offset, limit)
            result["recovery_root"] = self._recovery_store.root()
            return result
        except Exception as exc:
            self._logger.exception("Removed-game cleanup preview failed")
            return self._failure(ErrorCode.UNKNOWN.value, str(exc))

    async def stage_prune_installed_selection(self, request: object) -> dict[str, Any]:
        """Append one bounded page to an ephemeral preview-bound selection."""
        parsed = parse_selection_page(request)
        if isinstance(parsed, dict):
            return parsed
        preview_id, selection_id, rom_ids, final = parsed
        async with self._admission_lock:
            preview = self._preview
            if self.is_active() or preview is None or preview.preview_id != preview_id:
                return self._failure("stale_preview", _STALE_PREVIEW_MESSAGE)
            installed_ids = {
                int(entry["rom_id"])
                for entry in preview.entries
                if entry.get("installed") is True and type(entry.get("rom_id")) is int
            }
            if not set(rom_ids) <= installed_ids:
                return self._failure(
                    "invalid_selection", "Selection contains a ROM without disclosed installed content."
                )
            selection = self._selection
            if selection_id is None:
                selection = InstalledSelection(preview_id, self._uuid_gen.uuid4(), set())
                self._selection = selection
            elif selection is None or selection.selection_id != selection_id or selection.preview_id != preview_id:
                return self._failure("stale_selection", "This installed-content selection is stale.")
            if selection.finalized:
                return self._failure("selection_finalized", "This installed-content selection is already complete.")
            selection.rom_ids.update(rom_ids)
            selection.finalized = final
            return {
                "success": True,
                "selection_id": selection.selection_id,
                "selected_count": len(selection.rom_ids),
                "finalized": selection.finalized,
            }

    async def start_prune(self, request: object) -> dict[str, Any]:
        """Atomically consume a preview and start one explicit cleanup run."""
        if not isinstance(request, dict) or request.get("confirmed") is not True:
            return self._failure("confirmation_required", "Explicit confirmation is required before cleanup.")
        selected = self._finalized_selection(request)
        if isinstance(selected, dict):
            return selected
        options = parse_options(request, selected)
        if isinstance(options, dict):
            return options
        preview_id = request.get("preview_id")
        async with self._admission_lock:
            if self._closed:
                return self._failure("service_stopping", "Removed-game cleanup is shutting down.")
            if self.is_active():
                return self._failure("prune_active", "A removed-game cleanup is already running.")
            preview = self._preview
            if not isinstance(preview_id, str) or preview is None or preview.preview_id != preview_id:
                return self._failure("stale_preview", _STALE_PREVIEW_MESSAGE)
            self._starting = True
            self._admission_task = asyncio.current_task()
        started_run = False
        run_id = ""
        try:
            refreshed = await self._loop.run_in_executor(
                None,
                self._preview_builder.build,
                preview.preview_id,
                preview.scope,
                preview.explicit_rom_id,
            )
            async with self._admission_lock:
                if self._closed:
                    return self._failure("service_stopping", "Removed-game cleanup is shutting down.")
                if (
                    refreshed.candidate_ids != preview.candidate_ids
                    or refreshed.fingerprint != preview.fingerprint
                    or refreshed.server_namespace != preview.server_namespace
                ):
                    self._preview = refreshed
                    self._selection = None
                    return self._failure("stale_preview", "Local game state changed. Review a fresh cleanup preview.")
                self._preview = None
                self._selection = None
                run_id = self._uuid_gen.uuid4()
                self._run_id = run_id
                self._run_preview_id = refreshed.preview_id
                self._release_run_id = run_id
                self._release_event = asyncio.Event()
                self._completed_action_tokens.clear()
                self._task = self._loop.create_task(self._run(run_id, refreshed, options))
                self._task.add_done_callback(self._release_stranded_claim)
                started_run = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.exception("Removed-game cleanup start validation failed")
            return self._failure(ErrorCode.UNKNOWN.value, str(exc))
        finally:
            async with self._admission_lock:
                self._starting = False
                self._admission_task = None
        if not started_run:
            return self._failure("service_stopping", "Removed-game cleanup did not start.")
        response: dict[str, Any] = {"success": True, "run_id": run_id}
        response["status"] = "running"
        return response

    def _finalized_selection(self, request: dict[str, Any]) -> frozenset[int] | dict[str, Any]:
        """The installed-content ids this start is authorized to include.

        An absent selection id means "none selected", which is the empty set —
        distinct from a selection that exists but was never finished, where
        starting would silently back up less than the user chose.
        """
        raw_selection_id = request.get("installed_selection_id")
        if raw_selection_id is None:
            return frozenset[int]()
        if not isinstance(raw_selection_id, str):
            return self._failure("invalid_selection_id", "Installed selection id must be a string or null.")
        selection = self._selection
        if selection is None or selection.selection_id != raw_selection_id or not selection.finalized:
            return self._failure("stale_selection", "Finish staging installed-content selections before cleanup.")
        return frozenset(selection.rom_ids)

    async def cancel_prune(self, run_id: object) -> dict[str, Any]:
        """Request that one identified run stop before starting another group.

        Cancellation is cooperative and never rolls anything back: the group
        already executing runs to its own terminal verdict, and the run reports
        what it committed. Idempotent for the same id — a second request while
        the first is still propagating is a success, not an error.
        """
        if not isinstance(run_id, str) or not run_id:
            return self._failure("invalid_run_id", "Cleanup run id must be a non-empty string.")
        async with self._admission_lock:
            task = self._task
            if self._run_id != run_id or task is None:
                return self._failure("stale_run", "That cleanup run is not running.")
            already_cancelling = task.cancelling() > 0 or task.done()
            if not already_cancelling:
                task.cancel()
        self._logger.info(
            f"Cleanup run {run_id} cancellation requested{' (already cancelling)' if already_cancelling else ''}"
        )
        return {
            "success": True,
            "run_id": run_id,
            "already_cancelling": already_cancelling,
            "message": "Cleanup will stop before the next group.",
        }

    async def report_prune_action(self, request: object) -> dict[str, Any]:
        """Claim or complete the exact frontend action currently awaited by the run."""
        try:
            return await self._report_prune_action(request)
        except Exception as exc:
            self._logger.exception("Removed-game cleanup action report failed")
            return self._failure(ErrorCode.UNKNOWN.value, str(exc))

    async def wait_for_prune_release(self, run_id: object) -> dict[str, Any]:
        """Boundedly acknowledge that one terminal run released its exclusive claim."""
        if not isinstance(run_id, str) or not run_id:
            return self._failure("invalid_run_id", "Cleanup run id must be a non-empty string.")
        if self._release_run_id != run_id or self._release_event.is_set():
            return {"success": True, "message": "Cleanup claim is released."}
        try:
            await asyncio.wait_for(self._release_event.wait(), timeout=_RELEASE_TIMEOUT_SECONDS)
        except TimeoutError:
            return self._failure("release_timeout", "Cleanup claim release was not observed in time.")
        return {"success": True, "message": "Cleanup claim is released."}

    async def _report_prune_action(self, request: object) -> dict[str, Any]:
        if not isinstance(request, dict):
            return self._failure("invalid_request", "Action result must be an object.")
        token = request.get("action_token")
        run_id = request.get("run_id")
        phase = request.get("phase")
        if phase not in {"claim", "complete"}:
            return self._failure("invalid_request", "Action phase must be claim or complete.")
        async with self._action_lock:
            if phase == "complete" and isinstance(token, str) and token in self._completed_action_tokens:
                return {"success": True, "ignored": True, "message": "Action result was already received."}
            pending = self._pending_action
            if pending is None or token != pending.token or run_id != pending.run_id:
                return self._failure("stale_action", "This cleanup action token is no longer active.")
            if self._clock.monotonic() >= pending.expires_at:
                return self._failure("stale_action", "This cleanup action token has expired.")
            if phase == "claim":
                return await self._claim_action(request, pending)
            return self._complete_action(request, pending)

    async def _claim_action(self, request: dict[str, Any], pending: PendingAction) -> dict[str, Any]:
        """Take exclusive ownership of the awaited action, or refuse the claim.

        The claim is what authorizes the frontend to touch Steam, so it has to
        match the pending operation exactly and the local binding has to still
        be the one the run planned against — re-checked after the await, because
        the token can expire or be replaced while the validation runs.
        """
        if (
            request.get("action") != pending.kind
            or request.get("app_id") != pending.app_id
            or request.get("target_rom_id") != pending.target_rom_id
        ):
            return self._failure("action_mismatch", "Action claim does not match the pending Steam operation.")
        if pending.claimed:
            return {"success": True, "ignored": True, "message": "Action token was already claimed."}
        if pending.app_id is not None and pending.expected_bound_rom_id is not None:
            valid = await self._loop.run_in_executor(
                None,
                self._registry.validate_action_state,
                pending.kind,
                pending.expected_bound_rom_id,
                pending.app_id,
                pending.target_rom_id,
                pending.group_rom_ids,
            )
            if not valid:
                return self._failure("local_state_changed", "The shortcut binding changed before the Steam action.")
        if self._pending_action is not pending or self._clock.monotonic() >= pending.expires_at:
            return self._failure("stale_action", "This cleanup action token has expired.")
        pending.claimed = True
        pending.expires_at = self._clock.monotonic() + _ACTION_TIMEOUT_SECONDS
        cast("asyncio.Event", pending.claim_event).set()
        return {"success": True, "message": "Action token claimed."}

    def _complete_action(self, request: dict[str, Any], pending: PendingAction) -> dict[str, Any]:
        """Accept the outcome of a claimed action, or refuse a malformed report."""
        if not pending.claimed:
            return self._failure("action_not_claimed", "Claim the action token before reporting its result.")
        invalid = _invalid_action_report(request, pending)
        if invalid is not None:
            return self._failure(*invalid)
        future = cast("asyncio.Future[dict[str, Any]]", pending.future)
        if future.done():
            return {"success": True, "ignored": True, "message": "Action result was already received."}
        result = dict(request)
        result["claimed"] = pending.claimed
        future.set_result(result)
        self._completed_action_tokens.add(pending.token)
        return {"success": True, "message": "Action result accepted."}

    async def shutdown(self) -> None:
        """Cancel unstarted work and await any already-claimed destructive phase."""
        async with self._admission_lock:
            self._closed = True
            admission_task = self._admission_task
            task = self._task
        current = asyncio.current_task()
        pending = [
            item for item in (admission_task, task) if item is not None and item is not current and not item.done()
        ]
        for item in pending:
            item.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _release_stranded_claim(self, task: asyncio.Task[None]) -> None:
        """Release the claim for a run task that never entered its own body.

        ``_run``'s ``finally`` owns the normal release, but a task cancelled
        before the loop first schedules it never gets there. Without this the
        run id would stay set for the process's lifetime and every conflicting
        callable — Play, downloads, saves — would keep being refused. Bound to
        the exact task, so a later run that already replaced it is untouched.
        """
        if self._task is not task or self._run_id is None:
            return
        self._logger.info(f"Cleanup run {self._run_id} released a claim its task never started")
        self._pending_action = None
        self._run_id = None
        self._run_preview_id = None
        self._release_event.set()

    async def _run(self, run_id: str, preview: PrunePreview, options: PruneOptions) -> None:
        try:
            await self._executor.run(run_id, preview, options)
        finally:
            pending = self._pending_action
            if pending is not None:
                future = cast("asyncio.Future[dict[str, Any]]", pending.future)
                if not future.done():
                    future.cancel()
            self._pending_action = None
            self._run_id = None
            self._run_preview_id = None
            self._release_event.set()

    async def _request_action(
        self,
        run_id: str,
        kind: str,
        data: dict[str, object],
        expected_bound_rom_id: int | None,
        target_rom_id: int | None,
        group_rom_ids: set[int],
    ) -> dict[str, Any]:
        token = self._uuid_gen.uuid4()
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        claim_event = asyncio.Event()
        raw_app_id = data.get("app_id")
        app_id = raw_app_id if type(raw_app_id) is int else None
        pending = PendingAction(
            run_id=run_id,
            token=token,
            kind=kind,
            app_id=app_id,
            expected_bound_rom_id=expected_bound_rom_id,
            target_rom_id=target_rom_id,
            group_rom_ids=frozenset(group_rom_ids),
            future=future,
            claim_event=claim_event,
            expires_at=self._clock.monotonic() + _ACTION_TIMEOUT_SECONDS,
        )
        self._pending_action = pending
        await self._emit(
            "prune_action_required",
            {
                "run_id": run_id,
                "preview_id": self._run_preview_id,
                "action_token": token,
                "action": kind,
                **data,
            },
        )
        try:
            await asyncio.wait_for(claim_event.wait(), timeout=_ACTION_TIMEOUT_SECONDS)
            result = await asyncio.wait_for(asyncio.shield(future), timeout=_ACTION_TIMEOUT_SECONDS)
            return self._action_result_or_cancel(result)
        except asyncio.CancelledError as exc:
            if pending.claimed:
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), timeout=_ACTION_TIMEOUT_SECONDS)
                except TimeoutError:
                    result = self._action_timeout_result(pending)
                cancellation_state(exc).action_result = result
            raise
        except TimeoutError:
            return self._action_result_or_cancel(self._action_timeout_result(pending))
        finally:
            if self._pending_action is pending:
                self._pending_action = None

    @staticmethod
    def _action_timeout_result(pending: PendingAction) -> dict[str, Any]:
        return {
            "success": False,
            "reason": "action_ambiguous" if pending.claimed else "action_timeout",
            "message": (
                "Steam action was claimed but its outcome is unknown."
                if pending.claimed
                else "Steam did not claim the action in time."
            ),
            "claimed": pending.claimed,
        }

    @staticmethod
    def _action_result_or_cancel(result: dict[str, Any]) -> dict[str, Any]:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            cancellation = asyncio.CancelledError()
            cancellation_state(cancellation).action_result = result
            raise cancellation
        return result

    @staticmethod
    def _failure(reason: str, message: str) -> dict[str, Any]:
        return {"success": False, "reason": reason, "message": message}
