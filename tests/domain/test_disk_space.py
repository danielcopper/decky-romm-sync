"""Whether a download fits — the arithmetic the pre-flight refuses on.

The refusal costs the user a download they asked for, so the cases that matter
are the boundary (exactly enough is enough) and every claim that must be counted
against the free space the store reported.
"""

from domain.disk_space import disk_space_verdict

_MB = 1024 * 1024
_HEADROOM = 100 * _MB


class TestDiskSpaceVerdict:
    def test_a_rom_that_fits_with_headroom_to_spare_fits(self):
        verdict = disk_space_verdict(file_size=500 * _MB, free_space=700 * _MB, reserved_bytes=0, multi_file=False)
        assert verdict.fits is True

    def test_the_headroom_is_required_on_top_of_the_rom(self):
        # 500 MB free for a 500 MB ROM is not enough: an emulator writes saves
        # and shader caches beside it.
        verdict = disk_space_verdict(file_size=500 * _MB, free_space=500 * _MB, reserved_bytes=0, multi_file=False)
        assert verdict.fits is False
        assert verdict.needed_bytes == 500 * _MB + _HEADROOM

    def test_exactly_enough_fits(self):
        verdict = disk_space_verdict(
            file_size=500 * _MB, free_space=500 * _MB + _HEADROOM, reserved_bytes=0, multi_file=False
        )
        assert verdict.fits is True

    def test_one_byte_short_does_not_fit(self):
        verdict = disk_space_verdict(
            file_size=500 * _MB, free_space=500 * _MB + _HEADROOM - 1, reserved_bytes=0, multi_file=False
        )
        assert verdict.fits is False

    def test_a_multi_file_rom_is_counted_twice(self):
        # It lands as a ZIP and is extracted beside it, so both exist at once.
        verdict = disk_space_verdict(file_size=500 * _MB, free_space=700 * _MB, reserved_bytes=0, multi_file=True)
        assert verdict.fits is False
        assert verdict.needed_bytes == 1000 * _MB + _HEADROOM

    def test_bytes_another_download_reserved_are_not_available(self):
        # Without this, two concurrent pre-flights each pass on space only one of
        # them fits (#1053).
        verdict = disk_space_verdict(
            file_size=500 * _MB, free_space=700 * _MB, reserved_bytes=300 * _MB, multi_file=False
        )
        assert verdict.fits is False

    def test_bytes_already_on_disk_are_not_needed_twice(self):
        # A resumed transfer only needs the remainder, so a near-complete resume
        # is not refused for the full size.
        verdict = disk_space_verdict(
            file_size=500 * _MB,
            free_space=200 * _MB,
            reserved_bytes=0,
            multi_file=False,
            already_on_disk=450 * _MB,
        )
        assert verdict.fits is True
        assert verdict.needed_bytes == 150 * _MB

    def test_more_on_disk_than_needed_never_goes_negative(self):
        verdict = disk_space_verdict(
            file_size=10 * _MB, free_space=0, reserved_bytes=0, multi_file=False, already_on_disk=10_000 * _MB
        )
        assert verdict.needed_bytes == 0
        assert verdict.fits is True

    def test_a_rom_of_unstated_size_always_fits(self):
        # Refusing on a number the server never sent would be a claim the plugin
        # cannot make.
        verdict = disk_space_verdict(file_size=0, free_space=0, reserved_bytes=0, multi_file=False)
        assert verdict.fits is True

    def test_free_space_is_reported_after_the_reservations(self):
        verdict = disk_space_verdict(
            file_size=900 * _MB, free_space=700 * _MB, reserved_bytes=300 * _MB, multi_file=False
        )
        assert verdict.free_mb == 400

    def test_free_space_never_reads_as_negative(self):
        # Reservations can exceed what the store reports once a sibling is
        # mid-write; "-200MB free" is not a sentence to show anyone.
        verdict = disk_space_verdict(
            file_size=900 * _MB, free_space=100 * _MB, reserved_bytes=300 * _MB, multi_file=False
        )
        assert verdict.free_mb == 0

    def test_the_needed_figure_is_stated_in_the_unit_the_refusal_uses(self):
        verdict = disk_space_verdict(file_size=500 * _MB, free_space=0, reserved_bytes=0, multi_file=False)
        assert verdict.needed_mb == 600
