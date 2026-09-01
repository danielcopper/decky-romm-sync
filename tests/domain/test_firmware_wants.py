"""Tests for the firmware-want vocabulary and its one classification."""

from __future__ import annotations

import pytest

from domain.firmware_wants import (
    WANTED_NEEDED,
    WANTED_NOT_NEEDED,
    WANTED_OPTIONAL,
    WANTED_UNKNOWN,
    FirmwareCatalogue,
    FirmwarePlacement,
    FirmwareWant,
    classify_wanted,
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
        catalogue = _catalogue(resolved=False)
        assert catalogue.reading_complete_for([]) is False

    def test_an_empty_scope_on_a_resolved_reading_is_complete(self):
        catalogue = _catalogue(unread=frozenset({"fbalpha_libretro"}))
        assert catalogue.reading_complete_for([]) is True


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
