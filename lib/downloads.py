import os
import json
import asyncio
import shutil
import time
import zipfile
import urllib.parse
from datetime import datetime
from typing import TYPE_CHECKING, Any

import decky

if TYPE_CHECKING:
    from typing import Callable, Optional, Protocol

    class _DownloadDeps(Protocol):
        _download_in_progress: set
        _download_queue: dict
        _download_tasks: dict
        _state: dict
        loop: asyncio.AbstractEventLoop
        def _romm_request(self, path: str) -> Any: ...
        def _romm_download(self, path: str, dest: str, progress_callback: Optional[Callable] = None) -> None: ...
        def _resolve_system(self, platform_slug: str, platform_fs_slug: Optional[str] = None) -> str: ...
        def _save_state(self) -> None: ...


class DownloadMixin:
    async def _poll_download_requests(self):
        """Poll for download requests from the launcher script."""
        requests_path = os.path.join(
            decky.DECKY_PLUGIN_RUNTIME_DIR, "download_requests.json"
        )
        while True:
            try:
                await asyncio.sleep(2)
                if not os.path.exists(requests_path):
                    continue
                with open(requests_path, "r") as f:
                    requests = json.load(f)
                if not requests:
                    continue
                # Clear the file immediately
                with open(requests_path, "w") as f:
                    json.dump([], f)
                for req in requests:
                    rom_id = req.get("rom_id")
                    if rom_id:
                        await self.start_download(rom_id)
            except asyncio.CancelledError:
                return
            except Exception as e:
                decky.logger.warning(f"Download request poll error: {e}")

    async def start_download(self, rom_id):
        rom_id = int(rom_id)
        if rom_id in self._download_in_progress:
            return {"success": False, "message": "Already downloading"}

        self._download_in_progress.add(rom_id)
        try:
            rom_detail = await self.loop.run_in_executor(
                None, self._romm_request, f"/api/roms/{rom_id}"
            )
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            decky.logger.error(f"Failed to fetch ROM {rom_id}: {e}")
            return {"success": False, "message": "Could not connect to RomM server"}

        platform_slug = rom_detail.get("platform_slug", "")
        platform_fs_slug = rom_detail.get("platform_fs_slug")
        system = self._resolve_system(platform_slug, platform_fs_slug)

        roms_dir = os.path.join(decky.DECKY_USER_HOME, "retrodeck", "roms", system)
        file_name = rom_detail.get("fs_name", f"rom_{rom_id}")
        # Fix 1: Sanitize fs_name to prevent path traversal
        safe_name = os.path.basename(file_name)
        if safe_name != file_name:
            decky.logger.warning(f"Sanitized fs_name from '{file_name}' to '{safe_name}'")
            file_name = safe_name
        file_size = rom_detail.get("fs_size_bytes", 0)

        # Check disk space (need file size + 100MB buffer)
        os.makedirs(roms_dir, exist_ok=True)
        free_space = shutil.disk_usage(roms_dir).free
        required = file_size + (100 * 1024 * 1024)
        if file_size and free_space < required:
            self._download_in_progress.discard(rom_id)
            free_mb = free_space // (1024 * 1024)
            need_mb = required // (1024 * 1024)
            return {"success": False, "message": f"Not enough disk space ({free_mb}MB free, need {need_mb}MB)"}

        target_path = os.path.join(roms_dir, file_name)

        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", platform_slug)

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

        task = self.loop.create_task(
            self._do_download(rom_id, rom_detail, target_path, system)
        )
        self._download_tasks[rom_id] = task
        return {"success": True, "message": "Download started"}

    async def _do_download(self, rom_id, rom_detail, target_path, system):
        file_name = rom_detail.get("fs_name", f"rom_{rom_id}")
        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", rom_detail.get("platform_slug", ""))
        has_multiple = rom_detail.get("has_multiple_files", False)
        last_emit = [0.0]  # mutable container for closure

        def progress_callback(downloaded, total):
            now = time.monotonic()
            if now - last_emit[0] < 0.5 and downloaded < total:
                return
            last_emit[0] = now
            progress = downloaded / total if total else 0
            self._download_queue[rom_id].update({
                "progress": progress,
                "bytes_downloaded": downloaded,
                "total_bytes": total,
            })
            self.loop.call_soon_threadsafe(
                self.loop.create_task,
                decky.emit("download_progress", {
                    "rom_id": rom_id,
                    "rom_name": rom_name,
                    "status": "downloading",
                    "progress": progress,
                    "bytes_downloaded": downloaded,
                    "total_bytes": total,
                })
            )

        try:
            download_path = f"/api/roms/{rom_id}/content/{urllib.parse.quote(file_name, safe='')}"

            if has_multiple:
                # Multi-file ROM: API returns ZIP, download to temp then extract
                tmp_zip = target_path + ".zip.tmp"
                await self.loop.run_in_executor(
                    None, self._romm_download, download_path, tmp_zip, progress_callback
                )
                # Extract to a subdirectory named after the ROM
                rom_dir_name = os.path.splitext(file_name)[0]
                extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
                os.makedirs(extract_dir, exist_ok=True)
                # Fix 4: Validate extract_dir is within roms_dir
                roms_base = os.path.join(decky.DECKY_USER_HOME, "retrodeck", "roms")
                if not os.path.realpath(extract_dir).startswith(os.path.realpath(roms_base) + os.sep):
                    raise ValueError(f"Extract directory would be outside roms directory: {extract_dir}")
                with zipfile.ZipFile(tmp_zip, "r") as zf:
                    # Fix 3: ZIP slip protection
                    real_extract = os.path.realpath(extract_dir)
                    for member in zf.namelist():
                        member_path = os.path.realpath(os.path.join(extract_dir, member))
                        if not member_path.startswith(real_extract + os.sep) and member_path != real_extract:
                            raise ValueError(f"ZIP member {member} would extract outside target directory")
                    zf.extractall(extract_dir)
                os.remove(tmp_zip)
                # Fix URL-encoded filenames from RomM (e.g. %20 -> space)
                for root, dirs, files in os.walk(extract_dir, topdown=False):
                    for fname in files:
                        decoded = urllib.parse.unquote(fname)
                        if decoded != fname:
                            old_path = os.path.join(root, fname)
                            new_path = os.path.join(root, decoded)
                            os.replace(old_path, new_path)
                            decky.logger.info(f"Renamed URL-encoded file: {fname} -> {decoded}")
                    for dname in dirs:
                        decoded = urllib.parse.unquote(dname)
                        if decoded != dname:
                            old_path = os.path.join(root, dname)
                            new_path = os.path.join(root, decoded)
                            os.replace(old_path, new_path)
                            decky.logger.info(f"Renamed URL-encoded dir: {dname} -> {decoded}")
                # Auto-generate M3U if missing and multiple disc files exist
                self._maybe_generate_m3u(extract_dir, rom_detail)
                # Detect launch file: prefer M3U > CUE > largest file
                launch_file = self._detect_launch_file(extract_dir)
                final_path = launch_file
            else:
                tmp_path = target_path + ".tmp"
                await self.loop.run_in_executor(
                    None, self._romm_download, download_path, tmp_path, progress_callback
                )
                os.replace(tmp_path, target_path)
                final_path = target_path

            # Register as installed
            installed_entry = {
                "rom_id": rom_id,
                "file_name": file_name,
                "file_path": final_path,
                "system": system,
                "platform_slug": rom_detail.get("platform_slug", ""),
                "installed_at": datetime.now().isoformat(),
            }
            if has_multiple:
                installed_entry["rom_dir"] = extract_dir
            self._state["installed_roms"][str(rom_id)] = installed_entry
            self._save_state()

            self._download_queue[rom_id]["status"] = "completed"
            self._download_queue[rom_id]["progress"] = 1.0
            await decky.emit("download_complete", {
                "rom_id": rom_id,
                "rom_name": rom_name,
                "platform_name": platform_name,
                "file_path": final_path,
            })
            decky.logger.info(f"Download complete: {rom_name} -> {final_path}")

        except asyncio.CancelledError:
            self._download_queue[rom_id]["status"] = "cancelled"
            self._cleanup_partial_download(target_path, rom_detail.get("has_multiple_files", False), file_name)
            decky.logger.info(f"Download cancelled: {rom_name}")

        except Exception as e:
            self._download_queue[rom_id]["status"] = "failed"
            self._download_queue[rom_id]["error"] = str(e)
            self._cleanup_partial_download(target_path, rom_detail.get("has_multiple_files", False), file_name)
            decky.logger.error(f"Download failed for {rom_name}: {e}")

        finally:
            self._download_tasks.pop(rom_id, None)
            self._download_in_progress.discard(rom_id)

    def _maybe_generate_m3u(self, extract_dir, rom_detail):
        """Auto-generate an M3U playlist if none exists and multiple disc files are found."""
        # Check if an M3U already exists (search recursively)
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                if f.lower().endswith(".m3u"):
                    return

        # Collect disc files: .cue, .chd, .iso (search recursively)
        disc_files = []
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                if f.lower().endswith((".cue", ".chd", ".iso")):
                    # Store path relative to extract_dir for M3U entries
                    rel_path = os.path.relpath(os.path.join(root, f), extract_dir)
                    disc_files.append(rel_path)

        if len(disc_files) < 2:
            return

        # Sort naturally (Disc 1, Disc 2, etc.)
        disc_files.sort()

        rom_name = rom_detail.get("fs_name_no_ext", rom_detail.get("name", "playlist"))
        m3u_path = os.path.join(extract_dir, f"{rom_name}.m3u")
        with open(m3u_path, "w") as f:
            f.write("\n".join(disc_files) + "\n")
        decky.logger.info(f"Auto-generated M3U playlist: {m3u_path}")

    def _detect_launch_file(self, extract_dir):
        """Find the best launch file in an extracted multi-file ROM directory."""
        all_files = []
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                all_files.append(os.path.join(root, f))

        # Prefer M3U > CUE > largest file
        for ext in (".m3u", ".cue"):
            matches = [f for f in all_files if f.lower().endswith(ext)]
            if matches:
                return matches[0]

        if all_files:
            return max(all_files, key=os.path.getsize)
        return extract_dir

    def _cleanup_partial_download(self, target_path, has_multiple, file_name):
        """Clean up partial download files."""
        try:
            tmp_zip = target_path + ".zip.tmp"
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
            tmp_single = target_path + ".tmp"
            if os.path.exists(tmp_single):
                os.remove(tmp_single)
            if os.path.exists(target_path):
                os.remove(target_path)
            if has_multiple:
                rom_dir_name = os.path.splitext(file_name)[0]
                extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
                if os.path.isdir(extract_dir):
                    shutil.rmtree(extract_dir)
        except Exception as e:
            decky.logger.warning(f"Cleanup error: {e}")

    async def cancel_download(self, rom_id):
        rom_id = int(rom_id)
        task = self._download_tasks.get(rom_id)
        if not task:
            return {"success": False, "message": "No active download for this ROM"}
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return {"success": True, "message": "Download cancelled"}

    async def get_download_queue(self):
        return {"downloads": list(self._download_queue.values())}

    async def get_installed_rom(self, rom_id):
        return self._state["installed_roms"].get(str(int(rom_id)))

    def _is_safe_rom_path(self, path):
        """Check that a path is safely contained within the roms base directory."""
        roms_base = os.path.join(decky.DECKY_USER_HOME, "retrodeck", "roms")
        resolved = os.path.realpath(path)
        real_base = os.path.realpath(roms_base)
        if not resolved.startswith(real_base + os.sep):
            return False
        # Must be at least 2 levels deep (e.g. roms/gb/file.zip, not roms/gb/)
        rel = os.path.relpath(resolved, real_base)
        parts = rel.split(os.sep)
        if len(parts) < 2:
            return False
        return True

    def _delete_rom_files(self, installed):
        """Delete ROM files for an installed entry. Handles both single-file and multi-file ROMs."""
        rom_dir = installed.get("rom_dir", "")
        file_path = installed.get("file_path", "")

        if rom_dir and os.path.isdir(rom_dir):
            if not self._is_safe_rom_path(rom_dir):
                decky.logger.error(f"Refusing to delete path outside roms directory: {rom_dir}")
                return
            shutil.rmtree(rom_dir)
        elif file_path:
            if not self._is_safe_rom_path(file_path):
                decky.logger.error(f"Refusing to delete path outside roms directory: {file_path}")
                return
            if os.path.isdir(file_path):
                shutil.rmtree(file_path)
            elif os.path.exists(file_path):
                os.remove(file_path)

    async def remove_rom(self, rom_id):
        rom_id_str = str(int(rom_id))
        installed = self._state["installed_roms"].get(rom_id_str)
        if not installed:
            return {"success": False, "message": "ROM not installed"}

        try:
            self._delete_rom_files(installed)
        except Exception as e:
            decky.logger.error(f"Failed to delete ROM files: {e}")
            return {"success": False, "message": "Failed to delete ROM files"}

        del self._state["installed_roms"][rom_id_str]
        self._download_queue.pop(int(rom_id), None)
        self._save_state()
        return {"success": True, "message": "ROM removed"}

    async def uninstall_all_roms(self):
        count = 0
        errors = []
        successfully_deleted = []
        for rom_id_str, installed in list(self._state["installed_roms"].items()):
            try:
                self._delete_rom_files(installed)
                count += 1
                successfully_deleted.append(rom_id_str)
            except Exception as e:
                errors.append(f"{rom_id_str}: {e}")
                decky.logger.error(f"Failed to delete ROM {rom_id_str}: {e}")

        for rom_id_str in successfully_deleted:
            self._state["installed_roms"].pop(rom_id_str, None)
        self._download_queue.clear()
        self._save_state()
        msg = f"Removed {count} ROMs"
        if errors:
            msg += f" ({len(errors)} errors)"
        return {"success": True, "message": msg, "removed_count": count}
