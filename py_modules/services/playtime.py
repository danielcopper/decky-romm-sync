"""PlaytimeService — playtime tracking via RomM Notes API.

Owns per-ROM play sessions: opening a session, folding its duration into the
``Playtime`` aggregate on close, and reconciling the local total with the
shared RomM record (stored in a ROM note, since RomM has no playtime API). All
durable state lives in the ``rom_playtime`` table behind the Unit of Work; all
RomM communication goes through ``RommPlaytimeApi``. No ``import decky``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.playtime import Playtime, parse_playtime_note_content
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        Clock,
        DebugLogger,
        RetryStrategy,
        RommPlaytimeApi,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class PlaytimeServiceConfig:
    """Frozen wiring bundle handed to ``PlaytimeService.__init__``.

    Holds the Protocol-typed RomM adapter and retry strategy, the live
    ``settings.json`` dict (home of the device label stamped onto synced
    playtime notes), runtime infrastructure, the clock/debug-logger seams,
    and the SQLite Unit-of-Work factory (the transactional seam over the
    ``rom_playtime`` aggregate this service reads and writes).
    """

    romm_api: RommPlaytimeApi
    retry: RetryStrategy
    settings: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    log_debug: DebugLogger
    uow_factory: UnitOfWorkFactory


def _empty_reconcile_result(*, server_query_failed: bool) -> dict[str, Any]:
    """The reconcile result for a missing local row — zero totals."""
    return {"total_seconds": 0, "session_count": 0, "server_query_failed": server_query_failed}


class PlaytimeService:
    """Playtime tracking: record sessions and reconcile with RomM notes."""

    PLAYTIME_NOTE_TITLE = "romm-sync:playtime"

    def __init__(self, *, config: PlaytimeServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._retry = config.retry
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._log_debug = config.log_debug
        self._uow_factory = config.uow_factory

    # ------------------------------------------------------------------
    # Playtime Notes API Helpers
    # ------------------------------------------------------------------

    def _get_playtime_note(self, rom_id: int) -> dict[str, Any] | None:
        """Fetch the playtime note for a ROM via the save API protocol.

        Reads ``all_user_notes`` from ROM detail and filters by title.
        """
        rom_detail = self._romm_api.get_rom_with_notes(rom_id)
        if not isinstance(rom_detail, dict):
            return None
        notes = rom_detail.get("all_user_notes", [])
        if not isinstance(notes, list):
            return None
        for note in notes:
            if note.get("title") == self.PLAYTIME_NOTE_TITLE:
                return note
        return None

    def _create_playtime_note(self, rom_id: int, playtime_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new playtime note for a ROM."""
        return self._romm_api.create_note(
            rom_id,
            {
                "title": self.PLAYTIME_NOTE_TITLE,
                "content": json.dumps(playtime_data),
                "is_public": False,
            },
        )

    def _update_playtime_note(self, rom_id: int, note_id: int, playtime_data: dict[str, Any]) -> dict[str, Any]:
        """Update an existing playtime note."""
        return self._romm_api.update_note(
            rom_id,
            note_id,
            {"content": json.dumps(playtime_data)},
        )

    def _sync_playtime_to_romm_io(self, rom_id: int, session_duration_sec: int) -> None:
        """Push playtime to RomM via the Notes API after a session.

        Reads the current local total from its own short read UoW, fetches the
        server note, merges (server baseline plus this session, or the local
        total, whichever is higher), and creates/updates the note — all RomM
        I/O happens outside any transaction. A single short write UoW then
        links the created note id and reconciles the aggregate's total to the
        merged value. Best-effort — errors are logged, not raised.

        Synchronous worker: the SQLite connection has thread affinity, so the
        UoW must run inside the ``run_in_executor`` worker, not on the loop.
        """
        rom_id = int(rom_id)

        with self._uow_factory() as uow:
            entry = uow.playtime.get(rom_id)
        if not entry:
            return

        local_total = entry.total_seconds
        device_name = self._settings.get("device_name") or ""

        try:
            note = self._retry.with_retry(self._get_playtime_note, rom_id)
            server_seconds = 0
            note_id = None

            if note:
                note_id = note.get("id")
                server_data = parse_playtime_note_content(note.get("content", ""))
                if server_data:
                    server_seconds = int(server_data.get("seconds", 0))

            # Merge: server baseline + this session, or local total, whichever is higher
            new_total = max(local_total, server_seconds + session_duration_sec)

            playtime_data = {
                "seconds": new_total,
                "updated": self._clock.now().isoformat(),
                "device": device_name,
            }

            created_note_id = None
            if note_id:
                self._retry.with_retry(self._update_playtime_note, rom_id, note_id, playtime_data)
            else:
                result = self._retry.with_retry(self._create_playtime_note, rom_id, playtime_data)
                if isinstance(result, dict) and result.get("id"):
                    created_note_id = result["id"]

            self._commit_reconciled_total(rom_id, new_total, created_note_id)

        except Exception as e:
            self._log_debug(f"Failed to sync playtime to RomM for rom {rom_id}: {e}")

    def _commit_reconciled_total(self, rom_id: int, new_total: int, created_note_id: int | None) -> None:
        """Fold the merged total (and any freshly-created note id) into the aggregate.

        Re-reads the aggregate inside its own short write UoW — if the row was
        removed between the RomM round-trip and now, this is a no-op.
        """
        with self._uow_factory() as uow:
            entry = uow.playtime.get(rom_id)
            if entry is None:
                return
            if created_note_id is not None:
                entry.link_note(created_note_id)
            entry.reconcile_total(new_total)
            uow.playtime.save(rom_id, entry)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def record_session_start(self, rom_id: int) -> dict[str, Any]:
        """Record the start of a play session for playtime tracking.

        Opens (or re-opens) the session marker on the ROM's ``Playtime``
        aggregate in a short write UoW. A ``rom_id`` with no matching ``roms``
        row violates the FK at commit; that is reported as a failure rather
        than auto-creating an identity anchor (ADR-0007).
        """
        rid = int(rom_id)
        try:
            with self._uow_factory() as uow:
                pt = uow.playtime.get(rid) or Playtime()
                pt.begin_session(self._clock.now().isoformat())
                uow.playtime.save(rid, pt)
        except sqlite3.IntegrityError as e:
            self._log_debug(f"Failed to record session start for rom {rid}: {e}")
            return {"success": False, "reason": "unknown_rom", "message": "Unknown ROM"}
        return {"success": True}

    async def record_session_end(self, rom_id: int, suspended_seconds: int = 0) -> dict[str, Any]:
        """Record end of play session, accumulate playtime delta.

        Only handles playtime — save sync is handled separately. ``suspended_seconds``
        is the wall-clock time the device spent suspended during the session; it
        is subtracted from the raw elapsed span so suspend time is not counted as
        play. The work runs in an executor: the durable fold happens in a short
        write UoW (the SQLite connection has thread affinity), then the RomM note
        push runs best-effort outside any transaction.
        """
        return await self._loop.run_in_executor(None, self._record_session_end_io, int(rom_id), suspended_seconds)

    def _record_session_end_io(self, rom_id: int, suspended_seconds: int = 0) -> dict[str, Any]:
        """Synchronous twin of :meth:`record_session_end` (runs in the executor).

        Phase A — fold the closed session (minus ``suspended_seconds``) into the
        aggregate in a short write UoW. Phase B — push the merged total to RomM
        outside the transaction (best-effort). Returns the same dict shape the
        frontend consumes: ``success`` plus ``duration_sec`` / ``total_seconds`` /
        ``session_count`` on the happy path, or ``success: False`` with a
        ``message`` otherwise.
        """
        try:
            with self._uow_factory() as uow:
                entry = uow.playtime.get(rom_id)
                if entry is None or not entry.last_session_start:
                    return {"success": False, "reason": "no_active_session", "message": "No active session"}
                try:
                    entry.record_session(self._clock.now().isoformat(), suspended_seconds=suspended_seconds)
                except ValueError:
                    return {
                        "success": False,
                        "reason": ErrorCode.UNKNOWN.value,
                        "message": "Failed to calculate session duration",
                    }
                uow.playtime.save(rom_id, entry)
                duration = entry.last_session_duration_sec or 0
                total_seconds = entry.total_seconds
                session_count = entry.session_count
        except sqlite3.IntegrityError as e:
            self._log_debug(f"Failed to record session end for rom {rom_id}: {e}")
            return {"success": False, "reason": "unknown_rom", "message": "Unknown ROM"}

        # Best-effort sync playtime to RomM server notes (outside the UoW). A
        # failure here is non-fatal (the local total is already persisted), but
        # log it at debug so the swallow leaves a breadcrumb (#971).
        try:
            self._sync_playtime_to_romm_io(rom_id, duration)
        except Exception as e:
            self._log_debug(f"record_session_end: playtime-to-RomM sync failed (non-fatal) for rom {rom_id}: {e}")

        return {
            "success": True,
            "duration_sec": duration,
            "total_seconds": total_seconds,
            "session_count": session_count,
        }

    def get_all_playtime(self) -> dict[str, Any]:
        """Return all local playtime entries keyed by rom_id string.

        Wire shape is the minimal pair the frontend types and reads:
        ``{playtime: {rom_id_str: {total_seconds, session_count}}}``.
        Callable-only, so its own short read UoW is safe (no in-transaction
        caller).
        """
        with self._uow_factory() as uow:
            return {
                "playtime": {
                    str(rom_id): {"total_seconds": pt.total_seconds, "session_count": pt.session_count}
                    for rom_id, pt in uow.playtime.iter_all()
                }
            }

    async def reconcile_playtime(self, rom_id: int) -> dict[str, Any]:
        """Pull the shared RomM-note total into the local row on detail-page view.

        Read-only against the server: fetches the playtime note and folds its
        total into the ``Playtime`` aggregate via ``reconcile_total`` (the clamp
        never regresses local play). Unlike the session-end path this never
        writes a note — it only catches the local row up to a server record that
        moved ahead on another device. The work runs in an executor (the SQLite
        connection has thread affinity).
        """
        return await self._loop.run_in_executor(None, self._reconcile_playtime_io, int(rom_id))

    def _reconcile_playtime_io(self, rom_id: int) -> dict[str, Any]:
        """Synchronous twin of :meth:`reconcile_playtime` (runs in the executor).

        Fetches the note outside any transaction, then folds its total into the
        aggregate in a short write UoW. Returns the partial-success shape
        ``{total_seconds, session_count, server_query_failed}``: ``total``/
        ``count`` come from the resulting (or existing) local row, and
        ``server_query_failed`` flags an unreachable server. Never raises out of
        the callable — a fetch failure or an orphan ``rom_id`` (no ``roms`` row)
        degrades to the local row's values.
        """
        try:
            note = self._retry.with_retry(self._get_playtime_note, rom_id)
        except Exception as e:
            self._log_debug(f"Failed to reconcile playtime for rom {rom_id}: {e}")
            return self._local_playtime_result(rom_id, server_query_failed=True)

        if note is None:
            # No server record — do not seed an empty row; report the local row.
            result = self._local_playtime_result(rom_id, server_query_failed=False)
            self._log_debug(
                f"Reconciled playtime for rom {rom_id}: no server note, kept local total={result['total_seconds']}s"
            )
            return result

        server_data = parse_playtime_note_content(note.get("content", ""))
        server_seconds = int(server_data.get("seconds", 0)) if server_data else 0
        note_id = note.get("id")

        try:
            with self._uow_factory() as uow:
                pt = uow.playtime.get(rom_id) or Playtime()
                pt.reconcile_total(server_seconds)
                if note_id is not None:
                    pt.link_note(note_id)
                uow.playtime.save(rom_id, pt)
                total_seconds = pt.total_seconds
                session_count = pt.session_count
        except sqlite3.IntegrityError as e:
            # Orphan FK (rom_id absent from roms): the commit rolls back, so no
            # row exists to report — a graceful 0/0 no-op.
            self._log_debug(f"Failed to reconcile playtime for rom {rom_id}: {e}")
            return _empty_reconcile_result(server_query_failed=False)

        self._log_debug(
            f"Reconciled playtime for rom {rom_id}: server={server_seconds}s "
            f"note_id={note_id} -> total={total_seconds}s"
        )
        return {
            "total_seconds": total_seconds,
            "session_count": session_count,
            "server_query_failed": False,
        }

    def _local_playtime_result(self, rom_id: int, *, server_query_failed: bool) -> dict[str, Any]:
        """Build the reconcile result from the existing local row (0/0 when absent)."""
        with self._uow_factory() as uow:
            entry = uow.playtime.get(rom_id)
        if entry is None:
            return _empty_reconcile_result(server_query_failed=server_query_failed)
        return {
            "total_seconds": entry.total_seconds,
            "session_count": entry.session_count,
            "server_query_failed": server_query_failed,
        }
