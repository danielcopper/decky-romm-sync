"""RomRemovalService — ROM file deletion and state cleanup."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.path_safety import is_safe_rom_path

if TYPE_CHECKING:
    import logging

    from services.protocols import DownloadQueueCleanup, RomsPathProvider, StatePersister


@dataclass(frozen=True)
class RomRemovalServiceConfig:
    """Frozen wiring bundle handed to ``RomRemovalService.__init__``.

    Holds the live state dicts, runtime infrastructure, persistence
    callbacks, optional roms-path provider, and the optional
    ``DownloadQueueCleanup`` eviction seam. Decomposes the ctor so a
    new dependency does not push past the S107 parameter-count limit.
    """

    state: dict
    save_sync_state: dict
    logger: logging.Logger
    loop: asyncio.AbstractEventLoop
    save_state: StatePersister
    save_save_sync_state: StatePersister
    get_roms_path: RomsPathProvider | None = None
    download_queue_cleanup: DownloadQueueCleanup | None = None


class RomRemovalService:
    """Handles physical deletion of installed ROM files and state cleanup."""

    def __init__(
        self,
        *,
        config: RomRemovalServiceConfig,
    ):
        self._state = config.state
        self._save_sync_state = config.save_sync_state
        self._logger = config.logger
        self._loop = config.loop
        self._save_state = config.save_state
        self._save_save_sync_state = config.save_save_sync_state
        self._get_roms_path = config.get_roms_path
        self._download_queue_cleanup = config.download_queue_cleanup

    def _delete_rom_files(self, installed: dict) -> None:
        """Delete ROM files for an installed entry. Handles both single-file and multi-file ROMs."""
        rom_dir = installed.get("rom_dir", "")
        file_path = installed.get("file_path", "")

        if rom_dir and os.path.isdir(rom_dir):
            if not is_safe_rom_path(rom_dir, self._get_roms_path() if self._get_roms_path else ""):
                self._logger.error(f"Refusing to delete path outside roms directory: {rom_dir}")
                return
            shutil.rmtree(rom_dir)
        elif file_path:
            if not is_safe_rom_path(file_path, self._get_roms_path() if self._get_roms_path else ""):
                self._logger.error(f"Refusing to delete path outside roms directory: {file_path}")
                return
            if os.path.isdir(file_path):
                shutil.rmtree(file_path)
            elif os.path.exists(file_path):
                os.remove(file_path)

    def _remove_rom_io(self, rom_id_str: str, installed: dict) -> None:
        """Sync helper for remove_rom — file deletion + state update in executor."""
        self._delete_rom_files(installed)

        del self._state["installed_roms"][rom_id_str]
        # Clean save sync state for removed ROM
        save_changed = False
        if self._save_sync_state.get("saves", {}).pop(rom_id_str, None) is not None:
            save_changed = True
        if self._save_sync_state.get("playtime", {}).pop(rom_id_str, None) is not None:
            save_changed = True
        if save_changed:
            self._save_save_sync_state()
        self._save_state()

    async def remove_rom(self, rom_id: int | str) -> dict:
        """Remove a single installed ROM: delete files and clean state."""
        rom_id_str = str(int(rom_id))
        installed = self._state["installed_roms"].get(rom_id_str)
        if not installed:
            return {"success": False, "message": "ROM not installed"}

        try:
            await self._loop.run_in_executor(None, self._remove_rom_io, rom_id_str, installed)
        except Exception as e:
            self._logger.error(f"Failed to delete ROM files: {e}")
            return {"success": False, "message": "Failed to delete ROM files"}

        if self._download_queue_cleanup is not None:
            self._download_queue_cleanup.evict(int(rom_id))

        return {"success": True, "message": "ROM removed"}

    def _uninstall_all_roms_io(self) -> tuple[int, list[str]]:
        """Sync helper for uninstall_all_roms — bulk file deletion + state update in executor."""
        count = 0
        errors: list[str] = []
        successfully_deleted: list[str] = []
        for rom_id_str, installed in self._state["installed_roms"].items():
            try:
                self._delete_rom_files(installed)
                count += 1
                successfully_deleted.append(rom_id_str)
            except Exception as e:
                errors.append(f"{rom_id_str}: {e}")
                self._logger.error(f"Failed to delete ROM {rom_id_str}: {e}")

        for rom_id_str in successfully_deleted:
            self._state["installed_roms"].pop(rom_id_str, None)
        # Clean save sync state for all removed ROMs
        save_changed = False
        for rom_id_str in successfully_deleted:
            if self._save_sync_state.get("saves", {}).pop(rom_id_str, None) is not None:
                save_changed = True
            if self._save_sync_state.get("playtime", {}).pop(rom_id_str, None) is not None:
                save_changed = True
        if save_changed:
            self._save_save_sync_state()
        self._save_state()
        return count, errors

    async def uninstall_all_roms(self) -> dict:
        """Remove all installed ROMs: delete files and clear state."""
        count, errors = await self._loop.run_in_executor(None, self._uninstall_all_roms_io)
        if self._download_queue_cleanup is not None:
            self._download_queue_cleanup.clear()
        msg = f"Removed {count} ROMs"
        if errors:
            msg += f" ({len(errors)} errors)"
        return {"success": True, "message": msg, "removed_count": count}
