"""ShortcutRemovalService — Steam-shortcut removal and ROM unbinding.

The home for clearing a ROM's Steam-shortcut binding: both the user-driven
removal flows (the frontend removes shortcuts via SteamClient, this service
unbinds the rows) and the sync-start reconcile against Steam's live shortcut
set (a shortcut the user deleted through Steam's own UI is unbound so the next
sync recreates it — #1046). Unbinding clears ``shortcut_app_id`` and keeps the
row and its per-ROM children (ADR-0007), never deletes. Every unbind here also
invalidates the touched platforms' completion stamps (ADR-0023) — and any
collection stamp whose member set contained a removed ROM (#742) — so the next
sync's incremental-skip gate can't skip a platform/collection whose shortcuts
were removed locally and leave the removal never recreated. Reads the synced-shortcut binding
from ``uow.roms``; the offline ``platform_slug → display_name`` label comes from
the ``kv_config`` cache the library sync refreshes each run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.platform_names import decode_platform_names
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import asyncio
    import logging

    from models.state import ShortcutRegistryEntry

    from services.protocols import (
        ArtworkRemover,
        SteamConfigStore,
        UnitOfWorkFactory,
    )

# kv_config key for the offline ``platform_slug → display_name`` cache the
# library sync refreshes every run. Read here so the DangerZone "clear
# platform" response shows "Nintendo 64" rather than the bare "n64" slug when
# RomM is unreachable. Mirrors ``library.reporter._PLATFORM_NAMES_KEY``.
_PLATFORM_NAMES_KEY = "platform_names"


@dataclass(frozen=True)
class ShortcutRemovalServiceConfig:
    """Frozen wiring bundle handed to ``ShortcutRemovalService.__init__``.

    Holds the Protocol-typed Steam-config adapter, runtime infrastructure, the
    artwork remover peer, and the SQLite Unit-of-Work factory (the transactional
    seam over the ``roms`` / ``kv_config`` repositories ShortcutRemovalService
    reads and unbinds).
    """

    steam_config: SteamConfigStore
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    artwork_remover: ArtworkRemover
    uow_factory: UnitOfWorkFactory


class ShortcutRemovalService:
    """Resolves shortcut removal sets and unbinds the affected ROMs in SQLite."""

    def __init__(self, *, config: ShortcutRemovalServiceConfig) -> None:
        self._steam_config = config.steam_config
        self._loop = config.loop
        self._logger = config.logger
        self._artwork_remover = config.artwork_remover
        self._uow_factory = config.uow_factory

    # ── Removal queries ────────────────────────────────────────────────────

    def remove_all_shortcuts(self) -> dict[str, Any]:
        """Return app_ids and rom_ids for the frontend to remove via SteamClient.

        Bound ROMs contribute their ``shortcut_app_id``; every ROM contributes
        its ``rom_id`` (the frontend reports back the full removed set). Unbound
        rows (NULL ``shortcut_app_id``) have no Steam shortcut, so they carry no
        ``app_id``.
        """
        with self._uow_factory() as uow:
            roms = list(uow.roms.iter_all())
        app_ids = [rom.shortcut_app_id for rom in roms if rom.shortcut_app_id is not None]
        rom_ids = [str(rom.rom_id) for rom in roms]
        return {"success": True, "app_ids": app_ids, "rom_ids": rom_ids}

    async def remove_platform_shortcuts(self, platform_slug: str) -> dict[str, Any]:
        """Return app_ids and rom_ids for a platform for the frontend to remove via SteamClient.

        Filters ``uow.roms`` by ``platform_slug`` directly; the display name in
        the response is resolved from the offline ``kv_config`` cache, falling
        back to the slug when RomM has never been seen for it.
        """
        try:
            return await self._loop.run_in_executor(None, self._remove_platform_shortcuts_io, platform_slug)
        except Exception as e:
            self._logger.error(f"Failed to get platform shortcuts: {e}")
            return {
                "success": False,
                "reason": ErrorCode.UNKNOWN.value,
                "message": f"Failed: {e}",
                "app_ids": [],
                "rom_ids": [],
            }

    def _remove_platform_shortcuts_io(self, platform_slug: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            roms = list(uow.roms.iter_by_platform(platform_slug))
            platform_name = self._read_platform_name_cache(uow).get(platform_slug, platform_slug)
        app_ids = [rom.shortcut_app_id for rom in roms if rom.shortcut_app_id is not None]
        rom_ids = [str(rom.rom_id) for rom in roms]
        return {"success": True, "app_ids": app_ids, "rom_ids": rom_ids, "platform_name": platform_name}

    def _read_platform_name_cache(self, uow) -> dict[str, str]:
        """Decode the ``platform_slug → display_name`` cache, ``{}`` when absent/corrupt."""
        return decode_platform_names(uow.kv_config.get(_PLATFORM_NAMES_KEY))

    # ── Removal results ────────────────────────────────────────────────────

    def _report_removal_results_io(self, removed_rom_ids: list[int | str]) -> None:
        """Sync helper for report_removal_results — Steam-Input reset, artwork deletion, unbind."""
        with self._uow_factory() as uow:
            roms = {rom_id: uow.roms.get(int(rom_id)) for rom_id in removed_rom_ids}

        # Clean up Steam Input config for removed shortcuts (always reset to default).
        removed_app_ids = [
            rom.shortcut_app_id for rom in roms.values() if rom is not None and rom.shortcut_app_id is not None
        ]
        if removed_app_ids:
            try:
                self._steam_config.set_steam_input_config(removed_app_ids, mode="default")
            except Exception as e:
                self._logger.error(f"Failed to clean up Steam Input config: {e}")

        grid = self._steam_config.grid_dir()
        for rom_id in removed_rom_ids:
            rom = roms.get(rom_id)
            if rom is not None and grid:
                self._artwork_remover.remove_artwork_files(grid, rom_id, self._artwork_entry(rom))

        # Unbind the removed ROMs — clear the Steam link, keep the row (ADR-0007) —
        # and invalidate the completion stamp (ADR-0023) of every platform this
        # removal touched. Unbinding keeps the row, so the platform's persisted-row
        # count is unchanged and a still-valid stamp would let the next sync's
        # incremental-skip gate skip the platform wholesale and never recreate the
        # removed shortcuts (the #1025 silent-gap class). Both DangerZone flows —
        # remove-all and per-platform removal — funnel their unbind here, so
        # deleting the stamp per touched slug covers each: remove-all reports every
        # ROM (all platforms invalidated), a per-platform removal reports only that
        # platform's ROMs (only its slug invalidated). Same write UoW as the unbind.
        removed_ids: set[int] = set()
        with self._uow_factory() as uow:
            touched_slugs: set[str] = set()
            for rom_id in removed_rom_ids:
                removed_ids.add(int(rom_id))
                rom = uow.roms.get(int(rom_id))
                if rom is None:
                    continue
                touched_slugs.add(rom.platform_slug)
                if rom.shortcut_app_id is None:
                    continue
                rom.unbind_shortcut()
                uow.roms.save(rom)
            for slug in touched_slugs:
                uow.platform_sync_state.delete(slug)
            self._invalidate_collection_stamps_for(uow, removed_ids)

    @staticmethod
    def _invalidate_collection_stamps_for(uow, removed_ids: set[int]) -> None:
        """Drop any collection stamp whose member set intersects the removed ROMs.

        The collection sibling of the platform-stamp invalidation (#742 /
        ADR-0023): a collection member losing its Steam shortcut (removed locally)
        must re-fetch + re-apply that collection next sync, else the collection's
        incremental skip would rebuild the Steam collection from a stale member
        set and never recreate the removed shortcut. Surgical — only collections
        that actually contained a removed ROM lose their stamp (a collection id
        can't be mapped from a platform slug, so this scans the stamps' stored
        member sets). Shares the caller's write UoW.
        """
        if not removed_ids:
            return
        for stamp in list(uow.collection_sync_state.iter_all()):
            if removed_ids.intersection(stamp.member_rom_ids):
                uow.collection_sync_state.delete(stamp.collection_id, stamp.collection_kind)

    @staticmethod
    def _artwork_entry(rom) -> ShortcutRegistryEntry:
        """Project the ROM's artwork-relevant fields into the entry shape the remover reads."""
        entry: dict[str, object] = {"cover_path": rom.cover_path or ""}
        if rom.shortcut_app_id is not None:
            entry["app_id"] = rom.shortcut_app_id
        return entry  # type: ignore[return-value]

    async def report_removal_results(self, removed_rom_ids: list[int | str]) -> dict[str, Any]:
        """Called by frontend after removing shortcuts via SteamClient."""
        await self._loop.run_in_executor(None, self._report_removal_results_io, removed_rom_ids)
        return {"success": True, "message": f"Removed {len(removed_rom_ids)} shortcuts"}

    # ── Live-shortcut reconcile ────────────────────────────────────────────

    def _reconcile_live_shortcuts_io(self, live_app_ids: list[int | str]) -> int:
        """Unbind every bound ROM whose ``shortcut_app_id`` is absent from the live set.

        *live_app_ids* is the set of appIds the frontend observed in Steam's live
        shortcut store (every shortcut whose exe is the plugin launcher). Each
        bound ROM (``shortcut_app_id`` not NULL) whose appId is **not** in that
        set lost its Steam shortcut out-of-band — unbind it (ADR-0007: clear the
        link, keep the row and its per-ROM children), so the next sync's
        incremental skip no longer counts it and the unit re-fetches to recreate
        the shortcut. Returns the count unbound. Defensive ``int`` coercion: the
        frontend may serialize appIds as strings, and a non-numeric entry is
        dropped (it can never match a numeric ``shortcut_app_id``).
        """
        live: set[int] = set()
        for raw in live_app_ids:
            try:
                live.add(int(raw))
            except (TypeError, ValueError):
                continue

        unbound = 0
        touched_slugs: set[str] = set()
        unbound_ids: set[int] = set()
        with self._uow_factory() as uow:
            for rom in list(uow.roms.iter_all()):
                if rom.shortcut_app_id is None or rom.shortcut_app_id in live:
                    continue
                touched_slugs.add(rom.platform_slug)
                unbound_ids.add(rom.rom_id)
                rom.unbind_shortcut()
                uow.roms.save(rom)
                unbound += 1
            # A shortcut deleted through Steam's own UI leaves the row's persisted
            # count unchanged, so its platform's completion stamp (ADR-0023) would
            # still let the next sync skip the platform and never recreate the
            # shortcut — the same silent-gap class as the DangerZone flows (#1025).
            # Invalidate the stamp of every platform we unbound here, in the same
            # write UoW, so the platform full-fetches and recreates the shortcut
            # (completing the #1046 recovery under the persisted-count skip).
            for slug in touched_slugs:
                uow.platform_sync_state.delete(slug)
            self._invalidate_collection_stamps_for(uow, unbound_ids)
        return unbound

    async def reconcile_live_shortcuts(self, live_app_ids: list[int | str]) -> dict[str, Any]:
        """Reconcile bound ROMs against the live Steam-shortcut set the frontend supplies.

        Called at sync start with the appIds of every RomM shortcut still present
        in Steam's live shortcut store. Bindings absent from that set are stale
        (the user deleted the shortcut via Steam's own UI) and are unbound so the
        next sync recreates them — fixing the "deleted shortcut never comes back"
        loop (#1046). An empty *live_app_ids* means the frontend's scan found zero
        RomM shortcuts in Steam, so every binding is unbound; the frontend MUST
        only call this when its scan actually ran (Steam's store was readable),
        never on a scan it could not perform.
        """
        try:
            unbound = await self._loop.run_in_executor(None, self._reconcile_live_shortcuts_io, live_app_ids)
        except Exception as e:
            self._logger.error(f"Failed to reconcile live shortcuts: {e}")
            return {
                "success": False,
                "reason": ErrorCode.UNKNOWN.value,
                "message": f"Reconcile failed: {e}",
            }
        if unbound:
            self._logger.info(f"Reconcile: unbound {unbound} stale Steam shortcut(s)")
        return {"success": True, "unbound_count": unbound, "message": f"Unbound {unbound} stale shortcut(s)"}
