"""Per-group state machine for explicit vanished-ROM cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from domain.prune import selected_prune_ids
from domain.sibling_resolution import AUTO_REGION, fs_name_stem, resolve_group_representative
from lib.errors import RommNotFoundError, classify_error
from lib.list_result import ErrorCode
from lib.url_host import romm_namespace
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
_COMPLETION_IDS_PER_GROUP = 50
_COMPLETION_TEXT_CHARS = 512
_COMPLETION_PATH_CHARS = 2048
_COMPLETION_REASON_CHARS = 128
_COMPLETION_WARNING_CHARS = 256
_COMPLETION_WARNINGS_PER_GROUP = 5
_COMPLETION_BUDGET_BYTES = 48 * 1024

ActionRequester = Callable[[str, str, dict[str, object], int | None, int | None, set[int]], Awaitable[dict[str, Any]]]


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


class _ChildFaultAfterCancellation(asyncio.CancelledError):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error


@dataclass
class _MutationLedger:
    rows: list[Rom]
    app_id: int | None = None
    target_rom_id: int | None = None
    bundle_path: str | None = None
    committed_action: str | None = None
    action_ambiguous: bool = False
    mutations: list[str] = field(default_factory=list)
    ambiguous_mutations: list[str] = field(default_factory=list)

    def has_commit(self) -> bool:
        return (
            self.committed_action is not None
            or self.action_ambiguous
            or bool(self.mutations)
            or bool(self.ambiguous_mutations)
        )


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
        self._run_namespace: str | None = None

    async def run(self, run_id: str, preview: PrunePreview, options: PruneOptions) -> None:
        """Execute every candidate group and emit bounded terminal chunks."""
        results: list[dict[str, Any]] = []
        cancelled = False
        terminal_reason: str | None = None
        terminal_message: str | None = None
        self._run_namespace = preview.server_namespace
        try:
            if romm_namespace(self._settings) != preview.server_namespace:
                raise RuntimeError("The RomM server or user changed after cleanup preview.")
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
        try:
            await self._emit_completion(
                run_id,
                results,
                cancelled=cancelled,
                reason=terminal_reason,
                message=terminal_message,
            )
        finally:
            self._run_namespace = None
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
        ledger = _MutationLedger(initial_rows)
        try:
            return await self._run_group_inner(
                run_id, initial_rows, preview_candidate_ids, options, index, total, ledger
            )
        except _ChildFaultAfterCancellation as exc:
            raise _CancelledWithResult(self._fault_result(ledger, initial_rows, exc.error)) from exc
        except _CancelledWithResult:
            raise
        except asyncio.CancelledError as exc:
            if ledger.has_commit():
                raise _CancelledWithResult(
                    self._ledger_result(
                        ledger,
                        "cancelled",
                        "Cleanup was cancelled after a committed or ambiguous action; later groups were not started.",
                    )
                ) from exc
            raise
        except Exception as exc:
            if ledger.has_commit():
                self._logger.exception(f"Vanished-ROM cleanup group {initial_rows[0].rom_id} failed after mutation")
                return self._ledger_result(ledger, ErrorCode.UNKNOWN.value, str(exc))
            raise

    async def _run_group_inner(
        self,
        run_id: str,
        initial_rows: list[Rom],
        preview_candidate_ids: set[int],
        options: PruneOptions,
        index: int,
        total: int,
        ledger: _MutationLedger,
    ) -> dict[str, Any]:
        rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
        ledger.rows = rows or initial_rows
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
            namespace_changed = any(
                verdicts[rom_id]["reason"] == "server_namespace_changed" for rom_id in uncertain_ids
            )
            return self._group_result(
                rows,
                "skipped",
                "server_namespace_changed" if namespace_changed else "liveness_uncertain",
                (
                    "The RomM server or user changed during exact-ID proof; nothing was removed."
                    if namespace_changed
                    else f"RomM could not confirm {len(uncertain_ids)} group member(s); nothing was removed."
                ),
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
                group_ids,
            )
            cancel_requested |= bool(capture.pop("_cancelled", False))
            if capture.get("success") and capture.get("shortcut_absent") is True:
                ledger.app_id = app_id
                ledger.committed_action = "remove_shortcut"
                reconciled, cancelled = await self._shielded(
                    self._loop.run_in_executor(
                        None, self._registry.reconcile_removed_shortcut, bound_row.rom_id, app_id
                    )
                )
                if reconciled:
                    ledger.mutations.append("shortcut_binding")
                result = self._ledger_result(
                    ledger,
                    "shortcut_absence_reconciled" if reconciled else "local_state_changed",
                    (
                        "Steam already lacked this shortcut; its local binding was reconciled. Run cleanup again."
                        if reconciled
                        else "Steam lacked the shortcut, but its local binding changed before reconciliation."
                    ),
                    removed_app_id=app_id if reconciled else None,
                )
                return self._cancel_or_return(result, cancel_requested or cancelled)
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
                    sealed, seal_cancelled = await self._shielded(
                        self._loop.run_in_executor(
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
                    )
                    bundle_path, steam_backend = sealed
                    sealed_claims = await self._loop.run_in_executor(
                        None, self._recovery_store.source_claims, bundle_path
                    )
                    handle = RecoveryHandle(
                        bundle_path,
                        snapshot,
                        save_inventory,
                        steam_backend,
                        sealed_claims["claims"],
                        sealed_claims["bundle_digest"],
                    )
                    ledger.bundle_path = bundle_path
                    if seal_cancelled:
                        raise asyncio.CancelledError
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
        proof_ids = set(delete_ids)
        if target_id is not None:
            proof_ids.add(target_id)
            if bound_row is not None:
                proof_ids.add(bound_row.rom_id)
        refreshed = await self._probe_many(proof_ids)
        guard = self._fresh_guard(
            refreshed,
            delete_ids,
            target_id,
            bound_row.rom_id if target_id is not None and bound_row is not None else None,
        )
        if guard is not None:
            return self._group_result(
                rows,
                "skipped",
                guard[0],
                guard[1],
                bundle_path=handle.bundle_path if handle else None,
            )

        if handle is not None:
            recovery_guard = await self._recovery_guard(
                handle,
                recovery_ids,
                committed_action=None,
                app_id=app_id,
                target_id=target_id,
                launch_options=None,
            )
            if recovery_guard is not None:
                return self._group_result(
                    rows,
                    "skipped",
                    "recovery_state_changed",
                    recovery_guard,
                    bundle_path=handle.bundle_path,
                )

        committed_action: Literal["repoint_shortcut", "remove_shortcut"] | None = None
        launch_options: str | None = None
        if target_id is not None and app_id is not None and bound_row is not None:
            ledger.app_id = app_id
            ledger.committed_action = "repoint_shortcut"
            ledger.action_ambiguous = True
            switch, cancelled = await self._shielded(
                self._switch_version(app_id, target_id, drifted and handle is not None)
            )
            cancel_requested |= cancelled
            if not switch.get("success"):
                ledger.committed_action = None
                ledger.action_ambiguous = False
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
                result = self._ledger_result(
                    ledger,
                    "repoint_result_invalid",
                    "The binding changed but the switch result was incomplete.",
                )
                return self._cancel_or_return(result, cancel_requested)
            launch_options = switch["launch_options"]
            committed_action = "repoint_shortcut"
            ledger.target_rom_id = target_id
            ledger.action_ambiguous = False
            ledger.mutations.append("shortcut_binding")
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
                group_ids,
            )
            cancel_requested |= bool(action.pop("_cancelled", False))
            if not action.get("success"):
                if action.get("mutation_attempted") is True or action.get("reason") == "action_ambiguous":
                    ledger.action_ambiguous = True
                    result = self._ledger_result(
                        ledger,
                        "action_ambiguous",
                        action.get("message", "The binding changed but Steam confirmation is unknown."),
                    )
                else:
                    result = self._ledger_result(
                        ledger,
                        "steam_action_failed",
                        action.get("message", "The binding changed but Steam confirmation failed."),
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
                {
                    "app_id": app_id,
                    **({"expected_snapshot": frontend_steam} if isinstance(frontend_steam, dict) else {}),
                },
                bound_row.rom_id,
                None,
                group_ids,
            )
            cancel_requested |= bool(action.pop("_cancelled", False))
            if not action.get("success"):
                if action.get("mutation_attempted") is True or action.get("reason") == "action_ambiguous":
                    ledger.app_id = app_id
                    ledger.committed_action = "remove_shortcut"
                    ledger.action_ambiguous = True
                    result = self._ledger_result(
                        ledger,
                        "action_ambiguous",
                        "Steam removal was claimed but its outcome is unknown; source data was retained.",
                    )
                else:
                    result = self._group_result(
                        rows,
                        "failed",
                        "steam_action_failed",
                        action.get("message", "Shortcut removal failed."),
                        bundle_path=handle.bundle_path if handle else None,
                    )
                return self._cancel_or_return(result, cancel_requested)
            ledger.app_id = app_id
            ledger.committed_action = "remove_shortcut"
            ledger.action_ambiguous = False
            reconciled, cancelled = await self._shielded(
                self._loop.run_in_executor(None, self._registry.reconcile_removed_shortcut, bound_row.rom_id, app_id)
            )
            cancel_requested |= cancelled
            committed_action = "remove_shortcut"
            if reconciled:
                ledger.mutations.append("shortcut_binding")
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
            final_proof = await self._probe_many(
                {bound_row.rom_id, target_id} if bound_row is not None else {target_id}
            )
            final_guard = self._fresh_guard(
                final_proof,
                set(),
                target_id,
                bound_row.rom_id if bound_row is not None else None,
            )
            if final_guard is not None:
                result = self._ledger_result(ledger, final_guard[0], final_guard[1])
                return self._cancel_or_return(result, cancel_requested)
            result = self._group_result(
                rows,
                "repointed",
                None,
                "Repointed the shortcut to the live Default.",
                app_id=app_id,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                target_rom_id=target_id,
            )
            return self._cancel_or_return(result, cancel_requested)

        try:
            result = await self._finish_group(
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
                ledger=ledger,
                vanished_source_id=bound_row.rom_id if target_id is not None and bound_row is not None else None,
            )
        except _ChildFaultAfterCancellation as exc:
            raise _CancelledWithResult(self._fault_result(ledger, initial_rows, exc.error)) from exc
        except _CancelledWithResult:
            raise
        except asyncio.CancelledError as exc:
            if ledger.has_commit():
                raise _CancelledWithResult(
                    self._ledger_result(
                        ledger,
                        "cancelled",
                        "Cleanup was cancelled before local finalization; later groups were not started.",
                    )
                ) from exc
            raise
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
        ledger: _MutationLedger,
        vanished_source_id: int | None,
    ) -> dict[str, Any]:
        refreshed = await self._probe_many(
            delete_ids
            | ({target_id} if target_id is not None else set())
            | ({vanished_source_id} if vanished_source_id is not None else set())
        )
        guard = self._fresh_guard(refreshed, delete_ids, target_id, vanished_source_id)
        if guard is not None:
            return self._ledger_or_guard_result(ledger, initial_rows, guard, handle, app_id)
        if self._active_downloads() & delete_ids:
            return self._ledger_or_guard_result(
                ledger,
                initial_rows,
                ("download_in_progress", "A download became active; source data was retained."),
                handle,
                app_id,
            )

        rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
        if not rows or not delete_ids <= {row.rom_id for row in rows}:
            return self._ledger_or_guard_result(
                ledger,
                initial_rows,
                ("local_state_changed", "Local state changed; source data was retained."),
                handle,
                app_id,
            )
        ledger.rows = rows
        await self._emit_progress(
            run_id, index, total, "removing", rows, bundle_path=handle.bundle_path if handle else None
        )

        async with self._stable_save_locks(recovery_ids) as recovery_inventory:
            rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
            ledger.rows = rows or initial_rows
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
                return self._ledger_or_guard_result(
                    ledger,
                    initial_rows,
                    ("local_state_changed", "Final local revalidation failed before source removal."),
                    handle,
                    app_id,
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
                    None,
                    partial(self._recovery_store.validate_sources, handle.bundle_path, handle.bundle_digest),
                )
                backend_matches = True
                if committed_action == "remove_shortcut" and app_id is not None and handle.steam_backend is not None:
                    backend_matches = await self._loop.run_in_executor(
                        None, self._steam_recovery.validate_state, app_id, handle.steam_backend
                    )
                if (
                    recovery_inventory != handle.save_inventory
                    or not state_matches
                    or not sources_match
                    or not backend_matches
                ):
                    return self._ledger_or_guard_result(
                        ledger,
                        rows,
                        (
                            "recovery_state_changed",
                            "Local state no longer matches the sealed recovery bundle; source data was retained.",
                        ),
                        handle,
                        app_id,
                    )

            delete_inventory = await self._loop.run_in_executor(
                None, self._save_coordinator.inventory_prune_saves, sorted(delete_ids)
            )
            delete_locks = {int(value) for value in delete_inventory.get("lock_rom_ids", delete_ids)}
            held_locks = {int(value) for value in recovery_inventory.get("lock_rom_ids", recovery_ids)}
            if not delete_locks <= held_locks:
                return self._ledger_or_guard_result(
                    ledger,
                    rows,
                    ("save_ownership_changed", "Save ownership expanded; source data was retained."),
                    handle,
                    app_id,
                    warnings=self._inventory_warnings(delete_inventory),
                )
            commit = self._commit_group(
                rows=rows,
                delete_ids=delete_ids,
                target_id=target_id,
                app_id=app_id,
                fully_dead=fully_dead,
                committed_action=committed_action,
                handle=handle,
                expected_app_id=expected_app_id,
                delete_inventory=delete_inventory,
                ledger=ledger,
            )
            result, cancelled = await self._shielded(commit)
            if cancelled:
                raise _CancelledWithResult(result)

        try:
            await self._emit_progress(
                run_id, index, total, "removed", rows, bundle_path=handle.bundle_path if handle else None
            )
        except Exception as exc:
            self._logger.warning(f"Removed-game cleanup final progress delivery failed: {exc}")
        return result

    async def _commit_group(
        self,
        *,
        rows: list[Rom],
        delete_ids: set[int],
        target_id: int | None,
        app_id: int | None,
        fully_dead: bool,
        committed_action: str | None,
        handle: RecoveryHandle | None,
        expected_app_id: int | None,
        delete_inventory: dict[str, Any],
        ledger: _MutationLedger,
    ) -> dict[str, Any]:
        acted_app_id = app_id if committed_action is not None else None
        warnings = self._inventory_warnings(delete_inventory)
        quarantine = await self._loop.run_in_executor(
            None,
            self._save_coordinator.quarantine_prune_saves,
            delete_inventory["exclusive"],
            handle.source_claims if handle is not None else delete_inventory.get("source_claims"),
        )
        raw_moved = quarantine.get("moved")
        moved = [str(path) for path in raw_moved] if isinstance(raw_moved, list) else []
        if moved:
            ledger.mutations.append("save_quarantine")
        if quarantine.get("ambiguous") and "save_quarantine" not in ledger.ambiguous_mutations:
            ledger.ambiguous_mutations.append("save_quarantine")
        if not quarantine.get("success"):
            return self._group_result(
                rows,
                "partial" if ledger.has_commit() else "failed",
                "save_quarantine_failed",
                quarantine.get("message", "Save quarantine failed."),
                app_id=acted_app_id,
                removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                mutations=ledger.mutations,
                ambiguous_mutations=ledger.ambiguous_mutations,
                warnings=warnings,
                target_rom_id=target_id,
            )

        for rom_id in sorted(delete_ids):
            claims = handle.source_claims if handle is not None else None
            removal = await self._loop.run_in_executor(None, self._remove_installed_files, rom_id, claims)
            if removal.get("changed") and "installed_rom_content" not in ledger.mutations:
                ledger.mutations.append("installed_rom_content")
            if removal.get("ambiguous") and "installed_rom_content" not in ledger.ambiguous_mutations:
                ledger.ambiguous_mutations.append("installed_rom_content")
            if removal.get("success"):
                continue
            if removal.get("reason") == "not_installed":
                continue
            return self._group_result(
                rows,
                "partial" if ledger.has_commit() else "failed",
                "rom_removal_failed",
                removal.get("message", "ROM removal failed."),
                app_id=acted_app_id,
                removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                mutations=ledger.mutations,
                ambiguous_mutations=ledger.ambiguous_mutations,
                warnings=warnings,
                target_rom_id=target_id,
            )
        try:
            claims = handle.source_claims if handle is not None else None
            artifact_outcome = await self._loop.run_in_executor(
                None, self._prune_artifacts.remove, sorted(delete_ids), claims
            )
            if artifact_outcome.get("changed"):
                ledger.mutations.append("plugin_artifacts")
            if artifact_outcome.get("ambiguous"):
                ledger.ambiguous_mutations.append("plugin_artifacts")
            if not artifact_outcome.get("success"):
                raise RuntimeError(artifact_outcome.get("message", "Plugin artifact cleanup failed"))
            if committed_action == "remove_shortcut" and app_id is not None and handle is not None:
                if handle.steam_backend is None:
                    raise RuntimeError("Steam recovery identity was not captured")
                steam_outcome = await self._loop.run_in_executor(
                    None, self._steam_recovery.remove_state, app_id, handle.steam_backend, handle.source_claims
                )
                if steam_outcome.get("changed"):
                    ledger.mutations.append("steam_files")
                if steam_outcome.get("ambiguous"):
                    ledger.ambiguous_mutations.append("steam_files")
                if not steam_outcome.get("success"):
                    raise RuntimeError(steam_outcome.get("message", "Steam state cleanup failed"))
        except Exception as exc:
            return self._group_result(
                rows,
                "partial" if ledger.has_commit() else "failed",
                "artifact_cleanup_failed",
                str(exc),
                app_id=acted_app_id,
                removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                mutations=ledger.mutations,
                ambiguous_mutations=ledger.ambiguous_mutations,
                warnings=warnings,
                target_rom_id=target_id,
            )

        try:
            deleted = await self._loop.run_in_executor(
                None, self._registry.delete_rows, rows, delete_ids, target_id, expected_app_id, fully_dead
            )
        except Exception:
            ledger.mutations.append("database_rows_ambiguous")
            raise
        if not deleted:
            return self._group_result(
                rows,
                "partial" if ledger.has_commit() else "skipped",
                "local_state_changed",
                "Final local revalidation failed after filesystem cleanup.",
                app_id=acted_app_id,
                removed_app_id=app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                mutations=ledger.mutations,
                ambiguous_mutations=ledger.ambiguous_mutations,
                warnings=warnings,
                target_rom_id=target_id,
            )
        ledger.mutations.append("database_rows")

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
            mutations=ledger.mutations,
            ambiguous_mutations=ledger.ambiguous_mutations,
            warnings=warnings,
            target_rom_id=target_id,
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
        expected_namespace = self._run_namespace or romm_namespace(self._settings)
        if romm_namespace(self._settings) != expected_namespace:
            return {
                "status": "uncertain",
                "reason": "server_namespace_changed",
                "message": "The RomM server or user changed before the exact-ID proof.",
            }
        try:
            payload: Any = self._romm_api.get_rom_once(rom_id)
        except RommNotFoundError:
            if romm_namespace(self._settings) != expected_namespace:
                return {
                    "status": "uncertain",
                    "reason": "server_namespace_changed",
                    "message": "The RomM server or user changed during the exact-ID proof.",
                }
            return {"status": "vanished", "reason": ErrorCode.NOT_FOUND.value, "message": "RomM confirmed 404."}
        except Exception as exc:
            reason, message = classify_error(exc)
            return {"status": "uncertain", "reason": reason, "message": message}
        if romm_namespace(self._settings) != expected_namespace:
            return {
                "status": "uncertain",
                "reason": "server_namespace_changed",
                "message": "The RomM server or user changed during the exact-ID proof.",
            }
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
        verdicts: dict[int, dict[str, str]],
        delete_ids: set[int],
        target_id: int | None,
        vanished_source_id: int | None,
    ) -> tuple[str, str] | None:
        for rom_id in sorted(delete_ids):
            verdict = verdicts[rom_id]
            if verdict["status"] != "vanished":
                return verdict["reason"], f"ROM {rom_id}: {verdict['message']} Nothing else in this group was removed."
        if target_id is not None and verdicts[target_id]["status"] != "live":
            verdict = verdicts[target_id]
            return verdict["reason"], f"Default target {target_id}: {verdict['message']}"
        if vanished_source_id is not None and verdicts[vanished_source_id]["status"] != "vanished":
            verdict = verdicts[vanished_source_id]
            return verdict["reason"], f"Vanished source {vanished_source_id}: {verdict['message']}"
        return None

    async def _recovery_guard(
        self,
        handle: RecoveryHandle,
        recovery_ids: set[int],
        *,
        committed_action: str | None,
        app_id: int | None,
        target_id: int | None,
        launch_options: str | None,
    ) -> str | None:
        """Revalidate the sealed intended state before any irreversible action."""
        async with self._stable_save_locks(recovery_ids) as inventory:
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
                None,
                partial(self._recovery_store.validate_sources, handle.bundle_path, handle.bundle_digest),
            )
            backend_matches = True
            if app_id is not None and handle.steam_backend is not None:
                backend_matches = await self._loop.run_in_executor(
                    None, self._steam_recovery.validate_state, app_id, handle.steam_backend
                )
        if inventory != handle.save_inventory or not state_matches or not sources_match or not backend_matches:
            return "Local state no longer matches the sealed recovery bundle; no Steam action was started."
        return None

    @staticmethod
    def _inventory_warnings(inventory: dict[str, Any]) -> list[str]:
        raw = inventory.get("warnings")
        return [str(item) for item in raw] if isinstance(raw, list) else []

    def _ledger_result(
        self,
        ledger: _MutationLedger,
        reason: str,
        message: object,
        *,
        removed_app_id: int | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._group_result(
            ledger.rows,
            "partial",
            reason,
            message,
            app_id=ledger.app_id,
            removed_app_id=removed_app_id,
            bundle_path=ledger.bundle_path,
            committed_action=ledger.committed_action,
            mutations=ledger.mutations,
            ambiguous_mutations=ledger.ambiguous_mutations,
            warnings=warnings,
            action_ambiguous=ledger.action_ambiguous,
            target_rom_id=ledger.target_rom_id,
        )

    def _ledger_or_guard_result(
        self,
        ledger: _MutationLedger,
        rows: list[Rom],
        guard: tuple[str, str],
        handle: RecoveryHandle | None,
        app_id: int | None,
        *,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        if ledger.has_commit():
            return self._ledger_result(
                ledger,
                guard[0],
                guard[1],
                removed_app_id=app_id
                if ledger.committed_action == "remove_shortcut" and not ledger.action_ambiguous
                else None,
                warnings=warnings,
            )
        return self._group_result(
            rows,
            "skipped",
            guard[0],
            guard[1],
            bundle_path=handle.bundle_path if handle else None,
            warnings=warnings,
        )

    async def _shielded(self, awaitable: Awaitable[Any]) -> tuple[Any, bool]:
        task = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.shield(task), False
        except asyncio.CancelledError:
            try:
                return await task, True
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                raise _ChildFaultAfterCancellation(exc) from exc

    def _fault_result(self, ledger: _MutationLedger, rows: list[Rom], error: BaseException) -> dict[str, Any]:
        if ledger.has_commit():
            return self._ledger_result(ledger, ErrorCode.UNKNOWN.value, str(error))
        return self._group_result(rows, "failed", ErrorCode.UNKNOWN.value, str(error))

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
            payload["bundle_path"] = bundle_path[:_COMPLETION_PATH_CHARS]
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
        committed = bool(removed_count) or any(
            result.get("committed_action")
            or result.get("action_ambiguous")
            or result.get("mutations")
            or result.get("ambiguous_mutations")
            for result in results
        )
        run_failed = cancelled or reason is not None
        partial = any(result["status"] == "partial" for result in results) or bool(
            committed and (failures or run_failed)
        )
        bounded_reason = reason[:_COMPLETION_REASON_CHARS] if reason is not None else None
        bounded_message = message[:_COMPLETION_TEXT_CHARS] if message is not None else None
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for result in results:
            candidate = [*current, result]
            probe = self._completion_payload(
                run_id,
                candidate,
                chunk_index=len(chunks),
                final=False,
                success=not failures and not cancelled and reason is None,
                partial=partial,
                removed_count=removed_count,
                problem_count=len(failures),
                reason=bounded_reason,
                message=bounded_message,
            )
            if current and len(json.dumps(probe, ensure_ascii=True).encode("utf-8")) > _COMPLETION_BUDGET_BYTES:
                chunks.append(current)
                current = [result]
            else:
                current = candidate
        chunks.append(current)
        for chunk_index, chunk in enumerate(chunks):
            payload = self._completion_payload(
                run_id,
                chunk,
                chunk_index=chunk_index,
                final=chunk_index == len(chunks) - 1,
                success=not failures and not cancelled and reason is None,
                partial=partial,
                removed_count=removed_count,
                problem_count=len(failures),
                reason=bounded_reason,
                message=bounded_message,
            )
            await self._emit("prune_complete", payload)

    @staticmethod
    def _completion_payload(
        run_id: str,
        chunk: list[dict[str, Any]],
        *,
        chunk_index: int,
        final: bool,
        success: bool,
        partial: bool,
        removed_count: int,
        problem_count: int,
        reason: str | None,
        message: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": success,
            "partial": partial,
            "run_id": run_id,
            "chunk_index": chunk_index,
            "final": final,
            "removed_count": removed_count,
            "problem_count": problem_count,
            "removed_rom_ids": sorted({int(value) for result in chunk for value in result.get("removed_rom_ids", [])}),
            "affected_app_ids": sorted(
                {int(result["app_id"]) for result in chunk if type(result.get("app_id")) is int}
            ),
            "removed_app_ids": sorted(
                {int(result["removed_app_id"]) for result in chunk if type(result.get("removed_app_id")) is int}
            ),
            "results": chunk,
        }
        if reason is not None:
            payload["reason"] = reason
        if message is not None:
            payload["message"] = message
        return payload

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
        ambiguous_mutations: list[str] | None = None,
        warnings: list[str] | None = None,
        action_ambiguous: bool = False,
        target_rom_id: int | None = None,
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
            raw_reason = str(reason)
            result["reason"] = raw_reason[:_COMPLETION_REASON_CHARS]
            result["reason_truncated"] = len(raw_reason) > _COMPLETION_REASON_CHARS
        if removed_rom_ids is not None:
            result["removed_rom_ids"] = bounded_removed
            result["removed_count"] = len(removed_rom_ids)
            result["removed_rom_ids_truncated"] = len(removed_rom_ids) > _COMPLETION_IDS_PER_GROUP
        if app_id is not None:
            result["app_id"] = app_id
        if removed_app_id is not None:
            result["removed_app_id"] = removed_app_id
        if bundle_path is not None:
            result["bundle_path"] = bundle_path[:_COMPLETION_PATH_CHARS]
            result["bundle_path_truncated"] = len(bundle_path) > _COMPLETION_PATH_CHARS
        if committed_action is not None:
            result["committed_action"] = committed_action
        if mutations:
            result["mutations"] = [str(item)[:_COMPLETION_REASON_CHARS] for item in mutations]
        if ambiguous_mutations:
            result["ambiguous_mutations"] = [str(item)[:_COMPLETION_REASON_CHARS] for item in ambiguous_mutations]
        if warnings:
            bounded_warnings = [
                str(item)[:_COMPLETION_WARNING_CHARS] for item in warnings[:_COMPLETION_WARNINGS_PER_GROUP]
            ]
            result["warnings"] = bounded_warnings
            result["warning_count"] = len(warnings)
            result["warnings_truncated"] = len(warnings) > len(bounded_warnings) or any(
                len(str(item)) > _COMPLETION_WARNING_CHARS for item in warnings[:_COMPLETION_WARNINGS_PER_GROUP]
            )
        if action_ambiguous:
            result["action_ambiguous"] = True
        if target_rom_id is not None:
            result["target_rom_id"] = target_rom_id
        return result
