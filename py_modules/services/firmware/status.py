"""The BIOS status the QAM panel reads — one platform, or every platform at once.

Joins the RomM listing with the machine's demand and answers in rows plus
aggregates: what each file is, whether the requirement it carries is met, and
the platform-wide verdict that follows. Both answers come off one builder, so a
platform and its games can never show a different level for the same files.

The demand is read once per query for the whole machine, so one file cannot
answer differently on two surfaces. Only the question "may an absence be read as
*nothing wants it*" is narrowed, to the libretro cores ES-DE offers for the
platform being rendered.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from domain import firmware_paths
from domain.bios_status import (
    BIOS_LEVEL_UNKNOWN,
    collect_firmware_status,
    compute_bios_label,
    compute_bios_level,
    count_required,
    count_required_withheld,
    count_wanted,
    format_bios_status,
)
from domain.emulator_commands import options_to_payload, resolve_platform_label

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Mapping

    from domain.bios_file import BiosFile
    from domain.firmware_wants import FirmwareCatalogue, FirmwarePlacement, FolderVerdict
    from services.firmware.demand import FirmwareDemand
    from services.firmware.listing import FirmwareListing
    from services.protocols import (
        CoreInfoProvider,
        FirmwareFileStore,
        PlatformCoreReader,
        SystemResolver,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class FirmwareStatusReaderConfig:
    """Frozen wiring bundle handed to ``FirmwareStatusReader.__init__``.

    Holds the two peer sub-services the answers are built from — the machine's
    demand and the RomM listing — plus the ES-DE core reads, the slug/system
    mapping, the per-platform emulator override, the file store the delete
    count probes through, the Unit-of-Work factory, and runtime infrastructure.
    """

    demand: FirmwareDemand
    listing: FirmwareListing
    core_info: CoreInfoProvider
    resolve_system: SystemResolver
    platform_core_reader: PlatformCoreReader
    firmware_file_store: FirmwareFileStore
    uow_factory: UnitOfWorkFactory
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger


class FirmwareStatusReader:
    """Every status-bearing BIOS query the panel runs — the overview and the per-game check."""

    def __init__(self, *, config: FirmwareStatusReaderConfig) -> None:
        self._demand = config.demand
        self._listing = config.listing
        self._core_info = config.core_info
        self._resolve_system = config.resolve_system
        self._platform_core_reader = config.platform_core_reader
        self._firmware_file_store = config.firmware_file_store
        self._uow_factory = config.uow_factory
        self._loop = config.loop
        self._logger = config.logger

    # ── Rows and aggregates ──────────────────────────────────

    def _build_firmware_status_items(
        self, firmware_iter, placements: Mapping[str, FirmwarePlacement]
    ) -> list[dict[str, Any]]:
        """Build ``collect_firmware_status`` items, dropping traversal-poisoned entries.

        Each item carries the resolved ``dest`` and its ``downloaded``
        flag. A firmware entry whose ``file_name`` fails the path-safety
        check resolves to ``None`` and is skipped (logged in
        ``FirmwareDemand.safe_dest_path``) so a single malicious entry cannot
        crash the status query.
        """
        items: list[dict[str, Any]] = []
        for fw in firmware_iter:
            file_name = fw.get("file_name", "")
            placement = placements.get(file_name)
            dest = self._demand.safe_dest_path(fw, placement)
            if dest is None:
                continue
            items.append(
                {
                    "file_name": file_name,
                    "downloaded": self._demand.is_downloaded(placement, dest),
                    "dest": dest,
                }
            )
        return items

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

        One derivation for the per-game paths and the overview, so a platform
        and its games can never show a different level for the same files.

        Three axes, and each counts a different set on purpose.
        ``required_count`` is the launching core's — the badge's — and includes a
        required file the library does not hold, because that file is a
        prerequisite whether or not anything here can fetch it.
        ``required_withheld`` rides with it as the part of that count nothing
        could judge, so a surface warning about an absence can subtract what was
        never established rather than reading a declined verdict as a gap.
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
            "required_withheld": count_required_withheld(files),
            "unknown_count": unknown_count,
            "known_count": known_count,
        }
        # The bios_level state ("unknown" / "ok" / "partial" / "missing") and the
        # compact bios_label beside it, so every consumer reads the verdict
        # straight off this payload instead of re-deriving the threshold logic.
        # "unknown" replaces the false "ok" a bare count would give, for a
        # platform whose server files went entirely unanswered, for one holding
        # no file at all under a reading that never happened, and for one whose
        # launching core requires a file nothing could judge.
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

    # ── The whole-library overview ───────────────────────────

    def _group_server_firmware(self, firmware_list, placements: Mapping[str, FirmwarePlacement]):
        """Group server firmware list by platform slug."""
        platforms_map = {}
        for fw in firmware_list:
            platform_slug = firmware_paths.parse_firmware_slug(fw.get("file_path", "")) or "unknown"
            file_name = fw.get("file_name", "")
            placement = placements.get(file_name)
            dest = self._demand.safe_dest_path(fw, placement)
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
                    "downloaded": self._demand.is_downloaded(placement, dest),
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

    async def _enrich_platform_map(
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
        by ADR-0020). ``active_core_label`` is the resolved **display** label
        (:func:`resolve_platform_label`) — the per-platform override
        (``platform_cores``) when set and still resolvable, else the es_systems
        default emulator label, so it reflects a just-applied per-platform pick
        (libretro OR standalone) the same way the game-detail menu does, instead
        of always showing the libretro system default. The ``emulators`` list is
        the full classified picker payload and ``emulator_data_available`` flags
        whether ``es_systems.xml`` was readable.

        *catalogue* is read once by the caller and shared across every platform:
        it is one machine-wide question costing hundreds of milliseconds, and
        asking it per platform would multiply that by the platform count while
        answering the same thing each time. *in_library* is likewise the whole
        listing's file names rather than one platform's slice — see
        :meth:`FirmwareDemand.wanted_beyond_server`. *records* is every BIOS
        download row, read once for the same reason and sliced per platform by
        :meth:`_deletable_count`. The folder verdicts are scoped per platform and
        memoised across them (:meth:`FirmwareDemand.folder_answers`) — the cores
        are the platform's, the answer is the core's.
        """
        index = catalogue.by_file_name()
        asked: dict[str, Mapping[str, FolderVerdict]] = {}
        for plat in platforms_map.values():
            slug = plat["platform_slug"]
            system = self._resolve_system(slug)
            core_so, _core_label = self._core_info.get_active_core(system)
            options = self._core_info.get_emulator_options(system)
            plat["active_core"] = core_so
            plat["active_core_label"] = resolve_platform_label(
                options["options"], self._platform_core_reader.get_platform_core(slug)
            )
            plat["emulators"] = options_to_payload(options["options"])
            plat["emulator_data_available"] = options["available"]
            scope = _core_scope(options)
            placements = await self._loop.run_in_executor(None, self._demand.folder_answers, index, scope, asked)
            complete = catalogue.reading_complete_for(scope)
            plat["files"].extend(
                _overview_row(item) for item in self._demand.wanted_beyond_server(placements, scope, in_library)
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

    def _set_platform_bios_aggregates(self, plat: dict[str, Any], slug: str, files, complete: bool) -> None:
        """Stamp the per-platform BIOS aggregates onto a ``get_firmware_status`` entry.

        Adds ``server_count`` / ``local_count`` / ``required_count`` /
        ``required_downloaded`` / ``required_withheld`` and the ``bios_level``
        state (``"unknown"`` / ``"ok"`` / ``"partial"`` / ``"missing"``) so the
        platform detail reads the decision and the display counts straight off
        this payload instead of re-deriving the threshold logic in the frontend. The
        whole payload comes from the same builder the per-game path uses, so the
        level a platform shows and the level its games show cannot diverge.

        ``required_withheld`` is what tells the page's two unknowns apart: a
        platform nothing could speak for withdraws its downloads, while one whose
        rows were answered and whose verdict was declined by a single unjudgeable
        requirement keeps every one of them.

        *complete* is the reading state for the platform's own emulators, and it
        is what stops a platform with no file at all from reading a green "all
        ready" it could not have established.
        """
        payload = self._bios_aggregates(files, slug, complete)
        plat["server_count"] = payload["server_count"]
        plat["local_count"] = payload["local_count"]
        plat["required_count"] = payload["required_count"]
        plat["required_downloaded"] = payload["required_downloaded"]
        plat["required_withheld"] = payload["required_withheld"]
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
            firmware_list = await self._loop.run_in_executor(None, self._listing.get_firmware_list)
            platforms_map = self._group_server_firmware(firmware_list, placements)
        except Exception as e:
            self._logger.warning(f"Building the firmware overview without the server listing: {e}")
            server_offline = True
            platforms_map = {}

        seeded = self._seed_synced_platforms(platforms_map, synced_slugs)
        in_library = {fw.get("file_name", "") for fw in firmware_list}
        await self._enrich_platform_map(platforms_map, synced_slugs, catalogue, in_library, records)
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
        return self._demand.catalogue(), self._read_synced_slugs(), self._read_bios_records()

    def _read_bios_records(self) -> list[BiosFile]:
        """Every BIOS download record, for the overview's per-platform delete count.

        Read whole rather than per platform: it is one small table and the page
        asks the same question of it for each platform it renders. One short read
        UoW, closed before the file probes :meth:`_deletable_count` runs.
        """
        with self._uow_factory() as uow:
            return list(uow.bios_files.iter_all())

    # ── The per-game check ───────────────────────────────────

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
        scope = _core_scope(options)
        active_core_so = self._resolve_bios_filter_core(system, active_core_so)

        try:
            firmware_list = await self._loop.run_in_executor(None, self._listing.get_firmware_list)
        except Exception as e:
            self._logger.warning(f"Answering BIOS status without the server listing: {e}")
            firmware_list = []

        catalogue = await self._loop.run_in_executor(None, self._demand.catalogue)
        placements = await self._loop.run_in_executor(
            None, self._demand.folder_answers, catalogue.by_file_name(), scope, {}
        )
        items = self._build_firmware_status_items(
            (fw for fw in firmware_list if firmware_paths.parse_firmware_slug(fw.get("file_path", "")) in fw_slugs),
            placements,
        )
        items.extend(
            self._demand.wanted_beyond_server(placements, scope, {fw.get("file_name", "") for fw in firmware_list})
        )
        complete = catalogue.reading_complete_for(scope)
        files = collect_firmware_status(items, placements, complete, active_core_so)

        if not files:
            return {"needs_bios": False} if complete else {"needs_bios": False, "bios_status_unknown": True}

        return self._bios_payload(files, platform_slug, complete)


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


def _has_something_to_say(plat: dict[str, Any]) -> bool:
    """Is a seeded platform worth an entry in the overview?

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
    """The overview row for a file the library does not hold.

    ``on_server: False`` is the load-bearing field: every download affordance
    filters on it (``src/components/library/PlatformDetail.tsx``), and so do the
    platform detail's own progress totals. ``id``
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
    """The overview projection of one classified file.

    The overview's rows keep the server's own fields (id, size, md5) and gain
    only what the machine answered, so the two vocabularies stay separable.
    """
    return {
        "declared_path": entry.declared_path,
        "description": entry.description,
        "wanted": entry.wanted,
        "required_by_active": entry.required_by_active,
        "supplied_by": entry.supplied_by,
        "satisfied": entry.satisfied,
        "declared_kind": entry.declared_kind,
        "caveats": entry.caveats,
        "images": entry.images,
    }
