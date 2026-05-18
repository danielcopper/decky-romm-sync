"""RomRemovalService — ROM file deletion and state cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.save_state import SaveSyncState
from lib.path_safety import is_safe_rom_path

if TYPE_CHECKING:
    import logging

    from services.protocols import DownloadQueueCleanup, RetroDeckPaths, RomFileStore, StatePersister


@dataclass(frozen=True)
class RomRemovalServiceConfig:
    """Frozen wiring bundle handed to ``RomRemovalService.__init__``.

    Holds the live state dicts, runtime infrastructure, persistence
    callbacks, the Protocol-typed filesystem adapter, optional
    RetroDECK paths bundle, and the optional ``DownloadQueueCleanup``
    eviction seam. Decomposes the ctor so a new dependency does not
    push past the S107 parameter-count limit.
    """

    state: dict
    save_sync_state: SaveSyncState
    logger: logging.Logger
    loop: asyncio.AbstractEventLoop
    state_persister: StatePersister
    save_sync_state_writer: StatePersister
    rom_file_store: RomFileStore
    retrodeck_paths: RetroDeckPaths | None = None
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
        self._state_persister = config.state_persister
        self._save_sync_state_writer = config.save_sync_state_writer
        self._rom_file_store = config.rom_file_store
        self._retrodeck_paths = config.retrodeck_paths
        self._download_queue_cleanup = config.download_queue_cleanup

    def _delete_rom_files(self, installed: dict) -> None:
        """Delete ROM files for an installed entry. Handles both single-file and multi-file ROMs."""
        rom_dir = installed.get("rom_dir", "")
        file_path = installed.get("file_path", "")

        roms_base = self._retrodeck_paths.roms_path() if self._retrodeck_paths else ""
        if rom_dir and self._rom_file_store.is_dir(rom_dir):
            if not is_safe_rom_path(rom_dir, roms_base):
                self._logger.error(f"Refusing to delete path outside roms directory: {rom_dir}")
                return
            self._rom_file_store.remove_tree(rom_dir)
        elif file_path:
            if not is_safe_rom_path(file_path, roms_base):
                self._logger.error(f"Refusing to delete path outside roms directory: {file_path}")
                return
            if self._rom_file_store.is_dir(file_path):
                self._rom_file_store.remove_tree(file_path)
            elif self._rom_file_store.exists(file_path):
                self._rom_file_store.remove_file(file_path)

    def _remove_rom_io(self, rom_id_str: str, installed: dict) -> None:
        """Sync helper for remove_rom — file deletion + state update in executor."""
        self._delete_rom_files(installed)

        del self._state["installed_roms"][rom_id_str]
        # Clean save sync state for removed ROM
        save_changed = False
        if self._save_sync_state.saves.pop(rom_id_str, None) is not None:
            save_changed = True
        if self._save_sync_state.playtime.pop(rom_id_str, None) is not None:
            save_changed = True
        if save_changed:
            self._save_sync_state_writer.save_state()
        self._state_persister.save_state()

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

    def _uninstall_all_roms_io(self) -> tuple[int, list[dict]]:
        """Sync helper for uninstall_all_roms — bulk file deletion + state update in executor."""
        count = 0
        errors: list[dict] = []
        successfully_deleted: list[str] = []
        for rom_id_str, installed in self._state["installed_roms"].items():
            try:
                self._delete_rom_files(installed)
                count += 1
                successfully_deleted.append(rom_id_str)
            except Exception as e:
                errors.append({"rom_id": rom_id_str, "error": str(e)})
                self._logger.error(f"Failed to delete ROM {rom_id_str}: {e}")

        for rom_id_str in successfully_deleted:
            self._state["installed_roms"].pop(rom_id_str, None)
        # Clean save sync state for all removed ROMs
        save_changed = False
        for rom_id_str in successfully_deleted:
            if self._save_sync_state.saves.pop(rom_id_str, None) is not None:
                save_changed = True
            if self._save_sync_state.playtime.pop(rom_id_str, None) is not None:
                save_changed = True
        if save_changed:
            self._save_sync_state_writer.save_state()
        self._state_persister.save_state()
        return count, errors

    async def uninstall_all_roms(self) -> dict:
        """Remove all installed ROMs: delete files and clear state.

        Returns ``success`` (True only when every per-ROM deletion
        succeeded), ``removed_count`` (number of ROMs whose files were
        deleted), and ``errors`` (one ``{"rom_id", "error"}`` entry per
        failed deletion). State for partially-failed bulk runs is left
        intact for the failing entries so the user can retry.
        """
        count, errors = await self._loop.run_in_executor(None, self._uninstall_all_roms_io)
        if self._download_queue_cleanup is not None:
            self._download_queue_cleanup.clear()
        return {
            "success": len(errors) == 0,
            "removed_count": count,
            "errors": errors,
        }
