"""Tests for the atlas firmware adapter — the translation into plugin vocabulary.

What is under test is the adapter's own work: folding the resolver's per-core
answer into one row per file, naming the cores that could not be asked, deciding
whether a declared location is one the plugin's own BIOS root can honour, and
refusing to turn any failure into "nothing needed". The resolver's own decisions
are upstream's and are not re-tested here — including what it read at a
destination, which is carried through rather than re-derived.

Answers are built from real atlas value objects rather than mocks, so every
invariant the resolver enforces on its own shapes is enforced on the fixtures
too — a fixture that could not come off a real machine fails to construct.
"""

from __future__ import annotations

import pytest
from _vendor.atlas import (
    CAVEAT_FIRMWARE_DIRECTORY_HOLDS_NO_CANDIDATE,
    CAVEAT_FIRMWARE_IDENTITY_NOT_COMPARABLE,
    CAVEAT_FIRMWARE_IMAGE_IDENTIFIED,
    CAVEAT_FIRMWARE_IMAGE_UNLISTED,
    CAVEAT_FIRMWARE_PATH_OBSTRUCTED,
    CAVEAT_FIRMWARE_SEARCH_UNVERIFIED,
)
from _vendor.atlas.firmware import DECLARED_DIRECTORY as ATLAS_DECLARED_DIRECTORY
from _vendor.atlas.firmware import DECLARED_FILE as ATLAS_DECLARED_FILE
from _vendor.atlas.firmware import (
    CoreDeclarationState,
    CoreFirmware,
    DeclaredKind,
    FirmwareAlternatives,
    FirmwareAnswer,
    FirmwareChecked,
    FirmwareNeed,
    FirmwareRequirement,
    RefusedDeclaration,
    SuppliedBy,
)
from _vendor.atlas.machine import KIND_DIRECTORY, KIND_FILE, KIND_INACCESSIBLE, KIND_MISSING, PathKind
from _vendor.atlas.placement import Caveat

from adapters.atlas_firmware import AtlasFirmwareAdapter, AtlasFolderVerdictAdapter
from domain.firmware_wants import (
    CAVEAT_PATH_OBSTRUCTED,
    DECLARED_DIRECTORY,
    DECLARED_FILE,
)

_ROOT = "/home/deck/retrodeck/bios"


def _requirement(
    *,
    core_so: str | None = "mgba_libretro.so",
    file_name: str = "gba_bios.bin",
    path: str | None = None,
    declared: str | None = None,
    need: FirmwareNeed = "required",
    description: str = "",
    regions: tuple[str, ...] | None = None,
    found: PathKind = KIND_MISSING,
    checked: FirmwareChecked | None = None,
    supplied_by: SuppliedBy | None = None,
    declared_kind: DeclaredKind = ATLAS_DECLARED_FILE,
    contents_satisfied: bool | None = None,
) -> FirmwareRequirement:
    """One (core, declared file) pair, defaulting to a flat destination with nothing there.

    ``declared`` and ``path`` default to the same flat spelling under the root
    because that is what the overwhelming majority of declarations look like;
    a test spells them apart when the divergence between the two IS the case
    under test.
    """
    return FirmwareRequirement(
        core_so=core_so,
        system="gba",
        system_source="systemname",
        need=need,
        file_name=file_name,
        path=path if path is not None else f"{_ROOT}/{file_name}",
        declared=declared if declared is not None else file_name,
        description=description or f"{file_name} (BIOS)",
        identity=None,
        found=found,
        checked=checked,
        regions=regions,
        supplied_by=supplied_by,
        declared_kind=declared_kind,
        contents_satisfied=contents_satisfied,
    )


def _core(
    *,
    core_so: str | None = "mgba_libretro.so",
    declaration: CoreDeclarationState = "read",
    requirements: tuple[FirmwareRequirement | FirmwareAlternatives, ...] = (),
    caveats: tuple[Caveat, ...] = (),
    refused: tuple[RefusedDeclaration, ...] = (),
) -> CoreFirmware:
    if declaration != "read" and not caveats:
        caveats = (Caveat(code="core-info-unreadable", message="its .info could not be read"),)
    if refused and not caveats:
        caveats = (Caveat(code="firmware-declaration-leaves-root", message="leaves the root"),)
    return CoreFirmware(
        core_so=core_so,
        label=None,
        declaration=declaration,
        requirements=requirements,
        caveats=caveats,
        refused=refused,
    )


