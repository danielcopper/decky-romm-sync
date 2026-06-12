"""FirmwareService — BIOS/firmware registry orchestration.

Owns the BIOS registry lifecycle and every status-bearing query the
QAM panel runs against it: presence checks per system or core,
deletion, and reads of cached server-side firmware metadata. Raw
filesystem I/O is delegated to the ``FirmwareFileStore`` Protocol and
HTTP traffic flows through ``RommFirmwareApi``; the registry shape,
caching policy, and per-core filtering logic remain this service's
responsibility.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from domain import firmware_paths
from domain.bios import collect_firmware_status, compute_bios_level, format_bios_status
from domain.bios_file import BiosFile
from domain.firmware_cache import FirmwareCacheEntry
from lib.errors import error_response
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        Clock,
        CoreInfoProvider,
        FirmwareFileStore,
        RetroDeckPaths,
        RommFirmwareApi,
        SystemResolver,
        UnitOfWorkFactory,
    )

_FIRMWARE_CACHE_TTL = 3600  # 1 hour


@dataclass(frozen=True)
class FirmwareServiceConfig:
    """Frozen wiring bundle handed to ``FirmwareService.__init__``.

    Holds the API adapter, runtime infrastructure, Protocol-typed file
    adapters, the SQLite Unit-of-Work factory, and the provider callables
    FirmwareService needs at construction time. Decomposes the ctor
    so a new dependency does not push past the S107 parameter-count
    limit.
    """

    romm_api: RommFirmwareApi
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    clock: Clock
    firmware_file_store: FirmwareFileStore
    retrodeck_paths: RetroDeckPaths
    core_info: CoreInfoProvider
    resolve_system: SystemResolver
    uow_factory: UnitOfWorkFactory


class FirmwareService:
    """BIOS/firmware management: registry, status, downloads, deletion."""

    def __init__(
        self,
        *,
        config: FirmwareServiceConfig,
    ) -> None:
        self._romm_api = config.romm_api
        self._loop = config.loop
        self._logger = config.logger
        self._plugin_dir = config.plugin_dir
        self._clock = config.clock
        self._firmware_file_store = config.firmware_file_store
        self._retrodeck_paths = config.retrodeck_paths
        self._core_info = config.core_info
        self._resolve_system = config.resolve_system
        self._uow_factory = config.uow_factory
        self._bios_registry: dict[str, Any] = {}
        self._bios_files_index: dict[str, dict[str, Any]] | None = None
        self._firmware_cache: list[dict[str, Any]] | None = None
        self._firmware_cache_epoch: float = 0
        self._restore_firmware_cache()

    @property
    def bios_files_index(self) -> dict[str, dict[str, Any]]:
        """Flat reverse index of BIOS files: {filename: entry_data}.

        Raises ``RuntimeError`` if accessed before ``load_bios_registry()`` —
        a silent empty dict at startup masks order-sensitive wiring bugs.
        """
        if self._bios_files_index is None:
            raise RuntimeError("firmware registry not loaded — call load_bios_registry() first")
        return self._bios_files_index

    # ── Registry loading ─────────────────────────────────────

    def load_bios_registry(self) -> None:
        self._bios_registry = {}
        self._bios_files_index = {}
        # Check plugin root first (Decky CLI moves defaults/ contents to root),
        # then defaults/ subdirectory (dev deploys via mise run deploy)
        root_path = os.path.join(self._plugin_dir, "bios_registry.json")
        defaults_path = os.path.join(self._plugin_dir, "defaults", "bios_registry.json")
        registry_path = root_path if self._firmware_file_store.exists(root_path) else defaults_path
        try:
            data = self._firmware_file_store.read_bytes(registry_path)
            self._bios_registry = json.loads(data)
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
        entry = self.bios_files_index.get(file_dict.get("file_name", ""))
        if entry:
            # Use per-core required value if active core is known
            if core_so and "cores" in entry and core_so in entry["cores"]:
                is_required = entry["cores"][core_so]["required"]
            else:
                is_required = entry.get("required", True)
            required = is_required
            description = entry.get("description", file_dict.get("file_name", ""))
            classification = "required" if is_required else "optional"
        else:
            # Unknown file: not in registry, don't count as required
            required = False
            description = file_dict.get("file_name", "")
            classification = "unknown"
        file_md5 = file_dict.get("md5", "")
        registry_md5 = entry.get("md5", "") if entry else ""
        hash_valid = file_md5.lower() == registry_md5.lower() if file_md5 and registry_md5 else None
        return {
            **file_dict,
            "required": required,
            "description": description,
            "classification": classification,
            "hash_valid": hash_valid,
        }

    def _firmware_dest_path(self, firmware):
        """Determine local destination path for a firmware file.

        Uses firmware_path from bios_registry.json for correct subdirectory
        placement (e.g. dc/dc_boot.bin). Falls back to flat in bios root
        for files not in the registry.
        """
        bios_base = self._retrodeck_paths.bios_path()
        file_name = firmware.get("file_name", "")
        reg_entry = self.bios_files_index.get(file_name)
        if reg_entry and reg_entry.get("firmware_path"):
            return os.path.join(bios_base, reg_entry["firmware_path"])
        return os.path.join(bios_base, file_name)

    # ── Firmware list cache ─────────────────────────────────

    def _restore_firmware_cache(self) -> None:
        """Rebuild the in-memory firmware cache from the SQLite cache table.

        The ``firmware_cache`` table is a thin record per ADR-0003 — it carries
        the already-parsed ``platform_slug`` and ``name`` but not the raw RomM
        ``file_path`` or ``md5_hash``. We synthesize a ``bios/<slug>/<name>``
        ``file_path`` that round-trips through ``parse_firmware_slug`` so the
        cache-only consumers (``check_platform_bios_cached``) keep working after
        a restart; ``md5_hash`` is intentionally absent (display-only).
        """
        try:
            with self._uow_factory() as uow:
                entries = list(uow.firmware_cache.iter_all())
                epoch = uow.firmware_cache.get_cache_epoch()
        except Exception as e:
            self._logger.warning(f"Failed to load firmware cache from DB: {e}")
            return

        if not entries or epoch is None:
            return

        self._firmware_cache = [self._entry_to_firmware_dict(entry) for entry in entries]
        self._firmware_cache_epoch = epoch
        self._logger.info("Restored firmware cache from DB (%d items)", len(entries))

    @staticmethod
    def _entry_to_firmware_dict(entry: FirmwareCacheEntry) -> dict[str, Any]:
        """Reconstruct an in-memory firmware dict from a thin cache aggregate."""
        return {
            "id": entry.id,
            "file_name": entry.name,
            "file_path": f"bios/{entry.platform_slug}/{entry.name}",
            "file_size_bytes": entry.file_size_bytes,
            "md5_hash": "",
        }

    def _persist_firmware_cache(self) -> None:
        """Replace the SQLite firmware cache with the current in-memory listing.

        Maps each raw RomM firmware dict to a thin ``FirmwareCacheEntry`` (slug
        pre-parsed from ``file_path``) and writes them wholesale.
        """
        if self._firmware_cache is None:
            return
        entries = [
            FirmwareCacheEntry.cached(
                id=fw.get("id"),
                name=fw.get("file_name", ""),
                platform_slug=firmware_paths.parse_firmware_slug(fw.get("file_path", "")),
                file_size_bytes=fw.get("file_size_bytes", 0),
                cached_at=self._firmware_cache_epoch,
            )
            for fw in self._firmware_cache
        ]
        try:
            with self._uow_factory() as uow:
                uow.firmware_cache.replace_all(entries)
        except Exception as e:
            self._logger.warning(f"Failed to persist firmware cache: {e}")

    def _get_firmware_list(self) -> list[dict[str, Any]]:
        """Return firmware list, using cache if TTL has not expired.

        TTL is checked against the wall-clock cache epoch so a cache
        restored from disk after a plugin restart still expires.
        On HTTP error, falls back to cached data (if any) or empty list.
        """
        now = self._clock.time()
        if self._firmware_cache is not None and (now - self._firmware_cache_epoch) < _FIRMWARE_CACHE_TTL:
            return self._firmware_cache

        try:
            result = self._romm_api.list_firmware()
            self._firmware_cache = result
            self._firmware_cache_epoch = self._clock.time()
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
        self._firmware_cache_epoch = 0
        try:
            with self._uow_factory() as uow:
                uow.firmware_cache.clear()
        except Exception as e:
            self._logger.warning(f"Failed to clear persisted firmware cache: {e}")

    def _resolve_bios_filter_core(self, system: str, active_core_so: str | None) -> str | None:
        """Return the ``.so`` to filter the firmware list against.

        A non-``None`` ``active_core_so`` is the pre-resolved per-game core (the
        game-detail path runs ``ActiveCoreReader`` upstream) and is used as-is.
        ``None`` is the platform-level callers' "use the system default" signal —
        resolved here via the system-layer ``get_active_core(system)``.
        """
        if active_core_so is not None:
            return active_core_so
        core_so, _ = self._core_info.get_active_core(system)
        return core_so

    def check_platform_bios_cached(self, platform_slug, active_core_so=None):
        """Return BIOS status from in-memory cache only — no HTTP.

        Returns None if the firmware cache is empty (never fetched).
        Includes ``cached_at`` timestamp so the frontend can decide staleness.
        ``active_core_so`` is the pre-resolved core that drives core-aware BIOS
        filtering — it is an INPUT to ``collect_firmware_status``, never served
        back to the UI. ``None`` means "use the system default" (resolved here
        via ``get_active_core(system)``); the per-game game-detail path passes
        the ROM's resolved ``.so`` directly. Core info (active core, available
        cores) reaches the frontend through the dedicated
        ``get_platform_core_info`` path, not this BIOS payload (#923).
        """
        if self._firmware_cache is None:
            return None

        system = self._resolve_system(platform_slug)
        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        active_core_so = self._resolve_bios_filter_core(system, active_core_so)

        registry_platform = {}
        for slug in fw_slugs:
            registry_platform.update(self._bios_registry.get("platforms", {}).get(slug, {}))

        items = [
            {
                "file_name": fw.get("file_name", ""),
                "downloaded": self._firmware_file_store.exists(self._firmware_dest_path(fw)),
                "dest": self._firmware_dest_path(fw),
            }
            for fw in self._firmware_cache
            if firmware_paths.parse_firmware_slug(fw.get("file_path", "")) in fw_slugs
        ]
        files = collect_firmware_status(items, registry_platform, active_core_so)

        if not files:
            return {
                "needs_bios": False,
                "cached_at": self._firmware_cache_epoch,
            }

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
            "cached_at": self._firmware_cache_epoch,
        }

    # ── Public API ───────────────────────────────────────────

    def _group_server_firmware(self, firmware_list):
        """Group server firmware list by platform slug."""
        platforms_map = {}
        for fw in firmware_list:
            platform_slug = firmware_paths.parse_firmware_slug(fw.get("file_path", "")) or "unknown"
            if platform_slug not in platforms_map:
                platforms_map[platform_slug] = {"platform_slug": platform_slug, "files": []}
            dest = self._firmware_dest_path(fw)
            platforms_map[platform_slug]["files"].append(
                {
                    "id": fw.get("id"),
                    "file_name": fw.get("file_name", ""),
                    "size": fw.get("file_size_bytes", 0),
                    "md5": fw.get("md5_hash", ""),
                    "downloaded": self._firmware_file_store.exists(dest),
                }
            )
        return platforms_map

    def _group_registry_firmware(self):
        """Build platform map from bios registry (offline fallback)."""
        bios_base = self._retrodeck_paths.bios_path()
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
                        "downloaded": self._firmware_file_store.exists(dest),
                    }
                )
        return platforms_map

    def _read_synced_slugs(self) -> set[str]:
        """Return platform slugs with at least one ROM bound to a Steam shortcut.

        A bound ROM (``shortcut_app_id`` set) is one that is currently in the
        synced library, covering both platform- and collection-sync. Deselected
        platforms get unbound on the next sync (ADR-0007) and drop out.
        """
        with self._uow_factory() as uow:
            return {
                rom.platform_slug
                for rom in uow.roms.iter_all()
                if rom.platform_slug and rom.shortcut_app_id is not None
            }

    def _enrich_platform_map(self, platforms_map, synced_slugs):
        """Add core info and game-installed flags to each platform entry.

        The core read seams key by the resolved RetroDECK ``system`` (ADR-0010
        §2), so each entry's raw RomM/BIOS-folder slug is normalized before the
        ``get_active_core`` / ``get_available_cores`` calls; ``has_games`` and the
        BIOS-folder file lookups stay on the raw slug (their own vocabulary).
        """
        for plat in platforms_map.values():
            slug = plat["platform_slug"]
            system = self._resolve_system(slug)
            core_so, core_label = self._core_info.get_active_core(system)
            plat["active_core"] = core_so
            plat["active_core_label"] = core_label
            plat["available_cores"] = self._core_info.get_available_cores(system)
            plat["files"] = [self._enrich_firmware_file(f, core_so=core_so) for f in plat["files"]]
            plat["has_games"] = slug in synced_slugs
            plat["all_downloaded"] = all(f["downloaded"] for f in plat["files"])
            self._set_platform_bios_aggregates(plat, slug)

    def _set_platform_bios_aggregates(self, plat: dict[str, Any], slug: str) -> None:
        """Stamp the per-platform BIOS aggregates onto a ``get_firmware_status`` entry.

        Adds ``server_count`` / ``local_count`` / ``required_count`` /
        ``required_downloaded`` and the ``bios_level`` trichotomy
        (``"ok"`` / ``"partial"`` / ``"missing"``) so the System page reads the
        ok/partial/missing decision and the display counts straight off this
        payload instead of re-deriving the threshold logic in the frontend. The
        classification comes from the already-core-aware enriched files, so the
        level matches the per-game game-detail path. Required-file counts key off
        ``classification == "required"`` (the enriched per-core required flag).
        """
        files = plat["files"]
        server_count = len(files)
        local_count = sum(1 for f in files if f["downloaded"])
        required_files = [f for f in files if f["classification"] == "required"]
        required_count = len(required_files)
        required_downloaded = sum(1 for f in required_files if f["downloaded"])

        bios_obj = format_bios_status(
            {
                "server_count": server_count,
                "local_count": local_count,
                "all_downloaded": local_count >= server_count,
                "required_count": required_count,
                "required_downloaded": required_downloaded,
            },
            slug,
        )
        plat["server_count"] = server_count
        plat["local_count"] = local_count
        plat["required_count"] = required_count
        plat["required_downloaded"] = required_downloaded
        plat["bios_level"] = compute_bios_level(bios_obj)

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

        synced_slugs = await self._loop.run_in_executor(None, self._read_synced_slugs)
        self._enrich_platform_map(platforms_map, synced_slugs)
        platforms = sorted(platforms_map.values(), key=lambda p: p["platform_slug"])
        return {"success": True, "server_offline": server_offline, "platforms": platforms}

    def _download_firmware_post_io(self, fw, firmware_id, dest, tmp_path):
        """Sync worker for download_firmware — file rename, hash verification, DB persist.

        Runs in an executor. The filesystem work (rename, checksum) happens
        outside any transaction; only the ``BiosFile`` upsert is wrapped in a
        short write UoW (ADR-0006).

        Returns ``(md5_match, registry_hash_valid, error)``. ``error`` is a
        string when the firmware is malformed — RomM data that fails the
        ``BiosFile`` invariants (empty slug/file_name) — in which case the
        renamed file is removed and nothing is persisted; otherwise ``None``.
        """
        file_name = fw.get("file_name", "")
        self._firmware_file_store.rename(tmp_path, dest)

        # Compute local MD5 once (used for both server-hash and registry-hash checks)
        expected_md5 = fw.get("md5_hash", "")
        reg_entry = self.bios_files_index.get(file_name)
        reg_md5 = reg_entry.get("md5", "") if reg_entry else ""

        local_md5 = self._firmware_file_store.checksum_md5(dest) if (expected_md5 or reg_md5) else None

        md5_match = local_md5 == expected_md5 if expected_md5 and local_md5 is not None else None
        registry_hash_valid = local_md5.lower() == reg_md5.lower() if reg_md5 and local_md5 is not None else None

        try:
            bios_file = BiosFile.mark_downloaded(
                platform_slug=firmware_paths.parse_firmware_slug(fw.get("file_path", "")),
                file_name=file_name,
                file_path=dest,
                downloaded_at=self._clock.now().isoformat(),
                firmware_id=firmware_id,
            )
        except ValueError as e:
            # Malformed RomM firmware (e.g. file_path with no parseable slug):
            # the aggregate's invariant rejects it. Drop the renamed file so we
            # don't leave it untracked, and signal a download failure.
            self._firmware_file_store.remove_file(dest)
            return md5_match, registry_hash_valid, f"Invalid firmware metadata: {e}"

        with self._uow_factory() as uow:
            uow.bios_files.save(bios_file)

        return md5_match, registry_hash_valid, None

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
            await self._loop.run_in_executor(None, self._firmware_file_store.make_dirs, os.path.dirname(dest))
            await self._loop.run_in_executor(None, self._romm_api.download_firmware, firmware_id, file_name, tmp_path)
        except Exception as e:
            await self._loop.run_in_executor(None, self._firmware_file_store.remove_file, tmp_path)
            self._logger.error(f"Failed to download firmware {file_name}: {e}")
            return error_response(e)

        md5_match, registry_hash_valid, post_io_error = await self._loop.run_in_executor(
            None, self._download_firmware_post_io, fw, firmware_id, dest, tmp_path
        )
        if post_io_error is not None:
            self._logger.error(f"Failed to persist firmware {file_name}: {post_io_error}")
            return error_response(ValueError(post_io_error))

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
        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        platform_firmware = []
        for fw in firmware_list:
            slug = firmware_paths.parse_firmware_slug(fw.get("file_path", ""))
            if slug in fw_slugs:
                platform_firmware.append(fw)

        downloaded = 0
        errors = []
        for fw in platform_firmware:
            dest = self._firmware_dest_path(fw)
            if self._firmware_file_store.exists(dest):
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
        index_entry = self.bios_files_index.get(file_name)
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
            if self._firmware_file_store.exists(dest):
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

        system = self._resolve_system(platform_slug)
        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        core_so, _ = self._core_info.get_active_core(system)

        platform_firmware = [
            fw
            for fw in firmware_list
            if firmware_paths.parse_firmware_slug(fw.get("file_path", "")) in fw_slugs
            and self._is_firmware_required(fw.get("file_name", ""), core_so) is True
        ]

        downloaded, errors = await self._download_firmware_batch(platform_firmware)

        msg = f"Downloaded {downloaded} required firmware files"
        if errors:
            msg += f" ({len(errors)} failed: {', '.join(errors)})"
        return {"success": True, "message": msg, "downloaded": downloaded}

    async def check_platform_bios(self, platform_slug, active_core_so=None):
        """Check if RomM has firmware for this platform and whether it's downloaded.

        Returns BIOS status only. ``active_core_so`` is the pre-resolved core used
        to filter the firmware list by what THIS core needs (an INPUT to
        ``collect_firmware_status`` so ``required_count`` / the missing-BIOS badge
        stay core-aware); it is never served back to the UI. ``None`` means "use
        the system default" (resolved here via ``get_active_core(system)``); the
        per-game game-detail path passes the ROM's resolved ``.so``. Core info
        reaches the frontend through the dedicated ``get_platform_core_info``
        path, not this payload (#923).
        """
        system = self._resolve_system(platform_slug)
        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        active_core_so = self._resolve_bios_filter_core(system, active_core_so)

        # Build combined registry entries for this platform from all mapped slugs
        registry_platform = {}
        for slug in fw_slugs:
            registry_platform.update(self._bios_registry.get("platforms", {}).get(slug, {}))

        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
            items = [
                {
                    "file_name": fw.get("file_name", ""),
                    "downloaded": self._firmware_file_store.exists(self._firmware_dest_path(fw)),
                    "dest": self._firmware_dest_path(fw),
                }
                for fw in firmware_list
                if firmware_paths.parse_firmware_slug(fw.get("file_path", "")) in fw_slugs
            ]
            files = collect_firmware_status(items, registry_platform, active_core_so)
        except Exception:
            if not registry_platform:
                return {
                    "needs_bios": False,
                }
            bios_base = self._retrodeck_paths.bios_path()
            registry_items = [
                {
                    "file_name": file_name,
                    "downloaded": self._firmware_file_store.exists(
                        os.path.join(bios_base, reg_entry.get("firmware_path", file_name))
                    ),
                    "dest": os.path.join(bios_base, reg_entry.get("firmware_path", file_name)),
                }
                for file_name, reg_entry in registry_platform.items()
            ]
            files = collect_firmware_status(registry_items, registry_platform, active_core_so)

        if not files:
            return {
                "needs_bios": False,
            }

        server_count = len(files)
        local_count = sum(1 for f in files if f.downloaded)

        # required_count/required_downloaded: only files used by the active core (for badge)
        active_files = [f for f in files if f.used_by_active]
        required_files = [f for f in active_files if f.classification == "required"]

        result = {
            "needs_bios": True,
            "server_count": server_count,
            "local_count": local_count,
            "all_downloaded": local_count >= server_count,
            "required_count": len(required_files),
            "required_downloaded": sum(1 for f in required_files if f.downloaded),
            "unknown_count": sum(1 for f in files if f.classification == "unknown"),
            "files": [asdict(f) for f in files],
        }
        # bios_level trichotomy ("ok" / "partial" / "missing") so the frontend reads
        # the classification straight off this payload instead of re-deriving the
        # threshold logic. Matches the System page and per-game game-detail paths.
        result["bios_level"] = compute_bios_level(format_bios_status(result, platform_slug))
        return result

    def _delete_platform_bios_io(self, platform_slug, files):
        """Sync worker for delete_platform_bios — file deletions then DB prune.

        Runs in an executor. Every filesystem removal happens outside any
        transaction; only the ``BiosFile`` deletes for the files that were
        actually removed are wrapped in a single short write UoW (ADR-0006).
        """
        deleted = 0
        errors = []
        removed_names: list[str] = []
        for f in files:
            if not f["downloaded"]:
                continue
            try:
                self._firmware_file_store.remove_file(f["local_path"])
            except OSError as e:
                self._logger.warning(f"Failed to remove BIOS file {f['file_name']}: {e}")
                errors.append(f"{f['file_name']}: {e}")
                continue
            deleted += 1
            removed_names.append(f["file_name"])

        if removed_names:
            self._prune_bios_records(platform_slug, removed_names)

        return deleted, errors

    def _prune_bios_records(self, platform_slug, file_names):
        """Delete the ``BiosFile`` records for *file_names* on *platform_slug*.

        The BIOS rows are keyed by the firmware-directory slug stored at download
        time, which may differ from the platform slug (e.g. ``psx`` → ``ps``), so
        we resolve the candidate firmware slugs and match each removed filename
        against the records under them. One short write UoW.
        """
        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        wanted = set(file_names)
        with self._uow_factory() as uow:
            keys = [
                (record.platform_slug, record.file_name)
                for slug in fw_slugs
                for record in uow.bios_files.iter_by_platform(slug)
                if record.file_name in wanted
            ]
            for slug, file_name in keys:
                uow.bios_files.delete(slug, file_name)

    async def delete_platform_bios(self, platform_slug):
        """Delete locally downloaded BIOS files for a platform."""
        bios_status = await self.check_platform_bios(platform_slug)
        if not bios_status.get("needs_bios") or not bios_status.get("files"):
            return {"success": True, "deleted_count": 0, "message": "No BIOS files for this platform"}

        deleted, errors = await self._loop.run_in_executor(
            None, self._delete_platform_bios_io, platform_slug, bios_status["files"]
        )
        self.invalidate_firmware_cache()

        if errors:
            return {
                "success": False,
                "reason": ErrorCode.UNKNOWN.value,
                "deleted_count": deleted,
                "message": f"Deleted {deleted} file(s), {len(errors)} error(s)",
            }
        return {"success": True, "deleted_count": deleted, "message": f"Deleted {deleted} BIOS file(s)"}
