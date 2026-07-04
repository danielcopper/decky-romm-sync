"""TypedDicts for RomM's standalone play-session ingest wire protocol.

The dict shapes exchanged with ``POST /api/play-sessions`` (the additive
per-session ingest of ADR-0018): the per-session entries the client batches
under a top-level ``device_id`` (``PlaySessionIngestEntry``) and the server's
per-entry ingest verdict (``PlaySessionIngestResult`` / ``PlaySessionIngestResponse``).
Kept separate from ``models/sync.py`` — this ingest is decoupled from the
save-sync ``negotiate`` session lifecycle. Runtime dicts; these describe the
wire contract without changing their identity.

The request envelope (``{"device_id", "sessions"}``) is assembled inline at the
adapter, not modeled here; the ``GET`` history response is a bare
``list[dict[str, Any]]`` (unvalidated at the seam, summed by ``duration_ms``).

A whole-batch validation failure (RomM validates the ``sessions`` array
atomically — any invalid entry rejects the entire POST, #1312) is a
transport-level HTTP 422, surfaced as ``lib.errors.RommUnprocessableEntityError``
carrying the failing ``sessions`` indices in its ``detail`` — NOT a per-entry
verdict in ``results``.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class PlaySessionIngestEntry(TypedDict):
    """One play-session window in the batch the client POSTs to ``/api/play-sessions``.

    ``start_time`` / ``end_time`` are the ISO-8601 session window; ``duration_ms``
    is the suspend-adjusted screen-on time (our counted seconds x 1000). The
    server dedupes on ``(user_id, device_id, rom_id, start_time)``.
    """

    rom_id: int
    start_time: str
    end_time: str
    duration_ms: int


class PlaySessionIngestResult(TypedDict):
    """The server's verdict for one submitted session, correlated by ``index``.

    ``index`` is the entry's position in the submitted ``sessions`` batch;
    ``status`` is the per-session verdict:

    - ``created`` — newly stored.
    - ``duplicate`` — already present, a no-op; still a successful ingest.
    - ``skipped`` — acknowledged but deliberately not stored (e.g. a sub-second
      launch-death the server rejects on validation). A terminal verdict: the
      server will draw the same one for the byte-identical window forever.
    - ``error`` — the server hit an error storing this row (possibly transient).

    ``id`` is the stored session row id when ``created``; ``detail`` is the
    server's optional human-readable explanation for a non-``created`` verdict.
    """

    index: int
    status: Literal["created", "duplicate", "skipped", "error"]
    id: NotRequired[int | None]
    detail: NotRequired[str | None]


class PlaySessionIngestResponse(TypedDict):
    """The server's response to ``POST /api/play-sessions``."""

    results: list[PlaySessionIngestResult]
    created_count: int
    skipped_count: int
