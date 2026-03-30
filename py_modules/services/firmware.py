"""FirmwareService — BIOS/firmware management.

Handles BIOS registry loading, firmware status checks, downloads,
deletion, and per-core filtering for RetroArch emulators.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from domain import es_de_config
from domain.bios import collect_firmware_status
from lib.errors import error_response

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from services.protocols import BiosPathProvider, RommApiProtocol, StatePersister

_FIRMWARE_CACHE_TTL = 3600  # 1 hour


class FirmwareService:
    """BIOS/firmware management: registry, status, downloads, deletion."""

    def __init__(
        self,
        *,
        romm_api: RommApiProtocol,
        state: dict,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        plugin_dir: str,
        save_state: StatePersister,
        save_firmware_cache: Callable[[dict], None] | None = None,
        load_firmware_cache: Callable[[], dict] | None = None,
        get_bios_path: BiosPathProvider | None = None,
    ) -> None:
        self._romm_api = romm_api
        self._state = state
        self._loop = loop
        self._logger = logger
        self._plugin_dir = plugin_dir
        self._save_state = save_state
        self._save_firmware_cache = save_firmware_cache
        self._load_firmware_cache = load_firmware_cache
        self._get_bios_path = get_bios_path
        self._bios_registry: dict = {}
        self._bios_files_index: dict = {}
        self._firmware_cache: list | None = None
        self._firmware_cache_at: float = 0
        self._firmware_cache_epoch: float = 0
        self._restore_firmware_cache()

    @property
    def bios_files_index(self) -> dict:
        """Flat reverse index of BIOS files: {filename: entry_data}."""
        return self._bios_files_index

    # ── Registry loading ─────────────────────────────────────

    def load_bios_registry(self) -> None:
        self._bios_registry = {}
        self._bios_files_index = {}
        # Check plugin root first (Decky CLI moves defaults/ contents to root),
        # then defaults/ subdirectory (dev deploys via mise run deploy)
        root_path = os.path.join(self._plugin_dir, "bios_registry.json")
        defaults_path = os.path.join(self._plugin_dir, "defaults", "bios_registry.json")
        registry_path = root_path if os.path.exists(root_path) else defaults_path
        try:
            with open(registry_path) as f:
                self._bios_registry = json.load(f)
            # Build flat reverse index: {filename: {entry_data + "platform": slug}}
            for platform, files in self._bios_registry.get("platforms", {}).items():
                for filename, entry in files.items():
                    self._bios_files_index[filename] = {**entry, "platform": platform}
        except FileNotFoundError:
            self._logger.warning("bios_registry.json not found, registry enrichment disabled")
        except Exception as e:
            self._logger.error(f"Failed to load bios_registry.json: {e}")

    # ── Internal helpers ─────────────────────────────────────

    def _enrich_firmware_file(self, file_dict, core_so=None):
        entry = self._bios_files_index.get(file_dict.get("file_name", ""))
        if entry:
            # Use per-core required value if active core is known
            if core_so and "cores" in entry and core_so in entry["cores"]:
                is_required = entry["cores"][core_so]["required"]
            else:
                is_required = entry.get("required", True)
            file_dict["required"] = is_required
            file_dict["description"] = entry.get("description", file_dict.get("file_name", ""))
            file_dict["classification"] = "required" if is_required else "optional"
        else:
            # Unknown file: not in registry, don't count as required
            file_dict["required"] = False
            file_dict["description"] = file_dict.get("file_name", "")
            file_dict["classification"] = "unknown"
        file_md5 = file_dict.get("md5", "")
        registry_md5 = entry.get("md5", "") if entry else ""
        if file_md5 and registry_md5:
            file_dict["hash_valid"] = file_md5.lower() == registry_md5.lower()
        else:
            file_dict["hash_valid"] = None
        return file_dict

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
        """Determine local destination path for a firmware file.

        Uses firmware_path from bios_registry.json for correct subdirectory
        placement (e.g. dc/dc_boot.bin). Falls back to flat in bios root
        for files not in the registry.
        """
        bios_base = self._get_bios_path() if self._get_bios_path else ""
        file_name = firmware.get("file_name", "")
        reg_entry = self._bios_files_index.get(file_name)
        if reg_entry and reg_entry.get("firmware_path"):
            return os.path.join(bios_base, reg_entry["firmware_path"])
        return os.path.join(bios_base, file_name)

    # ── Firmware list cache ─────────────────────────────────

    def _restore_firmware_cache(self) -> None:
        """Load firmware cache from disk on init."""
        if not self._load_firmware_cache:
            return
        try:
            data = self._load_firmware_cache()
            if data and "items" in data and "cached_at" in data:
                self._firmware_cache = data["items"]
                self._firmware_cache_epoch = data["cached_at"]
                self._firmware_cache_at = time.monotonic()
                self._logger.info("Restored firmware cache from disk (%d items)", len(data["items"]))
        except Exception as e:
            self._logger.warning(f"Failed to load firmware cache from disk: {e}")

    def _persist_firmware_cache(self) -> None:
        """Write current firmware cache to disk."""
        if not self._save_firmware_cache or self._firmware_cache is None:
            return
        try:
            self._save_firmware_cache({"items": self._firmware_cache, "cached_at": self._firmware_cache_epoch})
        except Exception as e:
            self._logger.warning(f"Failed to persist firmware cache: {e}")

    def _get_firmware_list(self) -> list:
        """Return firmware list, using cache if TTL has not expired.

        On HTTP error, falls back to cached data (if any) or empty list.
        """
        now = time.monotonic()
        if self._firmware_cache is not None and (now - self._firmware_cache_at) < _FIRMWARE_CACHE_TTL:
            return self._firmware_cache

        try:
            result = self._romm_api.list_firmware()
            self._firmware_cache = result
            self._firmware_cache_at = time.monotonic()
            self._firmware_cache_epoch = time.time()
            self._persist_firmware_cache()
            return result
        except Exception as e:
            self._logger.warning(f"Failed to fetch firmware list: {e}")
            if self._firmware_cache is not None:
                return self._firmware_cache
            raise

    def invalidate_firmware_cache(self) -> None:
        """Clear cached firmware list so the next call re-fetches."""
        self._firmware_cache = None
        self._firmware_cache_at = 0
        self._firmware_cache_epoch = 0
        if self._save_firmware_cache:
            try:
                self._save_firmware_cache({})
            except Exception as e:
                self._logger.warning(f"Failed to clear persisted firmware cache: {e}")

    def check_platform_bios_cached(self, platform_slug, rom_filename=None):
        """Return BIOS status from in-memory cache only — no HTTP.

        Returns None if the firmware cache is empty (never fetched).
        Includes ``cached_at`` timestamp so the frontend can decide staleness.
        """
        if self._firmware_cache is None:
            return None

        fw_slugs = self._platform_to_firmware_slugs(platform_slug)
        active_core_so, active_core_label = es_de_config.get_active_core(platform_slug, rom_filename=rom_filename)

        registry_platform = {}
        for slug in fw_slugs:
            registry_platform.update(self._bios_registry.get("platforms", {}).get(slug, {}))

        items = [
            {
                "file_name": fw.get("file_name", ""),
                "downloaded": os.path.exists(self._firmware_dest_path(fw)),
                "dest": self._firmware_dest_path(fw),
            }
            for fw in self._firmware_cache
            if self._firmware_slug(fw.get("file_path", "")) in fw_slugs
        ]
        files = collect_firmware_status(items, registry_platform, active_core_so)

        if not files:
            return {"needs_bios": False, "cached_at": self._firmware_cache_epoch}

        server_count = len(files)
        local_count = sum(1 for f in files if f.downloaded)
        active_files = [f for f in files if f.used_by_active]
        required_files = [f for f in active_files if f.classification == "required"]

        return {
            "needs_bios": True,
            "server_count": server_count,
            "local_count": local_count,
            "all_downloaded": local_count >= server_count,
            "required_count": len(required_files),
            "required_downloaded": sum(1 for f in required_files if f.downloaded),
            "unknown_count": sum(1 for f in files if f.classification == "unknown"),
            "files": [asdict(f) for f in files],
            "active_core": active_core_so,
            "active_core_label": active_core_label,
            "available_cores": es_de_config.get_available_cores(platform_slug),
            "cached_at": self._firmware_cache_epoch,
        }

    # ── Public API ───────────────────────────────────────────

    def _group_server_firmware(self, firmware_list):
        """Group server firmware list by platform slug."""
        platforms_map = {}
        for fw in firmware_list:
            platform_slug = self._firmware_slug(fw.get("file_path", "")) or "unknown"
            if platform_slug not in platforms_map:
                platforms_map[platform_slug] = {"platform_slug": platform_slug, "files": []}
            dest = self._firmware_dest_path(fw)
            platforms_map[platform_slug]["files"].append(
                {
                    "id": fw.get("id"),
                    "file_name": fw.get("file_name", ""),
                    "size": fw.get("file_size_bytes", 0),
                    "md5": fw.get("md5_hash", ""),
                    "downloaded": os.path.exists(dest),
                }
            )
        return platforms_map

    def _group_registry_firmware(self):
        """Build platform map from bios registry (offline fallback)."""
        bios_base = self._get_bios_path() if self._get_bios_path else ""
        platforms_map = {}
        for reg_slug, reg_files in self._bios_registry.get("platforms", {}).items():
            if reg_slug not in platforms_map:
                platforms_map[reg_slug] = {"platform_slug": reg_slug, "files": []}
            for file_name, reg_entry in reg_files.items():
                firmware_path = reg_entry.get("firmware_path", file_name)
                dest = os.path.join(bios_base, firmware_path)
                platforms_map[reg_slug]["files"].append(
                    {
                        "id": None,
                        "file_name": file_name,
                        "size": 0,
                        "md5": reg_entry.get("md5", ""),
                        "downloaded": os.path.exists(dest),
                    }
                )
        return platforms_map

    def _enrich_platform_map(self, platforms_map):
        """Add core info and game-installed flags to each platform entry."""
        installed_slugs = {
            entry.get("platform_slug", "")
            for entry in self._state["shortcut_registry"].values()
            if entry.get("platform_slug")
        }
        for plat in platforms_map.values():
            slug = plat["platform_slug"]
            core_so, core_label = es_de_config.get_active_core(slug)
            plat["active_core"] = core_so
            plat["active_core_label"] = core_label
            plat["available_cores"] = es_de_config.get_available_cores(slug)
            for f in plat["files"]:
                self._enrich_firmware_file(f, core_so=core_so)
            plat["has_games"] = slug in installed_slugs
            plat["all_downloaded"] = all(f["downloaded"] for f in plat["files"])

    async def get_firmware_status(self):
        """Return BIOS/firmware status for all platforms on the RomM server.

        When the server is unreachable, falls back to registry-based status
        for installed platforms so core switching remains available offline.
        """
        server_offline = False
        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
            platforms_map = self._group_server_firmware(firmware_list)
        except Exception as e:
            self._logger.warning(f"Failed to fetch firmware from server: {e}")
            server_offline = True
            platforms_map = self._group_registry_firmware()

        self._enrich_platform_map(platforms_map)
        platforms = sorted(platforms_map.values(), key=lambda p: p["platform_slug"])
        return {"success": True, "server_offline": server_offline, "platforms": platforms}

    def _download_firmware_post_io(self, fw, firmware_id, dest, tmp_path):
        """Sync helper for download_firmware — rename, hash verification, state save in executor."""
        file_name = fw.get("file_name", "")
        os.replace(tmp_path, dest)

        # Verify MD5 if available
        md5_match = None
        expected_md5 = fw.get("md5_hash", "")
        local_md5 = None
        if expected_md5:
            h = hashlib.md5()
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            local_md5 = h.hexdigest()
            md5_match = local_md5 == expected_md5

        # Check against registry hash
        registry_hash_valid = None
        reg_entry = self._bios_files_index.get(file_name)
        if reg_entry:
            reg_md5 = reg_entry.get("md5", "")
            if reg_md5:
                if local_md5 is None:
                    h = hashlib.md5()
                    with open(dest, "rb") as f:
                        for chunk in iter(lambda: f.read(8192), b""):
                            h.update(chunk)
                    local_md5 = h.hexdigest()
                registry_hash_valid = local_md5.lower() == reg_md5.lower()

        # Track in state for migration support
        self._state["downloaded_bios"][file_name] = {
            "file_path": dest,
            "firmware_id": firmware_id,
            "platform_slug": self._firmware_slug(fw.get("file_path", "")),
            "downloaded_at": datetime.now(UTC).isoformat(),
        }
        self._save_state()

        return md5_match, registry_hash_valid

    async def download_firmware(self, firmware_id):
        """Download a single firmware file from RomM."""
        firmware_id = int(firmware_id)
        try:
            fw = await self._loop.run_in_executor(None, self._romm_api.get_firmware, firmware_id)
        except Exception as e:
            self._logger.error(f"Failed to fetch firmware {firmware_id}: {e}")
            return error_response(e)

        file_name = fw.get("file_name", "")
        dest = self._firmware_dest_path(fw)
        tmp_path = dest + ".tmp"

        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            await self._loop.run_in_executor(None, self._romm_api.download_firmware, firmware_id, file_name, tmp_path)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            self._logger.error(f"Failed to download firmware {file_name}: {e}")
            return error_response(e)

        md5_match, registry_hash_valid = await self._loop.run_in_executor(
            None, self._download_firmware_post_io, fw, firmware_id, dest, tmp_path
        )

        self.invalidate_firmware_cache()
        self._logger.info(f"Firmware downloaded: {file_name} -> {dest}")
        return {"success": True, "file_path": dest, "md5_match": md5_match, "registry_hash_valid": registry_hash_valid}

    async def download_all_firmware(self, platform_slug):
        """Download all firmware for a given platform slug."""
        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
        except Exception as e:
            self._logger.error(f"Failed to fetch firmware: {e}")
            resp = error_response(e)
            resp["downloaded"] = 0
            return resp

        # Filter by platform slug (use mapped slugs, e.g. "psx" -> ["psx", "ps"])
        fw_slugs = self._platform_to_firmware_slugs(platform_slug)
        platform_firmware = []
        for fw in firmware_list:
            slug = self._firmware_slug(fw.get("file_path", ""))
            if slug in fw_slugs:
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

    def _is_firmware_required(self, file_name, core_so):
        """Check if a firmware file is required for the given core."""
        index_entry = self._bios_files_index.get(file_name)
        if not index_entry:
            return None  # Unknown file
        if core_so and "cores" in index_entry and core_so in index_entry["cores"]:
            return index_entry["cores"][core_so]["required"]
        return index_entry.get("required", True)

    async def _download_firmware_batch(self, platform_firmware):
        """Download a batch of firmware files, skipping already-downloaded ones."""
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
        return downloaded, errors

    async def download_required_firmware(self, platform_slug):
        """Download only required firmware for a given platform slug."""
        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
        except Exception as e:
            self._logger.error(f"Failed to fetch firmware: {e}")
            resp = error_response(e)
            resp["downloaded"] = 0
            return resp

        fw_slugs = self._platform_to_firmware_slugs(platform_slug)
        core_so, _ = es_de_config.get_active_core(platform_slug)

        platform_firmware = [
            fw
            for fw in firmware_list
            if self._firmware_slug(fw.get("file_path", "")) in fw_slugs
            and self._is_firmware_required(fw.get("file_name", ""), core_so) is True
        ]

        downloaded, errors = await self._download_firmware_batch(platform_firmware)

        msg = f"Downloaded {downloaded} required firmware files"
        if errors:
            msg += f" ({len(errors)} failed: {', '.join(errors)})"
        return {"success": True, "message": msg, "downloaded": downloaded}

    async def check_platform_bios(self, platform_slug, rom_filename=None):
        """Check if RomM has firmware for this platform and whether it's downloaded."""
        fw_slugs = self._platform_to_firmware_slugs(platform_slug)
        active_core_so, active_core_label = es_de_config.get_active_core(platform_slug, rom_filename=rom_filename)

        # Build combined registry entries for this platform from all mapped slugs
        registry_platform = {}
        for slug in fw_slugs:
            registry_platform.update(self._bios_registry.get("platforms", {}).get(slug, {}))

        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
            items = [
                {
                    "file_name": fw.get("file_name", ""),
                    "downloaded": os.path.exists(self._firmware_dest_path(fw)),
                    "dest": self._firmware_dest_path(fw),
                }
                for fw in firmware_list
                if self._firmware_slug(fw.get("file_path", "")) in fw_slugs
            ]
            files = collect_firmware_status(items, registry_platform, active_core_so)
        except Exception:
            if not registry_platform:
                return {"needs_bios": False}
            bios_base = self._get_bios_path() if self._get_bios_path else ""
            registry_items = [
                {
                    "file_name": file_name,
                    "downloaded": os.path.exists(os.path.join(bios_base, reg_entry.get("firmware_path", file_name))),
                    "dest": os.path.join(bios_base, reg_entry.get("firmware_path", file_name)),
                }
                for file_name, reg_entry in registry_platform.items()
            ]
            files = collect_firmware_status(registry_items, registry_platform, active_core_so)

        if not files:
            return {"needs_bios": False}

        server_count = len(files)
        local_count = sum(1 for f in files if f.downloaded)

        # required_count/required_downloaded: only files used by the active core (for badge)
        active_files = [f for f in files if f.used_by_active]
        required_files = [f for f in active_files if f.classification == "required"]

        return {
            "needs_bios": True,
            "server_count": server_count,
            "local_count": local_count,
            "all_downloaded": local_count >= server_count,
            "required_count": len(required_files),
            "required_downloaded": sum(1 for f in required_files if f.downloaded),
            "unknown_count": sum(1 for f in files if f.classification == "unknown"),
            "files": [asdict(f) for f in files],
            "active_core": active_core_so,
            "active_core_label": active_core_label,
            "available_cores": es_de_config.get_available_cores(platform_slug),
        }

    def _delete_platform_bios_io(self, files):
        """Sync helper for delete_platform_bios — file deletions + state save in executor."""
        deleted = 0
        errors = []
        for f in files:
            if not f.downloaded:
                continue
            try:
                os.remove(f.local_path)
                deleted += 1
                # Remove from state tracking
                self._state["downloaded_bios"].pop(f.file_name, None)
            except Exception as e:
                errors.append(f"{f.file_name}: {e}")

        if deleted:
            self._save_state()

        return deleted, errors

    async def delete_platform_bios(self, platform_slug):
        """Delete locally downloaded BIOS files for a platform."""
        bios_status = await self.check_platform_bios(platform_slug)
        if not bios_status.get("needs_bios") or not bios_status.get("files"):
            return {"success": True, "deleted_count": 0, "message": "No BIOS files for this platform"}

        deleted, errors = await self._loop.run_in_executor(None, self._delete_platform_bios_io, bios_status["files"])
        self.invalidate_firmware_cache()

        if errors:
            return {
                "success": False,
                "deleted_count": deleted,
                "message": f"Deleted {deleted} file(s), {len(errors)} error(s)",
            }
        return {"success": True, "deleted_count": deleted, "message": f"Deleted {deleted} BIOS file(s)"}
