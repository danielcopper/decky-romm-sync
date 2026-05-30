"""Save-sync subsystem.

The package's public API is the ``SaveService`` aggregate root — composes
the save-sync sub-services (sync_engine, status, versions, slots, rom_info)
over the SQLite ``rom_save_states`` aggregate and exposes the callable
surface consumed by the Decky entrypoints. RomM communication goes through
Protocol-typed adapters; no ``import decky`` (error helpers come from
``lib.errors``).
"""

from services.saves._config import SaveServiceConfig
from services.saves.service import SaveService

__all__ = ["SaveService", "SaveServiceConfig"]
