"""Plan-time estimate kernels for the skip-aware sync time estimate.

Pure predictions derived from locally persisted state: whether the
fetch-time wholesale-skip gate is expected to skip a platform unit, and
how many Steam shortcuts a platform's persisted rows collapse into.
Both exist ONLY to price the ``sync_plan`` estimate payload
(``predicted_skip`` / ``collapsed_count`` / ``total_estimated_items``).

Hard constraint (ADR-0023): the fetch-time gate
(``LibraryFetcher._try_unit_incremental_skip``) remains the SOLE skip
authority — nothing computed here may ever feed the actual skip
decision. A mis-prediction can only mis-estimate (the countdown reads
long or short); it can never mis-apply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def predict_unit_skip(
    *,
    stamp_completed_at: str | None,
    stamp_rom_count: int | None,
    unit_rom_count: int,
    persisted_count: int,
    registry_count: int,
    needs_backfill: bool,
) -> bool:
    """Predict whether the wholesale-skip gate will skip a platform unit.

    Replays the gate's LOCAL conditions only: a completion stamp exists
    (truthy ``completed_at``, non-``None`` ``rom_count``), the stamped ROM
    count still matches the server's ``rom_count`` for the unit, rows are
    persisted and at least one is bound, no ``sibling_group_key`` backfill
    is pending, and the persisted row count matches the server count. The
    gate's server-delta check (``list_roms_updated_after``) is deliberately
    NOT replayed — no network at plan time — so a platform whose rows
    changed server-side since the stamp may be predicted as a skip that the
    fetch then refuses; the estimate reads short, the apply stays correct.

    A Force Full Sync needs no special case: ``clear_sync_cache`` deletes
    every stamp before the run starts, so ``stamp_completed_at`` reads
    ``None`` at plan time and no skip is predicted.

    Estimate-only (ADR-0023): the return value prices the plan payload and
    must never feed the actual skip decision.
    """
    return (
        bool(stamp_completed_at)
        and stamp_rom_count is not None
        and stamp_rom_count == unit_rom_count
        and persisted_count > 0
        and persisted_count == unit_rom_count
        and registry_count > 0
        and not needs_backfill
    )


def collapsed_shortcut_count(rows: Iterable[tuple[str | None, bool]]) -> int:
    """Post-collapse shortcut count for one platform's persisted rows.

    Mirrors the lane selection of ``collapse_sibling_groups`` (ADR-0021)
    under the plan-time assumption that the next fetch matches the
    persisted rows: a group with **bound** siblings is grandfathered — one
    shortcut per bound sibling (§5, a pre-ADR-0021 library keeps its
    duplicate shortcuts) — while a group with no binding anywhere mints
    exactly one representative. Given every persisted row as
    ``(sibling_group_key, is_bound)`` (bound = ``shortcut_app_id`` is set),
    each non-``None`` group therefore counts ``max(1, bound rows in
    group)``, and a keyless row is its own singleton (its real group is
    unknown until the backfill fetch computes the key).
    """
    bound_by_key: dict[str, int] = {}
    singletons = 0
    for key, is_bound in rows:
        if key is None:
            singletons += 1
        else:
            bound_by_key[key] = bound_by_key.get(key, 0) + (1 if is_bound else 0)
    return sum(max(1, bound) for bound in bound_by_key.values()) + singletons
