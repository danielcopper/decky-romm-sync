"""Preview / apply / full-sync lifecycle and the safety heartbeat.

Owns every async path the user triggers from the QAM that mutates
in-flight sync state: starting and cancelling syncs, building a
preview delta from a fetch result, applying that delta back via the
``sync_apply`` event, and the safety-timeout watchdog that closes
out stuck "applying" phases. Progress emission also lives here —
sub-services that need to surface progress receive the orchestrator's
``_emit_progress`` callback through their config. Anything that
fetches ROMs belongs in :class:`LibraryFetcher`; anything that
finalises shortcuts after the apply completes belongs in
:class:`SyncReporter`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.preview_delta import PreviewDelta
from domain.sync_diff import (
    classify_roms,
    compute_collection_diff,
    compute_platform_collection_diff,
)
from domain.sync_state import SyncState
from lib.errors import classify_error
from services.library._state import LibrarySyncStateBox

if TYPE_CHECKING:
    import logging

    from services.library.fetcher import LibraryFetcher
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


@dataclass(frozen=True)
class SyncOrchestratorConfig:
    """Frozen wiring bundle handed to ``SyncOrchestrator.__init__``.

    Holds the live state dict (read for the existing-registry stale
    diff), runtime infrastructure (loop, logger), event emitter, the
    Clock/UuidGen/Sleeper test seams, state-persistence callback, the
    shared :class:`LibrarySyncStateBox`, and three peer references the
    orchestrator drives at runtime: the :class:`LibraryFetcher` whose
    ``_fetch_and_prepare`` it consumes, an optional
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

    # ── Sync control ─────────────────────────────────────────────

    def start_sync(self):
        box = self._sync_state
        if box.sync_state != SyncState.IDLE:
            return {"success": False, "message": "Sync already in progress"}
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = self._uuid_gen.uuid4()
        box.sync_last_heartbeat = self._clock.monotonic()
        self._loop.create_task(self._do_sync())
        return {"success": True, "message": "Sync started"}

    def cancel_sync(self):
        box = self._sync_state
        if box.sync_state != SyncState.RUNNING:
            return {"success": True, "message": "No sync in progress"}
        box.sync_state = SyncState.CANCELLING
        return {"success": True, "message": "Sync cancelling..."}

    def sync_heartbeat(self):
        """Called by frontend during shortcut application to keep safety timeout alive."""
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
        try:
            fetch_result = await self._fetcher._fetch_and_prepare()
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
            box.pending_delta = PreviewDelta(
                preview_id=preview_id,
                created_at=self._clock.time(),
                new=new,
                changed=changed,
                unchanged_ids=unchanged_ids,
                remove_rom_ids=stale,
                all_shortcuts={sd["rom_id"]: sd for sd in shortcuts_data},
                delta_roms=delta_roms,
                platforms_count=len(platforms),
                total_roms=len(all_roms),
                collection_memberships=collection_memberships,
                platform_rom_ids=platform_rom_ids,
            )

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

        # Calculate apply step plan
        delta_roms = delta.delta_roms
        has_artwork = len(delta_roms) > 0
        has_shortcuts = len(delta.new) + len(delta.changed) > 0
        has_removals = len(delta.remove_rom_ids) > 0

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
            for sd in delta.new + delta.changed:
                sd["cover_path"] = cover_paths.get(sd["rom_id"], "")

        # Populate _pending_sync for report_sync_results and get_artwork_base64
        box.pending_sync = delta.all_shortcuts
        box.pending_collection_memberships = delta.collection_memberships
        box.pending_platform_rom_ids = delta.platform_rom_ids

        # Update sync_stats
        self._state["sync_stats"] = {
            "platforms": delta.platforms_count,
            "roms": delta.total_roms,
        }
        self._state_persister.save_state()

        # Figure out which step the frontend starts at
        next_step = current_step + 1

        total_changes = len(delta.new) + len(delta.changed)
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
                "shortcuts": delta.new,
                "changed_shortcuts": delta.changed,
                "remove_rom_ids": delta.remove_rom_ids,
                "next_step": next_step,
                "total_steps": total_steps,
            },
        )

        self._logger.info(
            f"Delta sync emitted: {len(delta.new)} new, {len(delta.changed)} changed, "
            f"{len(delta.remove_rom_ids)} removed"
        )

        # Heartbeat safety timeout
        self._start_safety_timeout()

        return {"success": True, "message": "Applying changes"}

    def sync_cancel_preview(self):
        self._sync_state.pending_delta = None
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

    def _start_safety_timeout(self, heartbeat_timeout_sec=30) -> asyncio.Task:
        """Launch a background task that auto-completes sync if no heartbeat arrives.

        Returns the spawned task so tests can deterministically await its
        completion; production callers ignore the return value.
        """
        box = self._sync_state
        box.sync_last_heartbeat = self._clock.monotonic()
        captured_sync_id = box.current_sync_id

        async def _safety_timeout():
            while box.sync_progress.get("running"):
                await self._sleeper.sleep(10)
                # Generation guard: if our sync has ended (cancel, error,
                # normal completion), current_sync_id was cleared or
                # replaced. Don't fire stale "done" or overwrite the new
                # sync state.
                if box.current_sync_id != captured_sync_id:
                    return
                elapsed = self._clock.monotonic() - box.sync_last_heartbeat
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
                    if box.current_sync_id != captured_sync_id:
                        return
                    box.sync_state = SyncState.IDLE
                    box.current_sync_id = None
                    return

        return self._loop.create_task(_safety_timeout())

    # ── Full sync ────────────────────────────────────────────────

    async def _do_sync(self):
        box = self._sync_state
        try:
            try:
                fetch_result = await self._fetcher._fetch_and_prepare()
                all_roms, shortcuts_data, platforms, collection_memberships, platform_rom_ids = fetch_result
            except asyncio.CancelledError:
                await self._finish_sync(_SYNC_CANCELLED)
                raise
            except Exception as e:
                self._logger.error(f"Failed to fetch platforms: {e}")
                _code, _msg = classify_error(e)
                await self._emit_progress("error", message=_msg, running=False)
                box.sync_state = SyncState.IDLE
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

            if box.sync_state == SyncState.CANCELLING:
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
            self._state_persister.save_state()

            # Store pending data for report_sync_results to reference
            box.pending_sync = {sd["rom_id"]: sd for sd in shortcuts_data}
            box.pending_collection_memberships = collection_memberships
            box.pending_platform_rom_ids = platform_rom_ids

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
            box.sync_progress = {
                "running": False,
                "phase": "error",
                "current": 0,
                "total": 0,
                "message": f"Sync failed — {_msg}",
            }
            self._loop.create_task(self._emit("sync_progress", box.sync_progress))
        finally:
            if self._metadata_service is not None:
                self._metadata_service.flush_metadata_if_dirty()
            box.sync_state = SyncState.IDLE
            if box.sync_progress.get("phase") != "error" and box.sync_progress.get("running"):
                self._start_safety_timeout()

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
