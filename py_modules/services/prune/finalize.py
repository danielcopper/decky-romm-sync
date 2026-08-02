"""The last gate before local data is destroyed, and the ordered cascade past it.

Everything a group does that cannot be undone happens here, and it may only
happen while every proof still holds: fresh liveness, no new download, unchanged
local rows, a sealed recovery bundle that still matches, and save ownership no
wider than the locks being held. The cascade's order is itself the contract —
saves are quarantined before content is removed, artifacts before Steam state,
and the database rows only once the filesystem is clean and their absence has
been re-proven.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from domain.prune import liveness_guard
from services.prune._models import cancellation_state, shielded
from services.prune.results import GroupOutcome

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable

    from models.prune import MutationOutcome, SourceClaim

    from domain.rom import Rom
    from services.protocols import (
        ActiveDownloadRomIdsFn,
        InstalledRomFilesRemoverFn,
        PruneArtifactStore,
        PruneSaveCoordinator,
        RecoveryBundleStore,
        SteamRecoveryStore,
    )
    from services.prune._models import RecoveryHandle
    from services.prune.liveness import LivenessProber
    from services.prune.planning import GroupPlan
    from services.prune.recovery import RecoveryCoordinator
    from services.prune.registry import PruneRegistry
    from services.prune.results import MutationLedger, PruneResultReporter
    from services.prune.save_locks import SaveLockCoordinator


@dataclass(frozen=True)
class GroupFinalizerConfig:
    """Dependencies for one group's revalidated, irreversible cascade."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    results: PruneResultReporter
    liveness: LivenessProber
    save_locks: SaveLockCoordinator
    registry: PruneRegistry
    recovery: RecoveryCoordinator
    recovery_store: RecoveryBundleStore
    steam_recovery: SteamRecoveryStore
    save_coordinator: PruneSaveCoordinator
    prune_artifacts: PruneArtifactStore
    active_downloads: ActiveDownloadRomIdsFn
    remove_installed_files: InstalledRomFilesRemoverFn


