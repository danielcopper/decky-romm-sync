"""PlatformSyncState — the per-platform "this platform fully synced" completion stamp.

Recorded when a platform work unit finishes its **last** apply chunk (ADR-0023),
so the incremental-skip gate can honor durable per-platform progress that a
cancelled / crashed run leaves behind. A completed ``SyncRun`` advances the
library-wide ``last_sync`` only when the *whole* run finishes; a run that
committed every chunk of platforms A, B, C but was cancelled during platform D
never completes, so ``last_sync`` stays put and the next sync re-walks A, B, C
from zero. This stamp is the per-platform checkpoint that survives that
cancellation: the skip reads ``completed_at`` as the platform's own effective
``last_sync`` and ``rom_count`` as the server count captured at completion (a
later server-side count change invalidates the stamp).

Keyed by ``platform_slug``. A thin record built whole and upserted — never a
partial field mutation — so it carries a single ``stamp`` constructor and no
verb-named mutators. The contract is *stamp exists ⟺ the platform's most recent
apply attempt ran to completion*: it is deleted at a platform unit's apply start
(``sync_orchestrator``) so an interrupted re-apply leaves none and the final
chunk re-writes it, deleted per touched platform by the local destructive flows
(``shortcut_removal``) that unbind shortcuts outside a sync, and cleared wholesale
by Force Full Sync (the repository's ``clear``) — the stamps are the fetcher's sole
skip authority, so clearing them arms the full re-fetch; the ``SyncRun`` history is
preserved (it feeds no skip gate).
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class PlatformSyncState:
    """One platform's last fully-completed sync, keyed by ``platform_slug``."""

    platform_slug: str
    completed_at: str
    rom_count: int

    @classmethod
    def stamp(cls, *, platform_slug: str, at: str, rom_count: int) -> PlatformSyncState:
        """Record that ``platform_slug`` fully synced at ISO timestamp ``at``.

        ``rom_count`` is the server's platform ROM count as of completion — the
        skip re-checks it against the live count and invalidates the stamp on any
        change.
        """
        if not platform_slug:
            raise ValueError("platform_slug is required")
        if rom_count < 0:
            raise ValueError("rom_count must be non-negative")
        return cls(platform_slug=platform_slug, completed_at=at, rom_count=rom_count)
