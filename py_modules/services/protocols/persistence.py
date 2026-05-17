"""On-disk persistence Protocols for plugin state files.

Services delegate disk round-trips for plugin state, settings, save-sync
state, the metadata cache, and the firmware listing cache to these
Protocols so atomic writes, locking, and corrupt-file recovery stay in
adapters. Each Protocol carries a domain-specific method name
(``save_state`` / ``save_settings`` / ``save_metadata`` / ``save``)
rather than a generic ``__call__`` so the type checker rejects
mis-wires between the three plugin-level persisters.
"""

from __future__ import annotations

from typing import Protocol


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
