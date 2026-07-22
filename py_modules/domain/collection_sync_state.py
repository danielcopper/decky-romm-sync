"""CollectionSyncState — the per-collection "this collection fully synced" stamp.

The collection sibling of :class:`domain.platform_sync_state.PlatformSyncState`
(ADR-0023): recorded when a standard/smart collection work unit finishes its **last**
apply chunk, so the incremental-skip gate can avoid re-paginating an unchanged
collection on the next sync. Keyed by ``(collection_id, collection_kind)`` — a
standard collection id and a smart collection id can collide (both are small ints on
the server), so the kind is part of the identity.

Unlike a platform, a collection has no local membership column to reconstruct its
member set from (``roms.platform_slug`` is per-platform; collection membership
lives only on the server). So the stamp additionally stores ``member_rom_ids`` —
the collection's full member set as of completion — which a skipped run replays
into the run's ``synced_rom_ids`` and Steam-collection membership map, resolving
each id to its shortcut through the registry (the same sibling-group fallback the
reporter uses at finalize).

The skip is gated on three verified RomM signals, ALL of which must agree with
the stamp for a skip to fire (see ``LibraryFetcher._try_collection_incremental_skip``):

1. ``updated_at`` unchanged — RomM bumps the Collection/SmartCollection row's
   ``updated_at`` on any membership add/remove (and a smart-criteria edit), so an
   equal value is the membership-stable signal.
2. a scoped ``updated_after`` probe (keyed off ``completed_at``, our own last
   sync time) returns zero — catches a member ROM's content change, and a ROM
   entering a smart collection via its own metadata change (the collection row's
   ``updated_at`` does not move for that).
3. ``rom_count`` unchanged vs. both the live listing and the stored member set.

Only **standard** and **smart** collections carry a stamp — virtual collections
are auto-generated groupings with no stable ``updated_at`` and are never
stamped (they always full-fetch). A thin record built whole and upserted —
never a partial field mutation — so it carries a single ``stamp`` constructor and
no verb-named mutators. Cleared on the same events that clear platform stamps:
the local destructive flows (shortcut removal / live-shortcut reconcile) drop any
stamp whose member set intersects the removed ROMs, and Force Full Sync clears
every stamp wholesale.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class CollectionSyncState:
    """One collection's last fully-completed sync, keyed by ``(collection_id, collection_kind)``."""

    collection_id: str
    collection_kind: str
    updated_at: str
    completed_at: str
    rom_count: int
    member_rom_ids: tuple[int, ...]

    @classmethod
    def stamp(
        cls,
        *,
        collection_id: str,
        collection_kind: str,
        updated_at: str,
        completed_at: str,
        rom_count: int,
        member_rom_ids: tuple[int, ...],
    ) -> CollectionSyncState:
        """Record that a collection fully synced with the given member set.

        ``updated_at`` is the collection's server ``updated_at`` at completion —
        the membership-stable signal the skip re-checks. ``completed_at`` is our
        own ISO sync timestamp, the reference the scoped ``updated_after`` probe
        keys off to detect a member ROM's content change. ``rom_count`` is the
        server's collection ROM count as of completion, and ``member_rom_ids`` is
        the full member set the skip replays to rebuild collection membership
        without re-fetching. Only ``standard`` / ``smart`` kinds are stampable.
        """
        if not collection_id:
            raise ValueError("collection_id is required")
        if collection_kind not in ("standard", "smart"):
            raise ValueError(f"collection_kind must be 'standard' or 'smart', got {collection_kind!r}")
        if not updated_at:
            raise ValueError("updated_at is required")
        if not completed_at:
            raise ValueError("completed_at is required")
        if rom_count < 0:
            raise ValueError("rom_count must be non-negative")
        return cls(
            collection_id=collection_id,
            collection_kind=collection_kind,
            updated_at=updated_at,
            completed_at=completed_at,
            rom_count=rom_count,
            member_rom_ids=tuple(member_rom_ids),
        )
