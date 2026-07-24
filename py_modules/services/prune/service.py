"""Facade and execution state machine for explicit vanished-ROM cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from domain.prune import selected_prune_ids
from domain.sibling_resolution import AUTO_REGION, resolve_group_representative
from lib.errors import RommNotFoundError, classify_error
from lib.list_result import ErrorCode
from services.prune._models import PendingAction, PruneOptions, PrunePreview
from services.prune.preview import PreviewBuilder, PreviewBuilderConfig
from services.prune.recovery import RecoveryCoordinator, RecoveryCoordinatorConfig
from services.prune.registry import PruneRegistry, PruneRegistryConfig
from services.prune.requests import parse_options, parse_preview_request, valid_snapshot

if TYPE_CHECKING:
    import logging

    from domain.rom import Rom
    from services.protocols import (
        ActiveDownloadRomIdsFn,
        Clock,
        EventEmitter,
        InstalledRomRemoverFn,
        PruneArtifactStore,
        PruneSaveCoordinator,
        RecoveryBundleStore,
        RetroDeckPaths,
        RommRomReader,
        SaveDriftProbeFn,
        SteamConfigStore,
        SteamRecoveryStore,
        UnitOfWorkFactory,
        UuidGen,
    )

_ACTION_TIMEOUT_SECONDS = 60.0
_LIVENESS_CONCURRENCY = 4


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
    steam_config: SteamConfigStore
    retrodeck_paths: RetroDeckPaths
    save_coordinator: PruneSaveCoordinator
    active_downloads: ActiveDownloadRomIdsFn
    drift_probe: SaveDriftProbeFn
    remove_installed_rom: InstalledRomRemoverFn
    settings: dict[str, Any]


class PruneService:
    """Own preview tokens, one cleanup run, and tokenized frontend actions."""

    def __init__(self, *, config: PruneServiceConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._uuid_gen = config.uuid_gen
        self._emit = config.emit
        self._romm_api = config.romm_api
        self._recovery_store = config.recovery_store
        self._prune_artifacts = config.prune_artifacts
        self._steam_recovery = config.steam_recovery
        self._steam_config = config.steam_config
        self._save_coordinator = config.save_coordinator
        self._active_downloads = config.active_downloads
        self._drift_probe = config.drift_probe
        self._remove_installed_rom = config.remove_installed_rom
        self._settings = config.settings
        self._preview_builder = PreviewBuilder(
            config=PreviewBuilderConfig(
                uow_factory=config.uow_factory,
                recovery_store=config.recovery_store,
                retrodeck_paths=config.retrodeck_paths,
            )
        )
        self._recovery = RecoveryCoordinator(
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
        self._registry = PruneRegistry(config=PruneRegistryConfig(uow_factory=config.uow_factory))
        self._preview: PrunePreview | None = None
        self._task: asyncio.Task[None] | None = None
        self._run_id: str | None = None
        self._pending_action: PendingAction | None = None
        self._completed_action_tokens: set[str] = set()

    async def get_prune_preview(self, request: object) -> dict[str, Any]:
        """Create or page an ephemeral local-only candidate preview."""
        parsed = parse_preview_request(request)
        if isinstance(parsed, dict):
            return parsed
        scope, explicit_rom_id, preview_id, offset, limit = parsed
        preview = self._preview
        if preview_id is None:
            token = self._uuid_gen.uuid4()
            preview = await self._loop.run_in_executor(None, self._preview_builder.build, token, scope, explicit_rom_id)
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

    async def start_prune(self, request: object) -> dict[str, Any]:
        """Validate a preview and start one explicit cleanup run."""
        if self._task is not None and not self._task.done():
            return self._failure("prune_active", "A removed-game cleanup is already running.")
        if not isinstance(request, dict) or request.get("confirmed") is not True:
            return self._failure("confirmation_required", "Explicit confirmation is required before cleanup.")
        preview_id = request.get("preview_id")
        preview = self._preview
        if not isinstance(preview_id, str) or preview is None or preview.preview_id != preview_id:
            return self._failure("stale_preview", "This cleanup preview is stale. Scan again before confirming.")
        refreshed = await self._loop.run_in_executor(
            None,
            self._preview_builder.build,
            preview.preview_id,
            preview.scope,
            preview.explicit_rom_id,
        )
        if refreshed.candidate_ids != preview.candidate_ids or refreshed.fingerprint != preview.fingerprint:
            self._preview = refreshed
            return self._failure("stale_preview", "Local game state changed. Review a fresh cleanup preview.")
        options = parse_options(request)
        if isinstance(options, dict):
            return options
        self._preview = None
        run_id = self._uuid_gen.uuid4()
        self._run_id = run_id
        self._completed_action_tokens.clear()
        self._task = self._loop.create_task(self._run(run_id, refreshed, options))
        response: dict[str, Any] = {"success": True, "run_id": run_id}
        response["status"] = "running"
        return response

    async def report_prune_action(self, request: object) -> dict[str, Any]:
        """Resolve the exact frontend action token currently awaited by the run."""
        if not isinstance(request, dict):
            return self._failure("invalid_request", "Action result must be an object.")
        token = request.get("action_token")
        run_id = request.get("run_id")
        if isinstance(token, str) and token in self._completed_action_tokens:
            return {"success": True, "ignored": True, "message": "Action result was already received."}
        pending = self._pending_action
        if pending is None or token != pending.token or run_id != pending.run_id:
            return self._failure("stale_action", "This cleanup action token is no longer active.")
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
        """Cancel the in-memory run; no pending action survives plugin unload."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self, run_id: str, preview: PrunePreview, options: PruneOptions) -> None:
        results: list[dict[str, Any]] = []
        removed_ids: list[int] = []
        affected_app_ids: set[int] = set()
        try:
            groups = await self._loop.run_in_executor(
                None, self._registry.groups_for_candidates, set(preview.candidate_ids)
            )
            total = len(groups)
            for index, rows in enumerate(groups, start=1):
                await self._emit_progress(run_id, index, total, "checking", rows)
                result = await self._run_group(run_id, rows, set(preview.candidate_ids), options, index, total)
                results.append(result)
                removed_ids.extend(int(value) for value in result.get("removed_rom_ids", []))
                app_id = result.get("app_id")
                if type(app_id) is int:
                    affected_app_ids.add(app_id)
            failures = [result for result in results if result["status"] in {"failed", "skipped"}]
            payload = {
                "success": not failures,
                "partial": bool(removed_ids) and bool(failures),
                "run_id": run_id,
                "removed_rom_ids": sorted(set(removed_ids)),
                "affected_app_ids": sorted(affected_app_ids),
                "results": results,
            }
            await self._emit("prune_complete", payload)
        except asyncio.CancelledError:
            await self._emit(
                "prune_complete",
                {
                    "success": False,
                    "partial": bool(removed_ids),
                    "run_id": run_id,
                    "removed_rom_ids": sorted(set(removed_ids)),
                    "affected_app_ids": sorted(affected_app_ids),
                    "results": results,
                    "reason": "cancelled",
                    "message": "Cleanup was cancelled; unconfirmed groups were left unchanged.",
                },
            )
            raise
        except Exception as exc:
            self._logger.exception("Vanished-ROM cleanup failed")
            await self._emit(
                "prune_complete",
                {
                    "success": False,
                    "partial": bool(removed_ids),
                    "run_id": run_id,
                    "removed_rom_ids": sorted(set(removed_ids)),
                    "affected_app_ids": sorted(affected_app_ids),
                    "results": results,
                    "reason": ErrorCode.UNKNOWN.value,
                    "message": str(exc),
                },
            )
        finally:
            pending = self._pending_action
            if pending is not None:
                future = cast("asyncio.Future[dict[str, Any]]", pending.future)
                if not future.done():
                    future.cancel()
            self._pending_action = None
            self._run_id = None

    async def _run_group(
        self,
        run_id: str,
        initial_rows: list[Rom],
        preview_candidate_ids: set[int],
        options: PruneOptions,
        index: int,
        total: int,
    ) -> dict[str, Any]:
        rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
        if not rows:
            return self._group_result(initial_rows, "skipped", "local_state_changed", "The local group changed.")
        group_ids = {row.rom_id for row in rows}
        candidate_ids = group_ids & preview_candidate_ids
        bound = [row for row in rows if row.shortcut_app_id is not None]
        if len(bound) > 1:
            return self._group_result(rows, "skipped", "multiple_bindings", "The group has multiple shortcuts.")
        if self._active_downloads() & group_ids:
            return self._group_result(rows, "skipped", "download_in_progress", "Cancel active downloads first.")

        verdicts = await self._probe_many(group_ids)
        vanished_ids = {rom_id for rom_id, verdict in verdicts.items() if verdict["status"] == "vanished"}
        live_ids = {rom_id for rom_id, verdict in verdicts.items() if verdict["status"] == "live"}
        uncertain_ids = group_ids - vanished_ids - live_ids
        fully_dead = bool(group_ids) and group_ids <= vanished_ids
        delete_ids = selected_prune_ids(
            group_ids=sorted(group_ids),
            candidate_ids=candidate_ids,
            vanished_ids=vanished_ids,
            live_ids=live_ids,
            remove_rows=options.remove_rows,
            remove_fully_vanished=options.remove_fully_vanished,
        )
        if not live_ids and uncertain_ids:
            return self._group_result(
                rows,
                "skipped",
                "liveness_uncertain",
                f"RomM could not confirm {len(uncertain_ids)} group member(s); nothing was removed.",
            )

        bound_row = bound[0] if bound else None
        target_id: int | None = None
        drifted = False
        if bound_row is not None and bound_row.rom_id in delete_ids and not fully_dead:
            if not options.repoint_shortcuts:
                delete_ids.remove(bound_row.rom_id)
            else:
                target_id = self._natural_default(rows, live_ids)
                if target_id is None:
                    return self._group_result(rows, "skipped", "no_live_default", "No live default could be selected.")
                drift = await self._drift_probe(bound_row.rom_id)
                drifted = bool(drift.get("drifted"))
                if drifted and not options.create_recovery_bundle:
                    return self._group_result(
                        rows,
                        "skipped",
                        "unsynced_saves",
                        "Unsynced saves require a sealed recovery bundle before automatic repointing.",
                    )
        if not delete_ids:
            return self._group_result(
                rows, "skipped", "options_excluded", "No confirmed rows matched the selected options."
            )

        app_id = bound_row.shortcut_app_id if bound_row is not None else None
        frontend_steam: dict[str, object] | None = None
        if fully_dead and app_id is not None and options.create_recovery_bundle:
            capture = await self._request_action(
                run_id,
                "capture_shortcut_snapshot",
                {"app_id": app_id},
            )
            if not capture.get("success") or not isinstance(capture.get("snapshot"), dict):
                return self._group_result(
                    rows,
                    "failed",
                    "steam_snapshot_failed",
                    capture.get("message", "Steam snapshot failed."),
                )
            frontend_steam = capture["snapshot"]

        first_inventory = await self._loop.run_in_executor(
            None, self._save_coordinator.inventory_prune_saves, sorted(delete_ids)
        )
        lock_ids = [int(value) for value in first_inventory.get("lock_rom_ids", sorted(delete_ids))]
        bundle_path: str | None = None
        async with self._save_coordinator.lock_prune_roms(lock_ids):
            rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
            if not rows or not delete_ids <= {row.rom_id for row in rows}:
                return self._group_result(initial_rows, "skipped", "local_state_changed", "The local group changed.")
            save_inventory = await self._loop.run_in_executor(
                None, self._save_coordinator.inventory_prune_saves, sorted(delete_ids)
            )
            if options.create_recovery_bundle:
                await self._emit_progress(run_id, index, total, "creating_recovery", rows)
                try:
                    snapshot = await self._loop.run_in_executor(
                        None, self._recovery.snapshot_state, sorted(delete_ids), frontend_steam
                    )
                    bundle_path, _steam_backend = await self._loop.run_in_executor(
                        None,
                        lambda: self._recovery.seal(
                            rows=[row for row in rows if row.rom_id in delete_ids],
                            snapshot=snapshot,
                            save_inventory=save_inventory,
                            include_installed_rom_ids=set(options.include_installed_rom_ids),
                            app_id=app_id if fully_dead else None,
                        ),
                    )
                except Exception as exc:
                    self._logger.error(f"Recovery bundle failed for group {min(group_ids)}: {exc}")
                    return self._group_result(rows, "failed", "recovery_failed", str(exc))
                await self._emit_progress(run_id, index, total, "recovery_sealed", rows, bundle_path=bundle_path)

        if self._active_downloads() & delete_ids:
            return self._group_result(rows, "skipped", "download_in_progress", "Cancel active downloads first.")
        refreshed = await self._probe_many(delete_ids | ({target_id} if target_id is not None else set()))
        guard = self._fresh_guard(refreshed, delete_ids, target_id)
        if guard is not None:
            return self._group_result(rows, "skipped", guard[0], guard[1], bundle_path=bundle_path)

        action_kind: Literal["repoint_shortcut", "remove_shortcut"] | None = None
        if target_id is not None and app_id is not None:
            action_kind = "repoint_shortcut"
            await self._emit_progress(run_id, index, total, "repointing", rows, bundle_path=bundle_path)
            action = await self._request_action(
                run_id,
                action_kind,
                {
                    "app_id": app_id,
                    "target_rom_id": target_id,
                    "allow_stranded": drifted and bundle_path is not None,
                },
            )
            if not action.get("success"):
                return self._group_result(
                    rows,
                    "failed",
                    "steam_action_failed",
                    action.get("message", "Repoint failed."),
                    app_id=app_id,
                    bundle_path=bundle_path,
                )
        elif fully_dead and app_id is not None:
            action_kind = "remove_shortcut"
            await self._emit_progress(run_id, index, total, "removing_shortcut", rows, bundle_path=bundle_path)
            action = await self._request_action(run_id, action_kind, {"app_id": app_id})
            if not action.get("success"):
                return self._group_result(
                    rows,
                    "failed",
                    "steam_action_failed",
                    action.get("message", "Shortcut removal failed."),
                    app_id=app_id,
                    bundle_path=bundle_path,
                )

        acted_app_id = app_id if action_kind is not None else None
        refreshed = await self._probe_many(delete_ids | ({target_id} if target_id is not None else set()))
        guard = self._fresh_guard(refreshed, delete_ids, target_id)
        if guard is not None:
            return self._group_result(rows, "skipped", guard[0], guard[1], app_id=acted_app_id, bundle_path=bundle_path)
        if self._active_downloads() & delete_ids:
            return self._group_result(
                rows,
                "skipped",
                "download_in_progress",
                "Cancel active downloads first.",
                app_id=acted_app_id,
            )

        state_matches = await self._loop.run_in_executor(
            None,
            self._registry.validate_deletion_state,
            rows,
            delete_ids,
            target_id,
            app_id,
            fully_dead,
        )
        if not state_matches:
            return self._group_result(
                rows,
                "skipped",
                "local_state_changed",
                "Local group or shortcut state changed; no source data was removed.",
                app_id=acted_app_id,
                bundle_path=bundle_path,
            )

        refreshed_inventory = await self._loop.run_in_executor(
            None, self._save_coordinator.inventory_prune_saves, sorted(delete_ids)
        )
        refreshed_lock_ids = [int(value) for value in refreshed_inventory.get("lock_rom_ids", sorted(delete_ids))]
        async with self._save_coordinator.lock_prune_roms(refreshed_lock_ids):
            save_inventory = await self._loop.run_in_executor(
                None, self._save_coordinator.inventory_prune_saves, sorted(delete_ids)
            )
            quarantine = await self._loop.run_in_executor(
                None, self._save_coordinator.quarantine_prune_saves, save_inventory["exclusive"]
            )
            if not quarantine.get("success"):
                return self._group_result(
                    rows,
                    "failed",
                    "save_quarantine_failed",
                    quarantine["message"],
                    app_id=acted_app_id,
                    bundle_path=bundle_path,
                )

        for rom_id in sorted(delete_ids):
            removal = await self._remove_installed_rom(rom_id)
            if not removal.get("success") and removal.get("reason") != "not_installed":
                return self._group_result(
                    rows,
                    "failed",
                    "rom_removal_failed",
                    removal.get("message", "ROM removal failed."),
                    app_id=acted_app_id,
                    bundle_path=bundle_path,
                )
        try:
            await self._loop.run_in_executor(None, self._prune_artifacts.remove, sorted(delete_ids))
            if action_kind == "remove_shortcut" and app_id is not None:
                await self._loop.run_in_executor(None, self._steam_recovery.remove_files, app_id)
                await self._loop.run_in_executor(None, self._steam_config.set_steam_input_config, [app_id], "default")
        except Exception as exc:
            return self._group_result(
                rows,
                "failed",
                "artifact_cleanup_failed",
                str(exc),
                app_id=acted_app_id,
                bundle_path=bundle_path,
            )

        deleted = await self._loop.run_in_executor(
            None,
            self._registry.delete_rows,
            rows,
            delete_ids,
            target_id,
            app_id,
            fully_dead,
        )
        if not deleted:
            return self._group_result(
                rows,
                "failed",
                "local_state_changed",
                "Final local revalidation failed.",
                app_id=acted_app_id,
                bundle_path=bundle_path,
            )

        await self._emit_progress(run_id, index, total, "removed", rows, bundle_path=bundle_path)
        return self._group_result(
            rows,
            "removed",
            None,
            f"Removed {len(delete_ids)} confirmed vanished entr{'y' if len(delete_ids) == 1 else 'ies'}.",
            removed_rom_ids=sorted(delete_ids),
            app_id=app_id,
            bundle_path=bundle_path,
        )

    async def _probe_many(self, rom_ids: set[int]) -> dict[int, dict[str, str]]:
        semaphore = asyncio.Semaphore(_LIVENESS_CONCURRENCY)

        async def one(rom_id: int) -> tuple[int, dict[str, str]]:
            async with semaphore:
                verdict = await self._loop.run_in_executor(None, self._probe_one, rom_id)
                return rom_id, verdict

        return dict(await asyncio.gather(*(one(rom_id) for rom_id in sorted(rom_ids))))

    def _probe_one(self, rom_id: int) -> dict[str, str]:
        try:
            payload: Any = self._romm_api.get_rom_once(rom_id)
        except RommNotFoundError:
            return {"status": "vanished", "reason": ErrorCode.NOT_FOUND.value, "message": "RomM confirmed 404."}
        except Exception as exc:
            reason, message = classify_error(exc)
            return {"status": "uncertain", "reason": reason, "message": message}
        payload_id = payload.get("id") if isinstance(payload, dict) else None
        if type(payload_id) is int and payload_id == rom_id:
            return {"status": "live", "reason": "live", "message": "RomM returned the exact ROM."}
        return {
            "status": "uncertain",
            "reason": "untrustworthy_response",
            "message": "RomM returned an empty, malformed, or wrong-id response.",
        }

    async def _request_action(self, run_id: str, kind: str, data: dict[str, object]) -> dict[str, Any]:
        token = self._uuid_gen.uuid4()
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        raw_app_id = data.get("app_id")
        app_id = raw_app_id if type(raw_app_id) is int else None
        self._pending_action = PendingAction(
            run_id=run_id,
            token=token,
            kind=kind,
            app_id=app_id,
            future=future,
        )
        await self._emit(
            "prune_action_required",
            {"run_id": run_id, "action_token": token, "action": kind, **data},
        )
        try:
            return await asyncio.wait_for(future, timeout=_ACTION_TIMEOUT_SECONDS)
        except TimeoutError:
            return {
                "success": False,
                "reason": "action_timeout",
                "message": "Steam did not confirm the action in time.",
            }
        finally:
            if self._pending_action.token == token:
                self._pending_action = None

    def _natural_default(self, rows: list[Rom], live_ids: set[int]) -> int | None:
        candidates = [
            {
                "rom_id": row.rom_id,
                "is_main_sibling": row.is_main_sibling,
                "regions": list(row.regions),
                "revision": row.revision,
                "tags": list(row.tags),
                "fs_name_no_ext": row.fs_name,
            }
            for row in rows
            if row.rom_id in live_ids
        ]
        try:
            return resolve_group_representative(
                candidates,
                installed_rom_ids=set(),
                bound_rom_ids=set(),
                preferred_region=self._settings.get("preferred_region", AUTO_REGION),
            )
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _fresh_guard(
        verdicts: dict[int, dict[str, str]], delete_ids: set[int], target_id: int | None
    ) -> tuple[str, str] | None:
        for rom_id in sorted(delete_ids):
            verdict = verdicts[rom_id]
            if verdict["status"] != "vanished":
                return verdict["reason"], f"ROM {rom_id}: {verdict['message']} Nothing else in this group was removed."
        if target_id is not None and verdicts[target_id]["status"] != "live":
            verdict = verdicts[target_id]
            return verdict["reason"], f"Default target {target_id}: {verdict['message']}"
        return None

    async def _emit_progress(
        self,
        run_id: str,
        current: int,
        total: int,
        stage: str,
        rows: list[Rom],
        *,
        bundle_path: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "run_id": run_id,
            "current": current,
            "total": total,
            "stage": stage,
            "rom_ids": [row.rom_id for row in rows],
            "name": rows[0].name if rows else "",
        }
        if bundle_path is not None:
            payload["bundle_path"] = bundle_path
        await self._emit("prune_progress", payload)

    @staticmethod
    def _group_result(
        rows: list[Rom],
        status: str,
        reason: str | None,
        message: object,
        *,
        removed_rom_ids: list[int] | None = None,
        app_id: int | None = None,
        bundle_path: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "group_id": rows[0].sibling_group_key or f"rom:{rows[0].rom_id}",
            "rom_ids": [row.rom_id for row in rows],
            "status": status,
            "message": str(message),
        }
        if reason is not None:
            result["reason"] = reason
        if removed_rom_ids is not None:
            result["removed_rom_ids"] = removed_rom_ids
        if app_id is not None:
            result["app_id"] = app_id
        if bundle_path is not None:
            result["bundle_path"] = bundle_path
        return result

    @staticmethod
    def _failure(reason: str, message: str) -> dict[str, Any]:
        return {"success": False, "reason": reason, "message": message}
