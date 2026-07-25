"""Per-group state machine for explicit vanished-ROM cleanup."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from domain.prune import selected_prune_ids
from domain.sibling_resolution import AUTO_REGION, fs_name_stem, resolve_group_representative
from lib.errors import RommNotFoundError, classify_error
from lib.list_result import ErrorCode
from services.prune._models import PruneOptions, PrunePreview, RecoveryHandle

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncIterator

    from domain.rom import Rom
    from services.protocols import (
        ActiveDownloadRomIdsFn,
        InstalledRomFilesRemoverFn,
        PruneArtifactStore,
        PruneSaveCoordinator,
        RecoveryBundleStore,
        RommRomReader,
        SaveDriftProbeFn,
        SteamRecoveryStore,
        VersionSwitcherFn,
    )
    from services.prune.recovery import RecoveryCoordinator
    from services.prune.registry import PruneRegistry

_LIVENESS_CONCURRENCY = 4
_COMPLETION_CHUNK_SIZE = 25
_COMPLETION_IDS_PER_GROUP = 50
_COMPLETION_TEXT_CHARS = 1024

ActionRequester = Callable[[str, str, dict[str, object], int | None, int | None], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PruneExecutorConfig:
    """Dependencies for one cleanup run's per-group state machine."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: Callable[..., Awaitable[None]]
    romm_api: RommRomReader
    recovery_store: RecoveryBundleStore
    prune_artifacts: PruneArtifactStore
    steam_recovery: SteamRecoveryStore
    save_coordinator: PruneSaveCoordinator
    active_downloads: ActiveDownloadRomIdsFn
    drift_probe: SaveDriftProbeFn
    remove_installed_files: InstalledRomFilesRemoverFn
    switch_version: VersionSwitcherFn
    settings: dict[str, Any]
    recovery: RecoveryCoordinator
    registry: PruneRegistry
    request_action: ActionRequester


class _CancelledWithResult(asyncio.CancelledError):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__()
        self.result = result