class GroupFinalizer:
    """Re-prove one group's every precondition, then run its cascade to a verdict."""

    def __init__(self, *, config: GroupFinalizerConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._results = config.results
        self._liveness = config.liveness
        self._save_locks = config.save_locks
        self._registry = config.registry
        self._recovery = config.recovery
        self._recovery_store = config.recovery_store
        self._steam_recovery = config.steam_recovery
        self._save_coordinator = config.save_coordinator
        self._prune_artifacts = config.prune_artifacts
        self._active_downloads = config.active_downloads
        self._remove_installed_files = config.remove_installed_files

    async def finish(
        self,
        *,
        run_id: str,
        initial_rows: list[Rom],
        plan: GroupPlan,
        committed_action: str | None,
        handle: RecoveryHandle | None,
        recovery_ids: set[int],
        index: int,
        total: int,
        launch_options: str | None,
        ledger: MutationLedger,
        vanished_source_id: int | None,
    ) -> dict[str, Any]:
        """Revalidate everything the cascade depends on, then commit or retain."""
        delete_ids = plan.delete_ids
        target_id = plan.target_id
        app_id = plan.app_id
        guard = await self._refreshed_liveness_guard(plan, vanished_source_id)
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

        async with self._save_locks.stable_locks(recovery_ids) as recovery_inventory:
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
                plan.fully_dead,
            ):
                return self._results.ledger_or_guard_result(
                    ledger,
                    initial_rows,
                    ("local_state_changed", "Final local revalidation failed before source removal."),
                    handle,
                    app_id,
                )
            if handle is not None and not await self._recovery_still_matches(
                handle, recovery_inventory, recovery_ids, plan, committed_action, launch_options
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

            delete_inventory = await self._save_locks.inventory(sorted(delete_ids))
            delete_locks = {int(value) for value in delete_inventory.get("lock_rom_ids", delete_ids)}
            held_locks = {int(value) for value in recovery_inventory.get("lock_rom_ids", recovery_ids)}
            if not delete_locks <= held_locks:
                return self._results.ledger_or_guard_result(
                    ledger,
                    rows,
                    ("save_ownership_changed", "Save ownership expanded; source data was retained."),
                    handle,
                    app_id,
                    warnings=self._save_locks.inventory_warnings(delete_inventory),
                )
            commit = self._commit(
                rows=rows,
                plan=plan,
                committed_action=committed_action,
                handle=handle,
                expected_app_id=expected_app_id,
                delete_inventory=delete_inventory,
                ledger=ledger,
            )
            result = await self._commit_shielded(commit)

        await self._report_removed(run_id, index, total, rows, handle, result)
        return result

    async def _refreshed_liveness_guard(
        self, plan: GroupPlan, vanished_source_id: int | None
    ) -> tuple[str, str] | None:
        """Re-prove every id the cascade turns on, immediately before it may run."""
        target_id = plan.target_id
        refreshed = await self._liveness.probe_many(
            plan.delete_ids
            | ({target_id} if target_id is not None else set())
            | ({vanished_source_id} if vanished_source_id is not None else set())
        )
        return liveness_guard(refreshed, plan.delete_ids, target_id, vanished_source_id)

    @staticmethod
    async def _commit_shielded(commit: Awaitable[Any]) -> dict[str, Any]:
        """Run the cascade to its own end, keeping its verdict if the run is cancelled."""
        try:
            return await shielded(commit)
        except asyncio.CancelledError as exc:
            state = cancellation_state(exc)
            if state.child_completed and isinstance(state.child_result, dict):
                state.group_result = state.child_result
            raise

    async def _report_removed(
        self,
        run_id: str,
        index: int,
        total: int,
        rows: list[Rom],
        handle: RecoveryHandle | None,
        result: dict[str, Any],
    ) -> None:
        """Publish the group's last progress frame without letting delivery change its verdict."""
        try:
            await self._results.emit_progress(
                run_id, index, total, "removed", rows, bundle_path=handle.bundle_path if handle else None
            )
        except asyncio.CancelledError as exc:
            cancellation_state(exc).group_result = result
            raise
        except Exception as exc:
            self._logger.warning(f"Removed-game cleanup final progress delivery failed: {exc}")

    async def _recovery_still_matches(
        self,
        handle: RecoveryHandle,
        recovery_inventory: dict[str, Any],
        recovery_ids: set[int],
        plan: GroupPlan,
        committed_action: str | None,
        launch_options: str | None,
    ) -> bool:
        """Whether every sealed thing still matches what is on disk right now.

        Four independent proofs, all required: the save inventory under the held
        locks, the database state the snapshot recorded, the bundle's own source
        bytes, and — when a shortcut was removed — the Steam-side identity.
        """
        state_matches = await self._loop.run_in_executor(
            None,
            self._recovery.state_matches,
            handle.snapshot,
            sorted(recovery_ids),
            committed_action,
            plan.app_id,
            plan.target_id,
            launch_options,
        )
        sources_match = await self._loop.run_in_executor(
            None,
            partial(self._recovery_store.validate_sources, handle.bundle_path, handle.bundle_digest),
        )
        backend_matches = True
        if committed_action == "remove_shortcut" and plan.app_id is not None and handle.steam_backend is not None:
            backend_matches = await self._loop.run_in_executor(
                None, self._steam_recovery.validate_state, plan.app_id, handle.steam_backend
            )
        return recovery_inventory == handle.save_inventory and state_matches and sources_match and backend_matches

    async def _commit(
        self,
        *,
        rows: list[Rom],
        plan: GroupPlan,
        committed_action: str | None,
        handle: RecoveryHandle | None,
        expected_app_id: int | None,
        delete_inventory: dict[str, Any],
        ledger: MutationLedger,
    ) -> dict[str, Any]:
        """Run the cascade in its contracted order, stopping at the first refusal.

        Saves are quarantined before content is removed, artifacts before Steam
        state, and the rows only once the filesystem is clean and their absence
        has been re-proven. Each step reports the reason it stopped; the ledger
        it has been updating is what keeps the resulting verdict truthful.
        """
        acted_app_id = plan.app_id if committed_action is not None else None
        warnings = self._save_locks.inventory_warnings(delete_inventory)

        def retained(failure: tuple[str, object], *, uncommitted_status: str = "failed") -> dict[str, Any]:
            return self._retained(
                rows,
                failure[0],
                failure[1],
                plan=plan,
                committed_action=committed_action,
                handle=handle,
                ledger=ledger,
                warnings=warnings,
                acted_app_id=acted_app_id,
                uncommitted_status=uncommitted_status,
            )

        claims = handle.source_claims if handle is not None else None
        failure = await self._quarantine_saves(delete_inventory, claims, ledger)
        if failure is not None:
            return retained(failure)
        failure = await self._remove_installed_content(plan.delete_ids, claims, ledger)
        if failure is not None:
            return retained(failure)
        failure = await self._remove_artifacts(plan, committed_action, handle, ledger)
        if failure is not None:
            return retained(failure)
        failure = await self._confirm_absences(delete_inventory)
        if failure is not None:
            return retained(failure)

        deleted = await self._delete_rows(rows, plan, expected_app_id, ledger)
        if not deleted:
            return retained(
                ("local_state_changed", "Final local revalidation failed after filesystem cleanup."),
                uncommitted_status="skipped",
            )
        return self._results.group_result(
            rows,
            "removed",
            None,
            f"Removed {len(plan.delete_ids)} confirmed vanished entr{'y' if len(plan.delete_ids) == 1 else 'ies'}.",
            GroupOutcome(
                removed_rom_ids=sorted(plan.delete_ids),
                app_id=acted_app_id,
                removed_app_id=plan.app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                mutations=ledger.mutations,
                ambiguous_mutations=ledger.ambiguous_mutations,
                warnings=warnings,
                target_rom_id=plan.target_id,
            ),
        )

    async def _quarantine_saves(
        self,
        delete_inventory: dict[str, Any],
        claims: dict[str, SourceClaim] | None,
        ledger: MutationLedger,
    ) -> tuple[str, object] | None:
        """Move the exclusively-owned saves aside, recording what actually moved."""
        quarantine = await self._loop.run_in_executor(
            None,
            self._save_coordinator.quarantine_prune_saves,
            delete_inventory["exclusive"],
            claims if claims is not None else delete_inventory.get("source_claims"),
        )
        raw_moved = quarantine.get("moved")
        if isinstance(raw_moved, list) and raw_moved:
            ledger.mutations.append("save_quarantine")
        if quarantine.get("ambiguous") and "save_quarantine" not in ledger.ambiguous_mutations:
            ledger.ambiguous_mutations.append("save_quarantine")
        if quarantine.get("success"):
            return None
        return "save_quarantine_failed", quarantine.get("message", "Save quarantine failed.")

    async def _remove_installed_content(
        self,
        delete_ids: set[int],
        claims: dict[str, SourceClaim] | None,
        ledger: MutationLedger,
    ) -> tuple[str, object] | None:
        """Delete each row's installed ROM files; a ROM that was never installed is fine."""
        for rom_id in sorted(delete_ids):
            removal = await self._loop.run_in_executor(None, self._remove_installed_files, rom_id, claims)
            if removal.get("changed") and "installed_rom_content" not in ledger.mutations:
                ledger.mutations.append("installed_rom_content")
            if removal.get("ambiguous") and "installed_rom_content" not in ledger.ambiguous_mutations:
                ledger.ambiguous_mutations.append("installed_rom_content")
            if removal.get("success") or removal.get("reason") == "not_installed":
                continue
            return "rom_removal_failed", removal.get("message", "ROM removal failed.")
        return None

    async def _remove_artifacts(
        self,
        plan: GroupPlan,
        committed_action: str | None,
        handle: RecoveryHandle | None,
        ledger: MutationLedger,
    ) -> tuple[str, object] | None:
        """Clear the plugin's own caches, then the Steam files a removal orphaned."""
        try:
            claims = handle.source_claims if handle is not None else None
            artifact_outcome = await self._loop.run_in_executor(
                None, self._prune_artifacts.remove, sorted(plan.delete_ids), claims
            )
            self._record_outcome(artifact_outcome, "plugin_artifacts", ledger)
            if not artifact_outcome.get("success"):
                raise RuntimeError(artifact_outcome.get("message", "Plugin artifact cleanup failed"))
            if committed_action == "remove_shortcut" and plan.app_id is not None and handle is not None:
                await self._remove_steam_state(plan.app_id, handle, ledger)
        except Exception as exc:
            return "artifact_cleanup_failed", str(exc)
        return None

    async def _remove_steam_state(self, app_id: int, handle: RecoveryHandle, ledger: MutationLedger) -> None:
        """Drop the Steam-side files for a shortcut this run removed."""
        if handle.steam_backend is None:
            raise RuntimeError("Steam recovery identity was not captured")
        steam_outcome = await self._loop.run_in_executor(
            None, self._steam_recovery.remove_state, app_id, handle.steam_backend, handle.source_claims
        )
        self._record_outcome(steam_outcome, "steam_files", ledger)
        if not steam_outcome.get("success"):
            raise RuntimeError(steam_outcome.get("message", "Steam state cleanup failed"))

    @staticmethod
    def _record_outcome(outcome: MutationOutcome, label: str, ledger: MutationLedger) -> None:
        """Write one mutation's changed/ambiguous flags into the ledger."""
        if outcome.get("changed"):
            ledger.mutations.append(label)
        if outcome.get("ambiguous"):
            ledger.ambiguous_mutations.append(label)

    async def _confirm_absences(self, delete_inventory: dict[str, Any]) -> tuple[str, object] | None:
        """Require every quarantined save to still be absent before the rows go."""
        absence_claims = delete_inventory.get("source_claims")
        if isinstance(absence_claims, dict) and await self._loop.run_in_executor(
            None, self._save_coordinator.validate_prune_absences, absence_claims
        ):
            return None
        return (
            "save_state_changed",
            "A previously absent save appeared before finalization; the aggregate was retained.",
        )

    async def _delete_rows(
        self,
        rows: list[Rom],
        plan: GroupPlan,
        expected_app_id: int | None,
        ledger: MutationLedger,
    ) -> bool:
        """Remove the aggregate rows, marking the ledger ambiguous if that raises."""
        try:
            deleted = await self._loop.run_in_executor(
                None,
                self._registry.delete_rows,
                rows,
                plan.delete_ids,
                plan.target_id,
                expected_app_id,
                plan.fully_dead,
            )
        except Exception:
            ledger.mutations.append("database_rows_ambiguous")
            raise
        if deleted:
            ledger.mutations.append("database_rows")
        return bool(deleted)

    def _retained(
        self,
        rows: list[Rom],
        reason: str,
        message: object,
        *,
        plan: GroupPlan,
        committed_action: str | None,
        handle: RecoveryHandle | None,
        ledger: MutationLedger,
        warnings: list[str],
        acted_app_id: int | None,
        uncommitted_status: str = "failed",
    ) -> dict[str, Any]:
        """Report a cascade step that stopped, carrying everything already changed.

        The status turns on the ledger: a group that has already committed
        something is ``partial`` no matter which step refused, because reporting
        it as a clean failure would hide the mutation from the user.
        """
        return self._results.group_result(
            rows,
            "partial" if ledger.has_commit() else uncommitted_status,
            reason,
            message,
            GroupOutcome(
                app_id=acted_app_id,
                removed_app_id=plan.app_id if committed_action == "remove_shortcut" else None,
                bundle_path=handle.bundle_path if handle else None,
                committed_action=committed_action,
                mutations=ledger.mutations,
                ambiguous_mutations=ledger.ambiguous_mutations,
                warnings=warnings,
                target_rom_id=plan.target_id,
            ),
        )


__all__ = ["GroupFinalizer", "GroupFinalizerConfig"]
