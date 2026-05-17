"""Preview / apply / per-unit sync lifecycle and the heartbeat clock.

Owns every async path the user triggers from the QAM that mutates
in-flight sync state: starting and cancelling syncs, prefetching
units for a preview, and dispatching the per-unit sync pipeline on
apply. The heartbeat clock — refreshed on every progress emission
and inspected by per-unit waits — lives here too. Progress emission
also lives here — sub-services that need to surface progress receive
the orchestrator's ``_emit_progress`` callback through their config.
Anything that fetches ROMs belongs in :class:`LibraryFetcher`;
anything that finalises shortcuts after the apply completes belongs
in :class:`SyncReporter`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.preview_delta import PreviewDelta
from domain.shortcut_data import build_shortcuts_data
from domain.sync_diff import (
    classify_roms,
    compute_collection_diff,
    compute_platform_collection_diff,
)
from domain.sync_state import SyncState
from domain.work_unit import WorkUnit
from lib.errors import classify_error
from services.library._state import LibrarySyncStateBox, PrefetchedUnit

if TYPE_CHECKING:
    import logging

    from services.library.fetcher import LibraryFetcher
    from services.library.reporter import SyncReporter
    from services.protocols import (
        ArtworkManager,
        Clock,
        EventEmitter,
        MetadataExtractor,
        Sleeper,
        StatePersister,
        UuidGen,
    )


_SYNC_CANCELLED = "Sync cancelled"
_PREVIEW_MAX_AGE_SECONDS = 1800  # 30 minutes — preview snapshots stale beyond this

# Per-unit heartbeat-based timeout. If the frontend stops calling
# ``sync_heartbeat`` for this many seconds while the orchestrator is
# waiting for ``report_unit_results``, the wait is treated as a
# recoverable cancellation — the in-flight unit is dropped and the
# next sync resumes via the incremental-skip path.
_UNIT_HEARTBEAT_TIMEOUT_SEC = 60.0
# Polling cadence the wait loop uses while watching the heartbeat
# clock. Kept short so cancel propagation feels responsive without
# burning CPU.
_UNIT_WAIT_POLL_SEC = 1.0


@dataclass(frozen=True)
class SyncOrchestratorConfig:
    """Frozen wiring bundle handed to ``SyncOrchestrator.__init__``.

    Holds the live state dict (read for the existing-registry stale
    diff), runtime infrastructure (loop, logger), event emitter, the
    Clock/UuidGen/Sleeper test seams, state-persistence callback, the
    shared :class:`LibrarySyncStateBox`, and three peer references the
    orchestrator drives at runtime: the :class:`LibraryFetcher` it
    delegates prefetching and per-unit fetches to, an optional
    :class:`ArtworkManager` for the apply-phase artwork download, and
    an optional :class:`MetadataExtractor` it asks to flush its dirty
    metadata cache during the sync ``finally``.
    """

    state: dict
    settings: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: EventEmitter
    clock: Clock
    uuid_gen: UuidGen
    sleeper: Sleeper
    state_persister: StatePersister
    sync_state_box: LibrarySyncStateBox
    fetcher: LibraryFetcher
    metadata_service: MetadataExtractor | None = None
    artwork: ArtworkManager | None = None
    # Late-bound reporter reference — the per-unit pipeline emits its
    # final ``sync_complete`` / ``sync_collections`` through the reporter
    # so the same registry-driven collection-mapping code path is reused.
    # Optional because the orchestrator is constructed before the
    # reporter exists in :class:`LibraryService`; the façade injects the
    # binding immediately after the reporter is built.
    reporter: SyncReporter | None = None


class SyncOrchestrator:
    """Preview/apply/full-sync lifecycle with cancellation + heartbeat safety."""

    def __init__(self, *, config: SyncOrchestratorConfig) -> None:
        self._state = config.state
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._clock = config.clock
        self._uuid_gen = config.uuid_gen
        self._sleeper = config.sleeper
        self._state_persister = config.state_persister
        self._sync_state = config.sync_state_box
        self._fetcher = config.fetcher
        self._metadata_service = config.metadata_service
        self._artwork = config.artwork
        self._reporter = config.reporter

    # ── Sync control ─────────────────────────────────────────────

    def start_sync(self):
        box = self._sync_state
        if box.sync_state != SyncState.IDLE:
            return {"success": False, "message": "Sync already in progress"}
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = self._uuid_gen.uuid4()
        box.sync_last_heartbeat = self._clock.monotonic()
        # Skip Preview ON path: any cache left from a prior preview is
        # stale by definition — start_sync refetches from scratch via
        # _do_sync_per_unit's no-prefetched branch.
        box.pending_prefetched_units = None
        self._loop.create_task(self._do_sync_per_unit())
        return {"success": True, "message": "Sync started"}

    def cancel_sync(self):
        box = self._sync_state
        if box.sync_state != SyncState.RUNNING:
            return {"success": True, "message": "No sync in progress"}
        box.sync_state = SyncState.CANCELLING
        return {"success": True, "message": "Sync cancelling..."}

    def sync_heartbeat(self):
        """Called by frontend during shortcut application to refresh the per-unit heartbeat clock."""
        self._sync_state.sync_last_heartbeat = self._clock.monotonic()
        return {"success": True}

    def shutdown(self) -> None:
        """Request graceful shutdown — cancels sync if running."""
        box = self._sync_state
        if box.sync_state == SyncState.RUNNING:
            box.sync_state = SyncState.CANCELLING

    # ── Preview / Apply ──────────────────────────────────────────

    async def sync_preview(self):
        box = self._sync_state
        if box.sync_state != SyncState.IDLE:
            return {"success": False, "message": "Sync already in progress"}
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = self._uuid_gen.uuid4()
        box.sync_last_heartbeat = self._clock.monotonic()
        # New preview always invalidates a prior cache — the user may
        # have changed enabled platforms/collections or the RomM library
        # may have shifted underneath them.
        box.pending_prefetched_units = None
        try:
            (
                prefetched,
                all_roms,
                shortcuts_data,
                collection_memberships,
                platform_rom_ids,
            ) = await self._fetcher.prefetch_all_units()
            platform_names = {u.unit.name for u in prefetched if u.unit.type == "platform"}
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
            platforms_count = sum(1 for u in prefetched if u.unit.type == "platform")
            box.pending_delta = PreviewDelta(
                preview_id=preview_id,
                created_at=self._clock.time(),
                new=new,
                changed=changed,
                unchanged_ids=unchanged_ids,
                remove_rom_ids=stale,
                all_shortcuts={sd["rom_id"]: sd for sd in shortcuts_data},
                delta_roms=delta_roms,
                platforms_count=platforms_count,
                total_roms=len(all_roms),
                collection_memberships=collection_memberships,
                platform_rom_ids=platform_rom_ids,
            )
            box.pending_prefetched_units = prefetched

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
            box.pending_prefetched_units = None
            await self._finish_sync(_SYNC_CANCELLED)
            raise
        except Exception as e:
            import traceback

            self._logger.error(f"Sync preview failed: {e}\n{traceback.format_exc()}")
            box.pending_prefetched_units = None
            _code, _msg = classify_error(e)
            await self._emit_progress("error", message=_msg, running=False)
            return {"success": False, "message": _msg, "error_code": _code}
        finally:
            box.sync_state = SyncState.IDLE

    async def sync_apply_delta(self, preview_id):
        box = self._sync_state
        if not box.pending_delta or box.pending_delta.preview_id != preview_id:
            return {"success": False, "message": "Preview expired, please re-sync", "error_code": "stale_preview"}
        age = self._clock.time() - box.pending_delta.created_at
        if age > _PREVIEW_MAX_AGE_SECONDS:
            box.pending_delta = None
            box.pending_prefetched_units = None
            return {
                "success": False,
                "message": "Preview is older than 30 minutes, please re-run sync",
                "error_code": "stale_preview",
            }
        delta = box.pending_delta
        prefetched = box.pending_prefetched_units
        box.pending_delta = None
        # Take the cache out of the box before dispatch so a concurrent
        # cancel/error path can't double-consume it; the per-unit driver
        # owns the list for the lifetime of this apply.
        box.pending_prefetched_units = None
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = self._uuid_gen.uuid4()
        box.sync_last_heartbeat = self._clock.monotonic()

        # Update sync_stats up-front so ``get_sync_stats`` and any
        # subsequent shortcut-removal pass see the apply's intended
        # counts even if the per-unit dispatch later stalls.
        self._state["sync_stats"] = {
            "platforms": delta.platforms_count,
            "roms": delta.total_roms,
        }
        self._state_persister.save_state()

        if prefetched is None:
            # Cache was lost (server restart, race, etc.) — log a warning
            # and let _do_sync_per_unit refetch from scratch so the user
            # still gets a successful sync rather than a hard failure.
            self._logger.warning("sync_apply_delta: prefetch cache empty, falling back to fresh fetch")

        self._loop.create_task(self._do_sync_per_unit(prefetched=prefetched))

        return {"success": True, "message": "Applying changes"}

    def sync_cancel_preview(self):
        self._sync_state.pending_delta = None
        self._sync_state.pending_prefetched_units = None
        return {"success": True}

    # ── Progress & safety ────────────────────────────────────────

    async def _emit_progress(self, phase, current=0, total=0, message="", running=True, step=0, total_steps=0):
        """Update _sync_progress and emit sync_progress event to frontend."""
        self._sync_state.sync_progress = {
            "running": running,
            "phase": phase,
            "current": current,
            "total": total,
            "message": message,
            "step": step,
            "totalSteps": total_steps,
        }
        await self._emit("sync_progress", self._sync_state.sync_progress)

    # ── Sync termination ─────────────────────────────────────────

    async def _finish_sync(self, message):
        box = self._sync_state
        box.sync_progress = {
            "running": False,
            "phase": "cancelled",
            "current": box.sync_progress.get("current", 0),
            "total": box.sync_progress.get("total", 0),
            "message": message,
        }
        await self._emit("sync_progress", box.sync_progress)
        box.sync_state = SyncState.IDLE
        box.current_sync_id = None
        self._logger.info(message)

    # ── Per-unit pipeline ────────────────────────────────────────

    async def _do_sync_per_unit(self, prefetched: list[PrefetchedUnit] | None = None):
        """Per-unit sync pipeline (Phase 0 + per-unit dispatch + finalize).

        Replaces the monolithic all-platforms-then-all-shortcuts flow:
        builds a work queue, processes each platform/collection unit
        to completion (fetch -> shortcuts -> artwork -> apply ->
        registry update) before moving on, then emits stale-removal +
        Steam-collection mappings + ``sync_complete`` at the end. Each
        completed unit is a crash-safe checkpoint in the on-disk
        registry.

        When ``prefetched`` is supplied, the work queue is taken from
        the cached units and each unit's ROMs come from the cache —
        used by the Skip Preview OFF path so apply does not refetch the
        library after preview has already paginated it.
        """
        box = self._sync_state
        # Cross-unit accumulators — built up unit-by-unit, consumed by the
        # final phase. ``synced_rom_ids`` is shared with collection units
        # for dedup. ``all_rom_id_to_app_id`` aggregates every unit's
        # frontend-reported mapping for stale detection. ``collection_
        # memberships`` and ``platform_rom_ids`` feed the reporter's
        # ``_build_collection_app_ids`` once every unit has been applied.
        synced_rom_ids: set[int] = set()
        all_rom_id_to_app_id: dict[str, int] = {}
        collection_memberships: dict[str, list[int]] = {}
        platform_rom_ids: set[int] = set()
        total_games_applied = 0
        cancelled = False

        try:
            work_queue: list[WorkUnit]
            prefetched_by_unit: dict[WorkUnit, PrefetchedUnit] = {}
            if prefetched is not None:
                work_queue = [pu.unit for pu in prefetched]
                prefetched_by_unit = {pu.unit: pu for pu in prefetched}
            else:
                try:
                    work_queue = await self._fetcher.build_work_queue()
                except asyncio.CancelledError:
                    await self._finish_sync(_SYNC_CANCELLED)
                    raise
                except Exception as e:
                    self._logger.error(f"Failed to build work queue: {e}")
                    _code, _msg = classify_error(e)
                    await self._emit_progress("error", message=_msg, running=False)
                    box.sync_state = SyncState.IDLE
                    return

            total_units = len(work_queue)
            total_roms_planned = sum(u.rom_count for u in work_queue)
            self._logger.info(f"Per-unit pipeline: {total_units} units planned, {total_roms_planned} ROMs total")
            await self._emit(
                "sync_plan",
                {
                    "units": [u.to_event_payload() for u in work_queue],
                    "total_units": total_units,
                    "total_roms": total_roms_planned,
                },
            )

            if total_units == 0:
                await self._emit_progress("done", message="Nothing to sync", running=False)
                box.sync_state = SyncState.IDLE
                box.current_sync_id = None
                return

            for unit_index, unit in enumerate(work_queue):
                if box.sync_state == SyncState.CANCELLING:
                    cancelled = True
                    break

                applied = await self._sync_one_unit(
                    unit,
                    unit_index=unit_index,
                    total_units=total_units,
                    synced_rom_ids=synced_rom_ids,
                    collection_memberships=collection_memberships,
                    platform_rom_ids=platform_rom_ids,
                    all_rom_id_to_app_id=all_rom_id_to_app_id,
                    prefetched=prefetched_by_unit.get(unit),
                )
                total_games_applied += applied

                if box.sync_state == SyncState.CANCELLING:
                    cancelled = True
                    break

            # Final phase: stale cleanup + Steam collections + sync_complete.
            await self._finalize_per_unit(
                total_games_applied=total_games_applied,
                synced_rom_ids=synced_rom_ids,
                collection_memberships=collection_memberships,
                platform_rom_ids=platform_rom_ids,
                cancelled=cancelled,
            )
        except Exception as e:
            import traceback

            self._logger.error(f"Per-unit sync failed: {e}\n{traceback.format_exc()}")
            _code, _msg = classify_error(e)
            box.sync_progress = {
                "running": False,
                "phase": "error",
                "current": 0,
                "total": 0,
                "message": f"Sync failed — {_msg}",
            }
            self._loop.create_task(self._emit("sync_progress", box.sync_progress))
            box.sync_state = SyncState.IDLE
        finally:
            if self._metadata_service is not None:
                self._metadata_service.flush_metadata_if_dirty()

    async def _sync_one_unit(
        self,
        unit: WorkUnit,
        *,
        unit_index: int,
        total_units: int,
        synced_rom_ids: set[int],
        collection_memberships: dict[str, list[int]],
        platform_rom_ids: set[int],
        all_rom_id_to_app_id: dict[str, int],
        prefetched: PrefetchedUnit | None = None,
    ) -> int:
        """Process one work unit start-to-finish; return shortcuts applied.

        When ``prefetched`` is supplied (Skip Preview OFF path), this
        unit's ROMs come from the cached preview result and no extra
        RomM round trip happens. When ``prefetched`` is ``None`` (Skip
        Preview ON or cache-fallback path), the unit's ROMs are fetched
        on demand via the per-unit fetcher.
        """
        box = self._sync_state
        await self._emit_progress(
            "unit",
            current=unit_index,
            total=total_units,
            message=f"{unit.name} ({unit_index + 1}/{total_units})",
        )

        # Fetch (or replay) this unit's ROMs. Platform units may
        # incremental-skip; collection units always paginate (collection
        # membership is the source of truth, no per-collection
        # "last_sync" gate today).
        if unit.type == "platform":
            if prefetched is not None:
                unit_roms = prefetched.roms
                skipped = prefetched.skipped
            else:
                unit_roms, skipped = await self._fetcher.fetch_platform_unit(unit)
            platform_rom_ids.update(r["id"] for r in unit_roms)
            synced_rom_ids.update(r["id"] for r in unit_roms)
        else:
            skipped = False
            if prefetched is not None:
                unit_roms = prefetched.roms
                all_collection_rom_ids = prefetched.all_collection_rom_ids or []
                synced_rom_ids.update(r["id"] for r in unit_roms)
            else:
                unit_roms, all_collection_rom_ids = await self._fetcher.fetch_collection_unit(unit, synced_rom_ids)
            if all_collection_rom_ids:
                collection_memberships[unit.name] = all_collection_rom_ids

        if box.sync_state == SyncState.CANCELLING:
            return 0

        # Build shortcut data + cache metadata for this unit only.
        # When ``prefetched`` is set the preview path already stamped
        # the metadata cache for every ROM in the run; skip the
        # redundant re-stamp.
        shortcuts_data = build_shortcuts_data(unit_roms, self._fetcher._plugin_dir)
        if prefetched is None:
            self._fetcher.cache_metadata_for_unit(unit_roms)

        # Download artwork for this unit (skipped if the incremental
        # path already populated cover_path from the registry).
        if not skipped and unit_roms:
            cover_paths = await self._download_artwork(
                unit_roms, progress_step=unit_index + 1, progress_total_steps=total_units
            )
            for sd in shortcuts_data:
                sd["cover_path"] = cover_paths.get(sd["rom_id"], "")

        if box.sync_state == SyncState.CANCELLING:
            return 0

        # Stage pending_sync for this unit so report_unit_results can
        # finalise cover paths + build registry entries against it.
        box.pending_sync = {sd["rom_id"]: sd for sd in shortcuts_data}

        # Emit per-unit apply event + wait for the frontend callback.
        box.unit_complete_event = asyncio.Event()
        box.last_unit_results = None
        box.sync_last_heartbeat = self._clock.monotonic()
        await self._emit(
            "sync_apply_unit",
            {
                "unit_type": unit.type,
                "unit_id": unit.id,
                "unit_name": unit.name,
                "unit_index": unit_index,
                "total_units": total_units,
                "shortcuts": shortcuts_data,
            },
        )

        applied = await self._wait_for_unit_complete(unit, box.unit_complete_event)
        if applied is None:
            # Heartbeat timeout or cancel — drop the unit's pending state
            # and surface the cancellation. The orchestrator's outer loop
            # observes CANCELLING and stops.
            box.pending_sync = {}
            box.unit_complete_event = None
            box.sync_state = SyncState.CANCELLING
            return 0

        # Reporter has already updated the registry + persisted state via
        # report_unit_results — mirror the rom_id_to_app_id into the
        # cross-run accumulator for stale + Steam-collection mapping.
        all_rom_id_to_app_id.update(applied)
        box.pending_sync = {}
        box.unit_complete_event = None
        return len(applied)

    async def _wait_for_unit_complete(self, unit: WorkUnit, event: asyncio.Event) -> dict[str, int] | None:
        """Heartbeat-based wait for the active unit's frontend callback.

        Returns the frontend-reported ``rom_id_to_app_id`` on success.
        Returns ``None`` on timeout or cancel — the outer loop maps that
        onto a recoverable cancellation. The wait poll polls the
        heartbeat clock rather than ``asyncio.wait_for(timeout=...)``
        because the frontend sends ``sync_heartbeat`` calls during long
        per-unit applies (artwork download, Set* calls) and a 60s
        absolute cap would still race those.
        """
        box = self._sync_state
        while not event.is_set():
            if box.sync_state == SyncState.CANCELLING:
                self._logger.info(f"Per-unit cancel observed while waiting for unit {unit.name}")
                return None
            elapsed = self._clock.monotonic() - box.sync_last_heartbeat
            if elapsed > _UNIT_HEARTBEAT_TIMEOUT_SEC:
                self._logger.warning(f"Per-unit timeout: no heartbeat for {elapsed:.0f}s waiting on unit {unit.name}")
                return None
            try:
                await self._sleeper.sleep(_UNIT_WAIT_POLL_SEC)
            except asyncio.CancelledError:
                self._logger.info(f"Per-unit wait cancelled for unit {unit.name}")
                raise

        results = box.last_unit_results or {}
        box.last_unit_results = None
        return results

    async def _finalize_per_unit(
        self,
        *,
        total_games_applied: int,
        synced_rom_ids: set[int],
        collection_memberships: dict[str, list[int]],
        platform_rom_ids: set[int],
        cancelled: bool,
    ):
        """Emit stale-removal, collection mappings, and the terminal sync_complete."""
        # Stale rom_ids: anything in the registry whose rom_id wasn't seen
        # by any processed unit. Only meaningful on a non-cancelled run —
        # a partial run can't tell "stale" from "didn't get to it yet".
        if not cancelled:
            stale_rom_ids = [
                int(rid) for rid in self._state.get("shortcut_registry", {}) if int(rid) not in synced_rom_ids
            ]
        else:
            stale_rom_ids = []
        await self._emit("sync_stale", {"remove_rom_ids": stale_rom_ids})

        if self._reporter is not None:
            await self._reporter.finalize_per_unit_run(
                pending_collection_memberships=collection_memberships,
                pending_platform_rom_ids=platform_rom_ids,
                total_games=total_games_applied,
                cancelled=cancelled,
            )

    # ── Artwork delegation ───────────────────────────────────────

    async def _download_artwork(self, all_roms, progress_step=4, progress_total_steps=6):
        """Delegate artwork download to ArtworkService callback."""
        box = self._sync_state
        if self._artwork is not None:
            return await self._artwork.download_artwork(
                all_roms,
                emit_progress=self._emit_progress,
                is_cancelling=lambda: box.sync_state == SyncState.CANCELLING,
                progress_step=progress_step,
                progress_total_steps=progress_total_steps,
            )
        return {}
