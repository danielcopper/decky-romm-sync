"""Work-queue unit for the per-unit sync pipeline.

A :class:`WorkUnit` names one platform or one collection that the
per-unit sync pipeline will fetch + apply as a self-contained slice.
The queue is built in Phase 0 from enabled-platforms and
enabled-collections settings; downstream sub-services dispatch on
:attr:`WorkUnit.type` to choose the platform-fetch vs collection-
fetch path. Anything that requires inter-unit context (running
``synced_rom_ids`` deduplication, accumulating registry deltas) is
threaded through separately — the unit itself is a static descriptor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

UnitType = Literal["platform", "collection"]
CollectionKind = Literal["user", "smart", "franchise"]


@dataclass(frozen=True)
class WorkUnit:
    """One platform or one collection in the per-unit work queue."""

    type: UnitType
    id: int | str
    name: str
    slug: str
    rom_count: int
    # Collection-only: dispatches the correct list-roms endpoint at fetch time.
    # ``None`` is only valid when ``type == "platform"``.
    collection_kind: CollectionKind | None = None
    # Collection-only: the collection's server ``updated_at`` from the listing,
    # threaded so the incremental-skip gate can compare it against the stored
    # ``CollectionSyncState`` stamp (#742). RomM bumps this on any membership
    # add/remove (and a smart-criteria edit), so an equal value is the
    # membership-stable signal. ``None`` when the listing omits it (e.g. a
    # franchise collection, which is never stamped) — skip-internal, off the wire.
    collection_updated_at: str | None = None
    # Plan-time estimate riders (#1382 / #1511 / #1517). ``predicted_skip`` is
    # the plan's local-conditions guess at the fetch-time wholesale-skip gate's
    # outcome, ``collapsed_count`` the persisted post-collapse shortcut count
    # and ``new_shortcut_count`` how many of those shortcuts do not exist yet —
    # all three platform-only. ``bound_count`` carries on BOTH unit types:
    # how many of the unit's known ROMs already hold a Steam shortcut, which is
    # what lets the frontend price them at the cheap UPDATE rate instead of the
    # create rate (a platform reads its persisted rows; a collection reads its
    # stamped member set).
    # Estimate-ONLY: they price the ``sync_plan`` payload and must never feed
    # the actual skip decision — ``_try_unit_incremental_skip`` remains the
    # sole skip authority (ADR-0023). ``None`` means unknown and is omitted
    # from the event payload.
    predicted_skip: bool | None = None
    collapsed_count: int | None = None
    bound_count: int | None = None
    new_shortcut_count: int | None = None

    def estimated_items(self) -> int:
        """This unit's weight in the plan's skip-aware estimate total.

        ``0`` when the plan predicts the wholesale skip, else the persisted
        post-collapse shortcut count, falling back to the raw ``rom_count``
        when no collapsed count is known. Estimate-only — prices the
        ``sync_plan`` payload, never the actual skip decision (ADR-0023).
        """
        if self.predicted_skip:
            return 0
        return self.collapsed_count if self.collapsed_count is not None else self.rom_count

    def to_event_payload(self) -> dict[str, Any]:
        """Serialise to the shape emitted in ``sync_plan`` / ``sync_apply_unit``."""
        payload: dict[str, Any] = {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "rom_count": self.rom_count,
        }
        if self.type == "collection":
            payload["collection_kind"] = self.collection_kind
        if self.predicted_skip is not None:
            payload["predicted_skip"] = self.predicted_skip
        if self.collapsed_count is not None:
            payload["collapsed_count"] = self.collapsed_count
        if self.bound_count is not None:
            payload["bound_count"] = self.bound_count
        if self.new_shortcut_count is not None:
            payload["new_shortcut_count"] = self.new_shortcut_count
        return payload
