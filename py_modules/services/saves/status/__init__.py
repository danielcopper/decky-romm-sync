"""Read-only save-status reporting.

Anything that needs to render or compute current save sync state for
one or more ROMs lives here. Mutations belong in SyncEngine; storage
in StateService.
"""

from services.saves.status.service import StatusService, StatusServiceConfig

__all__ = ["StatusService", "StatusServiceConfig"]
