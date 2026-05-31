"""ShortcutRemovalService — Steam-shortcut removal and ROM unbinding.

Resolves the Steam ``app_id``/``rom_id`` sets the frontend removes via the
SteamClient API, then reconciles persistence: removing a shortcut **unbinds**
the ROM (clears ``shortcut_app_id``, keeps the row and its per-ROM children per
ADR-0007), never deletes it. Reads the synced-shortcut binding from ``uow.roms``;
the offline ``platform_slug → display_name`` label comes from the ``kv_config``
cache the library sync refreshes each run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.platform_names import decode_platform_names

if TYPE_CHECKING:
    import asyncio
    import logging

    from models.state import ShortcutRegistryEntry

    from services.protocols import (
        ArtworkRemover,
        SteamConfigStore,
        UnitOfWorkFactory,
    )

# kv_config key for the offline ``platform_slug → display_name`` cache the
# library sync refreshes every run. Read here so the DangerZone "clear
# platform" response shows "Nintendo 64" rather than the bare "n64" slug when
# RomM is unreachable. Mirrors ``library.reporter._PLATFORM_NAMES_KEY``.
_PLATFORM_NAMES_KEY = "platform_names"


@dataclass(frozen=True)
class ShortcutRemovalServiceConfig:
    """Frozen wiring bundle handed to ``ShortcutRemovalService.__init__``.

    Holds the Protocol-typed Steam-config adapter, runtime infrastructure, the
    artwork remover peer, and the SQLite Unit-of-Work factory (the transactional
    seam over the ``roms`` / ``kv_config`` repositories ShortcutRemovalService
    reads and unbinds).
    """

    steam_config: SteamConfigStore
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    artwork_remover: ArtworkRemover
    uow_factory: UnitOfWorkFactory


class ShortcutRemovalService:
    """Resolves shortcut removal sets and unbinds the affected ROMs in SQLite."""

    def __init__(self, *, config: ShortcutRemovalServiceConfig) -> None:
        self._steam_config = config.steam_config
        self._loop = config.loop
        self._logger = config.logger
        self._artwork_remover = config.artwork_remover
        self._uow_factory = config.uow_factory

    # ── Removal queries ────────────────────────────────────────────────────

    def remove_all_shortcuts(self) -> dict:
        """Return app_ids and rom_ids for the frontend to remove via SteamClient.

        Bound ROMs contribute their ``shortcut_app_id``; every ROM contributes
        its ``rom_id`` (the frontend reports back the full removed set). Unbound
        rows (NULL ``shortcut_app_id``) have no Steam shortcut, so they carry no
        ``app_id``.
        """
        with self._uow_factory() as uow:
            roms = list(uow.roms.iter_all())
        app_ids = [rom.shortcut_app_id for rom in roms if rom.shortcut_app_id is not None]
        rom_ids = [str(rom.rom_id) for rom in roms]
        return {"success": True, "app_ids": app_ids, "rom_ids": rom_ids}

    async def remove_platform_shortcuts(self, platform_slug: str) -> dict:
        """Return app_ids and rom_ids for a platform for the frontend to remove via SteamClient.

        Filters ``uow.roms`` by ``platform_slug`` directly; the display name in
        the response is resolved from the offline ``kv_config`` cache, falling
        back to the slug when RomM has never been seen for it.
        """
        try:
            return await self._loop.run_in_executor(None, self._remove_platform_shortcuts_io, platform_slug)
        except Exception as e:
            self._logger.error(f"Failed to get platform shortcuts: {e}")
            return {"success": False, "message": f"Failed: {e}", "app_ids": [], "rom_ids": []}

    def _remove_platform_shortcuts_io(self, platform_slug: str) -> dict:
        with self._uow_factory() as uow:
            roms = list(uow.roms.iter_by_platform(platform_slug))
            platform_name = self._read_platform_name_cache(uow).get(platform_slug, platform_slug)
        app_ids = [rom.shortcut_app_id for rom in roms if rom.shortcut_app_id is not None]
        rom_ids = [str(rom.rom_id) for rom in roms]
        return {"success": True, "app_ids": app_ids, "rom_ids": rom_ids, "platform_name": platform_name}

    def _read_platform_name_cache(self, uow) -> dict[str, str]:
        """Decode the ``platform_slug → display_name`` cache, ``{}`` when absent/corrupt."""
        return decode_platform_names(uow.kv_config.get(_PLATFORM_NAMES_KEY))

    # ── Removal results ────────────────────────────────────────────────────

    def _report_removal_results_io(self, removed_rom_ids: list) -> None:
        """Sync helper for report_removal_results — Steam-Input reset, artwork deletion, unbind."""
        with self._uow_factory() as uow:
            roms = {rom_id: uow.roms.get(int(rom_id)) for rom_id in removed_rom_ids}

        # Clean up Steam Input config for removed shortcuts (always reset to default).
        removed_app_ids = [
            rom.shortcut_app_id for rom in roms.values() if rom is not None and rom.shortcut_app_id is not None
        ]
        if removed_app_ids:
            try:
                self._steam_config.set_steam_input_config(removed_app_ids, mode="default")
            except Exception as e:
                self._logger.error(f"Failed to clean up Steam Input config: {e}")

        grid = self._steam_config.grid_dir()
        for rom_id in removed_rom_ids:
            rom = roms.get(rom_id)
            if rom is not None and grid:
                self._artwork_remover.remove_artwork_files(grid, rom_id, self._artwork_entry(rom))

        # Unbind the removed ROMs — clear the Steam link, keep the row (ADR-0007).
        with self._uow_factory() as uow:
            for rom_id in removed_rom_ids:
                rom = uow.roms.get(int(rom_id))
                if rom is None or rom.shortcut_app_id is None:
                    continue
                rom.unbind_shortcut()
                uow.roms.save(rom)

    @staticmethod
    def _artwork_entry(rom) -> ShortcutRegistryEntry:
        """Project the ROM's artwork-relevant fields into the entry shape the remover reads."""
        entry: dict[str, object] = {"cover_path": rom.cover_path or ""}
        if rom.shortcut_app_id is not None:
            entry["app_id"] = rom.shortcut_app_id
        return entry  # type: ignore[return-value]

    async def report_removal_results(self, removed_rom_ids: list) -> dict:
        """Called by frontend after removing shortcuts via SteamClient."""
        await self._loop.run_in_executor(None, self._report_removal_results_io, removed_rom_ids)
        return {"success": True, "message": f"Removed {len(removed_rom_ids)} shortcuts"}
