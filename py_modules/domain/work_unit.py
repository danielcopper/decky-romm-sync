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
from typing import Literal

UnitType = Literal["platform", "collection"]


@dataclass(frozen=True)
class WorkUnit:
    """One platform or one collection in the per-unit work queue."""

    type: UnitType
    id: int | str
    name: str
    slug: str
    rom_count: int
    # Collection-only: discriminates user/franchise lookup at fetch time.
    is_virtual: bool = False

    def to_event_payload(self) -> dict:
        """Serialise to the shape emitted in ``sync_plan`` / ``sync_apply_unit``."""
        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "rom_count": self.rom_count,
        }
