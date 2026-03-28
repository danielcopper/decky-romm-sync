"""DownloadService — ROM download engine.

Handles ROM downloads (single and multi-file), disk space checks,
download queue management, and partial download cleanup.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import time
import urllib.parse
import zipfile
from datetime import datetime
from typing import TYPE_CHECKING

from domain.rom_files import build_m3u_content, detect_launch_file, needs_m3u
from lib.errors import error_response

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        BiosPathProvider,
        EventEmitter,
        RommApiProtocol,
        RomsPathProvider,
        StatePersister,
        SystemResolver,
    )

_DOWNLOAD_QUEUE_MAX_TERMINAL = 50
_ZIP_TMP_EXT = ".zip.tmp"
_TMP_EXT = ".tmp"


class DownloadService:
    """ROM download engine: downloads and queue management."""

    def __init__(
        self,
        *,
        romm_api: RommApiProtocol,
        resolve_system: SystemResolver,
        state: dict,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        runtime_dir: str,
        emit: EventEmitter,
        save_state: StatePersister,
        get_roms_path: RomsPathProvider | None = None,
        get_bios_path: BiosPathProvider | None = None,
    ):
        self._romm_api = romm_api
        self._resolve_system = resolve_system
        self._state = state
        self._loop = loop
        self._logger = logger
        self._runtime_dir = runtime_dir
        self._emit = emit
        self._save_state = save_state
        self._get_roms_path = get_roms_path
        self._get_bios_path = get_bios_path

        # Owned state
        self._download_in_progress: set = set()
        self._download_queue: dict = {}
        self._download_tasks: dict = {}

    @property
    def download_tasks(self) -> dict:
        """Active download tasks (read-only view)."""
        return self._download_tasks

    def shutdown(self) -> None:
        """Cancel all active downloads and clear task tracking."""
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

    def clear_completed_downloads(self):
        """Remove all completed/failed/cancelled items from the download queue."""
        terminal_ids = [
            rid
            for rid, item in self._download_queue.items()
            if item.get("status") in ("completed", "failed", "cancelled")
        ]
        for rid in terminal_ids:
            del self._download_queue[rid]
        return {"success": True, "removed": len(terminal_ids)}

    def _remove_tmp_file(self, filepath):
        """Try to remove a tmp file, return True on success."""
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
                self._logger.info(f"Removed leftover tmp file: {filepath}")
                return True
        except OSError as e:
            self._logger.warning(f"Failed to remove tmp file {filepath}: {e}")
        return False

    def _clean_rom_tmp_files(self):
        """Remove leftover .tmp and .zip.tmp files from ROM directories."""
        cleaned = 0
        roms_base = self._get_roms_path() if self._get_roms_path else ""
        if not os.path.isdir(roms_base):
            return cleaned
        for system_dir in os.listdir(roms_base):
            system_path = os.path.join(roms_base, system_dir)
            if not os.path.isdir(system_path):
                continue
            for filename in os.listdir(system_path):
                full_path = os.path.join(system_path, filename)
                if filename.endswith((_TMP_EXT, _ZIP_TMP_EXT)) and self._remove_tmp_file(full_path):
                    cleaned += 1
        return cleaned

    def _clean_bios_tmp_files(self):
        """Remove leftover .tmp files from BIOS directory."""
        cleaned = 0
        bios_base = self._get_bios_path() if self._get_bios_path else ""
        if not os.path.isdir(bios_base):
            return cleaned
        for root, _dirs, files in os.walk(bios_base):
            for filename in files:
                if filename.endswith(_TMP_EXT) and self._remove_tmp_file(os.path.join(root, filename)):
                    cleaned += 1
        return cleaned

    def cleanup_leftover_tmp_files(self):
        """Remove leftover .tmp and .zip.tmp files from ROM and BIOS directories on startup."""
        cleaned = self._clean_rom_tmp_files() + self._clean_bios_tmp_files()
        if cleaned:
            self._logger.info(f"Cleaned {cleaned} leftover tmp file(s)")

    def _poll_download_requests_io(self, requests_path):
        """Sync helper for poll_download_requests — file lock + read + write in executor."""
        try:
            with open(requests_path, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    requests = json.load(f)
                except json.JSONDecodeError:
                    requests = []
                if not requests:
                    return []
                f.seek(0)
                f.truncate()
                json.dump([], f)
            return requests
        except FileNotFoundError:
            return []

    async def poll_download_requests(self):
        """Poll for download requests from the launcher script."""
        requests_path = os.path.join(self._runtime_dir, "download_requests.json")
        while True:
            try:
                await asyncio.sleep(2)
                requests = await self._loop.run_in_executor(None, self._poll_download_requests_io, requests_path)
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

        roms_dir = os.path.join(self._get_roms_path() if self._get_roms_path else "", system)
        file_name = rom_detail.get("fs_name", f"rom_{rom_id}")
        # Fix 1: Sanitize fs_name to prevent path traversal
        safe_name = os.path.basename(file_name)
        if safe_name != file_name:
            self._logger.warning(f"Sanitized fs_name from '{file_name}' to '{safe_name}'")
            file_name = safe_name
        file_size = rom_detail.get("fs_size_bytes", 0)

        # Check disk space: multi-file ROMs need space for ZIP + extracted contents
        os.makedirs(roms_dir, exist_ok=True)
        free_space = shutil.disk_usage(roms_dir).free
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
            task = self._loop.create_task(self._do_download(rom_id, rom_detail, target_path, system))
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

    def _decode_url_encoded_names(self, directory: str) -> None:
        """Rename URL-encoded filenames and directories (e.g. %20 -> space)."""
        for root, dirs, files in os.walk(directory, topdown=False):
            for fname in files:
                decoded = urllib.parse.unquote(fname)
                if decoded != fname:
                    os.replace(os.path.join(root, fname), os.path.join(root, decoded))
                    self._logger.info(f"Renamed URL-encoded file: {fname} -> {decoded}")
            for dname in dirs:
                decoded = urllib.parse.unquote(dname)
                if decoded != dname:
                    os.replace(os.path.join(root, dname), os.path.join(root, decoded))
                    self._logger.info(f"Renamed URL-encoded dir: {dname} -> {decoded}")

    def _post_download_multi_io(self, rom_id, rom_detail, target_path, file_name, system):
        """Sync helper for _do_download multi-file — extraction + renames in executor."""
        rom_dir_name = os.path.splitext(file_name)[0]
        extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
        os.makedirs(extract_dir, exist_ok=True)
        # Fix 4: Validate extract_dir is within roms_dir
        roms_base = self._get_roms_path() if self._get_roms_path else ""
        if not os.path.realpath(extract_dir).startswith(os.path.realpath(roms_base) + os.sep):
            raise ValueError(f"Extract directory would be outside roms directory: {extract_dir}")
        tmp_zip = target_path + _ZIP_TMP_EXT
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            # Fix 3: ZIP slip protection
            real_extract = os.path.realpath(extract_dir)
            for member in zf.namelist():
                member_path = os.path.realpath(os.path.join(extract_dir, member))
                if not member_path.startswith(real_extract + os.sep):
                    raise ValueError(f"ZIP member {member} would extract outside target directory")
            zf.extractall(extract_dir)
        os.remove(tmp_zip)
        self._decode_url_encoded_names(extract_dir)
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
            "installed_at": datetime.now().isoformat(),
            "rom_dir": extract_dir,
        }
        self._state["installed_roms"][str(rom_id)] = installed_entry
        self._save_state()
        return launch_file

    def _post_download_single_io(self, rom_id, rom_detail, target_path, file_name, system):
        """Sync helper for _do_download single-file — rename + state update in executor."""
        tmp_path = target_path + _TMP_EXT
        os.replace(tmp_path, target_path)

        installed_entry = {
            "rom_id": rom_id,
            "file_name": file_name,
            "file_path": target_path,
            "system": system,
            "platform_slug": rom_detail.get("platform_slug", ""),
            "installed_at": datetime.now().isoformat(),
        }
        self._state["installed_roms"][str(rom_id)] = installed_entry
        self._save_state()
        return target_path

    def _make_progress_callback(self, rom_id, rom_name, platform_name, file_name):
        """Build a throttled progress callback for a download."""
        last_emit = [0.0]  # mutable container for closure
        last_log = [0.0]

        def progress_callback(downloaded, total):
            now = time.monotonic()
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

    async def _do_download(self, rom_id, rom_detail, target_path, system):
        file_name = rom_detail.get("fs_name", f"rom_{rom_id}")
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

        finally:
            self._download_tasks.pop(rom_id, None)
            self._download_in_progress.discard(rom_id)
            self._prune_download_queue()

    def _maybe_generate_m3u_io(self, extract_dir: str, rom_detail: dict) -> None:
        """Auto-generate an M3U playlist if none exists and multiple disc files are found."""
        # Check if an M3U already exists (search recursively)
        for _root, _dirs, files in os.walk(extract_dir):
            for f in files:
                if f.lower().endswith(".m3u"):
                    return

        # Collect disc files: .cue, .chd, .iso (search recursively)
        disc_files = []
        for root, _dirs, files in os.walk(extract_dir):
            for f in files:
                if f.lower().endswith((".cue", ".chd", ".iso")):
                    # Store path relative to extract_dir for M3U entries
                    rel_path = os.path.relpath(os.path.join(root, f), extract_dir)
                    disc_files.append(rel_path)

        if not needs_m3u(disc_files):
            return

        rom_name = rom_detail.get("fs_name_no_ext", rom_detail.get("name", "playlist"))
        m3u_path = os.path.join(extract_dir, f"{rom_name}.m3u")
        with open(m3u_path, "w") as f:
            f.write(build_m3u_content(disc_files))
        self._logger.info(f"Auto-generated M3U playlist: {m3u_path}")

    def _collect_and_detect_launch_file(self, extract_dir: str) -> str:
        """Find the best launch file in an extracted multi-file ROM directory."""
        all_files: list[tuple[str, int]] = []
        for root, _dirs, files in os.walk(extract_dir):
            for f in files:
                path = os.path.join(root, f)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                all_files.append((path, size))

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
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                self._logger.warning(f"Cleanup failed for {path}: {e}")
        if has_multiple:
            rom_dir_name = os.path.splitext(file_name)[0]
            extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
            try:
                if os.path.isdir(extract_dir):
                    shutil.rmtree(extract_dir)
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
