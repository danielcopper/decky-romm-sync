"""The shielded window where Steam changes and the backend cannot see it happen.

Steam is mutated by the frontend, so every action here is a request whose outcome
arrives back over the wire — or does not. Anything that reads such an answer
belongs here, and the rule it exists to hold is that an outcome lost in transit
is reported as ambiguous, never as success and never as failure: the ledger is
marked before the request goes out, so a cancellation or a timeout still leaves
the group able to say what Steam may already have done.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from services.prune._models import cancellation_state, shielded
from services.prune.results import GroupOutcome

if TYPE_CHECKING:
    from domain.rom import Rom
    from services.protocols import VersionSwitcherFn
    from services.prune._models import ActionRequester, RecoveryHandle
    from services.prune.planning import GroupPlan
    from services.prune.registry import PruneRegistry
    from services.prune.results import MutationLedger, PruneResultReporter


@dataclass(frozen=True)
class SteamActionRunnerConfig:
    """Dependencies for the frontend-owned Steam mutations of one group."""

    loop: asyncio.AbstractEventLoop
    results: PruneResultReporter
    registry: PruneRegistry
    switch_version: VersionSwitcherFn
    request_action: ActionRequester


class SteamActionRunner:
    """Request one group's Steam mutations and interpret what came back."""

    def __init__(self, *, config: SteamActionRunnerConfig) -> None:
        self._loop = config.loop
        self._results = config.results
        self._registry = config.registry
        self._switch_version = config.switch_version
        self._request_action = config.request_action

    async def capture_snapshot(
        self,
        run_id: str,
        plan: GroupPlan,
        ledger: MutationLedger,
    ) -> tuple[dict[str, object] | None, dict[str, Any] | None]:
        """Ask Steam for the shortcut's recoverable state before it is removed."""
        app_id = plan.app_id
        bound_row = plan.bound_row
        if bound_row is None or app_id is None:
            raise RuntimeError("Bound shortcut state disappeared before snapshot capture")
        try:
            capture = await self._request_action(
                run_id,
                "capture_shortcut_snapshot",
                {"app_id": app_id},
                bound_row.rom_id,
                None,
                plan.group_ids,
            )
        except asyncio.CancelledError as exc:
            state = cancellation_state(exc)
            if state.action_result is not None:
                _, state.group_result = await self._snapshot_outcome(
                    state.action_result, plan.rows, ledger, bound_row.rom_id, app_id
                )
            raise
        return await self._snapshot_outcome(capture, plan.rows, ledger, bound_row.rom_id, app_id)

    async def repoint(
        self,
        run_id: str,
        plan: GroupPlan,
        ledger: MutationLedger,
        handle: RecoveryHandle | None,
        index: int,
        total: int,
    ) -> tuple[str | None, Literal["repoint_shortcut"] | None, dict[str, Any] | None]:
        """Move the shortcut onto the live default and have Steam confirm it."""
        app_id = plan.app_id
        bound_row = plan.bound_row
        target_id = plan.target_id
        if bound_row is None or app_id is None or target_id is None:
            raise RuntimeError("Repoint was requested without a bound shortcut and a live target")
        rows = plan.rows
        ledger.app_id = app_id
        ledger.committed_action = "repoint_shortcut"
        ledger.action_ambiguous = True
        try:
            switch = await shielded(self._switch_version(app_id, target_id, plan.drifted and handle is not None))
        except asyncio.CancelledError as exc:
            self._record_switch_cancellation(exc, rows, ledger, target_id, app_id, handle)
            raise
        launch_options, result = self._switch_outcome(switch, rows, ledger, target_id, app_id, handle)
        if result is not None:
            return None, None, result
        if launch_options is None:
            raise RuntimeError("Successful version switch did not produce launch options")
        await shielded(
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
                plan.group_ids,
            )
        except asyncio.CancelledError as exc:
            self._record_repoint_cancellation(exc, ledger)
            raise
        return launch_options, "repoint_shortcut", self._repoint_action_outcome(action, ledger)

    def _record_switch_cancellation(
        self,
        exc: asyncio.CancelledError,
        rows: list[Rom],
        ledger: MutationLedger,
        target_id: int,
        app_id: int,
        handle: RecoveryHandle | None,
    ) -> None:
        """Keep a version switch that finished as the cancelled run's outcome."""
        state = cancellation_state(exc)
        if state.child_completed and isinstance(state.child_result, dict):
            _, state.group_result = self._switch_outcome(state.child_result, rows, ledger, target_id, app_id, handle)
            if state.group_result is None:
                state.group_result = self._results.ledger_result(
                    ledger,
                    "cancelled",
                    "Cleanup was cancelled after the version binding changed; later groups were not started.",
                )

    def _record_repoint_cancellation(self, exc: asyncio.CancelledError, ledger: MutationLedger) -> None:
        """Keep a repoint Steam confirmed as the cancelled run's outcome."""
        state = cancellation_state(exc)
        if state.action_result is not None:
            state.group_result = self._repoint_action_outcome(state.action_result, ledger)
            if state.group_result is None:
                state.group_result = self._results.ledger_result(
                    ledger,
                    "cancelled",
                    "Cleanup was cancelled after Steam confirmed the repoint; later groups were not started.",
                )

    async def remove(
        self,
        run_id: str,
        plan: GroupPlan,
        ledger: MutationLedger,
        handle: RecoveryHandle | None,
        frontend_steam: dict[str, object] | None,
        index: int,
        total: int,
    ) -> tuple[Literal["remove_shortcut"] | None, dict[str, Any] | None]:
        """Have Steam drop the shortcut of a game whose every version is gone."""
        app_id = plan.app_id
        bound_row = plan.bound_row
        if bound_row is None or app_id is None:
            raise RuntimeError("Shortcut removal was requested without a bound shortcut")
        rows = plan.rows
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
                plan.group_ids,
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
        return await self._remove_action_outcome(action, rows, ledger, bound_row.rom_id, app_id, handle)

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
                reconciled = await shielded(
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
                GroupOutcome(bundle_path=handle.bundle_path if handle else None),
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
                GroupOutcome(bundle_path=handle.bundle_path if handle else None),
            )
        ledger.app_id = app_id
        ledger.committed_action = "remove_shortcut"
        ledger.action_ambiguous = False
        try:
            reconciled = await shielded(
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
            GroupOutcome(
                app_id=app_id,
                removed_app_id=app_id,
                bundle_path=handle.bundle_path if handle else None,
                committed_action="remove_shortcut",
            ),
        )


__all__ = ["SteamActionRunner", "SteamActionRunnerConfig"]
