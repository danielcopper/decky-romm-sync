"""Decide what one group's cleanup would do, before anything can be changed.

Reads the group as it stands right now — local rows, active downloads, fresh
liveness, unsynced saves — and either refuses it with a terminal result or
freezes the decision as a :class:`GroupPlan`. Every later phase re-proves what it
is about to touch, so nothing decided here is trusted as still true; the plan
carries only the choices that cannot be re-derived, which is what keeps the
phases separable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.prune import natural_default, selected_prune_ids
from domain.sibling_resolution import AUTO_REGION
from services.prune.liveness import UNCONFIRMED_REASON

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.rom import Rom
    from services.protocols import ActiveDownloadRomIdsFn, SaveDriftProbeFn
    from services.prune._models import PruneOptions
    from services.prune.liveness import LivenessProber
    from services.prune.registry import PruneRegistry
    from services.prune.results import MutationLedger, PruneResultReporter


@dataclass(frozen=True)
class GroupPlan:
    """What one group's later phases act on, frozen at the end of planning.

    Only the decisions downstream cannot re-derive: liveness verdicts, candidate
    intersections and skip reasons all die here, because arming, the Steam
    action and finalization each re-prove liveness and local state from scratch.
    """

    rows: list[Rom]
    group_ids: set[int]
    bound_row: Rom | None
    app_id: int | None
    delete_ids: set[int]
    target_id: int | None
    fully_dead: bool
    whole_game_action: bool
    drifted: bool


@dataclass(frozen=True)
class _GroupLiveness:
    """One group's ids sorted by what RomM proved about each of them."""

    vanished_ids: set[int]
    live_ids: set[int]
    uncertain_ids: set[int]
    fully_dead: bool

    @classmethod
    def of(cls, group_ids: set[int], verdicts: dict[int, dict[str, str]]) -> _GroupLiveness:
        vanished_ids = {rom_id for rom_id, verdict in verdicts.items() if verdict["status"] == "vanished"}
        live_ids = {rom_id for rom_id, verdict in verdicts.items() if verdict["status"] == "live"}
        return cls(
            vanished_ids=vanished_ids,
            live_ids=live_ids,
            uncertain_ids=group_ids - vanished_ids - live_ids,
            fully_dead=bool(group_ids) and group_ids <= vanished_ids,
        )


