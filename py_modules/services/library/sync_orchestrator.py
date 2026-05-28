"""Preview / apply / per-unit sync lifecycle and the heartbeat clock.

Owns every async path the user triggers from the QAM that mutates
in-flight sync state: starting and cancelling syncs, computing a
preview (read-only), and dispatching the per-unit sync pipeline on
apply. The heartbeat clock — refreshed on every progress emission and
inspected by per-unit waits — lives here too. Progress emission also
lives here — sub-services that need to surface progress receive the
orchestrator's ``emit_progress`` callback through their config.
Anything that fetches ROMs belongs in :class:`LibraryFetcher`;
anything that finalises shortcuts after the apply completes belongs
in :class:`SyncReporter`. The metadata-cache is only stamped per
applied unit (by :class:`MetadataExtractor.record_unit_metadata`), so
preview never mutates the cache and an interrupted apply leaves only
already-applied units stamped.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.state import PluginState

from domain.preview_delta import PreviewDelta
from domain.shortcut_data import build_shortcuts_data
from domain.sync_diff import (
    classify_roms,
    compute_collection_diff,
    compute_platform_collection_diff,
)
from domain.sync_stage import SyncStage
from domain.sync_state import SyncState
from domain.work_unit import WorkUnit
from lib.errors import classify_error
from lib.late_binding import LateBinding
from services.library._state import LibrarySyncStateBox

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
    plugin-dir reference for shortcut data construction, the shared
    :class:`LibrarySyncStateBox`, and three peer references the
    orchestrator drives at runtime: the :class:`LibraryFetcher` it
    delegates per-unit fetches to, an :class:`ArtworkManager` for the
    apply-phase artwork download, and a :class:`MetadataExtractor` it
    asks to stamp the metadata cache per applied unit. The ``reporter``
    field is a :class:`LateBinding` because :class:`LibraryService`
    constructs the orchestrator before the reporter exists; the façade
    plugs the reader in via ``set()`` once the reporter is built.
    """

    state: PluginState
    settings: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    emit: EventEmitter
    clock: Clock
    uuid_gen: UuidGen
    sleeper: Sleeper
    state_persister: StatePersister
    sync_state_box: LibrarySyncStateBox
    fetcher: LibraryFetcher
    reporter: LateBinding[SyncReporter]
    metadata_service: MetadataExtractor
    artwork: ArtworkManager


