"""Sync result reporter and registry-query sub-service.

Owns the post-apply path: the frontend-callable ``report_unit_results``
ack (event signal only) and the orchestrator-driven
``commit_unit_results`` that finalises artwork file names, appends
per-ROM registry entries, and persists state. The terminal
``finalize_per_unit_run`` step builds the cross-unit collection
mappings, persists last-sync metadata, and emits the ``sync_complete``
event. Also owns the registry-derived query methods
(``get_registry_platforms``, ``get_sync_stats``,
``get_rom_by_steam_app_id``) and the ``clear_sync_cache`` reset.
Anything that mutates the registry as a side-effect of a finished
sync run belongs here; anything that decides "what should this sync
do?" belongs in the orchestrator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.registry_patches import RegistrySyncApplyPatch
from models.state import PluginState

from domain.sync_diff import should_include_in_platform_collection
from domain.sync_state import SyncState
from services.library._state import LibrarySyncStateBox

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable

    from services.protocols import (
        ArtworkManager,
        Clock,
        EventEmitter,
        ShortcutRegistryStore,
        StatePersister,
        SteamConfigStore,
    )

    EmitProgressFn = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class SyncReporterConfig:
    """Frozen wiring bundle handed to ``SyncReporter.__init__``.

    Holds the Protocol-typed Steam-config adapter (used for grid-dir
    lookup and Steam-Input mode application), the live state/settings
    dicts, runtime infrastructure (loop, logger), event emitter, clock,
    state-persistence callback, the shared ``LibrarySyncStateBox`` (the
    reporter reads the pending-sync dicts populated by the orchestrator
    and clears the active sync id when reporting completes), an
    orchestrator-supplied ``emit_progress`` callback for the terminal
    "done" event, and the ``ArtworkManager`` peer used for cover-path
    finalisation.
    """

    steam_config: SteamConfigStore
    state: PluginState
    settings: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: EventEmitter
    clock: Clock
    state_persister: StatePersister
    registry_store: ShortcutRegistryStore
    sync_state_box: LibrarySyncStateBox
    emit_progress: EmitProgressFn
    artwork: ArtworkManager


class SyncReporter:
    """Post-apply reporter + registry queries + cache reset."""

    def __init__(self, *, config: SyncReporterConfig) -> None:
        self._steam_config = config.steam_config
        self._state = config.state
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._clock = config.clock
        self._state_persister = config.state_persister
        self._registry_store = config.registry_store
        self._sync_state = config.sync_state_box
        self._emit_progress = config.emit_progress
        self._artwork = config.artwork

    # ── Report sync results (frontend callback) ──────────────────

    def _finalize_cover_path(self, grid, cover_path, app_id, rom_id_str):
        """Delegate to ArtworkService for the final ``{app_id}p.png`` cover-path."""
        return self._artwork.finalize_cover_path(grid, cover_path, app_id, rom_id_str)

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

    # ── Finalise per-unit run ────────────────────────────────────

    def _finalize_per_unit_run_io(
        self,
        pending_collection_memberships: dict[str, list[int]],
        pending_platform_rom_ids: set[int] | None,
    ) -> tuple[dict, dict[str, list]]:
        """Build collection app-id maps and persist last_sync metadata.

        By the time this runs, every per-unit ``report_unit_results``
        has already updated the registry, so we only need to build the
        cross-unit collection mappings and write the final
        ``last_sync`` / ``last_synced_*`` fields.
        """
        platform_app_ids, romm_collection_app_ids = self._build_collection_app_ids(
            self._state["shortcut_registry"],
            pending_platform_rom_ids,
            pending_collection_memberships,
        )

        self._state["last_sync"] = self._clock.now().isoformat()
        self._state["last_synced_collections"] = list(pending_collection_memberships.keys())
        self._state["last_synced_platforms"] = list(platform_app_ids.keys())
        self._state_persister.save_state()

        return platform_app_ids, romm_collection_app_ids

    async def finalize_per_unit_run(
        self,
        pending_collection_memberships: dict[str, list[int]],
        pending_platform_rom_ids: set[int] | None,
        total_games: int,
        cancelled: bool = False,
    ):
        """Emit ``sync_collections`` + ``sync_complete`` after all units finish.

        Stale-removal is emitted separately by the orchestrator via
        ``sync_stale`` so the frontend can apply removals before
        collections are recomputed.
        """
        platform_app_ids, romm_collection_app_ids = await self._loop.run_in_executor(
            None,
            self._finalize_per_unit_run_io,
            pending_collection_memberships,
            pending_platform_rom_ids,
        )

        await self._emit(
            "sync_collections",
            {
                "platform_app_ids": platform_app_ids,
                "romm_collection_app_ids": romm_collection_app_ids,
            },
        )

        complete_payload = {
            "platform_app_ids": platform_app_ids,
            "romm_collection_app_ids": romm_collection_app_ids,
            "total_games": total_games,
        }
        if cancelled:
            complete_payload["cancelled"] = True
        await self._emit("sync_complete", complete_payload)

        total = len(self._state["shortcut_registry"])
        if cancelled:
            await self._emit_progress(
                "done",
                current=total_games,
                total=total,
                message=f"Sync cancelled: {total_games} of {total} games processed",
                running=False,
            )
        else:
            await self._emit_progress(
                "done",
                current=total,
                total=total,
                message=f"Sync complete: {total} games from {len(platform_app_ids)} platforms",
                running=False,
            )

        self._sync_state.sync_state = SyncState.IDLE
        self._sync_state.current_sync_id = None
        return platform_app_ids, romm_collection_app_ids

    # ── Report unit results (per-unit pipeline) ──────────────────

    def _commit_unit_results_io(self, rom_id_to_app_id):
        """Sync helper: finalise artwork + registry for one unit, persist state."""
        grid = self._steam_config.grid_dir()
        box = self._sync_state

        for rom_id_str, app_id in rom_id_to_app_id.items():
            pending = box.pending_sync.get(int(rom_id_str), {})
            cover_path = self._finalize_cover_path(grid, pending.get("cover_path", ""), app_id, rom_id_str)
            self._registry_store.apply_sync(
                RegistrySyncApplyPatch(
                    rom_id_str=rom_id_str,
                    app_id=app_id,
                    name=pending.get("name", ""),
                    fs_name=pending.get("fs_name", ""),
                    platform_name=pending.get("platform_name", ""),
                    platform_slug=pending.get("platform_slug", ""),
                    cover_path=cover_path,
                    igdb_id=pending.get("igdb_id"),
                    sgdb_id=pending.get("sgdb_id"),
                    ra_id=pending.get("ra_id"),
                )
            )

        steam_input_mode = self._settings.get("steam_input_mode", "default")
        if steam_input_mode != "default" and rom_id_to_app_id:
            try:
                self._steam_config.set_steam_input_config(
                    [int(aid) for aid in rom_id_to_app_id.values()], mode=steam_input_mode
                )
            except Exception as e:
                self._logger.error(f"Failed to set Steam Input config: {e}")

        # Crash-safe checkpoint: persist after every unit so a kill between
        # units preserves every prior unit's work in the registry.
        self._state_persister.save_state()

    async def report_unit_results(self, rom_id_to_app_id):
        """Frontend-Callable: ack that this unit's shortcuts have been applied.

        Records the rom_id→app_id mapping into the state box and signals
        the orchestrator's per-unit wait event. The orchestrator drives
        the actual per-unit commit (metadata-cache stamp + registry
        update + state persist) after this returns, so the write order
        is metadata-first then state.
        """
        box = self._sync_state
        box.last_unit_results = dict(rom_id_to_app_id)
        if box.unit_complete_event is not None:
            box.unit_complete_event.set()

        self._logger.info(f"Unit results acknowledged: {len(rom_id_to_app_id)} shortcuts")
        return {"success": True, "count": len(rom_id_to_app_id)}

    async def commit_unit_results(self, rom_id_to_app_id):
        """Per-unit commit: cover-path finalize, registry update, state save.

        Called by the orchestrator after metadata-cache stamping for the
        unit completes. This is the second half of the per-unit write
        transaction (metadata first, state second — crash-safe order).
        """
        await self._loop.run_in_executor(None, self._commit_unit_results_io, rom_id_to_app_id)

    # ── Registry queries ─────────────────────────────────────────

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

    # ── Cache / stats ────────────────────────────────────────────

    def clear_sync_cache(self):
        """Clear last_sync timestamp to force a full re-fetch on next sync."""
        self._state["last_sync"] = None
        self._state_persister.save_state()
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
