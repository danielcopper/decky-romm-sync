"""Sequences one cleanup run's groups through plan, arm, Steam action, finalize.

Owns the order the phases run in and the state that only exists between them —
the sealed recovery handle and the arming proofs that stand between a plan and
the first irreversible act — plus the run-level audit trail. The phases
themselves live in their own modules; what belongs here is the sequencing and
the guarantee that a cancellation anywhere in it still reports what the group
committed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from domain.prune import liveness_guard
from lib.errors import OperationAbortedError
from lib.list_result import ErrorCode
from lib.url_host import romm_namespace
from services.prune._models import (
    BackupControl,
    PrunePreview,
    RecoveryHandle,
    cancellation_state,
    shielded,
)
from services.prune.finalize import GroupFinalizer, GroupFinalizerConfig
from services.prune.liveness import LivenessProber, LivenessProberConfig
from services.prune.planning import GroupPlan, GroupPlanner, GroupPlannerConfig
from services.prune.results import GroupOutcome, MutationLedger, PruneResultReporter, PruneResultReporterConfig
from services.prune.save_locks import SaveLockCoordinator, SaveLockCoordinatorConfig
from services.prune.steam_actions import SteamActionRunner, SteamActionRunnerConfig

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from domain.rom import Rom
    from services.protocols import (
        ActiveDownloadRomIdsFn,
        InstalledRomFilesRemoverFn,
        PruneArtifactStore,
        PruneSaveCoordinator,
        RecoveryBundleStore,
        RommLivenessApi,
        SaveDriftProbeFn,
        SteamRecoveryStore,
        VersionSwitcherFn,
    )
    from services.prune._models import ActionRequester, PruneOptions
    from services.prune.recovery import RecoveryCoordinator
    from services.prune.registry import PruneRegistry


def _steam_action_planned(plan: GroupPlan) -> bool:
    """Whether this group will ask the frontend to touch Steam at all."""
    return (
        plan.app_id is not None
        and plan.bound_row is not None
        and (plan.target_id is not None or plan.whole_game_action)
    )


@dataclass(frozen=True)
class PruneExecutorConfig:
    """Dependencies for one cleanup run's per-group state machine."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: Callable[..., Awaitable[None]]
    romm_api: RommLivenessApi
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
    """Run every confirmed group through its phases and publish the outcome."""

    def __init__(self, *, config: PruneExecutorConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._results = PruneResultReporter(config=PruneResultReporterConfig(emit=config.emit))
        self._recovery_store = config.recovery_store
        self._steam_recovery = config.steam_recovery
        self._active_downloads = config.active_downloads
        self._settings = config.settings
        self._recovery = config.recovery
        self._registry = config.registry

        self._liveness = LivenessProber(
            config=LivenessProberConfig(
                loop=config.loop,
                logger=config.logger,
                romm_api=config.romm_api,
                settings=config.settings,
                canary_rom_ids=config.registry.canary_rom_ids,
            ),
        )
        self._save_locks = SaveLockCoordinator(
            config=SaveLockCoordinatorConfig(loop=config.loop, save_coordinator=config.save_coordinator),
        )
        self._planner = GroupPlanner(
            config=GroupPlannerConfig(
                loop=config.loop,
                logger=config.logger,
                results=self._results,
                registry=config.registry,
                liveness=self._liveness,
                active_downloads=config.active_downloads,
                drift_probe=config.drift_probe,
                settings=config.settings,
            ),
        )
        self._steam = SteamActionRunner(
            config=SteamActionRunnerConfig(
                loop=config.loop,
                results=self._results,
                registry=config.registry,
                switch_version=config.switch_version,
                request_action=config.request_action,
            ),
        )
        self._finalizer = GroupFinalizer(
            config=GroupFinalizerConfig(
                loop=config.loop,
                logger=config.logger,
                results=self._results,
                liveness=self._liveness,
                save_locks=self._save_locks,
                registry=config.registry,
                recovery=config.recovery,
                recovery_store=config.recovery_store,
                steam_recovery=config.steam_recovery,
                save_coordinator=config.save_coordinator,
                prune_artifacts=config.prune_artifacts,
                active_downloads=config.active_downloads,
                remove_installed_files=config.remove_installed_files,
            ),
        )

    async def run(self, run_id: str, preview: PrunePreview, options: PruneOptions) -> None:
        """Execute every candidate group and emit bounded terminal chunks."""
        results: list[dict[str, Any]] = []
        self._liveness.bind_run(run_id, preview.server_namespace)
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
            self._liveness.end_run()
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
            # A worker that stopped because this run asked it to did what it was
            # told; reporting that as a fault would turn an obedient stop into an
            # error the user never caused.
            aborted = isinstance(state.child_fault, OperationAbortedError)
            if aborted:
                state.child_fault = None
            if state.child_fault is not None:
                state.group_result = self._results.fault_result(ledger, initial_rows, state.child_fault)
            elif state.group_result is None and ledger.has_commit():
                state.group_result = self._results.ledger_result(
                    ledger,
                    "cancelled",
                    "Cleanup was cancelled after a committed or ambiguous action; later groups were not started.",
                )
            elif state.group_result is None and aborted:
                state.group_result = self._results.group_result(
                    ledger.rows,
                    "skipped",
                    "cancelled",
                    "Cleanup was cancelled while the recovery bundle was being written; nothing was removed.",
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
        planned = await self._planner.plan(run_id, initial_rows, preview_candidate_ids, options, index, total, ledger)
        if not isinstance(planned, GroupPlan):
            return planned
        bound_row = planned.bound_row
        app_id = planned.app_id
        target_id = planned.target_id

        frontend_steam: dict[str, object] | None = None
        if planned.whole_game_action and app_id is not None and options.create_recovery_bundle:
            frontend_steam, result = await self._steam.capture_snapshot(run_id, planned, ledger)
            if result is not None:
                return result

        recovery_ids = set(planned.delete_ids)
        if target_id is not None and bound_row is not None:
            recovery_ids.add(bound_row.rom_id)
            recovery_ids.add(target_id)
        handle: RecoveryHandle | None = None
        if options.create_recovery_bundle:
            handle, result = await self._arm_recovery(
                run_id, initial_rows, planned, options, recovery_ids, frontend_steam, index, total, ledger
            )
            if result is not None:
                return result

        armed = await self._armed_to_mutate(planned, recovery_ids, handle)
        if armed is not None:
            return armed

        launch_options, committed_action, result = await self._mutate_steam(
            run_id, planned, ledger, handle, frontend_steam, index, total
        )
        if result is not None:
            return result

        repointed = await self._repoint_only_result(planned, ledger, handle, committed_action)
        if repointed is not None:
            return repointed

        return await self._finalizer.finish(
            run_id=run_id,
            initial_rows=initial_rows,
            plan=planned,
            committed_action=committed_action,
            handle=handle,
            recovery_ids=recovery_ids,
            index=index,
            total=total,
            launch_options=launch_options,
            ledger=ledger,
            vanished_source_id=bound_row.rom_id if target_id is not None and bound_row is not None else None,
        )

    async def _mutate_steam(
        self,
        run_id: str,
        plan: GroupPlan,
        ledger: MutationLedger,
        handle: RecoveryHandle | None,
        frontend_steam: dict[str, object] | None,
        index: int,
        total: int,
    ) -> tuple[str | None, Literal["repoint_shortcut", "remove_shortcut"] | None, dict[str, Any] | None]:
        """Run the one Steam mutation this group planned, if it planned one.

        Exactly one of the two is reachable: a repoint needs a live target to
        move onto, a removal needs the whole game gone. A group with neither
        changes nothing in Steam and falls straight through.
        """
        if plan.target_id is not None and plan.app_id is not None and plan.bound_row is not None:
            return await self._steam.repoint(run_id, plan, ledger, handle, index, total)
        if plan.whole_game_action and plan.app_id is not None and plan.bound_row is not None:
            committed, result = await self._steam.remove(run_id, plan, ledger, handle, frontend_steam, index, total)
            return None, committed, result
        return None, None, None

    async def _repoint_only_result(
        self,
        plan: GroupPlan,
        ledger: MutationLedger,
        handle: RecoveryHandle | None,
        committed_action: str | None,
    ) -> dict[str, Any] | None:
        """Terminal verdict for a group that moved a shortcut and removes nothing.

        There is no cascade to run, so this is the last chance to notice that the
        binding it just moved no longer holds — hence one more proof of the
        source and the target before reporting success.
        """
        if plan.target_id is None or plan.delete_ids:
            return None
        bound_row = plan.bound_row
        ids = {bound_row.rom_id, plan.target_id} if bound_row is not None else {plan.target_id}
        final_proof = await self._liveness.probe_many(ids)
        final_guard = liveness_guard(
            final_proof, set(), plan.target_id, bound_row.rom_id if bound_row is not None else None
        )
        if final_guard is not None:
            return self._results.ledger_result(ledger, final_guard[0], final_guard[1])
        return self._results.group_result(
            plan.rows,
            "repointed",
            None,
            "Repointed the shortcut to the live Default.",
            GroupOutcome(
                app_id=plan.app_id,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                target_rom_id=plan.target_id,
            ),
        )

    async def _armed_to_mutate(
        self,
        plan: GroupPlan,
        recovery_ids: set[int],
        handle: RecoveryHandle | None,
    ) -> dict[str, Any] | None:
        """Re-prove everything the Steam action depends on, or refuse the group.

        Runs after the bundle is sealed and before anything irreversible: a
        download that started meanwhile, liveness that no longer holds, and — when
        a Steam action is actually planned — a sealed recovery state that no
        longer matches. Returns the terminal result that refuses the group, or
        ``None`` when it may proceed.
        """
        if self._active_downloads() & plan.delete_ids:
            return self._results.group_result(
                plan.rows, "skipped", "download_in_progress", "Cancel active downloads first."
            )
        guard = await self._reprove_liveness(plan)
        if guard is not None:
            return self._results.group_result(
                plan.rows,
                "skipped",
                guard[0],
                guard[1],
                GroupOutcome(bundle_path=handle.bundle_path if handle else None),
            )
        # This early revalidation exists to protect the Steam action below, which
        # the finalizer's identical pre-mutation check runs too late to precede.
        # Without a planned Steam action nothing irreversible happens in between,
        # so that later check is the pre-mutation gate and this one only repeats
        # the same full source rehash.
        if handle is None or not _steam_action_planned(plan):
            return None
        recovery_guard = await self._recovery_guard(
            handle,
            recovery_ids,
            committed_action=None,
            app_id=plan.app_id,
            target_id=plan.target_id,
            launch_options=None,
        )
        if recovery_guard is None:
            return None
        return self._results.group_result(
            plan.rows,
            "skipped",
            "recovery_state_changed",
            recovery_guard,
            GroupOutcome(bundle_path=handle.bundle_path),
        )

    async def _reprove_liveness(self, plan: GroupPlan) -> tuple[str, str] | None:
        """Re-probe the ids this group's plan turns on, and guard on the answers."""
        proof_ids = set(plan.delete_ids)
        vanished_source_id: int | None = None
        if plan.target_id is not None:
            proof_ids.add(plan.target_id)
            if plan.bound_row is not None:
                proof_ids.add(plan.bound_row.rom_id)
                vanished_source_id = plan.bound_row.rom_id
        refreshed = await self._liveness.probe_many(proof_ids)
        return liveness_guard(refreshed, plan.delete_ids, plan.target_id, vanished_source_id)

    async def _arm_recovery(
        self,
        run_id: str,
        initial_rows: list[Rom],
        plan: GroupPlan,
        options: PruneOptions,
        recovery_ids: set[int],
        frontend_steam: dict[str, object] | None,
        index: int,
        total: int,
        ledger: MutationLedger,
    ) -> tuple[RecoveryHandle | None, dict[str, Any] | None]:
        """Seal everything this group would destroy, under the locks it will hold.

        The snapshot, the bundle and the source claims are taken inside one
        stable-lock hold and against rows re-read inside it, so the sealed state
        is the state the later phases revalidate against rather than whatever
        was true when the group was planned.
        """
        rows = plan.rows
        await self._results.emit_progress(run_id, index, total, "creating_recovery", rows)
        try:
            async with self._save_locks.stable_locks(recovery_ids) as save_inventory:
                locked_rows = await self._loop.run_in_executor(
                    None, self._registry.reread_group, initial_rows[0].rom_id
                )
                if not locked_rows or not recovery_ids <= {row.rom_id for row in locked_rows}:
                    return None, self._results.group_result(
                        rows, "skipped", "local_state_changed", "The local group changed before recovery."
                    )
                snapshot = await self._loop.run_in_executor(
                    None, self._recovery.snapshot_state, sorted(recovery_ids), frontend_steam
                )
                # Still shielded, so the worker's own cleanup finishes before the
                # run ends — but the control turns "wait for the copy" into
                # "tell it to stop, then wait", which is a chunk rather than
                # minutes of copying the user already asked to abandon.
                backup = BackupControl()
                sealed = await shielded(
                    self._loop.run_in_executor(
                        None,
                        lambda: self._recovery.seal(
                            rows=[row for row in locked_rows if row.rom_id in recovery_ids],
                            snapshot=snapshot,
                            save_inventory=save_inventory,
                            include_installed_rom_ids=set(options.include_installed_rom_ids),
                            delete_ids=plan.delete_ids,
                            app_id=plan.app_id if plan.whole_game_action else None,
                            should_abort=backup.is_aborted,
                        ),
                    ),
                    on_cancel=backup.abort,
                )
                bundle_path, steam_backend = sealed
                sealed_claims = await self._loop.run_in_executor(None, self._recovery_store.source_claims, bundle_path)
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
            self._logger.error(f"Recovery bundle failed for group {min(plan.group_ids)}: {exc}")
            return None, self._results.group_result(rows, "failed", "recovery_failed", str(exc))
        await self._results.emit_progress(
            run_id,
            index,
            total,
            "recovery_sealed",
            rows,
            bundle_path=handle.bundle_path,
        )
        return handle, None

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
        async with self._save_locks.stable_locks(recovery_ids) as inventory:
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


__all__ = ["PruneExecutor", "PruneExecutorConfig"]
