"""How many locally persisted rows the platform incremental skip may count.

The generation marker's read side: given a platform's ``roms`` rows and the
generation its completion stamp recorded, decide the row count the skip's
"local mirror matches the server" condition compares against RomM's platform ROM
count. Anything that decides whether to skip belongs to the fetcher, and
anything that writes a generation belongs to the reporter; this module only
decides which rows still count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from domain.platform_sync_state import PlatformSyncState
    from domain.rom import Rom


def count_rows_for_skip(rows: Sequence[Rom], fetch_id: str | None) -> int:
    """Count the rows the skip may compare against the server's ROM count.

    Every fetched sibling persists (ADR-0021), so bound and unbound rows count
    alike. What must NOT count is a row for a rom_id the server has since
    dropped: RomM re-creating a ROM under a new id leaves the old row behind
    unbound, and ADR-0007 keeps it forever as an identity anchor, so counting it
    means the platform can never satisfy the condition again (#1504). When the
    stamp names a fetch generation, only the rows carrying it count — a row the
    last complete fetch returned has that generation, a superseded row keeps an
    older one — which excludes exactly the dropped ids while deleting nothing.

    A stamp with **no** generation (``fetch_id`` falsy) predates this contract
    and cannot say what its fetch saw, so every row counts, exactly as before
    #1504. That legacy path is deliberately permissive rather than refusing to
    count: a platform with no superseded rows keeps skipping straight through
    the upgrade instead of paying a forced re-fetch, and a platform that DOES
    carry them already fails the count today, so it full-fetches until both
    sides are re-stamped. That re-stamp lands on the next sync that APPLIES
    something, not simply the next sync — the generation is written by the
    apply's commit, and a run whose library-wide delta is empty stops at the
    preview and reaches no commit.
    """
    if not fetch_id:
        return len(rows)
    return sum(1 for row in rows if row.last_fetch_id == fetch_id)


def backfill_needed(rows: Sequence[Rom], fetch_id: str | None) -> bool:
    """Whether a NULL ``sibling_group_key`` among *rows* may force a full fetch.

    Sibling twin of :func:`count_rows_for_skip` and gated on the same generation:
    a full fetch can only backfill a row the server still returns, so only a row
    carrying the stamp's generation may demand one. A row for a rom_id RomM has
    dropped keeps its older generation (ADR-0007 retains it, nothing is deleted),
    and no fetch will ever fill its key in — letting it force a backfill would
    wedge the platform into a full fetch on every sync, forever.

    A stamp with **no** generation (``fetch_id`` falsy) predates the contract and
    cannot say what its fetch saw, so every NULL-key row still forces the fetch,
    exactly as before — the same deliberately permissive legacy path
    :func:`count_rows_for_skip` takes, here erring towards fetching rather than
    skipping.
    """
    if not fetch_id:
        return any(rom.sibling_group_key is None for rom in rows)
    return any(rom.sibling_group_key is None and rom.last_fetch_id == fetch_id for rom in rows)


def prune_candidate_ids(rows: Sequence[Rom], stamp: PlatformSyncState | None) -> set[int]:
    """Return rows absent from a known non-empty completed platform fetch.

    This is discovery only, never deletion authority. A missing/legacy stamp or
    an empty completed fetch cannot establish a safe comparison set and yields
    no candidates. With a usable generation, every row carrying a different
    generation is a candidate, including rows whose ``last_fetch_id`` is NULL.
    """
    if stamp is None or not stamp.fetch_id or stamp.rom_count <= 0:
        return set()
    return {row.rom_id for row in rows if row.last_fetch_id != stamp.fetch_id}


def current_generation_ids(rows: Sequence[Rom], stamp: PlatformSyncState | None) -> set[int]:
    """Return rows a known non-empty completed fetch is recorded as having returned.

    The mirror of :func:`prune_candidate_ids`: those are the rows absent from the
    last complete fetch, these are the ones present in it. A missing, legacy, or
    empty stamp yields nothing, because it cannot establish that any row was
    seen. Useful wherever a row that RomM *should* still serve is needed as a
    control — it is a local record of what the server last said, never a promise
    about what it says now.
    """
    if stamp is None or not stamp.fetch_id or stamp.rom_count <= 0:
        return set()
    return {row.rom_id for row in rows if row.last_fetch_id == stamp.fetch_id}
