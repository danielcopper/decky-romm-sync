"""PlaytimeService — playtime tracking via RomM Notes API.

All RomM communication goes through ``RommPlaytimeApi``.
No ``import decky``.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.save_state import PlaytimeEntry, SaveSyncState
from lib.iso_time import parse_iso
from services.protocols import Clock, DebugLogger, RetryStrategy, RommPlaytimeApi, StatePersister

if TYPE_CHECKING:
    import asyncio
    import logging


@dataclass(frozen=True)
class PlaytimeServiceConfig:
    """Frozen wiring bundle handed to ``PlaytimeService.__init__``.

    Holds the Protocol-typed RomM adapter and retry strategy, the typed
    save-sync aggregate, runtime infrastructure, clock/debug-logger
    seams, and the persistence callback PlaytimeService needs at
    construction time.
    """

    romm_api: RommPlaytimeApi
    retry: RetryStrategy
    save_sync_state: SaveSyncState
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    state_persister: StatePersister
    log_debug: DebugLogger


class PlaytimeService:
    """Playtime tracking: record sessions and sync to RomM notes."""

    PLAYTIME_NOTE_TITLE = "romm-sync:playtime"

    def __init__(self, *, config: PlaytimeServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._retry = config.retry
        self._save_sync_state = config.save_sync_state
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._state_persister = config.state_persister
        self._log_debug = config.log_debug

    # ------------------------------------------------------------------
    # Playtime Notes API Helpers
    # ------------------------------------------------------------------

    def _get_playtime_note(self, rom_id: int) -> dict | None:
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

    def _create_playtime_note(self, rom_id: int, playtime_data: dict) -> dict:
        """Create a new playtime note for a ROM."""
        result = self._romm_api.create_note(
            rom_id,
            {
                "title": self.PLAYTIME_NOTE_TITLE,
                "content": json.dumps(playtime_data),
                "is_public": False,
            },
        )
        # Store note_id in state for future updates
        if isinstance(result, dict) and result.get("id"):
            rom_id_str = str(int(rom_id))
            entry = self._save_sync_state.playtime.get(rom_id_str)
            if entry is not None:
                entry.note_id = result["id"]
                self._state_persister.save_state()
        return result

    def _update_playtime_note(self, rom_id: int, note_id: int, playtime_data: dict) -> dict:
        """Update an existing playtime note."""
        return self._romm_api.update_note(
            rom_id,
            note_id,
            {"content": json.dumps(playtime_data)},
        )

    @staticmethod
    def _parse_playtime_note_content(content: str) -> dict | None:
        """Parse JSON content from a playtime note. Returns dict or None."""
        if not content:
            return None
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        return None

    def _sync_playtime_to_romm(self, rom_id: int, session_duration_sec: int) -> None:
        """Push playtime to RomM via the Notes API after a session.

        Fetches the server note, adds the session delta to the server total,
        and creates/updates the note. Best-effort — errors are logged.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)
        entry = self._save_sync_state.playtime.get(rom_id_str)
        if not entry:
            return

        local_total = entry.total_seconds
        device_name = self._save_sync_state.device_name or ""

        try:
            note = self._retry.with_retry(self._get_playtime_note, rom_id)
            server_seconds = 0
            note_id = None

            if note:
                note_id = note.get("id")
                server_data = self._parse_playtime_note_content(note.get("content", ""))
                if server_data:
                    server_seconds = int(server_data.get("seconds", 0))

            # Merge: server baseline + this session, or local total, whichever is higher
            new_total = max(local_total, server_seconds + session_duration_sec)

            playtime_data = {
                "seconds": new_total,
                "updated": self._clock.now().isoformat(),
                "device": device_name,
            }

            if note_id:
                self._retry.with_retry(self._update_playtime_note, rom_id, note_id, playtime_data)
            else:
                self._retry.with_retry(self._create_playtime_note, rom_id, playtime_data)

            # Sync local state to the merged total
            entry.total_seconds = new_total
            self._state_persister.save_state()

        except Exception as e:
            self._log_debug(f"Failed to sync playtime to RomM for rom {rom_id}: {e}")

    # ------------------------------------------------------------------
    # Public async methods
    # ------------------------------------------------------------------

    def record_session_start(self, rom_id: int) -> dict:
        """Record the start of a play session for playtime tracking."""
        rom_id_str = str(int(rom_id))
        playtime = self._save_sync_state.playtime
        entry = playtime.setdefault(rom_id_str, PlaytimeEntry())
        entry.last_session_start = self._clock.now().isoformat()
        self._state_persister.save_state()
        return {"success": True}

    async def record_session_end(self, rom_id: int) -> dict:
        """Record end of play session, accumulate playtime delta.

        Only handles playtime — save sync is handled separately.
        """
        rom_id_str = str(int(rom_id))
        entry = self._save_sync_state.playtime.get(rom_id_str)

        if not entry or not entry.last_session_start:
            return {"success": False, "message": "No active session"}

        try:
            start = parse_iso(entry.last_session_start)
            if start is None:
                return {"success": False, "message": "Failed to calculate session duration"}
            now = self._clock.now()
            duration = (now - start).total_seconds()

            # Sanity check: clamp to 0-24h
            duration = max(0, min(duration, 86400))

            entry.total_seconds += int(duration)
            entry.session_count += 1
            entry.last_session_duration_sec = int(duration)
            entry.last_session_start = None

            self._state_persister.save_state()

            # Best-effort sync playtime to RomM server notes
            with contextlib.suppress(Exception):
                await self._loop.run_in_executor(None, self._sync_playtime_to_romm, int(rom_id), int(duration))

            return {
                "success": True,
                "duration_sec": int(duration),
                "total_seconds": entry.total_seconds,
                "session_count": entry.session_count,
            }
        except (ValueError, TypeError):
            return {"success": False, "message": "Failed to calculate session duration"}

    async def get_server_playtime(self, rom_id: int) -> dict:
        """Read playtime from RomM server notes for a ROM."""
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        local_entry = self._save_sync_state.playtime.get(rom_id_str)
        local_seconds = local_entry.total_seconds if local_entry else 0

        server_seconds = 0
        try:
            note = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(self._get_playtime_note, rom_id),
            )
            if note:
                server_data = self._parse_playtime_note_content(note.get("content", ""))
                if server_data:
                    server_seconds = int(server_data.get("seconds", 0))
        except Exception as e:
            self._log_debug(f"Failed to read server playtime for rom {rom_id}: {e}")

        return {
            "rom_id": rom_id,
            "local_seconds": local_seconds,
            "server_seconds": server_seconds,
            "total_seconds": max(local_seconds, server_seconds),
            "session_count": local_entry.session_count if local_entry else 0,
        }

    def get_all_playtime(self) -> dict:
        """Return all local playtime entries keyed by rom_id string."""
        return {"playtime": {rid: pe.to_dict() for rid, pe in self._save_sync_state.playtime.items()}}
