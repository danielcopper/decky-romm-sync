"""Mutation bookkeeping and the bounded wire frames one cleanup run publishes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lib.list_result import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from domain.rom import Rom
    from services.prune._models import RecoveryHandle

_COMPLETION_IDS_PER_GROUP = 50
_COMPLETION_TEXT_CHARS = 512
_COMPLETION_PATH_CHARS = 2048
_COMPLETION_REASON_CHARS = 128
_COMPLETION_WARNING_CHARS = 256
_COMPLETION_WARNINGS_PER_GROUP = 5
_COMPLETION_BUDGET_BYTES = 48 * 1024


@dataclass
class MutationLedger:
    """What one group already changed, so every later outcome stays truthful."""

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


@dataclass(frozen=True)
class GroupOutcome:
    """What one group's cleanup did, beyond the status it ended in.

    Every field is optional because most outcomes are a refusal that changed
    nothing, and a field reaches the completion frame only when it happened.
    """

    removed_rom_ids: list[int] | None = None
    app_id: int | None = None
    removed_app_id: int | None = None
    bundle_path: str | None = None
    committed_action: str | None = None
    mutations: list[str] | None = None
    ambiguous_mutations: list[str] | None = None
    warnings: list[str] | None = None
    action_ambiguous: bool = False
    target_rom_id: int | None = None


_NOTHING_COMMITTED = GroupOutcome()


def _removed_count(results: list[dict[str, Any]]) -> int:
    """How many rows the run removed, counting each group's own tally."""
    return sum(int(result.get("removed_count", len(result.get("removed_rom_ids", [])))) for result in results)


def _is_partial(results: list[dict[str, Any]], removed_count: int, *, failed: bool) -> bool:
    """Whether the run changed something without finishing what it set out to do.

    A group reporting ``partial`` makes the run partial outright; so does any
    committed change in a run that also failed, was cancelled, or skipped a
    group — reporting that as a clean success would hide the mutation.
    """
    if any(result["status"] == "partial" for result in results):
        return True
    committed = bool(removed_count) or any(
        result.get("committed_action")
        or result.get("action_ambiguous")
        or result.get("mutations")
        or result.get("ambiguous_mutations")
        for result in results
    )
    return committed and failed


def _needs_publication(result: dict[str, Any]) -> bool:
    """Whether a group's confirmed repoint leaves Steam needing a publish."""
    return (
        result.get("committed_action") == "repoint_shortcut"
        and not result.get("action_ambiguous")
        and type(result.get("app_id")) is int
        and type(result.get("target_rom_id")) is int
    )


@dataclass(frozen=True)
class PruneResultReporterConfig:
    """Event dependency for one cleanup run's published frames."""

    emit: Callable[..., Awaitable[None]]


