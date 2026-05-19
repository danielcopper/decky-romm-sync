"""ArtworkService — cover art download, staging, and cleanup."""

from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.registry_patches import RegistryCoverPathPatch
from models.state import PluginState, ShortcutRegistryEntry

from domain.artwork_paths import final_filename, staging_filename
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from services.protocols import (
        CoverArtFileStore,
        PendingSyncReader,
        RommRomReader,
        ShortcutRegistryStore,
        StatePersister,
        SteamConfigStore,
    )


@dataclass(frozen=True)
class ArtworkServiceConfig:
    """Frozen wiring bundle handed to ``ArtworkService.__init__``.

    Holds the Protocol-typed adapters, the live state dict, runtime
    infrastructure, and the read seam ArtworkService uses to consult
    the in-flight sync's pending cover paths.
    """

    romm_api: RommRomReader
    steam_config: SteamConfigStore
    cover_art_file_store: CoverArtFileStore
    state: PluginState
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    get_pending_sync: PendingSyncReader
    registry_store: ShortcutRegistryStore
    state_persister: StatePersister


class ArtworkService:
    """Manages artwork downloading, staging, finalisation, and cleanup."""

    def __init__(self, *, config: ArtworkServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._steam_config = config.steam_config
        self._cover_art_file_store = config.cover_art_file_store
        self._state = config.state
        self._loop = config.loop
        self._logger = config.logger
        self._get_pending_sync = config.get_pending_sync
        self._registry_store = config.registry_store
        self._state_persister = config.state_persister

    # ── Existing cover path check ──────────────────────────────────────────

    def existing_cover_path(self, rom_id: int, grid: str) -> str | None:
        """Return an existing cover path for *rom_id*, or ``None`` if a download is needed."""
        staging = os.path.join(grid, staging_filename(rom_id))

        # If already synced and final artwork exists, reuse it
        reg = self._state["shortcut_registry"].get(str(rom_id))
        if reg and reg.get("app_id"):
            final = os.path.join(grid, final_filename(reg["app_id"]))
            if self._cover_art_file_store.exists(final):
                return final

        # If staging file already exists (e.g. retry), reuse it
        if self._cover_art_file_store.exists(staging):
            return staging

        return None

    # ── Artwork download ───────────────────────────────────────────────────

    async def download_artwork(
        self,
        all_roms: list[dict],
        emit_progress: Callable,
        is_cancelling: Callable,
        progress_step: int = 4,
        progress_total_steps: int = 6,
    ) -> dict[int, str]:
        """Download cover artwork to staging filenames (romm_{rom_id}_cover.png).

        Decouples download from the final Steam app_id, which isn't known until
        after AddShortcut. finalize_cover_path() renames to {app_id}p.png.
        Returns dict of rom_id -> local cover path.
        """
        cover_paths: dict[int, str] = {}
        grid = self._steam_config.grid_dir()
        if not grid:
            self._logger.warning("Cannot find grid directory, skipping artwork")
            return cover_paths

        total = len(all_roms)

        for i, rom in enumerate(all_roms):
            if is_cancelling():
                return cover_paths

            await emit_progress(
                "applying",
                current=i + 1,
                total=total,
                message=f"Downloading artwork {i + 1}/{total}",
                step=progress_step,
                total_steps=progress_total_steps,
            )

            cover_url = rom.get("path_cover_large") or rom.get("path_cover_small")
            if not cover_url:
                continue

            rom_id = rom["id"]
            existing = self.existing_cover_path(rom_id, grid)
            if existing:
                cover_paths[rom_id] = existing
                continue

            staging = os.path.join(grid, staging_filename(rom_id))
            try:
                await self._loop.run_in_executor(None, self._romm_api.download_cover, cover_url, staging)
                cover_paths[rom_id] = staging
            except Exception as e:
                self._logger.warning(f"Failed to download artwork for {rom['name']}: {e}")

        return cover_paths

    # ── Artwork finalisation ───────────────────────────────────────────────

    def finalize_cover_path(self, grid: str | None, cover_path: str, app_id: int, rom_id_str: str) -> str:
        """Rename staged artwork to final Steam app_id filename, return final path."""
        if not grid or not cover_path:
            return cover_path
        final_path = os.path.join(grid, final_filename(app_id))
        if cover_path != final_path and self._cover_art_file_store.exists(cover_path):
            try:
                self._cover_art_file_store.rename(cover_path, final_path)
                return final_path
            except OSError as e:
                self._logger.warning(f"Failed to rename artwork for rom {rom_id_str}: {e}")
        elif self._cover_art_file_store.exists(final_path):
            return final_path
        return cover_path

    # ── Artwork removal ────────────────────────────────────────────────────

    def remove_artwork_files(self, grid: str, rom_id: str | int, entry: ShortcutRegistryEntry) -> None:
        """Remove all artwork files for a registry entry."""
        removed = False
        # Try cover_path first (stores the final renamed path)
        cover_path = entry.get("cover_path", "")
        if cover_path and self._cover_art_file_store.exists(cover_path):
            self._cover_art_file_store.remove_file(cover_path)
            removed = True
        # Try {app_id}p.png (the standard Steam grid filename)
        if not removed and entry.get("app_id"):
            app_path = os.path.join(grid, final_filename(entry["app_id"]))
            if self._cover_art_file_store.exists(app_path):
                self._cover_art_file_store.remove_file(app_path)
                removed = True
        # Fallback: legacy artwork_id format
        if not removed:
            artwork_id = entry.get("artwork_id")
            if artwork_id:
                art_path = os.path.join(grid, final_filename(artwork_id))
                if self._cover_art_file_store.exists(art_path):
                    self._cover_art_file_store.remove_file(art_path)
        # Clean up any leftover staging file
        staging = os.path.join(grid, staging_filename(rom_id))
        if self._cover_art_file_store.exists(staging):
            self._cover_art_file_store.remove_file(staging)

    # ── Artwork base64 query ───────────────────────────────────────────────

    async def get_artwork_base64(self, rom_id: int) -> dict:
        """Return base64-encoded cover artwork for a single ROM."""
        grid = self._steam_config.grid_dir()
        if not grid:
            return {"base64": None}

        # Check pending sync data first (staging path)
        pending_sync = self._get_pending_sync()
        pending = pending_sync.get(rom_id, {})
        cover_path = pending.get("cover_path", "")

        # Fall back to registry
        if not cover_path:
            reg = self._state["shortcut_registry"].get(str(rom_id), {})
            cover_path = reg.get("cover_path", "")

        # Try staging filename as last resort
        if not cover_path:
            staging = os.path.join(grid, staging_filename(rom_id))
            if self._cover_art_file_store.exists(staging):
                cover_path = staging

        # The canonical {app_id}p.png may be on disk even when the registry
        # row has no cover_path — recover it via the registry's app_id.
        if not cover_path:
            fallback = self.existing_cover_path(rom_id, grid)
            if fallback:
                cover_path = fallback

        if cover_path and self._cover_art_file_store.exists(cover_path):
            try:
                data = await self._loop.run_in_executor(None, self._cover_art_file_store.read_bytes, cover_path)
                return {"base64": base64.b64encode(data).decode("ascii")}
            except Exception as e:
                self._logger.warning(f"Failed to read artwork for rom {rom_id}: {e}")

        return {"base64": None}

    # ── Cover refresh (single-ROM repair) ──────────────────────────────────

    async def refresh_cover(self, rom_id: int) -> dict:
        """Re-download a ROM's RomM cover and update its registry row.

        Looks up the ROM's current registry entry to get its ``app_id``,
        fetches the fresh cover URL from RomM, downloads to staging,
        renames to ``{app_id}p.png``, and applies a
        :class:`RegistryCoverPathPatch` so the registry's ``cover_path``
        catches up. Returns the canonical ``{success, reason, message}``
        failure shape on every failure branch — see
        ``lib/list_result.py``.
        """
        reg = self._state["shortcut_registry"].get(str(rom_id))
        if not reg or not reg.get("app_id"):
            return {
                "success": False,
                "reason": "not_synced",
                "message": "ROM is not synced to Steam",
            }
        app_id = reg["app_id"]

        grid = self._steam_config.grid_dir()
        if not grid:
            return {
                "success": False,
                "reason": "no_grid_dir",
                "message": "Steam grid directory not found",
            }

        try:
            rom = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
        except Exception as e:
            self._logger.warning(f"refresh_cover: failed to fetch rom {rom_id}: {e}")
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Could not fetch ROM from server",
            }
        if not rom:
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Could not fetch ROM from server",
            }

        cover_url = rom.get("path_cover_large") or rom.get("path_cover_small")
        if not cover_url:
            return {
                "success": False,
                "reason": "no_cover",
                "message": "ROM has no cover artwork",
            }

        staging = os.path.join(grid, staging_filename(rom_id))
        try:
            await self._loop.run_in_executor(None, self._romm_api.download_cover, cover_url, staging)
        except Exception as e:
            self._logger.warning(f"refresh_cover: failed to download cover for rom {rom_id}: {e}")
            return {
                "success": False,
                "reason": "download_failed",
                "message": str(e),
            }

        final = self.finalize_cover_path(grid, staging, app_id, str(rom_id))

        self._registry_store.apply_cover_path(
            RegistryCoverPathPatch(rom_id_str=str(rom_id), cover_path=final),
        )
        await self._loop.run_in_executor(None, self._state_persister.save_state)

        return {
            "success": True,
            "message": "Cover refreshed",
            "cover_path": final,
        }

    # ── Staging file housekeeping ──────────────────────────────────────────

    def is_staging_file_orphaned(self, grid: str, registry: dict, rom_id: str) -> bool:
        """Check if a staging artwork file is orphaned (not in registry or has final artwork)."""
        if rom_id not in registry:
            return True
        app_id = registry[rom_id].get("app_id")
        if app_id:
            final = os.path.join(grid, final_filename(app_id))
            return self._cover_art_file_store.exists(final)
        return False

    def prune_orphaned_staging_artwork(self) -> None:
        """Remove orphaned romm_{rom_id}_cover.png staging files from Steam grid dir."""
        grid = self._steam_config.grid_dir()
        if not grid or not self._cover_art_file_store.is_dir(grid):
            return
        registry = self._state.get("shortcut_registry", {})
        pruned = []
        for filename in self._cover_art_file_store.listdir(grid):
            if not filename.startswith("romm_") or not filename.endswith("_cover.png"):
                continue
            try:
                rom_id = filename[len("romm_") : -len("_cover.png")]
                int(rom_id)  # validate it's numeric
            except (ValueError, IndexError):
                continue
            if not self.is_staging_file_orphaned(grid, registry, rom_id):
                continue
            try:
                self._cover_art_file_store.remove_file(os.path.join(grid, filename))
                pruned.append(filename)
            except OSError as e:
                self._logger.warning(f"Failed to remove orphaned staging artwork {filename}: {e}")
        if pruned:
            self._logger.info(f"Pruned {len(pruned)} orphaned staging artwork file(s)")
