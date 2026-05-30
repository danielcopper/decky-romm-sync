"""Slot lifecycle for save sync.

Anything that creates, lists, switches, migrates, or deletes slots
lives here, including the first-sync setup wizard. The matrix
executor and I/O orchestrators belong in SyncEngine; status
reporting in StatusService; persistence is each operation's own
narrow Unit of Work (ADR-0006).
"""

from services.saves.slots.service import SlotsService, SlotsServiceConfig

__all__ = ["SlotsService", "SlotsServiceConfig"]
