"""ROM content the plugin finds rather than fetches.

The dialog and its exits (``service.py``) over the rename both exits share
(``renamer.py``), across the one type they speak in (``_target.py``). Consumers
import the service; the renamer is a sub-service the service owns.
"""

from services.rom_adoption.service import RomAdoptionService, RomAdoptionServiceConfig

__all__ = ["RomAdoptionService", "RomAdoptionServiceConfig"]