@dataclass(frozen=True)
class GroupPlannerConfig:
    """Dependencies for deciding one group's cleanup without mutating anything."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    results: PruneResultReporter
    registry: PruneRegistry
    liveness: LivenessProber
    active_downloads: ActiveDownloadRomIdsFn
    drift_probe: SaveDriftProbeFn
    settings: dict[str, Any]


_NO_LIVE_DEFAULT = -1


def _unproven_refusal(reasons: set[str], unproven_count: int) -> tuple[str, str]:
    """Name the most specific reason a group with no live answer is refused.

    Ordered by how much it tells the user: a namespace change and an
    unconfirmed server both explain *why* no answer could be trusted, and only
    the generic case is a plain count.
    """
    if "server_namespace_changed" in reasons:
        return (
            "server_namespace_changed",
            "The RomM server or user changed during exact-ID proof; nothing was removed.",
        )
    if UNCONFIRMED_REASON in reasons:
        return (
            UNCONFIRMED_REASON,
            "RomM's answers could not be confirmed, so a 404 could not be trusted; nothing was removed.",
        )
    return "liveness_uncertain", f"RomM could not confirm {unproven_count} group member(s); nothing was removed."


class GroupPlanner:
    """Turn one candidate group into a plan, or into the reason there is none."""

    def __init__(self, *, config: GroupPlannerConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._results = config.results
        self._registry = config.registry
        self._liveness = config.liveness
        self._active_downloads = config.active_downloads
        self._drift_probe = config.drift_probe
        self._settings = config.settings

    def _repoint_target(
        self,
        rows: list[Rom],
        bound_row: Rom | None,
        vanished_ids: set[int],
        live_ids: set[int],
        options: PruneOptions,
    ) -> int | None:
        """The live row a vanished shortcut should move onto, if it should move.

        ``None`` when no repoint applies at all; ``_NO_LIVE_DEFAULT`` when one
        does but the group's live rows cannot yield a representative, which is a
        refusal rather than a no-op.
        """
        if bound_row is None or bound_row.rom_id not in vanished_ids or not live_ids:
            return None
        if not options.repoint_shortcuts:
            return None
        target_id = natural_default(rows, live_ids, self._settings.get("preferred_region", AUTO_REGION))
        return _NO_LIVE_DEFAULT if target_id is None else target_id

    async def plan(
        self,
        run_id: str,
        initial_rows: list[Rom],
        preview_candidate_ids: set[int],
        options: PruneOptions,
        index: int,
        total: int,
        ledger: MutationLedger,
    ) -> GroupPlan | dict[str, Any]:
        """Return this group's plan, or the terminal result dict that refuses it."""
        rows = await self._loop.run_in_executor(None, self._registry.reread_group, initial_rows[0].rom_id)
        ledger.rows = rows or initial_rows
        if not rows:
            return self._results.group_result(
                initial_rows, "skipped", "local_state_changed", "The local group changed."
            )
        group_ids = {row.rom_id for row in rows}
        candidate_ids = group_ids & preview_candidate_ids
        bound_row, unstable = self._admit_group(rows, group_ids)
        if unstable is not None:
            return unstable

        verdicts = await self._liveness.probe_many(group_ids)
        proof = _GroupLiveness.of(group_ids, verdicts)
        self._log_liveness(f"{run_id} group {index}/{total}", proof, candidate_ids, bound_row)
        if not proof.live_ids and proof.uncertain_ids:
            reason, message = _unproven_refusal(
                {verdicts[rom_id]["reason"] for rom_id in proof.uncertain_ids}, len(proof.uncertain_ids)
            )
            return self._results.group_result(rows, "skipped", reason, message)

        delete_ids = selected_prune_ids(
            group_ids=sorted(group_ids),
            candidate_ids=candidate_ids,
            vanished_ids=proof.vanished_ids,
            live_ids=proof.live_ids,
            remove_rows=options.remove_rows,
            remove_fully_vanished=options.remove_fully_vanished,
        )
        target_id = self._repoint_target(rows, bound_row, proof.vanished_ids, proof.live_ids, options)
        if target_id is _NO_LIVE_DEFAULT:
            return self._results.group_result(rows, "skipped", "no_live_default", "No live default could be selected.")
        if bound_row is not None and bound_row.rom_id in delete_ids and proof.live_ids and target_id is None:
            # Deleting the row that owns the shortcut would strand a live game.
            delete_ids.remove(bound_row.rom_id)

        whole_game_action = proof.fully_dead and bool(delete_ids)
        if not delete_ids and target_id is None:
            return self._nothing_to_do(rows, proof.uncertain_ids)

        drifted, unsynced = await self._probe_drift(rows, bound_row, proof, target_id, whole_game_action, options)
        if unsynced is not None:
            return unsynced

        return GroupPlan(
            rows=rows,
            group_ids=group_ids,
            bound_row=bound_row,
            app_id=bound_row.shortcut_app_id if bound_row is not None else None,
            delete_ids=delete_ids,
            target_id=target_id,
            fully_dead=proof.fully_dead,
            whole_game_action=whole_game_action,
            drifted=drifted,
        )

    def _log_liveness(
        self, run_label: str, proof: _GroupLiveness, candidate_ids: set[int], bound_row: Rom | None
    ) -> None:
        """Record the verdicts every later decision turns on.

        A group reported as skipped is otherwise impossible to explain after the
        fact without re-running against the same server.
        """
        self._logger.info(
            f"Cleanup run {run_label} liveness: "
            f"gone={sorted(proof.vanished_ids)}, still_there={sorted(proof.live_ids)}, "
            f"unconfirmed={sorted(proof.uncertain_ids)}, "
            f"candidates={sorted(candidate_ids)}, bound={bound_row.rom_id if bound_row else None}"
        )

    def _admit_group(self, rows: list[Rom], group_ids: set[int]) -> tuple[Rom | None, dict[str, Any] | None]:
        """Name the group's bound row, or refuse a group nothing may act on yet."""
        bound = [row for row in rows if row.shortcut_app_id is not None]
        if len(bound) > 1:
            return None, self._results.group_result(
                rows, "skipped", "multiple_bindings", "The group has multiple shortcuts."
            )
        if self._active_downloads() & group_ids:
            return None, self._results.group_result(
                rows, "skipped", "download_in_progress", "Cancel active downloads first."
            )
        return (bound[0] if bound else None), None

    def _nothing_to_do(self, rows: list[Rom], uncertain_ids: set[int]) -> dict[str, Any]:
        """Say why a group with nothing selected has nothing selected.

        Distinguish "your options excluded everything" from "RomM never
        confirmed anything gone". Both leave nothing to do, but only the first
        is answered by changing a toggle — reporting the second as an options
        problem sends the user to fiddle with settings that cannot help.
        """
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

    async def _probe_drift(
        self,
        rows: list[Rom],
        bound_row: Rom | None,
        proof: _GroupLiveness,
        target_id: int | None,
        whole_game_action: bool,
        options: PruneOptions,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Ask whether the shortcut this run would change still holds unsynced saves."""
        if bound_row is None or bound_row.rom_id not in proof.vanished_ids:
            return False, None
        if target_id is None and not whole_game_action:
            return False, None
        drifted = bool((await self._drift_probe(bound_row.rom_id)).get("drifted"))
        if drifted and not options.create_recovery_bundle:
            return drifted, self._results.group_result(
                rows,
                "skipped",
                "unsynced_saves",
                "Unsynced saves require a sealed recovery bundle before changing this shortcut.",
            )
        return drifted, None


__all__ = ["GroupPlan", "GroupPlanner", "GroupPlannerConfig"]
