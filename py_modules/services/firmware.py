"""FirmwareService — BIOS/firmware orchestration over the live resolver.

Owns every status-bearing BIOS query the QAM panel runs: what the RomM server
holds, what the installed emulators want, where each file goes, and what is on
disk. What an emulator wants is never stored — it is read per query through the
``FirmwareResolver`` seam, because it changes with every RetroDECK update and a
stored answer would drift silently. Raw filesystem I/O is delegated to the
``FirmwareFileStore`` Protocol and HTTP traffic flows through ``RommFirmwareApi``;
the classification scope, the destination layout, and the per-core filtering
remain this service's responsibility.

A platform's file list is the **union** of what the RomM library offers and what
the platform's emulators ask for. The two overlap but neither contains the
other: the library holds files nothing wants, and an emulator can want a file
the library has never had. That third kind is shown like any other and marked
``on_server: False`` — it is real, it may be missing, and nothing here can fetch
it, so it counts towards readiness and never towards a download button.

Readiness therefore needs no server at all. Which emulator is active comes from
ES-DE, what it wants comes from the resolver, what is on disk comes from the
filesystem; RomM contributes the download and nothing else. What an unreachable
server costs is the files only it knows about.

The demand is read once for the whole machine, so one file cannot answer
differently on two surfaces. Only the question "may an absence be read as
*nothing wants it*" is narrowed, to the libretro cores ES-DE offers for the
platform being rendered.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from domain import firmware_paths
from domain.bios_file import BiosFile
from domain.bios_status import (
    BIOS_LEVEL_UNKNOWN,
    collect_firmware_status,
    compute_bios_label,
    compute_bios_level,
    count_required,
    count_wanted,
    format_bios_status,
)
from domain.emulator_commands import label_to_invocation, options_to_payload, select_default_option
from domain.firmware_cache import FirmwareCacheEntry
from lib.errors import error_response
from lib.list_result import ErrorCode
from lib.path_safety import PathTraversalError, safe_join

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Mapping

    from domain.firmware_wants import FirmwareCatalogue, FirmwarePlacement
    from services.protocols import (
        Clock,
        CoreInfoProvider,
        FirmwareFileStore,
        FirmwareResolver,
        PlatformCoreReader,
        RetroDeckPaths,
        RommFirmwareApi,
        SystemResolver,
        UnitOfWorkFactory,
    )

_FIRMWARE_CACHE_TTL = 3600  # 1 hour


@dataclass(frozen=True)
class FirmwareServiceConfig:
    """Frozen wiring bundle handed to ``FirmwareService.__init__``.

    Holds the API adapter, runtime infrastructure, Protocol-typed file
    adapters, the SQLite Unit-of-Work factory, and the provider callables
    FirmwareService needs at construction time. Decomposes the ctor
    so a new dependency does not push past the S107 parameter-count
    limit.
    """

    romm_api: RommFirmwareApi
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    firmware_file_store: FirmwareFileStore
    firmware_resolver: FirmwareResolver
    retrodeck_paths: RetroDeckPaths
    core_info: CoreInfoProvider
    resolve_system: SystemResolver
    platform_core_reader: PlatformCoreReader
    uow_factory: UnitOfWorkFactory


class FirmwareService:
    """BIOS/firmware management: what is wanted, what is here, downloads, deletion."""

    def __init__(
        self,
        *,
        config: FirmwareServiceConfig,
    ) -> None:
        self._romm_api = config.romm_api
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._firmware_file_store = config.firmware_file_store
        self._firmware_resolver = config.firmware_resolver
        self._retrodeck_paths = config.retrodeck_paths
        self._core_info = config.core_info
        self._resolve_system = config.resolve_system
        self._platform_core_reader = config.platform_core_reader
        self._uow_factory = config.uow_factory
        self._firmware_cache: list[dict[str, Any]] | None = None
        self._firmware_cache_epoch: float = 0
        self._restore_firmware_cache()

    # ── What the machine wants ───────────────────────────────

    @staticmethod
    def _core_scope(options: dict[str, Any]) -> list[str] | None:
        """The libretro cores an ES-DE emulator list offers, or ``None`` when it names none.

        The scope an absence is judged against. ``None`` means the scope itself
        is unestablished, so nothing may be ruled out for this platform — and it
        has two causes that are the same thing to the caller: ``es_systems.xml``
        could not be read, or it was read and offers this system no libretro
        core at all.

        The second is not a corner case. 35 of ES-DE's 172 systems have no
        libretro command, ``ps3`` among them — a mapped RomM platform whose only
        entry is RPCS3. An empty list would be a *complete* reading of nothing:
        every server file classifies ``not_needed``, ``required_count`` is 0, and
        the platform reads a green "Nothing required" over firmware RPCS3 will
        not boot without. The same applies to any RomM slug ``resolve_system``
        maps to a name ``es_systems.xml`` does not carry. Grey ``unknown`` is
        the honest answer for a platform whose emulators this service does not
        enumerate, and it is what ADR-0020's standalone deferral licenses.

        Standalone entries are out of the scope by that deferral rather than by
        oversight: ADR-0020 defers standalone BIOS accuracy (inheriting
        ADR-0012's), and ``get_active_core`` is libretro-only for the same
        reason. Widening the scope is not the whole of lifting it — the resolver
        does answer per-system for standalone emulators, but
        ``_vendor/atlas/data/standalone_firmware.json`` holds cards for five of
        them (CEMU, DUCKSTATION, MELONDS, PCSX2, XEMU), and those five answer
        ``declaration="packaged"``. Every other standalone emulator answers
        ``declaration="unsupported"``, which upstream documents as meaning
        unknown, not "needs nothing" — measured here, 20 distinct standalone
        emulators in ``es_systems.xml`` and so 15 of them uncovered, though that
        count moves with each RetroDECK release. So the deferral outlives this
        scope: upstream coverage has to arrive first.

        Takes the already-read options rather than the system name so a caller
        that needs the emulator list anyway reads it once.
        """
        if not options["available"]:
            return None
        return [option.core_so for option in options["options"] if option.core_so] or None

    def _placement_index(self) -> Mapping[str, FirmwarePlacement]:
        """The machine's demand indexed by file name, for the paths that need only that."""
        return self._firmware_resolver().by_file_name()

    # ── Destinations ─────────────────────────────────────────

    def _firmware_dest_path(self, firmware, placement: FirmwarePlacement | None) -> str:
        """Determine the local destination path for a firmware file.

        Uses the resolver's own placement for correct subdirectory placement
        (e.g. ``dc/dc_boot.bin``), falling back to flat in the BIOS root for a
        server file no emulator declares — there is then no stated layout to
        honour.

        Both branches go through ``safe_join`` so neither a server-supplied
        ``file_name`` nor a declared placement can escape the BIOS directory via
        ``..`` or an absolute path. Raises :class:`PathTraversalError` on an
        escape attempt — the write path (``download_firmware``) turns that into a
        canonical failure; the read paths skip the poisoned entry.

        Only the placement branch accepts the BIOS root itself as a
        destination, because only a declared location can legitimately resolve
        onto it (``allow_base``); a server-supplied name landing there would be
        the empty string, which is not a file.
        """
        bios_base = self._retrodeck_paths.bios_path()
        if placement is not None:
            return safe_join(bios_base, placement.destination, allow_base=True)
        return safe_join(bios_base, firmware.get("file_name", ""))

    def _safe_firmware_dest_path(self, firmware, placement: FirmwarePlacement | None) -> str | None:
        """Read-path wrapper for ``_firmware_dest_path`` — ``None`` on a poisoned entry.

        The status queries (``check_platform_bios``, the System-page group)
        only need to know whether a firmware file is downloaded; a server
        entry whose ``file_name`` attempts path traversal cannot be on
        disk, so it is logged and dropped from the listing instead of
        crashing the whole panel. The write path keeps the raising
        ``_firmware_dest_path`` so a download attempt fails closed.
        """
        try:
            return self._firmware_dest_path(firmware, placement)
        except PathTraversalError as e:
            self._logger.warning(f"Skipping firmware with unsafe file name: {e}")
            return None

    def _is_downloaded(self, placement: FirmwarePlacement | None, dest: str) -> bool:
        """Is the file at *dest* there? The resolver answers wherever it has a requirement.

        The boundary, and it is drawn rather than incidental: **a row the
        resolver declared is a row the resolver answers for**, because it read
        that destination the way the emulator will reach it — following the
        symlinks a distribution strings through the BIOS tree — while a bare
        existence check answers about whatever the path assembled here happens
        to name. Two derivations of one fact is one too many, and the LRPS2 row
        is what it cost: with the destination wrong, the resolver had the file
        and this service did not.

        Our own probe covers what is left, and both halves of it are the same
        rule read backwards — we answer for the destinations the resolver did
        not read. A library file no installed emulator declares has no
        requirement at all; a placement with no ``relative_path`` has one, at a
        destination this service cannot honour, so *dest* is its own flat
        fallback and the resolver's reading is about somewhere else. The third
        is the re-check in ``_download_firmware_batch``, where re-reading the
        whole machine to learn whether one file just landed would cost hundreds
        of milliseconds.

        A ``present`` of ``None`` on a placement we do honour is a destination
        the resolver could not look at. It is not a claim that anything is
        there, so it reads as absent — the safe direction, since the row then
        shows work outstanding rather than a readiness nobody established. What
        this never asks is whether the file is the RIGHT one: the resolver
        withholds that verdict for a directory, and answering it here would
        turn a withheld verdict into a green one.
        """
        if placement is None or placement.relative_path is None:
            return self._firmware_file_store.exists(dest)
        return placement.present is True

    def _wanted_beyond_server(
        self, placements: Mapping[str, FirmwarePlacement], scope: list[str] | None, in_library: set[str]
    ) -> list[dict[str, Any]]:
        """Items for files this platform's emulators want that the library lacks.

        A placement belongs to this platform when one of the libretro cores
        ES-DE offers for it declares the file — the same scope the completeness
        question uses, so a platform never claims a requirement from an emulator
        it does not offer. ``scope`` is ``None`` when ``es_systems.xml`` could
        not be read, and then no requirement can be attributed to any platform.

        ``in_library`` is every file name the RomM listing carries, across all
        platforms — not just this one's. A core that serves several systems
        declares the same file for each of them while RomM files it under one
        directory, so a per-platform check would tell the user a file is not in
        their library while it sits there under the neighbouring system. It is
        one download either way: the destination comes from the placement, so
        fetching it anywhere satisfies every core that asked.
        """
        if not scope:
            return []
        cores = set(scope)
        bios_base = self._retrodeck_paths.bios_path()
        items: list[dict[str, Any]] = []
        for placement in sorted(placements.values(), key=lambda entry: entry.file_name):
            if placement.file_name in in_library:
                continue
            if not any(want.core_so in cores for want in placement.wants):
                continue
            try:
                dest = safe_join(bios_base, placement.destination, allow_base=True)
            except PathTraversalError as e:
                self._logger.warning(f"Skipping firmware with unsafe placement: {e}")
                continue
            items.append(
                {
                    "file_name": placement.file_name,
                    "downloaded": self._is_downloaded(placement, dest),
                    "dest": dest,
                    "on_server": False,
                }
            )
        return items

    def _build_firmware_status_items(
        self, firmware_iter, placements: Mapping[str, FirmwarePlacement]
    ) -> list[dict[str, Any]]:
        """Build ``collect_firmware_status`` items, dropping traversal-poisoned entries.

        Each item carries the resolved ``dest`` and its ``downloaded``
        flag. A firmware entry whose ``file_name`` fails the path-safety
        check resolves to ``None`` and is skipped (logged in
        ``_safe_firmware_dest_path``) so a single malicious entry cannot
        crash the status query.
        """
        items: list[dict[str, Any]] = []
        for fw in firmware_iter:
            file_name = fw.get("file_name", "")
            placement = placements.get(file_name)
            dest = self._safe_firmware_dest_path(fw, placement)
            if dest is None:
                continue
            items.append(
                {
                    "file_name": file_name,
                    "downloaded": self._is_downloaded(placement, dest),
                    "dest": dest,
                }
            )
        return items

    # ── Firmware list cache ─────────────────────────────────

    def _restore_firmware_cache(self) -> None:
        """Rebuild the in-memory firmware cache from the SQLite cache table.

        The ``firmware_cache`` table is a thin record per ADR-0003 — it carries
        the already-parsed ``platform_slug`` and ``name`` but not the raw RomM
        ``file_path`` or ``md5_hash``. We synthesize a ``bios/<slug>/<name>``
        ``file_path`` that round-trips through ``parse_firmware_slug`` so a
        restart still has a listing to answer from while the server is
        unreachable; ``md5_hash`` is intentionally absent (display-only).
        """
        try:
            with self._uow_factory() as uow:
                entries = list(uow.firmware_cache.iter_all())
                epoch = uow.firmware_cache.get_cache_epoch()
        except Exception as e:
            self._logger.warning(f"Failed to load firmware cache from DB: {e}")
            return

        if not entries or epoch is None:
            return

        self._firmware_cache = [self._entry_to_firmware_dict(entry) for entry in entries]
        self._firmware_cache_epoch = epoch
        self._logger.info("Restored firmware cache from DB (%d items)", len(entries))

    @staticmethod
    def _entry_to_firmware_dict(entry: FirmwareCacheEntry) -> dict[str, Any]:
        """Reconstruct an in-memory firmware dict from a thin cache aggregate."""
        return {
            "id": entry.id,
            "file_name": entry.name,
            "file_path": f"bios/{entry.platform_slug}/{entry.name}",
            "file_size_bytes": entry.file_size_bytes,
            "md5_hash": "",
        }

    def _persist_firmware_cache(self) -> None:
        """Replace the SQLite firmware cache with the current in-memory listing.

        Maps each raw RomM firmware dict to a thin ``FirmwareCacheEntry`` (slug
        pre-parsed from ``file_path``) and writes them wholesale.
        """
        if self._firmware_cache is None:
            return
        entries = [
            FirmwareCacheEntry.cached(
                id=fw.get("id"),
                name=fw.get("file_name", ""),
                platform_slug=firmware_paths.parse_firmware_slug(fw.get("file_path", "")),
                file_size_bytes=fw.get("file_size_bytes", 0),
                cached_at=self._firmware_cache_epoch,
            )
            for fw in self._firmware_cache
        ]
        try:
            with self._uow_factory() as uow:
                uow.firmware_cache.replace_all(entries)
        except Exception as e:
            self._logger.warning(f"Failed to persist firmware cache: {e}")

    def _get_firmware_list(self) -> list[dict[str, Any]]:
        """Return firmware list, using cache if TTL has not expired.

        TTL is checked against the wall-clock cache epoch so a cache
        restored from disk after a plugin restart still expires.

        On HTTP error, falls back to cached data if there is any and RAISES
        otherwise. Returning an empty list instead would be indistinguishable
        from a server that genuinely holds no firmware, and ``check_platform_bios``
        answers a confident "needs none" for that — so the raise is what lets a
        failed fetch be reported as unknown rather than as a negative (#1693).
        """
        now = self._clock.time()
        if self._firmware_cache is not None and (now - self._firmware_cache_epoch) < _FIRMWARE_CACHE_TTL:
            return self._firmware_cache

        try:
            result = self._romm_api.list_firmware()
            self._firmware_cache = result
            self._firmware_cache_epoch = self._clock.time()
            self._persist_firmware_cache()
            return result
        except Exception as e:
            self._logger.warning(f"Failed to fetch firmware list: {e}")
            if self._firmware_cache is not None:
                return self._firmware_cache
            raise

    def invalidate_firmware_cache(self) -> None:
        """Clear cached firmware list so the next call re-fetches."""
        self._firmware_cache = None
        self._firmware_cache_epoch = 0
        try:
            with self._uow_factory() as uow:
                uow.firmware_cache.clear()
        except Exception as e:
            self._logger.warning(f"Failed to clear persisted firmware cache: {e}")

    def _resolve_bios_filter_core(self, system: str, active_core_so: str | None) -> str | None:
        """Return the ``.so`` to filter the firmware list against.

        A non-``None`` ``active_core_so`` is the pre-resolved per-game core (the
        game-detail path runs ``ActiveCoreReader`` upstream) and is used as-is.
        ``None`` is the platform-level callers' "use the system default" signal —
        resolved here via the system-layer ``get_active_core(system)``.
        """
        if active_core_so is not None:
            return active_core_so
        core_so, _ = self._core_info.get_active_core(system)
        return core_so

    def _bios_aggregates(self, files, platform_slug: str, complete: bool) -> dict[str, Any]:
        """The counts, level and label every surface reads off one classified file list.

        One derivation for the per-game paths and the System page, so a platform
        and its games can never show a different level for the same files.

        Three axes, and each counts a different set on purpose.
        ``required_count`` is the launching core's — the badge's — and includes a
        required file the library does not hold, because that file is a
        prerequisite whether or not anything here can fetch it.
        ``server_count`` / ``local_count`` are the library's, and count only what
        it holds: "N/M files ready" is a progress bar over a set the user can
        actually complete, and folding in files that were never uploaded would
        read as work outstanding on a system that needs nothing — a SNES page
        would say ``0 / 26 files, 26 missing`` for twenty-six optional files no
        core requires. ``known_count`` / ``unknown_count`` are the machine's
        answer about the files themselves, and are the library's set too — they
        are weighed against ``server_count``, so a row it does not hold would
        raise the numerator of a ratio it is not in.
        """
        on_server = [f for f in files if f.on_server]
        server_count = len(on_server)
        local_count = sum(1 for f in on_server if f.downloaded)
        required_count, required_downloaded = count_required(files)
        known_count, unknown_count = count_wanted(files)

        result = {
            "needs_bios": True,
            "server_count": server_count,
            "local_count": local_count,
            "all_downloaded": local_count >= server_count,
            "required_count": required_count,
            "required_downloaded": required_downloaded,
            "unknown_count": unknown_count,
            "known_count": known_count,
        }
        # The bios_level state ("unknown" / "ok" / "partial" / "missing") and the
        # compact bios_label beside it, so every consumer reads the verdict
        # straight off this payload instead of re-deriving the threshold logic.
        # "unknown" replaces the false "ok" a bare count would give, for a
        # platform whose server files went entirely unanswered and for one
        # holding no file at all under a reading that never happened.
        #
        # Derived HERE and only here because this is where ``complete`` is known:
        # a second derivation elsewhere would have to be handed the same reading
        # state to reach the same answer, and would agree by coincidence if it
        # were not.
        #
        # The rows travel with the counts here because one of those two shapes
        # turns on there being no row; ``result`` itself stays row-free, because
        # it is the aggregate half of a payload that carries them separately.
        status = format_bios_status({**result, "files": files}, platform_slug, reading_complete=complete)
        result["bios_level"] = compute_bios_level(status)
        result["bios_label"] = compute_bios_label(status)
        return result

    def _bios_payload(self, files, platform_slug: str, complete: bool) -> dict[str, Any]:
        """The aggregates plus the per-file rows — what the per-game surfaces read."""
        return {**self._bios_aggregates(files, platform_slug, complete), "files": [asdict(f) for f in files]}

    # ── Public API ───────────────────────────────────────────

    def _group_server_firmware(self, firmware_list, placements: Mapping[str, FirmwarePlacement]):
        """Group server firmware list by platform slug."""
        platforms_map = {}
        for fw in firmware_list:
            platform_slug = firmware_paths.parse_firmware_slug(fw.get("file_path", "")) or "unknown"
            file_name = fw.get("file_name", "")
            placement = placements.get(file_name)
            dest = self._safe_firmware_dest_path(fw, placement)
            if dest is None:
                continue
            if platform_slug not in platforms_map:
                platforms_map[platform_slug] = {"platform_slug": platform_slug, "files": []}
            platforms_map[platform_slug]["files"].append(
                {
                    "id": fw.get("id"),
                    "file_name": file_name,
                    "size": fw.get("file_size_bytes", 0),
                    "md5": fw.get("md5_hash", ""),
                    "local_path": dest,
                    "downloaded": self._is_downloaded(placement, dest),
                    "on_server": True,
                }
            )
        return platforms_map

    @staticmethod
    def _seed_synced_platforms(platforms_map, synced_slugs) -> set[str]:
        """Add an empty entry for every synced platform the listing did not name.

        A platform whose emulators want firmware the library has never held would
        otherwise be absent from a page that is about exactly that — and with the
        server unreachable, every platform is in that position. Returns the slugs
        it seeded so the caller can drop the ones that turn out to have nothing
        to say (:func:`_has_something_to_say`).

        A slug is seeded only when none of its firmware-directory spellings is
        already a key: RomM files a platform's firmware under its own directory
        name (``psx`` → ``bios/ps/``), so the raw slug and the listing's key are
        routinely different words for one platform.
        """
        seeded: set[str] = set()
        for slug in sorted(synced_slugs):
            if any(fw_slug in platforms_map for fw_slug in firmware_paths.resolve_firmware_slugs(slug)):
                continue
            platforms_map[slug] = {"platform_slug": slug, "files": []}
            seeded.add(slug)
        return seeded

    def _read_synced_slugs(self) -> set[str]:
        """Return platform slugs with at least one ROM bound to a Steam shortcut.

        A bound ROM (``shortcut_app_id`` set) is one that is currently in the
        synced library, covering both platform- and collection-sync. Deselected
        platforms get unbound on the next sync (ADR-0007) and drop out.
        """
        with self._uow_factory() as uow:
            return {
                rom.platform_slug
                for rom in uow.roms.iter_all()
                if rom.platform_slug and rom.shortcut_app_id is not None
            }

    def _deletable_count(self, platform_slug: str, records: list[BiosFile]) -> int:
        """How many files ``delete_platform_bios`` would remove for *platform_slug*.

        The delete is authorised by the download record and unlinks the path that
        record holds, so this counts exactly that: recorded paths under one of
        the platform's firmware slugs that are still on disk. Distinct paths,
        because two rows naming one file are one unlink.

        ``local_count`` counts a different set and cannot stand in for it: it is
        the library's progress ratio, so it includes files the plugin never put
        there and drops our own downloads once RomM stops listing them. Used for
        the button it would be wrong in both directions, and zero — hiding the
        button — for a platform whose downloads have all left the library.
        """
        slugs = set(firmware_paths.resolve_firmware_slugs(platform_slug))
        paths = {record.file_path for record in records if record.platform_slug in slugs}
        return sum(1 for path in paths if self._firmware_file_store.exists(path))

    def _enrich_platform_map(
        self,
        platforms_map,
        synced_slugs,
        catalogue: FirmwareCatalogue,
        in_library: set[str],
        records: list[BiosFile],
    ):
        """Add core info, wants, and game-installed flags to each platform entry.

        The core read seams key by the resolved RetroDECK ``system`` (ADR-0010
        §2), so each entry's raw RomM/BIOS-folder slug is normalized before the
        ``get_active_core`` / ``get_emulator_options`` calls; ``has_games`` and
        the BIOS-folder file lookups stay on the raw slug (their own vocabulary).
        ``active_core`` stays the libretro system default *core_so* (the BIOS
        filter keys on it — the standalone-default BIOS accuracy work is deferred
        by ADR-0020). ``active_core_label`` is the resolved **display** label the
        System-page control shows — the per-platform override (``platform_cores``)
        when set and still resolvable, else the es_systems default emulator label,
        so it reflects a just-applied per-platform pick (libretro OR standalone)
        the same way the game-detail menu does, instead of always showing the
        libretro system default. The ``emulators`` list is the full classified
        picker payload and ``emulator_data_available`` flags whether
        ``es_systems.xml`` was readable.

        *catalogue* is read once by the caller and shared across every platform:
        it is one machine-wide question costing hundreds of milliseconds, and
        asking it per platform would multiply that by the platform count while
        answering the same thing each time. *in_library* is likewise the whole
        listing's file names rather than one platform's slice — see
        :meth:`_wanted_beyond_server`. *records* is every BIOS download row, read
        once for the same reason and sliced per platform by
        :meth:`_deletable_count`.
        """
        placements = catalogue.by_file_name()
        for plat in platforms_map.values():
            slug = plat["platform_slug"]
            system = self._resolve_system(slug)
            core_so, _core_label = self._core_info.get_active_core(system)
            options = self._core_info.get_emulator_options(system)
            plat["active_core"] = core_so
            plat["active_core_label"] = self._resolve_platform_emulator_label(slug, options["options"])
            plat["emulators"] = options_to_payload(options["options"])
            plat["emulator_data_available"] = options["available"]
            scope = self._core_scope(options)
            complete = catalogue.reading_complete_for(scope)
            plat["files"].extend(
                _overview_row(item) for item in self._wanted_beyond_server(placements, scope, in_library)
            )
            files = collect_firmware_status(
                [
                    {
                        "file_name": f["file_name"],
                        "downloaded": f["downloaded"],
                        "dest": f["local_path"],
                        "on_server": f["on_server"],
                    }
                    for f in plat["files"]
                ],
                placements,
                complete,
                core_so,
            )
            plat["files"] = [{**raw, **_wanted_fields(entry)} for raw, entry in zip(plat["files"], files, strict=True)]
            plat["has_games"] = slug in synced_slugs
            plat["all_downloaded"] = all(f["downloaded"] for f in plat["files"])
            plat["deletable_count"] = self._deletable_count(slug, records)
            self._set_platform_bios_aggregates(plat, slug, files, complete)

    def _resolve_platform_emulator_label(self, platform_slug: str, options: list[Any]) -> str | None:
        """Resolve the System-page active-emulator display label for a platform.

        The platform-level projection of the read-path precedence
        (``ActiveCoreResolver`` without the per-game layer): the per-platform
        override label (``settings.json`` ``platform_cores``) when it is set and
        still resolves to a bakeable emulator, else the es_systems default
        emulator label (the first bakeable command). A stale/no-longer-installed
        override degrades to the default — never fatal — mirroring the launch-bake
        resolver so the button and the actual launch agree. ``None`` when the
        platform has no bakeable emulator at all (empty menu / es_systems
        unreadable), which the frontend renders as "Default".
        """
        override = self._platform_core_reader.get_platform_core(platform_slug)
        if override is not None and label_to_invocation(options, override) is not None:
            return override
        default = select_default_option(options)
        return default.label if default is not None else None

    def _set_platform_bios_aggregates(self, plat: dict[str, Any], slug: str, files, complete: bool) -> None:
        """Stamp the per-platform BIOS aggregates onto a ``get_firmware_status`` entry.

        Adds ``server_count`` / ``local_count`` / ``required_count`` /
        ``required_downloaded`` and the ``bios_level`` state
        (``"unknown"`` / ``"ok"`` / ``"partial"`` / ``"missing"``) so the System
        page reads the decision and the display counts straight off this payload
        instead of re-deriving the threshold logic in the frontend. The whole
        payload comes from the same builder the per-game path uses, so the level
        a platform shows and the level its games show cannot diverge.

        *complete* is the reading state for the platform's own emulators, and it
        is what stops a platform with no file at all from reading a green "all
        ready" it could not have established.
        """
        payload = self._bios_aggregates(files, slug, complete)
        plat["server_count"] = payload["server_count"]
        plat["local_count"] = payload["local_count"]
        plat["required_count"] = payload["required_count"]
        plat["required_downloaded"] = payload["required_downloaded"]
        plat["bios_level"] = payload["bios_level"]

    async def get_firmware_status(self) -> dict[str, Any]:
        """Return BIOS/firmware status for every platform the page can speak for.

        An unreachable server removes the files only it knows about and the
        ability to download; what the installed emulators want is read locally,
        so the platforms, their emulator pickers and their readiness all survive.
        """
        server_offline = False
        catalogue, synced_slugs, records = await self._loop.run_in_executor(None, self._read_status_inputs)
        placements = catalogue.by_file_name()
        firmware_list: list[dict[str, Any]] = []
        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
            platforms_map = self._group_server_firmware(firmware_list, placements)
        except Exception as e:
            self._logger.warning(f"Building the firmware overview without the server listing: {e}")
            server_offline = True
            platforms_map = {}

        seeded = self._seed_synced_platforms(platforms_map, synced_slugs)
        in_library = {fw.get("file_name", "") for fw in firmware_list}
        self._enrich_platform_map(platforms_map, synced_slugs, catalogue, in_library, records)
        platforms = sorted(
            (plat for slug, plat in platforms_map.items() if slug not in seeded or _has_something_to_say(plat)),
            key=lambda p: p["platform_slug"],
        )
        return {"success": True, "server_offline": server_offline, "platforms": platforms}

    def _read_status_inputs(self) -> tuple[FirmwareCatalogue, set[str], list[BiosFile]]:
        """The three blocking reads the overview needs, in one worker hop.

        The resolver walks a few hundred ``.info`` files and the two DB reads
        each open a UoW; all of it belongs off the loop thread, and none of it
        depends on the others.
        """
        return self._firmware_resolver(), self._read_synced_slugs(), self._read_bios_records()

    def _read_bios_records(self) -> list[BiosFile]:
        """Every BIOS download record, for the overview's per-platform delete count.

        Read whole rather than per platform: it is one small table and the page
        asks the same question of it for each platform it renders. One short read
        UoW, closed before the file probes :meth:`_deletable_count` runs.
        """
        with self._uow_factory() as uow:
            return list(uow.bios_files.iter_all())

    def _download_firmware_post_io(self, fw, firmware_id, dest, tmp_path):
        """Sync worker for download_firmware — file rename, hash verification, DB persist.

        Runs in an executor. The filesystem work (rename, checksum) happens
        outside any transaction; only the ``BiosFile`` upsert is wrapped in a
        short write UoW (ADR-0006).

        Returns ``(md5_match, error)``. ``error`` is a string when the firmware is
        malformed — RomM data that fails the ``BiosFile`` invariants (empty
        slug/file_name) — in which case the renamed file is removed and nothing
        is persisted; otherwise ``None``.
        """
        file_name = fw.get("file_name", "")
        self._firmware_file_store.rename(tmp_path, dest)

        expected_md5 = fw.get("md5_hash", "")
        local_md5 = self._firmware_file_store.checksum_md5(dest) if expected_md5 else None
        md5_match = local_md5 == expected_md5 if expected_md5 and local_md5 is not None else None

        try:
            bios_file = BiosFile.mark_downloaded(
                platform_slug=firmware_paths.parse_firmware_slug(fw.get("file_path", "")),
                file_name=file_name,
                file_path=dest,
                downloaded_at=self._clock.now().isoformat(),
                firmware_id=firmware_id,
            )
        except ValueError as e:
            # Malformed RomM firmware (e.g. file_path with no parseable slug):
            # the aggregate's invariant rejects it. Drop the renamed file so we
            # don't leave it untracked, and signal a download failure.
            self._firmware_file_store.remove_file(dest)
            return md5_match, f"Invalid firmware metadata: {e}"

        with self._uow_factory() as uow:
            uow.bios_files.save(bios_file)

        return md5_match, None

    async def download_firmware(self, firmware_id) -> dict[str, Any]:
        """Download a single firmware file from RomM."""
        placements = await self._loop.run_in_executor(None, self._placement_index)
        return await self._download_one(firmware_id, placements)

    async def _download_one(self, firmware_id, placements: Mapping[str, FirmwarePlacement]) -> dict[str, Any]:
        """Fetch, place and record one firmware file against a pre-read demand index.

        The index is a parameter rather than a per-call read so a batch pays for
        the machine-wide question once instead of once per file.
        """
        firmware_id = int(firmware_id)
        try:
            fw = await self._loop.run_in_executor(None, self._romm_api.get_firmware, firmware_id)
        except Exception as e:
            self._logger.error(f"Failed to fetch firmware {firmware_id}: {e}")
            return error_response(e)

        file_name = fw.get("file_name", "")
        try:
            dest = self._firmware_dest_path(fw, placements.get(file_name))
        except PathTraversalError as e:
            self._logger.error(f"Rejected firmware with unsafe file name {file_name!r}: {e}")
            return {
                "success": False,
                "reason": "path_traversal",
                "message": "Server sent an unsafe firmware file name — download aborted",
            }
        tmp_path = dest + ".tmp"

        try:
            await self._loop.run_in_executor(None, self._firmware_file_store.make_dirs, os.path.dirname(dest))
            await self._loop.run_in_executor(None, self._romm_api.download_firmware, firmware_id, file_name, tmp_path)
        except Exception as e:
            await self._loop.run_in_executor(None, self._firmware_file_store.remove_file, tmp_path)
            self._logger.error(f"Failed to download firmware {file_name}: {e}")
            return error_response(e)

        md5_match, post_io_error = await self._loop.run_in_executor(
            None, self._download_firmware_post_io, fw, firmware_id, dest, tmp_path
        )
        if post_io_error is not None:
            self._logger.error(f"Failed to persist firmware {file_name}: {post_io_error}")
            return error_response(ValueError(post_io_error))

        self.invalidate_firmware_cache()
        self._logger.info(f"Firmware downloaded: {file_name} -> {dest}")
        return {"success": True, "file_path": dest, "md5_match": md5_match}

    async def download_all_firmware(self, platform_slug) -> dict[str, Any]:
        """Download all firmware for a given platform slug."""
        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
        except Exception as e:
            self._logger.error(f"Failed to fetch firmware: {e}")
            resp = error_response(e)
            resp["downloaded"] = 0
            return resp

        # Filter by platform slug (use mapped slugs, e.g. "psx" -> ["psx", "ps"])
        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        platform_firmware = [
            fw for fw in firmware_list if firmware_paths.parse_firmware_slug(fw.get("file_path", "")) in fw_slugs
        ]

        placements = await self._loop.run_in_executor(None, self._placement_index)
        downloaded, errors = await self._download_firmware_batch(platform_firmware, placements)

        msg = f"Downloaded {downloaded} firmware files"
        if errors:
            msg += f" ({len(errors)} failed: {', '.join(errors)})"
        return {"success": True, "message": msg, "downloaded": downloaded}

    async def _download_firmware_batch(
        self, platform_firmware, placements: Mapping[str, FirmwarePlacement]
    ) -> tuple[int, list[str]]:
        """Download a batch of firmware files, skipping already-downloaded ones.

        *placements* is the machine's demand index, read once by the caller: the
        question costs hundreds of milliseconds, and a batch that asked it per
        file would pay that for every download.

        The already-there skip probes the disk rather than reading the
        catalogue's answer (:meth:`_is_downloaded`) — that answer predates every
        download this batch has performed, and re-reading the whole machine per
        file to refresh it is the cost the index exists to avoid.
        """
        downloaded = 0
        errors = []
        for fw in platform_firmware:
            dest = self._safe_firmware_dest_path(fw, placements.get(fw.get("file_name", "")))
            if dest is not None and self._firmware_file_store.exists(dest):
                continue
            result = await self._download_one(fw["id"], placements)
            if result.get("success"):
                downloaded += 1
            else:
                errors.append(fw.get("file_name", str(fw["id"])))
        return downloaded, errors

    async def download_required_firmware(self, platform_slug) -> dict[str, Any]:
        """Download only the firmware the platform's launching core will not run without."""
        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
        except Exception as e:
            self._logger.error(f"Failed to fetch firmware: {e}")
            resp = error_response(e)
            resp["downloaded"] = 0
            return resp

        system = self._resolve_system(platform_slug)
        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        core_so, _ = self._core_info.get_active_core(system)
        placements = await self._loop.run_in_executor(None, self._placement_index)

        platform_firmware = [
            fw
            for fw in firmware_list
            if firmware_paths.parse_firmware_slug(fw.get("file_path", "")) in fw_slugs
            and _required_by(placements.get(fw.get("file_name", "")), core_so)
        ]

        downloaded, errors = await self._download_firmware_batch(platform_firmware, placements)

        msg = f"Downloaded {downloaded} required firmware files"
        if errors:
            msg += f" ({len(errors)} failed: {', '.join(errors)})"
        return {"success": True, "message": msg, "downloaded": downloaded}

    async def check_platform_bios(self, platform_slug, active_core_so=None) -> dict[str, Any]:
        """Check if RomM has firmware for this platform and whether it's downloaded.

        Returns BIOS status only. ``active_core_so`` is the pre-resolved core used
        to filter the firmware list by what THIS core needs (an INPUT to
        ``collect_firmware_status`` so ``required_count`` / the missing-BIOS badge
        stay core-aware); it is never served back to the UI. ``None`` means "use
        the system default" (resolved here via ``get_active_core(system)``); the
        per-game game-detail path passes the ROM's resolved ``.so``. Core info
        reaches the frontend through the dedicated ``get_platform_core_info``
        path, not this payload (#923).

        An unreachable server costs the files only it knows about, not the
        answer: what the platform's emulators want is read locally either way.
        The one payload that still says nothing is a platform with no requirement
        AND no complete reading — that ``needs_bios: False`` carries
        ``bios_status_unknown: True`` and no consumer may read it as "this
        platform needs none" (#1693).
        """
        system = self._resolve_system(platform_slug)
        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        options = self._core_info.get_emulator_options(system)
        scope = self._core_scope(options)
        active_core_so = self._resolve_bios_filter_core(system, active_core_so)

        try:
            firmware_list = await self._loop.run_in_executor(None, self._get_firmware_list)
        except Exception as e:
            self._logger.warning(f"Answering BIOS status without the server listing: {e}")
            firmware_list = []

        catalogue = await self._loop.run_in_executor(None, self._firmware_resolver)
        placements = catalogue.by_file_name()
        items = self._build_firmware_status_items(
            (fw for fw in firmware_list if firmware_paths.parse_firmware_slug(fw.get("file_path", "")) in fw_slugs),
            placements,
        )
        items.extend(self._wanted_beyond_server(placements, scope, {fw.get("file_name", "") for fw in firmware_list}))
        complete = catalogue.reading_complete_for(scope)
        files = collect_firmware_status(items, placements, complete, active_core_so)

        if not files:
            return {"needs_bios": False} if complete else {"needs_bios": False, "bios_status_unknown": True}

        return self._bios_payload(files, platform_slug, complete)

    def _delete_platform_bios_io(self, platform_slug):
        """Sync worker for delete_platform_bios — file deletions then DB prune.

        Runs in an executor. Every filesystem removal happens outside any
        transaction: the records are read in one short UoW before the loop and
        the rows they leave behind are dropped in one after it (ADR-0006).

        The download records are the whole input: a ``downloaded_bios`` row is
        written in the download path and nowhere else, so it is the only
        evidence the plugin put the file there — and having put it there is what
        authorises removing it. A status row's ``downloaded`` proves nothing of
        the sort: it is ``os.path.exists``, equally true of firmware RetroDECK
        ships with its own components (``dolphin-emu/Sys/codehandler.bin`` is
        one) and of a file the user placed by hand under a name the server
        happens to share. Neither can be fetched back, so neither is ours to
        delete.

        The row also says WHERE, and that is why the status rows are not
        consulted for the path either. ``BiosFile.file_path`` is where the
        download actually wrote the file (kept current by the home migration's
        ``relocate``), while a status row's ``local_path`` is recomputed from
        today's placement — and placements move with every emu-atlas bump. A
        file fetched while nothing declared it landed flat in the BIOS root; let
        the resolver later declare a subdirectory for it and the recomputed path
        names whatever now sits there instead, which for ``codehandler.bin`` is
        RetroDECK's own copy. Unlinking the recorded path can reach nothing but
        what the plugin wrote.

        ``on_server`` is deliberately not part of the test. It describes what
        the library holds *now*, not who wrote the file: a firmware file removed
        from RomM after we downloaded it flips to ``on_server: False``, and
        refusing that row would strand our own download with nothing in the UI
        able to clean it up.

        A record whose file is already gone is not a deletion and not an error —
        the row is dropped and nothing is counted. That is also what makes two
        rows for one file name under different firmware slugs harmless: they
        name one path, the first unlink takes it, and the second prunes its row
        over an absence.
        """
        deleted = 0
        errors = []
        pruned: list[tuple[str, str]] = []
        for record in self._recorded_bios_files(platform_slug):
            if self._firmware_file_store.exists(record.file_path):
                try:
                    self._firmware_file_store.remove_file(record.file_path)
                except OSError as e:
                    self._logger.warning(f"Failed to remove BIOS file {record.file_name}: {e}")
                    errors.append(f"{record.file_name}: {e}")
                    continue
                deleted += 1
            pruned.append((record.platform_slug, record.file_name))

        if pruned:
            self._prune_bios_records(pruned)

        return deleted, errors

    def _recorded_bios_files(self, platform_slug) -> list[BiosFile]:
        """The plugin's own download records for *platform_slug*.

        The BIOS rows are keyed by the firmware-directory slug stored at download
        time, which may differ from the platform slug (e.g. ``psx`` → ``ps``), so
        every candidate spelling is read. Each record is kept whole rather than
        reduced to a name: the delete needs its ``file_path`` to unlink and its
        ``platform_slug`` to address the row again. One short read UoW, closed
        before any file I/O.
        """
        records: list[BiosFile] = []
        with self._uow_factory() as uow:
            for slug in firmware_paths.resolve_firmware_slugs(platform_slug):
                records.extend(uow.bios_files.iter_by_platform(slug))
        return records

    def _prune_bios_records(self, keys) -> None:
        """Delete the ``BiosFile`` records named by the ``(slug, file_name)`` *keys*.

        One short write UoW, after the file I/O it follows. A key reaches here
        for a row whose file was removed AND for one whose file was already
        gone; only a row whose removal *failed* is left standing.
        """
        with self._uow_factory() as uow:
            for slug, file_name in keys:
                uow.bios_files.delete(slug, file_name)

    async def delete_platform_bios(self, platform_slug) -> dict[str, Any]:
        """Delete the BIOS files the plugin downloaded for a platform.

        Scoped to the plugin's own downloads, never to everything sitting in the
        platform's BIOS locations — see ``_delete_platform_bios_io``. The
        download records are the only input: a status listing would re-introduce
        the library as a gate, and our own download is deletable long after RomM
        stops offering it (the case that used to hide the button entirely).
        """
        deleted, errors = await self._loop.run_in_executor(None, self._delete_platform_bios_io, platform_slug)
        self.invalidate_firmware_cache()

        if errors:
            return {
                "success": False,
                "reason": ErrorCode.UNKNOWN.value,
                "deleted_count": deleted,
                "message": f"Deleted {deleted} file(s), {len(errors)} error(s)",
            }
        if deleted == 0:
            return {"success": True, "deleted_count": 0, "message": "No BIOS files for this platform"}
        return {"success": True, "deleted_count": deleted, "message": f"Deleted {deleted} BIOS file(s)"}


def _has_something_to_say(plat: dict[str, Any]) -> bool:
    """Is a seeded platform worth a block on the System page?

    A seeded platform is one the listing never named — it is here because the
    user syncs games for it, not because anything is known to be wanted. With a
    file to show, it speaks for itself. With none, it stays only when its answer
    is ``unknown``: dropping the block is itself a claim, read by anyone looking
    for the platform as "nothing to manage here", and that is the false negative
    a platform whose emulators cannot be asked must never give. A platform whose
    reading finished and found nothing wanted really has nothing to manage, and
    still drops out.
    """
    return bool(plat["files"]) or plat["bios_level"] == BIOS_LEVEL_UNKNOWN


def _overview_row(item: dict[str, Any]) -> dict[str, Any]:
    """The System-page row for a file the library does not hold.

    ``on_server: False`` is the load-bearing field: both download buttons filter
    on it (``SystemPage.tsx``), and so do the page's own progress totals. ``id``
    is ``None`` as an honest absence — there is no server record to name — and
    no consumer reads it, so filling it in with a placeholder would withhold
    nothing but would make a row that cannot be fetched look fetchable to the
    next reader.
    """
    return {
        "id": None,
        "file_name": item["file_name"],
        "size": 0,
        "md5": "",
        "local_path": item["dest"],
        "downloaded": item["downloaded"],
        "on_server": False,
    }


def _wanted_fields(entry) -> dict[str, Any]:
    """The System-page projection of one classified file.

    The overview's rows keep the server's own fields (id, size, md5) and gain
    only what the machine answered, so the two vocabularies stay separable.
    """
    return {
        "description": entry.description,
        "wanted": entry.wanted,
        "required_by_active": entry.required_by_active,
        "supplied_by": entry.supplied_by,
        "is_directory": entry.is_directory,
    }


def _required_by(placement: FirmwarePlacement | None, core_so: str | None) -> bool:
    """Will *core_so* refuse to run without the file *placement* describes?

    ``None`` for the core — the platform's default could not be resolved — falls
    back to "any emulator requires it", the same permissive default the status
    surfaces use when they cannot name the launching core.
    """
    if placement is None:
        return False
    if core_so is None:
        return placement.required_by_any
    return any(want.core_so == core_so and want.required for want in placement.wants)