def _answer(
    *cores: CoreFirmware,
    root: str | None = _ROOT,
    caveats: tuple[Caveat, ...] = (),
) -> FirmwareAnswer:
    if root is None and not caveats:
        caveats = (Caveat(code="firmware-root-unstated", message="no system directory"),)
    return FirmwareAnswer(
        root=root,
        cores=cores,
        unclaimed=(),
        hash_checked=False,
        sources=(),
        caveats=caveats,
    )


class _Installation:
    """Stand-in for a detected installation handle — answers one prepared inventory."""

    kind = "retrodeck"

    def __init__(self, answer: FirmwareAnswer | Exception) -> None:
        self._answer = answer

    def firmware_inventory(self, *, verify: bool = False) -> FirmwareAnswer:
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer

    def firmware_for_core(self, core_so: str, *, verify: bool = False) -> FirmwareAnswer:
        self.asked_for = (core_so, verify)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


@pytest.fixture
def traces() -> list[str]:
    return []


@pytest.fixture
def adapter(traces):
    return AtlasFirmwareAdapter(user_home="/home/deck", log_debug=traces.append)


def _detecting(*installations):
    def detect(home, machine=None):
        return list(installations)

    return detect


def catalogue_cores(catalogue) -> set[str | None]:
    return {want.core_so for placement in catalogue.placements for want in placement.wants}


