"""Callable facade and ephemeral run state for explicit vanished-ROM cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from lib.list_result import ErrorCode
from services.prune._models import PendingAction, PruneOptions, PrunePreview
from services.prune.executor import PruneExecutor, PruneExecutorConfig
from services.prune.preview import PreviewBuilder, PreviewBuilderConfig
from services.prune.recovery import RecoveryCoordinator, RecoveryCoordinatorConfig
from services.prune.registry import PruneRegistry, PruneRegistryConfig
from services.prune.requests import parse_options, parse_preview_request, valid_snapshot

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
        RommRomReader,
        SaveDriftProbeFn,
        SteamRecoveryStore,
        UnitOfWorkFactory,
        UuidGen,
        VersionSwitcherFn,
    )

_ACTION_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class PruneServiceConfig:
    """Frozen composition-root wiring for explicit vanished-ROM cleanup."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    uuid_gen: UuidGen
    emit: EventEmitter
    uow_factory: UnitOfWorkFactory
    romm_api: RommRomReader
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


class PruneService:
    """Own preview consumption, one run claim, and frontend action leases."""

    def __init__(self, *, config: PruneServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._uuid_gen = config.uuid_gen
        self._emit = config.emit
        self._recovery_store = config.recovery_store
        self._preview_builder = PreviewBuilder(
            config=PreviewBuilderConfig(
                uow_factory=config.uow_factory,
                recovery_store=config.recovery_store,
                retrodeck_paths=config.retrodeck_paths,
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
        self._run_id: str | None = None
        self._starting = False
        self._pending_action: PendingAction | None = None
        self._completed_action_tokens: set[str] = set()
        self._admission_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()

    def is_active(self) -> bool:
        """Return whether admission or execution currently owns the prune claim."""
        return self._starting or (self._task is not None and not self._task.done())

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
            elif (
                preview is None
                or preview.preview_id != preview_id
                or preview.scope != scope
                or preview.explicit_rom_id != explicit_rom_id
            ):
                return self._failure("stale_preview", "This cleanup preview is stale. Scan again before confirming.")
            result = self._preview_builder.page(preview, offset, limit)
            result["recovery_root"] = self._recovery_store.root()
            return result
        except Exception as exc:
            self._logger.exception("Removed-game cleanup preview failed")
            return self._failure(ErrorCode.UNKNOWN.value, str(exc))

    async def start_prune(self, request: object) -> dict[str, Any]:
        """Atomically consume a preview and start one explicit cleanup run."""
        if not isinstance(request, dict) or request.get("confirmed") is not True:
            return self._failure("confirmation_required", "Explicit confirmation is required before cleanup.")
        options = parse_options(request)
        if isinstance(options, dict):
            return options
        preview_id = request.get("preview_id")
        async with self._admission_lock:
            if self.is_active():
                return self._failure("prune_active", "A removed-game cleanup is already running.")
            preview = self._preview
            if not isinstance(preview_id, str) or preview is None or preview.preview_id != preview_id:
                return self._failure("stale_preview", "This cleanup preview is stale. Scan again before confirming.")
            self._starting = True
        try:
            refreshed = await self._loop.run_in_executor(
                None,
                self._preview_builder.build,
                preview.preview_id,
                preview.scope,
                preview.explicit_rom_id,
            )
        except Exception as exc:
            self._logger.exception("Removed-game cleanup start validation failed")
            async with self._admission_lock:
                self._starting = False
            return self._failure(ErrorCode.UNKNOWN.value, str(exc))

        async with self._admission_lock:
            if refreshed.candidate_ids != preview.candidate_ids or refreshed.fingerprint != preview.fingerprint:
                self._preview = refreshed
                self._starting = False
                return self._failure("stale_preview", "Local game state changed. Review a fresh cleanup preview.")
            self._preview = None
            run_id = self._uuid_gen.uuid4()
            self._run_id = run_id
            self._completed_action_tokens.clear()
            self._task = self._loop.create_task(self._run(run_id, refreshed, options))
            self._starting = False
        response: dict[str, Any] = {"success": True, "run_id": run_id}
        response["status"] = "running"
        return response

    async def report_prune_action(self, request: object) -> dict[str, Any]:
        """Claim or complete the exact frontend action currently awaited by the run."""
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
                if pending.claimed:
                    return self._failure("action_already_claimed", "This cleanup action token is already claimed.")
                if pending.app_id is not None and pending.expected_bound_rom_id is not None:
                    valid = await self._loop.run_in_executor(
                        None,
                        self._registry.validate_action_state,
                        pending.kind,
                        pending.expected_bound_rom_id,
                        pending.app_id,
                        pending.target_rom_id,
                    )
                    if not valid:
                        return self._failure(
                            "local_state_changed", "The shortcut binding changed before the Steam action."
                        )
                if self._pending_action is not pending or self._clock.monotonic() >= pending.expires_at:
                    return self._failure("stale_action", "This cleanup action token has expired.")
                pending.claimed = True
                pending.expires_at = self._clock.monotonic() + _ACTION_TIMEOUT_SECONDS
                claim_event = cast("asyncio.Event", pending.claim_event)
                claim_event.set()
                return {"success": True, "message": "Action token claimed."}

            if not pending.claimed:
                return self._failure("action_not_claimed", "Claim the action token before reporting its result.")
            if type(request.get("success")) is not bool or not isinstance(request.get("message"), str):
                return self._failure("invalid_action_result", "Action success and message fields are required.")
            snapshot = request.get("snapshot")
            snapshot_required = pending.kind == "capture_shortcut_snapshot" and request["success"] is True
            if snapshot_required and snapshot is None:
                return self._failure("invalid_snapshot", "The Steam recovery snapshot was missing.")
            if snapshot is not None and (
                pending.kind != "capture_shortcut_snapshot" or not valid_snapshot(snapshot, pending.app_id)
            ):
                return self._failure("invalid_snapshot", "The Steam recovery snapshot was invalid or too large.")
            future = cast("asyncio.Future[dict[str, Any]]", pending.future)
            if future.done():
                return {"success": True, "ignored": True, "message": "Action result was already received."}
            future.set_result(dict(request))
            self._completed_action_tokens.add(pending.token)
            return {"success": True, "message": "Action result accepted."}

    async def shutdown(self) -> None:
        """Cancel unstarted work and await any already-claimed destructive phase."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

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

    async def _request_action(
        self,
        run_id: str,
        kind: str,
        data: dict[str, object],
        expected_bound_rom_id: int | None,
        target_rom_id: int | None,
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
            future=future,
            claim_event=claim_event,
            expires_at=self._clock.monotonic() + _ACTION_TIMEOUT_SECONDS,
        )
        self._pending_action = pending
        await self._emit(
            "prune_action_required",
            {"run_id": run_id, "action_token": token, "action": kind, **data},
        )
        try:
            await asyncio.wait_for(claim_event.wait(), timeout=_ACTION_TIMEOUT_SECONDS)
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=_ACTION_TIMEOUT_SECONDS)
                return self._with_cancellation_state(result)
            except asyncio.CancelledError:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=_ACTION_TIMEOUT_SECONDS)
                result["_cancelled"] = True
                return result
        except asyncio.CancelledError:
            if pending.claimed:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=_ACTION_TIMEOUT_SECONDS)
                result["_cancelled"] = True
                return result
            raise
        except TimeoutError:
            return {
                "success": False,
                "reason": "action_timeout",
                "message": "Steam did not confirm the action in time.",
            }
        finally:
            if self._pending_action is pending:
                self._pending_action = None

    @staticmethod
    def _with_cancellation_state(result: dict[str, Any]) -> dict[str, Any]:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            result["_cancelled"] = True
        return result

    @staticmethod
    def _failure(reason: str, message: str) -> dict[str, Any]:
        return {"success": False, "reason": reason, "message": message}
