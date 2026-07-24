"""ArtworkService — cover art download, per-ROM cache, grid publish, cleanup.

Cover art is downloaded per RomM ID into the plugin-owned per-ROM cover cache
(``{rom_id}.png``), the single source of truth for a ROM's cover. The active
version of a sibling group is *published* onto the shared Steam grid as
``{app_id}p.png`` (a copy, so every sibling keeps its own cache file, ADR-0021).
The persisted ``roms.cover_path`` records the cache path; the frontend reads a
ROM's cover by RomM ID through the base64 query callables.

A cache entry is valid only while the server's cover is unchanged: the
persisted ``roms.cover_source`` fingerprint (the full RomM cover source string,
``?ts=…`` cache-buster included) records which server cover the cached bytes
came from, and every sync compares it against the fresh fetch (#1386). A
mismatch re-downloads the cache and republishes the grid copy; a NULL
fingerprint with an existing cache file is adopted without a download, so the
fingerprint's introduction never mass re-downloads an existing library.

When the RomM-local cover asset returns a definitive HTTP 404, the download
retries once against the ROM's external ``url_cover`` (a metadata-provider CDN
such as SteamGridDB / IGDB) before giving up — the RomM bearer is never sent to
that third-party host (#1450). The fingerprint records the source *actually*
applied (``url_cover`` on a fallback), so a later fixed RomM asset or changed
``url_cover`` is still detected as a change.

When the fingerprint changed by ONLY its ``?ts=`` cache-buster (a server-side
rescan re-stamps every ROM's ``updated_at`` without touching the cover files),
the cached bytes are REVALIDATED with a conditional request instead of
re-downloaded (#1454): each regular download records the response's HTTP
validator (``ETag`` / ``Last-Modified``) in a ``{rom_id}.cover-meta.json``
sidecar beside the cache file, and a ts-only fingerprint change re-requests with
``If-None-Match`` (else ``If-Modified-Since``) — a ``304`` keeps the bytes and
adopts the fresh fingerprint, a ``200`` replaces them. Validators are an optional
capability: a ROM with no stored validator (or a server/proxy that sends none)
falls back to a plain download, unchanged.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.artwork_paths import (
    COVER_META_SUFFIX,
    TMP_SUFFIX,
    cache_filename,
    cover_meta_filename,
    final_filename,
    grid_image_filenames,
    is_shortcut_app_id,
    parse_grid_image_app_id,
    staging_filename,
    with_tmp_suffix,
)
from domain.cover_refresh import cover_ts_only_change, scan_cover_refresh_candidates
from domain.sync_stage import SyncStage
from lib.errors import RommNotFoundError, classify_error
from lib.list_result import ErrorCode


@dataclass(frozen=True)
class _CoverWrite:
    """The outcome of one cover download-or-revalidate (#1454, service-internal).

    ``applied_source`` is the cover source now reflected by the cache bytes —
    the fresh ``cover_url`` normally, or the ROM's ``url_cover`` when the #1450
    fallback wins. ``not_modified`` is ``True`` when a conditional request drew a
    304 (the cache bytes were kept). ``etag``/``last_modified`` are the validators
    to record in the sidecar, or ``None`` when the server sent none.
    """

    applied_source: str
    not_modified: bool
    etag: str | None
    last_modified: str | None


# Emit a cover-download progress frame on the first cover, every Nth cover, and
# the last one, so a large library's cover phase narrates its progress (a moving
# counter, not a frozen bar) without a WebSocket frame per cover.
_COVER_PROGRESS_INTERVAL = 50

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

    def _meta_path(self, rom_id: int | str) -> str:
        """Return the per-ROM cover-validator sidecar path for *rom_id* (#1454)."""
        return os.path.join(self._cover_cache_dir, cover_meta_filename(rom_id))

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
        label: str = "",
        applied_sources: dict[int, str] | None = None,
    ) -> dict[int, str]:
        """Download cover artwork into the per-ROM cache, returning rom_id → cache path.

        Per ROM the short-circuit is: reuse an existing cache file; else seed the
        cache from an already-present grid cover (the released single-version
        flow — avoids a mass re-download on the first sync after the cache
        landed); else download from RomM. The returned cache path becomes the
        pending-sync ``cover_path`` that ``finalize_cover_path`` publishes onto
        the grid once the Steam app_id is known.

        ``applied_sources`` is an optional accumulator the caller passes to learn
        which cover source each resolved ROM's bytes actually came from —
        ``path_cover_large``/``path_cover_small`` for the common case, or the
        ROM's ``url_cover`` when the RomM asset 404s and the external fallback
        wins (#1450). It is filled per resolved ROM (fresh download, reuse, or
        grid seed), so the per-unit commit persists a truthful ``cover_source``
        fingerprint even for fallback covers — the caller keys its own
        derivation off it. Absent/unread, ``download_artwork`` behaves as before.

        Progress is narrated under the ``fetching`` stage with the ``covers``
        sub-stage (this runs in the per-unit prep phase, before the shortcuts
        are applied) as "Preparing covers for <label>", throttled to the
        first/last/every-Nth cover — the sub-stage places these frames in the
        unit's covers sub-slice of the bar (#1407). ``label`` is the owning
        unit's display name; blank falls back to a bare "Preparing covers".
        """
        cover_paths: dict[int, str] = {}
        grid = self._steam_config.grid_dir()
        if not grid:
            self._logger.warning("Cannot find grid directory, skipping artwork")
            return cover_paths

        self._cover_art_file_store.make_dirs(self._cover_cache_dir)
        total = len(all_roms)
        cover_target = f"Preparing covers for {label}" if label else "Preparing covers"

        for i, rom in enumerate(all_roms):
            if is_cancelling():
                return cover_paths

            processed = i + 1
            if processed == 1 or processed == total or processed % _COVER_PROGRESS_INTERVAL == 0:
                await emit_progress(
                    SyncStage.FETCHING,
                    current=processed,
                    total=total,
                    message=f"{cover_target} ({processed}/{total})",
                    step=progress_step,
                    total_steps=progress_total_steps,
                    sub_stage="covers",
                )

            cover_url = rom.get("path_cover_large") or rom.get("path_cover_small")
            if not cover_url:
                continue

            rom_id = rom["id"]
            cache_path = self._cache_path(rom_id)
            existing, stored_source = self._resolve_cached_cover(rom_id, cache_path, grid, cover_url)
            if existing:
                cover_paths[rom_id] = existing
                if applied_sources is not None:
                    applied_sources[rom_id] = cover_url
                continue

            try:
                result = await self._loop.run_in_executor(
                    None,
                    self._fetch_and_record_cover,
                    rom_id,
                    cover_url,
                    cache_path,
                    rom.get("url_cover"),
                    stored_source,
                )
                cover_paths[rom_id] = cache_path
                if applied_sources is not None:
                    applied_sources[rom_id] = result.applied_source
            except Exception as e:
                self._logger.warning(f"Failed to download artwork for {rom['name']}: {e}")

        return cover_paths

    def _resolve_cached_cover(
        self, rom_id: int, cache_path: str, grid: str, cover_url: str
    ) -> tuple[str | None, str | None]:
        """Resolve *rom_id*'s cache cover, returning ``(reuse_path, stored_source)``.

        ``reuse_path`` is a ready cache cover to reuse, or ``None`` when a
        download is needed; ``stored_source`` is the persisted
        ``roms.cover_source`` fingerprint (surfaced so the caller can decide
        between a plain download and a #1454 conditional revalidation without a
        second DB read).

        The reuse gate is fingerprint-aware (#1386): a persisted
        ``roms.cover_source`` that differs from the fresh *cover_url* means the
        server-side cover changed since the cache was written, so neither the
        cache file nor the grid seed may be reused — the caller downloads
        fresh. A ``None`` fingerprint (pre-migration row, or no row yet) adopts
        whatever local bytes exist, matching the pre-fingerprint behaviour.

        With the fingerprint unchanged/unknown: a cache hit reuses the file. On
        a miss, a **single-version** bound ROM whose grid ``{app_id}p.png``
        already exists seeds the cache from it (grid → cache copy) so the
        released single-version install is not re-downloaded. A multi-version
        group is *never* seeded: its members share one grid file that holds
        whatever version was last published, so seeding would copy a different
        version's art into this ROM's cache — those members download their own
        cover fresh instead (ADR-0021 / #1346).
        """
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            stored_source = rom.cover_source if rom is not None else None
            seedable = rom is not None and rom.shortcut_app_id is not None and not self._is_multi_version(uow, rom)
            app_id = rom.shortcut_app_id if rom is not None else None
        if stored_source is not None and stored_source != cover_url:
            return None, stored_source
        if self._cover_art_file_store.exists(cache_path):
            return cache_path, stored_source
        if seedable and app_id is not None:
            final = os.path.join(grid, final_filename(app_id))
            if self._cover_art_file_store.exists(final) and self._seed_cache(final, cache_path):
                return cache_path, stored_source
        return None, stored_source

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

    # ── Cover-cache invalidation pass (#1386) ──────────────────────────────

    async def refresh_changed_covers(
        self,
        all_roms: list[dict[str, Any]],
        registry: dict[str, dict[str, Any]],
        emit_progress: Callable[..., Awaitable[None]],
        is_cancelling: Callable[[], bool],
        progress_step: int = 4,
        progress_total_steps: int = 6,
        label: str = "",
    ) -> list[dict[str, int]]:
        """Refresh the cover cache of every BOUND fetched ROM whose server cover changed.

        The delta-restricted apply never re-downloads a skipped ROM's cover, so a
        server-side cover change would otherwise stay invisible forever. This
        pass runs over a unit's live fetch (*all_roms*) before the apply: for
        each bound ROM whose persisted ``cover_source`` differs from the fresh
        cover string, the cache file is re-downloaded (atomic tmp+rename — a
        failed download leaves the old bytes AND the old fingerprint intact, so
        the change is retried next sync), the grid ``{app_id}p.png`` copy is
        republished, and the new fingerprint is persisted. A ``None`` stored
        fingerprint with an existing cache file is ADOPTED without a download
        (the pre-fingerprint upgrade path — no thundering herd); with no cache
        file it is left for the apply path, as before. Unbound ROMs are the
        apply path's job and are never touched here.

        *registry* is the caller's bound-row projection keyed by ``str(rom_id)``
        (each entry carrying ``app_id`` and ``cover_source``) — the same read
        the apply's group collapse diffs against, so the pass opens no extra
        per-ROM DB lookups of its own.

        Returns the refreshed ``[{"rom_id", "app_id"}]`` list — the shortcuts
        whose Steam tile the frontend must re-apply via
        ``SetCustomArtworkForApp`` (the grid file alone leaves the in-session
        tile stale until a client restart). One ROM's failure never aborts the
        pass. Progress is narrated under the ``fetching`` stage with the
        ``covers`` sub-stage (the same sub-slice as the cover-download loop,
        #1407), throttled the same way.
        """
        adoptions, changed = await self._loop.run_in_executor(
            None, self._scan_cover_refresh_candidates, all_roms, registry
        )
        if adoptions:
            await self._loop.run_in_executor(None, self._adopt_cover_sources_io, adoptions)
        if not changed:
            return []

        self._cover_art_file_store.make_dirs(self._cover_cache_dir)
        grid = self._steam_config.grid_dir()
        url_covers = {int(rom["id"]): rom.get("url_cover") for rom in all_roms if "id" in rom}
        refresh_target = f"Refreshing covers for {label}" if label else "Refreshing covers"
        refreshed: list[dict[str, int]] = []
        total = len(changed)
        for i, (rom_id, app_id, cover_url) in enumerate(changed):
            if is_cancelling():
                break
            processed = i + 1
            if processed == 1 or processed == total or processed % _COVER_PROGRESS_INTERVAL == 0:
                await emit_progress(
                    SyncStage.FETCHING,
                    current=processed,
                    total=total,
                    message=f"{refresh_target} ({processed}/{total})",
                    step=progress_step,
                    total_steps=progress_total_steps,
                    sub_stage="covers",
                )
            stored_source = registry.get(str(rom_id), {}).get("cover_source")
            ok = await self._loop.run_in_executor(
                None, self._refresh_one_cover_io, rom_id, app_id, cover_url, grid, url_covers.get(rom_id), stored_source
            )
            if ok:
                refreshed.append({"rom_id": rom_id, "app_id": app_id})
        if refreshed:
            self._logger.info(f"Cover refresh: {len(refreshed)} of {total} changed cover(s) re-downloaded")
        return refreshed

    def _scan_cover_refresh_candidates(
        self, all_roms: list[dict[str, Any]], registry: dict[str, dict[str, Any]]
    ) -> tuple[list[tuple[int, str]], list[tuple[int, int, str]]]:
        """Split a unit's fetched ROMs into fingerprint adoptions and changed covers.

        Returns ``(adoptions, changed)``: ``adoptions`` are ``(rom_id, fresh_source)``
        pairs whose stored fingerprint is ``None`` but whose cache file exists (adopt
        without download); ``changed`` are ``(rom_id, app_id, fresh_source)`` triples
        whose stored fingerprint differs from the fresh one (re-download + republish).
        Only BOUND rows qualify — *registry* projects them, so a ROM with no fresh
        cover, no registry entry, or an unchanged fingerprint is skipped without any
        DB access; the compare itself is :func:`domain.cover_refresh.scan_cover_refresh_candidates`
        (the kernel the preview count shares). Only the cache-existence check on
        NULL-fingerprint rows touches I/O here.
        """
        scan = scan_cover_refresh_candidates(all_roms, registry)
        adoptions = [
            (rom_id, fresh)
            for rom_id, fresh in scan.null_fingerprint
            if self._cover_art_file_store.exists(self._cache_path(rom_id))
        ]
        return adoptions, scan.changed

    def _adopt_cover_sources_io(self, adoptions: list[tuple[int, str]]) -> None:
        """Persist adopted fingerprints (no download) in one short write UoW."""
        with self._uow_factory() as uow:
            for rom_id, source in adoptions:
                rom = uow.roms.get(rom_id)
                if rom is None:
                    continue
                rom.adopt_cover_source(source)
                uow.roms.save(rom)

    def _refresh_one_cover_io(
        self,
        rom_id: int,
        app_id: int,
        cover_url: str,
        grid: str | None,
        url_cover: str | None = None,
        stored_source: str | None = None,
    ) -> bool:
        """Re-download (or revalidate) one changed cover, republish, persist the fingerprint.

        Ordered so nothing advances on failure: the fetch either replaces the
        cache in full (200), keeps it on a 304 revalidation (#1454), or leaves the
        old bytes on error. A 200 republishes the grid ``{app_id}p.png`` (a missing
        grid dir skips the publish — the cache is the source of truth), persists
        the new ``cover_source``, and returns ``True`` so the frontend re-applies
        the tile; a 304 only persists the fingerprint (the bytes and the tile are
        already current) and returns ``False``; a failure persists nothing and
        returns ``False``. The persisted fingerprint is the source actually
        applied — *url_cover* when the RomM asset 404s and the fallback wins
        (#1450), else *cover_url*. When *stored_source* differs from *cover_url* by
        only its ``?ts=`` and a validator sidecar exists, the fetch revalidates
        with a conditional request instead of re-downloading.
        """
        cache_path = self._cache_path(rom_id)
        try:
            result = self._fetch_and_record_cover(rom_id, cover_url, cache_path, url_cover, stored_source)
        except Exception as e:
            self._logger.warning(f"Cover refresh: failed to download cover for rom {rom_id}: {e}")
            return False
        if result.not_modified:
            # A 304 confirmed the cached bytes — and the already-published grid
            # tile — are current; only the fingerprint advances. No republish and
            # no ``refreshed`` entry (the frontend need not re-apply an unchanged
            # tile), the whole point of the #1454 revalidation.
            self._persist_cover_source(rom_id, result.applied_source)
            return False
        if grid:
            self.finalize_cover_path(grid, cache_path, app_id, str(rom_id))
        self._persist_cover_source(rom_id, result.applied_source)
        return True

    def _persist_cover_source(self, rom_id: int, source: str) -> None:
        """Record the confirmed cover fingerprint on the ROM row in a short write UoW."""
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            if rom is None:
                return
            rom.adopt_cover_source(source)
            uow.roms.save(rom)

    # ── Atomic write helpers ───────────────────────────────────────────────

    def _fetch_and_record_cover(
        self, rom_id: int | str, cover_url: str, cache_path: str, url_cover: str | None, stored_source: str | None
    ) -> _CoverWrite:
        """Download-or-revalidate *rom_id*'s cover and record its validator sidecar (#1454).

        Sync worker for the executor. Revalidates with a conditional request
        (instead of re-downloading) only when *stored_source* differs from
        *cover_url* by ONLY its ``?ts=`` cache-buster, a validator sidecar
        exists, and the cache file is present — the case a server-side rescan
        creates. Otherwise it is a plain download. Either way the sidecar is
        refreshed from the response so the *next* sync can revalidate. Returns
        the :class:`_CoverWrite` the caller persists as the ``cover_source``
        fingerprint.
        """
        etag, last_modified = self._read_cover_meta(rom_id)
        revalidate = (
            bool(etag or last_modified)
            and cover_ts_only_change(stored_source, cover_url)
            and self._cover_art_file_store.exists(cache_path)
        )
        result = self._download_cover_atomic(
            cover_url,
            cache_path,
            url_cover,
            etag if revalidate else None,
            last_modified if revalidate else None,
        )
        self._record_cover_meta(rom_id, result)
        return result

    def _download_cover_atomic(
        self,
        cover_url: str,
        dest: str,
        url_cover: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> _CoverWrite:
        """Download-or-revalidate the cover into ``dest.tmp`` then atomically publish over *dest*.

        With *etag*/*last_modified* the RomM fetch is a conditional request: a
        304 leaves *dest* untouched (the cache bytes are still current, #1454) and
        nothing is renamed; a 200 renames the freshly-streamed ``dest.tmp`` over
        *dest*. Without a validator it is a plain download. Returns the applied
        source (*cover_url*, or *url_cover* when the #1450 404-fallback wins) plus
        the response validators. A reader of *dest* sees either the old file or
        the complete new one, never a partial write; the sidecar is removed on any
        failure — and on a 304 (no bytes streamed) — so no ``.tmp`` lingers.
        """
        tmp = with_tmp_suffix(dest)
        try:
            result = self._fetch_cover_to_tmp(cover_url, tmp, url_cover, etag, last_modified)
            if result.not_modified:
                self._cover_art_file_store.remove_file(tmp)
            else:
                self._cover_art_file_store.rename(tmp, dest)
            return result
        except Exception:
            self._cover_art_file_store.remove_file(tmp)
            raise

    def _fetch_cover_to_tmp(
        self, cover_url: str, tmp: str, url_cover: str | None, etag: str | None, last_modified: str | None
    ) -> _CoverWrite:
        """Fetch the RomM cover into *tmp* (conditionally), falling back to *url_cover* on a 404.

        Returns the source actually applied (``cover_url`` normally, *url_cover*
        on a successful fallback) plus the outcome's validators. A conditional
        request (when *etag*/*last_modified* are given) may draw a 304 — *tmp* is
        then left unwritten and ``not_modified`` is ``True``. ONLY a definitive
        RomM 404 (:class:`RommNotFoundError`) with a non-empty *url_cover*
        triggers the external fetch — every other failure (transport, 5xx, auth)
        propagates unchanged, keeping today's retry-ladder behaviour and no
        fallback. The external fetch goes out WITHOUT the RomM bearer (host-bound
        token, #1450), carries no validator (its revalidation is out of scope,
        #1454), and is logged at INFO with no secret material.
        """
        try:
            reval = self._romm_api.download_cover(cover_url, tmp, etag=etag, last_modified=last_modified)
            return _CoverWrite(
                applied_source=cover_url,
                not_modified=reval.not_modified,
                etag=reval.etag,
                last_modified=reval.last_modified,
            )
        except RommNotFoundError:
            if not url_cover:
                raise
            self._romm_api.download_cover_from_url(url_cover, tmp)
            self._logger.info(f"Cover asset 404 ({cover_url}); applied url_cover fallback")
            return _CoverWrite(applied_source=url_cover, not_modified=False, etag=None, last_modified=None)

    def _read_cover_meta(self, rom_id: int | str) -> tuple[str | None, str | None]:
        """Return the stored ``(etag, last_modified)`` validators for *rom_id*, or ``(None, None)``.

        Reads the ``{rom_id}.cover-meta.json`` sidecar (#1454). A missing,
        unreadable, or malformed sidecar degrades to ``(None, None)`` — the
        signal to plain-download rather than revalidate, so a corrupt sidecar is
        never fatal.
        """
        meta_path = self._meta_path(rom_id)
        if not self._cover_art_file_store.exists(meta_path):
            return None, None
        try:
            data = json.loads(self._cover_art_file_store.read_bytes(meta_path))
        except Exception:
            return None, None
        if not isinstance(data, dict):
            return None, None
        etag = data.get("etag")
        last_modified = data.get("last_modified")
        return (etag if isinstance(etag, str) else None, last_modified if isinstance(last_modified, str) else None)

    def _record_cover_meta(self, rom_id: int | str, result: _CoverWrite) -> None:
        """Persist (or clear) *rom_id*'s validator sidecar from a fetch *result* (#1454).

        Writes the sidecar when the response carried a validator; on a genuine
        (re)download that carried NONE, removes any stale sidecar so the next sync
        plain-downloads rather than revalidating bytes it can't validate. A 304
        with no fresh validator keeps the existing sidecar — it still describes
        the unchanged cached bytes.
        """
        meta_path = self._meta_path(rom_id)
        if result.etag or result.last_modified:
            content = json.dumps({"etag": result.etag, "last_modified": result.last_modified})
            self._cover_art_file_store.write_text_atomic(meta_path, content)
        elif not result.not_modified:
            self._cover_art_file_store.remove_file(meta_path)

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
        """Remove all artwork files for a registry entry, including the cache cover.

        Sweeps the FULL grid-image set for the removed shortcut's appId —
        portrait/wide/hero/logo/icon across png/jpg/jpeg — not just the
        ``{app_id}p.png`` the plugin itself writes: Steam (or the user) may
        have saved companion art beside the portrait, and a removed shortcut
        must not leave any of it behind.
        """
        # The persisted cover_path (a cache path, or a legacy grid path)
        cover_path = entry.get("cover_path", "")
        if cover_path and self._cover_art_file_store.exists(cover_path):
            self._cover_art_file_store.remove_file(cover_path)
        # Sweep every grid-image form for the removed shortcut's appId
        app_id = entry.get("app_id")
        if app_id:
            self._remove_grid_images(grid, app_id)
        # Fallback: legacy artwork_id format
        artwork_id = entry.get("artwork_id")
        if artwork_id:
            self._remove_grid_images(grid, artwork_id)
        # Clean up any leftover staging file
        staging = os.path.join(grid, staging_filename(rom_id))
        if self._cover_art_file_store.exists(staging):
            self._cover_art_file_store.remove_file(staging)
        # Remove the per-ROM cover cache file
        cache_path = self._cache_path(rom_id)
        if self._cover_art_file_store.exists(cache_path):
            self._cover_art_file_store.remove_file(cache_path)
        # Remove the per-ROM cover-validator sidecar (#1454)
        meta_path = self._meta_path(rom_id)
        if self._cover_art_file_store.exists(meta_path):
            self._cover_art_file_store.remove_file(meta_path)

    def _remove_grid_images(self, grid: str, app_id: int | str) -> None:
        """Remove every grid-image form for *app_id* from the grid dir."""
        for filename in grid_image_filenames(app_id):
            path = os.path.join(grid, filename)
            if self._cover_art_file_store.exists(path):
                self._cover_art_file_store.remove_file(path)

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
            await self._loop.run_in_executor(
                None, self._fetch_and_record_cover, rom_id, cover_url, cache_path, rom.get("url_cover"), None
            )
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
        ``Rom.update_cover_path`` plus the confirmed ``cover_source``
        fingerprint (#1386). ADR-0006: the read and write each own a short
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
            reason, _message = classify_error(e)
            return {
                "success": False,
                "reason": reason,
                # Already true for both branches (the ROM is gone / the server
                # is down), and more specific than the classifier's generic
                # string — only the routing slug was ever wrong here.
                "message": "Could not fetch ROM from server",
            }
        if not rom:
            return {
                "success": False,
                "reason": ErrorCode.NOT_FOUND.value,
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
            # A manual repair forces a fresh download (stored_source=None never
            # matches, so it never revalidates), but still seeds the validator.
            result = await self._loop.run_in_executor(
                None, self._fetch_and_record_cover, rom_id, cover_url, cache_path, rom.get("url_cover"), None
            )
        except Exception as e:
            self._logger.warning(f"refresh_cover: failed to download cover for rom {rom_id}: {e}")
            return {
                "success": False,
                "reason": "download_failed",
                "message": str(e),
            }

        self.finalize_cover_path(grid, cache_path, app_id, str(rom_id))
        await self._loop.run_in_executor(None, self._persist_cover_path, rom_id, cache_path, result.applied_source)

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

    def _persist_cover_path(self, rom_id: int, cover_path: str, cover_source: str) -> None:
        """Record *cover_path* + the confirmed *cover_source* fingerprint in one write UoW."""
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            if rom is None:
                return
            rom.update_cover_path(cover_path)
            rom.adopt_cover_source(cover_source)
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
            # rename): it belongs to no ROM's live cache. Covers both the cover
            # ``.tmp`` and a crashed validator-sidecar ``.cover-meta.json.tmp``.
            if filename.endswith(TMP_SUFFIX):
                if self._remove_cache_entry(cache_dir, filename):
                    pruned.append(filename)
                continue
            # Cover-validator sidecar (#1454): orphaned when its rom_id has no row.
            if filename.endswith(COVER_META_SUFFIX):
                stem = filename[: -len(COVER_META_SUFFIX)]
                if self._prune_cache_stem(cache_dir, filename, stem, known):
                    pruned.append(filename)
                continue
            if not filename.endswith(".png"):
                continue
            if self._prune_cache_stem(cache_dir, filename, filename[: -len(".png")], known):
                pruned.append(filename)
        if pruned:
            self._logger.info(f"Pruned {len(pruned)} orphaned cover cache file(s)")

    def _prune_cache_stem(self, cache_dir: str, filename: str, stem: str, known: set[int]) -> bool:
        """Remove *filename* iff *stem* is a numeric rom_id absent from *known*.

        Shared by the cache-file (``{rom_id}.png``) and validator-sidecar
        (``{rom_id}.cover-meta.json``) sweeps: a non-numeric stem or a live
        rom_id is kept, an orphaned one is removed. Returns whether it was
        removed.
        """
        try:
            rom_id = int(stem)
        except ValueError:
            return False
        if rom_id in known:
            return False
        return self._remove_cache_entry(cache_dir, filename)

    def _remove_cache_entry(self, cache_dir: str, filename: str) -> bool:
        """Remove one cover-cache entry; return whether it was removed."""
        try:
            self._cover_art_file_store.remove_file(os.path.join(cache_dir, filename))
            return True
        except OSError as e:
            self._logger.warning(f"Failed to remove orphaned cover cache {filename}: {e}")
            return False

    # ── Orphaned grid-image cleanup (user-triggered) ───────────────────────

    async def cleanup_orphaned_grid_images(self, live_app_ids: list[int], dry_run: bool) -> dict[str, Any]:
        """Delete grid images whose appId belongs to no live non-Steam shortcut.

        *live_app_ids* is the frontend's full scan of Steam's live non-Steam
        shortcuts (RomM-owned AND foreign) — the keep-set. Candidates are only
        grid-image-named files whose parsed appId sits in the shortcut range
        (:func:`is_shortcut_app_id`), so custom art for regular Steam games
        (small store appIds) is never touched. A submitted set missing even
        one bound ``roms.shortcut_app_id`` is provably incomplete — the whole
        run refuses (``incomplete_scan``) and deletes nothing. ``dry_run``
        counts the candidates without deleting (the first tap of the QAM
        confirm flow); the real run hard-deletes and reports ``removed_count``.
        """
        grid = self._steam_config.grid_dir()
        if not grid or not self._cover_art_file_store.is_dir(grid):
            return {
                "success": False,
                "reason": "no_grid_dir",
                "message": "Steam grid directory not found",
            }
        live = {int(app_id) for app_id in live_app_ids}
        return await self._loop.run_in_executor(None, self._cleanup_orphaned_grid_images_io, grid, live, dry_run)

    def _cleanup_orphaned_grid_images_io(self, grid: str, live: set[int], dry_run: bool) -> dict[str, Any]:
        """Sync worker: sanity-check the live set, scan the grid dir, delete orphans.

        The sanity guard runs before any deletion: every bound
        ``roms.shortcut_app_id`` must appear in *live*, else the scan missed
        at least one real shortcut and nothing can be trusted as orphaned.
        """
        with self._uow_factory() as uow:
            bound = {rom.shortcut_app_id for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None}
        missing = bound - live
        if missing:
            return {
                "success": False,
                "reason": "incomplete_scan",
                "message": (
                    f"Steam's shortcut scan is missing {len(missing)} synced shortcut(s) — "
                    "the scan is incomplete, nothing was removed."
                ),
            }

        orphans: list[str] = []
        for filename in self._cover_art_file_store.listdir(grid):
            app_id = parse_grid_image_app_id(filename)
            if app_id is None or not is_shortcut_app_id(app_id) or app_id in live:
                continue
            if self._cover_art_file_store.is_dir(os.path.join(grid, filename)):
                continue
            orphans.append(filename)

        if dry_run:
            return {"success": True, "candidate_count": len(orphans)}

        removed = 0
        for filename in orphans:
            try:
                self._cover_art_file_store.remove_file(os.path.join(grid, filename))
                removed += 1
            except OSError as e:
                self._logger.warning(f"Failed to remove orphaned grid image {filename}: {e}")
        if removed:
            self._logger.info(f"Removed {removed} orphaned grid image(s)")
        return {"success": True, "removed_count": removed}
