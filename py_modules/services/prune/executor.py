"""Per-group state machine for explicit vanished-ROM cleanup."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast

from domain.prune import selected_prune_ids
from domain.sibling_resolution import AUTO_REGION, fs_name_stem, resolve_group_representative
from lib.errors import RommNotFoundError, classify_error
from lib.list_result import ErrorCode
from lib.url_host import romm_namespace
from services.prune._models import PruneOptions, PrunePreview, RecoveryHandle, cancellation_state
from services.prune.results import MutationLedger, PruneResultReporter, PruneResultReporterConfig

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


class PruneExecutor:
    """Own liveness, recovery, frontend actions, and final cleanup sequencing."""

    def __init__(self, *, config: PruneExecutorConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._results = PruneResultReporter(config=PruneResultReporterConfig(emit=config.emit))
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
        self._run_namespace = preview.server_namespace
        self._results.bind_run(preview.preview_id)
        try:
            if romm_namespace(self._settings) != preview.server_namespace:
                raise RuntimeError("The RomM server or user changed after cleanup preview.")
            groups = await self._loop.run_in_executor(
                None, self._registry.groups_for_candidates, set(preview.candidate_ids)
            )
            total = len(groups)
            self._log_run_start(run_id, preview, options, total)
            for index, rows in enumerate(groups, start=1):
                await self._results.emit_progress(run_id, index, total, "checking", rows)
                try:
                    result = await self._run_group(
                        run_id,
                        rows,
                        set(preview.candidate_ids),
                        options,
                        index,
                        total,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._logger.exception(f"Vanished-ROM cleanup group {rows[0].rom_id} failed")
                    result = self._results.group_result(rows, "failed", ErrorCode.UNKNOWN.value, str(exc))
                self._log_group_outcome(run_id, index, total, result)
                results.append(result)
        except asyncio.CancelledError as exc:
            result = cancellation_state(exc).group_result
            if result is not None:
                results.append(result)
            await self._finish_run(
                run_id,
                results,
                cancelled=True,
                reason="cancelled",
                message="Cleanup was cancelled; no unstarted destructive phase was run.",
            )
            raise
        except Exception as exc:
            self._logger.exception("Vanished-ROM cleanup failed")
            await self._finish_run(
                run_id,
                results,
                cancelled=False,
                reason=ErrorCode.UNKNOWN.value,
                message=str(exc),
            )
        else:
            await self._finish_run(run_id, results, cancelled=False, reason=None, message=None)

    def _log_run_start(self, run_id: str, preview: PrunePreview, options: PruneOptions, groups: int) -> None:
        """Open this run's audit trail.

        Cleanup is the one operation that deletes local state a server can no
        longer supply, so what it was asked to do has to survive in the log
        independently of the UI that asked — every later line ties back to this
        run id.
        """
        self._logger.info(
            f"Cleanup run {run_id} starting: {groups} group(s), {len(preview.candidate_ids)} candidate(s), "
            f"scope={preview.scope}, repoint={options.repoint_shortcuts}, remove_rows={options.remove_rows}, "
            f"remove_fully_vanished={options.remove_fully_vanished}, recovery={options.create_recovery_bundle}, "
            f"installed_content_selected={len(options.include_installed_rom_ids)}"
        )

    def _log_group_outcome(self, run_id: str, index: int, total: int, result: dict[str, Any]) -> None:
        """Record one group's verdict, including the reason it was not touched."""
        reason = result.get("reason")
        bundle = result.get("bundle_path")
        detail = [f"status={result.get('status')}"]
        if reason:
            detail.append(f"reason={reason}")
        if result.get("committed_action"):
            detail.append(f"action={result['committed_action']}")
        if result.get("action_ambiguous"):
            detail.append("action=ambiguous")
        removed = result.get("removed_rom_ids") or []
        if removed:
            detail.append(f"removed={sorted(removed)}")
        if bundle:
            detail.append(f"bundle={bundle}")
        self._logger.info(
            f"Cleanup run {run_id} group {index}/{total} {result.get('group_id')} "
            f"rom_ids={result.get('rom_ids')}: {', '.join(detail)}"
        )

    async def _finish_run(
        self,
        run_id: str,
        results: list[dict[str, Any]],
        *,
        cancelled: bool,
        reason: str | None,
        message: str | None,
    ) -> None:
        self._log_run_end(run_id, results, cancelled=cancelled, reason=reason, message=message)
        try:
            await self._results.emit_completion(run_id, results, cancelled=cancelled, reason=reason, message=message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Same protection the final progress frame already has. Every
            # mutation is committed and the audit trail already records it, so a
            # failed terminal frame costs only the frontend's completion state —
            # which its lost-result timeout recovers. Letting it escape into the
            # unawaited run task would lose that recovery too, and the modal
            # would wait for a frame that is never coming.
            self._logger.warning(f"Removed-game cleanup completion delivery failed: {exc}")
        finally:
            self._run_namespace = None
            self._results.end_run()

    def _log_run_end(
        self,
        run_id: str,
        results: list[dict[str, Any]],
        *,
        cancelled: bool,
        reason: str | None,
        message: str | None,
    ) -> None:
        """Close the audit trail with what the run actually changed.

        Logged before the completion frame is emitted, so a run whose terminal
        event never reaches the frontend still leaves the removed ids on disk.
        """
        removed = sorted({rom_id for result in results for rom_id in (result.get("removed_rom_ids") or [])})
        app_ids = sorted(
            {
                app_id
                for result in results
                for app_id in (result.get("app_id"), result.get("removed_app_id"))
                if app_id is not None
            }
        )
        outcome = "cancelled" if cancelled else "finished"
        tail = f", reason={reason}: {message}" if reason else ""
        self._logger.info(
            f"Cleanup run {run_id} {outcome}: {len(results)} group(s), removed={removed}, affected_app_ids={app_ids}"
            f"{tail}"
        )

    async def _run_group(
        self,
        run_id: str,
        initial_rows: list[Rom],
        preview_candidate_ids: set[int],
        options: PruneOptions,
        index: int,
        total: int,
    ) -> dict[str, Any]:
        ledger = MutationLedger(initial_rows)
        try:
            return await self._run_group_inner(
                run_id, initial_rows, preview_candidate_ids, options, index, total, ledger
            )
        except asyncio.CancelledError as exc:
            state = cancellation_state(exc)
            if state.child_fault is not None:
                state.group_result = self._results.fault_result(ledger, initial_rows, state.child_fault)
            elif state.group_result is None and ledger.has_commit():
                state.group_result = self._results.ledger_result(
                    ledger,
                    "cancelled",
                    "Cleanup was cancelled after a committed or ambiguous action; later groups were not started.",
                )
            raise
        except Exception as exc:
            if ledger.has_commit():
                self._logger.exception(f"Vanished-ROM cleanup group {initial_rows[0].rom_id} failed after mutation")
                return self._results.ledger_result(ledger, ErrorCode.UNKNOWN.value, str(exc))
            raise

    async def _run_group_inner(
        self,
        run_id: str,
        initial_rows: list[Rom],
        preview_candidate_ids: set[int],
        options: PruneOptions,
        index: int,
        total: int,
        ledger: MutationLedger,
    ) -> dict[str, Any]:
        rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
        ledger.rows = rows or initial_rows
        if not rows:
            return self._results.group_result(
                initial_rows, "skipped", "local_state_changed", "The local group changed."
            )
        group_ids = {row.rom_id for row in rows}
        candidate_ids = group_ids & preview_candidate_ids
        bound = [row for row in rows if row.shortcut_app_id is not None]
        if len(bound) > 1:
            return self._results.group_result(rows, "skipped", "multiple_bindings", "The group has multiple shortcuts.")
        if self._active_downloads() & group_ids:
            return self._results.group_result(rows, "skipped", "download_in_progress", "Cancel active downloads first.")

        verdicts = await self._probe_many(group_ids)
        vanished_ids = {rom_id for rom_id, verdict in verdicts.items() if verdict["status"] == "vanished"}
        live_ids = {rom_id for rom_id, verdict in verdicts.items() if verdict["status"] == "live"}
        uncertain_ids = group_ids - vanished_ids - live_ids
        fully_dead = bool(group_ids) and group_ids <= vanished_ids
        # The verdicts are what every later decision turns on, so they belong in
        # the audit trail: a group reported as skipped is otherwise impossible to
        # explain after the fact without re-running against the same server.
        self._logger.info(
            f"Cleanup run {run_id} group {index}/{total} liveness: "
            f"gone={sorted(vanished_ids)}, still_there={sorted(live_ids)}, unconfirmed={sorted(uncertain_ids)}, "
            f"candidates={sorted(candidate_ids)}, bound={bound[0].rom_id if bound else None}"
        )
        if not live_ids and uncertain_ids:
            namespace_changed = any(
                verdicts[rom_id]["reason"] == "server_namespace_changed" for rom_id in uncertain_ids
            )
            return self._results.group_result(
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
                return self._results.group_result(
                    rows, "skipped", "no_live_default", "No live default could be selected."
                )
        if bound_row is not None and bound_row.rom_id in delete_ids and live_ids and target_id is None:
            delete_ids.remove(bound_row.rom_id)

        whole_game_action = fully_dead and bool(delete_ids)
        if not delete_ids and target_id is None:
            # Distinguish "your options excluded everything" from "RomM never
            # confirmed anything gone". Both leave nothing to do, but only the
            # first is answered by changing a toggle — reporting the second as
            # an options problem sends the user to fiddle with settings that
            # cannot help (#1570 F17).
            if uncertain_ids:
                return self._results.group_result(
                    rows,
                    "skipped",
                    "liveness_uncertain",
                    f"RomM could not confirm {len(uncertain_ids)} of this game's version(s); nothing was removed.",
                )
            return self._results.group_result(
                rows, "skipped", "options_excluded", "No confirmed rows matched the selected options."
            )

        drifted = False
        if bound_row is not None and bound_row.rom_id in vanished_ids and (target_id is not None or whole_game_action):
            drift = await self._drift_probe(bound_row.rom_id)
            drifted = bool(drift.get("drifted"))
            if drifted and not options.create_recovery_bundle:
                return self._results.group_result(
                    rows,
                    "skipped",
                    "unsynced_saves",
                    "Unsynced saves require a sealed recovery bundle before changing this shortcut.",
                )

        app_id = bound_row.shortcut_app_id if bound_row is not None else None
        frontend_steam: dict[str, object] | None = None
        if whole_game_action and app_id is not None and options.create_recovery_bundle:
            if bound_row is None:
                raise RuntimeError("Bound shortcut state disappeared before snapshot capture")
            try:
                capture = await self._request_action(
                    run_id,
                    "capture_shortcut_snapshot",
                    {"app_id": app_id},
                    bound_row.rom_id,
                    None,
                    group_ids,
                )
            except asyncio.CancelledError as exc:
                state = cancellation_state(exc)
                if state.action_result is not None:
                    _, state.group_result = await self._snapshot_outcome(
                        state.action_result, rows, ledger, bound_row.rom_id, app_id
                    )
                raise
            frontend_steam, result = await self._snapshot_outcome(capture, rows, ledger, bound_row.rom_id, app_id)
            if result is not None:
                return result

        recovery_ids = set(delete_ids)
        if target_id is not None and bound_row is not None:
            recovery_ids.add(bound_row.rom_id)
            recovery_ids.add(target_id)
        handle: RecoveryHandle | None = None
        if options.create_recovery_bundle:
            await self._results.emit_progress(run_id, index, total, "creating_recovery", rows)
            try:
                async with self._stable_save_locks(recovery_ids) as save_inventory:
                    locked_rows = await self._loop.run_in_executor(
                        None, self._registry.reread_group, initial_rows[0].rom_id
                    )
                    if not locked_rows or not recovery_ids <= {row.rom_id for row in locked_rows}:
                        return self._results.group_result(
                            rows, "skipped", "local_state_changed", "The local group changed before recovery."
                        )
                    snapshot = await self._loop.run_in_executor(
                        None, self._recovery.snapshot_state, sorted(recovery_ids), frontend_steam
                    )
                    sealed = await self._shielded(
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
            except Exception as exc:
                self._logger.error(f"Recovery bundle failed for group {min(group_ids)}: {exc}")
                return self._results.group_result(rows, "failed", "recovery_failed", str(exc))
            await self._results.emit_progress(
                run_id,
                index,
                total,
                "recovery_sealed",
                rows,
                bundle_path=handle.bundle_path,
            )

        if self._active_downloads() & delete_ids:
            return self._results.group_result(rows, "skipped", "download_in_progress", "Cancel active downloads first.")
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
            return self._results.group_result(
                rows,
                "skipped",
                guard[0],
                guard[1],
                bundle_path=handle.bundle_path if handle else None,
            )

        # This early revalidation exists to protect the Steam action below, which
        # _finish_group's identical pre-mutation check runs too late to precede.
        # Without a planned Steam action nothing irreversible happens in between,
        # so that later check is the pre-mutation gate and this one only repeats
        # the same full source rehash.
        steam_action_planned = (
            app_id is not None and bound_row is not None and (target_id is not None or whole_game_action)
        )
        if handle is not None and steam_action_planned:
            recovery_guard = await self._recovery_guard(
                handle,
                recovery_ids,
                committed_action=None,
                app_id=app_id,
                target_id=target_id,
                launch_options=None,
            )
            if recovery_guard is not None:
                return self._results.group_result(
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
            try:
                switch = await self._shielded(self._switch_version(app_id, target_id, drifted and handle is not None))
            except asyncio.CancelledError as exc:
                state = cancellation_state(exc)
                if state.child_completed and isinstance(state.child_result, dict):
                    _, state.group_result = self._switch_outcome(
                        state.child_result, rows, ledger, target_id, app_id, handle
                    )
                    if state.group_result is None:
                        state.group_result = self._results.ledger_result(
                            ledger,
                            "cancelled",
                            "Cleanup was cancelled after the version binding changed; later groups were not started.",
                        )
                raise
            launch_options, result = self._switch_outcome(switch, rows, ledger, target_id, app_id, handle)
            if result is not None:
                return result
            if launch_options is None:
                raise RuntimeError("Successful version switch did not produce launch options")
            committed_action = "repoint_shortcut"
            await self._shielded(
                self._results.emit_progress(
                    run_id, index, total, "repointing", rows, bundle_path=handle.bundle_path if handle else None
                )
            )
            try:
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
            except asyncio.CancelledError as exc:
                state = cancellation_state(exc)
                if state.action_result is not None:
                    state.group_result = self._repoint_action_outcome(state.action_result, ledger)
                    if state.group_result is None:
                        state.group_result = self._results.ledger_result(
                            ledger,
                            "cancelled",
                            "Cleanup was cancelled after Steam confirmed the repoint; later groups were not started.",
                        )
                raise
            result = self._repoint_action_outcome(action, ledger)
            if result is not None:
                return result
        elif whole_game_action and app_id is not None and bound_row is not None:
            await self._results.emit_progress(
                run_id,
                index,
                total,
                "removing_shortcut",
                rows,
                bundle_path=handle.bundle_path if handle else None,
            )
            try:
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
            except asyncio.CancelledError as exc:
                state = cancellation_state(exc)
                if state.action_result is not None:
                    _, state.group_result = await self._remove_action_outcome(
                        state.action_result, rows, ledger, bound_row.rom_id, app_id, handle
                    )
                    if state.group_result is None:
                        state.group_result = self._results.ledger_result(
                            ledger,
                            "cancelled",
                            "Cleanup was cancelled after Steam confirmed removal; later groups were not started.",
                            removed_app_id=app_id,
                        )
                raise
            committed_action, result = await self._remove_action_outcome(
                action, rows, ledger, bound_row.rom_id, app_id, handle
            )
            if result is not None:
                return result

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
                return self._results.ledger_result(ledger, final_guard[0], final_guard[1])
            return self._results.group_result(
                rows,
                "repointed",
                None,
                "Repointed the shortcut to the live Default.",
                app_id=app_id,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                target_rom_id=target_id,
            )

        return await self._finish_group(
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

    async def _snapshot_outcome(
        self,
        capture: dict[str, Any],
        rows: list[Rom],
        ledger: MutationLedger,
        bound_rom_id: int,
        app_id: int,
    ) -> tuple[dict[str, object] | None, dict[str, Any] | None]:
        if capture.get("success") and capture.get("shortcut_absent") is True:
            ledger.app_id = app_id
            ledger.committed_action = "remove_shortcut"
            try:
                reconciled = await self._shielded(
                    self._loop.run_in_executor(None, self._registry.reconcile_removed_shortcut, bound_rom_id, app_id)
                )
            except asyncio.CancelledError as exc:
                state = cancellation_state(exc)
                if state.child_completed and type(state.child_result) is bool:
                    state.group_result = self._shortcut_absence_result(ledger, state.child_result, app_id)
                raise
            return None, self._shortcut_absence_result(ledger, bool(reconciled), app_id)
        snapshot = capture.get("snapshot")
        if not capture.get("success") or not isinstance(snapshot, dict):
            return None, self._results.group_result(
                rows,
                "failed",
                "steam_snapshot_failed",
                capture.get("message", "Steam snapshot failed."),
            )
        return cast("dict[str, object]", snapshot), None

    def _shortcut_absence_result(self, ledger: MutationLedger, reconciled: bool, app_id: int) -> dict[str, Any]:
        if reconciled and "shortcut_binding" not in ledger.mutations:
            ledger.mutations.append("shortcut_binding")
        return self._results.ledger_result(
            ledger,
            "shortcut_absence_reconciled" if reconciled else "local_state_changed",
            (
                "Steam already lacked this shortcut; its local binding was reconciled. Run cleanup again."
                if reconciled
                else "Steam lacked the shortcut, but its local binding changed before reconciliation."
            ),
            removed_app_id=app_id if reconciled else None,
        )

    def _switch_outcome(
        self,
        switch: dict[str, Any],
        rows: list[Rom],
        ledger: MutationLedger,
        target_id: int,
        app_id: int,
        handle: RecoveryHandle | None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        if not switch.get("success"):
            ledger.committed_action = None
            ledger.action_ambiguous = False
            return None, self._results.group_result(
                rows,
                "failed",
                switch.get("reason", "repoint_failed"),
                switch.get("message", "Repoint failed."),
                bundle_path=handle.bundle_path if handle else None,
            )
        launch_options = switch.get("launch_options")
        if switch.get("rom_id") != target_id or switch.get("app_id") != app_id or not isinstance(launch_options, str):
            return None, self._results.ledger_result(
                ledger,
                "repoint_result_invalid",
                "The binding changed but the switch result was incomplete.",
            )
        ledger.target_rom_id = target_id
        ledger.action_ambiguous = False
        if "shortcut_binding" not in ledger.mutations:
            ledger.mutations.append("shortcut_binding")
        return launch_options, None

    def _repoint_action_outcome(self, action: dict[str, Any], ledger: MutationLedger) -> dict[str, Any] | None:
        if action.get("success"):
            return None
        if action.get("mutation_attempted") is True or action.get("reason") == "action_ambiguous":
            ledger.action_ambiguous = True
            return self._results.ledger_result(
                ledger,
                "action_ambiguous",
                action.get("message", "The binding changed but Steam confirmation is unknown."),
            )
        return self._results.ledger_result(
            ledger,
            "steam_action_failed",
            action.get("message", "The binding changed but Steam confirmation failed."),
        )

    async def _remove_action_outcome(
        self,
        action: dict[str, Any],
        rows: list[Rom],
        ledger: MutationLedger,
        bound_rom_id: int,
        app_id: int,
        handle: RecoveryHandle | None,
    ) -> tuple[Literal["remove_shortcut"] | None, dict[str, Any] | None]:
        if not action.get("success"):
            if action.get("mutation_attempted") is True or action.get("reason") == "action_ambiguous":
                ledger.app_id = app_id
                ledger.committed_action = "remove_shortcut"
                ledger.action_ambiguous = True
                return None, self._results.ledger_result(
                    ledger,
                    "action_ambiguous",
                    "Steam removal was claimed but its outcome is unknown; source data was retained.",
                )
            return None, self._results.group_result(
                rows,
                "failed",
                "steam_action_failed",
                action.get("message", "Shortcut removal failed."),
                bundle_path=handle.bundle_path if handle else None,
            )
        ledger.app_id = app_id
        ledger.committed_action = "remove_shortcut"
        ledger.action_ambiguous = False
        try:
            reconciled = await self._shielded(
                self._loop.run_in_executor(None, self._registry.reconcile_removed_shortcut, bound_rom_id, app_id)
            )
        except asyncio.CancelledError as exc:
            state = cancellation_state(exc)
            if state.child_completed and type(state.child_result) is bool:
                _, state.group_result = self._removed_shortcut_reconcile_result(
                    ledger, state.child_result, rows, app_id, handle
                )
                if state.group_result is None:
                    state.group_result = self._results.ledger_result(
                        ledger,
                        "cancelled",
                        "Cleanup was cancelled after Steam confirmed removal; later groups were not started.",
                        removed_app_id=app_id,
                    )
            raise
        return self._removed_shortcut_reconcile_result(ledger, bool(reconciled), rows, app_id, handle)

    def _removed_shortcut_reconcile_result(
        self,
        ledger: MutationLedger,
        reconciled: bool,
        rows: list[Rom],
        app_id: int,
        handle: RecoveryHandle | None,
    ) -> tuple[Literal["remove_shortcut"], dict[str, Any] | None]:
        if reconciled:
            if "shortcut_binding" not in ledger.mutations:
                ledger.mutations.append("shortcut_binding")
            return "remove_shortcut", None
        return "remove_shortcut", self._results.group_result(
            rows,
            "partial",
            "local_state_changed",
            "Steam removed the shortcut, but its local binding changed before reconciliation.",
            app_id=app_id,
            removed_app_id=app_id,
            bundle_path=handle.bundle_path if handle else None,
            committed_action="remove_shortcut",
        )

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
        ledger: MutationLedger,
        vanished_source_id: int | None,
    ) -> dict[str, Any]:
        refreshed = await self._probe_many(
            delete_ids
            | ({target_id} if target_id is not None else set())
            | ({vanished_source_id} if vanished_source_id is not None else set())
        )
        guard = self._fresh_guard(refreshed, delete_ids, target_id, vanished_source_id)
        if guard is not None:
            return self._results.ledger_or_guard_result(ledger, initial_rows, guard, handle, app_id)
        if self._active_downloads() & delete_ids:
            return self._results.ledger_or_guard_result(
                ledger,
                initial_rows,
                ("download_in_progress", "A download became active; source data was retained."),
                handle,
                app_id,
            )

        rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
        if not rows or not delete_ids <= {row.rom_id for row in rows}:
            return self._results.ledger_or_guard_result(
                ledger,
                initial_rows,
                ("local_state_changed", "Local state changed; source data was retained."),
                handle,
                app_id,
            )
        ledger.rows = rows
        await self._results.emit_progress(
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
                return self._results.ledger_or_guard_result(
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
                    return self._results.ledger_or_guard_result(
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
                return self._results.ledger_or_guard_result(
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
            try:
                result = await self._shielded(commit)
            except asyncio.CancelledError as exc:
                state = cancellation_state(exc)
                if state.child_completed and isinstance(state.child_result, dict):
                    state.group_result = state.child_result
                raise

        try:
            await self._results.emit_progress(
                run_id, index, total, "removed", rows, bundle_path=handle.bundle_path if handle else None
            )
        except asyncio.CancelledError as exc:
            cancellation_state(exc).group_result = result
            raise
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
        ledger: MutationLedger,
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
            return self._results.group_result(
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
            return self._results.group_result(
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
            return self._results.group_result(
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

        absence_claims = delete_inventory.get("source_claims")
        if not isinstance(absence_claims, dict) or not await self._loop.run_in_executor(
            None, self._save_coordinator.validate_prune_absences, absence_claims
        ):
            return self._results.group_result(
                rows,
                "partial" if ledger.has_commit() else "failed",
                "save_state_changed",
                "A previously absent save appeared before finalization; the aggregate was retained.",
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
            return self._results.group_result(
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

        return self._results.group_result(
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

    async def _shielded(self, awaitable: Awaitable[Any]) -> Any:
        task = asyncio.ensure_future(awaitable)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            state = cancellation_state(exc)
            try:
                state.child_result = await task
                state.child_completed = True
            except asyncio.CancelledError:
                # The child was cancelled too. Its CancelledError carries no
                # captured state, and callers read that state off whatever
                # propagates to decide what the group actually did — so the
                # original cancellation is re-raised instead of this one.
                pass
            except BaseException as child_fault:
                state.child_fault = child_fault
            raise
