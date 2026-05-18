"""DownloadService — ROM download orchestration.

Handles ROM downloads (single and multi-file), disk space checks,
download queue management, and partial download cleanup. All raw
filesystem I/O is delegated to the ``DownloadFileStore`` and
``DownloadQueueAdapter`` Protocols.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.rom_files import build_m3u_content, detect_launch_file, needs_m3u, resolve_local_file_name
from lib.errors import error_response

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        Clock,
        DownloadFileStore,
        DownloadQueueAdapter,
        EventEmitter,
        MigrationPendingFn,
        RetroDeckPaths,
        RommRomReader,
        Sleeper,
        StatePersister,
        SystemResolver,
    )

_DOWNLOAD_QUEUE_MAX_TERMINAL = 50
_ZIP_TMP_EXT = ".zip.tmp"
_TMP_EXT = ".tmp"


@dataclass(frozen=True)
class DownloadServiceConfig:
    """Frozen wiring bundle handed to ``DownloadService.__init__``.

    Holds the Protocol-typed adapters, the live state dict, runtime
    infrastructure, time/sleep seams, path providers, and migration-
    aware callbacks DownloadService needs at construction time.
    """

    romm_api: RommRomReader
    state: dict
    download_file_store: DownloadFileStore
    download_queue: DownloadQueueAdapter
    resolve_system: SystemResolver
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    runtime_dir: str
    emit: EventEmitter
    clock: Clock
    sleeper: Sleeper
    state_persister: StatePersister
    retrodeck_paths: RetroDeckPaths | None = None
    is_retrodeck_migration_pending: MigrationPendingFn | None = None


class DownloadService:
    """ROM download engine: downloads and queue management."""

    def __init__(self, *, config: DownloadServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._state = config.state
        self._download_file_store = config.download_file_store
        self._download_queue_io = config.download_queue
        self._resolve_system = config.resolve_system
        self._loop = config.loop
        self._logger = config.logger
        self._runtime_dir = config.runtime_dir
        self._emit = config.emit
        self._clock = config.clock
        self._sleeper = config.sleeper
        self._state_persister = config.state_persister
        self._retrodeck_paths = config.retrodeck_paths
        self._is_retrodeck_migration_pending = config.is_retrodeck_migration_pending

        # Owned state
        self._download_in_progress: set = set()
        self._download_queue: dict = {}
        self._download_tasks: dict = {}
        self._poll_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Spawn the background ``poll_download_requests`` task.

        Owns the task handle so :meth:`shutdown` can cancel it on
        unload. Idempotent: a second call while a poll task is already
        running is a no-op.
        """
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = self._loop.create_task(self.poll_download_requests())

    async def shutdown(self) -> None:
        """Cancel the background poll task and all active downloads.

        Awaits the poll task so the loop fully exits before unload
        returns; per-ROM download tasks are cancelled fire-and-forget
        (their ``finally`` clauses run on the event loop after this
        method returns, which is acceptable on plugin unload).
        """
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        for task in self._download_tasks.values():
            task.cancel()
        self._download_tasks.clear()

    def _prune_download_queue(self):
        """Remove oldest completed/failed/cancelled items when over the limit.

        Keeps all active (downloading) items. Retains up to
        _DOWNLOAD_QUEUE_MAX_TERMINAL terminal items, removing the oldest
        (by insertion order) when the count exceeds the limit.
        """
        terminal_ids = [
            rid
            for rid, item in self._download_queue.items()
            if item.get("status") in ("completed", "failed", "cancelled")
        ]
        excess = len(terminal_ids) - _DOWNLOAD_QUEUE_MAX_TERMINAL
        if excess <= 0:
            return
        # Dict preserves insertion order (Python 3.7+), so the first
        # entries in terminal_ids are the oldest.
        for rid in terminal_ids[:excess]:
            del self._download_queue[rid]

    def _remove_tmp_files(self, paths: list[str]) -> int:
        """Remove each path in *paths*, logging a warning on per-file failure.

        Returns the count of successful removals. Mirrors the
        SteamGridService cache-prune pattern: service owns the loop +
        ``try``/``except`` + ``logger.warning`` so the operational
        signal on each failure is preserved instead of being swallowed
        inside the adapter.
        """
        removed = 0
        for path in paths:
            try:
                self._download_file_store.remove_file(path)
                removed += 1
            except OSError as e:
                self._logger.warning(f"Failed to remove tmp file {path}: {e}")
        return removed

    def _clean_rom_tmp_files(self):
        """Remove leftover .tmp and .zip.tmp files from ROM directories."""
        roms_base = self._retrodeck_paths.roms_path() if self._retrodeck_paths else ""
        if not roms_base:
            return 0
        paths = self._download_file_store.walk_files_matching_suffixes(roms_base, (_TMP_EXT, _ZIP_TMP_EXT))
        return self._remove_tmp_files(paths)

    def _clean_bios_tmp_files(self):
        """Remove leftover .tmp files from BIOS directory."""
        bios_base = self._retrodeck_paths.bios_path() if self._retrodeck_paths else ""
        if not bios_base:
            return 0
        paths = self._download_file_store.walk_files_matching_suffixes(bios_base, (_TMP_EXT,))
        return self._remove_tmp_files(paths)

    def cleanup_leftover_tmp_files(self):
        """Remove leftover .tmp and .zip.tmp files from ROM and BIOS directories on startup."""
        cleaned = self._clean_rom_tmp_files() + self._clean_bios_tmp_files()
        if cleaned:
            self._logger.info(f"Cleaned {cleaned} leftover tmp file(s)")

    async def poll_download_requests(self):
        """Poll for download requests from the launcher script."""
        requests_path = os.path.join(self._runtime_dir, "download_requests.json")
        while True:
            try:
                await self._sleeper.sleep(2)
                # Pause polling while a RetroDECK migration is pending — must
                # short-circuit BEFORE poll_and_clear reads + clears the
                # request file, otherwise queued requests would be silently
                # dropped on the floor.
                if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
                    continue
                requests = await self._loop.run_in_executor(None, self._download_queue_io.poll_and_clear, requests_path)
                if not requests:
                    continue
                for req in requests:
                    rom_id = req.get("rom_id")
                    if rom_id:
                        await self.start_download(rom_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._logger.warning(f"Download request poll error: {e}")

    async def start_download(self, rom_id):
        rom_id = int(rom_id)
        if rom_id in self._download_in_progress:
            return {"success": False, "message": "Already downloading"}

        self._download_in_progress.add(rom_id)
        try:
            rom_detail = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            self._logger.error(f"Failed to fetch ROM {rom_id}: {e}")
            return error_response(e)

        platform_slug = rom_detail.get("platform_slug", "")
        platform_fs_slug = rom_detail.get("platform_fs_slug")
        system = self._resolve_system(platform_slug, platform_fs_slug)

        roms_dir = os.path.join(self._retrodeck_paths.roms_path() if self._retrodeck_paths else "", system)
        file_name, files_missing = resolve_local_file_name(rom_detail)
        if files_missing:
            self._logger.warning(
                f"has_nested_single_file=true but files list is empty; falling back to fs_name='{file_name}'"
            )
        # Fix 1: Sanitize fs_name to prevent path traversal
        safe_name = os.path.basename(file_name)
        if safe_name != file_name:
            self._logger.warning(f"Sanitized fs_name from '{file_name}' to '{safe_name}'")
            file_name = safe_name
        file_size = rom_detail.get("fs_size_bytes", 0)

        # Check disk space: multi-file ROMs need space for ZIP + extracted contents
        self._download_file_store.make_dirs(roms_dir)
        free_space = self._download_file_store.disk_free(roms_dir)
        buffer = 100 * 1024 * 1024
        required = file_size * 2 + buffer if rom_detail.get("has_multiple_files") else file_size + buffer
        if file_size and free_space < required:
            self._download_in_progress.discard(rom_id)
            free_mb = free_space // (1024 * 1024)
            need_mb = required // (1024 * 1024)
            return {"success": False, "message": f"Not enough disk space ({free_mb}MB free, need {need_mb}MB)"}

        target_path = os.path.join(roms_dir, file_name)

        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", platform_slug)

        try:
            task = self._loop.create_task(self._do_download(rom_id, rom_detail, target_path, system, file_name))
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            self._logger.error(f"Failed to start download task for ROM {rom_id}: {e}")
            return {"success": False, "message": "Failed to start download"}

        self._download_queue[rom_id] = {
            "rom_id": rom_id,
            "rom_name": rom_name,
            "platform_name": platform_name,
            "file_name": file_name,
            "status": "downloading",
            "progress": 0,
            "bytes_downloaded": 0,
            "total_bytes": file_size,
        }
        self._download_tasks[rom_id] = task
        return {"success": True, "message": "Download started"}

    def _post_download_multi_io(self, rom_id, rom_detail, target_path, file_name, system):
        """Sync helper for _do_download multi-file — extraction + renames in executor."""
        rom_dir_name = os.path.splitext(file_name)[0]
        extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
        self._download_file_store.make_dirs(extract_dir)
        roms_base = self._retrodeck_paths.roms_path() if self._retrodeck_paths else ""
        tmp_zip = target_path + _ZIP_TMP_EXT
        # ZIP-slip protection: adapter validates members resolve within extract_dir
        # AND that extract_dir itself resolves within roms_base.
        self._download_file_store.extract_zip(tmp_zip, extract_dir, roms_base)
        self._download_file_store.remove_file(tmp_zip)
        self._download_file_store.decode_url_encoded_names(extract_dir)
        # Auto-generate M3U if missing and multiple disc files exist
        self._maybe_generate_m3u_io(extract_dir, rom_detail)
        # Detect launch file: prefer M3U > CUE > largest file
        launch_file = self._collect_and_detect_launch_file(extract_dir)

        # Register as installed
        installed_entry = {
            "rom_id": rom_id,
            "file_name": file_name,
            "file_path": launch_file,
            "system": system,
            "platform_slug": rom_detail.get("platform_slug", ""),
            "installed_at": self._clock.now().isoformat(),
            "rom_dir": extract_dir,
        }
        self._state["installed_roms"][str(rom_id)] = installed_entry
        self._state_persister.save_state()
        return launch_file

    def _post_download_single_io(self, rom_id, rom_detail, target_path, file_name, system):
        """Sync helper for _do_download single-file — rename + state update in executor."""
        tmp_path = target_path + _TMP_EXT
        self._download_file_store.rename(tmp_path, target_path)

        installed_entry = {
            "rom_id": rom_id,
            "file_name": file_name,
            "file_path": target_path,
            "system": system,
            "platform_slug": rom_detail.get("platform_slug", ""),
            "installed_at": self._clock.now().isoformat(),
        }
        self._state["installed_roms"][str(rom_id)] = installed_entry
        self._state_persister.save_state()
        return target_path

    def _make_progress_callback(self, rom_id, rom_name, platform_name, file_name):
        """Build a throttled progress callback for a download."""
        last_emit = [0.0]  # mutable container for closure
        last_log = [0.0]

        def progress_callback(downloaded, total):
            now = self._clock.monotonic()
            if now - last_log[0] >= 30.0:
                last_log[0] = now
                mb_dl = downloaded / (1024 * 1024)
                mb_total = total / (1024 * 1024) if total else 0
                pct = (downloaded / total * 100) if total else 0
                self._logger.info(f"Download progress: {rom_name} — {mb_dl:.1f}/{mb_total:.1f} MB ({pct:.0f}%)")
            if now - last_emit[0] < 0.5 and downloaded < total:
                return
            last_emit[0] = now
            progress = downloaded / total if total else 0
            self._download_queue[rom_id].update(
                {
                    "progress": progress,
                    "bytes_downloaded": downloaded,
                    "total_bytes": total,
                }
            )
            self._loop.call_soon_threadsafe(
                self._loop.create_task,
                self._emit(
                    "download_progress",
                    {
                        "rom_id": rom_id,
                        "rom_name": rom_name,
                        "platform_name": platform_name,
                        "file_name": file_name,
                        "status": "downloading",
                        "progress": progress,
                        "bytes_downloaded": downloaded,
                        "total_bytes": total,
                    },
                ),
            )

        return progress_callback

    async def _do_download(self, rom_id, rom_detail, target_path, system, file_name):
        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", rom_detail.get("platform_slug", ""))
        has_multiple = rom_detail.get("has_multiple_files", False)
        progress_callback = self._make_progress_callback(rom_id, rom_name, platform_name, file_name)

        try:
            self._logger.info(f"Download starting: {rom_name} (rom_id={rom_id}, multi={has_multiple}) -> {target_path}")

            if has_multiple:
                # Multi-file ROM: API returns ZIP, download to temp then extract
                tmp_zip = target_path + _ZIP_TMP_EXT
                await self._loop.run_in_executor(
                    None, self._romm_api.download_rom_content, rom_id, file_name, tmp_zip, progress_callback
                )
                final_path = await self._loop.run_in_executor(
                    None, self._post_download_multi_io, rom_id, rom_detail, target_path, file_name, system
                )
            else:
                tmp_path = target_path + _TMP_EXT
                await self._loop.run_in_executor(
                    None, self._romm_api.download_rom_content, rom_id, file_name, tmp_path, progress_callback
                )
                final_path = await self._loop.run_in_executor(
                    None, self._post_download_single_io, rom_id, rom_detail, target_path, file_name, system
                )

            self._download_queue[rom_id]["status"] = "completed"
            self._download_queue[rom_id]["progress"] = 1.0
            await self._emit(
                "download_complete",
                {
                    "rom_id": rom_id,
                    "rom_name": rom_name,
                    "platform_name": platform_name,
                    "file_path": final_path,
                },
            )
            self._logger.info(f"Download complete: {rom_name} -> {final_path}")

        except asyncio.CancelledError:
            self._download_queue[rom_id]["status"] = "cancelled"
            self._cleanup_partial_download(target_path, rom_detail.get("has_multiple_files", False), file_name)
            self._logger.info(f"Download cancelled: {rom_name}")
            raise

        except Exception as e:
            self._download_queue[rom_id]["status"] = "failed"
            self._download_queue[rom_id]["error"] = str(e)
            self._cleanup_partial_download(target_path, rom_detail.get("has_multiple_files", False), file_name)
            self._logger.error(f"Download failed for {rom_name}: {e}")
            await self._emit(
                "download_failed",
                {
                    "rom_id": rom_id,
                    "rom_name": rom_name,
                    "platform_name": platform_name,
                    "error_message": str(e),
                },
            )

        finally:
            self._download_tasks.pop(rom_id, None)
            self._download_in_progress.discard(rom_id)
            self._prune_download_queue()

    def _maybe_generate_m3u_io(self, extract_dir: str, rom_detail: dict) -> None:
        """Auto-generate an M3U playlist if none exists and multiple disc files are found."""
        all_files = self._download_file_store.scan_files_with_sizes(extract_dir)
        # Check if an M3U already exists (search recursively)
        if any(path.lower().endswith(".m3u") for path, _size in all_files):
            return

        # Collect disc files: .cue, .chd, .iso (search recursively)
        disc_files = [
            os.path.relpath(path, extract_dir)
            for path, _size in all_files
            if path.lower().endswith((".cue", ".chd", ".iso"))
        ]

        if not needs_m3u(disc_files):
            return

        rom_name = rom_detail.get("fs_name_no_ext", rom_detail.get("name", "playlist"))
        m3u_path = os.path.join(extract_dir, f"{rom_name}.m3u")
        self._download_file_store.write_text_atomic(m3u_path, build_m3u_content(disc_files))
        self._logger.info(f"Auto-generated M3U playlist: {m3u_path}")

    def _collect_and_detect_launch_file(self, extract_dir: str) -> str:
        """Find the best launch file in an extracted multi-file ROM directory."""
        all_files = self._download_file_store.scan_files_with_sizes(extract_dir)
        result = detect_launch_file(all_files)
        return result if result is not None else extract_dir

    def _cleanup_partial_download(self, target_path, has_multiple, file_name):
        """Clean up partial download files. Each step is independent so one failure doesn't block others."""
        paths_to_remove = [
            target_path + _ZIP_TMP_EXT,
            target_path + _TMP_EXT,
            target_path,
        ]
        for path in paths_to_remove:
            try:
                self._download_file_store.remove_file(path)
            except Exception as e:
                self._logger.warning(f"Cleanup failed for {path}: {e}")
        if has_multiple:
            rom_dir_name = os.path.splitext(file_name)[0]
            extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
            try:
                self._download_file_store.remove_tree(extract_dir)
            except Exception as e:
                self._logger.warning(f"Cleanup failed for directory {extract_dir}: {e}")

    def cancel_download(self, rom_id):
        rom_id = int(rom_id)
        task = self._download_tasks.get(rom_id)
        if not task:
            return {"success": False, "message": "No active download for this ROM"}
        task.cancel()
        return {"success": True, "message": "Download cancelled"}

    def get_download_queue(self):
        return {"downloads": list(self._download_queue.values())}

    def get_installed_rom(self, rom_id):
        return self._state["installed_roms"].get(str(int(rom_id)))

    # ── DownloadQueueCleanup Protocol ──────────────────────────────

    def evict(self, rom_id: int) -> None:
        """Remove the queue entry for *rom_id* if present. Idempotent."""
        self._download_queue.pop(int(rom_id), None)

    def clear(self) -> None:
        """Remove all entries from the download queue."""
        self._download_queue.clear()
