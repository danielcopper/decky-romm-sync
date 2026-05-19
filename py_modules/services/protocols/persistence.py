"""On-disk persistence Protocols for plugin state files.

Services delegate disk round-trips for plugin state, settings, save-sync
state, the metadata cache, and the firmware listing cache to these
Protocols so atomic writes, locking, and corrupt-file recovery stay in
adapters. Each Protocol carries a domain-specific method name
(``save_state`` / ``save_settings`` / ``save_metadata`` / ``save``)
rather than a generic ``__call__`` so the type checker rejects
mis-wires between the three plugin-level persisters.

Field-owning stores (``ShortcutRegistryStore``, ``MetadataCacheStore``)
live here too: they expose typed-patch mutation seams over the live
state and metadata-cache dicts. Stores mutate but do not flush — the
caller drives the matching ``*Persister.save_*`` after a batch.
"""

from __future__ import annotations

from typing import Protocol

from models.metadata_patches import MetadataStampPatch
from models.registry_patches import (
    RegistryCoverPathPatch,
    RegistryDeletePatch,
    RegistryIdsPatch,
    RegistrySgdbIdPatch,
    RegistrySyncApplyPatch,
)
from models.state import ShortcutRegistryEntry


class StatePersister(Protocol):
    """Persist the live plugin state dict (``state.json``)."""

    def save_state(self) -> None: ...


class SettingsPersister(Protocol):
    """Persist the live settings dict (``settings.json``)."""

    def save_settings(self) -> None: ...


class MetadataCachePersister(Protocol):
    """Persist the live metadata cache dict (``metadata_cache.json``)."""

    def save_metadata(self) -> None: ...


class SaveSyncStatePersister(Protocol):
    """Read/write the on-disk save-sync state file.

    Implementations are responsible for atomic writes, locking, and
    handling missing/corrupt files. They perform dumb I/O only —
    schema migrations on loaded data live in ``StateService``, so
    ``load`` returns the raw dict (or ``None`` when the file does not
    yet exist) without versioning the payload.
    """

    def save(self, data: dict) -> None: ...

    def load(self) -> dict | None: ...


class FirmwareCachePersister(Protocol):
    """Read/write the on-disk firmware list cache.

    Owns the round-trip for the cached firmware listing consumed by
    ``FirmwareService``. Path, file format, and version handling are
    adapter concerns — services see only the dict payload they
    previously wrote. ``load`` returns an empty dict (not ``None``)
    when no cached payload is available so callers can probe with
    ``"items" in data`` without a None-check.
    """

    def save(self, data: dict) -> None: ...

    def load(self) -> dict: ...


class PluginMetadataReader(Protocol):
    """Read plugin install metadata from ``package.json``.

    Owns the one-shot read of the plugin's ``package.json`` at startup
    so ``bootstrap`` does not perform raw ``open()`` calls. The plugin
    directory is supplied by the caller — implementations resolve the
    ``package.json`` path and parse the JSON payload. A missing or
    malformed file must not abort bootstrap; implementations return the
    documented fallback (``"0.0.0"`` for ``read_version``).
    """

    def read_version(self, plugin_dir: str) -> str:
        """Return the plugin's declared semantic version.

        Falls back to ``"0.0.0"`` when the file is missing, unreadable,
        malformed, or has no ``version`` field — bootstrap must not
        abort on a metadata read.
        """
        ...


class ShortcutRegistryStore(Protocol):
    """Owned mutations for ``shortcut_registry[rom_id_str]``.

    Every mutation goes through a typed patch — writers may only touch
    fields the patch type owns. The store does not persist; callers
    drive ``StatePersister.save_state()`` after a batch of writes.
    """

    def apply_sync(self, patch: RegistrySyncApplyPatch) -> None: ...

    def apply_cover_path(self, patch: RegistryCoverPathPatch) -> None: ...

    def apply_sgdb_id(self, patch: RegistrySgdbIdPatch) -> None: ...

    def apply_ids(self, patch: RegistryIdsPatch) -> None: ...

    def delete(self, patch: RegistryDeletePatch) -> ShortcutRegistryEntry | None: ...


class MetadataCacheStore(Protocol):
    """Owned mutations for ``metadata_cache[rom_id_str]``.

    Mirrors :class:`ShortcutRegistryStore`: typed patches in, in-memory
    mutations out, no flush. The caller drives the matching
    ``MetadataCachePersister.save_metadata()`` after a batch.
    """

    def apply_stamp(self, patch: MetadataStampPatch) -> None: ...
