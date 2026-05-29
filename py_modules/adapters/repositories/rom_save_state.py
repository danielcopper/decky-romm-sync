"""SQLite adapter for the ``RomSaveState`` aggregate.

Spans two tables: the per-ROM scalars live in ``rom_save_states`` and the
per-file ``FileSyncState`` baselines in ``rom_save_files``. ``get`` rebuilds the
aggregate from both; ``save`` writes the scalar row and replaces the child file
rows inside the unit-of-work's open transaction.

Two schema subtleties are preserved exactly:

- ``own_upload_ids``: SQL NULL (Python ``None``) and ``'[]'`` (Python ``[]``)
  are DISTINCT. ``None`` → NULL, ``[]`` → ``json.dumps([])``; neither is coerced
  to the other.
- ``rom_save_files.last_sync_at`` / ``last_sync_server_updated_at`` are NOT NULL
  DEFAULT '' — the never-synced sentinel is the empty string, written as-is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from adapters.repositories._base import BaseRepository
from domain.rom_save_state import FileSyncState, RomSaveState

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

_STATE_COLUMNS = (
    "rom_id, active_slot, slot_confirmed, emulator, system, last_synced_core, own_upload_ids, slots, last_sync_check_at"
)
_FILE_COLUMNS = (
    "rom_id, filename, tracked_save_id, last_sync_hash, last_sync_at, "
    "last_sync_server_updated_at, last_sync_server_save_id, last_sync_server_size, "
    "last_sync_local_mtime, last_sync_local_size"
)


def _row_to_file(row: sqlite3.Row) -> FileSyncState:
    return FileSyncState(
        tracked_save_id=row["tracked_save_id"],
        last_sync_hash=row["last_sync_hash"],
        last_sync_at=row["last_sync_at"],
        last_sync_server_updated_at=row["last_sync_server_updated_at"],
        last_sync_server_save_id=row["last_sync_server_save_id"],
        last_sync_server_size=row["last_sync_server_size"],
        last_sync_local_mtime=row["last_sync_local_mtime"],
        last_sync_local_size=row["last_sync_local_size"],
    )


class SqliteRomSaveStateRepository(BaseRepository):
    """Per-ROM save-sync state spanning rom_save_states + rom_save_files."""

    def _row_to_state(self, row: sqlite3.Row, files: dict[str, FileSyncState]) -> RomSaveState:
        return RomSaveState(
            active_slot=row["active_slot"],
            slot_confirmed=self._to_bool(row["slot_confirmed"]),
            emulator=row["emulator"],
            system=row["system"],
            last_synced_core=row["last_synced_core"],
            own_upload_ids=self._json_or_none(row["own_upload_ids"]),
            slots=self._json_or_none(row["slots"]) or {},
            files=files,
            last_sync_check_at=row["last_sync_check_at"],
        )

    def get(self, rom_id: int) -> RomSaveState | None:
        row = self._conn.execute(
            f"SELECT {_STATE_COLUMNS} FROM rom_save_states WHERE rom_id = ?",
            (rom_id,),
        ).fetchone()
        if row is None:
            return None
        files = {
            file_row["filename"]: _row_to_file(file_row)
            for file_row in self._conn.execute(
                f"SELECT {_FILE_COLUMNS} FROM rom_save_files WHERE rom_id = ?",
                (rom_id,),
            )
        }
        return self._row_to_state(row, files)

    def save(self, rom_id: int, state: RomSaveState) -> None:
        self._conn.execute(
            f"INSERT OR REPLACE INTO rom_save_states ({_STATE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rom_id,
                state.active_slot,
                int(state.slot_confirmed),
                state.emulator,
                state.system,
                state.last_synced_core,
                None if state.own_upload_ids is None else self._json(state.own_upload_ids),
                self._json(state.slots),
                state.last_sync_check_at,
            ),
        )
        self._conn.execute("DELETE FROM rom_save_files WHERE rom_id = ?", (rom_id,))
        self._conn.executemany(
            f"INSERT INTO rom_save_files ({_FILE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    rom_id,
                    filename,
                    file.tracked_save_id,
                    file.last_sync_hash,
                    file.last_sync_at,
                    file.last_sync_server_updated_at,
                    file.last_sync_server_save_id,
                    file.last_sync_server_size,
                    file.last_sync_local_mtime,
                    file.last_sync_local_size,
                )
                for filename, file in state.files.items()
            ],
        )

    def delete(self, rom_id: int) -> None:
        self._conn.execute("DELETE FROM rom_save_files WHERE rom_id = ?", (rom_id,))
        self._conn.execute("DELETE FROM rom_save_states WHERE rom_id = ?", (rom_id,))

    def iter_all(self) -> Iterator[tuple[int, RomSaveState]]:
        rom_ids = [row["rom_id"] for row in self._conn.execute("SELECT rom_id FROM rom_save_states")]
        for rom_id in rom_ids:
            state = self.get(rom_id)
            if state is not None:
                yield (rom_id, state)
