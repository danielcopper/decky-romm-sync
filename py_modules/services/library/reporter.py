"""Sync result reporter and registry-query sub-service.

Owns the post-apply path: the frontend-callable ``report_unit_results``
ack (event signal only) and the orchestrator-driven
``commit_unit_results`` that finalises artwork file names and upserts
each acked ROM into the ``roms`` aggregate, stamping its cached
``rom_metadata`` in the same write UoW (Rom row first, then metadata —
FK-safe). The terminal
``finalize_per_unit_run`` step builds the cross-unit collection
mappings, refreshes the ``platform_slug → display_name`` cache, and
emits the ``sync_complete`` event. Also owns the registry-derived
query methods (``get_registry_platforms``, ``get_sync_stats``,
``get_rom_by_steam_app_id``) and the ``clear_sync_cache`` reset.
Anything that mutates the ``roms`` registry as a side-effect of a
finished sync run belongs here; anything that decides "what should
this sync do?" belongs in the orchestrator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.platform_names import decode_platform_names
from domain.rom import Rom
from domain.rom_metadata_mapping import build_rom_metadata
from domain.sync_diff import should_include_in_platform_collection
from domain.sync_stage import SyncStage
from domain.sync_state import SyncState

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Awaitable, Callable

    from services.library._state import LibrarySyncStateBox
    from services.protocols import (
        ArtworkManager,
        Clock,
        EventEmitter,
        SteamConfigStore,
        UnitOfWorkFactory,
    )

    EmitProgressFn = Callable[..., Awaitable[None]]


# kv_config key for the offline ``platform_slug → display_name`` cache,
# refreshed on every sync from the live work-queue. Read by the offline
# registry queries (DangerZone label, game-detail platform name) so a
# RomM-down panel shows "Nintendo 64" rather than the bare "n64" slug.
_PLATFORM_NAMES_KEY = "platform_names"


@dataclass(frozen=True)
class SyncReporterConfig:
    """Frozen wiring bundle handed to ``SyncReporter.__init__``.

    Holds the Protocol-typed Steam-config adapter (used for grid-dir
    lookup and Steam-Input mode application), the live settings dict,
    runtime infrastructure (loop, logger), event emitter, clock,
    the SQLite Unit-of-Work factory (the transactional seam over the
    ``roms`` / ``rom_installs`` / ``sync_runs`` / ``kv_config``
    repositories), the shared ``LibrarySyncStateBox`` (the reporter reads
    the pending-sync dicts populated by the orchestrator and clears the
    active sync id when reporting completes), an orchestrator-supplied
    ``emit_progress`` callback for the terminal "done" event, and the
    ``ArtworkManager`` peer used for cover-path finalisation.
    """

    steam_config: SteamConfigStore
    settings: dict
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: EventEmitter
    clock: Clock
    uow_factory: UnitOfWorkFactory
    sync_state_box: LibrarySyncStateBox
    emit_progress: EmitProgressFn
    artwork: ArtworkManager


class SyncReporter:
    """Post-apply reporter + registry queries + cache reset."""

    def __init__(self, *, config: SyncReporterConfig) -> None:
        self._steam_config = config.steam_config
        self._settings = config.settings
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._clock = config.clock
        self._uow_factory = config.uow_factory
        self._sync_state = config.sync_state_box
        self._emit_progress = config.emit_progress
        self._artwork = config.artwork

    # ── Report sync results (frontend callback) ──────────────────

    def _finalize_cover_path(self, grid, cover_path, app_id, rom_id_str):
        """Delegate to ArtworkService for the final ``{app_id}p.png`` cover-path."""
        return self._artwork.finalize_cover_path(grid, cover_path, app_id, rom_id_str)

    def _build_collection_app_ids(
        self,
        uow,
        pending_platform_rom_ids: set[int] | None,
        pending_collection_memberships: dict[str, list[int]],
        platform_names: dict[str, str],
    ) -> tuple[dict, dict[str, list]]:
        """Build platform_app_ids and romm_collection_app_ids from ``uow.roms``.

        Platform collections are grouped from the full ``roms`` table
        (every bound ROM, including ones an incremental sync skipped —
        they remain rows), keyed by the platform's live display name
        resolved from *platform_names* (the work-queue), falling back to
        the slug when absent. RomM collections keep the per-run
        membership accumulator and resolve each ``rom_id`` to its bound
        ``shortcut_app_id`` via ``uow.roms``. Both loops EXCLUDE rows
        whose ``shortcut_app_id`` is ``None`` (unbound / stale).
        """
        create_groups = self._settings.get("collection_create_platform_groups", False)
        platform_app_ids: dict = {}
        for rom in uow.roms.iter_all():
            if rom.shortcut_app_id is None:
                continue
            if not should_include_in_platform_collection(rom.rom_id, pending_platform_rom_ids, create_groups):
                continue
            display = platform_names.get(rom.platform_slug, rom.platform_slug)
            platform_app_ids.setdefault(display, []).append(rom.shortcut_app_id)

        romm_collection_app_ids: dict[str, list] = {}
        for coll_name, rom_ids in pending_collection_memberships.items():
            app_ids = [
                rom.shortcut_app_id
                for rid in rom_ids
                if (rom := uow.roms.get(rid)) is not None and rom.shortcut_app_id is not None
            ]
            if app_ids:
                romm_collection_app_ids[coll_name] = app_ids

        return platform_app_ids, romm_collection_app_ids

    # ── Finalise per-unit run ────────────────────────────────────

    def _finalize_per_unit_run_io(
        self,
        pending_collection_memberships: dict[str, list[int]],
        pending_platform_rom_ids: set[int] | None,
        platform_names: dict[str, str],
        stale_rom_ids: list[int] | None = None,
    ) -> tuple[dict, dict[str, list]]:
        """Unbind stale ROMs, refresh the name cache, and build collection maps.

        By the time this runs, every per-unit ``commit_unit_results``
        has already upserted its ROMs into ``uow.roms``, so we only need
        to: (1) unbind the stale ROMs (clear ``shortcut_app_id``, keeping
        the row per ADR-0007 — never delete), (2) refresh the offline
        ``platform_slug → display_name`` cache from the live work-queue,
        and (3) build the cross-unit collection mappings. The last-sync
        timestamp and the synced platform/collection lists now live on
        the ``SyncRun`` record the orchestrator writes — they are not
        persisted here.

        Everything happens inside one write UoW so the unbind + cache
        refresh + reads commit atomically.
        """
        with self._uow_factory() as uow:
            for rid in stale_rom_ids or []:
                rom = uow.roms.get(rid)
                if rom is None or rom.shortcut_app_id is None:
                    continue
                rom.unbind_shortcut()
                uow.roms.save(rom)

            uow.kv_config.set(_PLATFORM_NAMES_KEY, json.dumps(platform_names))

            return self._build_collection_app_ids(
                uow,
                pending_platform_rom_ids,
                pending_collection_memberships,
                platform_names,
            )

    async def finalize_per_unit_run(
        self,
        pending_collection_memberships: dict[str, list[int]],
        pending_platform_rom_ids: set[int] | None,
        total_games: int,
        platform_names: dict[str, str] | None = None,
        cancelled: bool = False,
        stale_rom_ids: list[int] | None = None,
    ):
        """Emit ``sync_collections`` + ``sync_complete`` after all units finish.

        Stale-removal is emitted separately by the orchestrator via
        ``sync_stale`` so the frontend can apply removals before
        collections are recomputed. ``stale_rom_ids`` (default ``None`` =
        unbind nothing) have their Steam-shortcut binding cleared in the
        ``roms`` table (the row survives) before collections are built,
        keeping the backend registry in sync with the frontend removals.
        ``platform_names`` is the live ``platform_slug → display_name``
        map from the work-queue, cached for offline registry queries.
        """
        names = platform_names or {}
        platform_app_ids, romm_collection_app_ids = await self._loop.run_in_executor(
            None,
            self._finalize_per_unit_run_io,
            pending_collection_memberships,
            pending_platform_rom_ids,
            names,
            stale_rom_ids,
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

        total = await self._loop.run_in_executor(None, self._count_bound_roms)
        if cancelled:
            await self._emit_progress(
                SyncStage.CANCELLED,
                current=total_games,
                total=total,
                message=f"Sync cancelled: {total_games} of {total} games processed",
                running=False,
            )
        else:
            await self._emit_progress(
                SyncStage.DONE,
                current=total,
                total=total,
                message=f"Sync complete: {total} games from {len(platform_app_ids)} platforms",
                running=False,
            )

        self._sync_state.sync_state = SyncState.IDLE
        self._sync_state.current_sync_id = None
        return platform_app_ids, romm_collection_app_ids

    def _count_bound_roms(self) -> int:
        """Count ROMs that still carry a Steam-shortcut binding."""
        with self._uow_factory() as uow:
            return sum(1 for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None)

    # ── Report unit results (per-unit pipeline) ──────────────────

    def _commit_unit_results_io(self, rom_id_to_app_id, acked_roms):
        """Sync helper: finalise artwork file names, then upsert ``roms`` + metadata for one unit.

        ADR-0006 two-pass: cover-file RENAME is filesystem I/O so it runs
        FIRST (outside any UoW); the final paths are collected, then one
        short write UoW upserts every acked ROM via
        :meth:`_upsert_acked_rom`, which lands each ROM row and its cached
        metadata atomically. ``acked_roms`` is the live RomM fetch keyed by
        rom_id so the per-rom upsert can stamp metadata in the same write
        UoW as the ``roms`` row.
        """
        grid = self._steam_config.grid_dir()
        box = self._sync_state

        # ``acked_roms`` is the live RomM fetch for the ROMs the frontend
        # acked — the only source of ``metadatum``. Keyed by rom_id so the
        # Pass-2 loop can stamp metadata in the same iteration as the upsert.
        roms_by_id = {int(r["id"]): r for r in acked_roms if "id" in r}

        # Pass 1: rename staged covers to their final ``{app_id}p.png``
        # path (file I/O — no UoW open).
        finalized: dict[str, str] = {}
        for rom_id_str, app_id in rom_id_to_app_id.items():
            pending = box.pending_sync.get(int(rom_id_str), {})
            finalized[rom_id_str] = self._finalize_cover_path(grid, pending.get("cover_path", ""), app_id, rom_id_str)

        # Pass 2: one write UoW for the whole unit's ROM + metadata upserts.
        with self._uow_factory() as uow:
            for rom_id_str, app_id in rom_id_to_app_id.items():
                self._upsert_acked_rom(uow, rom_id_str, app_id, finalized, roms_by_id)

        steam_input_mode = self._settings.get("steam_input_mode", "default")
        if steam_input_mode != "default" and rom_id_to_app_id:
            try:
                self._steam_config.set_steam_input_config(
                    [int(aid) for aid in rom_id_to_app_id.values()], mode=steam_input_mode
                )
            except Exception as e:
                self._logger.error(f"Failed to set Steam Input config: {e}")

    def _upsert_acked_rom(self, uow, rom_id_str, app_id, finalized, roms_by_id) -> None:
        """Upsert one acked ROM + its cached metadata into the open write UoW.

        Builds the ``Rom`` via ``Rom.synced`` (which validates untrusted
        RomM fields; a ``ValueError`` is caught here so one bad row is
        skipped while the rest of the unit still commits), read-merges the
        plugin-resolved ids (``sgdb_id`` / ``ra_id`` / ``cover_path`` follow
        "non-None new wins, else preserve existing, else None"), saves the
        Rom, then stamps its cached metadata. Saving the Rom before its
        metadata satisfies the ``rom_metadata.rom_id → roms(rom_id)`` FK at
        commit, so a ROM and its metadata land atomically.
        """
        box = self._sync_state
        pending = box.pending_sync.get(int(rom_id_str), {})
        rom_id = int(rom_id_str)
        existing = uow.roms.get(rom_id)
        try:
            rom = Rom.synced(
                rom_id=rom_id,
                platform_slug=pending.get("platform_slug", ""),
                name=pending.get("name", ""),
                fs_name=pending.get("fs_name", ""),
                shortcut_app_id=int(app_id),
                synced_at=self._clock.now().isoformat(),
                igdb_id=pending.get("igdb_id"),
            )
        except ValueError as e:
            self._logger.warning(f"Skipping invalid ROM {rom_id_str} during commit: {e}")
            return
        cover_path = finalized.get(rom_id_str) or (existing.cover_path if existing is not None else None)
        if cover_path:
            rom.update_cover_path(cover_path)
        sgdb_id = self._merge_optional_id(pending.get("sgdb_id"), existing.sgdb_id if existing else None)
        if sgdb_id is not None:
            rom.assign_sgdb_id(sgdb_id)
        ra_id = self._merge_optional_id(pending.get("ra_id"), existing.ra_id if existing else None)
        if ra_id is not None:
            rom.assign_ra_id(ra_id)
        uow.roms.save(rom)

        self._stamp_rom_metadata(uow, rom_id, roms_by_id.get(rom_id))

    def _stamp_rom_metadata(self, uow, rom_id: int, rom: dict | None) -> None:
        """Stamp the ROM's cached metadata into ``uow.rom_metadata`` for this commit.

        No-op when the acked ROM carries no ``metadatum`` (defensive: thin
        registry-reconstructed ROMs from the incremental-skip path are
        already gated out upstream, but this guard prevents accidental
        cache erasure). The Rom row was saved just before this call in the
        same UoW, so the ``rom_metadata.rom_id`` FK is satisfied at commit.
        A malformed ``metadatum`` raises ``ValueError`` / ``TypeError`` in
        the mapping — caught here so only this ROM's metadata is skipped
        while its Rom row still commits.
        """
        if not rom or not rom.get("metadatum"):
            return
        try:
            meta = build_rom_metadata(rom, self._clock.time())
        except (ValueError, TypeError) as e:
            self._logger.warning(f"Skipping metadata for ROM {rom_id} — malformed metadatum: {e}")
            return
        uow.rom_metadata.save(rom_id, meta)

    @staticmethod
    def _merge_optional_id(new_value, existing_value) -> int | None:
        """Resolve a plugin-resolved id: non-None new wins, else preserve existing, else None."""
        if new_value is not None:
            return int(new_value)
        if existing_value is not None:
            return int(existing_value)
        return None

    async def report_unit_results(self, rom_id_to_app_id):
        """Frontend-Callable: ack that this unit's shortcuts have been applied.

        Records the rom_id→app_id mapping into the state box and signals
        the orchestrator's per-unit wait event. The orchestrator drives
        the actual per-unit commit (the ``roms`` upsert + metadata stamp,
        atomic in one write UoW) after this returns.
        """
        box = self._sync_state
        box.last_unit_results = dict(rom_id_to_app_id)
        if box.unit_complete_event is not None:
            box.unit_complete_event.set()

        self._logger.info(f"Unit results acknowledged: {len(rom_id_to_app_id)} shortcuts")
        return {"success": True, "count": len(rom_id_to_app_id)}

    async def commit_unit_results(self, rom_id_to_app_id, acked_roms):
        """Per-unit commit: cover-path finalize then atomic ``roms`` + metadata upsert.

        Called by the orchestrator once the frontend has acked the unit's
        shortcuts. The ``roms`` upsert and the cached-metadata stamp land
        in one write UoW (Rom row first, then ``rom_metadata`` — FK-safe),
        so a ROM and its metadata are always consistent across a crash.
        ``acked_roms`` is the live RomM fetch for the acked ROMs — the
        source of each ROM's ``metadatum``.
        """
        await self._loop.run_in_executor(None, self._commit_unit_results_io, rom_id_to_app_id, acked_roms)

    # ── Registry queries ─────────────────────────────────────────

    def _read_platform_name_cache(self, uow) -> dict[str, str]:
        """Decode the ``platform_slug → display_name`` cache, ``{}`` when absent/corrupt."""
        return decode_platform_names(uow.kv_config.get(_PLATFORM_NAMES_KEY))

    def get_registry_platforms(self):
        """Return synced platforms from ``uow.roms`` (works offline, no RomM API call).

        Counts bound ROMs per ``platform_slug`` and resolves display
        names from the ``platform_names`` cache refreshed each sync,
        degrading to the slug when a name is absent (RomM never seen for
        that slug). Unbound (stale) rows are excluded.
        """
        return self._read_registry_platforms_io()

    def _read_registry_platforms_io(self):
        with self._uow_factory() as uow:
            names = self._read_platform_name_cache(uow)
            platforms: dict[str, dict] = {}
            for rom in uow.roms.iter_all():
                if rom.shortcut_app_id is None:
                    continue
                slug = rom.platform_slug
                display = names.get(slug, slug)
                platforms.setdefault(display, {"count": 0, "slug": slug})
                platforms[display]["count"] += 1
        return {
            "platforms": [{"name": k, "slug": v["slug"], "count": v["count"]} for k, v in sorted(platforms.items())],
        }

    # ── Cache / stats ────────────────────────────────────────────

    def clear_sync_cache(self):
        """Force a full re-fetch on the next sync by clearing the completed-run history.

        The incremental-skip gate (fetcher) and ``get_sync_stats`` both derive
        ``last_sync`` from the newest completed ``SyncRun``; deleting the
        completed runs in a short write UoW resets that read to ``None`` so every
        platform full-fetches next time (and the "Force Full Sync" button hides
        until a fresh run completes).
        """
        with self._uow_factory() as uow:
            uow.sync_runs.delete_completed()
        self._logger.info("Sync cache cleared — next sync will do a full fetch")
        return {"success": True, "message": "Next sync will do a full fetch"}

    def get_sync_stats(self):
        enabled_platforms = self._settings.get("enabled_platforms", {})
        enabled_platform_count = sum(1 for v in enabled_platforms.values() if v)
        enabled_collections = self._settings.get("enabled_collections", {})
        if isinstance(enabled_collections, dict):
            enabled_collection_count = sum(
                1 for bucket in enabled_collections.values() if isinstance(bucket, dict) for v in bucket.values() if v
            )
        else:
            enabled_collection_count = 0
        last_sync, rom_count = self._read_sync_stats_io()
        return {
            "last_sync": last_sync,
            "platforms": enabled_platform_count,
            "collections": enabled_collection_count,
            "roms": rom_count,
            "total_shortcuts": rom_count,
        }

    def _read_sync_stats_io(self) -> tuple[str | None, int]:
        """Read ``(last_sync_iso, bound_rom_count)`` from SQLite.

        ``last_sync`` is the ``finished_at`` of the latest completed
        ``SyncRun``; the ROM count is the bound-shortcut count in ``roms``.
        """
        with self._uow_factory() as uow:
            latest = uow.sync_runs.get_latest_completed()
            last_sync = latest.finished_at if latest is not None else None
            rom_count = sum(1 for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None)
        return last_sync, rom_count

    def get_rom_by_steam_app_id(self, app_id):
        return self._read_rom_by_app_id_io(int(app_id))

    def _read_rom_by_app_id_io(self, app_id: int):
        with self._uow_factory() as uow:
            rom = uow.roms.get_by_app_id(app_id)
            if rom is None:
                return None
            display = self._read_platform_name_cache(uow).get(rom.platform_slug, rom.platform_slug)
            installed = uow.rom_installs.get(rom.rom_id) is not None
        return {
            "rom_id": rom.rom_id,
            "name": rom.name,
            "platform_name": display,
            "platform_slug": rom.platform_slug,
            "installed": installed,
        }
