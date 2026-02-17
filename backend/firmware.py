import os
import hashlib
import urllib.parse
from typing import TYPE_CHECKING, Any

import decky

if TYPE_CHECKING:
    import asyncio
    from typing import Callable, Optional, Protocol

    class _FirmwareDeps(Protocol):
        _state: dict
        loop: asyncio.AbstractEventLoop
        def _romm_request(self, path: str) -> Any: ...
        def _romm_download(self, path: str, dest: str, progress_callback: Optional[Callable] = None) -> None: ...


# BIOS destination subfolders within ~/retrodeck/bios/
# Most platforms: files go flat in bios/
# Exceptions need platform-specific subfolders
BIOS_DEST_MAP = {
    "dc": "dc/",            # Dreamcast
    "ps2": "pcsx2/bios/",   # PS2
}


class FirmwareMixin:
    def _firmware_slug(self, file_path):
        """Extract firmware slug from file_path (e.g. 'bios/ps' -> 'ps')."""
        parts = file_path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "bios":
            return parts[1]
        elif len(parts) >= 2:
            return parts[0]
        return ""

    def _platform_to_firmware_slugs(self, platform_slug):
        """Map platform slug to possible firmware directory slugs.

        RomM uses different slugs for platforms vs firmware directories
        (e.g. platform 'psx' -> firmware dir 'ps').
        """
        mapping = {
            "psx": ["psx", "ps"],
            "ps2": ["ps2"],
        }
        return mapping.get(platform_slug, [platform_slug])

    def _firmware_dest_path(self, firmware):
        """Determine local destination path for a firmware file."""
        bios_base = os.path.join(decky.DECKY_USER_HOME, "retrodeck", "bios")
        file_name = firmware.get("file_name", "")
        file_path = firmware.get("file_path", "")

        slug = self._firmware_slug(file_path)
        subfolder = BIOS_DEST_MAP.get(slug, "")
        return os.path.join(bios_base, subfolder, file_name)

    async def get_firmware_status(self):
        """Return BIOS/firmware status for all platforms on the RomM server."""
        try:
            firmware_list = await self.loop.run_in_executor(
                None, self._romm_request, "/api/firmware"
            )
        except Exception as e:
            decky.logger.error(f"Failed to fetch firmware: {e}")
            return {"success": False, "message": f"Failed to fetch firmware: {e}", "platforms": []}

        # Group firmware by platform
        platforms_map = {}
        for fw in firmware_list:
            platform_slug = self._firmware_slug(fw.get("file_path", "")) or "unknown"

            if platform_slug not in platforms_map:
                platforms_map[platform_slug] = {
                    "platform_slug": platform_slug,
                    "files": [],
                }

            dest = self._firmware_dest_path(fw)
            platforms_map[platform_slug]["files"].append({
                "id": fw.get("id"),
                "file_name": fw.get("file_name", ""),
                "size": fw.get("file_size_bytes", 0),
                "md5": fw.get("md5_hash", ""),
                "downloaded": os.path.exists(dest),
            })

        # Cross-reference: installed platforms that have firmware on server but not all downloaded
        installed_slugs = set()
        for entry in self._state["shortcut_registry"].values():
            slug = entry.get("platform_slug", "")
            if slug:
                installed_slugs.add(slug)

        # Mark platforms where user has games installed
        for plat in platforms_map.values():
            plat["has_games"] = plat["platform_slug"] in installed_slugs
            plat["all_downloaded"] = all(f["downloaded"] for f in plat["files"])

        platforms = sorted(platforms_map.values(), key=lambda p: p["platform_slug"])
        return {"success": True, "platforms": platforms}

    async def download_firmware(self, firmware_id):
        """Download a single firmware file from RomM."""
        firmware_id = int(firmware_id)
        try:
            fw = await self.loop.run_in_executor(
                None, self._romm_request, f"/api/firmware/{firmware_id}"
            )
        except Exception as e:
            decky.logger.error(f"Failed to fetch firmware {firmware_id}: {e}")
            return {"success": False, "message": f"Failed to fetch firmware details: {e}"}

        file_name = fw.get("file_name", "")
        dest = self._firmware_dest_path(fw)
        tmp_path = dest + ".tmp"

        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            download_path = f"/api/firmware/{firmware_id}/content/{urllib.parse.quote(file_name, safe='')}"
            await self.loop.run_in_executor(
                None, self._romm_download, download_path, tmp_path
            )
            os.replace(tmp_path, dest)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            decky.logger.error(f"Failed to download firmware {file_name}: {e}")
            return {"success": False, "message": f"Download failed: {e}"}

        # Verify MD5 if available
        md5_match = None
        expected_md5 = fw.get("md5_hash", "")
        if expected_md5:
            h = hashlib.md5()
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            md5_match = h.hexdigest() == expected_md5

        decky.logger.info(f"Firmware downloaded: {file_name} -> {dest}")
        return {"success": True, "file_path": dest, "md5_match": md5_match}

    async def download_all_firmware(self, platform_slug):
        """Download all firmware for a given platform slug."""
        try:
            firmware_list = await self.loop.run_in_executor(
                None, self._romm_request, "/api/firmware"
            )
        except Exception as e:
            decky.logger.error(f"Failed to fetch firmware: {e}")
            return {"success": False, "message": f"Failed to fetch firmware: {e}", "downloaded": 0}

        # Filter by platform slug
        platform_firmware = []
        for fw in firmware_list:
            slug = self._firmware_slug(fw.get("file_path", ""))
            if slug == platform_slug:
                platform_firmware.append(fw)

        downloaded = 0
        errors = []
        for fw in platform_firmware:
            dest = self._firmware_dest_path(fw)
            if os.path.exists(dest):
                continue
            result = await self.download_firmware(fw["id"])
            if result.get("success"):
                downloaded += 1
            else:
                errors.append(fw.get("file_name", str(fw["id"])))

        msg = f"Downloaded {downloaded} firmware files"
        if errors:
            msg += f" ({len(errors)} failed: {', '.join(errors)})"
        return {"success": True, "message": msg, "downloaded": downloaded}

    async def check_platform_bios(self, platform_slug):
        """Check if RomM has firmware for this platform and whether it's downloaded."""
        local_count = 0
        server_count = 0
        files = []
        fw_slugs = self._platform_to_firmware_slugs(platform_slug)
        try:
            firmware_list = await self.loop.run_in_executor(
                None, self._romm_request, "/api/firmware"
            )
            for fw in firmware_list:
                fw_slug = self._firmware_slug(fw.get("file_path", ""))
                if not fw_slug:
                    continue
                if fw_slug in fw_slugs:
                    server_count += 1
                    dest = self._firmware_dest_path(fw)
                    downloaded = os.path.exists(dest)
                    if downloaded:
                        local_count += 1
                    files.append({
                        "file_name": fw.get("file_name", ""),
                        "downloaded": downloaded,
                    })
        except Exception:
            pass

        if server_count == 0:
            return {"needs_bios": False}

        return {
            "needs_bios": True,
            "server_count": server_count,
            "local_count": local_count,
            "all_downloaded": local_count >= server_count,
            "files": files,
        }
