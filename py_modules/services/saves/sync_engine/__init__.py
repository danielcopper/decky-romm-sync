"""Newest-wins matrix executor and the I/O orchestrators that perform
sync transfers.

Anything that decides "which side wins for this file" or actually
moves bytes between local saves_dir and the RomM server lives here.
Read-only matrix consumption (status reporting) belongs in
StatusService; persistence belongs in StateService.
"""

from services.saves.sync_engine.engine import MatrixOutcome, SyncEngine, SyncEngineConfig

__all__ = ["MatrixOutcome", "SyncEngine", "SyncEngineConfig"]
