"""LibraryService — library sync engine.

Handles platform/ROM fetching, shortcut data preparation,
delta preview/apply, and shortcut registry management.

Artwork operations are delegated to ArtworkService via callbacks.
Shortcut removal is delegated to ShortcutRemovalService.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.shortcut_data import build_registry_entry, build_shortcuts_data
from domain.sync_diff import (
    classify_roms,
    compute_collection_diff,
    compute_platform_collection_diff,
    should_include_in_platform_collection,
)
from domain.sync_state import SyncState
from lib.errors import classify_error

if TYPE_CHECKING:
    import logging

    from services.protocols import (
        ArtworkManager,
        Clock,
        DebugLogger,
        EventEmitter,
        MetadataExtractor,
        RommApiProtocol,
        SettingsPersister,
        Sleeper,
        StatePersister,
        SteamConfigAdapter,
        UuidGen,
    )


_SYNC_CANCELLED = "Sync cancelled"
_PREVIEW_MAX_AGE_SECONDS = 1800  # 30 minutes — preview snapshots stale beyond this


@dataclass(frozen=True)
class LibraryServiceConfig:
    """Frozen wiring bundle handed to ``LibraryService.__init__``.

    Holds the Protocol-typed adapters, the live state/settings/metadata
    cache dicts, runtime infrastructure, time/sleep/uuid seams, plugin-
    dir reference, event emitter, persistence callbacks, debug-logger
    seam, and the optional metadata/artwork peer services LibraryService
    needs at construction time.
    """

    romm_api: RommApiProtocol
    steam_config: SteamConfigAdapter
    state: dict
    settings: dict
    metadata_cache: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    emit: EventEmitter
    clock: Clock
    uuid_gen: UuidGen
    sleeper: Sleeper
    save_state: StatePersister
    save_settings_to_disk: SettingsPersister
    log_debug: DebugLogger
    metadata_service: MetadataExtractor | None = None
    artwork: ArtworkManager | None = None


class LibraryService:
    """Sync engine: fetch ROMs, prepare shortcuts, manage registry."""

    def __init__(self, *, config: LibraryServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._steam_config = config.steam_config
        self._state = config.state
        self._settings = config.settings
        self._metadata_cache = config.metadata_cache
        self._loop = config.loop
        self._logger = config.logger
        self._plugin_dir = config.plugin_dir
        self._emit = config.emit
        self._clock = config.clock
        self._uuid_gen = config.uuid_gen
        self._sleeper = config.sleeper
        self._save_state = config.save_state
        self._save_settings_to_disk = config.save_settings_to_disk
        self._log_debug = config.log_debug
        self._metadata_service = config.metadata_service
        self._artwork = config.artwork

        # Sync-specific state (owned by this service)
        self._sync_state = SyncState.IDLE
        self._sync_last_heartbeat = 0.0
        # Generation id for the active sync run. Background tasks
        # (safety timeout) capture this at start and check it still
        # matches before mutating state — prevents a late timeout from
        # overwriting the post-cancel/error transition done by
        # ``_finish_sync``.
        self._current_sync_id: str | None = None
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

    async def get_collections(self):
        try:
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}
        try:
            franchise_collections = await self._loop.run_in_executor(
                None, self._romm_api.list_virtual_collections, "franchise"
            )
        except Exception as e:
            self._logger.warning(f"Failed to fetch franchise collections, continuing without them: {e}")
            franchise_collections = []

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
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
        except Exception as e:
            self._logger.error(f"Failed to fetch collections: {e}")
            _code, _msg = classify_error(e)
            return {"success": False, "message": _msg, "error_code": _code}
        try:
            franchise_collections = await self._loop.run_in_executor(
                None, self._romm_api.list_virtual_collections, "franchise"
            )
        except Exception as e:
            self._logger.warning(f"Failed to fetch franchise collections, continuing without them: {e}")
            franchise_collections = []

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
        self._current_sync_id = self._uuid_gen.uuid4()
        self._sync_last_heartbeat = self._clock.monotonic()
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
        self._sync_last_heartbeat = self._clock.monotonic()
        return {"success": True}

    # ── Preview / Apply ──────────────────────────────────────

    async def sync_preview(self):
        if self._sync_state != SyncState.IDLE:
            return {"success": False, "message": "Sync already in progress"}
        self._sync_state = SyncState.RUNNING
        self._current_sync_id = self._uuid_gen.uuid4()
        self._sync_last_heartbeat = self._clock.monotonic()
        try:
            fetch_result = await self._fetch_and_prepare()
            all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids = fetch_result
            platform_names = {p["name"] for p in platforms if p.get("name")}
            new, changed, unchanged_ids, stale, disabled_count = classify_roms(
                shortcuts_data,
                self._state["shortcut_registry"],
                platform_names,
            )

            # Build rom lookup for artwork download during apply
            roms_by_id = {r["id"]: r for r in all_roms}
            delta_rom_ids = {sd["rom_id"] for sd in new + changed}
            delta_roms = [roms_by_id[rid] for rid in delta_rom_ids if rid in roms_by_id]

            preview_id = self._uuid_gen.uuid4()
            self._pending_delta = {
                "preview_id": preview_id,
                "created_at": self._clock.time(),
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
                    "collection_diff": compute_collection_diff(
                        collection_memberships,
                        self._state.get("last_synced_collections", []),
                    ),
                    "platform_collection_diff": compute_platform_collection_diff(
                        shortcuts_data,
                        platform_rom_ids,
                        self._state.get("last_synced_platforms", []),
                        self._settings.get("collection_create_platform_groups", False),
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
        age = self._clock.time() - self._pending_delta.get("created_at", 0)
        if age > _PREVIEW_MAX_AGE_SECONDS:
            self._pending_delta = None
            return {
                "success": False,
                "message": "Preview is older than 30 minutes, please re-run sync",
                "error_code": "stale_preview",
            }
        delta = self._pending_delta
        self._pending_delta = None
        self._sync_state = SyncState.RUNNING
        self._current_sync_id = self._uuid_gen.uuid4()
        self._sync_last_heartbeat = self._clock.monotonic()

        # Calculate apply step plan
        delta_roms = delta.get("delta_roms", [])
        has_artwork = len(delta_roms) > 0
        has_shortcuts = len(delta["new"]) + len(delta["changed"]) > 0
        has_removals = len(delta["remove_rom_ids"]) > 0

        apply_steps = []
        if has_artwork:
            apply_steps.append("artwork")
        if has_shortcuts:
            apply_steps.append("shortcuts")
        if has_removals:
            apply_steps.append("removals")
        total_steps = len(apply_steps)
        current_step = 0

        # Step: Download artwork
        if has_artwork:
            current_step += 1
            await self._emit_progress(
                "applying",
                total=len(delta_roms),
                message=f"Downloading artwork 0/{len(delta_roms)}",
                step=current_step,
                total_steps=total_steps,
            )
            cover_paths = await self._download_artwork(
                delta_roms, progress_step=current_step, progress_total_steps=total_steps
            )
            for sd in delta["new"] + delta["changed"]:
                sd["cover_path"] = cover_paths.get(sd["rom_id"], "")

        # Populate _pending_sync for report_sync_results and get_artwork_base64
        self._pending_sync = delta["all_shortcuts"]
        self._pending_collection_memberships = delta.get("collection_memberships", {})
        self._pending_platform_rom_ids = delta.get("platform_rom_ids", set())

        # Update sync_stats
        self._state["sync_stats"] = {
            "platforms": delta["platforms_count"],
            "roms": delta["total_roms"],
        }
        self._save_state()

        # Figure out which step the frontend starts at
        next_step = current_step + 1

        total_changes = len(delta["new"]) + len(delta["changed"])
        await self._emit_progress(
            "applying",
            total=total_changes,
            message=f"Applying shortcuts 0/{total_changes}",
            step=next_step,
            total_steps=total_steps,
        )

        # Emit delta with step plan for frontend
        await self._emit(
            "sync_apply",
            {
                "shortcuts": delta["new"],
                "changed_shortcuts": delta["changed"],
                "remove_rom_ids": delta["remove_rom_ids"],
                "next_step": next_step,
                "total_steps": total_steps,
            },
        )

        self._logger.info(
            f"Delta sync emitted: {len(delta['new'])} new, {len(delta['changed'])} changed, "
            f"{len(delta['remove_rom_ids'])} removed"
        )

        # Heartbeat safety timeout
        self._start_safety_timeout()

        return {"success": True, "message": "Applying changes"}

    def sync_cancel_preview(self):
        self._pending_delta = None
        return {"success": True}

    # ── Progress & safety ────────────────────────────────────

    async def _emit_progress(self, phase, current=0, total=0, message="", running=True, step=0, total_steps=0):
        """Update _sync_progress and emit sync_progress event to frontend."""
        self._sync_progress = {
            "running": running,
            "phase": phase,
            "current": current,
            "total": total,
            "message": message,
            "step": step,
            "totalSteps": total_steps,
        }
        await self._emit("sync_progress", self._sync_progress)

    def _start_safety_timeout(self, heartbeat_timeout_sec=30) -> asyncio.Task:
        """Launch a background task that auto-completes sync if no heartbeat arrives.

        Returns the spawned task so tests can deterministically await its
        completion; production callers ignore the return value.
        """
        self._sync_last_heartbeat = self._clock.monotonic()
        captured_sync_id = self._current_sync_id

        async def _safety_timeout():
            while self._sync_progress.get("running"):
                await self._sleeper.sleep(10)
                # Generation guard: if our sync has ended (cancel, error,
                # normal completion), _current_sync_id was cleared or
                # replaced. Don't fire stale "done" or overwrite the new
                # sync state.
                if self._current_sync_id != captured_sync_id:
                    return
                elapsed = self._clock.monotonic() - self._sync_last_heartbeat
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
                    # Second generation check: the await above yielded the
                    # event loop, so a new sync may have started during
                    # _emit_progress. Don't stomp its state.
                    if self._current_sync_id != captured_sync_id:
                        return
                    self._sync_state = SyncState.IDLE
                    self._current_sync_id = None
                    return

        return self._loop.create_task(_safety_timeout())

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
        """Full paginated fetch of ROMs for a single platform."""
        offset = 0
        limit = 50
        await self._emit_progress(
            "roms",
            current=len(all_roms),
            message=f"Fetching {platform_name}... {len(all_roms)} found ({pi}/{total_platforms})",
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

            all_roms.extend(rom_list)
            await self._emit_progress(
                "roms",
                current=len(all_roms),
                message=f"Fetching {platform_name}... {len(all_roms)} found ({pi}/{total_platforms})",
            )
            if len(rom_list) < limit:
                break
            offset += limit

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
        cid = str(collection.get("id", ""))
        is_virtual = collection.get("is_virtual", False)
        coll_rom_ids: list[int] = []

        offset = 0
        limit = 50
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
                if rid not in all_seen:
                    all_seen.add(rid)
                    rom["platform_name"] = rom.get("platform_name", rom.get("platform_display_name", "Unknown"))
                    rom["platform_slug"] = rom.get("platform_slug", rom.get("platform_fs_slug", ""))
                    rom.pop("files", None)
                    collection_only_roms.append(rom)

            if len(items) < limit:
                break
            offset += limit

        return coll_rom_ids

    async def _fetch_collection_roms(self, seen_rom_ids: set[int]) -> tuple[list[dict], dict[str, list[int]]]:
        """Fetch ROMs from enabled collections, deduplicating against seen_rom_ids.

        Returns (collection_only_roms, collection_memberships).
        collection_only_roms: ROMs not already fetched via platforms
        collection_memberships: {collection_name: [all rom_ids in collection]}
        """
        collection_only_roms: list[dict] = []
        collection_memberships: dict[str, list[int]] = {}

        enabled_collections = self._settings.get("enabled_collections", {})
        enabled_ids = {k for k, v in enabled_collections.items() if v}
        self._log_debug(f"Collection sync: {len(enabled_ids)} enabled: {enabled_ids}")
        if not enabled_ids:
            return collection_only_roms, collection_memberships

        try:
            user_collections = await self._loop.run_in_executor(None, self._romm_api.list_collections)
            franchise_collections: list[dict] = []
            try:
                franchise_collections = await self._loop.run_in_executor(
                    None, self._romm_api.list_virtual_collections, "franchise"
                )
            except Exception as e:
                self._logger.warning(f"Failed to fetch franchise collections: {e}")

            self._log_debug(
                f"Collection metadata: {len(user_collections)} user, {len(franchise_collections)} franchise"
            )
            all_seen = set(seen_rom_ids)  # Copy so we don't mutate caller's set

            for c in user_collections + franchise_collections:
                cid = str(c.get("id", ""))
                if cid not in enabled_ids:
                    self._log_debug(f"  Skipping collection '{c.get('name', cid)}' (id={cid}, not enabled)")
                    continue

                coll_name = c.get("name", cid)
                is_virtual = c.get("is_virtual", False)
                self._log_debug(f"  Fetching collection '{coll_name}' (id={cid}, virtual={is_virtual})")

                coll_rom_ids = await self._fetch_single_collection_roms(c, all_seen, collection_only_roms)

                if coll_rom_ids:
                    collection_memberships[coll_name] = coll_rom_ids
                    self._log_debug(f"  Collection '{coll_name}': {len(coll_rom_ids)} ROMs")

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
        Emits sync_progress events throughout."""

        # Phase 1: Fetch platforms
        await self._emit_progress("platforms", message="Fetching platforms...")
        platforms = await self._fetch_enabled_platforms()
        self._check_cancelling()

        # Phase 2: Fetch ROMs per platform (incremental if possible)
        await self._emit_progress("roms", message="Fetching ROMs...")
        last_sync = self._state.get("last_sync")
        registry = self._state.get("shortcut_registry", {})

        all_roms: list[dict] = []
        total_platforms = len(platforms)
        for pi, platform in enumerate(platforms, 1):
            self._check_cancelling()
            platform_name = platform.get("name", platform.get("display_name", "Unknown"))
            platform_slug = platform.get("slug", "")

            skipped = await self._try_incremental_skip(
                platform, registry, last_sync, platform_name, platform_slug, all_roms, pi, total_platforms
            )
            if not skipped:
                await self._full_fetch_platform_roms(
                    platform["id"], platform_name, platform_slug, all_roms, pi, total_platforms
                )

        self._check_cancelling()
        self._logger.info(f"Fetched {len(all_roms)} ROMs from {len(platforms)} platforms")

        # Record which rom_ids came from platforms
        platform_rom_ids: set[int] = {r["id"] for r in all_roms}

        # Phase 3: Fetch collection ROMs (adds ROMs not already in all_roms)
        collection_only_roms, collection_memberships = await self._fetch_collection_roms(platform_rom_ids)
        all_roms.extend(collection_only_roms)

        # Phase 4: Prepare shortcut data
        shortcuts_data = self._build_shortcuts_data(all_roms)
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
        try:
            try:
                fetch_result = await self._fetch_and_prepare()
                all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids = fetch_result
            except asyncio.CancelledError:
                await self._finish_sync(_SYNC_CANCELLED)
                raise
            except Exception as e:
                self._logger.error(f"Failed to fetch platforms: {e}")
                _code, _msg = classify_error(e)
                await self._emit_progress("error", message=_msg, running=False)
                self._sync_state = SyncState.IDLE
                return

            # Calculate step plan for full sync
            has_artwork = len(all_roms) > 0
            has_shortcuts = len(shortcuts_data) > 0
            full_steps = []
            if has_artwork:
                full_steps.append("artwork")
            if has_shortcuts:
                full_steps.append("shortcuts")
            full_total_steps = len(full_steps)
            full_current_step = 0

            if has_artwork:
                full_current_step += 1
                await self._emit_progress(
                    "applying",
                    total=len(all_roms),
                    message=f"Downloading artwork 0/{len(all_roms)}",
                    step=full_current_step,
                    total_steps=full_total_steps,
                )
                cover_paths = await self._download_artwork(
                    all_roms, progress_step=full_current_step, progress_total_steps=full_total_steps
                )
            else:
                cover_paths = {}

            if self._sync_state == SyncState.CANCELLING:
                await self._finish_sync(_SYNC_CANCELLED)
                return

            for sd in shortcuts_data:
                sd["cover_path"] = cover_paths.get(sd["rom_id"], "")

            # Determine stale rom_ids by comparing current sync with registry
            current_rom_ids = {r["id"] for r in all_roms}
            stale_rom_ids = [int(rid) for rid in self._state["shortcut_registry"] if int(rid) not in current_rom_ids]

            # Emit sync_apply for frontend to process via SteamClient
            next_step = full_current_step + 1
            await self._emit_progress(
                "applying",
                total=len(shortcuts_data),
                message=f"Applying shortcuts 0/{len(shortcuts_data)}",
                step=next_step,
                total_steps=full_total_steps,
            )

            # Save sync stats (registry updated by report_sync_results)
            self._state["sync_stats"] = {
                "platforms": len(platforms),
                "roms": len(all_roms),
            }
            self._save_state()

            # Store pending data for report_sync_results to reference
            self._pending_sync = {sd["rom_id"]: sd for sd in shortcuts_data}
            self._pending_collection_memberships = collection_memberships
            self._pending_platform_rom_ids = platform_rom_ids

            await self._emit(
                "sync_apply",
                {
                    "shortcuts": shortcuts_data,
                    "remove_rom_ids": stale_rom_ids,
                    "next_step": next_step,
                    "total_steps": full_total_steps,
                },
            )

            self._logger.info(f"Sync data emitted: {len(shortcuts_data)} shortcuts, {len(stale_rom_ids)} stale")
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
        self._current_sync_id = None
        self._logger.info(message)

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
            if not should_include_in_platform_collection(
                int(rid_str),
                pending_platform_rom_ids,
                self._settings.get("collection_create_platform_groups", False),
            ):
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
        self._state["last_sync"] = self._clock.now().isoformat()
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
        # Invalidate any in-flight safety timeout — sync is over, no
        # late "done" event should fire on top of the one we just emitted.
        self._current_sync_id = None
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
