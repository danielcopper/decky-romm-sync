"""Tests for the firmware-want vocabulary and its one classification."""

from __future__ import annotations

import pytest

from domain.firmware_wants import (
    DECLARED_DIRECTORY,
    WANTED_NEEDED,
    WANTED_NOT_NEEDED,
    WANTED_OPTIONAL,
    WANTED_UNKNOWN,
    FirmwareCatalogue,
    FirmwarePlacement,
    FirmwareWant,
    FolderVerdict,
    classify_wanted,
    merge_folder_verdicts,
    unanswered_folder_cores,
)


def _placement(file_name: str, *wants: FirmwareWant, relative_path: str | None = None) -> FirmwarePlacement:
    return FirmwarePlacement(
        file_name=file_name,
        relative_path=relative_path if relative_path is not None else file_name,
        description=f"{file_name} (BIOS)",
        wants=wants,
    )


def _catalogue(
    *placements: FirmwarePlacement,
    unread: frozenset[str] = frozenset(),
    resolved: bool = True,
) -> FirmwareCatalogue:
    return FirmwareCatalogue(placements=placements, unread_cores=unread, resolved=resolved)


class TestRequiredByAny:
    def test_required_by_one_core_is_required(self):
        placement = _placement(
            "scph5501.bin",
            FirmwareWant(core_so="swanstation_libretro", required=False),
            FirmwareWant(core_so="mednafen_psx_libretro", required=True),
        )
        assert placement.required_by_any is True

    def test_optional_everywhere_is_not_required(self):
        placement = _placement(
            "dc_boot.bin",
            FirmwareWant(core_so="flycast_libretro", required=False),
        )
        assert placement.required_by_any is False


class TestClassifyWanted:
    def test_required_by_any_core_is_needed(self):
        placement = _placement("codehandler.bin", FirmwareWant(core_so="dolphin_libretro", required=True))
        assert classify_wanted(placement, complete=True) == WANTED_NEEDED

    def test_declared_but_never_required_is_optional(self):
        placement = _placement("dc_boot.bin", FirmwareWant(core_so="flycast_libretro", required=False))
        assert classify_wanted(placement, complete=True) == WANTED_OPTIONAL

    def test_a_needed_file_stays_needed_on_an_incomplete_reading(self):
        """A match is a match — the reading state only ever decides an ABSENCE."""
        placement = _placement("codehandler.bin", FirmwareWant(core_so="dolphin_libretro", required=True))
        assert classify_wanted(placement, complete=False) == WANTED_NEEDED

    def test_no_placement_on_a_complete_reading_is_not_needed(self):
        assert classify_wanted(None, complete=True) == WANTED_NOT_NEEDED

    def test_no_placement_on_an_incomplete_reading_is_unknown(self):
        """The distinction the collapsed boolean could not express."""
        assert classify_wanted(None, complete=False) == WANTED_UNKNOWN


class TestReadingCompleteFor:
    def test_scope_with_no_unread_core_is_complete(self):
        catalogue = _catalogue(unread=frozenset({"fbalpha_libretro"}))
        assert catalogue.reading_complete_for(["mgba_libretro", "gambatte_libretro"]) is True

    def test_an_unread_core_inside_the_scope_blocks_completeness(self):
        catalogue = _catalogue(unread=frozenset({"fbalpha_libretro"}))
        assert catalogue.reading_complete_for(["fbalpha_libretro", "mame_libretro"]) is False

    def test_an_unread_core_outside_the_scope_does_not(self):
        """One unreadable core anywhere must not silence every platform's answer."""
        catalogue = _catalogue(unread=frozenset({"gearlynx_libretro"}))
        assert catalogue.reading_complete_for(["mednafen_psx_libretro"]) is True

    def test_an_unknown_scope_is_never_complete(self):
        catalogue = _catalogue()
        assert catalogue.reading_complete_for(None) is False

    def test_an_unresolved_reading_is_never_complete(self):
        """A scope that would otherwise be complete, so the ``resolved`` gate is what answers."""
        catalogue = _catalogue(resolved=False)
        assert catalogue.reading_complete_for(["mgba_libretro"]) is False

    def test_an_empty_scope_is_never_complete(self):
        """Vacuously every core was asked — and that is exactly the trap.

        A platform ES-DE offers no libretro core for (35 of its 172 systems,
        ``ps3`` among them) yields an empty scope. Answering ``True`` would let
        every server file classify ``not_needed`` and the platform read a green
        "Nothing required" off asking nobody, which is the collapse the
        four-valued vocabulary exists to prevent. Asking no one establishes
        nothing, so an empty scope answers like ``None``.
        """
        catalogue = _catalogue(unread=frozenset({"fbalpha_libretro"}))
        assert catalogue.reading_complete_for([]) is False


