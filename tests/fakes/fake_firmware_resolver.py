"""In-memory firmware seams for service tests — the demand, and the folder verdicts."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from domain.firmware_wants import (
    DECLARED_DIRECTORY,
    DECLARED_FILE,
    FirmwareCatalogue,
    FirmwarePlacement,
    FirmwareWant,
    FolderVerdict,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import EllipsisType


class FakeFirmwareResolver:
    """The machine's firmware demand, stated by the test instead of read off disk.

    Seed it with :meth:`declare` — one call per file, naming the cores that
    require it and the cores that merely accept it. ``unread_cores`` names the
    emulators whose declaration could not be read, which is what decides whether
    a file the catalogue does not hold reads ``not_needed`` or ``unknown`` for a
    given platform; ``resolved=False`` stands for a reading that never happened
    at all.

    ``bios_root`` and ``present_probe`` are what make the fake a stand-in for a
    resolver that reads a disk: given them, each placement's ``present`` is
    looked up under the root at call time, so a test that puts a file in its
    BIOS directory gets the same answer the real resolver would give for it.
    ``_make_firmware_service`` wires both to the service's own BIOS root and
    file store, because that is the production relationship — one directory,
    two readers. Without a root there is nowhere to take a reading, so a
    placement's ``present`` stays ``None`` — withheld, not absent, which is the
    distinction the consumers are built around — unless :meth:`declare` said
    otherwise, and a test pins a reading the files would not give by passing
    ``present`` there.

    ``calls`` counts invocations so a test can pin that a whole-machine question
    costing hundreds of milliseconds on a real device is asked once per query
    rather than once per platform.
    """

    def __init__(
        self,
        *,
        placements: list[FirmwarePlacement] | None = None,
        unread_cores: frozenset[str] = frozenset(),
        resolved: bool = True,
        caveats: tuple[str, ...] = (),
        bios_root: str | None = None,
        present_probe: Callable[[str], bool] = os.path.exists,
    ) -> None:
        self.placements: list[FirmwarePlacement] = list(placements or [])
        self.unread_cores = unread_cores
        self.resolved = resolved
        self.caveats = caveats
        self.bios_root = bios_root
        self.present_probe = present_probe
        self.calls = 0

    def declare(
        self,
        file_name: str,
        *,
        required_by: tuple[str, ...] | list[str] = (),
        optional_for: tuple[str, ...] | list[str] = (),
        relative_path: str | None | EllipsisType = ...,
        description: str | None = None,
        present: bool | None = None,
        declares_directory: bool = False,
        folder: FolderVerdict | None = None,
        caveats: tuple[str, ...] = (),
        supplied_by: str | None = None,
    ) -> FirmwarePlacement:
        """State that some emulators ask for *file_name*, and return the placement.

        ``relative_path`` left unset is the bare file name — the flat layout —
        so a test only spells it out when the subdirectory placement is the
        point. Passing ``None`` is the third state and a different one: there is
        no location under the firmware root to honour at all, which is what an
        emulator keeping its firmware in its own tree produces.

        ``present`` left unset defers to ``bios_root``; set it to pin a reading
        the files under that root would not give.

        ``declares_directory`` is what the EMULATOR opens the destination at, not
        what is there — a folder declaration whose folder is absent is still one.
        ``folder`` is the verdict about its contents where the reading settled
        one; leaving it unset is the folder that has not been looked inside, and
        :class:`FakeFolderVerdicts` is what answers it.
        """
        placement = FirmwarePlacement(
            file_name=file_name,
            relative_path=file_name if relative_path is ... else relative_path,
            description=description if description is not None else file_name,
            wants=tuple(
                [FirmwareWant(core_so=core, required=True) for core in required_by]
                + [FirmwareWant(core_so=core, required=False) for core in optional_for]
            ),
            present=present,
            declared_kind=DECLARED_DIRECTORY if declares_directory else DECLARED_FILE,
            caveats=caveats,
            folder=folder,
            supplied_by=supplied_by,
        )
        self.placements.append(placement)
        return placement

    def _read(self, placement: FirmwarePlacement) -> FirmwarePlacement:
        """The placement as the reading would answer it, looked up under ``bios_root``."""
        if placement.present is not None or not self.bios_root:
            return placement
        there = self.present_probe(os.path.join(self.bios_root, placement.destination))
        return FirmwarePlacement(
            file_name=placement.file_name,
            relative_path=placement.relative_path,
            description=placement.description,
            wants=placement.wants,
            present=there,
            declared_kind=placement.declared_kind,
            caveats=placement.caveats,
            folder=placement.folder,
            supplied_by=placement.supplied_by,
        )

    def __call__(self) -> FirmwareCatalogue:
        self.calls += 1
        return FirmwareCatalogue(
            placements=tuple(self._read(placement) for placement in self.placements),
            unread_cores=self.unread_cores,
            resolved=self.resolved,
            caveats=self.caveats,
        )


class FakeFolderVerdicts:
    """The verified folder reading, stated by the test instead of read off disk.

    Seeded per core, because that is the seam's scope: a verified read opens one
    core's declared folder and reads the candidates inside it. ``calls`` records
    the cores it was asked about, in order, so a test can pin that the expensive
    question is asked once per core and only where a folder row is unanswered.
    """

    def __init__(self, verdicts: dict[str, dict[str, FolderVerdict]] | None = None) -> None:
        self.verdicts = verdicts or {}
        self.calls: list[str] = []

    def __call__(self, core_so: str) -> Mapping[str, FolderVerdict]:
        self.calls.append(core_so)
        return self.verdicts.get(core_so, {})