class TestPlacements:
    def test_one_row_per_file_folds_every_core_that_declares_it(self, adapter, monkeypatch):
        answer = _answer(
            _core(core_so="mednafen_psx_libretro.so", requirements=(_requirement(file_name="scph5501.bin"),)),
            _core(
                core_so="swanstation_libretro.so",
                requirements=(_requirement(file_name="scph5501.bin", need="optional"),),
            ),
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        catalogue = adapter()

        assert [p.file_name for p in catalogue.placements] == ["scph5501.bin"]
        placement = catalogue.placements[0]
        assert {(w.core_so, w.required) for w in placement.wants} == {
            ("mednafen_psx_libretro", True),
            ("swanstation_libretro", False),
        }
        assert placement.required_by_any is True

    def test_core_identifiers_lose_the_so_extension(self, adapter, monkeypatch):
        """The plugin's whole core identifier space is the bare basename."""
        answer = _answer(_core(core_so="mgba_libretro.so", requirements=(_requirement(),)))
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert catalogue_cores(adapter()) == {"mgba_libretro"}

    def test_a_subdirectory_destination_survives_as_a_relative_placement(self, adapter, monkeypatch):
        answer = _answer(
            _core(
                core_so="dolphin_libretro.so",
                requirements=(
                    _requirement(
                        file_name="codehandler.bin",
                        declared="dolphin-emu/Sys/codehandler.bin",
                        path=f"{_ROOT}/dolphin-emu/Sys/codehandler.bin",
                    ),
                ),
            )
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().placements[0].relative_path == "dolphin-emu/Sys/codehandler.bin"

    def test_a_destination_outside_the_root_has_no_placement_to_honour(self, adapter, monkeypatch):
        answer = _answer(
            _core(
                core_so="melonds_libretro.so",
                requirements=(_requirement(file_name="bios7.bin", path="/home/deck/.local/share/melonDS/bios7.bin"),),
            )
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().placements[0].relative_path is None

    def test_a_declaration_the_root_symlinks_onto_itself_keeps_its_subdirectory(self, adapter, monkeypatch):
        """LRPS2 on RetroDECK: ``pcsx2/bios`` is a link back to the BIOS root.

        The resolved destination collapses onto the root, so deriving the
        location from it yields ``.`` and loses the folder the emulator will
        actually open. What the emulator spelled survives that.
        """
        answer = _answer(
            _core(
                core_so="pcsx2_libretro.so",
                requirements=(_requirement(file_name="bios", declared="pcsx2/bios", path=_ROOT),),
            )
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().placements[0].relative_path == "pcsx2/bios"

    def test_an_absolute_declaration_has_no_placement_under_our_root(self, adapter, monkeypatch):
        """A location stated as an address is not one this plugin can join under its own root."""
        answer = _answer(
            _core(
                core_so="duckstation_libretro.so",
                requirements=(
                    _requirement(file_name="scph5501.bin", declared=f"{_ROOT}/scph5501.bin", path=f"{_ROOT}/x.bin"),
                ),
            )
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().placements[0].relative_path is None

    def test_a_declaration_that_climbs_out_of_the_root_has_no_placement_under_it(self, adapter, monkeypatch):
        """A relative spelling can escape too, and the resolved path does not show it.

        ``pcsx2/../..`` names the root's grandparent while the resolved
        destination stays inside — RetroDECK's link makes the two disagree — so
        the declaration is checked on its own terms rather than trusted for
        having landed somewhere acceptable.
        """
        answer = _answer(
            _core(
                core_so="pcsx2_libretro.so",
                requirements=(_requirement(file_name="bios", declared="pcsx2/../..", path=_ROOT),),
            )
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().placements[0].relative_path is None


class TestDestinationReadings:
    """What the resolver read AT the destination, carried instead of re-derived."""

    def _placement(self, adapter, monkeypatch, requirement):
        answer = _answer(_core(core_so="mgba_libretro.so", requirements=(requirement,)))
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))
        return adapter().placements[0]

    def test_a_file_at_the_destination_is_present(self, adapter, monkeypatch):
        placement = self._placement(adapter, monkeypatch, _requirement(found=KIND_FILE, checked="unchecked"))

        assert placement.present is True
        assert placement.declared_kind == DECLARED_FILE
        assert placement.folder is None

    def test_nothing_at_the_destination_is_absent(self, adapter, monkeypatch):
        assert self._placement(adapter, monkeypatch, _requirement()).present is False

    def test_a_destination_that_could_not_be_looked_at_is_neither(self, adapter, monkeypatch):
        """ "Could not look" is not "not there", and the placement keeps them apart."""
        placement = self._placement(adapter, monkeypatch, _requirement(found=KIND_INACCESSIBLE))

        assert placement.present is None

    def test_a_directory_a_core_declares_is_named_by_the_declaration(self, adapter, monkeypatch):
        """The kind is what the core OPENS the path at, never what was found there."""
        placement = self._placement(
            adapter,
            monkeypatch,
            _requirement(
                file_name="bios",
                declared="pcsx2/bios",
                found=KIND_DIRECTORY,
                checked="unchecked",
                declared_kind=ATLAS_DECLARED_DIRECTORY,
            ),
        )

        assert placement.declared_kind == DECLARED_DIRECTORY
        assert placement.present is True

    def test_the_supplying_distribution_travels_as_the_resolver_writes_it(self, adapter, monkeypatch):
        """The resolver's own display form — the plugin never maps an identifier itself."""
        placement = self._placement(
            adapter,
            monkeypatch,
            _requirement(
                file_name="codehandler.bin",
                declared="dolphin-emu/Sys/codehandler.bin",
                path=f"{_ROOT}/dolphin-emu/Sys/codehandler.bin",
                found=KIND_FILE,
                checked="unchecked",
                supplied_by=SuppliedBy(distribution="retrodeck", source="/app/retrodeck/x", card_version="1"),
            ),
        )

        assert placement.supplied_by == "RetroDECK"

    def test_a_reading_at_a_destination_we_cannot_honour_does_not_travel(self, adapter, monkeypatch):
        """An emulator keeping its firmware in its own tree: that file is not the plugin's.

        With no location to honour the caller places the file by its own flat
        default, and what was read in the emulator's XDG tree says nothing
        about the BIOS root.
        """
        placement = self._placement(
            adapter,
            monkeypatch,
            _requirement(
                file_name="bios7.bin",
                path="/home/deck/.local/share/melonDS/bios7.bin",
                found=KIND_FILE,
                checked="unchecked",
                supplied_by=SuppliedBy(distribution="retrodeck", source="/app/x", card_version="1"),
            ),
        )

        assert placement.relative_path is None
        assert placement.present is None
        assert placement.folder is None
        assert placement.caveats == ()
        assert placement.supplied_by is None

    def test_no_stated_provenance_claims_nothing(self, adapter, monkeypatch):
        placement = self._placement(adapter, monkeypatch, _requirement(found=KIND_FILE, checked="unchecked"))

        assert placement.supplied_by is None

    def test_per_region_alternatives_all_reach_the_catalogue(self, adapter, monkeypatch):
        """Which region a launch picks is unknowable here, so every option is a declared want."""
        group = FirmwareAlternatives(
            options=(
                _requirement(file_name="scph5501.bin", regions=("NTSC-U",)),
                _requirement(file_name="scph5502.bin", regions=("PAL",)),
            )
        )
        answer = _answer(_core(core_so="duckstation_libretro.so", requirements=(group,)))
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert [p.file_name for p in adapter().placements] == ["scph5501.bin", "scph5502.bin"]

    def test_a_core_that_declares_nothing_contributes_nothing(self, adapter, monkeypatch):
        """gearboy, gearsystem and geargrafx: read, and asking for nothing."""
        answer = _answer(
            _core(core_so="gearboy_libretro.so"),
            _core(core_so="gearsystem_libretro.so"),
            _core(core_so="geargrafx_libretro.so"),
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        catalogue = adapter()
        assert catalogue.placements == ()
        assert catalogue.resolved is True
        assert catalogue.unread_cores == frozenset()


class TestUnreadCores:
    def test_an_unreadable_core_is_named(self, adapter, monkeypatch):
        answer = _answer(
            _core(core_so="mgba_libretro.so", requirements=(_requirement(),)),
            _core(core_so="fbalpha_libretro.so", declaration="unreadable"),
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().unread_cores == frozenset({"fbalpha_libretro"})

    def test_a_refused_declaration_counts_as_unread(self, adapter, monkeypatch):
        """The core does want something; the resolver just would not place it."""
        answer = _answer(
            _core(
                core_so="odd_libretro.so",
                refused=(RefusedDeclaration(declared="../escape.bin", need="required", reason="leaves-root"),),
            )
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().unread_cores == frozenset({"odd_libretro"})

    def test_an_unsupported_emulator_counts_as_unread(self, adapter, monkeypatch):
        answer = _answer(_core(core_so="standalone_libretro.so", declaration="unsupported"))
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().unread_cores == frozenset({"standalone_libretro"})

    def test_a_read_core_is_never_named(self, adapter, monkeypatch):
        answer = _answer(_core(core_so="mgba_libretro.so", requirements=(_requirement(),)))
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().unread_cores == frozenset()


class TestDegradation:
    def test_a_raising_resolver_answers_unresolved_not_empty(self, adapter, monkeypatch, traces):
        """The one failure mode that must never read as 'this platform needs none'."""
        monkeypatch.setattr(
            "adapters.atlas_firmware.detect",
            _detecting(_Installation(ValueError("FirmwareRequirement: need must be one of ..."))),
        )

        catalogue = adapter()

        assert catalogue.resolved is False
        assert catalogue.placements == ()
        assert catalogue.reading_complete_for(["mgba_libretro"]) is False
        assert any("resolver failed" in trace for trace in traces)

    def test_a_raising_detect_answers_unresolved(self, adapter, monkeypatch):
        def detect(home, machine=None):
            raise OSError("no such home")

        monkeypatch.setattr("adapters.atlas_firmware.detect", detect)

        assert adapter().resolved is False

    def test_no_installation_answers_unresolved(self, adapter, monkeypatch, traces):
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting())

        catalogue = adapter()

        assert catalogue.resolved is False
        assert any("no emulator installation" in trace for trace in traces)

    def test_an_answer_without_a_root_is_unresolved(self, adapter, monkeypatch):
        """No firmware root means no destination to resolve anything against."""
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(_answer(root=None))))

        catalogue = adapter()

        assert catalogue.resolved is False
        assert catalogue.caveats == ("firmware-root-unstated",)

    def test_the_first_detected_installation_answers(self, adapter, monkeypatch):
        """Detection orders its finds; the plugin takes the leader, never a merge."""
        first = _Installation(_answer(_core(requirements=(_requirement(file_name="first.bin"),))))
        second = _Installation(_answer(_core(requirements=(_requirement(file_name="second.bin"),))))
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(first, second))

        assert [p.file_name for p in adapter().placements] == ["first.bin"]


class TestCaveats:
    def test_stable_codes_travel_from_the_answer_and_from_each_core(self, adapter, monkeypatch):
        answer = _answer(
            _core(
                core_so="fbalpha_libretro.so",
                declaration="unreadable",
                caveats=(Caveat(code="core-info-unreadable", message="whatever this says may change"),),
            ),
            caveats=(Caveat(code="firmware-path-obstructed", message="a directory is in the way"),),
        )
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert set(adapter().caveats) == {"firmware-path-obstructed", "core-info-unreadable"}

    def test_the_trace_names_the_codes_and_the_arrangement(self, adapter, monkeypatch, traces):
        answer = _answer(_core(requirements=(_requirement(),)))
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        adapter()

        assert any("retrodeck" in trace and "requirements=1" in trace for trace in traces)


class TestVocabularyConformance:
    """The words ``domain/`` spells for itself are the resolver's own.

    ``domain/`` may not import the vendored resolver, so its copies of the
    declaration kinds and the caveat codes are a second spelling. Held equal
    here rather than trusted, so an upstream rename is a red test instead of a
    rule that quietly stops firing.
    """

    def test_the_declaration_kinds_match(self):
        assert (DECLARED_FILE, DECLARED_DIRECTORY) == (ATLAS_DECLARED_FILE, ATLAS_DECLARED_DIRECTORY)

    def test_the_obstruction_code_matches(self):
        assert CAVEAT_PATH_OBSTRUCTED == CAVEAT_FIRMWARE_PATH_OBSTRUCTED


class TestFolderVerdictsTheInventoryAlreadySettles:
    """A folder question the resolver answers without a content read costs none.

    Whether the unverified machine-wide reading settles a folder is its own
    three-valued answer, not a shape this adapter enumerates. What matters here
    is that a settled answer is carried — with the codes that speak for it — and
    that only an unsettled one is left for the verified per-core question, which
    opens and reads every candidate in the folder.
    """

    def _placement(self, adapter, monkeypatch, requirement, *caveats):
        answer = _answer(_core(core_so="pcsx2_libretro.so", requirements=(requirement,)), caveats=caveats)
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))
        return adapter().placements[0]

    def _folder(self, **kwargs):
        return _requirement(
            file_name="bios",
            declared="pcsx2/bios",
            path=f"{_ROOT}/pcsx2/bios",
            declared_kind=ATLAS_DECLARED_DIRECTORY,
            **kwargs,
        )

    def test_an_absent_folder_is_unmet_without_reading_a_byte(self, adapter, monkeypatch):
        placement = self._placement(adapter, monkeypatch, self._folder(found=KIND_MISSING))

        assert placement.declared_kind == DECLARED_DIRECTORY
        assert placement.folder is not None
        assert placement.folder.satisfied is False

    def test_a_file_where_the_core_lists_a_folder_is_unmet_too(self, adapter, monkeypatch):
        """A regular file has no inside, so the listing the core makes reaches nothing."""
        placement = self._placement(adapter, monkeypatch, self._folder(found=KIND_FILE, checked="unknown"))

        assert placement.folder is not None
        assert placement.folder.satisfied is False

    def test_a_settled_folder_carries_what_the_listing_found_in_it(self, adapter, monkeypatch):
        """The stat settles it, so this row is never asked again — its words come from here.

        A folder holding no file of a size the core would even open is answered
        without verification, and the code saying so names the folder as ``dir``
        rather than as ``path``. Dropped, the row renders red with no word at
        all, which is the ordinary state of a RetroDECK holding no PS2 image.
        """
        placement = self._placement(
            adapter,
            monkeypatch,
            self._folder(found=KIND_DIRECTORY, checked="unknown", contents_satisfied=False),
            Caveat(
                code=CAVEAT_FIRMWARE_DIRECTORY_HOLDS_NO_CANDIDATE,
                message="holds no file of a size this core accepts",
                data={"dir": f"{_ROOT}/pcsx2/bios", "core_so": "pcsx2_libretro.so", "need": "required"},
            ),
        )

        assert placement.folder is not None
        assert placement.folder.satisfied is False
        assert placement.caveats == (CAVEAT_FIRMWARE_DIRECTORY_HOLDS_NO_CANDIDATE,)

    def test_a_folder_the_stat_leaves_open_carries_none_of_the_unverified_words(self, adapter, monkeypatch):
        """The verified read replaces this answer, so its statement must not survive beside it.

        Carried on, the row would say its contents were not checked next to the
        verdict of the check.
        """
        placement = self._placement(
            adapter,
            monkeypatch,
            self._folder(found=KIND_DIRECTORY, checked="unknown"),
            Caveat(
                code=CAVEAT_FIRMWARE_SEARCH_UNVERIFIED,
                message="which of them is a BIOS is a question about their bytes",
                data={"dir": f"{_ROOT}/pcsx2/bios", "candidates": "3", "core_so": "pcsx2_libretro.so"},
            ),
        )

        assert placement.folder is None
        assert placement.caveats == ()

    def test_a_file_declaration_never_picks_up_a_listings_findings(self, adapter, monkeypatch):
        """On a linked root the listed folder IS the root, and so is this file's destination.

        RetroDECK points ``<bios>/pcsx2/bios`` back at ``<bios>``, so a
        declaration collapsing onto the root resolves to the same place the
        folder was listed at. Only a folder declaration is ever listed, which is
        what keeps a statement about that listing off this row.
        """
        placement = self._placement(
            adapter,
            monkeypatch,
            _requirement(core_so="pcsx2_libretro.so", file_name="stray.bin", path=_ROOT, found=KIND_MISSING),
            Caveat(
                code=CAVEAT_FIRMWARE_DIRECTORY_HOLDS_NO_CANDIDATE,
                message="holds no file of a size this core accepts",
                data={"dir": _ROOT, "core_so": "pcsx2_libretro.so", "need": "required"},
            ),
        )

        assert placement.declared_kind == DECLARED_FILE
        assert placement.caveats == ()


def _not_comparable(file_name: str) -> Caveat:
    """One core's report that the identity at the shared destination settles nothing."""
    return Caveat(
        code=CAVEAT_FIRMWARE_IDENTITY_NOT_COMPARABLE,
        message=f"{file_name}'s bytes differ from the pinned ones",
        data={
            "path": f"{_ROOT}/gba_bios.bin",
            "file_name": file_name,
            "archive_reason": "romset",
            "table_version": "6.0.0",
        },
    )


class TestDestinationCaveats:
    """What else the reading found at a row's own destination, in the resolver's codes."""

    def _placement(self, adapter, monkeypatch, requirement, *caveats):
        answer = _answer(_core(core_so="mgba_libretro.so", requirements=(requirement,)), caveats=caveats)
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))
        return adapter().placements[0]

    def test_a_caveat_naming_the_rows_destination_travels_with_the_row(self, adapter, monkeypatch):
        placement = self._placement(
            adapter,
            monkeypatch,
            _requirement(found=KIND_DIRECTORY, checked="unknown"),
            Caveat(
                code=CAVEAT_FIRMWARE_PATH_OBSTRUCTED,
                message="a directory is in the way",
                data={"path": f"{_ROOT}/gba_bios.bin"},
            ),
        )

        assert placement.caveats == (CAVEAT_FIRMWARE_PATH_OBSTRUCTED,)

    def test_a_caveat_about_another_destination_does_not(self, adapter, monkeypatch):
        placement = self._placement(
            adapter,
            monkeypatch,
            _requirement(found=KIND_FILE, checked="unchecked"),
            Caveat(code="firmware-unreadable", message="elsewhere", data={"path": f"{_ROOT}/other.bin"}),
        )

        assert placement.caveats == ()

    def test_two_caveats_at_one_destination_list_their_code_once(self, adapter, monkeypatch):
        """The row carries codes, so two findings about one place must not list one twice.

        Two cores whose declarations spell one file differently resolve to the
        same destination, and each states the identity it could not compare —
        one ``path``, two ``file_name`` values, so the resolver keeps both.
        """
        placement = self._placement(
            adapter,
            monkeypatch,
            _requirement(found=KIND_FILE, checked="not-comparable"),
            _not_comparable("gba_bios.bin"),
            _not_comparable("bios/gba_bios.bin"),
        )

        assert placement.caveats == (CAVEAT_FIRMWARE_IDENTITY_NOT_COMPARABLE,)