class PruneExecutor:
    """Own liveness, recovery, frontend actions, and final cleanup sequencing."""

    def __init__(self, *, config: PruneExecutorConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._romm_api = config.romm_api
        self._recovery_store = config.recovery_store
        self._prune_artifacts = config.prune_artifacts
        self._steam_recovery = config.steam_recovery
        self._save_coordinator = config.save_coordinator
        self._active_downloads = config.active_downloads
        self._drift_probe = config.drift_probe
        self._remove_installed_files = config.remove_installed_files
        self._switch_version = config.switch_version
        self._settings = config.settings
        self._recovery = config.recovery
        self._registry = config.registry
        self._request_action = config.request_action

    async def run(self, run_id: str, preview: PrunePreview, options: PruneOptions) -> None:
        """Execute every candidate group and emit bounded terminal chunks."""
        results: list[dict[str, Any]] = []
        cancelled = False
        terminal_reason: str | None = None
        terminal_message: str | None = None
        try:
            groups = await self._loop.run_in_executor(
                None, self._registry.groups_for_candidates, set(preview.candidate_ids)
            )
            total = len(groups)
            for index, rows in enumerate(groups, start=1):
                await self._emit_progress(run_id, index, total, "checking", rows)
                try:
                    result = await self._run_group(
                        run_id,
                        rows,
                        set(preview.candidate_ids),
                        options,
                        index,
                        total,
                    )
                except _CancelledWithResult as exc:
                    results.append(exc.result)
                    raise asyncio.CancelledError from exc
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._logger.exception(f"Vanished-ROM cleanup group {rows[0].rom_id} failed")
                    result = self._group_result(rows, "failed", ErrorCode.UNKNOWN.value, str(exc))
                results.append(result)
        except asyncio.CancelledError:
            cancelled = True
            terminal_reason = "cancelled"
            terminal_message = "Cleanup was cancelled; no unstarted destructive phase was run."
        except Exception as exc:
            self._logger.exception("Vanished-ROM cleanup failed")
            terminal_reason = ErrorCode.UNKNOWN.value
            terminal_message = str(exc)
        await self._emit_completion(
            run_id,
            results,
            cancelled=cancelled,
            reason=terminal_reason,
            message=terminal_message,
        )
        if cancelled:
            raise asyncio.CancelledError

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
        if not live_ids and uncertain_ids:
            return self._group_result(
                rows,
                "skipped",
                "liveness_uncertain",
                f"RomM could not confirm {len(uncertain_ids)} group member(s); nothing was removed.",
            )

        delete_ids = selected_prune_ids(
            group_ids=sorted(group_ids),
            candidate_ids=candidate_ids,
            vanished_ids=vanished_ids,
            live_ids=live_ids,
            remove_rows=options.remove_rows,
            remove_fully_vanished=options.remove_fully_vanished,
        )
        bound_row = bound[0] if bound else None
        target_id: int | None = None
        if bound_row is not None and bound_row.rom_id in vanished_ids and live_ids and options.repoint_shortcuts:
            target_id = self._natural_default(rows, live_ids)
            if target_id is None:
                return self._group_result(rows, "skipped", "no_live_default", "No live default could be selected.")
        if bound_row is not None and bound_row.rom_id in delete_ids and live_ids and target_id is None:
            delete_ids.remove(bound_row.rom_id)

        whole_game_action = fully_dead and bool(delete_ids)
        if not delete_ids and target_id is None:
            return self._group_result(
                rows, "skipped", "options_excluded", "No confirmed rows matched the selected options."
            )

        drifted = False
        if bound_row is not None and bound_row.rom_id in vanished_ids and (target_id is not None or whole_game_action):
            drift = await self._drift_probe(bound_row.rom_id)
            drifted = bool(drift.get("drifted"))
            if drifted and not options.create_recovery_bundle:
                return self._group_result(
                    rows,
                    "skipped",
                    "unsynced_saves",
                    "Unsynced saves require a sealed recovery bundle before changing this shortcut.",
                )

        app_id = bound_row.shortcut_app_id if bound_row is not None else None
        frontend_steam: dict[str, object] | None = None
        cancel_requested = False
        if whole_game_action and app_id is not None and options.create_recovery_bundle:
            if bound_row is None:
                raise RuntimeError("Bound shortcut state disappeared before snapshot capture")
            capture = await self._request_action(
                run_id,
                "capture_shortcut_snapshot",
                {"app_id": app_id},
                bound_row.rom_id,
                None,
            )
            cancel_requested |= bool(capture.pop("_cancelled", False))
            if not capture.get("success") or not isinstance(capture.get("snapshot"), dict):
                return self._group_result(
                    rows,
                    "failed",
                    "steam_snapshot_failed",
                    capture.get("message", "Steam snapshot failed."),
                )
            frontend_steam = capture["snapshot"]
            if cancel_requested:
                raise asyncio.CancelledError

        recovery_ids = set(delete_ids)
        if target_id is not None and bound_row is not None:
            recovery_ids.add(bound_row.rom_id)
            recovery_ids.add(target_id)
        handle: RecoveryHandle | None = None
        if options.create_recovery_bundle:
            await self._emit_progress(run_id, index, total, "creating_recovery", rows)
            try:
                async with self._stable_save_locks(recovery_ids) as save_inventory:
                    locked_rows = await self._loop.run_in_executor(
                        None, self._registry.reread_group, initial_rows[0].rom_id
                    )
                    if not locked_rows or not recovery_ids <= {row.rom_id for row in locked_rows}:
                        return self._group_result(
                            rows, "skipped", "local_state_changed", "The local group changed before recovery."
                        )
                    snapshot = await self._loop.run_in_executor(
                        None, self._recovery.snapshot_state, sorted(recovery_ids), frontend_steam
                    )
                    bundle_path, steam_backend = await self._loop.run_in_executor(
                        None,
                        lambda: self._recovery.seal(
                            rows=[row for row in locked_rows if row.rom_id in recovery_ids],
                            snapshot=snapshot,
                            save_inventory=save_inventory,
                            include_installed_rom_ids=set(options.include_installed_rom_ids),
                            delete_ids=delete_ids,
                            app_id=app_id if whole_game_action else None,
                        ),
                    )
                    handle = RecoveryHandle(bundle_path, snapshot, save_inventory, steam_backend)
            except Exception as exc:
                self._logger.error(f"Recovery bundle failed for group {min(group_ids)}: {exc}")
                return self._group_result(rows, "failed", "recovery_failed", str(exc))
            await self._emit_progress(
                run_id,
                index,
                total,
                "recovery_sealed",
                rows,
                bundle_path=handle.bundle_path,
            )

        if self._active_downloads() & delete_ids:
            return self._group_result(rows, "skipped", "download_in_progress", "Cancel active downloads first.")
        refreshed = await self._probe_many(delete_ids | ({target_id} if target_id is not None else set()))
        guard = self._fresh_guard(refreshed, delete_ids, target_id)
        if guard is not None:
            return self._group_result(
                rows,
                "skipped",
                guard[0],
                guard[1],
                bundle_path=handle.bundle_path if handle else None,
            )

        committed_action: Literal["repoint_shortcut", "remove_shortcut"] | None = None
        launch_options: str | None = None
        if target_id is not None and app_id is not None and bound_row is not None:
            switch, cancelled = await self._shielded(
                self._switch_version(app_id, target_id, drifted and handle is not None)
            )
            cancel_requested |= cancelled
            if not switch.get("success"):
                result = self._group_result(
                    rows,
                    "failed",
                    switch.get("reason", "repoint_failed"),
                    switch.get("message", "Repoint failed."),
                    bundle_path=handle.bundle_path if handle else None,
                )
                return self._cancel_or_return(result, cancel_requested)
            if (
                switch.get("rom_id") != target_id
                or switch.get("app_id") != app_id
                or not isinstance(switch.get("launch_options"), str)
            ):
                result = self._group_result(
                    rows,
                    "partial",
                    "repoint_result_invalid",
                    "The binding changed but the switch result was incomplete.",
                    app_id=app_id,
                    bundle_path=handle.bundle_path if handle else None,
                    committed_action="repoint_shortcut",
                )
                return self._cancel_or_return(result, cancel_requested)
            launch_options = switch["launch_options"]
            committed_action = "repoint_shortcut"
            _, cancelled = await self._shielded(
                self._emit_progress(
                    run_id, index, total, "repointing", rows, bundle_path=handle.bundle_path if handle else None
                )
            )
            cancel_requested |= cancelled
            action = await self._request_action(
                run_id,
                "repoint_shortcut",
                {
                    "app_id": app_id,
                    "target_rom_id": target_id,
                    "launch_options": launch_options,
                    "target_installed": bool(switch.get("target_installed")),
                },
                bound_row.rom_id,
                target_id,
            )
            cancel_requested |= bool(action.pop("_cancelled", False))
            if not action.get("success"):
                result = self._group_result(
                    rows,
                    "partial",
                    "steam_action_failed",
                    action.get("message", "The binding changed but Steam confirmation failed."),
                    app_id=app_id,
                    bundle_path=handle.bundle_path if handle else None,
                    committed_action=committed_action,
                )
                return self._cancel_or_return(result, cancel_requested)
        elif whole_game_action and app_id is not None and bound_row is not None:
            await self._emit_progress(
                run_id,
                index,
                total,
                "removing_shortcut",
                rows,
                bundle_path=handle.bundle_path if handle else None,
            )
            action = await self._request_action(
                run_id,
                "remove_shortcut",
                {"app_id": app_id},
                bound_row.rom_id,
                None,
            )
            cancel_requested |= bool(action.pop("_cancelled", False))
            if not action.get("success"):
                result = self._group_result(
                    rows,
                    "failed",
                    "steam_action_failed",
                    action.get("message", "Shortcut removal failed."),
                    bundle_path=handle.bundle_path if handle else None,
                )
                return self._cancel_or_return(result, cancel_requested)
            reconciled, cancelled = await self._shielded(
                self._loop.run_in_executor(None, self._registry.reconcile_removed_shortcut, bound_row.rom_id, app_id)
            )
            cancel_requested |= cancelled
            committed_action = "remove_shortcut"
            if not reconciled:
                result = self._group_result(
                    rows,
                    "partial",
                    "local_state_changed",
                    "Steam removed the shortcut, but its local binding changed before reconciliation.",
                    app_id=app_id,
                    removed_app_id=app_id,
                    bundle_path=handle.bundle_path if handle else None,
                    committed_action=committed_action,
                )
                return self._cancel_or_return(result, cancel_requested)

        if target_id is not None and not delete_ids:
            result = self._group_result(
                rows,
                "repointed",
                None,
                "Repointed the shortcut to the live Default.",
                app_id=app_id,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
            )
            return self._cancel_or_return(result, cancel_requested)

        post_action = self._finish_group(
            run_id=run_id,
            initial_rows=initial_rows,
            delete_ids=delete_ids,
            target_id=target_id,
            app_id=app_id,
            fully_dead=fully_dead,
            committed_action=committed_action,
            handle=handle,
            recovery_ids=recovery_ids,
            index=index,
            total=total,
            launch_options=launch_options,
        )
        result, cancelled = await self._shielded(post_action)
        cancel_requested |= cancelled
        return self._cancel_or_return(result, cancel_requested)

    async def _finish_group(
        self,
        *,
        run_id: str,
        initial_rows: list[Rom],
        delete_ids: set[int],
        target_id: int | None,
        app_id: int | None,
        fully_dead: bool,
        committed_action: str | None,
        handle: RecoveryHandle | None,
        recovery_ids: set[int],
        index: int,
        total: int,
        launch_options: str | None,
    ) -> dict[str, Any]:
        acted_app_id = app_id if committed_action is not None else None
        refreshed = await self._probe_many(delete_ids | ({target_id} if target_id is not None else set()))
        guard = self._fresh_guard(refreshed, delete_ids, target_id)
        if guard is not None:
            return self._group_result(
                initial_rows,
                "partial" if committed_action else "skipped",
                guard[0],
                guard[1],
                app_id=acted_app_id,
                removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
            )
        if self._active_downloads() & delete_ids:
            return self._group_result(
                initial_rows,
                "partial" if committed_action else "skipped",
                "download_in_progress",
                "A download became active; source data was retained.",
                app_id=acted_app_id,
                removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
            )

        rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
        if not rows or not delete_ids <= {row.rom_id for row in rows}:
            return self._group_result(
                initial_rows,
                "partial" if committed_action else "skipped",
                "local_state_changed",
                "Local state changed; source data was retained.",
                app_id=acted_app_id,
                removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
            )

        await self._emit_progress(
            run_id,
            index,
            total,
            "removing",
            rows,
            bundle_path=handle.bundle_path if handle else None,
        )
        mutations: list[str] = []
        async with self._stable_save_locks(delete_ids) as save_inventory:
            rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
            expected_app_id = app_id if target_id is not None else None
            if not rows or not await self._loop.run_in_executor(
                None,
                self._registry.validate_deletion_state,
                rows,
                delete_ids,
                target_id,
                expected_app_id,
                fully_dead,
            ):
                return self._group_result(
                    initial_rows,
                    "partial" if committed_action else "skipped",
                    "local_state_changed",
                    "Final local revalidation failed before source removal.",
                    app_id=acted_app_id,
                    removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                    bundle_path=handle.bundle_path if handle else None,
                    committed_action=committed_action,
                )
            if handle is not None:
                state_matches = await self._loop.run_in_executor(
                    None,
                    self._recovery.state_matches,
                    handle.snapshot,
                    sorted(recovery_ids),
                    committed_action,
                    app_id,
                    target_id,
                    launch_options,
                )
                sources_match = await self._loop.run_in_executor(
                    None, self._recovery_store.validate_sources, handle.bundle_path
                )
                if save_inventory != handle.save_inventory or not state_matches or not sources_match:
                    return self._group_result(
                        rows,
                        "partial" if committed_action else "skipped",
                        "recovery_state_changed",
                        "Local state no longer matches the sealed recovery bundle; source data was retained.",
                        app_id=acted_app_id,
                        removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                        bundle_path=handle.bundle_path,
                        committed_action=committed_action,
                    )

            quarantine = await self._loop.run_in_executor(
                None, self._save_coordinator.quarantine_prune_saves, save_inventory["exclusive"]
            )
            raw_moved = quarantine.get("moved")
            moved = [str(path) for path in raw_moved] if isinstance(raw_moved, list) else []
            if moved:
                mutations.append("save_quarantine")
            if not quarantine.get("success"):
                return self._group_result(
                    rows,
                    "partial" if mutations or committed_action else "failed",
                    "save_quarantine_failed",
                    quarantine.get("message", "Save quarantine failed."),
                    app_id=acted_app_id,
                    removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                    bundle_path=handle.bundle_path if handle else None,
                    committed_action=committed_action,
                    mutations=mutations,
                )

            for rom_id in sorted(delete_ids):
                removal = await self._loop.run_in_executor(None, self._remove_installed_files, rom_id)
                if removal.get("success"):
                    if "installed_rom_content" not in mutations:
                        mutations.append("installed_rom_content")
                    continue
                if removal.get("reason") == "not_installed":
                    continue
                return self._group_result(
                    rows,
                    "partial" if mutations or committed_action else "failed",
                    "rom_removal_failed",
                    removal.get("message", "ROM removal failed."),
                    app_id=acted_app_id,
                    removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                    bundle_path=handle.bundle_path if handle else None,
                    committed_action=committed_action,
                    mutations=mutations,
                )
            try:
                await self._loop.run_in_executor(None, self._prune_artifacts.remove, sorted(delete_ids))
                mutations.append("plugin_artifacts")
                if committed_action == "remove_shortcut" and app_id is not None and handle is not None:
                    if handle.steam_backend is None:
                        raise RuntimeError("Steam recovery identity was not captured")
                    await self._loop.run_in_executor(
                        None, self._steam_recovery.remove_state, app_id, handle.steam_backend
                    )
                    mutations.append("steam_files")
            except Exception as exc:
                return self._group_result(
                    rows,
                    "partial",
                    "artifact_cleanup_failed",
                    str(exc),
                    app_id=acted_app_id,
                    removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                    bundle_path=handle.bundle_path if handle else None,
                    committed_action=committed_action,
                    mutations=mutations,
                )

            deleted = await self._loop.run_in_executor(
                None,
                self._registry.delete_rows,
                rows,
                delete_ids,
                target_id,
                expected_app_id,
                fully_dead,
            )
            if not deleted:
                return self._group_result(
                    rows,
                    "partial",
                    "local_state_changed",
                    "Final local revalidation failed after filesystem cleanup.",
                    app_id=acted_app_id,
                    removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                    bundle_path=handle.bundle_path if handle else None,
                    committed_action=committed_action,
                    mutations=mutations,
                )

        await self._emit_progress(
            run_id, index, total, "removed", rows, bundle_path=handle.bundle_path if handle else None
        )
        return self._group_result(
            rows,
            "removed",
            None,
            f"Removed {len(delete_ids)} confirmed vanished entr{'y' if len(delete_ids) == 1 else 'ies'}.",
            removed_rom_ids=sorted(delete_ids),
            app_id=acted_app_id,
            removed_app_id=app_id if committed_action == "remove_shortcut" else None,
            bundle_path=handle.bundle_path if handle else None,
            committed_action=committed_action,
            mutations=mutations,
        )

    @contextlib.asynccontextmanager
    async def _stable_save_locks(self, rom_ids: set[int]) -> AsyncIterator[dict[str, Any]]:
        requested = sorted(rom_ids)
        for _attempt in range(3):
            first = await self._loop.run_in_executor(None, self._save_coordinator.inventory_prune_saves, requested)
            lock_ids = sorted({int(value) for value in first.get("lock_rom_ids", requested)})
            async with self._save_coordinator.lock_prune_roms(lock_ids):
                current = await self._loop.run_in_executor(
                    None, self._save_coordinator.inventory_prune_saves, requested
                )
                current_locks = sorted({int(value) for value in current.get("lock_rom_ids", requested)})
                if current_locks == lock_ids:
                    yield current
                    return
        raise RuntimeError("Save ownership kept changing while cleanup acquired locks")

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

    def _natural_default(self, rows: list[Rom], live_ids: set[int]) -> int | None:
        candidates = [
            {
                "rom_id": row.rom_id,
                "is_main_sibling": row.is_main_sibling,
                "regions": list(row.regions),
                "revision": row.revision,
                "tags": list(row.tags),
                "fs_name_no_ext": fs_name_stem(row.fs_name),
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

    async def _shielded(self, awaitable: Awaitable[Any]) -> tuple[Any, bool]:
        task = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.shield(task), False
        except asyncio.CancelledError:
            return await task, True

    @staticmethod
    def _cancel_or_return(result: dict[str, Any], cancelled: bool) -> dict[str, Any]:
        if cancelled:
            raise _CancelledWithResult(result)
        return result

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
            "rom_ids": [row.rom_id for row in rows[:_COMPLETION_IDS_PER_GROUP]],
            "rom_count": len(rows),
            "rom_ids_truncated": len(rows) > _COMPLETION_IDS_PER_GROUP,
            "name": (rows[0].name if rows else "")[:_COMPLETION_TEXT_CHARS],
        }
        if bundle_path is not None:
            payload["bundle_path"] = bundle_path
        await self._emit("prune_progress", payload)

    async def _emit_completion(
        self,
        run_id: str,
        results: list[dict[str, Any]],
        *,
        cancelled: bool,
        reason: str | None,
        message: str | None,
    ) -> None:
        failures = [result for result in results if result["status"] in {"failed", "skipped", "partial"}]
        removed_count = sum(
            int(result.get("removed_count", len(result.get("removed_rom_ids", [])))) for result in results
        )
        partial = any(result["status"] == "partial" for result in results) or bool(removed_count and failures)
        chunks = [
            results[index : index + _COMPLETION_CHUNK_SIZE] for index in range(0, len(results), _COMPLETION_CHUNK_SIZE)
        ]
        if not chunks:
            chunks = [[]]
        for chunk_index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "success": not failures and not cancelled and reason is None,
                "partial": partial,
                "run_id": run_id,
                "chunk_index": chunk_index,
                "final": chunk_index == len(chunks) - 1,
                "removed_count": removed_count,
                "problem_count": len(failures),
                "removed_rom_ids": sorted(
                    {int(value) for result in chunk for value in result.get("removed_rom_ids", [])}
                ),
                "affected_app_ids": sorted(
                    {int(result["app_id"]) for result in chunk if type(result.get("app_id")) is int}
                ),
                "removed_app_ids": sorted(
                    {int(result["removed_app_id"]) for result in chunk if type(result.get("removed_app_id")) is int}
                ),
                "results": chunk,
            }
            if reason is not None:
                payload["reason"] = reason[:_COMPLETION_TEXT_CHARS]
            if message is not None:
                payload["message"] = message[:_COMPLETION_TEXT_CHARS]
            await self._emit("prune_complete", payload)

    @staticmethod
    def _group_result(
        rows: list[Rom],
        status: str,
        reason: str | None,
        message: object,
        *,
        removed_rom_ids: list[int] | None = None,
        app_id: int | None = None,
        removed_app_id: int | None = None,
        bundle_path: str | None = None,
        committed_action: str | None = None,
        mutations: list[str] | None = None,
    ) -> dict[str, Any]:
        raw_group_id = rows[0].sibling_group_key or f"rom:{rows[0].rom_id}"
        all_rom_ids = [row.rom_id for row in rows]
        bounded_removed = (removed_rom_ids or [])[:_COMPLETION_IDS_PER_GROUP]
        raw_message = str(message)
        result: dict[str, Any] = {
            "group_id": raw_group_id[:_COMPLETION_TEXT_CHARS],
            "group_id_truncated": len(raw_group_id) > _COMPLETION_TEXT_CHARS,
            "rom_ids": all_rom_ids[:_COMPLETION_IDS_PER_GROUP],
            "rom_count": len(all_rom_ids),
            "rom_ids_truncated": len(all_rom_ids) > _COMPLETION_IDS_PER_GROUP,
            "status": status,
            "message": raw_message[:_COMPLETION_TEXT_CHARS],
            "message_truncated": len(raw_message) > _COMPLETION_TEXT_CHARS,
        }
        if reason is not None:
            result["reason"] = reason
        if removed_rom_ids is not None:
            result["removed_rom_ids"] = bounded_removed
            result["removed_count"] = len(removed_rom_ids)
            result["removed_rom_ids_truncated"] = len(removed_rom_ids) > _COMPLETION_IDS_PER_GROUP
        if app_id is not None:
            result["app_id"] = app_id
        if removed_app_id is not None:
            result["removed_app_id"] = removed_app_id
        if bundle_path is not None:
            result["bundle_path"] = bundle_path
        if committed_action is not None:
            result["committed_action"] = committed_action
        if mutations:
            result["mutations"] = mutations
        return result