class SyncOrchestrator:
    """Preview/apply/full-sync lifecycle with cancellation + heartbeat safety."""

    def __init__(self, *, config: SyncOrchestratorConfig) -> None:
        self._state = config.state
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._plugin_dir = config.plugin_dir
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
        """Read-only preview: paginate every unit, classify, return the summary.

        Does NOT mutate the metadata cache — the metadata stamp happens
        per applied unit, after the frontend acknowledges shortcuts for
        that unit. Stamping during preview would erase populated entries
        with the registry-reconstructed thin ROMs from the per-unit
        incremental-skip path (#738).
        """
        box = self._sync_state
        if box.sync_state != SyncState.IDLE:
            return {"success": False, "message": "Sync already in progress"}
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = self._uuid_gen.uuid4()
        box.sync_last_heartbeat = self._clock.monotonic()
        try:
            await self.emit_progress(SyncStage.DISCOVERING, message="Fetching platforms...")
            work_queue = await self._fetcher.build_work_queue()

            all_roms: list[dict] = []
            platform_rom_ids: set[int] = set()
            collection_memberships: dict[str, list[int]] = {}
            synced_rom_ids: set[int] = set()

            total_units = len(work_queue)
            for unit_index, unit in enumerate(work_queue, 1):
                if box.sync_state == SyncState.CANCELLING:
                    raise asyncio.CancelledError(_SYNC_CANCELLED)
                await self.emit_progress(
                    SyncStage.FETCHING,
                    current=len(all_roms),
                    message=f"Fetching {unit.name}... ({unit_index}/{total_units})",
                    step=unit_index,
                    total_steps=total_units,
                )
                if unit.type == "platform":
                    unit_roms, _skipped = await self._fetcher.fetch_platform_unit(unit)
                    for rom in unit_roms:
                        platform_rom_ids.add(rom["id"])
                        synced_rom_ids.add(rom["id"])
                    all_roms.extend(unit_roms)
                else:
                    unit_roms, all_collection_rom_ids = await self._fetcher.fetch_collection_unit(unit, synced_rom_ids)
                    if all_collection_rom_ids:
                        collection_memberships[unit.name] = all_collection_rom_ids
                    all_roms.extend(unit_roms)

            shortcuts_data = build_shortcuts_data(all_roms, self._plugin_dir)
            platform_names = {u.name for u in work_queue if u.type == "platform"}
            new, changed, unchanged_ids, stale, disabled_count = classify_roms(
                shortcuts_data,
                self._state["shortcut_registry"],
                platform_names,
            )

            preview_id = self._uuid_gen.uuid4()
            platforms_count = sum(1 for u in work_queue if u.type == "platform")
            box.pending_delta = PreviewDelta(
                preview_id=preview_id,
                created_at=self._clock.time(),
                platforms_count=platforms_count,
                total_roms=len(all_roms),
            )

            await self.emit_progress(SyncStage.DONE, message="Preview ready", running=False)

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
            box.pending_delta = None
            await self._finish_sync(_SYNC_CANCELLED)
            raise
        except Exception as e:
            import traceback

            self._logger.error(f"Sync preview failed: {e}\n{traceback.format_exc()}")
            box.pending_delta = None
            _code, _msg = classify_error(e)
            await self.emit_progress(SyncStage.ERROR, message=_msg, running=False)
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
            return {
                "success": False,
                "message": "Preview is older than 30 minutes, please re-run sync",
                "error_code": "stale_preview",
            }
        delta = box.pending_delta
        box.pending_delta = None
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

        self._loop.create_task(self._do_sync_per_unit())

        return {"success": True, "message": "Applying changes"}

    def sync_cancel_preview(self):
        self._sync_state.pending_delta = None
        return {"success": True}

    # ── Progress & safety ────────────────────────────────────────

    async def emit_progress(self, stage, current=0, total=0, message="", running=True, step=0, total_steps=0):
        """Persist the progress snapshot and emit the sync_progress event.

        ``stage`` is a :class:`SyncStage` (or its string value); ``step``
        / ``total_steps`` are the coarse unit index / total units that
        drive the determinate main bar — stages without a unit index yet
        (discovering, fetching) pass ``0`` / ``0``, which the frontend
        treats as indeterminate. ``current`` / ``total`` are the fine
        within-unit counters. The snapshot is written to the box first so
        :meth:`get_sync_status` always returns the latest state even if
        the event never reaches a freshly remounted QAM.
        """
        self._sync_state.sync_progress = {
            "running": running,
            "stage": SyncStage(stage).value,
            "current": current,
            "total": total,
            "message": message,
            "step": step,
            "totalSteps": total_steps,
        }
        await self._emit("sync_progress", self._sync_state.sync_progress)

    def get_sync_status(self) -> dict:
        """Return the persisted progress snapshot — the authoritative sync state.

        Idle returns the default ``running: False`` snapshot; a live run
        returns the latest snapshot written by :meth:`emit_progress`.
        """
        return self._sync_state.sync_progress

    # ── Sync termination ─────────────────────────────────────────

    async def _finish_sync(self, message):
        box = self._sync_state
        box.sync_progress = {
            "running": False,
            "stage": SyncStage.CANCELLED.value,
            "current": box.sync_progress.get("current", 0),
            "total": box.sync_progress.get("total", 0),
            "message": message,
            "step": box.sync_progress.get("step", 0),
            "totalSteps": box.sync_progress.get("totalSteps", 0),
        }
        await self._emit("sync_progress", box.sync_progress)
        box.sync_state = SyncState.IDLE
        box.current_sync_id = None
        self._logger.info(message)

    # ── Per-unit pipeline ────────────────────────────────────────

    async def _do_sync_per_unit(self):
        """Per-unit sync pipeline (Phase 0 + per-unit dispatch + finalize).

        Builds a work queue, processes each platform/collection unit to
        completion (fetch -> shortcuts -> artwork -> apply -> per-unit
        metadata stamp + registry update) before moving on, then emits
        stale-removal + Steam-collection mappings + ``sync_complete``
        at the end. Each completed unit is a crash-safe checkpoint:
        metadata cache is written first, state second, both atomically
        — an orphaned metadata stamp after a crash is harmless; the
        reverse order would re-introduce the #738 cache wipe.
        """
        box = self._sync_state
        # Cross-unit accumulators — built up unit-by-unit, consumed by the
        # final phase. ``synced_rom_ids`` is shared with collection units
        # for dedup. ``collection_memberships`` and ``platform_rom_ids``
        # feed the reporter's ``_build_collection_app_ids`` once every
        # unit has been applied.
        synced_rom_ids: set[int] = set()
        collection_memberships: dict[str, list[int]] = {}
        platform_rom_ids: set[int] = set()
        total_games_applied = 0
        cancelled = False

        try:
            try:
                work_queue = await self._fetcher.build_work_queue()
            except asyncio.CancelledError:
                await self._finish_sync(_SYNC_CANCELLED)
                raise
            except Exception as e:
                self._logger.error(f"Failed to build work queue: {e}")
                _code, _msg = classify_error(e)
                await self.emit_progress(SyncStage.ERROR, message=_msg, running=False)
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
                await self.emit_progress(SyncStage.DONE, message="Nothing to sync", running=False)
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
                )
                total_games_applied += applied

                if box.sync_state == SyncState.CANCELLING:
                    cancelled = True
                    break

            # Final phase: stale cleanup + Steam collections + sync_complete.
            # Surface a non-terminal finalizing snapshot before the terminal
            # done/cancelled emit so the bar stays full while the reporter
            # commits collections. Cancelled runs skip it — their next emit
            # is the terminal cancelled snapshot from the reporter.
            if not cancelled:
                await self.emit_progress(
                    SyncStage.FINALIZING,
                    message="Finalizing…",
                    step=total_units,
                    total_steps=total_units,
                )
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
                "stage": SyncStage.ERROR.value,
                "current": 0,
                "total": 0,
                "message": f"Sync failed — {_msg}",
                "step": 0,
                "totalSteps": 0,
            }
            self._loop.create_task(self._emit("sync_progress", box.sync_progress))
            box.sync_state = SyncState.IDLE
        finally:
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
    ) -> int:
        """Process one work unit start-to-finish; return shortcuts applied.

        The ROMs for the unit come from a live per-unit fetch. After the
        frontend acks the unit's shortcuts (via ``report_unit_results``),
        the metadata cache is stamped for the acked ROMs *before* the
        reporter commits the registry update — that ordering guarantees
        any crash between the two leaves harmless orphan metadata
        rather than orphan registry entries pointing at unstamped
        metadata (#738).

        When the fetcher reports ``skipped=True`` (registry already
        matches the server-side platform state), the entire apply +
        commit branch is short-circuited: no frontend roundtrip, no
        registry write. The unit's ROMs still count toward the
        ``total_games_applied`` total returned to the user.
        """
        box = self._sync_state
        await self.emit_progress(
            SyncStage.APPLYING,
            message=f"{unit.name} ({unit_index + 1}/{total_units})",
            step=unit_index + 1,
            total_steps=total_units,
        )

        # Fetch this unit's ROMs. Platform units may incremental-skip;
        # collection units always paginate (collection membership is
        # the source of truth, no per-collection "last_sync" gate today).
        if unit.type == "platform":
            unit_roms, skipped = await self._sync_platform_unit(
                unit,
                synced_rom_ids=synced_rom_ids,
                platform_rom_ids=platform_rom_ids,
            )
        else:
            unit_roms, skipped = await self._sync_collection_unit(
                unit,
                synced_rom_ids=synced_rom_ids,
                collection_memberships=collection_memberships,
            )

        if box.sync_state == SyncState.CANCELLING:
            return 0

        # Per-unit incremental skip: registry already matches the
        # server-side state for this platform, so neither apply nor
        # commit have any work. Skip the frontend roundtrip and the
        # no-op two-phase commit. Force Full Sync clears ``last_sync``
        # upstream, so ``skipped`` is always False on forced runs.
        if skipped:
            self._logger.info(f"Per-unit apply skipped: {unit.name} ({len(unit_roms)} ROMs unchanged)")
            return len(unit_roms)

        # Build shortcut data for this unit.
        shortcuts_data = build_shortcuts_data(unit_roms, self._plugin_dir)

        # Download artwork for this unit. Empty unit_roms is a defensive
        # guard — an empty platform that survived planning still has no
        # artwork to fetch.
        if unit_roms:
            cover_paths = await self._download_artwork(
                unit_roms, progress_step=unit_index + 1, progress_total_steps=total_units
            )
            for sd in shortcuts_data:
                sd["cover_path"] = cover_paths.get(sd["rom_id"], "")

        if box.sync_state == SyncState.CANCELLING:
            return 0

        # Stage pending_sync for this unit so the reporter's commit step
        # can finalise cover paths + build registry entries against it.
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

        # Per-unit two-phase commit: stamp metadata cache for the acked
        # ROMs first, then commit the registry + persist state via the
        # reporter. The ordering matters across crashes: metadata-first
        # means an interrupted apply leaves only orphan metadata
        # (harmless, next sync re-stamps). Registry-first would leave
        # registry entries pointing at an unstamped (or freshly wiped)
        # cache (#738).
        acked_roms = [r for r in unit_roms if str(r["id"]) in applied]
        await self._loop.run_in_executor(None, self._metadata_service.record_unit_metadata, acked_roms)

        await self._reporter.get().commit_unit_results(applied)

        box.pending_sync = {}
        box.unit_complete_event = None
        return len(applied)

    async def _sync_platform_unit(
        self,
        unit: WorkUnit,
        *,
        synced_rom_ids: set[int],
        platform_rom_ids: set[int],
    ) -> tuple[list[dict], bool]:
        """Resolve ROMs for a platform unit and update cross-unit accumulators.

        Returns ``(unit_roms, skipped)`` for the caller's downstream
        shortcut + artwork + apply phases. ROMs come from a live per-unit
        fetch (no preview cache); the fetcher's incremental-skip path
        handles the "unchanged platform" optimisation internally.
        """
        unit_roms, skipped = await self._fetcher.fetch_platform_unit(unit)
        platform_rom_ids.update(r["id"] for r in unit_roms)
        synced_rom_ids.update(r["id"] for r in unit_roms)
        return unit_roms, skipped

    async def _sync_collection_unit(
        self,
        unit: WorkUnit,
        *,
        synced_rom_ids: set[int],
        collection_memberships: dict[str, list[int]],
    ) -> tuple[list[dict], bool]:
        """Resolve ROMs for a collection unit and record its membership.

        Returns ``(unit_roms, skipped)`` — ``skipped`` is always
        ``False`` because collection units have no incremental-skip
        gate today. ROMs come from a live per-unit fetch that already
        dedups against ``synced_rom_ids``.
        """
        skipped = False
        unit_roms, all_collection_rom_ids = await self._fetcher.fetch_collection_unit(unit, synced_rom_ids)
        if all_collection_rom_ids:
            collection_memberships[unit.name] = all_collection_rom_ids
        return unit_roms, skipped

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

        await self._reporter.get().finalize_per_unit_run(
            pending_collection_memberships=collection_memberships,
            pending_platform_rom_ids=platform_rom_ids,
            total_games=total_games_applied,
            cancelled=cancelled,
            stale_rom_ids=stale_rom_ids,
        )

    # ── Artwork delegation ───────────────────────────────────────

    async def _download_artwork(self, all_roms, progress_step=4, progress_total_steps=6):
        """Delegate artwork download to ArtworkService callback."""
        box = self._sync_state
        return await self._artwork.download_artwork(
            all_roms,
            emit_progress=self.emit_progress,
            is_cancelling=lambda: box.sync_state == SyncState.CANCELLING,
            progress_step=progress_step,
            progress_total_steps=progress_total_steps,
        )