_FOLDER = f"{_ROOT}/pcsx2/bios"


def _image(name: str, description: str, *, code: str = CAVEAT_FIRMWARE_IMAGE_IDENTIFIED) -> Caveat:
    """One caveat of the folder read's image family, as the resolver states it."""
    return Caveat(
        code=code,
        message=f"{name} reads as a PS2 BIOS",
        data={
            "path": f"{_FOLDER}/{name}",
            "image": name,
            "md5": "d333558cc14561c1fdc334c75d5f37b7",
            "table": "6.0.0",
            "core_so": "pcsx2_libretro.so",
            "description": description,
        },
    )


def _folder_requirement(*, contents_satisfied: bool | None, found: PathKind = KIND_DIRECTORY):
    return _requirement(
        core_so="pcsx2_libretro.so",
        file_name="bios",
        declared="pcsx2/bios",
        path=_FOLDER,
        found=found,
        checked="unknown" if found in (KIND_DIRECTORY, KIND_FILE) else None,
        declared_kind=ATLAS_DECLARED_DIRECTORY,
        contents_satisfied=contents_satisfied,
    )


@pytest.fixture
def folder_adapter(traces):
    return AtlasFolderVerdictAdapter(user_home="/home/deck", log_debug=traces.append)


class TestFolderVerdictAdapter:
    """The verified per-core read: what the folder holds, and in whose words."""

    def _verdicts(self, folder_adapter, monkeypatch, answer, core_so="pcsx2_libretro"):
        installation = _Installation(answer)
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(installation))
        return folder_adapter(core_so), installation

    def test_the_question_is_asked_of_the_core_with_verification_on(self, folder_adapter, monkeypatch):
        """A folder verdict without a content check is not a verdict at all."""
        answer = _answer(
            _core(core_so="pcsx2_libretro.so", requirements=(_folder_requirement(contents_satisfied=True),))
        )

        _, installation = self._verdicts(folder_adapter, monkeypatch, answer)

        assert installation.asked_for == ("pcsx2_libretro.so", True)

    def test_an_identified_image_satisfies_the_folder_and_is_named(self, folder_adapter, monkeypatch):
        """The two halves count differently: a description per image, the code once."""
        answer = _answer(
            _core(core_so="pcsx2_libretro.so", requirements=(_folder_requirement(contents_satisfied=True),)),
            caveats=(
                _image("ps2-0200e-20040614.bin", "Europe  v02.00(14/06/2004)  Console 20040614-100914"),
                _image("ps2-0200j-20040614.bin", "Japan   v02.00(14/06/2004)  Console 20040614-100905"),
            ),
        )

        verdicts, _ = self._verdicts(folder_adapter, monkeypatch, answer)

        assert verdicts["bios"].satisfied is True
        assert verdicts["bios"].images == (
            "Europe  v02.00(14/06/2004)  Console 20040614-100914",
            "Japan   v02.00(14/06/2004)  Console 20040614-100905",
        )
        assert verdicts["bios"].caveats == (CAVEAT_FIRMWARE_IMAGE_IDENTIFIED,)

    def test_an_image_the_packaged_table_does_not_list_counts_all_the_same(self, folder_adapter, monkeypatch):
        """The table lists what System.dat lists; the core's own test is the verdict."""
        answer = _answer(
            _core(core_so="pcsx2_libretro.so", requirements=(_folder_requirement(contents_satisfied=True),)),
            caveats=(_image("dump.bin", "USA v01.60", code=CAVEAT_FIRMWARE_IMAGE_UNLISTED),),
        )

        verdicts, _ = self._verdicts(folder_adapter, monkeypatch, answer)

        assert verdicts["bios"].images == ("USA v01.60",)

    def test_a_folder_holding_no_image_is_unmet_and_says_which_code(self, folder_adapter, monkeypatch):
        answer = _answer(
            _core(core_so="pcsx2_libretro.so", requirements=(_folder_requirement(contents_satisfied=False),)),
            caveats=(
                Caveat(
                    code="firmware-directory-holds-no-image",
                    message="none of them reads as a PS2 BIOS",
                    data={"dir": _FOLDER, "candidates": "1", "core_so": "pcsx2_libretro.so"},
                ),
            ),
        )

        verdicts, _ = self._verdicts(folder_adapter, monkeypatch, answer)

        assert verdicts["bios"].satisfied is False
        assert verdicts["bios"].caveats == ("firmware-directory-holds-no-image",)
        assert verdicts["bios"].images == ()

    def test_a_contradiction_withholds_the_verdict_and_names_no_image(self, folder_adapter, monkeypatch):
        """The table names the bytes and the core's own test denies them."""
        answer = _answer(
            _core(core_so="pcsx2_libretro.so", requirements=(_folder_requirement(contents_satisfied=None),)),
            caveats=(
                Caveat(
                    code="firmware-image-contradicted",
                    message="the two reads disagree",
                    data={"path": f"{_FOLDER}/odd.bin", "image": "odd.bin", "core_so": "pcsx2_libretro.so"},
                ),
            ),
        )

        verdicts, _ = self._verdicts(folder_adapter, monkeypatch, answer)

        assert verdicts["bios"].satisfied is None
        assert verdicts["bios"].caveats == ("firmware-image-contradicted",)
        assert verdicts["bios"].images == ()

    def test_a_caveat_about_another_folder_stays_out(self, folder_adapter, monkeypatch):
        answer = _answer(
            _core(core_so="pcsx2_libretro.so", requirements=(_folder_requirement(contents_satisfied=True),)),
            caveats=(Caveat(code="firmware-scan-incomplete", message="elsewhere", data={"dir": f"{_ROOT}/other"}),),
        )

        verdicts, _ = self._verdicts(folder_adapter, monkeypatch, answer)

        assert verdicts["bios"].caveats == ()

    def test_a_file_declaration_gets_no_folder_verdict(self, folder_adapter, monkeypatch):
        """A verdict about contents nobody listed would be a state that lies."""
        answer = _answer(
            _core(
                core_so="pcsx2_libretro.so",
                requirements=(
                    _requirement(
                        core_so="pcsx2_libretro.so",
                        file_name="GameIndex.yaml",
                        found=KIND_FILE,
                        checked="unchecked",
                    ),
                ),
            )
        )

        verdicts, _ = self._verdicts(folder_adapter, monkeypatch, answer)

        assert verdicts == {}

    def test_a_raising_resolver_answers_no_verdict_rather_than_an_empty_folder(
        self, folder_adapter, monkeypatch, traces
    ):
        monkeypatch.setattr(
            "adapters.atlas_firmware.detect",
            _detecting(_Installation(ValueError("FirmwareRequirement: need must be one of ..."))),
        )

        assert folder_adapter("pcsx2_libretro") == {}
        assert any("verified folder read failed" in trace for trace in traces)

    def test_no_installation_answers_no_verdict(self, folder_adapter, monkeypatch):
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting())

        assert folder_adapter("pcsx2_libretro") == {}

    def test_an_answer_without_a_root_answers_no_verdict(self, folder_adapter, monkeypatch):
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(_answer(root=None))))

        assert folder_adapter("pcsx2_libretro") == {}
