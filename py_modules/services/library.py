"""LibraryService — library sync engine.

Handles platform/ROM fetching, shortcut data preparation,
delta preview/apply, and shortcut registry management.

Artwork operations are delegated to ArtworkService via callbacks.
Shortcut removal is delegated to ShortcutRemovalService.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from domain.shortcut_data import build_registry_entry, build_shortcuts_data
from domain.sync_state import SyncState
from lib.errors import RommUnsupportedError, classify_error
from lib.perf import AdaptiveSemaphore, ETAEstimator, PerfCollector

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        ArtworkManager,
        DebugLogger,
        EventEmitter,
        MetadataExtractor,
        RommApiProtocol,
        SettingsPersister,
        StatePersister,
        SteamConfigAdapter,
    )


_SYNC_CANCELLED = "Sync cancelled"


class LibraryService:
    """Sync engine: fetch ROMs, prepare shortcuts, manage registry."""

    def __init__(
        self,
        *,
        romm_api: RommApiProtocol,
        steam_config: SteamConfigAdapter,
        state: dict,
        settings: dict,
        metadata_cache: dict,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        plugin_dir: str,
        emit: EventEmitter,
        save_state: StatePersister,
        save_settings_to_disk: SettingsPersister,
        log_debug: DebugLogger,
        metadata_service: MetadataExtractor | None = None,
        artwork: ArtworkManager | None = None,
    ) -> None:
        self._romm_api = romm_api
        self._steam_config = steam_config
        self._state = state
        self._settings = settings
        self._metadata_cache = metadata_cache
        self._loop = loop
        self._logger = logger
        self._plugin_dir = plugin_dir
        self._emit = emit
        self._save_state = save_state
        self._save_settings_to_disk = save_settings_to_disk
        self._log_debug = log_debug
        self._metadata_service = metadata_service
        self._artwork = artwork

        # Performance instrumentation
        self._perf = PerfCollector()
        self._eta = ETAEstimator(alpha=0.3, min_samples=5)

        # Collection list cache (avoids redundant HTTP fetches)
        self._collections_cache: tuple[list[dict], list[dict]] | None = None
        self._collections_cache_time: float = 0.0
        self._COLLECTIONS_CACHE_TTL: float = 300.0  # 5 minutes

        # Concurrent fetch settings
        self._FETCH_CONCURRENCY: int = 4  # max parallel platform fetches
        self._PAGE_SIZE: int = 250  # ROMs per API page (up from 50)

        # Sync-specific state (owned by this service)
        self._sync_state = SyncState.IDLE
        self._sync_last_heartbeat = 0.0
        self._sync_progress: dict = {
            "running": False,
            "phase": "",
            "current": 0,
            "total": 0,
            "message": "",
        }
        self._pending_sync: dict = {}
        self._pending_delta: dict | None = None
        self._pending_collection_memberships: dict = {}
        self._pending_platform_rom_ids: set[int] | None = None

    @property
    def sync_state(self) -> SyncState:
        """Current sync state (read-only)."""
        return self._sync_state

    @property
    def pending_sync(self) -> dict:
        """Public accessor for pending sync data (used by SteamGridService)."""
        return self._pending_sync

    def shutdown(self) -> None:
        """Request graceful shutdown — cancels sync if running."""
        if self._sync_state == SyncState.RUNNING:
            self._sync_state = SyncState.CANCELLING

    # ── Platform & ROM fetching ──────────────────────────────

    async def get_platforms(self):
        try:
            platforms = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        except Exception as e:
            self._logger.error(f"Failed to fetch platforms: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}

        if not isinstance(platforms, list):
            self._logger.error(f"Unexpected platforms response type: {type(platforms).__name__}")
            return {"success": False, "message": "Invalid server response", "error_code": "api_error"}

        enabled = self._settings.get("enabled_platforms", {})
        result = []
        for p in platforms:
            rom_count = p.get("rom_count", 0)
            if rom_count == 0:
                continue
            pid = str(p["id"])
            result.append(
                {
                    "id": p["id"],
                    "name": p.get("name", ""),
                    "slug": p.get("slug", ""),
                    "rom_count": rom_count,
                    "sync_enabled": enabled.get(pid, len(enabled) == 0),
                }
            )
        return {"success": True, "platforms": result}

    def save_platform_sync(self, platform_id, enabled):
        pid = str(platform_id)
        self._settings["enabled_platforms"][pid] = bool(enabled)
        self._save_settings_to_disk()
        return {"success": True}

    # ── Collection list caching ──────────────────────────────

    async def _get_collections_cached(self) -> tuple[list[dict], list[dict]]:
        """Return (user_collections, franchise_collections) with TTL cache.

        The first call fetches both lists from the RomM API in parallel
        using ``asyncio.gather``.  Subsequent calls within the TTL window
        return the cached result without any HTTP traffic.

        Raises ``RommUnsupportedError`` if collections aren't available.
        """
        now = time.monotonic()
        if self._collections_cache is not None and (now - self._collections_cache_time) < self._COLLECTIONS_CACHE_TTL:
            return self._collections_cache

        async def _fetch_user() -> list[dict]:
            return await self._loop.run_in_executor(None, self._romm_api.list_collections)

        async def _fetch_franchise() -> list[dict]:
            try:
                return await self._loop.run_in_executor(
                    None, self._romm_api.list_virtual_collections, "franchise"
                )
            except Exception as e:
                self._logger.warning(f"Failed to fetch franchise collections, continuing without them: {e}")
                return []

        user_collections, franchise_collections = await asyncio.gather(
            _fetch_user(), _fetch_franchise()
        )

        self._collections_cache = (user_collections, franchise_collections)
        self._collections_cache_time = time.monotonic()
        return self._collections_cache

    def _invalidate_collections_cache(self) -> None:
        """Clear the collection list cache (call at sync start)."""
        self._collections_cache = None
        self._collections_cache_time = 0.0

    async def get_collections(self):
        try:
            user_collections, franchise_collections = await self._get_collections_cached()
        except RommUnsupportedError:
            return {
                "success": False,
                "message": "Collections require RomM 4.7.0 or newer",
                "error_code": "unsupported_error",
            }
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}

        enabled = self._settings.get("enabled_collections", {})
        result = []
        for c in user_collections:
            cid = str(c["id"])
            result.append(
                {
                    "id": cid,
                    "name": c.get("name", ""),
                    "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                    "sync_enabled": enabled.get(cid, False),
                    "category": "favorites" if c.get("is_favorite") else "user",
                }
            )
        for c in franchise_collections:
            cid = str(c["id"])
            result.append(
                {
                    "id": cid,
                    "name": c.get("name", ""),
                    "rom_count": c.get("rom_count", len(c.get("rom_ids", []))),
                    "sync_enabled": enabled.get(cid, False),
                    "category": "franchise",
                }
            )

        _category_order = {"favorites": 0, "user": 1, "franchise": 2}
        result.sort(key=lambda x: (_category_order.get(x["category"], 99), x["name"].lower()))
        return {"success": True, "collections": result}

    def save_collection_sync(self, collection_id, enabled):
        self._settings.setdefault("enabled_collections", {})[str(collection_id)] = bool(enabled)
        self._save_settings_to_disk()
        return {"success": True}

    async def set_all_collections_sync(self, enabled, category=None):
        enabled = bool(enabled)
        try:
            user_collections, franchise_collections = await self._get_collections_cached()
        except RommUnsupportedError:
            return {
                "success": False,
                "message": "Collections require RomM 4.7.0 or newer",
                "error_code": "unsupported_error",
            }
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}

        all_collections = []
        for c in user_collections:
            cat = "favorites" if c.get("is_favorite") else "user"
            all_collections.append((str(c["id"]), cat))
        for c in franchise_collections:
            all_collections.append((str(c["id"]), "franchise"))

        ec = self._settings.setdefault("enabled_collections", {})
        for cid, cat in all_collections:
            if category is None or cat == category:
                ec[cid] = enabled
        self._save_settings_to_disk()
        return {"success": True}

    async def set_all_platforms_sync(self, enabled):
        enabled = bool(enabled)
        try:
            platforms = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        except Exception as e:
            self._logger.error(f"Failed to fetch platforms: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}

        ep = {}
        for p in platforms:
            ep[str(p["id"])] = enabled
        self._settings["enabled_platforms"] = ep
        self._save_settings_to_disk()
        return {"success": True}

    # ── Sync control ─────────────────────────────────────────

    def start_sync(self):
        if self._sync_state != SyncState.IDLE:
            return {"success": False, "message": "Sync already in progress"}
        self._sync_state = SyncState.RUNNING
        self._sync_last_heartbeat = time.monotonic()
        self._loop.create_task(self._do_sync())
        return {"success": True, "message": "Sync started"}

    def cancel_sync(self):
        if self._sync_state != SyncState.RUNNING:
            return {"success": True, "message": "No sync in progress"}
        self._sync_state = SyncState.CANCELLING
        return {"success": True, "message": "Sync cancelling..."}

    def get_sync_progress(self):
        return self._sync_progress

    def sync_heartbeat(self):
        """Called by frontend during shortcut application to keep safety timeout alive."""
        self._sync_last_heartbeat = time.monotonic()
        return {"success": True}

    # ── Preview / Apply ──────────────────────────────────────

    async def sync_preview(self):
        if self._sync_state != SyncState.IDLE:
            return {"success": False, "message": "Sync already in progress"}
        self._sync_state = SyncState.RUNNING
        self._sync_last_heartbeat = time.monotonic()
        try:
            fetch_result = await self._fetch_and_prepare()
            all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids = fetch_result
            platform_names = {p.get("name") for p in platforms}
            new, changed, unchanged_ids, stale, disabled_count = self._classify_roms(shortcuts_data, platform_names)

            # Build rom lookup for artwork download during apply
            roms_by_id = {r["id"]: r for r in all_roms}
            delta_rom_ids = {sd["rom_id"] for sd in new + changed}
            delta_roms = [roms_by_id[rid] for rid in delta_rom_ids if rid in roms_by_id]

            preview_id = str(uuid.uuid4())
            self._pending_delta = {
                "preview_id": preview_id,
                "new": new,
                "changed": changed,
                "unchanged_ids": unchanged_ids,
                "remove_rom_ids": stale,
                "all_shortcuts": {sd["rom_id"]: sd for sd in shortcuts_data},
                "delta_roms": delta_roms,
                "platforms_count": len(platforms),
                "total_roms": len(all_roms),
                "collection_memberships": collection_memberships,
                "platform_rom_ids": platform_rom_ids,
            }

            await self._emit_progress("done", message="Preview ready", running=False)

            return {
                "success": True,
                "summary": {
                    "new_count": len(new),
                    "changed_count": len(changed),
                    "unchanged_count": len(unchanged_ids),
                    "remove_count": len(stale),
                    "disabled_platform_remove_count": disabled_count,
                    "collection_diff": self._compute_collection_diff(collection_memberships),
                    "platform_collection_diff": self._compute_platform_collection_diff(
                        shortcuts_data, platform_rom_ids
                    ),
                },
                "new_names": [s["name"] for s in new[:10]],
                "changed_names": [s["name"] for s in changed[:10]],
                "preview_id": preview_id,
            }
        except asyncio.CancelledError:
            await self._finish_sync(_SYNC_CANCELLED)
            raise
        except Exception as e:
            import traceback

            self._logger.error(f"Sync preview failed: {e}\n{traceback.format_exc()}")
            _code, _msg = classify_error(e)
            await self._emit_progress("error", message=_msg, running=False)
            return {"success": False, "message": _msg, "error_code": _code}
        finally:
            self._sync_state = SyncState.IDLE

    async def sync_apply_delta(self, preview_id):
        if not self._pending_delta or self._pending_delta["preview_id"] != preview_id:
            return {"success": False, "message": "Preview expired, please re-sync", "error_code": "stale_preview"}
        delta = self._pending_delta
        self._pending_delta = None
        self._sync_state = SyncState.RUNNING
        self._sync_last_heartbeat = time.monotonic()

        # ── Apply phase ───────────────────────────────────────
        # Populate _pending_sync for report_sync_results and download_and_get_artwork
        self._pending_sync = delta["all_shortcuts"]
        self._pending_collection_memberships = delta.get("collection_memberships", {})
        self._pending_platform_rom_ids = delta.get("platform_rom_ids", set())

        # Update sync_stats
        self._state["sync_stats"] = {
            "platforms": delta["platforms_count"],
            "roms": delta["total_roms"],
        }
        self._save_state()

        total_changes = len(delta["new"]) + len(delta["changed"]) + len(delta["remove_rom_ids"])
        self._eta.start()
        await self._emit_progress(
            "applying",
            total=total_changes,
            message=f"Adding shortcuts 0/{total_changes}",
        )

        # ── Emit sync_plan so the frontend can render the accordion ──
        from collections import Counter
        all_shortcuts = delta["new"] + (delta["changed"] or [])
        platform_counts = Counter(sd.get("platform_name", "Unknown") for sd in all_shortcuts)
        plan_platforms = []
        for pname in sorted(platform_counts):
            slug = ""
            for sd in all_shortcuts:
                if sd.get("platform_name") == pname:
                    slug = sd.get("platform_slug", "")
                    break
            plan_platforms.append({"name": pname, "slug": slug, "rom_count": platform_counts[pname]})
        has_collections = bool(self._pending_collection_memberships)
        await self._emit("sync_plan", {
            "platforms": plan_platforms,
            "has_collections": has_collections,
            "estimated_total_roms": sum(platform_counts.values()),
        })

        # Emit per-platform events for delta apply
        await self._emit_per_platform_delta(
            new_shortcuts=delta["new"],
            changed_shortcuts=delta["changed"],
            stale_rom_ids=delta["remove_rom_ids"],
            collection_memberships=self._pending_collection_memberships,
        )

        # Heartbeat safety timeout
        self._start_safety_timeout()

        return {"success": True, "message": "Applying changes"}

    def sync_cancel_preview(self):
        self._pending_delta = None
        return {"success": True}

    # ── Progress & safety ────────────────────────────────────

    async def _emit_progress(
        self,
        phase,
        current=0,
        total=0,
        message="",
        running=True,
        *,
        sub_phase="",
        sub_message="",
        platforms_fetched=0,
        platforms_total=0,
    ):
        """Update _sync_progress and emit sync_progress event to frontend.

        Automatically enriches the payload with ETA/elapsed/speed data from
        ``self._eta`` when the estimator has been started and has enough samples.

        If *current* and *total* are both > 0 the ETA estimator is updated
        automatically — callers do **not** need to call ``_eta.update()``
        manually (though they still may for phase-level granularity).
        """
        if current > 0 and total > 0:
            self._eta.update(current)

        eta_sec = self._eta.eta_seconds(current, total) if total > 0 else None
        elapsed_sec = self._eta.elapsed
        ips = self._eta.items_per_sec

        self._sync_progress = {
            "running": running,
            "phase": phase,
            "current": current,
            "total": total,
            "message": message,
            # Enhanced fields — always present (frontend ignores 0/None gracefully)
            "elapsedSec": round(elapsed_sec, 1) if elapsed_sec else 0,
            "etaSec": round(eta_sec, 1) if eta_sec else None,
            "itemsPerSec": round(ips, 2) if ips else None,
            "subPhase": sub_phase,
            "subMessage": sub_message,
            # Fetch-phase fields
            "platformsFetched": platforms_fetched,
            "platformsTotal": platforms_total,
        }
        await self._emit("sync_progress", self._sync_progress)

    def _start_safety_timeout(self, heartbeat_timeout_sec=30):
        """Launch a background task that auto-completes sync if no heartbeat arrives."""
        self._sync_last_heartbeat = time.monotonic()

        async def _safety_timeout():
            while self._sync_progress.get("running"):
                await asyncio.sleep(10)
                elapsed = time.monotonic() - self._sync_last_heartbeat
                if elapsed > heartbeat_timeout_sec:
                    self._logger.warning(f"Sync safety timeout: no heartbeat for {elapsed:.0f}s")
                    stats = self._state.get("sync_stats", {})
                    await self._emit_progress(
                        "done",
                        current=stats.get("roms", 0),
                        total=stats.get("roms", 0),
                        message=(
                            f"Sync complete: {stats.get('roms', 0)} games from {stats.get('platforms', 0)} platforms"
                        ),
                        running=False,
                    )
                    self._sync_state = SyncState.IDLE
                    return

        self._loop.create_task(_safety_timeout())

    # ── Classification ───────────────────────────────────────

    def _classify_roms(self, shortcuts_data, fetched_platform_names):
        """Classify each ROM as new/changed/unchanged/stale."""
        registry = self._state["shortcut_registry"]
        new, changed, unchanged_ids = [], [], []

        for sd in shortcuts_data:
            reg = registry.get(str(sd["rom_id"]))
            if not reg or not reg.get("app_id"):
                new.append(sd)
            elif (
                reg.get("name") != sd["name"]
                or reg.get("platform_name") != sd.get("platform_name")
                or reg.get("platform_slug") != sd.get("platform_slug")
                or reg.get("fs_name") != sd.get("fs_name", "")
            ):
                sd["existing_app_id"] = reg["app_id"]
                changed.append(sd)
            else:
                unchanged_ids.append(sd["rom_id"])

        # Stale: in registry but not in fetched set
        current_ids = {sd["rom_id"] for sd in shortcuts_data}
        stale = [int(rid) for rid in registry if int(rid) not in current_ids]

        # If removal guard is active, suppress stale list entirely
        if not self._settings.get("remove_on_unsync", True):
            disabled_count = sum(
                1 for rid in stale if registry.get(str(rid), {}).get("platform_name") not in fetched_platform_names
            )
            return new, changed, unchanged_ids, [], disabled_count

        # Classify stale by disabled platform
        disabled_count = sum(
            1 for rid in stale if registry.get(str(rid), {}).get("platform_name") not in fetched_platform_names
        )

        return new, changed, unchanged_ids, stale, disabled_count

    def _compute_collection_diff(self, collection_memberships: dict[str, list[int]]) -> dict:
        """Compare current enabled collections against last synced state."""
        current = set(collection_memberships.keys())
        previous = set(self._state.get("last_synced_collections", []))
        added = sorted(current - previous)
        removed = sorted(previous - current)
        return {
            "has_changes": bool(added or removed or current),
            "added": added,
            "removed": removed,
        }

    def _compute_platform_collection_diff(self, shortcuts_data: list[dict], platform_rom_ids: set[int]) -> dict:
        """Compare future platform collections against current registry.

        Respects the collection_create_platform_groups toggle — if OFF,
        only platforms from platform-fetched ROMs get collections.
        """
        # Future: platforms that will have collections after sync
        future_platforms: set[str] = set()
        for sd in shortcuts_data:
            rid = sd["rom_id"]
            if self._should_include_in_platform_collection(rid, platform_rom_ids):
                pname = sd.get("platform_name", "")
                if pname:
                    future_platforms.add(pname)

        # Current: platforms that had collections at last sync
        current_platforms = set(self._state.get("last_synced_platforms", []))

        added = sorted(future_platforms - current_platforms)
        removed = sorted(current_platforms - future_platforms)
        return {
            "has_changes": bool(added or removed),
            "added_count": len(added),
            "removed_count": len(removed),
        }

    def _should_include_in_platform_collection(self, rom_id: int, platform_rom_ids: set[int] | None) -> bool:
        """Check if a ROM should appear in platform collections.

        If collection_create_platform_groups is False (default), only ROMs
        fetched via enabled platforms are included. Collection-only ROMs
        are excluded from platform collections but still synced.

        platform_rom_ids=None means no tracking data (legacy sync) → include all.
        platform_rom_ids=set() means no platforms enabled → exclude all (unless toggle ON).
        """
        if self._settings.get("collection_create_platform_groups", False):
            return True
        if platform_rom_ids is None:
            return True  # Legacy sync without platform tracking
        return rom_id in platform_rom_ids

    # ── Fetch & prepare ──────────────────────────────────────

    async def _fetch_enabled_platforms(self):
        """Fetch and filter platforms by enabled_platforms setting."""
        platforms = await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        if not isinstance(platforms, list):
            self._logger.error(f"Unexpected platforms response type: {type(platforms).__name__}")
            return []

        enabled = self._settings.get("enabled_platforms", {})
        no_prefs = len(enabled) == 0
        self._logger.info(f"Platform filter: {len(enabled)} prefs saved, no_prefs={no_prefs}")
        self._logger.info(f"Enabled platforms: {[k for k, v in enabled.items() if v]}")
        platforms = [p for p in platforms if enabled.get(str(p["id"]), no_prefs)]
        self._logger.info(f"Syncing {len(platforms)} platforms: {[p['name'] for p in platforms]}")
        return platforms

    def _reconstruct_platform_from_registry(self, registry, platform_name, platform_slug):
        """Reconstruct ROM list from registry for an unchanged platform."""
        return [
            {
                "id": int(rid),
                "name": entry["name"],
                "fs_name": entry.get("fs_name", ""),
                "platform_name": platform_name,
                "platform_slug": platform_slug,
                "platform_display_name": platform_name,
                "igdb_id": entry.get("igdb_id"),
                "sgdb_id": entry.get("sgdb_id"),
                "ra_id": entry.get("ra_id"),
            }
            for rid, entry in registry.items()
            if entry.get("platform_name") == platform_name
        ]

    async def _try_incremental_skip(
        self, platform, registry, last_sync, platform_name, platform_slug, all_roms, pi, total_platforms
    ):
        """Try incremental fetch; return True if platform was skipped (unchanged)."""
        registry_count = sum(1 for e in registry.values() if e.get("platform_name") == platform_name)
        if not last_sync or registry_count == 0:
            return False

        try:
            delta_resp = await self._loop.run_in_executor(
                None,
                self._romm_api.list_roms_updated_after,
                platform["id"],
                last_sync,
                1,
                0,
            )
            server_total = delta_resp.get("total", 0) if isinstance(delta_resp, dict) else 0
            platform_total = platform.get("rom_count", 0)

            if server_total == 0 and platform_total == registry_count:
                self._logger.info(f"Skipping {platform_name}: {registry_count} ROMs unchanged")
                all_roms.extend(self._reconstruct_platform_from_registry(registry, platform_name, platform_slug))
                await self._emit_progress(
                    "roms",
                    current=len(all_roms),
                    message=f"{platform_name} unchanged ({pi}/{total_platforms})",
                )
                return True

            self._logger.info(
                f"{platform_name}: {server_total} updated, "
                f"server={platform_total} vs registry={registry_count} — full fetch"
            )
        except Exception as e:
            self._logger.warning(f"Incremental check failed for {platform_name}, falling back to full fetch: {e}")
        return False

    async def _full_fetch_platform_roms(self, platform_id, platform_name, platform_slug, all_roms, pi, total_platforms):
        """Full paginated fetch of ROMs for a single platform.

        Returns the fetched ROM list.  Also appends to *all_roms* in-place for
        backward-compatible callers that still pass a shared accumulator.
        """
        offset = 0
        limit = self._PAGE_SIZE
        platform_roms: list[dict] = []
        await self._emit_progress(
            "roms",
            current=len(all_roms),
            message=f"Fetching {platform_name}... {len(all_roms)} found ({pi}/{total_platforms})",
            sub_phase=f"platform:{platform_name}",
        )

        while True:
            self._check_cancelling()
            try:
                roms = await self._loop.run_in_executor(
                    None,
                    self._romm_api.list_roms,
                    platform_id,
                    limit,
                    offset,
                )
            except Exception as e:
                self._logger.error(f"Failed to fetch ROMs for platform {platform_name}: {e}")
                break

            rom_list = roms.get("items", []) if isinstance(roms, dict) else roms
            for rom in rom_list:
                rom.pop("files", None)
                rom["platform_name"] = platform_name
                rom["platform_slug"] = platform_slug

            platform_roms.extend(rom_list)
            all_roms.extend(rom_list)
            await self._emit_progress(
                "roms",
                current=len(all_roms),
                message=f"Fetching {platform_name}... {len(all_roms)} found ({pi}/{total_platforms})",
                sub_phase=f"platform:{platform_name}",
            )
            if len(rom_list) < limit:
                break
            offset += limit

        return platform_roms

    async def _fetch_one_platform(
        self,
        platform: dict,
        registry: dict,
        last_sync: str | None,
        sem: AdaptiveSemaphore,
        progress: dict,
    ) -> list[dict]:
        """Fetch ROMs for a single platform (concurrent-safe).

        Uses *sem* to bound concurrency.  Updates *progress* dict
        (shared mutable counter) for live progress reporting.
        Returns the platform's ROM list.
        """
        async with sem:
            t0 = time.monotonic()
            self._check_cancelling()
            platform_name = platform.get("name", platform.get("display_name", "Unknown"))
            platform_slug = platform.get("slug", "")

            # Notify frontend this platform is being fetched
            await self._emit("sync_fetch_platform", {
                "name": platform_name, "slug": platform_slug, "status": "fetching",
            })

            # Try incremental skip
            skipped, roms = await self._try_incremental_skip_isolated(
                platform, registry, last_sync, platform_name, platform_slug
            )

            if not skipped:
                roms = await self._full_fetch_platform_roms_isolated(
                    platform["id"], platform_name, platform_slug, progress
                )

            progress["done"] += 1
            self._perf.increment("platforms_fetched")
            sem.record_latency(time.monotonic() - t0)

            # Notify frontend this platform is done fetching
            await self._emit("sync_fetch_platform", {
                "name": platform_name, "slug": platform_slug,
                "status": "done", "rom_count": len(roms),
            })

            await self._emit_progress(
                "roms",
                current=progress["roms_found"],
                total=progress["estimated_total_roms"],
                message=f"{platform_name} done · {progress['roms_found']}/{progress['estimated_total_roms']} ROMs ({progress['done']}/{progress['total']} platforms)",
                platforms_fetched=progress["done"],
                platforms_total=progress["total"],
            )
            return roms

    async def _try_incremental_skip_isolated(
        self,
        platform: dict,
        registry: dict,
        last_sync: str | None,
        platform_name: str,
        platform_slug: str,
    ) -> tuple[bool, list[dict]]:
        """Incremental skip check that returns (skipped, roms) without mutating shared state."""
        if not last_sync:
            return False, []

        platform_id = platform["id"]
        registry_count = sum(
            1 for entry in registry.values() if entry.get("platform_name") == platform_name
        )
        if registry_count == 0:
            return False, []

        try:
            delta_resp = await self._loop.run_in_executor(
                None,
                self._romm_api.list_roms_updated_after,
                platform_id,
                last_sync,
                1,
                0,
            )
            server_total = delta_resp.get("total", 0) if isinstance(delta_resp, dict) else 0
            platform_total = platform.get("rom_count", 0)

            if server_total == 0 and platform_total == registry_count:
                self._logger.info(f"Skipping {platform_name}: {registry_count} ROMs unchanged")
                roms = self._reconstruct_platform_from_registry(registry, platform_name, platform_slug)
                return True, roms

            self._logger.info(
                f"{platform_name}: {server_total} updated, "
                f"server={platform_total} vs registry={registry_count} — full fetch"
            )
        except Exception as e:
            self._logger.warning(f"Incremental check failed for {platform_name}, falling back to full fetch: {e}")
        return False, []

    async def _full_fetch_platform_roms_isolated(
        self,
        platform_id: int,
        platform_name: str,
        platform_slug: str,
        progress: dict,
    ) -> list[dict]:
        """Full paginated ROM fetch for one platform (concurrent-safe, no shared list mutation)."""
        offset = 0
        limit = self._PAGE_SIZE
        platform_roms: list[dict] = []

        while True:
            self._check_cancelling()
            try:
                roms = await self._loop.run_in_executor(
                    None,
                    self._romm_api.list_roms,
                    platform_id,
                    limit,
                    offset,
                )
            except Exception as e:
                self._logger.error(f"Failed to fetch ROMs for platform {platform_name}: {e}")
                break

            rom_list = roms.get("items", []) if isinstance(roms, dict) else roms
            for rom in rom_list:
                rom.pop("files", None)
                rom["platform_name"] = platform_name
                rom["platform_slug"] = platform_slug

            platform_roms.extend(rom_list)
            progress["roms_found"] += len(rom_list)
            await self._emit_progress(
                "roms",
                current=progress["roms_found"],
                total=progress["estimated_total_roms"],
                message=f"Fetching {platform_name}... {progress['roms_found']}/{progress['estimated_total_roms']} ROMs ({progress['done']}/{progress['total']} platforms)",
                sub_phase=f"platform:{platform_name}",
                platforms_fetched=progress["done"],
                platforms_total=progress["total"],
            )
            if len(rom_list) < limit:
                break
            offset += limit

        return platform_roms

    def _check_cancelling(self):
        """Raise CancelledError if sync is being cancelled."""
        if self._sync_state == SyncState.CANCELLING:
            raise asyncio.CancelledError(_SYNC_CANCELLED)

    def _build_shortcuts_data(self, all_roms):
        """Build shortcut data list from ROM list."""
        return build_shortcuts_data(all_roms, self._plugin_dir)

    async def _fetch_single_collection_roms(
        self, collection: dict, all_seen: set[int], collection_only_roms: list[dict]
    ) -> list[int]:
        """Fetch ROMs for a single collection, deduplicating against all_seen.

        Mutates all_seen and collection_only_roms in place.
        Returns the list of all rom_ids belonging to this collection.
        """
        coll_rom_ids, roms = await self._fetch_single_collection_roms_isolated(collection)
        for rom in roms:
            rid = rom["id"]
            if rid not in all_seen:
                all_seen.add(rid)
                collection_only_roms.append(rom)
        return coll_rom_ids

    async def _fetch_single_collection_roms_isolated(
        self, collection: dict
    ) -> tuple[list[int], list[dict]]:
        """Fetch ROMs for a single collection without shared state mutation.

        Returns (coll_rom_ids, roms) where roms have platform_name/slug set
        and files stripped.  Caller is responsible for deduplication.
        """
        cid = str(collection.get("id", ""))
        is_virtual = collection.get("is_virtual", False)
        coll_rom_ids: list[int] = []
        roms: list[dict] = []

        offset = 0
        limit = self._PAGE_SIZE
        while True:
            self._check_cancelling()
            if is_virtual:
                page = await self._loop.run_in_executor(
                    None, self._romm_api.list_roms_by_virtual_collection, cid, limit, offset
                )
            else:
                page = await self._loop.run_in_executor(
                    None, self._romm_api.list_roms_by_collection, collection["id"], limit, offset
                )

            items = page.get("items", [])
            for rom in items:
                rid = rom["id"]
                coll_rom_ids.append(rid)
                rom["platform_name"] = rom.get("platform_name", rom.get("platform_display_name", "Unknown"))
                rom["platform_slug"] = rom.get("platform_slug", rom.get("platform_fs_slug", ""))
                rom.pop("files", None)
                roms.append(rom)

            if len(items) < limit:
                break
            offset += limit

        return coll_rom_ids, roms

    async def _fetch_collection_roms(
        self, seen_rom_ids: set[int], emit_progress: bool = False
    ) -> tuple[list[dict], dict[str, list[int]]]:
        """Fetch ROMs from enabled collections, deduplicating against seen_rom_ids.

        Returns (collection_only_roms, collection_memberships).
        collection_only_roms: ROMs not already fetched via platforms
        collection_memberships: {collection_name: [all rom_ids in collection]}

        Fetches collections concurrently (bounded by semaphore) for speed,
        then merges and deduplicates results.
        When *emit_progress* is True, emits progress events.
        """
        collection_only_roms: list[dict] = []
        collection_memberships: dict[str, list[int]] = {}

        enabled_collections = self._settings.get("enabled_collections", {})
        enabled_ids = {k for k, v in enabled_collections.items() if v}
        self._log_debug(f"Collection sync: {len(enabled_ids)} enabled: {enabled_ids}")
        if not enabled_ids:
            return collection_only_roms, collection_memberships

        try:
            user_collections, franchise_collections = await self._get_collections_cached()

            self._log_debug(
                f"Collection metadata: {len(user_collections)} user, {len(franchise_collections)} franchise"
            )

            # Build list of enabled collections to fetch
            enabled_list = [
                c for c in user_collections + franchise_collections
                if str(c.get("id", "")) in enabled_ids
            ]
            collections_total = len(enabled_list)
            collections_done = 0

            # Concurrent fetch with bounded semaphore
            _COLLECTION_CONCURRENCY = 6
            sem = asyncio.Semaphore(_COLLECTION_CONCURRENCY)

            async def _fetch_one(c: dict) -> tuple[str, list[int], list[dict]]:
                nonlocal collections_done
                cname = c.get("name", str(c.get("id", "")))
                async with sem:
                    self._log_debug(
                        f"  Fetching collection '{cname}' "
                        f"(id={c.get('id')}, virtual={c.get('is_virtual', False)})"
                    )
                    rom_ids, roms = await self._fetch_single_collection_roms_isolated(c)

                    collections_done += 1
                    if emit_progress:
                        await self._emit_progress(
                            "collections",
                            current=collections_done,
                            total=collections_total,
                            message=f"Fetching collections — {collections_done}/{collections_total}",
                            sub_phase=f"collection:{cname}",
                            sub_message=cname,
                        )
                    return cname, rom_ids, roms

            results = await asyncio.gather(
                *[_fetch_one(c) for c in enabled_list],
                return_exceptions=True,
            )

            # Merge results and deduplicate
            all_seen = set(seen_rom_ids)
            for result in results:
                if isinstance(result, Exception):
                    self._logger.warning(f"Collection fetch failed: {result}")
                    continue
                cname, rom_ids, roms = result
                if rom_ids:
                    collection_memberships[cname] = rom_ids
                    self._log_debug(f"  Collection '{cname}': {len(rom_ids)} ROMs")
                for rom in roms:
                    rid = rom["id"]
                    if rid not in all_seen:
                        all_seen.add(rid)
                        collection_only_roms.append(rom)

        except RommUnsupportedError:
            self._logger.info("Collections not supported on this RomM version")
        except Exception as e:
            self._logger.warning(f"Failed to fetch collection ROMs: {e}")

        if collection_only_roms:
            self._logger.info(
                f"Fetched {len(collection_only_roms)} additional ROMs from {len(collection_memberships)} collections"
            )

        return collection_only_roms, collection_memberships

    async def _fetch_and_prepare(self):
        """Fetch platforms + ROMs + collection ROMs, prepare shortcut data.
        Returns (all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids)
        or raises on cancel/error.
        Artwork download is deferred to the apply phase.
        Uses updated_after on subsequent syncs to skip unchanged platforms.
        Emits sync_progress events throughout with global step plan."""

        # ── Global step plan (Improvement 3) ─────────────────────
        # We always have platforms + roms.  Collections and prepare
        # are folded into fetch; artwork + shortcuts are added by
        # the caller (_do_sync / sync_apply_delta) once it knows
        # what work remains.
        #
        # Steps emitted here:
        #   1  Fetching platforms
        #   2  Fetching ROMs
        #   3  Fetching collections   (only if enabled_collections exist)
        #
        # The caller appends:  artwork, shortcuts, removals (as needed).
        # total_steps is set to a preliminary value here and the caller
        # will adjust it once it knows the full plan.

        has_collections = bool(
            {k for k, v in self._settings.get("enabled_collections", {}).items() if v}
        )

        # Phase 1: Fetch platforms
        await self._emit_progress(
            "platforms",
            message="Fetching platforms...",
        )
        with self._perf.time_phase("fetch_platforms"):
            platforms = await self._fetch_enabled_platforms()
        self._check_cancelling()

        # ── Improvement 1: compute estimated total ROMs ──────────
        estimated_total_roms = sum(p.get("rom_count", 0) for p in platforms)
        self._logger.info(
            f"Estimated total ROMs across {len(platforms)} platforms: {estimated_total_roms}"
        )

        # ── Emit sync_plan so the frontend can render the accordion ──
        await self._emit("sync_plan", {
            "platforms": [
                {"name": p["name"], "slug": p["slug"], "rom_count": p.get("rom_count", 0)}
                for p in platforms
            ],
            "has_collections": has_collections,
            "estimated_total_roms": estimated_total_roms,
        })

        # Phase 2: Fetch ROMs per platform (concurrent, bounded by semaphore)
        total_platforms = len(platforms)
        await self._emit_progress(
            "roms",
            message=f"Fetching ROMs from {total_platforms} platforms...",
            total=estimated_total_roms,
            platforms_total=total_platforms,
        )
        last_sync = self._state.get("last_sync")
        registry = self._state.get("shortcut_registry", {})
        self._eta.start()

        sem = AdaptiveSemaphore(
            initial=self._FETCH_CONCURRENCY,
            min_concurrent=1,
            max_concurrent=8,
            low_latency_ms=1000.0,
            high_latency_ms=5000.0,
            window=5,
            adjust_every=3,
        )
        progress = {
            "done": 0,
            "roms_found": 0,
            "total": total_platforms,
            "estimated_total_roms": estimated_total_roms,
        }

        with self._perf.time_phase("fetch_roms"):
            tasks = [
                self._fetch_one_platform(platform, registry, last_sync, sem, progress)
                for platform in platforms
            ]
            results = await asyncio.gather(*tasks)

        # Record adaptive concurrency adjustments
        if sem.adjustments:
            self._perf.set_gauge("fetch_final_concurrency", sem.limit)
            self._logger.info(
                f"Fetch concurrency adapted: {self._FETCH_CONCURRENCY} → {sem.limit} "
                f"({len(sem.adjustments)} adjustment(s))"
            )

        all_roms: list[dict] = [rom for platform_roms in results for rom in platform_roms]

        self._check_cancelling()
        self._perf.set_gauge("total_roms_fetched", len(all_roms))
        self._logger.info(f"Fetched {len(all_roms)} ROMs from {len(platforms)} platforms")

        # Record which rom_ids came from platforms
        platform_rom_ids: set[int] = {r["id"] for r in all_roms}

        # Phase 3: Fetch collection ROMs (Improvement 2 — progress-aware)
        if has_collections:
            enabled_collections = self._settings.get("enabled_collections", {})
            enabled_count = sum(1 for v in enabled_collections.values() if v)
            await self._emit_progress(
                "collections",
                current=0,
                total=enabled_count,
                message=f"Fetching collections (0/{enabled_count})...",
            )
        with self._perf.time_phase("fetch_collections"):
            collection_only_roms, collection_memberships = await self._fetch_collection_roms(
                platform_rom_ids,
                emit_progress=has_collections,
            )
        all_roms.extend(collection_only_roms)

        # Phase 4: Prepare shortcut data (fast, CPU-only)
        with self._perf.time_phase("prepare_shortcuts"):
            shortcuts_data = self._build_shortcuts_data(all_roms)
            # Sort by platform_name so the frontend processes platforms
            # in a consistent, grouped order (per-platform pipeline).
            shortcuts_data.sort(key=lambda sd: sd.get("platform_name", ""))
        self._check_cancelling()

        # Cache metadata from sync response
        if self._metadata_service is not None:
            for rom in all_roms:
                rom_id_str = str(rom["id"])
                self._metadata_cache[rom_id_str] = self._metadata_service.extract_metadata(rom)
                self._metadata_service.mark_metadata_dirty()
            self._metadata_service.flush_metadata_if_dirty()
        self._log_debug(f"Metadata cached for {len(all_roms)} ROMs")

        return all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids

    # ── Full sync ────────────────────────────────────────────

    async def _do_sync(self):
        self._perf.start_sync()
        self._invalidate_collections_cache()
        try:
            # ── Phase 1: Fetch platform list ─────────────────
            await self._emit_progress("platforms", message="Fetching platforms...")
            with self._perf.time_phase("fetch_platforms"):
                platforms = await self._fetch_enabled_platforms()
            self._check_cancelling()

            has_collections = bool(
                {k for k, v in self._settings.get("enabled_collections", {}).items() if v}
            )
            estimated_total_roms = sum(p.get("rom_count", 0) for p in platforms)
            self._logger.info(
                f"Estimated total ROMs across {len(platforms)} platforms: {estimated_total_roms}"
            )

            # ── Emit sync_plan so the frontend can render the accordion ──
            await self._emit("sync_plan", {
                "platforms": [
                    {"name": p["name"], "slug": p["slug"], "rom_count": p.get("rom_count", 0)}
                    for p in platforms
                ],
                "has_collections": has_collections,
                "estimated_total_roms": estimated_total_roms,
            })

            # ── Phase 2: Per-platform fetch → build → emit pipeline ──
            total_platforms = len(platforms)
            last_sync = self._state.get("last_sync")
            registry = self._state.get("shortcut_registry", {})
            self._eta.start()

            sem = AdaptiveSemaphore(
                initial=self._FETCH_CONCURRENCY,
                min_concurrent=1,
                max_concurrent=8,
                low_latency_ms=1000.0,
                high_latency_ms=5000.0,
                window=5,
                adjust_every=3,
            )
            progress = {
                "done": 0,
                "roms_found": 0,
                "total": total_platforms,
                "estimated_total_roms": estimated_total_roms,
            }

            platform_rom_ids: set[int] = set()
            all_shortcuts_data: list[dict] = []
            shortcuts_before = 0
            total_roms_fetched = 0
            sorted_platforms = sorted(platforms, key=lambda p: p.get("name", ""))

            with self._perf.time_phase("fetch_and_apply_platforms"):
                for i, platform in enumerate(sorted_platforms):
                    self._check_cancelling()
                    platform_name = platform.get("name", platform.get("display_name", "Unknown"))

                    # 2a. Fetch this platform's ROMs
                    with self._perf.time_operation(f"platform:{platform.get('slug', '')}"):
                        platform_roms = await self._fetch_one_platform(
                            platform, registry, last_sync, sem, progress
                        )

                    # 2b. Build shortcut data for this platform
                    platform_shortcuts = self._build_shortcuts_data(platform_roms)

                    # 2c. Accumulate IDs for stale detection + collection dedup
                    for rom in platform_roms:
                        platform_rom_ids.add(rom["id"])
                    total_roms_fetched += len(platform_roms)

                    # 2d. Cache metadata
                    if self._metadata_service is not None:
                        for rom in platform_roms:
                            rom_id_str = str(rom["id"])
                            self._metadata_cache[rom_id_str] = self._metadata_service.extract_metadata(rom)
                            self._metadata_service.mark_metadata_dirty()

                    # 2e. Store in _pending_sync for report_sync_results
                    for sd in platform_shortcuts:
                        self._pending_sync[sd["rom_id"]] = sd
                    all_shortcuts_data.extend(platform_shortcuts)

                    # 2f. Emit per-platform event — frontend applies immediately
                    await self._emit("sync_apply_platform", {
                        "platform_name": platform_name,
                        "platform_index": i + 1,
                        "total_platforms": total_platforms,
                        "total_shortcuts_all": estimated_total_roms,
                        "shortcuts_before": shortcuts_before,
                        "shortcuts": platform_shortcuts,
                        "rom_count": len(platform_shortcuts),
                    })
                    shortcuts_before += len(platform_shortcuts)

                    self._logger.info(
                        f"Platform {i + 1}/{total_platforms}: {platform_name} "
                        f"({len(platform_shortcuts)} shortcuts emitted)"
                    )

                    # Small delay between emissions to avoid overwhelming the event bus
                    if i < total_platforms - 1:
                        await asyncio.sleep(0.05)

            if self._metadata_service is not None:
                self._metadata_service.flush_metadata_if_dirty()

            self._perf.set_gauge("total_roms_fetched", total_roms_fetched)
            self._logger.info(f"Fetched {total_roms_fetched} ROMs from {len(platforms)} platforms")

            # ── Phase 3: Fetch collection-only ROMs ──────────
            if has_collections:
                enabled_collections = self._settings.get("enabled_collections", {})
                enabled_count = sum(1 for v in enabled_collections.values() if v)
                await self._emit_progress(
                    "collections",
                    current=0,
                    total=enabled_count,
                    message=f"Fetching collections (0/{enabled_count})...",
                )
            collection_memberships: dict = {}
            with self._perf.time_phase("fetch_collections"):
                collection_only_roms, collection_memberships = await self._fetch_collection_roms(
                    platform_rom_ids,
                    emit_progress=has_collections,
                )

            # Build shortcuts for collection-only ROMs and emit as a batch
            if collection_only_roms:
                coll_shortcuts = self._build_shortcuts_data(collection_only_roms)
                for sd in coll_shortcuts:
                    self._pending_sync[sd["rom_id"]] = sd
                all_shortcuts_data.extend(coll_shortcuts)
                if self._metadata_service is not None:
                    for rom in collection_only_roms:
                        rom_id_str = str(rom["id"])
                        self._metadata_cache[rom_id_str] = self._metadata_service.extract_metadata(rom)
                        self._metadata_service.mark_metadata_dirty()
                    self._metadata_service.flush_metadata_if_dirty()
                total_roms_fetched += len(collection_only_roms)

            # Emit collection memberships
            if collection_memberships:
                await self._emit("sync_apply_collections", {
                    "collection_memberships": {
                        cname: [int(rid) for rid in rids]
                        for cname, rids in collection_memberships.items()
                    },
                    "total_collections": len(collection_memberships),
                })
                self._logger.info(
                    f"Emitted sync_apply_collections: {len(collection_memberships)} collections"
                )

            self._check_cancelling()

            # ── Phase 4: Stale reconciliation ────────────────
            # Compute stale detection using the accumulated ID set
            all_current_ids = platform_rom_ids | {r["id"] for r in collection_only_roms}
            stale_rom_ids: list[int] = []
            if self._settings.get("remove_on_unsync", True):
                stale_rom_ids = [
                    int(rid)
                    for rid in self._state["shortcut_registry"]
                    if int(rid) not in all_current_ids
                ]

            if stale_rom_ids:
                await self._emit("sync_apply_removals", {
                    "remove_rom_ids": stale_rom_ids,
                })
                self._logger.info(f"Emitted {len(stale_rom_ids)} removals")

            # Save sync stats
            self._state["sync_stats"] = {
                "platforms": len(platforms),
                "roms": total_roms_fetched,
            }
            self._save_state()

            # Store remaining pending data for report_sync_results
            self._pending_collection_memberships = collection_memberships
            self._pending_platform_rom_ids = platform_rom_ids

            # Signal frontend that all events have been emitted
            total_shortcuts = len(all_shortcuts_data)
            await self._emit("sync_apply_done", {
                "total_platforms": total_platforms,
                "total_shortcuts": total_shortcuts,
                "total_removals": len(stale_rom_ids),
                "remove_on_unsync": self._settings.get("remove_on_unsync", True),
            })
            self._logger.info(
                f"Sync emission complete: {total_platforms} platforms, "
                f"{total_shortcuts} shortcuts, {len(stale_rom_ids)} removals"
            )
        except Exception as e:
            import traceback

            self._logger.error(f"Sync failed: {e}\n{traceback.format_exc()}")
            _code, _msg = classify_error(e)
            self._sync_progress = {
                "running": False,
                "phase": "error",
                "current": 0,
                "total": 0,
                "message": f"Sync failed \u2014 {_msg}",
            }
            self._loop.create_task(self._emit("sync_progress", self._sync_progress))
        finally:
            self._perf.end_sync()
            self._logger.info(f"Sync performance:\n{self._perf.format_report()}")
            if self._metadata_service is not None:
                self._metadata_service.flush_metadata_if_dirty()
            self._sync_state = SyncState.IDLE
            if self._sync_progress.get("phase") != "error" and self._sync_progress.get("running"):
                self._start_safety_timeout()

    async def _finish_sync(self, message):
        self._sync_progress = {
            "running": False,
            "phase": "cancelled",
            "current": self._sync_progress.get("current", 0),
            "total": self._sync_progress.get("total", 0),
            "message": message,
        }
        await self._emit("sync_progress", self._sync_progress)
        self._sync_state = SyncState.IDLE
        self._logger.info(message)

    async def _emit_per_platform_delta(
        self,
        new_shortcuts: list,
        changed_shortcuts: list | None,
        stale_rom_ids: list,
        collection_memberships: dict,
    ):
        """Emit per-platform events for the delta-apply flow (sync_apply_delta).

        Groups shortcuts by platform_name, emits one event per platform so the
        frontend can process each platform before starting the next.
        Collection memberships, removals, and done are emitted separately.
        """
        from collections import defaultdict

        new_by_platform: dict[str, list] = defaultdict(list)
        for sd in new_shortcuts:
            new_by_platform[sd.get("platform_name", "Unknown")].append(sd)

        changed_by_platform: dict[str, list] = defaultdict(list)
        if changed_shortcuts:
            for sd in changed_shortcuts:
                changed_by_platform[sd.get("platform_name", "Unknown")].append(sd)

        platform_set = set(new_by_platform.keys()) | set(changed_by_platform.keys())
        sorted_platforms = sorted(platform_set)
        total_platforms = len(sorted_platforms)
        total_shortcuts = len(new_shortcuts) + (len(changed_shortcuts) if changed_shortcuts else 0)
        shortcuts_before = 0

        for i, pname in enumerate(sorted_platforms):
            p_new = new_by_platform.get(pname, [])
            p_changed = changed_by_platform.get(pname, [])

            event_data = {
                "platform_name": pname,
                "platform_index": i + 1,
                "total_platforms": total_platforms,
                "total_shortcuts_all": total_shortcuts,
                "shortcuts_before": shortcuts_before,
                "shortcuts": p_new,
                "rom_count": len(p_new) + len(p_changed),
            }
            if p_changed:
                event_data["changed_shortcuts"] = p_changed

            await self._emit("sync_apply_platform", event_data)
            shortcuts_before += len(p_new) + len(p_changed)

            self._logger.info(
                f"Emitted platform {i + 1}/{total_platforms}: {pname} "
                f"({len(p_new)} new, {len(p_changed)} changed)"
            )
            if i < total_platforms - 1:
                await asyncio.sleep(0.05)

        if collection_memberships:
            await self._emit("sync_apply_collections", {
                "collection_memberships": {
                    cname: [int(rid) for rid in rids]
                    for cname, rids in collection_memberships.items()
                },
                "total_collections": len(collection_memberships),
            })

        if stale_rom_ids:
            await self._emit("sync_apply_removals", {"remove_rom_ids": stale_rom_ids})

        await self._emit("sync_apply_done", {
            "total_platforms": total_platforms,
            "total_shortcuts": total_shortcuts,
            "total_removals": len(stale_rom_ids),
            "remove_on_unsync": self._settings.get("remove_on_unsync", True),
        })

    # ── Sync results (called by frontend) ────────────────────

    def _finalize_cover_path(self, grid, cover_path, app_id, rom_id_str):
        """Delegate to ArtworkService callback if available, else use local impl."""
        if self._artwork is not None:
            return self._artwork.finalize_cover_path(grid, cover_path, app_id, rom_id_str)
        # Fallback (no-op passthrough when callback not wired)
        return cover_path

    def _build_registry_entry(self, pending, app_id, cover_path):
        """Build a registry entry dict from pending sync data."""
        return build_registry_entry(pending, app_id, cover_path)

    def _build_collection_app_ids(
        self,
        registry: dict,
        pending_platform_rom_ids: set[int] | None,
        pending_collection_memberships: dict[str, list[int]],
    ) -> tuple[dict, dict[str, list]]:
        """Build platform_app_ids and romm_collection_app_ids from the shortcut registry."""
        platform_app_ids: dict = {}
        for rid_str, entry in registry.items():
            if not self._should_include_in_platform_collection(int(rid_str), pending_platform_rom_ids):
                continue
            pname = entry.get("platform_name", "Unknown")
            platform_app_ids.setdefault(pname, []).append(entry.get("app_id"))

        romm_collection_app_ids: dict[str, list] = {}
        for coll_name, rom_ids in pending_collection_memberships.items():
            app_ids = [entry["app_id"] for rid in rom_ids if (entry := registry.get(str(rid))) and "app_id" in entry]
            if app_ids:
                romm_collection_app_ids[coll_name] = app_ids

        return platform_app_ids, romm_collection_app_ids

    def _report_sync_results_io(self, rom_id_to_app_id, removed_rom_ids):
        """Sync helper for report_sync_results — artwork renames, state save in executor."""
        grid = self._steam_config.grid_dir()

        for rom_id_str, app_id in rom_id_to_app_id.items():
            pending = self._pending_sync.get(int(rom_id_str), {})
            cover_path = self._finalize_cover_path(grid, pending.get("cover_path", ""), app_id, rom_id_str)
            self._state["shortcut_registry"][rom_id_str] = self._build_registry_entry(pending, app_id, cover_path)

        for rom_id in removed_rom_ids:
            self._state["shortcut_registry"].pop(str(rom_id), None)

        # Apply Steam Input mode for new shortcuts
        steam_input_mode = self._settings.get("steam_input_mode", "default")
        if steam_input_mode != "default" and rom_id_to_app_id:
            try:
                self._steam_config.set_steam_input_config(
                    [int(aid) for aid in rom_id_to_app_id.values()], mode=steam_input_mode
                )
            except Exception as e:
                self._logger.error(f"Failed to set Steam Input config: {e}")

        # Capture pending state before clearing
        pending_collection_memberships = self._pending_collection_memberships
        pending_platform_rom_ids = self._pending_platform_rom_ids
        self._pending_collection_memberships = {}
        self._pending_platform_rom_ids = None
        self._pending_sync = {}

        # Build final collection mappings
        platform_app_ids, romm_collection_app_ids = self._build_collection_app_ids(
            self._state["shortcut_registry"],
            pending_platform_rom_ids,
            pending_collection_memberships,
        )

        # Save state with the actual synced platforms/collections
        self._state["last_sync"] = datetime.now(UTC).isoformat()
        self._state["last_synced_collections"] = list(pending_collection_memberships.keys())
        self._state["last_synced_platforms"] = list(platform_app_ids.keys())
        self._save_state()

        return platform_app_ids, romm_collection_app_ids

    async def report_sync_results(self, rom_id_to_app_id, removed_rom_ids, cancelled=False):
        """Called by frontend after applying shortcuts via SteamClient."""
        platform_app_ids, romm_collection_app_ids = await self._loop.run_in_executor(
            None, self._report_sync_results_io, rom_id_to_app_id, removed_rom_ids
        )

        total = len(self._state["shortcut_registry"])
        processed = len(rom_id_to_app_id)

        if cancelled:
            await self._emit(
                "sync_complete",
                {
                    "platform_app_ids": platform_app_ids,
                    "romm_collection_app_ids": romm_collection_app_ids,
                    "total_games": processed,
                    "cancelled": True,
                },
            )
            await self._emit_progress(
                "done",
                current=processed,
                total=total,
                message=f"Sync cancelled: {processed} of {total} games processed",
                running=False,
            )
            self._logger.info(f"Sync cancelled: {processed}/{total} games processed")
        else:
            await self._emit(
                "sync_complete",
                {
                    "platform_app_ids": platform_app_ids,
                    "romm_collection_app_ids": romm_collection_app_ids,
                    "total_games": processed,
                },
            )
            await self._emit_progress(
                "done",
                current=total,
                total=total,
                message=f"Sync complete: {total} games from {len(platform_app_ids)} platforms",
                running=False,
            )
            self._logger.info(f"Sync results reported: {total} games")
        self._sync_state = SyncState.IDLE
        return {"success": True}

    # ── Incremental sync results (called by frontend in batches) ──

    def _incremental_report_io(self, rom_id_to_app_id: dict, removed_rom_ids: list) -> None:
        """Sync helper: update registry + finalize artwork for a batch, then save state.

        Called every BATCH_SIZE shortcuts from the frontend so that progress is
        persisted incrementally.  Does NOT touch collection memberships or
        last_sync — those are deferred to ``_finalize_sync_io``.
        """
        grid = self._steam_config.grid_dir()

        for rom_id_str, app_id in rom_id_to_app_id.items():
            pending = self._pending_sync.get(int(rom_id_str), {})
            cover_path = self._finalize_cover_path(grid, pending.get("cover_path", ""), app_id, rom_id_str)
            self._state["shortcut_registry"][rom_id_str] = self._build_registry_entry(pending, app_id, cover_path)

        for rom_id in removed_rom_ids:
            self._state["shortcut_registry"].pop(str(rom_id), None)

        # Apply Steam Input mode for this batch
        steam_input_mode = self._settings.get("steam_input_mode", "default")
        if steam_input_mode != "default" and rom_id_to_app_id:
            try:
                self._steam_config.set_steam_input_config(
                    [int(aid) for aid in rom_id_to_app_id.values()], mode=steam_input_mode
                )
            except Exception as e:
                self._logger.error(f"Failed to set Steam Input config for batch: {e}")

        self._save_state()

    async def report_incremental_results(self, rom_id_to_app_id: dict, removed_rom_ids: list) -> dict:
        """Persist a batch of shortcut results incrementally during sync.

        Called by the frontend every BATCH_SIZE shortcuts (e.g. every 20).
        Updates ``shortcut_registry`` and saves state to disk immediately.
        Collection memberships and ``last_sync`` are deferred to
        ``report_sync_finalized()``.

        Returns ``{"success": True, "persisted": <count>}``.
        """
        await self._loop.run_in_executor(None, self._incremental_report_io, rom_id_to_app_id, removed_rom_ids)
        count = len(rom_id_to_app_id) + len(removed_rom_ids)
        self._logger.info(f"Incremental batch persisted: {count} items")
        return {"success": True, "persisted": count}

    def _finalize_sync_io(self, cancelled: bool) -> tuple[dict, dict]:
        """Sync helper: build collection mappings, set last_sync, save state.

        Extracted from ``_report_sync_results_io`` — this runs once after all
        incremental batches have been persisted.
        """
        # Capture pending state before clearing
        pending_collection_memberships = self._pending_collection_memberships
        pending_platform_rom_ids = self._pending_platform_rom_ids
        self._pending_collection_memberships = {}
        self._pending_platform_rom_ids = None
        self._pending_sync = {}

        if cancelled:
            # On cancellation: do NOT update last_sync so the next sync
            # will re-fetch everything and detect already-created shortcuts
            # as "unchanged".  Also skip collection building — partial
            # collection data would be misleading.
            self._save_state()
            return {}, {}

        # Build final collection mappings from the full registry
        platform_app_ids, romm_collection_app_ids = self._build_collection_app_ids(
            self._state["shortcut_registry"],
            pending_platform_rom_ids,
            pending_collection_memberships,
        )

        self._state["last_sync"] = datetime.now(UTC).isoformat()
        self._state["last_synced_collections"] = list(pending_collection_memberships.keys())
        self._state["last_synced_platforms"] = list(platform_app_ids.keys())
        self._save_state()

        return platform_app_ids, romm_collection_app_ids

    async def report_sync_finalized(self, remaining_rom_id_to_app_id: dict, removed_rom_ids: list, cancelled: bool = False) -> dict:
        """Called after all incremental batches.  Handles finalization:

        - Persist any remaining shortcuts not yet in an incremental batch
        - Build platform & collection app_id mappings
        - Set ``last_sync`` timestamp (only on success)
        - Emit ``sync_complete`` event
        """
        # Persist any stragglers (shortcuts added after the last flush)
        if remaining_rom_id_to_app_id or removed_rom_ids:
            await self._loop.run_in_executor(
                None, self._incremental_report_io, remaining_rom_id_to_app_id, removed_rom_ids
            )

        # Finalization: collections, timestamps
        platform_app_ids, romm_collection_app_ids = await self._loop.run_in_executor(
            None, self._finalize_sync_io, cancelled
        )

        total = len(self._state["shortcut_registry"])

        if cancelled:
            await self._emit(
                "sync_complete",
                {
                    "platform_app_ids": platform_app_ids,
                    "romm_collection_app_ids": romm_collection_app_ids,
                    "total_games": total,
                    "cancelled": True,
                },
            )
            await self._emit_progress(
                "done",
                current=total,
                total=total,
                message=f"Sync cancelled: {total} games in registry",
                running=False,
            )
            self._logger.info(f"Sync finalized (cancelled): {total} games in registry")
        else:
            await self._emit(
                "sync_complete",
                {
                    "platform_app_ids": platform_app_ids,
                    "romm_collection_app_ids": romm_collection_app_ids,
                    "total_games": total,
                },
            )
            await self._emit_progress(
                "done",
                current=total,
                total=total,
                message=f"Sync complete: {total} games from {len(platform_app_ids)} platforms",
                running=False,
            )
            self._logger.info(f"Sync finalized: {total} games from {len(platform_app_ids)} platforms")

        self._sync_state = SyncState.IDLE
        return {"success": True}

    # ── Artwork delegation ───────────────────────────────────

    async def _download_artwork(self, all_roms, progress_step=4, progress_total_steps=6):
        """Delegate artwork download to ArtworkService callback."""
        if self._artwork is not None:
            return await self._artwork.download_artwork(
                all_roms,
                emit_progress=self._emit_progress,
                is_cancelling=lambda: self._sync_state == SyncState.CANCELLING,
                progress_step=progress_step,
                progress_total_steps=progress_total_steps,
            )
        return {}

    # ── Registry queries ─────────────────────────────────────

    def get_registry_platforms(self):
        """Return platforms from the shortcut registry (works offline, no RomM API call)."""
        platforms = {}
        for entry in self._state["shortcut_registry"].values():
            pname = entry.get("platform_name", "Unknown")
            slug = entry.get("platform_slug", "")
            platforms.setdefault(pname, {"count": 0, "slug": slug})
            platforms[pname]["count"] += 1
        return {
            "platforms": [{"name": k, "slug": v["slug"], "count": v["count"]} for k, v in sorted(platforms.items())],
        }

    # ── Cache / stats ────────────────────────────────────────

    def clear_sync_cache(self):
        """Clear last_sync timestamp to force a full re-fetch on next sync."""
        self._state["last_sync"] = None
        self._invalidate_collections_cache()
        self._save_state()
        self._logger.info("Sync cache cleared — next sync will do a full fetch")
        return {"success": True, "message": "Next sync will do a full fetch"}

    def get_sync_stats(self):
        registry = self._state.get("shortcut_registry", {})
        enabled_platforms = self._settings.get("enabled_platforms", {})
        enabled_platform_count = sum(1 for v in enabled_platforms.values() if v)
        enabled_collections = self._settings.get("enabled_collections", {})
        enabled_collection_count = sum(1 for v in enabled_collections.values() if v)
        return {
            "last_sync": self._state.get("last_sync"),
            "platforms": enabled_platform_count,
            "collections": enabled_collection_count,
            "roms": len(registry),
            "total_shortcuts": len(registry),
        }

    def get_rom_by_steam_app_id(self, app_id):
        app_id = int(app_id)
        for rom_id, entry in self._state["shortcut_registry"].items():
            if entry.get("app_id") == app_id:
                installed = self._state["installed_roms"].get(rom_id)
                return {
                    "rom_id": int(rom_id),
                    "name": entry.get("name", ""),
                    "platform_name": entry.get("platform_name", ""),
                    "platform_slug": entry.get("platform_slug", ""),
                    "installed": installed,
                }
        return None

    def get_perf_report(self) -> dict:
        """Return the performance report from the most recent sync.

        Safe to call at any time — returns empty report if no sync has run.
        """
        if self._perf.wall_time > 0:
            return {
                "success": True,
                "report": self._perf.generate_report(),
                "formatted": self._perf.format_report(),
            }
        return {"success": False, "message": "No performance data available"}
