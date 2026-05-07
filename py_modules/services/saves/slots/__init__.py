"""Slot lifecycle for save sync.

Anything that creates, lists, switches, migrates, or deletes slots
lives here, including the first-sync setup wizard. The matrix
executor and I/O orchestrators belong in SyncEngine; status
reporting in StatusService; persistence in StateService.
"""

from services.saves.slots.service import SlotsService

__all__ = ["SlotsService"]
