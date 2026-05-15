"""ShortcutRemovalService — shortcut removal and state cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        ArtworkRemover,
        EventEmitter,
        RommApiProtocol,
        StatePersister,
        SteamConfigAdapter,
    )


@dataclass(frozen=True)
class ShortcutRemovalServiceConfig:
    """Frozen wiring bundle handed to ``ShortcutRemovalService.__init__``.

    Holds the Protocol-typed adapters, the live state dict, runtime
    infrastructure, event emitter, and persistence callbacks
    ShortcutRemovalService needs at construction time.
    """

    romm_api: RommApiProtocol
    steam_config: SteamConfigAdapter
    state: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: EventEmitter
    save_state: StatePersister
    artwork_remover: ArtworkRemover


class ShortcutRemovalService:
    """Handles shortcut removal: identifies app_ids/rom_ids and cleans up state."""

    def __init__(self, *, config: ShortcutRemovalServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._steam_config = config.steam_config
        self._state = config.state
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._save_state = config.save_state
        self._artwork_remover = config.artwork_remover

    # ── Registry helpers ───────────────────────────────────────────────────

    def _find_platform_name_in_registry(self, platform_slug: str) -> str | None:
        """Look up platform name from the shortcut registry by slug."""
        for entry in self._state["shortcut_registry"].values():
            if entry.get("platform_slug") == platform_slug:
                return entry.get("platform_name")
        return None

    async def _find_platform_name_from_api(self, platform_slug: str) -> str | None:
        """Look up platform name from the RomM API by slug."""
        platforms = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        for p in platforms:
            if p.get("slug") == platform_slug:
                return p.get("name", "")
        return None

    # ── Removal queries ────────────────────────────────────────────────────

    def remove_all_shortcuts(self) -> dict:
        """Return app_ids and rom_ids for the frontend to remove via SteamClient."""
        registry = self._state.get("shortcut_registry", {})
        app_ids = [entry["app_id"] for entry in registry.values() if "app_id" in entry]
        rom_ids = list(registry.keys())
        return {"success": True, "app_ids": app_ids, "rom_ids": rom_ids}

    async def remove_platform_shortcuts(self, platform_slug: str) -> dict:
        """Return app_ids and rom_ids for a platform for the frontend to remove via SteamClient."""
        try:
            platform_name = self._find_platform_name_in_registry(platform_slug)
            if not platform_name:
                platform_name = await self._find_platform_name_from_api(platform_slug)

            if not platform_name:
                return {
                    "success": False,
                    "message": f"Platform '{platform_slug}' not found",
                    "app_ids": [],
                    "rom_ids": [],
                }

            app_ids = [
                entry["app_id"]
                for entry in self._state["shortcut_registry"].values()
                if entry.get("platform_name") == platform_name and "app_id" in entry
            ]
            rom_ids = [
                rom_id
                for rom_id, entry in self._state["shortcut_registry"].items()
                if entry.get("platform_name") == platform_name
            ]

            return {"success": True, "app_ids": app_ids, "rom_ids": rom_ids, "platform_name": platform_name}
        except Exception as e:
            self._logger.error(f"Failed to get platform shortcuts: {e}")
            return {"success": False, "message": f"Failed: {e}", "app_ids": [], "rom_ids": []}

    # ── Removal results ────────────────────────────────────────────────────

    def _report_removal_results_io(self, removed_rom_ids: list) -> None:
        """Sync helper for report_removal_results — file deletions, state save in executor."""
        # Clean up Steam Input config for removed shortcuts (always reset to default)
        removed_app_ids = [
            entry["app_id"]
            for rom_id in removed_rom_ids
            for entry in [self._state["shortcut_registry"].get(str(rom_id))]
            if entry and entry.get("app_id")
        ]
        if removed_app_ids:
            try:
                self._steam_config.set_steam_input_config(removed_app_ids, mode="default")
            except Exception as e:
                self._logger.error(f"Failed to clean up Steam Input config: {e}")

        grid = self._steam_config.grid_dir()
        for rom_id in removed_rom_ids:
            entry = self._state["shortcut_registry"].pop(str(rom_id), None)
            if entry and grid:
                self._artwork_remover.remove_artwork_files(grid, rom_id, entry)

        # Update sync_stats to reflect current registry
        registry = self._state.get("shortcut_registry", {})
        platforms = {e.get("platform_name", "") for e in registry.values()}
        self._state["sync_stats"] = {
            "platforms": len(platforms),
            "roms": len(registry),
        }
        self._save_state()

    async def report_removal_results(self, removed_rom_ids: list) -> dict:
        """Called by frontend after removing shortcuts via SteamClient."""
        await self._loop.run_in_executor(None, self._report_removal_results_io, removed_rom_ids)
        return {"success": True, "message": f"Removed {len(removed_rom_ids)} shortcuts"}
