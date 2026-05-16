"""Save-sync subsystem.

The package's public API is the ``SaveService`` aggregate root — composes
the save-sync sub-services (state, sync_engine, status, versions, slots)
and exposes the callable surface consumed by the Decky entrypoints.
RomM communication goes through Protocol-typed adapters; no ``import decky``
(error helpers come from ``lib.errors``).
"""

from services.saves._config import SaveServiceConfig
from services.saves.service import SaveService
from services.saves.slots.service import _NO_MIGRATION

__all__ = ["_NO_MIGRATION", "SaveService", "SaveServiceConfig"]
