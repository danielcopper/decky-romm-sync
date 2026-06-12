"""RomRemovalService — installed-ROM file deletion and ``rom_installs`` cleanup.

Physically deletes a ROM's files from disk and drops its ``rom_installs``
record. Per [ADR-0007](docs/adr/0007-rom-retention-identity-anchor.md) an
uninstall is *not* a purge: the ``roms`` identity row, playtime, saves, and
metadata all survive — only the on-disk files and the install record go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lib.list_result import ErrorCode
from lib.path_safety import is_safe_rom_path

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.rom_install import RomInstall
    from services.protocols import (
        DownloadQueueCleanup,
        RetroDeckPaths,
        RomFileStore,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class RomRemovalServiceConfig:
    """Frozen wiring bundle handed to ``RomRemovalService.__init__``.

    Holds the runtime infrastructure, the Protocol-typed filesystem
    adapter, the RetroDECK paths bundle, the ``DownloadQueueCleanup``
    eviction seam (``None`` when no download cleanup is wired), and the
    SQLite Unit-of-Work factory (the transactional seam over the
    ``rom_installs`` repository). Decomposes the ctor so a new dependency
    does not push past the S107 parameter-count limit.
    """

    logger: logging.Logger
    loop: asyncio.AbstractEventLoop
    rom_file_store: RomFileStore
    retrodeck_paths: RetroDeckPaths
    download_queue_cleanup: DownloadQueueCleanup | None
    uow_factory: UnitOfWorkFactory


class RomRemovalService:
    """Handles physical deletion of installed ROM files and ``rom_installs`` cleanup."""

    def __init__(
        self,
        *,
        config: RomRemovalServiceConfig,
    ):
        self._logger = config.logger
        self._loop = config.loop
        self._rom_file_store = config.rom_file_store
        self._retrodeck_paths = config.retrodeck_paths
        self._download_queue_cleanup = config.download_queue_cleanup
        self._uow_factory = config.uow_factory

    def _delete_rom_files(self, install: RomInstall) -> None:
        """Delete ROM files for an install record. Handles both single-file and multi-file ROMs.

        A multi-file ROM owns a dedicated per-ROM directory (``rom_dir`` is set)
        and is removed whole. A single-file ROM has no ``rom_dir`` (``None``) —
        it lives as a bare file in the shared ``<roms_base>/<system>/`` dir,
        which must **never** be removed — so only the launch file itself is
        deleted. ``is_safe_rom_path`` stays the path-containment guard before
        any removal.
        """
        rom_dir = install.rom_dir
        file_path = install.file_path

        roms_base = self._retrodeck_paths.roms_path()
        if rom_dir and is_safe_rom_path(rom_dir, roms_base) and self._rom_file_store.is_dir(rom_dir):
            self._rom_file_store.remove_tree(rom_dir)
        elif file_path:
            if not is_safe_rom_path(file_path, roms_base):
                self._logger.error(f"Refusing to delete path outside roms directory: {file_path}")
                return
            if self._rom_file_store.is_dir(file_path):
                self._rom_file_store.remove_tree(file_path)
            elif self._rom_file_store.exists(file_path):
                self._rom_file_store.remove_file(file_path)

    def _remove_rom_io(self, rom_id: int, install: RomInstall) -> None:
        """Sync helper for remove_rom — file deletion (outside UoW) then row delete in a short write UoW.

        Files are deleted outside any transaction (ADR-0006); only the
        ``rom_installs`` row delete is wrapped. Per ADR-0007 the ``roms`` row,
        playtime, saves, and metadata are left untouched — an uninstall drops
        only the files and the install record.
        """
        self._delete_rom_files(install)
        with self._uow_factory() as uow:
            uow.rom_installs.delete(rom_id)

    async def remove_rom(self, rom_id: int | str) -> dict[str, Any]:
        """Remove a single installed ROM: delete files and drop the install record."""
        rom_id_int = int(rom_id)
        with self._uow_factory() as uow:
            install = uow.rom_installs.get(rom_id_int)
        if install is None:
            return {"success": False, "reason": "not_installed", "message": "ROM not installed"}

        try:
            await self._loop.run_in_executor(None, self._remove_rom_io, rom_id_int, install)
        except Exception as e:
            self._logger.error(f"Failed to delete ROM files: {e}")
            return {"success": False, "reason": ErrorCode.UNKNOWN.value, "message": "Failed to delete ROM files"}

        if self._download_queue_cleanup is not None:
            self._download_queue_cleanup.evict(rom_id_int)

        return {"success": True, "message": "ROM removed"}

    def _uninstall_all_roms_io(self) -> tuple[int, list[dict[str, str]]]:
        """Sync helper for uninstall_all_roms — bulk file deletion (outside UoW) then row deletes in a write UoW.

        Reads every install record in a short read UoW, deletes files outside
        any transaction (collecting per-ROM errors), then drops the install
        rows for the ROMs whose files were deleted in one write UoW. Per
        ADR-0007 the ``roms`` rows, playtime, saves, and metadata survive.
        """
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())

        count = 0
        errors: list[dict[str, str]] = []
        successfully_deleted: list[int] = []
        for install in installs:
            try:
                self._delete_rom_files(install)
                count += 1
                successfully_deleted.append(install.rom_id)
            except Exception as e:
                errors.append({"rom_id": str(install.rom_id), "error": str(e)})
                self._logger.error(f"Failed to delete ROM {install.rom_id}: {e}")

        with self._uow_factory() as uow:
            for rom_id in successfully_deleted:
                uow.rom_installs.delete(rom_id)
        return count, errors

    async def uninstall_all_roms(self) -> dict[str, Any]:
        """Remove all installed ROMs: delete files and drop their install records.

        Returns ``success`` (True only when every per-ROM deletion
        succeeded), ``removed_count`` (number of ROMs whose files were
        deleted), and ``errors`` (one ``{"rom_id", "error"}`` entry per
        failed deletion). Install records for partially-failed bulk runs
        are left intact for the failing entries so the user can retry.
        """
        count, errors = await self._loop.run_in_executor(None, self._uninstall_all_roms_io)
        if self._download_queue_cleanup is not None:
            self._download_queue_cleanup.clear()
        return {
            "success": len(errors) == 0,
            "removed_count": count,
            "errors": errors,
        }
