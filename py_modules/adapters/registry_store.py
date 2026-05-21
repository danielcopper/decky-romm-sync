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

        For ``igdb_id`` and ``ra_id``: a non-``None`` value on the patch
        wins; otherwise the existing value (if any) is preserved;
        otherwise the field is omitted.

        ``sgdb_id`` carries provenance and merges differently — see
        :meth:`_merge_sync_sgdb_id`. A manually-picked id is sticky and
        survives a sync that would otherwise overwrite it.
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
        self._merge_sync_sgdb_id(new_entry, existing, patch.sgdb_id)
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
        """Set ``sgdb_id`` (and optional provenance) on an existing row.

        No-op when the row is absent. A non-``None`` ``source`` records
        how the id was chosen (``"manual"`` / ``"romm"`` / ``"igdb"``);
        ``None`` leaves any existing provenance untouched.
        """
        entry = self._state["shortcut_registry"].get(patch.rom_id_str)
        if entry is None:
            self._logger.warning(
                "RegistryStoreAdapter.apply_sgdb_id: stale patch — rom_id_str=%s has no registry entry",
                patch.rom_id_str,
            )
            return
        entry["sgdb_id"] = patch.sgdb_id
        if patch.source is not None:
            entry["sgdb_id_source"] = patch.source

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

    @staticmethod
    def _merge_sync_sgdb_id(
        new_entry: ShortcutRegistryEntry,
        existing: ShortcutRegistryEntry | None,
        patch_value: int | None,
    ) -> None:
        """Merge ``sgdb_id`` for a sync write, honouring manual stickiness.

        A manually-picked id (``existing["sgdb_id_source"] == "manual"``)
        is authoritative: it survives the sync untouched, carrying its
        provenance forward, regardless of what the sync patch proposes.

        Otherwise the sync patch value wins when present and is tagged
        with source ``"romm"`` (sync ids come from RomM's authoritative
        ``sgdb_id`` field). When the patch carries no id, the existing id
        and its provenance (if any) are preserved.
        """
        existing_id = existing.get("sgdb_id") if existing else None
        existing_source = existing.get("sgdb_id_source") if existing else None

        if existing_id is not None and existing_source == "manual":
            new_entry["sgdb_id"] = existing_id
            new_entry["sgdb_id_source"] = "manual"
            return

        if patch_value is not None:
            new_entry["sgdb_id"] = patch_value
            new_entry["sgdb_id_source"] = "romm"
        elif existing_id is not None:
            new_entry["sgdb_id"] = existing_id
            if existing_source is not None:
                new_entry["sgdb_id_source"] = existing_source
