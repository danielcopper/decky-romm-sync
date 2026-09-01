"""Tests for the atlas firmware adapter — the translation into plugin vocabulary.

What is under test is the adapter's own work: folding the resolver's per-core
answer into one row per file, naming the cores that could not be asked, turning
an absolute destination back into a placement under the firmware root, and
refusing to turn any failure into "nothing needed". The resolver's own decisions
are upstream's and are not re-tested here.

Answers are built from real atlas value objects rather than mocks, so every
invariant the resolver enforces on its own shapes is enforced on the fixtures
too — a fixture that could not come off a real machine fails to construct.
"""

from __future__ import annotations

import pytest
from _vendor.atlas.firmware import (
    CoreDeclarationState,
    CoreFirmware,
    FirmwareAlternatives,
    FirmwareAnswer,
    FirmwareNeed,
    FirmwareRequirement,
    RefusedDeclaration,
)
from _vendor.atlas.machine import KIND_MISSING
from _vendor.atlas.placement import Caveat

from adapters.atlas_firmware import AtlasFirmwareAdapter

_ROOT = "/home/deck/retrodeck/bios"


def _requirement(
    *,
    core_so: str | None = "mgba_libretro.so",
    file_name: str = "gba_bios.bin",
    path: str | None = None,
    need: FirmwareNeed = "required",
    description: str = "",
    regions: tuple[str, ...] | None = None,
) -> FirmwareRequirement:
    return FirmwareRequirement(
        core_so=core_so,
        system="gba",
        system_source="systemname",
        need=need,
        file_name=file_name,
        path=path if path is not None else f"{_ROOT}/{file_name}",
        declared=file_name,
        description=description or f"{file_name} (BIOS)",
        identity=None,
        found=KIND_MISSING,
        checked=None,
        regions=regions,
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
                    _requirement(file_name="codehandler.bin", path=f"{_ROOT}/dolphin-emu/Sys/codehandler.bin"),
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

    def test_the_root_itself_is_not_a_relative_placement(self, adapter, monkeypatch):
        """A core declaring the firmware folder states a directory, not a file below it."""
        answer = _answer(_core(core_so="pcsx2_libretro.so", requirements=(_requirement(file_name="bios", path=_ROOT),)))
        monkeypatch.setattr("adapters.atlas_firmware.detect", _detecting(_Installation(answer)))

        assert adapter().placements[0].relative_path is None

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
