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
from typing import TYPE_CHECKING, Any

from domain.platform_names import decode_platform_names
from domain.rom import Rom
from domain.rom_metadata_mapping import build_rom_metadata
from domain.sync_diff import BIND_ROM_ID_KEY, should_include_in_platform_collection
from domain.sync_stage import SyncStage
from domain.version_metadata import VersionMetadata

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Awaitable, Callable

    from domain.collection_sync_state import CollectionSyncState
    from domain.platform_sync_state import PlatformSyncState
    from domain.sync_run import SyncRun
    from services.library._state import CollectionMembership, LibrarySyncStateBox
    from services.protocols import (
        ArtworkManager,
        Clock,
        EventEmitter,
        SteamConfigStore,
        UnitOfWork,
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
    the pending-sync dicts populated by the orchestrator; the run-lifecycle
    reset is owned by the orchestrator's terminal ``finally``, not here), an
    orchestrator-supplied ``emit_progress`` callback for the terminal "done"
    event, and the
    ``ArtworkManager`` peer used for cover-path finalisation.
    """

    steam_config: SteamConfigStore
    settings: dict[str, Any]
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
        """Delegate to ArtworkService for the final ``{app_id}p.png`` cover-path.

        The frontend already applies a created shortcut's cover through Steam's
        artwork API during apply, and Steam writes the same ``{app_id}p.png`` grid
        file itself. This commit-time copy is the durability net: it lands the grid
        file even if the per-item API call failed, and costs no renderer heap.
        """
        return self._artwork.finalize_cover_path(grid, cover_path, app_id, rom_id_str)

    def _build_collection_app_ids(
        self,
        uow: UnitOfWork,
        pending_platform_rom_ids: set[int] | None,
        pending_collection_memberships: dict[tuple[str, str], CollectionMembership],
        platform_names: dict[str, str],
    ) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
        """Build platform_app_ids and romm_collection_app_ids from ``uow.roms``.

        Platform collections are grouped from the full ``roms`` table
        (every bound ROM, including ones an incremental sync skipped —
        they remain rows), keyed by the platform's live display name
        resolved from *platform_names* (the work-queue), falling back to
        the slug when absent.

        RomM collections keep the per-run membership accumulator (keyed by a
        collision-free ``(collection_kind, collection_id)`` identity, name in the
        value) and resolve each member ``rom_id`` to a Steam appId with a
        **sibling-group fallback** (ADR-0021): a bound member uses its own
        binding; an unbound member maps to its group's bound sibling's appId, so
        favouriting / collecting ANY version of a game puts the game's single
        shortcut into the Steam collection. Per-collection appIds are
        de-duplicated — several siblings of one group collapse onto the one
        shortcut — and same-named collections UNION into the one by-name Steam
        collection (#1503). The platform loop still excludes rows whose
        ``shortcut_app_id`` is ``None``.
        """
        platform_app_ids, group_bound_app_id = self._scan_bound_rows(uow, pending_platform_rom_ids, platform_names)
        romm_collection_app_ids = self._resolve_collection_memberships(
            uow, pending_collection_memberships, group_bound_app_id
        )
        return platform_app_ids, romm_collection_app_ids

    def _scan_bound_rows(
        self,
        uow: UnitOfWork,
        pending_platform_rom_ids: set[int] | None,
        platform_names: dict[str, str],
    ) -> tuple[dict[str, list[int]], dict[str, int]]:
        """One pass over the bound rows: platform buckets + each group's bound appId.

        When a group carries several bound rows (grandfathered duplicates) the
        smallest rom_id's binding wins, deterministically.
        """
        create_groups = self._settings.get("collection_create_platform_groups", False)
        platform_app_ids: dict[str, list[int]] = {}
        group_bound: dict[str, tuple[int, int]] = {}
        for rom in uow.roms.iter_all():
            if rom.shortcut_app_id is None:
                continue
            if should_include_in_platform_collection(rom.rom_id, pending_platform_rom_ids, create_groups):
                display = platform_names.get(rom.platform_slug, rom.platform_slug)
                platform_app_ids.setdefault(display, []).append(rom.shortcut_app_id)
            self._note_group_binding(group_bound, rom)
        return platform_app_ids, {key: app_id for key, (_rid, app_id) in group_bound.items()}

    @staticmethod
    def _note_group_binding(group_bound: dict[str, tuple[int, int]], rom: Rom) -> None:
        """Record the group's winning binding: smallest bound rom_id wins."""
        group_key = rom.sibling_group_key
        if group_key is None or rom.shortcut_app_id is None:
            return
        current = group_bound.get(group_key)
        if current is None or rom.rom_id < current[0]:
            group_bound[group_key] = (rom.rom_id, rom.shortcut_app_id)

    def _resolve_collection_memberships(
        self,
        uow: UnitOfWork,
        pending_collection_memberships: dict[tuple[str, str], CollectionMembership],
        group_bound_app_id: dict[str, int],
    ) -> dict[str, list[int]]:
        """Resolve each collection's member rom_ids to de-duplicated appIds, UNION by name.

        A bound member uses its own binding; an unbound member falls back to its
        sibling group's bound appId (ADR-0021). Steam's collection namespace is
        by-name, so same-named RomM collections (permitted across kinds/users,
        #1503) merge into one entry: their resolved appIds are UNIONed,
        order-preserving and de-duplicated ACROSS collections (each collection's
        own resolution already dedups within). The accumulator is keyed by a
        collision-free identity, so a same-named pair never overwrote either
        member set upstream. Names that resolve to no appId are omitted; the
        common single-collection case unions a set of one and is byte-for-byte
        identical to the pre-#1503 output.
        """
        romm_collection_app_ids: dict[str, list[int]] = {}
        seen_by_name: dict[str, set[int]] = {}
        for membership in pending_collection_memberships.values():
            app_ids = romm_collection_app_ids.setdefault(membership.name, [])
            seen = seen_by_name.setdefault(membership.name, set())
            for rid in membership.rom_ids:
                app_id = self._member_app_id(uow, rid, group_bound_app_id)
                if app_id is not None and app_id not in seen:
                    seen.add(app_id)
                    app_ids.append(app_id)
        return {name: app_ids for name, app_ids in romm_collection_app_ids.items() if app_ids}

    @staticmethod
    def _member_app_id(uow: UnitOfWork, rid: int, group_bound_app_id: dict[str, int]) -> int | None:
        """A member's appId: its own binding, else its sibling group's bound appId."""
        rom = uow.roms.get(rid)
        if rom is None:
            return None
        if rom.shortcut_app_id is not None:
            return rom.shortcut_app_id
        if rom.sibling_group_key is None:
            return None
        return group_bound_app_id.get(rom.sibling_group_key)

    # ── Finalise per-unit run ────────────────────────────────────

    def _finalize_per_unit_run_io(
        self,
        pending_collection_memberships: dict[tuple[str, str], CollectionMembership],
        pending_platform_rom_ids: set[int] | None,
        platform_names: dict[str, str],
        stale_rom_ids: list[int] | None = None,
    ) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
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
            # Stale removal only UNBINDS the row (ADR-0007 keeps it), so a
            # platform's persisted-row count is unchanged and its completion stamp
            # (ADR-0023) stays valid — deliberately NOT invalidated here. If the
            # server actually dropped ROMs, the next skip catches it anyway: the
            # dropped ROM lowers RomM's platform rom_count, which no longer matches
            # the stamp's rom_count (nor the persisted-row count), so the platform
            # full-fetches. So the stale path needs no stamp invalidation.
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
        pending_collection_memberships: dict[tuple[str, str], CollectionMembership],
        pending_platform_rom_ids: set[int] | None,
        platform_names: dict[str, str] | None = None,
        stale_rom_ids: list[int] | None = None,
    ):
        """Unbind stale ROMs, rebuild Steam-collection maps, and emit ``sync_collections``.

        Stale-removal is emitted separately by the orchestrator via
        ``sync_stale`` so the frontend can apply removals before
        collections are recomputed. ``stale_rom_ids`` (default ``None`` =
        unbind nothing) have their Steam-shortcut binding cleared in the
        ``roms`` table (the row survives) before collections are built,
        keeping the backend registry in sync with the frontend removals.
        ``platform_names`` is the live ``platform_slug → display_name``
        map from the work-queue, cached for offline registry queries.

        Returns the ``(platform_app_ids, romm_collection_app_ids)`` maps the caller
        needs for the completed-run ``SyncRun`` write and the terminal emit. The
        terminal ``sync_complete`` + progress frame is deliberately NOT emitted here
        — it is the orchestrator's separate :meth:`emit_sync_complete` call made
        AFTER the terminal ``SyncRun`` status is persisted, so a frontend stats
        refetch triggered by those terminal signals reads the fresh run status
        instead of racing the DB write (#39).
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

        return platform_app_ids, romm_collection_app_ids

    async def emit_sync_complete(
        self,
        *,
        platform_app_ids: dict[str, list[int]],
        romm_collection_app_ids: dict[str, list[int]],
        total_games: int,
        cancelled: bool,
        interrupt_reason: str | None,
        restart_recommended: bool,
    ) -> None:
        """Emit the terminal ``sync_complete`` event + the terminal progress frame.

        Called by the orchestrator AFTER the terminal ``SyncRun`` status is
        persisted — this is the "emit last" ordering that closes the emit-before-
        persist race (#39): a frontend stats refetch triggered by these terminal
        signals now reads the freshly-written run status, not the prior run's.

        Session-budget surfacing (#1383): ``interrupt_reason`` (present only when a
        run was paused by the budget gate) rides the ``sync_complete`` payload and
        becomes the terminal progress message, so the UI shows the resume-friendly
        pause guidance distinctly instead of the generic cancelled/interrupted
        wording. ``restart_recommended`` (only on a clean run whose post-run RSS is
        high) sets an additive payload flag the UI turns into a "restart Steam" nudge.

        Heartbeat-timeout surfacing (#1384): an interrupted run (an external death —
        frontend crash/reload — flagged by the box's ``run_interrupted``) routes
        through the same cancelled finalize, so the payload carries an additive
        ``interrupted: True`` alongside ``cancelled`` and the completion toast can
        say "interrupted" instead of blaming a Cancel the user never pressed. A
        budget pause sets ``interrupt_reason`` (not ``run_interrupted``) and never
        carries the flag.
        """
        complete_payload: dict[str, Any] = {
            "platform_app_ids": platform_app_ids,
            "romm_collection_app_ids": romm_collection_app_ids,
            "total_games": total_games,
        }
        if cancelled:
            complete_payload["cancelled"] = True
            if self._sync_state.run_interrupted:
                complete_payload["interrupted"] = True
            if interrupt_reason:
                complete_payload["interrupt_reason"] = interrupt_reason
        elif restart_recommended:
            complete_payload["restart_recommended"] = True
        await self._emit("sync_complete", complete_payload)

        if cancelled:
            await self._emit_cancelled_frame(total_games=total_games, interrupt_reason=interrupt_reason)
        else:
            total = await self._loop.run_in_executor(None, self._count_bound_roms)
            await self._emit_progress(
                SyncStage.DONE,
                current=total,
                total=total,
                message=f"Sync complete: {total} games from {len(platform_app_ids)} platforms",
                running=False,
            )

    async def _emit_cancelled_frame(self, *, total_games: int, interrupt_reason: str | None) -> None:
        """Emit the terminal progress frame for a cancelled/interrupted run.

        A budget pause carries its own full-sentence guidance — used as the
        terminal message verbatim so the QAM status reads the resume-friendly
        reason. Otherwise a heartbeat-timeout run routes through this same
        cancelled finalize, so the leading word keys on the box's
        ``run_interrupted`` flag — the frame then reads "interrupted" instead of
        blaming the user's Cancel button (stage stays CANCELLED; last_attempt
        already reads "interrupted"). The frame's denominator is the run's
        PLANNED total (the sync_plan count stamped in the box) so "N of M games
        processed" compares against the plan, not the bound-ROM registry count
        (which equals N on a first sync); the registry count is only the
        fallback when the box was wiped before plan time (plugin reload).
        """
        planned_total = self._sync_state.run_total_items
        total = (
            planned_total
            if planned_total is not None
            else await self._loop.run_in_executor(None, self._count_bound_roms)
        )
        if interrupt_reason:
            message = interrupt_reason
        else:
            lead = "Sync interrupted" if self._sync_state.run_interrupted else "Sync cancelled"
            message = f"{lead}: {total_games} of {total} games processed"
        await self._emit_progress(
            SyncStage.CANCELLED,
            current=total_games,
            total=total,
            message=message,
            running=False,
        )

    def _count_bound_roms(self) -> int:
        """Count ROMs that still carry a Steam-shortcut binding."""
        with self._uow_factory() as uow:
            return sum(1 for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None)

    # ── Report unit results (per-unit pipeline) ──────────────────

    def _commit_unit_results_io(
        self, rom_id_to_app_id, unit_roms, platform_stamp=None, collection_stamp=None, fetch_id=None
    ):
        """Finalise artwork names, then persist EVERY fetched ROM of the chunk.

        Group-aware commit (ADR-0021): this chunk's slice of the live RomM fetch
        (*unit_roms*) is upserted — one ``roms`` row per sibling for its
        identity + version metadata — while only the acked representatives
        carry a Steam-shortcut binding. A non-representative sibling keeps
        whatever binding it already had (usually none); a bound row not
        re-acked this cycle is never silently unbound.

        The frontend ack is **translated through any rebind** first: a rebind
        entry is keyed by the vanished bound sibling (so the frontend reused its
        shortcut), but the binding — and the finalised cover — move onto the
        surviving representative named in ``bind_rom_id``.

        ADR-0006 two-pass: cover-file RENAME is filesystem I/O so it runs FIRST
        (outside any UoW); the final paths are collected, then one short write
        UoW upserts every ROM (Rom row first, cached metadata second — FK-safe),
        so a ROM and its metadata land atomically.

        ``platform_stamp`` (set by the orchestrator on the final chunk of a
        platform unit, ADR-0023) is saved inside that same write UoW, so the
        per-platform completion stamp commits atomically with the chunk's rom
        upserts — the platform is stamped complete iff its last chunk is durable.
        ``collection_stamp`` is the collection sibling (#742), set on the final
        chunk of a standard/smart collection unit; it rides the same UoW so a
        collection is stamped complete iff its last chunk is durable. Exactly one
        of the two is ever set per commit (a unit is a platform or a collection).

        ``fetch_id`` is the fetch generation stamped onto every row this commit
        upserts (#1504). It is set on EVERY chunk of a platform unit — not just
        the final one — so the whole unit's rows share the generation its
        completion stamp records, and left ``None`` by collection units and the
        late-ack path (see :meth:`Rom.record_fetch_generation`).
        """
        grid = self._steam_config.grid_dir()
        box = self._sync_state

        # Translate the ack onto binding targets (rebind moves the binding off
        # the vanished sibling onto its representative) and finalise the staged
        # cover to ``{app_id}p.png``, keyed by the target rom_id.
        binding: dict[int, int] = {}
        finalized: dict[int, str] = {}
        for rom_id_str, app_id in rom_id_to_app_id.items():
            entry = box.pending_sync.get(int(rom_id_str), {})
            target = int(entry.get(BIND_ROM_ID_KEY, int(rom_id_str)))
            binding[target] = int(app_id)
            finalized[target] = self._finalize_cover_path(grid, entry.get("cover_path", ""), int(app_id), str(target))

        # ``unit_roms`` is the live RomM fetch for the whole unit — the source of
        # each ROM's ``metadatum``. Keyed by rom_id so the persist loop can stamp
        # metadata in the same iteration as the upsert.
        roms_by_id = {int(r["id"]): r for r in unit_roms if "id" in r}

        with self._uow_factory() as uow:
            for raw in unit_roms:
                if "id" in raw:
                    self._persist_synced_rom(uow, int(raw["id"]), binding, finalized, roms_by_id, fetch_id)
            if platform_stamp is not None:
                uow.platform_sync_state.save(platform_stamp)
            if collection_stamp is not None:
                uow.collection_sync_state.save(collection_stamp)

        steam_input_mode = self._settings.get("steam_input_mode", "default")
        if steam_input_mode != "default" and binding:
            try:
                self._steam_config.set_steam_input_config([int(aid) for aid in binding.values()], mode=steam_input_mode)
            except Exception as e:
                self._logger.error(f"Failed to set Steam Input config: {e}")

    def _persist_synced_rom(self, uow, rom_id, binding, finalized, roms_by_id, fetch_id=None) -> None:
        """Upsert one fetched ROM + its cached metadata into the open write UoW.

        Reads the built identity + version fields from ``pending_all_roms`` (the
        whole unit's shortcut-shaped build), binds the ROM only when it is a
        binding target this cycle (else preserves its existing binding — a
        non-representative sibling stays unbound, a bound row not re-acked keeps
        its shortcut), read-merges the plugin-resolved ids
        (``sgdb_id`` / ``ra_id`` / ``cover_path`` / ``cover_source`` follow
        "confirmed new wins, else preserve existing, else None"), saves the Rom,
        then stamps its cached metadata. Saving the Rom before its metadata satisfies the
        ``rom_metadata.rom_id → roms(rom_id)`` FK at commit. ``Rom.synced``
        validates untrusted RomM fields; a ``ValueError`` is caught so one bad
        row is skipped while the rest of the unit still commits.
        """
        built = self._sync_state.pending_all_roms.get(rom_id, {})
        existing = uow.roms.get(rom_id)
        # Bind a binding target this cycle; otherwise preserve any existing
        # binding (a non-representative sibling stays unbound, a bound row not
        # re-acked keeps its shortcut).
        app_id = binding.get(rom_id, existing.shortcut_app_id if existing is not None else None)
        try:
            rom = Rom.synced(
                rom_id=rom_id,
                platform_slug=built.get("platform_slug", ""),
                name=built.get("name", ""),
                fs_name=built.get("fs_name", ""),
                shortcut_app_id=app_id,
                synced_at=self._clock.now().isoformat(),
                igdb_id=built.get("igdb_id"),
                version=VersionMetadata.from_mapping(built),
                fs_size_bytes=built.get("fs_size_bytes"),
            )
        except ValueError as e:
            self._logger.warning(f"Skipping invalid ROM {rom_id} during commit: {e}")
            return
        self._merge_plugin_resolved_fields(rom, rom_id, built, finalized, existing)
        self._merge_fetch_generation(rom, fetch_id, existing)
        uow.roms.save(rom)

        # Record the applied launch command for a binding TARGET this cycle (the
        # value the frontend just wrote onto the shortcut), so the next sync skips
        # this now-correct shortcut (delta-restricted apply, #1383). A skipped or
        # non-representative row is absent from ``binding`` and keeps its recorded
        # value — the pin-only ``set_applied_launch_options`` write never wipes it,
        # unlike ``save()``. This is the first of the five recorded-state writer
        # sites (the others: download-complete, uninstall, home migration, version
        # switch).
        if rom_id in binding:
            rom.record_applied_launch_options(built.get("launch_options", ""))
            uow.roms.set_applied_launch_options(rom_id, rom.applied_launch_options)

        self._stamp_rom_metadata(uow, rom_id, roms_by_id.get(rom_id))

    def _merge_plugin_resolved_fields(self, rom: Rom, rom_id: int, built, finalized, existing) -> None:
        """Read-merge the plugin-resolved fields onto the freshly built Rom.

        Each field follows "confirmed new wins, else preserve existing, else
        None": ``cover_path`` from this unit's finalized grid copies,
        ``cover_source`` from the box's confirmed ``pending_cover_sources``
        (never blindly the fetch's fresh string — a failed download keeps the
        old fingerprint so the change is retried next sync), and the
        ``sgdb_id`` / ``ra_id`` resolver results.
        """
        cover_path = finalized.get(rom_id) or (existing.cover_path if existing is not None else None)
        if cover_path:
            rom.update_cover_path(cover_path)
        cover_source = self._sync_state.pending_cover_sources.get(rom_id) or (
            existing.cover_source if existing is not None else None
        )
        if cover_source:
            rom.adopt_cover_source(cover_source)
        sgdb_id = self._merge_optional_id(built.get("sgdb_id"), existing.sgdb_id if existing else None)
        if sgdb_id is not None:
            rom.assign_sgdb_id(sgdb_id)
        ra_id = self._merge_optional_id(built.get("ra_id"), existing.ra_id if existing else None)
        if ra_id is not None:
            rom.assign_ra_id(ra_id)

    @staticmethod
    def _merge_fetch_generation(rom: Rom, fetch_id: str | None, existing) -> None:
        """Advance the row's fetch generation, or carry the existing one forward.

        Follows the same "confirmed new wins, else preserve existing, else None"
        merge as the plugin-resolved fields: a **platform** unit's commit supplies
        the generation and advances every row it upserts, while a **collection**
        unit (which supplies none) must leave a foreign platform's row on its own
        generation — re-marking it would drop it from that platform's counted
        rows and suppress that platform's skip (#1504). The column rides the sync
        UPSERT, so preserving here is what keeps the value across a re-save.
        """
        carried = fetch_id or (existing.last_fetch_id if existing is not None else None)
        if carried:
            rom.record_fetch_generation(carried)

    def _stamp_rom_metadata(self, uow, rom_id: int, rom: dict[str, Any] | None) -> None:
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

    async def report_unit_results(self, rom_id_to_app_id, run_id, unit_id, chunk_index):
        """Frontend-Callable: ack that this apply chunk's shortcuts are applied.

        Routes the ack in three cases, by run/unit/chunk identity:

        * **Active chunk** — the ack matches the dispatched
          ``current_sync_id`` / ``active_unit_id`` / ``active_chunk_index``
          (#1041). Record the rom_id→app_id mapping and, if the orchestrator is
          still waiting (``unit_complete_event`` live), signal the event so it
          drives the per-chunk commit. The happy path. A duplicate ack whose
          event was already consumed (identity still matches, event ``None``)
          simply records the mapping and no-ops — nothing is double-committed.
        * **Abandoned chunk** — the active identity no longer matches (the run
          wound down and ``finish_run`` nulled ``current_sync_id``), but the ack
          matches an ``abandoned_chunk`` stash left by a heartbeat timeout. The
          frontend already created the Steam shortcuts, so commit the delivered
          bindings here rather than discard them (#1052 / #1367). Popping the
          stash (``take_abandoned_chunk``) hands back the timed-out chunk's
          fetched rows; ``commit_unit_results`` upserts every fetched sibling
          (identity + metadata, the ``metadatum`` source) and binds only the
          acked representatives, reading the whole-unit staging still live on the
          box. Never passes a ``platform_stamp`` — a timed-out platform is
          incomplete and must not be stamped. A duplicate late ack finds the
          stash already cleared and falls through to *ignored*.
        * **Neither** — a stray/superseded ack (a late ack from a cancelled run,
          a different unit, or a stale chunk with no stash match). Ignored:
          neither recorded, signalled, nor committed, so it can never be
          credited to the wrong unit/run/chunk. Logged at debug, returns
          ``ignored: True`` with ``count: 0``.
        """
        box = self._sync_state
        if self._ack_matches_active_unit(run_id, unit_id, chunk_index):
            box.last_unit_results = dict(rom_id_to_app_id)
            if box.unit_complete_event is not None:
                box.unit_complete_event.set()
            self._logger.info(f"Unit results acknowledged: {len(rom_id_to_app_id)} shortcuts")
            return {"success": True, "count": len(rom_id_to_app_id)}

        stash = box.take_abandoned_chunk(run_id, unit_id, chunk_index)
        if stash is not None:
            await self.commit_unit_results(dict(rom_id_to_app_id), stash.chunk_rows)
            self._logger.info(f"Late ack recovered abandoned chunk: {len(rom_id_to_app_id)} shortcuts committed")
            return {"success": True, "count": len(rom_id_to_app_id)}

        self._logger.debug(
            f"Ignoring unit ack for run={run_id!r} unit={unit_id!r} chunk={chunk_index!r}: "
            f"active run={box.current_sync_id!r} unit={box.active_unit_id!r} chunk={box.active_chunk_index!r}, "
            f"no abandoned-chunk stash match"
        )
        return {"success": True, "count": 0, "ignored": True}

    def _ack_matches_active_unit(self, run_id, unit_id, chunk_index) -> bool:
        """True when the ack's run/unit/chunk identity matches the dispatched chunk.

        The frontend echoes back the ``run_id`` + ``unit_id`` + ``chunk_index``
        carried in the ``sync_apply_unit`` event. ``run_id`` and ``unit_id`` are
        compared by string value: the run id is a UUID string, and the unit id is
        JSON-shaped (a number for a platform, a string for a collection) so
        ``str()`` coercion on both sides is robust to int-vs-str drift on the
        wire; ``chunk_index`` is compared as an int. An ack is rejected when there
        is no active unit/chunk (``active_unit_id`` / ``active_chunk_index`` is
        ``None`` — the unit was cancelled or already committed), so a stray late
        ack from a cancelled run no-ops instead of being credited to a fresh run
        (#1041).
        """
        box = self._sync_state
        if box.active_unit_id is None or box.active_chunk_index is None:
            return False
        return (
            str(run_id) == str(box.current_sync_id)
            and str(unit_id) == str(box.active_unit_id)
            and int(chunk_index) == box.active_chunk_index
        )

    async def commit_unit_results(
        self,
        rom_id_to_app_id,
        unit_roms,
        platform_stamp: PlatformSyncState | None = None,
        collection_stamp: CollectionSyncState | None = None,
        fetch_id: str | None = None,
    ):
        """Per-chunk commit: cover-path finalize then atomic ``roms`` + metadata upsert.

        Called once the frontend has acked an apply chunk's shortcuts — by the
        orchestrator on the happy path, or by :meth:`report_unit_results`
        itself on the heartbeat-timeout late-ack path (#1052). ``unit_roms`` is
        this chunk's slice of the live RomM fetch: a ``roms`` row is upserted for
        EVERY sibling in the slice (identity + version metadata, ADR-0021), but
        only the acked representatives carry a binding. The upsert and the
        cached-metadata stamp land in one write UoW (Rom row first, then
        ``rom_metadata`` — FK-safe), so a ROM and its metadata are always
        consistent across a crash, and each committed chunk is durable on its own.

        ``platform_stamp`` / ``collection_stamp`` are passed only by the
        orchestrator on the **final chunk of a platform / user-smart-collection
        unit** (ADR-0023, #742); the relevant one rides the same write UoW so the
        completion stamp is atomic with the chunk's rom upserts. The
        heartbeat-timeout late-ack path never sets either — a timed-out unit is
        incomplete and must not be stamped.

        ``fetch_id`` rides EVERY chunk of a platform unit (not only the stamped
        final one), marking each upserted row with the generation that fetch
        stamp records (#1504). Collection units and the late-ack path pass
        ``None``, leaving each row's existing generation untouched.

        Records every bound appId in the shared box so the stale-removal scan
        excludes appIds this run committed, whichever path drove the commit —
        a new rom_id reusing an old appId must not look stale (#1036).
        """
        await self._loop.run_in_executor(
            None,
            self._commit_unit_results_io,
            rom_id_to_app_id,
            unit_roms,
            platform_stamp,
            collection_stamp,
            fetch_id,
        )
        self._sync_state.committed_app_ids.update(int(aid) for aid in rom_id_to_app_id.values())

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
            platforms: dict[str, dict[str, Any]] = {}
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
        """Force a full re-fetch AND a full re-apply on the next sync.

        The incremental-skip gate (fetcher) is stamp-based: its sole skip
        authority is the per-platform ``PlatformSyncState`` completion stamp
        (ADR-0023) and the per-collection ``CollectionSyncState`` stamp (#742),
        each of which is its own ``effective_last_sync``. So the full re-fetch is
        armed by clearing every stamp — no platform or collection then holds skip
        authority and each full-fetches next time. The recorded
        ``applied_launch_options`` are reset to NULL in the same write UoW, so
        the delta apply (ADR-0025) skips nothing on that run: "force" also means
        force past the per-item skip — the one repair path for Steam-side drift
        the recorded value cannot see (a manually edited or corrupted shortcut).
        The re-apply re-records every value.

        The ``SyncRun`` history is deliberately **preserved**: it feeds no skip
        gate (the fetcher never reads it), and it is the source of the "Last
        sync" display — the newest completed run's ``last_sync`` plus the newest
        terminal run's last-attempt hint, both read by ``get_sync_stats``.
        Deleting it forced nothing and only blanked the display to "Never" right
        after a reset (#1318). Keeping it means a post-force preview shows
        collections/platforms as "unchanged" (they exist in Steam; membership
        did not change), which is correct.
        """
        with self._uow_factory() as uow:
            uow.platform_sync_state.clear()
            uow.collection_sync_state.clear()
            uow.roms.clear_all_applied_launch_options()
        self._logger.info("Sync cache cleared — next sync will fully re-fetch and re-apply")
        return {"success": True, "message": "Next sync will fully re-fetch and re-apply"}

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
        last_sync, last_attempt, rom_count = self._read_sync_stats_io()
        return {
            "last_sync": last_sync,
            "last_attempt": last_attempt,
            "platforms": enabled_platform_count,
            "collections": enabled_collection_count,
            "roms": rom_count,
            "total_shortcuts": rom_count,
        }

    def _read_sync_stats_io(self) -> tuple[str | None, dict[str, str] | None, int]:
        """Read ``(last_sync_iso, last_attempt, bound_rom_count)`` from SQLite.

        ``last_sync`` is the ``finished_at`` of the latest completed ``SyncRun``;
        ``last_attempt`` surfaces the newest cancelled/interrupted/paused/errored run when
        it is newer than that (see :meth:`_last_attempt`); the ROM count is the
        bound-shortcut count in ``roms``.
        """
        with self._uow_factory() as uow:
            completed = uow.sync_runs.get_latest_completed()
            terminal = uow.sync_runs.get_latest_terminal()
            rom_count = sum(1 for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None)
        last_sync = completed.finished_at if completed is not None else None
        return last_sync, self._last_attempt(completed, terminal), rom_count

    @staticmethod
    def _last_attempt(completed: SyncRun | None, terminal: SyncRun | None) -> dict[str, str] | None:
        """The newest cancelled/interrupted/paused/errored run, but only when it is newer than the last completed one.

        A run that ended without completing (cancelled, interrupted, paused, or errored)
        still applied shortcuts; without this the last-completed-only ``last_sync``
        read reports "Never" even after thousands of games synced. Returns ``None``
        when the newest terminal run completed cleanly (``last_sync`` already covers
        it) or when a completed run is at least as recent as the attempt.
        ``finished_at`` is guaranteed set on a terminal run
        (mark_cancelled/mark_interrupted/mark_errored stamp it).
        """
        if terminal is None or terminal.status == "completed":
            return None
        if completed is not None and (terminal.finished_at or "") <= (completed.finished_at or ""):
            return None
        return {"finished_at": terminal.finished_at or "", "status": terminal.status}

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
