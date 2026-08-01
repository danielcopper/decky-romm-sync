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

if TYPE_CHECKING:
    import logging

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
        refreshed = await self._liveness.probe_many(
            delete_ids
            | ({target_id} if target_id is not None else set())
            | ({vanished_source_id} if vanished_source_id is not None else set())
        )
        guard = liveness_guard(refreshed, delete_ids, target_id, vanished_source_id)
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
            try:
                result = await shielded(commit)
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
        delete_ids = plan.delete_ids
        target_id = plan.target_id
        app_id = plan.app_id
        acted_app_id = app_id if committed_action is not None else None
        warnings = self._save_locks.inventory_warnings(delete_inventory)
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
            return self._retained(
                rows,
                "save_quarantine_failed",
                quarantine.get("message", "Save quarantine failed."),
                plan=plan,
                committed_action=committed_action,
                handle=handle,
                ledger=ledger,
                warnings=warnings,
                acted_app_id=acted_app_id,
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
            return self._retained(
                rows,
                "rom_removal_failed",
                removal.get("message", "ROM removal failed."),
                plan=plan,
                committed_action=committed_action,
                handle=handle,
                ledger=ledger,
                warnings=warnings,
                acted_app_id=acted_app_id,
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
            return self._retained(
                rows,
                "artifact_cleanup_failed",
                str(exc),
                plan=plan,
                committed_action=committed_action,
                handle=handle,
                ledger=ledger,
                warnings=warnings,
                acted_app_id=acted_app_id,
            )

        absence_claims = delete_inventory.get("source_claims")
        if not isinstance(absence_claims, dict) or not await self._loop.run_in_executor(
            None, self._save_coordinator.validate_prune_absences, absence_claims
        ):
            return self._retained(
                rows,
                "save_state_changed",
                "A previously absent save appeared before finalization; the aggregate was retained.",
                plan=plan,
                committed_action=committed_action,
                handle=handle,
                ledger=ledger,
                warnings=warnings,
                acted_app_id=acted_app_id,
            )

        try:
            deleted = await self._loop.run_in_executor(
                None, self._registry.delete_rows, rows, delete_ids, target_id, expected_app_id, plan.fully_dead
            )
        except Exception:
            ledger.mutations.append("database_rows_ambiguous")
            raise
        if not deleted:
            return self._retained(
                rows,
                "local_state_changed",
                "Final local revalidation failed after filesystem cleanup.",
                plan=plan,
                committed_action=committed_action,
                handle=handle,
                ledger=ledger,
                warnings=warnings,
                acted_app_id=acted_app_id,
                uncommitted_status="skipped",
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
            app_id=acted_app_id,
            removed_app_id=plan.app_id if committed_action == "remove_shortcut" else None,
            bundle_path=handle.bundle_path if handle else None,
            committed_action=committed_action,
            mutations=ledger.mutations,
            ambiguous_mutations=ledger.ambiguous_mutations,
            warnings=warnings,
            target_rom_id=plan.target_id,
        )


__all__ = ["GroupFinalizer", "GroupFinalizerConfig"]