class PruneResultReporter:
    """Shape every cleanup outcome and publish it within the Decky wire budget."""

    def __init__(self, *, config: PruneResultReporterConfig) -> None:
        self._emit = config.emit
        self._run_preview_id: str | None = None

    def bind_run(self, preview_id: str) -> None:
        """Bind every later frame to the preview that authorized this run."""
        self._run_preview_id = preview_id

    def end_run(self) -> None:
        """Release the run's preview binding once its frames are published."""
        self._run_preview_id = None

    async def emit_progress(
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
            "preview_id": self._run_preview_id,
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

    async def emit_completion(
        self,
        run_id: str,
        results: list[dict[str, Any]],
        *,
        cancelled: bool,
        reason: str | None,
        message: str | None,
    ) -> None:
        failures = [result for result in results if result["status"] in {"failed", "skipped", "partial"}]
        removed_count = _removed_count(results)
        partial = _is_partial(results, removed_count, failed=bool(failures) or cancelled or reason is not None)
        publication_required = any(_needs_publication(result) for result in results)
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
                publication_required=publication_required,
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
                publication_required=publication_required,
            )
            await self._emit("prune_complete", payload)

    def _completion_payload(
        self,
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
        publication_required: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": success,
            "partial": partial,
            "run_id": run_id,
            "preview_id": self._run_preview_id,
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
        if publication_required:
            payload["publication_required"] = True
        if reason is not None:
            payload["reason"] = reason
        if message is not None:
            payload["message"] = message
        return payload

    def ledger_result(
        self,
        ledger: MutationLedger,
        reason: str,
        message: object,
        *,
        removed_app_id: int | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.group_result(
            ledger.rows,
            "partial",
            reason,
            message,
            GroupOutcome(
                app_id=ledger.app_id,
                removed_app_id=removed_app_id,
                bundle_path=ledger.bundle_path,
                committed_action=ledger.committed_action,
                mutations=ledger.mutations,
                ambiguous_mutations=ledger.ambiguous_mutations,
                warnings=warnings,
                action_ambiguous=ledger.action_ambiguous,
                target_rom_id=ledger.target_rom_id,
            ),
        )

    def ledger_or_guard_result(
        self,
        ledger: MutationLedger,
        rows: list[Rom],
        guard: tuple[str, str],
        handle: RecoveryHandle | None,
        app_id: int | None,
        *,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        if ledger.has_commit():
            return self.ledger_result(
                ledger,
                guard[0],
                guard[1],
                removed_app_id=app_id
                if ledger.committed_action == "remove_shortcut" and not ledger.action_ambiguous
                else None,
                warnings=warnings,
            )
        return self.group_result(
            rows,
            "skipped",
            guard[0],
            guard[1],
            GroupOutcome(bundle_path=handle.bundle_path if handle else None, warnings=warnings),
        )

    def fault_result(self, ledger: MutationLedger, rows: list[Rom], error: BaseException) -> dict[str, Any]:
        if ledger.has_commit():
            return self.ledger_result(ledger, ErrorCode.UNKNOWN.value, str(error))
        return self.group_result(rows, "failed", ErrorCode.UNKNOWN.value, str(error))

    @staticmethod
    def group_result(
        rows: list[Rom],
        status: str,
        reason: str | None,
        message: object,
        outcome: GroupOutcome = _NOTHING_COMMITTED,
    ) -> dict[str, Any]:
        removed_rom_ids = outcome.removed_rom_ids
        warnings = outcome.warnings
        raw_group_id = rows[0].sibling_group_key or f"rom:{rows[0].rom_id}"
        all_rom_ids = [row.rom_id for row in rows]
        bounded_removed = (removed_rom_ids or [])[:_COMPLETION_IDS_PER_GROUP]
        raw_message = str(message)
        # The name the user knows the game by, so a result reads as a sentence
        # instead of being prefixed with a metadata key. The bound row is the
        # group's representative — it is the one with the Steam shortcut — and
        # any member's name identifies the game when nothing is bound.
        bound_row = next((row for row in rows if row.shortcut_app_id is not None), None)
        raw_name = (bound_row or rows[0]).name
        result: dict[str, Any] = {
            "group_id": raw_group_id[:_COMPLETION_TEXT_CHARS],
            "group_id_truncated": len(raw_group_id) > _COMPLETION_TEXT_CHARS,
            "name": raw_name[:_COMPLETION_TEXT_CHARS],
            "name_truncated": len(raw_name) > _COMPLETION_TEXT_CHARS,
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
        if outcome.app_id is not None:
            result["app_id"] = outcome.app_id
        if outcome.removed_app_id is not None:
            result["removed_app_id"] = outcome.removed_app_id
        if outcome.bundle_path is not None:
            bundle_path = outcome.bundle_path
            result["bundle_path"] = bundle_path[:_COMPLETION_PATH_CHARS]
            result["bundle_path_truncated"] = len(bundle_path) > _COMPLETION_PATH_CHARS
        if outcome.committed_action is not None:
            result["committed_action"] = outcome.committed_action
        if outcome.mutations:
            result["mutations"] = [str(item)[:_COMPLETION_REASON_CHARS] for item in outcome.mutations]
        if outcome.ambiguous_mutations:
            result["ambiguous_mutations"] = [
                str(item)[:_COMPLETION_REASON_CHARS] for item in outcome.ambiguous_mutations
            ]
        if warnings:
            bounded_warnings = [
                str(item)[:_COMPLETION_WARNING_CHARS] for item in warnings[:_COMPLETION_WARNINGS_PER_GROUP]
            ]
            result["warnings"] = bounded_warnings
            result["warning_count"] = len(warnings)
            result["warnings_omitted"] = len(warnings) > len(bounded_warnings)
            result["warnings_truncated"] = any(
                len(str(item)) > _COMPLETION_WARNING_CHARS for item in warnings[:_COMPLETION_WARNINGS_PER_GROUP]
            )
        if outcome.action_ambiguous:
            result["action_ambiguous"] = True
        if outcome.target_rom_id is not None:
            result["target_rom_id"] = outcome.target_rom_id
        return result
