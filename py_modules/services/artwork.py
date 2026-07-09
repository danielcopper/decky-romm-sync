"""ArtworkService — cover art download, per-ROM cache, grid publish, cleanup.

Cover art is downloaded once per RomM ID into the plugin-owned per-ROM cover
cache (``{rom_id}.png``), the single source of truth for a ROM's cover. The
active version of a sibling group is *published* onto the shared Steam grid as
``{app_id}p.png`` (a copy, so every sibling keeps its own cache file, ADR-0021).
The persisted ``roms.cover_path`` records the cache path; the frontend reads a
ROM's cover by RomM ID through the base64 query callables.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.artwork_paths import TMP_SUFFIX, cache_filename, final_filename, staging_filename, with_tmp_suffix
from domain.sync_stage import SyncStage
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Awaitable, Callable

    from models.state import ShortcutRegistryEntry

    from services.protocols import (
        CoverArtFileStore,
        PendingSyncReader,
        RommRomReader,
        SteamConfigStore,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class ArtworkServiceConfig:
    """Frozen wiring bundle handed to ``ArtworkService.__init__``.

    Holds the Protocol-typed adapters, runtime infrastructure, the read seam
    ArtworkService uses to consult the in-flight sync's pending cover paths, and
    ``cover_cache_dir`` — the plugin-owned per-ROM cover cache directory (built
    in bootstrap, never the shared Steam grid dir).
    """

    romm_api: RommRomReader
    steam_config: SteamConfigStore
    cover_art_file_store: CoverArtFileStore
    cover_cache_dir: str
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    get_pending_sync: PendingSyncReader
    uow_factory: UnitOfWorkFactory


class ArtworkService:
    """Manages artwork downloading, caching, grid publishing, and cleanup."""

    def __init__(self, *, config: ArtworkServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._steam_config = config.steam_config
        self._cover_art_file_store = config.cover_art_file_store
        self._cover_cache_dir = config.cover_cache_dir
        self._loop = config.loop
        self._logger = config.logger
        self._get_pending_sync = config.get_pending_sync
        self._uow_factory = config.uow_factory

    def _cache_path(self, rom_id: int | str) -> str:
        """Return the per-ROM cover cache path for *rom_id*."""
        return os.path.join(self._cover_cache_dir, cache_filename(rom_id))

    # ── Existing cover path check ──────────────────────────────────────────

    def existing_cover_path(self, rom_id: int, grid: str) -> str | None:
        """Return an existing grid cover path for *rom_id*, or ``None``.

        The grid-side fallback for the base64 query: the canonical
        ``{app_id}p.png`` for a bound ROM, else a legacy staging file.
        """
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
        if rom is not None and rom.shortcut_app_id is not None:
            final = os.path.join(grid, final_filename(rom.shortcut_app_id))
            if self._cover_art_file_store.exists(final):
                return final

        staging = os.path.join(grid, staging_filename(rom_id))
        if self._cover_art_file_store.exists(staging):
            return staging

        return None

    # ── Artwork download ───────────────────────────────────────────────────

    async def download_artwork(
        self,
        all_roms: list[dict[str, Any]],
        emit_progress: Callable[..., Awaitable[None]],
        is_cancelling: Callable[[], bool],
        progress_step: int = 4,
        progress_total_steps: int = 6,
    ) -> dict[int, str]:
        """Download cover artwork into the per-ROM cache, returning rom_id → cache path.

        Per ROM the short-circuit is: reuse an existing cache file; else seed the
        cache from an already-present grid cover (the released single-version
        flow — avoids a mass re-download on the first sync after the cache
        landed); else download from RomM. The returned cache path becomes the
        pending-sync ``cover_path`` that ``finalize_cover_path`` publishes onto
        the grid once the Steam app_id is known.
        """
        cover_paths: dict[int, str] = {}
        grid = self._steam_config.grid_dir()
        if not grid:
            self._logger.warning("Cannot find grid directory, skipping artwork")
            return cover_paths

        self._cover_art_file_store.make_dirs(self._cover_cache_dir)
        total = len(all_roms)

        for i, rom in enumerate(all_roms):
            if is_cancelling():
                return cover_paths

            await emit_progress(
                SyncStage.APPLYING,
                current=i + 1,
                total=total,
                message=f"Downloading artwork {i + 1}/{total}",
                step=progress_step,
                total_steps=progress_total_steps,
            )

            cover_url = rom.get("path_cover_large") or rom.get("path_cover_small")
            if not cover_url:
                continue

            rom_id = rom["id"]
            cache_path = self._cache_path(rom_id)
            existing = self._resolve_cached_cover(rom_id, cache_path, grid)
            if existing:
                cover_paths[rom_id] = existing
                continue

            try:
                await self._loop.run_in_executor(None, self._download_cover_atomic, cover_url, cache_path)
                cover_paths[rom_id] = cache_path
            except Exception as e:
                self._logger.warning(f"Failed to download artwork for {rom['name']}: {e}")

        return cover_paths

    def _resolve_cached_cover(self, rom_id: int, cache_path: str, grid: str) -> str | None:
        """Return a ready cache cover for *rom_id*, or ``None`` when a download is needed.

        A cache hit reuses the file. On a miss, a **single-version** bound ROM
        whose grid ``{app_id}p.png`` already exists seeds the cache from it
        (grid → cache copy) so the released single-version install is not
        re-downloaded. A multi-version group is *never* seeded: its members share
        one grid file that holds whatever version was last published, so seeding
        would copy a different version's art into this ROM's cache — those
        members download their own cover fresh instead (ADR-0021 / #1346).
        """
        if self._cover_art_file_store.exists(cache_path):
            return cache_path
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            seedable = rom is not None and rom.shortcut_app_id is not None and not self._is_multi_version(uow, rom)
            app_id = rom.shortcut_app_id if rom is not None else None
        if seedable and app_id is not None:
            final = os.path.join(grid, final_filename(app_id))
            if self._cover_art_file_store.exists(final) and self._seed_cache(final, cache_path):
                return cache_path
        return None

    @staticmethod
    def _is_multi_version(uow: Any, rom: Any) -> bool:
        """True when *rom*'s sibling group holds more than one synced row.

        A NULL ``sibling_group_key`` is a solo row (single version). A non-NULL
        key may still be a single-version group (synced singles carry a derived
        key), so group *size* — not key presence — is the test.
        """
        group_key = rom.sibling_group_key
        if group_key is None:
            return False
        members = iter(uow.roms.iter_by_group_key(group_key))
        if next(members, None) is None:
            return False
        return next(members, None) is not None

    def _seed_cache(self, src: str, cache_path: str) -> bool:
        """Copy an existing grid cover into the per-ROM cache; report success."""
        try:
            self._copy_atomic(src, cache_path)
            return True
        except OSError as e:
            self._logger.warning(f"Failed to seed cover cache from {src}: {e}")
            return False

    # ── Atomic write helpers ───────────────────────────────────────────────

    def _download_cover_atomic(self, cover_url: str, dest: str) -> None:
        """Download *cover_url* into ``dest.tmp`` then atomically rename over *dest*.

        A reader of *dest* sees either the old file or the complete new one,
        never a partially-streamed download. The sidecar is removed on any
        failure so a broken write leaves no ``.tmp`` behind.
        """
        tmp = with_tmp_suffix(dest)
        try:
            self._romm_api.download_cover(cover_url, tmp)
            self._cover_art_file_store.rename(tmp, dest)
        except Exception:
            self._cover_art_file_store.remove_file(tmp)
            raise

    def _copy_atomic(self, src: str, dest: str) -> None:
        """Copy *src* into ``dest.tmp`` then atomically rename over *dest*.

        Publishes the grid tile / seeds the cache without a concurrent reader
        ever seeing a half-copied file. The sidecar is removed on failure.
        """
        tmp = with_tmp_suffix(dest)
        try:
            self._cover_art_file_store.copy_file(src, tmp)
            self._cover_art_file_store.rename(tmp, dest)
        except OSError:
            self._cover_art_file_store.remove_file(tmp)
            raise

    # ── Artwork finalisation ───────────────────────────────────────────────

    def finalize_cover_path(self, grid: str | None, cover_path: str, app_id: int, rom_id_str: str) -> str:
        """Publish the per-ROM cache cover onto the grid as ``{app_id}p.png``.

        Copies (never renames) the cache cover so the per-ROM cache file
        survives — the group's siblings each keep their own cover even though
        they share the one grid file. Returns *cover_path* (the cache path, the
        persisted ``roms.cover_path``). A legacy row whose ``cover_path`` is
        already the grid file, or a missing staged file, degrades to the grid
        path so callers still resolve an on-disk cover.
        """
        if not grid or not cover_path:
            return cover_path
        final_path = os.path.join(grid, final_filename(app_id))
        if not self._cover_art_file_store.exists(cover_path):
            return final_path if self._cover_art_file_store.exists(final_path) else cover_path
        if cover_path != final_path:
            try:
                self._copy_atomic(cover_path, final_path)
            except OSError as e:
                self._logger.warning(f"Failed to copy artwork for rom {rom_id_str}: {e}")
        return cover_path

    # ── Artwork removal ────────────────────────────────────────────────────

    def remove_artwork_files(self, grid: str, rom_id: str | int, entry: ShortcutRegistryEntry) -> None:
        """Remove all artwork files for a registry entry, including the cache cover."""
        removed = False
        # Try cover_path first (the persisted cache or grid path)
        cover_path = entry.get("cover_path", "")
        if cover_path and self._cover_art_file_store.exists(cover_path):
            self._cover_art_file_store.remove_file(cover_path)
            removed = True
        # Try {app_id}p.png (the standard Steam grid filename)
        if not removed and entry.get("app_id"):
            app_path = os.path.join(grid, final_filename(entry["app_id"]))
            if self._cover_art_file_store.exists(app_path):
                self._cover_art_file_store.remove_file(app_path)
                removed = True
        # Fallback: legacy artwork_id format
        if not removed:
            artwork_id = entry.get("artwork_id")
            if artwork_id:
                art_path = os.path.join(grid, final_filename(artwork_id))
                if self._cover_art_file_store.exists(art_path):
                    self._cover_art_file_store.remove_file(art_path)
        # Clean up any leftover staging file
        staging = os.path.join(grid, staging_filename(rom_id))
        if self._cover_art_file_store.exists(staging):
            self._cover_art_file_store.remove_file(staging)
        # Remove the per-ROM cover cache file
        cache_path = self._cache_path(rom_id)
        if self._cover_art_file_store.exists(cache_path):
            self._cover_art_file_store.remove_file(cache_path)

    # ── Artwork base64 query ───────────────────────────────────────────────

    async def _read_base64(self, path: str) -> str | None:
        """Read *path* and return its base64 string, or ``None`` on failure."""
        try:
            data = await self._loop.run_in_executor(None, self._cover_art_file_store.read_bytes, path)
            return base64.b64encode(data).decode("ascii")
        except Exception as e:
            self._logger.warning(f"Failed to read artwork for {path}: {e}")
            return None

    async def get_artwork_base64(self, rom_id: int) -> dict[str, Any]:
        """Return base64-encoded cover artwork for a single ROM (read-only).

        Resolution order: the in-flight sync's pending cover path → the ROM
        row's persisted ``cover_path`` (a cache path, or a legacy grid path —
        read as-is) → the per-ROM cache file → a legacy staging file → the grid
        ``{app_id}p.png`` fallback.
        """
        # 1. Pending sync data (in-flight sync's staged cache path)
        pending = self._get_pending_sync().get(rom_id, {})
        cover_path = pending.get("cover_path", "")

        # 2. Persisted cover path on the ROM row (cache or legacy grid path)
        if not cover_path:
            with self._uow_factory() as uow:
                rom = uow.roms.get(rom_id)
            cover_path = (rom.cover_path or "") if rom is not None else ""

        # 3. Per-ROM cover cache file
        if not cover_path:
            cache_path = self._cache_path(rom_id)
            if self._cover_art_file_store.exists(cache_path):
                cover_path = cache_path

        # 4/5. Legacy staging + grid {app_id}p.png fallback (grid-dir bound)
        grid = self._steam_config.grid_dir()
        if not cover_path and grid:
            staging = os.path.join(grid, staging_filename(rom_id))
            if self._cover_art_file_store.exists(staging):
                cover_path = staging
        if not cover_path and grid:
            fallback = self.existing_cover_path(rom_id, grid)
            if fallback:
                cover_path = fallback

        if cover_path and self._cover_art_file_store.exists(cover_path):
            b64 = await self._read_base64(cover_path)
            if b64 is not None:
                return {"base64": b64}

        return {"base64": None}

    async def fetch_cover_base64(self, rom_id: int) -> dict[str, Any]:
        """Cache-first cover fetch for the version picker (ADR-0021).

        A cache hit reads the per-ROM cache cover directly; a miss fetches the
        ROM's cover from RomM into the cache and returns the fresh bytes. Works
        for a group version that has no local ``roms`` row (the picker lists
        not-yet-synced siblings). Every failure — server unreachable, no cover,
        read error — returns ``{"base64": None}`` silently; a data callable, not
        a ``{success, reason, message}`` result. Never re-downloads a cached
        cover.
        """
        rom_id = int(rom_id)
        cache_path = self._cache_path(rom_id)
        if self._cover_art_file_store.exists(cache_path):
            return {"base64": await self._read_base64(cache_path)}

        try:
            rom = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
        except Exception as e:
            self._logger.warning(f"fetch_cover: failed to fetch rom {rom_id}: {e}")
            return {"base64": None}
        if not rom:
            return {"base64": None}

        cover_url = rom.get("path_cover_large") or rom.get("path_cover_small")
        if not cover_url:
            return {"base64": None}

        self._cover_art_file_store.make_dirs(self._cover_cache_dir)
        try:
            await self._loop.run_in_executor(None, self._download_cover_atomic, cover_url, cache_path)
        except Exception as e:
            self._logger.warning(f"fetch_cover: failed to download cover for rom {rom_id}: {e}")
            return {"base64": None}

        return {"base64": await self._read_base64(cache_path)}

    # ── Cover refresh (single-ROM repair) ──────────────────────────────────

    async def refresh_cover(self, rom_id: int) -> dict[str, Any]:
        """Re-download a ROM's RomM cover into the cache and republish it.

        Looks up the ROM's current ``shortcut_app_id`` from ``uow.roms``,
        fetches the fresh cover URL from RomM, downloads it into the per-ROM
        cache (overwriting any stale cache file — this is an explicit repair, so
        it never short-circuits on a cache hit), publishes the cache cover onto
        the grid as ``{app_id}p.png``, and records the cache path via
        ``Rom.update_cover_path``. ADR-0006: the read and write each own a short
        UoW with the RomM/file I/O in between, outside any transaction. Returns
        the canonical ``{success, reason, message}`` failure shape on every
        failure branch — see ``lib/list_result.py``.
        """
        app_id = await self._loop.run_in_executor(None, self._read_bound_app_id, rom_id)
        if app_id is None:
            return {
                "success": False,
                "reason": "not_synced",
                "message": "ROM is not synced to Steam",
            }

        grid = self._steam_config.grid_dir()
        if not grid:
            return {
                "success": False,
                "reason": "no_grid_dir",
                "message": "Steam grid directory not found",
            }

        try:
            rom = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
        except Exception as e:
            self._logger.warning(f"refresh_cover: failed to fetch rom {rom_id}: {e}")
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Could not fetch ROM from server",
            }
        if not rom:
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Could not fetch ROM from server",
            }

        cover_url = rom.get("path_cover_large") or rom.get("path_cover_small")
        if not cover_url:
            return {
                "success": False,
                "reason": "no_cover",
                "message": "ROM has no cover artwork",
            }

        cache_path = self._cache_path(rom_id)
        self._cover_art_file_store.make_dirs(self._cover_cache_dir)
        try:
            await self._loop.run_in_executor(None, self._download_cover_atomic, cover_url, cache_path)
        except Exception as e:
            self._logger.warning(f"refresh_cover: failed to download cover for rom {rom_id}: {e}")
            return {
                "success": False,
                "reason": "download_failed",
                "message": str(e),
            }

        self.finalize_cover_path(grid, cache_path, app_id, str(rom_id))
        await self._loop.run_in_executor(None, self._persist_cover_path, rom_id, cache_path)

        return {
            "success": True,
            "message": "Cover refreshed",
            "cover_path": cache_path,
        }

    def _read_bound_app_id(self, rom_id: int) -> int | None:
        """Return the ROM's ``shortcut_app_id``, or ``None`` when unsynced/unbound."""
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
        return rom.shortcut_app_id if rom is not None else None

    def _persist_cover_path(self, rom_id: int, cover_path: str) -> None:
        """Record *cover_path* on the ROM row in a short write UoW."""
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            if rom is None:
                return
            rom.update_cover_path(cover_path)
            uow.roms.save(rom)

    # ── Staging file housekeeping ──────────────────────────────────────────

    def is_staging_file_orphaned(self, grid: str, registry: dict[str, int], rom_id: str) -> bool:
        """Check if a staging artwork file is orphaned (not bound or has final artwork).

        *registry* is a ``{str(rom_id): shortcut_app_id}`` map of the
        currently-bound ROMs (built from ``uow.roms``). A rom_id absent
        from it is unbound/stale → orphaned. A bound rom_id whose final
        ``{app_id}p.png`` already exists no longer needs the staging file.
        """
        if rom_id not in registry:
            return True
        app_id = registry[rom_id]
        if app_id:
            final = os.path.join(grid, final_filename(app_id))
            return self._cover_art_file_store.exists(final)
        return False

    def prune_orphaned_staging_artwork(self) -> None:
        """Remove orphaned romm_{rom_id}_cover.png staging files from Steam grid dir."""
        grid = self._steam_config.grid_dir()
        if not grid or not self._cover_art_file_store.is_dir(grid):
            return
        with self._uow_factory() as uow:
            registry = {
                str(rom.rom_id): rom.shortcut_app_id for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None
            }
        pruned = []
        for filename in self._cover_art_file_store.listdir(grid):
            if not filename.startswith("romm_") or not filename.endswith("_cover.png"):
                continue
            try:
                rom_id = filename[len("romm_") : -len("_cover.png")]
                int(rom_id)  # validate it's numeric
            except (ValueError, IndexError):
                continue
            if not self.is_staging_file_orphaned(grid, registry, rom_id):
                continue
            try:
                self._cover_art_file_store.remove_file(os.path.join(grid, filename))
                pruned.append(filename)
            except OSError as e:
                self._logger.warning(f"Failed to remove orphaned staging artwork {filename}: {e}")
        if pruned:
            self._logger.info(f"Pruned {len(pruned)} orphaned staging artwork file(s)")

    def prune_orphaned_cover_cache(self) -> None:
        """Remove per-ROM cover cache files whose rom_id no longer has a ``roms`` row.

        A cache entry is keyed by RomM ID; every synced sibling (bound or not)
        keeps a row, so its cover survives. A file for a rom_id absent from
        ``roms`` — a removed ROM, or a server-only version whose cover the picker
        cached without ever syncing it — is orphaned.
        """
        cache_dir = self._cover_cache_dir
        if not self._cover_art_file_store.is_dir(cache_dir):
            return
        with self._uow_factory() as uow:
            known = {rom.rom_id for rom in uow.roms.iter_all()}
        pruned = []
        for filename in self._cover_art_file_store.listdir(cache_dir):
            # Sweep any leftover atomic-write sidecar (a crash between write and
            # rename): it belongs to no ROM's live cache.
            if filename.endswith(TMP_SUFFIX):
                if self._remove_cache_entry(cache_dir, filename):
                    pruned.append(filename)
                continue
            if not filename.endswith(".png"):
                continue
            try:
                rom_id = int(filename[: -len(".png")])
            except ValueError:
                continue
            if rom_id in known:
                continue
            if self._remove_cache_entry(cache_dir, filename):
                pruned.append(filename)
        if pruned:
            self._logger.info(f"Pruned {len(pruned)} orphaned cover cache file(s)")

    def _remove_cache_entry(self, cache_dir: str, filename: str) -> bool:
        """Remove one cover-cache entry; return whether it was removed."""
        try:
            self._cover_art_file_store.remove_file(os.path.join(cache_dir, filename))
            return True
        except OSError as e:
            self._logger.warning(f"Failed to remove orphaned cover cache {filename}: {e}")
            return False
