"""What the installed emulators want, where each file goes, and whether it is there.

The demand side of the subsystem, and the one both the status surfaces and the
download entry points ask: the resolver reading, the destination a file is
placed at, and the presence answer that follows from the two. It holds no
server vocabulary at all — RomM contributes a download and nothing to any
question here, which is why readiness survives an unreachable server.

The demand is read per query and never stored: it changes with every RetroDECK
update, and a stored answer would drift silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.firmware_wants import merge_folder_verdicts, unanswered_folder_cores
from lib.path_safety import PathTraversalError, safe_join

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping

    from domain.firmware_wants import FirmwareCatalogue, FirmwarePlacement, FolderVerdict
    from services.protocols import (
        FirmwareFileStore,
        FirmwareFolderVerdictFn,
        FirmwareResolver,
        RetroDeckPaths,
    )


@dataclass(frozen=True)
class FirmwareDemandConfig:
    """Frozen wiring bundle handed to ``FirmwareDemand.__init__``.

    Holds the two resolver seams — the whole-machine reading and the narrower
    per-core folder read — the RetroDECK path accessor the destinations are
    built under, the file store the plugin's own presence probe goes through,
    and the logger a poisoned entry is reported on.
    """

    firmware_resolver: FirmwareResolver
    firmware_folder_verdicts: FirmwareFolderVerdictFn
    retrodeck_paths: RetroDeckPaths
    firmware_file_store: FirmwareFileStore
    logger: logging.Logger


class FirmwareDemand:
    """The machine's firmware demand: what is wanted, where it belongs, and what is there."""

    def __init__(self, *, config: FirmwareDemandConfig) -> None:
        self._firmware_resolver = config.firmware_resolver
        self._firmware_folder_verdicts = config.firmware_folder_verdicts
        self._retrodeck_paths = config.retrodeck_paths
        self._firmware_file_store = config.firmware_file_store
        self._logger = config.logger

    # ── What the machine wants ───────────────────────────────

    def catalogue(self) -> FirmwareCatalogue:
        """The whole machine's demand, read fresh. Blocking."""
        return self._firmware_resolver()

    def placement_index(self) -> Mapping[str, FirmwarePlacement]:
        """The machine's demand indexed by file name, for the paths that need only that."""
        return self.catalogue().by_file_name()

    def folder_answers(
        self,
        placements: Mapping[str, FirmwarePlacement],
        scope: list[str] | None,
        asked: dict[str, Mapping[str, FolderVerdict]],
    ) -> Mapping[str, FirmwarePlacement]:
        """*placements* with the folder rows in *scope* answered by a verified read.

        Blocking: the read opens the folder's candidates and reads them the way
        the core does, so it is asked per core and only for the rows the
        machine-wide reading left open. *asked* is the query's memo — the cores
        are per platform, the answer per core.
        """
        verdicts: dict[str, FolderVerdict] = {}
        for core_so in unanswered_folder_cores(placements, scope):
            if core_so not in asked:
                asked[core_so] = self._firmware_folder_verdicts(core_so)
            for file_name, verdict in asked[core_so].items():
                verdicts.setdefault(file_name, verdict)
        return merge_folder_verdicts(placements, verdicts) if verdicts else placements

    # ── Destinations ─────────────────────────────────────────

    def dest_path(self, firmware, placement: FirmwarePlacement | None) -> str:
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
        onto it (``allow_base``); a server-supplied name landing there would
        have to be the empty string, ``.``, or a link in the root pointing back
        at the root, and none of those three is a file.

        That leaves one shape this returns a directory for: a server file whose
        name matches a directory declaration — ``bios`` against LRPS2's
        ``pcsx2/bios``, which RetroDECK links onto the root. The read paths want
        exactly that; the write path would place a ``.tmp`` sibling of the root.
        """
        bios_base = self._retrodeck_paths.bios_path()
        if placement is not None:
            return safe_join(bios_base, placement.destination, allow_base=True)
        return safe_join(bios_base, firmware.get("file_name", ""))

    def safe_dest_path(self, firmware, placement: FirmwarePlacement | None) -> str | None:
        """Read-path wrapper for ``dest_path`` — ``None`` on a poisoned entry.

        The status queries (``check_platform_bios``, the overview)
        only need to know whether a firmware file is downloaded; a server
        entry whose ``file_name`` attempts path traversal cannot be on
        disk, so it is logged and dropped from the listing instead of
        crashing the whole panel. The write path keeps the raising
        ``dest_path`` so a download attempt fails closed.
        """
        try:
            return self.dest_path(firmware, placement)
        except PathTraversalError as e:
            self._logger.warning(f"Skipping firmware with unsafe file name: {e}")
            return None

    def is_downloaded(self, placement: FirmwarePlacement | None, dest: str) -> bool:
        """Is the file at *dest* there? The resolver answers wherever it has a requirement.

        The boundary, and it is drawn rather than incidental: **a row the
        resolver declared AND placed under this root is a row the resolver
        answers for**, because it read that destination the way the emulator
        will reach it — following the symlinks a distribution strings through
        the BIOS tree — while a bare existence check answers about whatever the
        path assembled here happens to name. Two derivations of one fact is one
        too many, and the LRPS2 row is what it cost: with the destination wrong,
        the resolver had the file and this service did not. Placed elsewhere the
        boundary falls the other way, and the next paragraph is why.

        Our own probe covers what is left, and both halves of it are the same
        rule read backwards — we answer for the destinations the resolver did
        not read. A library file no installed emulator declares has no
        requirement at all; a placement with no ``relative_path`` has one, at a
        destination this service cannot honour, so *dest* is its own flat
        fallback and the resolver's reading is about somewhere else. The third
        is the re-check in ``FirmwareDownloader._download_firmware_batch``, where
        re-reading the whole machine to learn whether one file just landed would
        cost hundreds of milliseconds.

        A ``present`` of ``None`` on a placement we do honour is a destination
        the resolver could not look at. It is not a claim that anything is
        there, so it reads as absent — the safe direction, since the row then
        shows work outstanding rather than a readiness nobody established.

        What this never asks is whether the requirement is MET — that is
        ``BiosFileEntry.satisfied``, and for a folder declaration the two come
        apart: what satisfies the core is a file inside the folder, and the
        folder itself is there on every stock RetroDECK.
        """
        if placement is None or placement.relative_path is None:
            return self._firmware_file_store.exists(dest)
        return placement.present is True

    def wanted_beyond_server(
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
                    "downloaded": self.is_downloaded(placement, dest),
                    "dest": dest,
                    "on_server": False,
                }
            )
        return items
