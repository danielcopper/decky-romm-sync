"""Field-owning store over the live ``metadata_cache`` dict.

Implements :class:`services.protocols.MetadataCacheStore`. Mirrors
:class:`adapters.registry_store.RegistryStoreAdapter`: in-memory
mutation only — flushing to disk stays the caller's job via the
``MetadataCachePersister`` Protocol.
"""

from __future__ import annotations

from models.metadata_patches import MetadataStampPatch
from models.state import MetadataCache


class MetadataCacheStoreAdapter:
    """In-memory store for ``metadata_cache[rom_id_str]`` writes.

    Parameters
    ----------
    metadata_cache:
        The live ``MetadataCache`` dict shared with every service via
        the existing persister adapter.
    """

    def __init__(self, metadata_cache: MetadataCache) -> None:
        self._metadata_cache = metadata_cache

    def apply_stamp(self, patch: MetadataStampPatch) -> None:
        """Replace ``metadata_cache[rom_id_str]`` with ``patch.entry``."""
        self._metadata_cache[patch.rom_id_str] = patch.entry
