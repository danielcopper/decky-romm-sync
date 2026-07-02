"""TypedDicts for RomM's 4.9 Device Sync (negotiate) wire protocol.

The dict shapes exchanged with ``POST /api/sync/negotiate`` and
``POST /api/sync/sessions/{id}/complete``: the client's per-save inventory
(``ClientSaveState``), the server's planned operations (``SyncOperation`` /
``SyncNegotiateResponse``), and the session-completion records. Mirrors the
shipped RomM 4.9.2 OpenAPI schema field-for-field — required keys are plain,
server-optional / nullable keys are ``NotRequired[... | None]``. Runtime dicts;
these describe the wire contract without changing their identity.

Note (ADR-0017): ``negotiate`` is kept only as a session **transport**. The
save-sync engine reads its response solely for ``session_id`` (the session
envelope) and intentionally ignores the planned ``operations`` — detection is the
local ``compute_sync_action`` matrix. ``SyncOperation`` / ``SyncNegotiateResponse``
remain the typed wire shape of the response, not a decision input.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class ClientSaveState(TypedDict):
    """One save in the inventory the client POSTs to ``negotiate``.

    ``content_hash`` is RomM's zip-aware content hash (see ``domain.save_hash``);
    ``slot`` / ``emulator`` are omitted or null for legacy ``slot:null`` saves.
    """

    rom_id: int
    file_name: str
    updated_at: str
    file_size_bytes: int
    slot: NotRequired[str | None]
    emulator: NotRequired[str | None]
    content_hash: NotRequired[str | None]


class SyncOperation(TypedDict):
    """One operation the server plans for a save in the negotiated session.

    ``action`` is the server's verdict and ``reason`` its human-readable
    justification. The server detects but never resolves — a ``conflict``
    carries no resolution directive; the client owns resolution.
    """

    action: Literal["upload", "download", "conflict", "no_op"]
    rom_id: int
    file_name: str
    reason: str
    save_id: NotRequired[int | None]
    slot: NotRequired[str | None]
    emulator: NotRequired[str | None]
    server_updated_at: NotRequired[str | None]
    server_content_hash: NotRequired[str | None]


class SyncNegotiateResponse(TypedDict):
    """The server's response to ``POST /api/sync/negotiate``."""

    session_id: int
    operations: list[SyncOperation]
    total_upload: int
    total_download: int
    total_conflict: int
    total_no_op: int


class SyncPlaySessionEntry(TypedDict):
    """One play-session window reported alongside session completion (#1219)."""

    start_time: str
    end_time: str
    duration_ms: int
    rom_id: NotRequired[int | None]
    save_slot: NotRequired[str | None]


class SyncSession(TypedDict):
    """A sync session's server record (RomM ``SyncSessionSchema``)."""

    id: int
    device_id: str
    user_id: int
    status: str
    initiated_at: str
    operations_planned: int
    operations_completed: int
    operations_failed: int
    created_at: str
    updated_at: str
    completed_at: NotRequired[str | None]
    error_message: NotRequired[str | None]


class SyncCompleteResponse(TypedDict):
    """The server's response to ``POST /api/sync/sessions/{id}/complete``.

    ``play_session_ingest`` is the optional play-session ingest summary (#1219),
    typed loosely until play sessions are wired.
    """

    session: SyncSession
    play_session_ingest: NotRequired[dict[str, Any] | None]
