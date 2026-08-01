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

from domain.collection_owner import is_own_collection

UnitType = Literal["platform", "collection"]
CollectionKind = Literal["standard", "smart", "virtual"]


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
    # Virtual-collection-only: which virtual type this unit is (``"franchise"``
    # or ``"collection"``), stamped from the query type at fetch time. Threaded
    # so the reporter can build the fine display label for the ``by_label``
    # Steam-collection naming mode (a ``kind == "virtual"`` unit needs the
    # sub-type to distinguish Franchise from IGDB Collection). ``None`` for a
    # platform or a standard/smart collection (their kind alone names them).
    virtual_type: str | None = None
    # Collection-only: the collection's server ``updated_at`` from the listing,
    # threaded so the incremental-skip gate can compare it against the stored
    # ``CollectionSyncState`` stamp (#742). RomM bumps this on any membership
    # add/remove (and a smart-criteria edit), so an equal value is the
    # membership-stable signal. ``None`` when the listing omits it (e.g. a
    # virtual collection, which is never stamped) — skip-internal, off the wire.
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


def collection_units(
    collections: list[dict[str, Any]],
    enabled_ids: set[str],
    kind: CollectionKind,
    *,
    virtual_type: str | None = None,
    own_user_id: int | None = None,
    filter_to_own: bool = False,
) -> list[WorkUnit]:
    """Build WorkUnits for collections whose id is in *enabled_ids*, tagged with *kind*.

    When *filter_to_own* is set (the "Mine" owner-scope), a foreign collection —
    one owned by a known user id other than *own_user_id* — is dropped from the
    queue even if it is enabled, so a scope selected over an earlier enable never
    syncs someone else's collection. Virtual collections have no owner and
    always survive (:func:`is_own_collection`).

    *virtual_type* stamps the unit's virtual sub-type (``"franchise"`` /
    ``"collection"``) for the ``kind == "virtual"`` caller, which fetches one
    type at a time and so knows it authoritatively — the same source the QAM
    listing uses. ``None`` for standard/smart callers (their kind alone labels
    them).
    """
    units: list[WorkUnit] = []
    for c in collections:
        cid = str(c.get("id", ""))
        if cid not in enabled_ids:
            continue
        if filter_to_own and not is_own_collection(c.get("user_id"), own_user_id, kind=kind):
            continue
        units.append(
            WorkUnit(
                type="collection",
                id=cid,
                name=c.get("name", cid),
                slug=c.get("slug", ""),
                rom_count=int(c.get("rom_count", len(c.get("rom_ids", [])))),
                collection_kind=kind,
                virtual_type=virtual_type,
                # RomM bumps the collection's updated_at on any membership change
                # (#742). Threaded so the skip gate compares it against the stamp;
                # ``None`` for a listing that omits it (e.g. virtual, never
                # stamped).
                collection_updated_at=c.get("updated_at"),
            )
        )
    return units