class TestByFileName:
    def test_indexes_every_placement(self):
        catalogue = _catalogue(
            _placement("a.bin", FirmwareWant(core_so="one_libretro", required=True)),
            _placement("b.bin", FirmwareWant(core_so="two_libretro", required=False)),
        )
        index = catalogue.by_file_name()
        assert set(index) == {"a.bin", "b.bin"}
        assert index["a.bin"].required_by_any is True

    def test_empty_catalogue_indexes_to_nothing(self):
        assert _catalogue().by_file_name() == {}


@pytest.mark.parametrize(
    ("complete", "expected"),
    [(True, WANTED_NOT_NEEDED), (False, WANTED_UNKNOWN)],
)
def test_absence_is_classified_by_the_reading_state_alone(complete, expected):
    assert classify_wanted(None, complete=complete) == expected


def _folder(*wants: FirmwareWant, verdict: FolderVerdict | None = None, caveats=()) -> FirmwarePlacement:
    return FirmwarePlacement(
        file_name="bios",
        relative_path="pcsx2/bios",
        description="'pcsx2/bios' folder",
        wants=wants,
        declared_kind=DECLARED_DIRECTORY,
        caveats=caveats,
        folder=verdict,
    )


_LRPS2 = FirmwareWant(core_so="pcsx2_libretro", required=True)


class TestUnansweredFolderCores:
    def test_a_core_whose_folder_row_is_open_is_named(self):
        placements = {"bios": _folder(_LRPS2)}

        assert unanswered_folder_cores(placements, ["pcsx2_libretro", "mgba_libretro"]) == ("pcsx2_libretro",)

    def test_a_folder_the_reading_already_settled_is_not_asked_about(self):
        placements = {"bios": _folder(_LRPS2, verdict=FolderVerdict(satisfied=False))}

        assert unanswered_folder_cores(placements, ["pcsx2_libretro"]) == ()

    def test_a_core_outside_the_scope_is_not_asked_about(self):
        placements = {"bios": _folder(_LRPS2)}

        assert unanswered_folder_cores(placements, ["mgba_libretro"]) == ()

    def test_an_unestablished_scope_asks_nobody(self):
        placements = {"bios": _folder(_LRPS2)}

        assert unanswered_folder_cores(placements, None) == ()

    def test_a_file_declaration_never_puts_its_cores_on_the_list(self):
        placements = {"gba_bios.bin": _placement("gba_bios.bin", FirmwareWant(core_so="mgba_libretro", required=True))}

        assert unanswered_folder_cores(placements, ["mgba_libretro"]) == ()

    def test_one_core_named_twice_is_asked_about_once(self):
        """An ES-DE catalogue can list one core under two entries for a system."""
        placements = {"bios": _folder(_LRPS2, _LRPS2)}

        assert unanswered_folder_cores(placements, ["pcsx2_libretro", "pcsx2_libretro"]) == ("pcsx2_libretro",)


class TestMergeFolderVerdicts:
    def test_the_verdict_lands_on_its_row(self):
        placements = {"bios": _folder(_LRPS2)}

        merged = merge_folder_verdicts(placements, {"bios": FolderVerdict(satisfied=True, images=("Europe",))})

        assert merged["bios"].folder == FolderVerdict(satisfied=True, images=("Europe",))

    def test_the_verdicts_caveats_join_the_destinations(self):
        """Both are statements about one place, and both are true."""
        placements = {"bios": _folder(_LRPS2, caveats=("firmware-scan-incomplete",))}

        merged = merge_folder_verdicts(
            placements, {"bios": FolderVerdict(satisfied=None, caveats=("firmware-image-contradicted",))}
        )

        assert merged["bios"].caveats == ("firmware-scan-incomplete", "firmware-image-contradicted")

    def test_a_verdict_for_a_file_declaration_is_dropped(self):
        """The declaration decides what the emulator opens; a folder verdict cannot override it."""
        placements = {"gba_bios.bin": _placement("gba_bios.bin", FirmwareWant(core_so="mgba_libretro", required=True))}

        merged = merge_folder_verdicts(placements, {"gba_bios.bin": FolderVerdict(satisfied=True)})

        assert merged["gba_bios.bin"].folder is None

    def test_a_verdict_for_a_row_that_is_not_there_is_dropped(self):
        assert merge_folder_verdicts({}, {"bios": FolderVerdict(satisfied=True)}) == {}

    def test_the_rows_beside_it_are_untouched(self):
        beside = _placement("GameIndex.yaml", FirmwareWant(core_so="pcsx2_libretro", required=True))
        placements = {"bios": _folder(_LRPS2), "GameIndex.yaml": beside}

        merged = merge_folder_verdicts(placements, {"bios": FolderVerdict(satisfied=True)})

        assert merged["GameIndex.yaml"] is beside
