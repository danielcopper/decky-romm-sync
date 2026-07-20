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
