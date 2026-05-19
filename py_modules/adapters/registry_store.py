"""Field-owning store over the live ``shortcut_registry`` dict.

Implements :class:`services.protocols.ShortcutRegistryStore`. The
adapter mutates the in-memory ``state["shortcut_registry"]`` dict that
all services share — flushing to disk stays the caller's job via the
``StatePersister`` Protocol.
"""

from __future__ import annotations

import logging
from typing import cast

from models.registry_patches import (
    RegistryCoverPathPatch,
    RegistryDeletePatch,
    RegistryIdsPatch,
    RegistrySgdbIdPatch,
    RegistrySyncApplyPatch,
)
from models.state import PluginState, ShortcutRegistryEntry


class RegistryStoreAdapter:
    """In-memory store for ``state["shortcut_registry"]`` writes.

    Parameters
    ----------
    state:
        The live ``PluginState`` dict whose ``shortcut_registry`` is
        being mutated. The same dict is shared with every service via
        the existing persister adapters.
    logger:
        A standard-library ``logging.Logger`` used to warn on stale
        patches against missing rows.
    """

    def __init__(self, state: PluginState, logger: logging.Logger) -> None:
        self._state = state
        self._logger = logger

    def apply_sync(self, patch: RegistrySyncApplyPatch) -> None:
        """Write the sync-apply row, preserving optional IDs not in *patch*.

        For each optional ID (``igdb_id``, ``sgdb_id``, ``ra_id``): a
        non-``None`` value on the patch wins; otherwise the existing
        value (if any) is preserved; otherwise the field is omitted.
        """
        registry = self._state["shortcut_registry"]
        existing = registry.get(patch.rom_id_str)

        new_entry: ShortcutRegistryEntry = {
            "app_id": patch.app_id,
            "name": patch.name,
            "fs_name": patch.fs_name,
            "platform_name": patch.platform_name,
            "platform_slug": patch.platform_slug,
            "cover_path": patch.cover_path,
        }

        self._merge_optional_id(new_entry, existing, patch.igdb_id, "igdb_id")
        self._merge_optional_id(new_entry, existing, patch.sgdb_id, "sgdb_id")
        self._merge_optional_id(new_entry, existing, patch.ra_id, "ra_id")

        registry[patch.rom_id_str] = new_entry

    def apply_cover_path(self, patch: RegistryCoverPathPatch) -> None:
        """Update ``cover_path`` on an existing row; no-op when absent."""
        entry = self._state["shortcut_registry"].get(patch.rom_id_str)
        if entry is None:
            self._logger.warning(
                "RegistryStoreAdapter.apply_cover_path: stale patch — rom_id_str=%s has no registry entry",
                patch.rom_id_str,
            )
            return
        entry["cover_path"] = patch.cover_path

    def apply_sgdb_id(self, patch: RegistrySgdbIdPatch) -> None:
        """Set ``sgdb_id`` on an existing row; no-op when absent."""
        entry = self._state["shortcut_registry"].get(patch.rom_id_str)
        if entry is None:
            self._logger.warning(
                "RegistryStoreAdapter.apply_sgdb_id: stale patch — rom_id_str=%s has no registry entry",
                patch.rom_id_str,
            )
            return
        entry["sgdb_id"] = patch.sgdb_id

    def apply_ids(self, patch: RegistryIdsPatch) -> None:
        """Set ``sgdb_id`` and/or ``igdb_id`` on an existing row.

        No-op when both ID fields on the patch are ``None`` (a failed
        upstream lookup) or when the row does not exist.
        """
        if patch.sgdb_id is None and patch.igdb_id is None:
            return
        entry = self._state["shortcut_registry"].get(patch.rom_id_str)
        if entry is None:
            self._logger.warning(
                "RegistryStoreAdapter.apply_ids: stale patch — rom_id_str=%s has no registry entry",
                patch.rom_id_str,
            )
            return
        if patch.sgdb_id is not None:
            entry["sgdb_id"] = patch.sgdb_id
        if patch.igdb_id is not None:
            entry["igdb_id"] = patch.igdb_id

    def delete(self, patch: RegistryDeletePatch) -> ShortcutRegistryEntry | None:
        """Pop and return the row, or ``None`` when nothing was stored."""
        return self._state["shortcut_registry"].pop(patch.rom_id_str, None)

    @staticmethod
    def _merge_optional_id(
        new_entry: ShortcutRegistryEntry,
        existing: ShortcutRegistryEntry | None,
        patch_value: int | None,
        key: str,
    ) -> None:
        if patch_value is not None:
            cast("dict", new_entry)[key] = patch_value
        elif existing is not None and key in existing:
            cast("dict", new_entry)[key] = cast("dict", existing)[key]
